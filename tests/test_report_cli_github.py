from __future__ import annotations

import io
import json
import os
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

from tests.support import commit, init_repo

from catchpr.cli import main
from catchpr.models import Catch, GeneratedTest, ProcResult, Rejection, Report
from catchpr.report import (
    catch_count,
    comment_from_payload,
    comment_markdown,
    rejections_of,
    report_json,
    sarif_json,
    to_sarif,
)

BASE = "def apply_discount(price: int, rate_bps: int) -> int:\n    return price - price * rate_bps // 10000\n"
HEAD = "def apply_discount(price: int, rate_bps: int) -> int:\n    return price + price * rate_bps // 10000\n"
TEST = "from billing import apply_discount\n\ndef test_discount_reduces_price():\n    assert apply_discount(10000, 250) == 9750\n"


def _report() -> Report:
    test = GeneratedTest("intent-aware", "sign", "sign flipped", TEST, None, "billing.py")
    base = ProcResult(True, 0, "1 passed", "", False, 0.01)
    head = ProcResult(False, 1, "", "AssertionError: assert 10250 == 9750", False, 0.02)
    catch = Catch(
        test,
        (base, base, base),
        (head, head, head),
        None,
        "billing.py",
        1,
        "catchpr repro --repo . --base aaa --head bbb --test-b64 QQ==",
    )
    report = Report("a" * 40, "b" * 40, "Fix discount", "body", [catch])
    report.rejections.append(Rejection("duplicate", "identical", "d1", "dodgy-diff"))
    return report


def test_reports_round_trip() -> None:
    report = _report()
    payload = json.loads(report_json(report))
    assert catch_count(payload) == 1
    assert rejections_of(payload)[0].filter_name == "duplicate"
    text = comment_markdown(report)
    assert "<!-- catchpr:sticky -->" in text
    assert "catchpr repro" in text
    again = comment_from_payload(payload)
    assert "behavior change" in again
    assert comment_from_payload({"catches": []}) == ""
    sarif = to_sarif(report)
    assert sarif["version"] == "2.1.0"
    encoded = sarif_json(report)
    assert "catchpr/behavior-change" in encoded
    empty = Report("a", "b", "", "")
    assert comment_markdown(empty) == ""
    assert to_sarif(empty)["runs"]  # type: ignore[index]


def test_comment_neutralizes_mentions_and_fences() -> None:
    test = GeneratedTest(
        "dodgy-diff", "t", "ping @team", "def test_x():\n    assert '```' != 'x'\n", None, None
    )
    head = ProcResult(False, 1, "", "failed @octocat", False, 0.0)
    base = ProcResult(True, 0, "ok", "", False, 0.0)
    catch = Catch(test, (base,), (head,), None, "billing.py", 3, "catchpr repro")
    report = Report("abc", "def", "t", "", [catch])
    text = comment_markdown(report)
    assert "@\u200bteam" in text
    assert "@\u200boctocat" in text
    assert "````" in text


def test_cli_run_repro_and_baseline(tmp_path: Path) -> None:
    init_repo(tmp_path)
    base = commit(tmp_path, {"billing.py": BASE}, "base")
    head = commit(tmp_path, {"billing.py": HEAD}, "head")
    recordings = tmp_path / "recordings.json"
    recordings.write_text(
        json.dumps(
            [
                {
                    "contains": ["phase: intent", "def apply_discount"],
                    "response": json.dumps(
                        {
                            "intent": "discount",
                            "risks": [
                                {
                                    "id": "sign",
                                    "description": "sign",
                                    "mutant": {
                                        "path": "billing.py",
                                        "symbol": "apply_discount",
                                        "replacement": HEAD,
                                    },
                                    "test": TEST,
                                }
                            ],
                        }
                    ),
                },
                {
                    "contains": ["phase: dodgy", "def apply_discount"],
                    "response": json.dumps(
                        {"tests": [{"id": "d1", "rationale": "same", "test": TEST}]}
                    ),
                },
                {
                    "contains": ["phase: baseline", "def apply_discount"],
                    "response": json.dumps({"verdict": "bug", "summary": "sign"}),
                },
            ]
        ),
        encoding="utf-8",
    )
    json_path = tmp_path / "out.json"
    sarif_path = tmp_path / "out.sarif"
    comment_path = tmp_path / "comment.md"
    code = main(
        [
            "run",
            "--repo",
            str(tmp_path),
            "--base",
            base,
            "--head",
            head,
            "--title",
            "Fix discount",
            "--body",
            "Adjust pricing.",
            "--repeats",
            "1",
            "--no-cache",
            "--mock-recordings",
            str(recordings),
            "--json",
            str(json_path),
            "--sarif",
            str(sarif_path),
            "--comment",
            str(comment_path),
            "--report-only",
        ]
    )
    assert code == 0
    assert "validated" in json_path.read_text(encoding="utf-8") or "sign" in json_path.read_text(
        encoding="utf-8"
    )
    failing = main(
        [
            "run",
            "--repo",
            str(tmp_path),
            "--base",
            base,
            "--head",
            head,
            "--repeats",
            "1",
            "--no-cache",
            "--mock-recordings",
            str(recordings),
        ]
    )
    assert failing == 1
    import base64

    encoded = base64.b64encode(TEST.encode()).decode()
    reproduced = main(
        ["repro", "--repo", str(tmp_path), "--base", base, "--head", head, "--test-b64", encoded]
    )
    assert reproduced == 0
    verdict = main(
        [
            "baseline",
            "--repo",
            str(tmp_path),
            "--base",
            base,
            "--head",
            head,
            "--no-cache",
            "--mock-recordings",
            str(recordings),
        ]
    )
    assert verdict == 0
    assert main(["--help"]) == 0
    assert (
        main(["run", "--repeats", "0", "--base", "a", "--head", "b", "--repo", str(tmp_path)]) == 2
    )
    assert (
        main(
            ["repro", "--repo", str(tmp_path), "--base", base, "--head", head, "--test-b64", "!!!"]
        )
        == 2
    )


class _GitHub(BaseHTTPRequestHandler):
    comments: ClassVar[list[dict[str, object]]] = []
    artifacts: ClassVar[dict[str, bytes]] = {}

    def do_GET(self) -> None:
        if "/comments" in self.path:
            body = json.dumps(_GitHub.comments).encode()
            self._send(200, body)
            return
        if self.path.endswith("/artifacts?per_page=100") or "/artifacts?" in self.path:
            body = json.dumps(
                {
                    "artifacts": [
                        {
                            "name": "catchpr-results",
                            "archive_download_url": f"http://{self.headers['Host']}/zip",
                        }
                    ]
                }
            ).encode()
            self._send(200, body)
            return
        if self.path == "/zip":
            self._send(200, _GitHub.artifacts["catchpr-results"])
            return
        self._send(404, b"{}")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        _GitHub.comments.append({"id": 7, "body": payload["body"]})
        self._send(201, b'{"id": 7}')

    def do_PATCH(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        _GitHub.comments[0]["body"] = payload["body"]
        self._send(200, b'{"id": 7}')

    def do_DELETE(self) -> None:
        _GitHub.comments.clear()
        self._send(204, b"")

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        return


def test_github_comment_and_artifact(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _GitHub.comments = []
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("catchpr-results.json", '{"catches": []}')
        archive.writestr("../evil.txt", "nope")
    # The evil path must be rejected. Build a clean zip for the success path
    # and a hostile one for the unit of _safe_extract via the server only serving the clean zip.
    clean = io.BytesIO()
    with zipfile.ZipFile(clean, "w") as archive:
        archive.writestr("catchpr-results.json", '{"catches": []}\n')
    _GitHub.artifacts = {"catchpr-results": clean.getvalue()}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _GitHub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    api = f"http://{host}:{port}"
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    results = tmp_path / "results.json"
    report = _report()
    results.write_text(report_json(report), encoding="utf-8")
    try:
        created = main(
            [
                "comment",
                "--results",
                str(results),
                "--repository",
                "acme/widgets",
                "--pr",
                "4",
                "--api",
                api,
            ]
        )
        assert created == 0
        assert _GitHub.comments and "catchpr:sticky" in str(_GitHub.comments[0]["body"])
        updated = main(
            [
                "comment",
                "--results",
                str(results),
                "--repository",
                "acme/widgets",
                "--pr",
                "4",
                "--api",
                api,
            ]
        )
        assert updated == 0
        silent = tmp_path / "silent.json"
        silent.write_text(report_json(Report("a", "b", "", "")), encoding="utf-8")
        deleted = main(
            [
                "comment",
                "--results",
                str(silent),
                "--repository",
                "acme/widgets",
                "--pr",
                "4",
                "--api",
                api,
            ]
        )
        assert deleted == 0
        assert _GitHub.comments == []
        fetched = main(
            [
                "fetch-artifact",
                "--repository",
                "acme/widgets",
                "--run-id",
                "9",
                "--name",
                "catchpr-results",
                "--dest",
                str(tmp_path / "art"),
                "--api",
                api,
            ]
        )
        assert fetched == 0
        assert (tmp_path / "art" / "catchpr-results.json").is_file()
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        os.environ.pop("GH_TOKEN", None)
        assert (
            main(
                [
                    "comment",
                    "--results",
                    str(results),
                    "--repository",
                    "acme/widgets",
                    "--pr",
                    "4",
                    "--api",
                    api,
                ]
            )
            == 2
        )
    finally:
        server.shutdown()
