from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from tests.orch_support import Num, double

from catchpr.orchestra.agents import adapt
from catchpr.orchestra.cli import main
from catchpr.orchestra.engine import Orchestrator
from catchpr.orchestra.pipeline import Pipeline, Step
from catchpr.orchestra.tiers import build_tierset
from catchpr.orchestra.trace import html_trace


@pytest.fixture(autouse=True)
def no_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("DEEPINFRA_API_KEY", "DEEPINFRA_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def only_run(runs: Path) -> Path:
    (run,) = [p for p in runs.iterdir() if p.is_dir() and p.name != "outbox"]
    return run


def test_demo_approve_resume_flow(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runs, keys = tmp_path / "runs", tmp_path / "keys"
    pub = keys / "approver_ed25519.pub"
    common = ["--runs-dir", str(runs), "--public-key", str(pub)]
    assert main(["demo", "coi", *common, "--html"]) == 11
    out = capsys.readouterr().out
    assert "AWAITING_APPROVAL" in out and "fast>standard" in out and "simulated" in out
    run = only_run(runs)
    assert (run / "trace.html").exists()
    item = json.loads((run / "review.jsonl").read_text().splitlines()[0])["id"]

    assert main(["verify", str(run)]) == 0
    assert main(["replay", str(run)]) == 0
    assert "MATCH" in capsys.readouterr().out
    assert main(["review", "list", "--runs-dir", str(runs)]) == 0
    assert "[approval/open]" in capsys.readouterr().out

    assert main(["keys", "init", "--dir", str(keys)]) == 0
    assert main(["keys", "init", "--dir", str(keys)]) == 2
    tokens = tmp_path / "approvals.json"
    approve = [
        "approve",
        str(run),
        item,
        "--approver",
        "Kevin",
        "--key",
        str(keys / "approver_ed25519"),
        "--tokens",
        str(tokens),
    ]
    assert main([*approve, "--yes"]) == 0
    assert main(["resume", str(run), *common, "--approvals", str(tokens)]) == 0
    outbox = list((runs / "outbox").glob("*.json"))
    assert len(outbox) == 1
    assert "corrections needed" in outbox[0].read_text()
    capsys.readouterr()
    assert main(["review", "list", "--runs-dir", str(runs)]) == 0
    assert "[approval/executed]" in capsys.readouterr().out

    # Resuming the original run again does not send a second email.
    assert main(["resume", str(run), *common, "--approvals", str(tokens)]) == 0
    ledger = (runs / "effects-ledger.jsonl").read_text()
    assert ledger.count('"kind": "result"') == 1


def test_approve_requires_typed_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs, keys = tmp_path / "runs", tmp_path / "keys"
    main(["demo", "coi", "--runs-dir", str(runs), "--public-key", str(keys / "none.pub")])
    main(["keys", "init", "--dir", str(keys)])
    run = only_run(runs)
    item = json.loads((run / "review.jsonl").read_text().splitlines()[0])["id"]
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    tokens = tmp_path / "t.json"
    assert (
        main(
            [
                "approve",
                str(run),
                item,
                "--approver",
                "K",
                "--key",
                str(keys / "approver_ed25519"),
                "--tokens",
                str(tokens),
            ]
        )
        == 1
    )
    assert not tokens.exists()
    assert main(["approve", str(run), "nope", "--approver", "K", "--tokens", str(tokens)]) == 2


def test_review_resolve_and_resume(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runs = tmp_path / "runs"
    inp = tmp_path / "in.json"
    inp.write_text(
        json.dumps(
            {
                "region": "LA",
                "vertical": "agency",
                "count": 1,
                "landing_page": "https://example.com/x",
            }
        )
    )
    # No demo responder via `run`: mock answers are not prospect lists -> review.
    code = main(
        [
            "run",
            "--pipeline",
            "catchpr.orchestra.examples.leadflow:build",
            "--input",
            str(inp),
            "--runs-dir",
            str(runs),
            "--provider",
            "mock",
            "--public-key",
            str(tmp_path / "none.pub"),
        ]
    )
    assert code == 10
    run = only_run(runs)
    item = json.loads((run / "review.jsonl").read_text().splitlines()[0])
    assert item["step"] == "researcher"
    human = tmp_path / "h.json"
    human.write_text(
        json.dumps({"prospects": [], "landing_page": "https://example.com/x", "confidence": 1})
    )
    assert (
        main(
            [
                "review",
                "resolve",
                str(run),
                item["id"],
                "--output",
                str(human),
                "--reviewer",
                "Kevin",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main(["review", "list", "--runs-dir", str(runs)]) == 0
    assert "[review/resolved]" in capsys.readouterr().out
    assert (
        main(
            [
                "approve",
                str(run),
                item["id"],
                "--approver",
                "K",
                "--tokens",
                str(tmp_path / "t.json"),
            ]
        )
        == 2
    )
    bad = tmp_path / "bad.json"
    bad.write_text('{"prospects": "no"}')
    assert (
        main(["review", "resolve", str(run), item["id"], "--output", str(bad), "--reviewer", "K"])
        == 2
    )


def test_misc_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["tiers"]) == 0
    out = capsys.readouterr().out
    assert (
        "DISABLED (disabled until an Anthropic API key is approved" in out
        and "missing (mock fallback)" in out
    )
    assert main(["demo", "coi", "--max-usd", "abc"]) == 2
    assert main(["verify", str(tmp_path)]) == 1
    assert main(["trace", str(tmp_path)]) == 2
    assert main([]) == 2
    runs = tmp_path / "runs"
    runs.mkdir()
    assert main(["review", "list", "--runs-dir", str(runs)]) == 0
    assert "empty" in capsys.readouterr().out
    assert (
        main(["demo", "leadflow", "--runs-dir", str(runs), "--public-key", str(tmp_path / "x.pub")])
        == 11
    )
    run = only_run(runs)
    html_out = tmp_path / "t.html"
    assert main(["trace", str(run), "--html", str(html_out)]) == 0
    assert "leadflow-sales" in html_out.read_text()
    assert main(["trace", str(run)]) == 0
    assert main(["demo", "coi", "--runs-dir", str(tmp_path / "r2"), "--provider", "deepinfra"]) == 2


def test_html_trace_escapes_untrusted_text(tmp_path: Path) -> None:
    p = Pipeline("<script>alert(1)</script>", Num, (Step("a", adapt(double)),))
    orch = Orchestrator(p, build_tierset(mode="mock"), runs_dir=tmp_path)
    result = orch.run({"value": 1})
    page = html_trace(result.run_dir)
    assert "<script>alert" not in page and "&lt;script&gt;" in page


def test_module_entry_point(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "catchpr.orchestra", "tiers"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert proc.returncode == 0 and "fast" in proc.stdout
