"""Cover error paths, assessors, and provider edges without new product behavior."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
from tests.support import commit, init_repo

from catchpr.baseline import baseline_review
from catchpr.cli import main
from catchpr.diff import analyze_repo
from catchpr.errors import GitError, GitHubError, ParseError, ProviderError
from catchpr.generate import generate_dodgy, generate_intent, validate_test_source
from catchpr.github_api import _safe_extract, fetch_artifact, post_sticky
from catchpr.gitutil import ensure_repo, git, show_file
from catchpr.jsonutil import as_dict, parse_model_json, require_dict
from catchpr.models import DiffAnalysis, Mutant, Usage
from catchpr.mutate import replace_symbol
from catchpr.pipeline import RunConfig, run_pipeline
from catchpr.providers.base import Recording
from catchpr.providers.cache import CachingProvider
from catchpr.providers.http import OpenAICompatibleProvider, extract_message
from catchpr.providers.mock import MockProvider, prompt_hash
from catchpr.report import comment_from_payload
from catchpr.runner import Sandbox, _clip, _kill_group, _scrub_env, unshare_available

BASE = (
    "def apply_discount(price: int, rate_bps: int) -> int:\n"
    "    return price - price * rate_bps // 10000\n"
)
HEAD = (
    "def apply_discount(price: int, rate_bps: int) -> int:\n"
    "    return price + price * rate_bps // 10000\n"
)
TEST = (
    "from billing import apply_discount\n\n"
    "def test_discount_reduces_price():\n"
    "    assert apply_discount(10000, 250) == 9750\n"
)


def _pair(tmp_path: Path) -> tuple[str, str]:
    init_repo(tmp_path)
    base = commit(tmp_path, {"billing.py": BASE}, "base")
    head = commit(tmp_path, {"billing.py": HEAD}, "head")
    return base, head


def test_validate_and_generators_reject_bad_model_output() -> None:
    assert validate_test_source("def nope():\n    return 1\n") == "no test_ function"
    assert validate_test_source("def test_(: \n")
    assert "banned import" in (
        validate_test_source("import socket\ndef test_a():\n    pass\n") or ""
    )
    assert "banned call" in (validate_test_source("def test_a():\n    eval('1')\n") or "")
    assert "banned call" in (
        validate_test_source("import os\ndef test_a():\n    os.system('true')\n") or ""
    )
    analysis = DiffAnalysis(".", "a", "b", "title", "body", ())
    empty = MockProvider([Recording(("phase: intent",), "not json")])
    assert generate_intent(analysis, empty) == []
    dodgy = MockProvider([Recording(("phase: dodgy",), '{"tests": [{"test": ""}]}')])
    assert generate_dodgy(analysis, dodgy) == []
    follow = MockProvider(
        [
            Recording(
                ("phase: intent",),
                json.dumps(
                    {
                        "intent": "x",
                        "risks": [
                            {
                                "description": "missed a sign",
                                "mutant": {
                                    "path": "billing.py",
                                    "symbol": "apply_discount",
                                    "replacement": HEAD,
                                },
                            },
                            {"id": "skip", "description": "no mutant"},
                            "not-an-object",
                        ],
                    }
                ),
            ),
            Recording(("phase: mutant-test",), '{"test": "' + TEST.replace("\n", "\\n") + '"}'),
        ]
    )
    # The second recording is not valid if we hand-escape poorly. Use a real JSON response.
    follow = MockProvider(
        [
            Recording(
                ("phase: intent",),
                json.dumps(
                    {
                        "intent": "x",
                        "risks": [
                            {
                                "description": "missed a sign",
                                "mutant": {
                                    "path": "billing.py",
                                    "symbol": "apply_discount",
                                    "replacement": HEAD,
                                },
                            }
                        ],
                    }
                ),
            ),
            Recording(("phase: mutant-test",), json.dumps({"test": TEST})),
        ]
    )
    generated = generate_intent(analysis, follow)
    assert generated and generated[0].source.startswith("from billing")
    broken_follow = MockProvider(
        [
            Recording(
                ("phase: intent",),
                json.dumps(
                    {
                        "intent": "x",
                        "risks": [
                            {
                                "mutant": {
                                    "path": "billing.py",
                                    "symbol": "apply_discount",
                                    "replacement": HEAD,
                                }
                            }
                        ],
                    }
                ),
            ),
            Recording(("phase: mutant-test",), "nope"),
        ]
    )
    assert generate_intent(analysis, broken_follow) == []
    partial = MockProvider([Recording(("phase: dodgy",), '{"tests": [{"rationale": "because"}]}')])
    assert generate_dodgy(analysis, partial) == []


def test_json_git_and_baseline(tmp_path: Path) -> None:
    with pytest.raises(ParseError):
        parse_model_json("no object")
    with pytest.raises(ParseError):
        parse_model_json("{")
    with pytest.raises(ParseError):
        require_dict([], "payload")
    assert as_dict({"a": 1, 2: "skip"}) == {"a": 1}
    bare = tmp_path / "bare"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    with pytest.raises(GitError):
        ensure_repo(bare)
    with pytest.raises(GitError):
        git(tmp_path, "rev-parse", "definitely-missing")
    init_repo(tmp_path / "repo")
    sha = commit(tmp_path / "repo", {"billing.py": BASE, "blob.bin": "a\x00b"}, "base")
    assert show_file(tmp_path / "repo", sha, "missing.py") is None
    assert show_file(tmp_path / "repo", sha, "blob.bin") is None
    analysis = analyze_repo(tmp_path / "repo", sha, sha, title="t", body="b")
    provider = MockProvider([Recording(("phase: baseline",), "not-json")])
    assert baseline_review(analysis, provider)[0] == "clean"
    other = MockProvider(
        [Recording(("phase: baseline",), '{"verdict": "maybe", "summary": "unsure"}')]
    )
    assert baseline_review(analysis, other) == ("clean", "unsure")


def test_sandbox_edges_and_env_scrub(tmp_path: Path) -> None:
    base, head = _pair(tmp_path)
    sandbox = Sandbox(tmp_path, base, head)
    with pytest.raises(RuntimeError):
        sandbox.run_base("def test_a():\n    assert True\n")
    with pytest.raises(RuntimeError):
        sandbox.run_head("def test_a():\n    assert True\n")
    with pytest.raises(RuntimeError):
        sandbox.run_mutant(
            Mutant("billing.py", "apply_discount", HEAD), "def test_a():\n    pass\n"
        )
    with Sandbox(tmp_path, base, head, block_network=False) as opened:
        missing = opened.run_mutant(Mutant("missing.py", "f", "def f():\n    return 1\n"), TEST)
        assert "mutant path missing" in missing.stderr
        bad = opened.run_mutant(
            Mutant("billing.py", "missing", "def missing():\n    return 1\n"), TEST
        )
        assert "mutant not applied" in bad.stderr
        assert opened.run_base(TEST).passed
    scrubbed = _scrub_env(
        {"GROQ_API_KEY": "secret", "GITHUB_TOKEN": "t", "PYTHONPATH": "/already", "OK": "1"},
        Path("/guard"),
        Path("/tree"),
    )
    assert "GROQ_API_KEY" not in scrubbed
    assert "GITHUB_TOKEN" not in scrubbed
    assert "/already" in scrubbed["PYTHONPATH"]
    assert _clip("x" * 30_000).endswith("truncated ...")
    _kill_group(2**31 - 1)
    assert replace_symbol("def empty():\n    return 1\n", "empty", "   \n")


def test_unshare_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("catchpr.runner.shutil.which", lambda _name: None)
    assert unshare_available() is False
    monkeypatch.setattr("catchpr.runner.shutil.which", lambda _name: "/usr/bin/unshare")

    def explode(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="unshare", timeout=5)

    monkeypatch.setattr("catchpr.runner.subprocess.run", explode)
    assert unshare_available() is False


def test_pipeline_filters_static_env_judge_and_flake(tmp_path: Path) -> None:
    base, head = _pair(tmp_path)
    socket_test = "import socket\ndef test_net():\n    socket.socket()\n"
    random_test = "import random\ndef test_rand():\n    assert random.random() >= 0\n"
    provider = MockProvider(
        [
            Recording(
                ("phase: dodgy",),
                json.dumps(
                    {
                        "tests": [
                            {"id": "sock", "rationale": "no", "test": socket_test},
                            {"id": "rand", "rationale": "no", "test": random_test},
                        ]
                    }
                ),
            )
        ]
    )
    report = run_pipeline(RunConfig(repo=tmp_path, base=base, head=head, repeats=1), provider)
    names = {item.filter_name for item in report.rejections}
    assert "static" in names
    assert "environment" in names

    class Boom:
        name = "boom"
        model = "x"

        def complete(
            self, prompt: str, *, system: str | None = None, temperature: float = 0.0
        ) -> str:
            raise ProviderError("down")

        def usage(self) -> Usage:
            return Usage()

    failed = run_pipeline(RunConfig(repo=tmp_path, base=base, head=head, repeats=1), Boom())
    assert len(failed.warnings) == 2
    assert (
        main(
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
                "--provider",
                "openai",
                "--no-cache",
            ]
        )
        == 3
    )

    judged = MockProvider(
        [
            Recording(
                ("phase: intent", "def apply_discount"),
                json.dumps(
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
            ),
            Recording(("phase: judge",), '{"verdict": "false_positive", "rationale": "no"}'),
        ]
    )
    judged_report = run_pipeline(
        RunConfig(
            repo=tmp_path, base=base, head=head, repeats=1, use_judge=True, title="t", body="b"
        ),
        judged,
    )
    assert judged_report.silent
    assert any(item.filter_name == "judge" for item in judged_report.rejections)

    flip = (
        "from pathlib import Path\n"
        "from billing import apply_discount\n\n"
        "def test_flip():\n"
        "    seen = Path('_seen').exists()\n"
        "    Path('_seen').write_text('1')\n"
        "    assert apply_discount(10000, 250) == 9750 or seen\n"
    )
    flip_provider = MockProvider(
        [
            Recording(
                ("phase: dodgy", "def apply_discount"),
                json.dumps({"tests": [{"id": "flip", "rationale": "flip", "test": flip}]}),
            )
        ]
    )
    flip_report = run_pipeline(
        RunConfig(repo=tmp_path, base=base, head=head, repeats=2),
        flip_provider,
    )
    assert any("stable failure" in item.reason for item in flip_report.rejections)
    with pytest.raises(ValueError):
        run_pipeline(RunConfig(repo=tmp_path, base=base, head=head, repeats=0), MockProvider())


def test_github_helpers_and_comment_edges(tmp_path: Path) -> None:
    with pytest.raises(GitHubError):
        post_sticky(repository="acme/widgets", pr=0, body="x", token="t")
    with pytest.raises(GitHubError):
        post_sticky(repository="acme/widgets", pr=1, body="x", token="")
    with pytest.raises(GitHubError):
        post_sticky(repository="not-a-repo", pr=1, body="x", token="t")
    hostile = io.BytesIO()
    with zipfile.ZipFile(hostile, "w") as archive:
        archive.writestr("../evil.txt", "nope")
    with pytest.raises(GitHubError):
        _safe_extract(hostile.getvalue(), tmp_path / "out")
    with pytest.raises(GitHubError):
        _safe_extract(b"not a zip", tmp_path / "bad")
    payload = {
        "base": "abc",
        "head": "def",
        "catches": [
            {
                "workflow": "intent-aware",
                "test_id": f"t{i}",
                "rationale": "because",
                "source": "def test_x():\n    assert True\n",
                "location_path": "billing.py",
                "base_output": "",
                "head_output": "E",
                "reproduce": "catchpr repro",
                "base_runs": 3,
                "head_runs": 3,
            }
            for i in range(6)
        ],
    }
    text = comment_from_payload(payload)
    assert "more catch" in text
    huge = dict(payload)
    huge["catches"] = [
        {
            "workflow": "intent-aware",
            "test_id": "big",
            "source": "x" * 9000,
            "head_output": "y" * 5000,
            "reproduce": "catchpr repro",
        }
    ]
    assert "truncated" in comment_from_payload(huge)
    with pytest.raises(ValueError):
        comment_from_payload([])
    with pytest.raises(ProviderError):
        extract_message('{"choices":[{"message":{}}]}')
    provider = OpenAICompatibleProvider(
        name="openai", model="m", base_url="http://127.0.0.1:1", api_key="k", timeout=1
    )
    with pytest.raises(ProviderError):
        provider.complete("ping")
    cached = CachingProvider(MockProvider(), tmp_path / "cache")
    assert cached.complete("phase: dodgy\n")
    key_dir = tmp_path / "cache"
    for path in key_dir.rglob("*.json"):
        path.write_text("[]", encoding="utf-8")
    assert cached.complete("phase: dodgy\n")
    digest = prompt_hash("phase: intent\n", None, 0.0)
    hashed = MockProvider([Recording((), "hashed", prompt_hash=digest)])
    assert hashed.complete("phase: intent\n") == "hashed"


def test_cli_body_file_and_missing_artifact_token(tmp_path: Path) -> None:
    base, head = _pair(tmp_path)
    body = tmp_path / "body.txt"
    body.write_text("Intended note.\n", encoding="utf-8")
    code = main(
        [
            "run",
            "--repo",
            str(tmp_path),
            "--base",
            base,
            "--head",
            head,
            "--body-file",
            str(body),
            "--repeats",
            "1",
            "--no-cache",
            "--allow-network",
            "--provider",
            "mock",
        ]
    )
    assert code == 0
    assert (
        main(
            [
                "fetch-artifact",
                "--repository",
                "acme/widgets",
                "--run-id",
                "1",
                "--name",
                "n",
                "--dest",
                str(tmp_path),
            ]
        )
        == 2
    )
    with pytest.raises(GitHubError):
        fetch_artifact(
            repository="acme/widgets",
            run_id=0,
            name="n",
            dest=tmp_path,
            token="t",
        )
    shutil.rmtree(tmp_path / ".git", ignore_errors=True)
