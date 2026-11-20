from __future__ import annotations

from pathlib import Path

from tests.support import commit, init_repo

from catchpr.models import GeneratedTest, Mutant
from catchpr.pipeline import RunConfig, run_pipeline
from catchpr.providers.base import Recording
from catchpr.providers.mock import MockProvider
from catchpr.runner import Sandbox, limit_specs, unshare_available

BASE = "def apply_discount(price: int, rate_bps: int) -> int:\n    return price - price * rate_bps // 10000\n"
HEAD = "def apply_discount(price: int, rate_bps: int) -> int:\n    return price + price * rate_bps // 10000\n"
TEST = (
    "from billing import apply_discount\n\n"
    "def test_discount_reduces_price():\n"
    "    assert apply_discount(10000, 250) == 9750\n"
)
MUTANT = GeneratedTest(
    workflow="intent-aware",
    test_id="r1",
    rationale="sign flip",
    source=TEST,
    mutant=Mutant("billing.py", "apply_discount", HEAD),
    target_path="billing.py",
)


def _repo(tmp_path: Path) -> tuple[Path, str, str]:
    init_repo(tmp_path)
    base = commit(tmp_path, {"billing.py": BASE}, "base")
    head = commit(tmp_path, {"billing.py": HEAD}, "head")
    return tmp_path, base, head


def test_limit_specs_are_positive() -> None:
    specs = limit_specs(2, 1024, 2048)
    assert specs[0][1] == 2
    assert specs[2][1] == 2048
    assert unshare_available() in {True, False}


def test_sandbox_confirms_a_real_catch_and_blocks_network(tmp_path: Path) -> None:
    repo, base, head = _repo(tmp_path)
    with Sandbox(repo, base, head, timeout=20) as sandbox:
        assert sandbox.run_base(TEST).passed
        assert sandbox.run_mutant(MUTANT.mutant, TEST).passed is False  # type: ignore[arg-type]
        assert sandbox.run_head(TEST).passed is False
        blocked = sandbox.run_base(
            "def test_net():\n"
            "    import socket\n"
            "    socket.create_connection(('127.0.0.1', 9), timeout=1)\n"
        )
        assert blocked.passed is False
        assert "network is disabled" in blocked.combined() or blocked.returncode != 0


def test_sandbox_times_out(tmp_path: Path) -> None:
    repo, base, head = _repo(tmp_path)
    spinning = "def test_spin():\n    while True:\n        pass\n"
    with Sandbox(repo, base, head, timeout=1) as sandbox:
        result = sandbox.run_base(spinning)
    assert result.timed_out or result.passed is False


def test_pipeline_comments_only_on_a_real_catch(tmp_path: Path) -> None:
    repo, base, head = _repo(tmp_path)
    provider = MockProvider(
        [
            Recording(
                ("phase: intent", "def apply_discount"),
                (
                    '{"intent":"price the discount","risks":[{"id":"sign","description":"sign",'
                    '"mutant":{"path":"billing.py","symbol":"apply_discount","replacement":'
                    + _json_string(HEAD)
                    + '},"test":'
                    + _json_string(TEST)
                    + "}]}"
                ),
            ),
            Recording(
                ("phase: dodgy", "def apply_discount"),
                '{"tests":[{"id":"d1","rationale":"same","test":' + _json_string(TEST) + "}]}",
            ),
        ]
    )
    report = run_pipeline(
        RunConfig(repo=repo, base=base, head=head, title="Fix discount", body="Adjust.", repeats=3),
        provider,
    )
    assert len(report.catches) == 1
    assert report.catches[0].test.workflow == "intent-aware"
    assert any(item.filter_name == "duplicate" for item in report.rejections)
    assert report.catches[0].reproduce.startswith("catchpr repro")
    assert len(report.catches[0].base_runs) == 3
    assert len(report.catches[0].head_runs) == 3


def test_pipeline_stays_silent_on_a_refactor(tmp_path: Path) -> None:
    init_repo(tmp_path)
    parent = "def total(values: list[int]) -> int:\n    acc = 0\n    for value in values:\n        acc += value\n    return acc\n"
    child = "def total(values: list[int]) -> int:\n    return sum(values)\n"
    base = commit(tmp_path, {"billing.py": parent}, "base")
    head = commit(tmp_path, {"billing.py": child}, "head")
    test = "from billing import total\n\ndef test_total():\n    assert total([1, 2, 3]) == 6\n"
    provider = MockProvider(
        [
            Recording(
                ("phase: intent", "def total"),
                '{"intent":"sum","risks":[{"id":"r","description":"same","mutant":'
                '{"path":"billing.py","symbol":"total","replacement":'
                + _json_string(child)
                + '},"test":'
                + _json_string(test)
                + "}]}",
            )
        ]
    )
    report = run_pipeline(
        RunConfig(repo=tmp_path, base=base, head=head, repeats=1, title="sum", body=""),
        provider,
    )
    assert report.silent
    assert any(item.filter_name == "mutant" for item in report.rejections)


def test_pipeline_filters_a_rename(tmp_path: Path) -> None:
    init_repo(tmp_path)
    parent = (
        "def _format_total(cents: int) -> str:\n"
        "    return f'{cents / 100:.2f}'\n\n"
        "def format_invoice(cents: int) -> str:\n"
        "    return _format_total(cents)\n"
    )
    child = (
        "def _render_total(cents: int) -> str:\n"
        "    return f'{cents / 100:.2f}'\n\n"
        "def format_invoice(cents: int) -> str:\n"
        "    return _render_total(cents)\n"
    )
    base = commit(tmp_path, {"billing.py": parent}, "base")
    head = commit(tmp_path, {"billing.py": child}, "head")
    test = (
        "from billing import _format_total\n\n"
        "def test_private():\n"
        "    assert _format_total(250) == '2.50'\n"
    )
    provider = MockProvider(
        [
            Recording(
                ("phase: dodgy", "def _format_total"),
                '{"tests":[{"id":"priv","rationale":"private","test":' + _json_string(test) + "}]}",
            )
        ]
    )
    report = run_pipeline(
        RunConfig(repo=tmp_path, base=base, head=head, repeats=1, title="rename", body=""),
        provider,
    )
    assert report.silent
    assert any(item.filter_name == "symbols" for item in report.rejections)


def _json_string(value: str) -> str:
    import json

    return json.dumps(value)
