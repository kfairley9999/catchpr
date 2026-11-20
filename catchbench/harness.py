"""Run catchbench. Mock mode replays recordings. Live mode calls a provider."""

from __future__ import annotations

import difflib
import json
import subprocess
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

from catchbench.cases import CASES, Case
from catchpr.baseline import baseline_review
from catchpr.diff import analyze_repo
from catchpr.pipeline import RunConfig, run_pipeline
from catchpr.providers.base import Provider, Recording
from catchpr.providers.mock import MockProvider


def build_repo(case: Case, repo: Path) -> tuple[str, str]:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "catchbench@example.com")
    _git(repo, "config", "user.name", "catchbench")
    _write(repo, case.filename, case.base)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo, case.filename, case.head)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "head")
    head = _git(repo, "rev-parse", "HEAD")
    return base, head


def recordings_for(case: Case) -> list[Recording]:
    anchor = f"def {case.symbol}"
    intent = {
        "intent": case.title,
        "risks": [
            {
                "id": "r1",
                "description": case.summary,
                "mutant": {
                    "path": case.filename,
                    "symbol": case.symbol,
                    "replacement": case.mutant,
                },
                "test": case.intent_test,
            }
        ],
    }
    rows = case.dodgy or (("d1", "characterize the parent", case.intent_test),)
    dodgy = {"tests": [{"id": item[0], "rationale": item[1], "test": item[2]} for item in rows]}
    baseline = {"verdict": case.baseline, "summary": case.summary}
    return [
        Recording(("phase: intent", anchor), json.dumps(intent), recording_id=f"{case.id}-intent"),
        Recording(("phase: dodgy", anchor), json.dumps(dodgy), recording_id=f"{case.id}-dodgy"),
        Recording(
            ("phase: baseline", anchor),
            json.dumps(baseline),
            recording_id=f"{case.id}-baseline",
        ),
    ]


def unified_diff(case: Case) -> str:
    return "".join(
        difflib.unified_diff(
            case.base.splitlines(keepends=True),
            case.head.splitlines(keepends=True),
            fromfile=f"a/{case.filename}",
            tofile=f"b/{case.filename}",
        )
    )


def evaluate_case(
    case: Case,
    *,
    provider: Provider | None = None,
    repeats: int = 3,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Execute one case. A None provider replays that case's recordings."""
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix=f"catchbench-{case.id}-") as tmp:
        repo = Path(tmp)
        base, head = build_repo(case, repo)
        chosen = provider if provider is not None else MockProvider(recordings_for(case))
        report = run_pipeline(
            RunConfig(
                repo=repo,
                base=base,
                head=head,
                title=case.title,
                body=case.body,
                repeats=repeats,
                timeout=timeout,
            ),
            chosen,
        )
        analysis = analyze_repo(repo, base, head, title=case.title, body=case.body)
        verdict, summary = baseline_review(analysis, chosen)
        usage = chosen.usage()
    elapsed = time.perf_counter() - started
    caught = bool(report.catches)
    replay_test = case.intent_test
    base_output = ""
    head_output = ""
    workflow = ""
    if report.catches:
        first = report.catches[0]
        replay_test = first.test.source
        workflow = first.test.workflow
        base_output = first.base_runs[-1].combined()
        head_output = first.head_runs[-1].combined()
    reasons = [f"{item.filter_name}: {item.reason}" for item in report.rejections]
    if caught:
        outcome = "catch"
    elif any(
        item.filter_name in {"symbols", "intended", "environment", "imports", "judge"}
        for item in report.rejections
    ):
        outcome = "filtered"
    else:
        outcome = "silent"
    return {
        "id": case.id,
        "kind": case.kind,
        "title": case.title,
        "body": case.body,
        "filename": case.filename,
        "diff": unified_diff(case),
        "caught": caught,
        "outcome": outcome,
        "catches": len(report.catches),
        "workflow": workflow,
        "test": replay_test,
        "base_output": base_output,
        "head_output": head_output,
        "reasons": reasons,
        "baseline_verdict": verdict,
        "baseline_summary": summary,
        "baseline_label_bug": case.baseline == "bug",
        "seconds": round(elapsed, 3),
        "logical_calls": usage.logical_calls,
        "remote_calls": usage.remote_calls,
        "prompt_chars": usage.prompt_chars,
        "warnings": list(report.warnings),
    }


def run_benchmark(
    *,
    cases: tuple[Case, ...] = CASES,
    repeats: int = 3,
    workers: int = 1,
    provider_name: str = "mock",
    live_provider: Provider | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    if workers > 1 and live_provider is None:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            rows = list(pool.map(_evaluate_recorded, [(case, repeats) for case in cases]))
    else:
        rows = [evaluate_case(case, provider=live_provider, repeats=repeats) for case in cases]
    elapsed = time.perf_counter() - started
    return summarize(
        rows,
        elapsed=elapsed,
        repeats=repeats,
        provider_name=provider_name,
        model=live_provider.model if live_provider is not None else "recorded",
    )


def summarize(
    rows: list[dict[str, Any]],
    *,
    elapsed: float,
    repeats: int,
    provider_name: str,
    model: str,
) -> dict[str, Any]:
    bugs = [row for row in rows if row["kind"] == "bug"]
    refactors = [row for row in rows if row["kind"] == "refactor"]
    bugs_caught = sum(1 for row in bugs if row["caught"])
    false_alarms = sum(1 for row in refactors if row["caught"])
    comments = sum(1 for row in rows if row["caught"])
    baseline_bugs = sum(1 for row in bugs if row["baseline_verdict"] == "bug")
    baseline_false = sum(1 for row in refactors if row["baseline_verdict"] == "bug")
    prompt_chars = sum(int(row["prompt_chars"]) for row in rows)
    remote_calls = sum(int(row["remote_calls"]) for row in rows)
    return {
        "mode": "live" if provider_name != "mock" else "mock",
        "provider": provider_name,
        "model": model,
        "repeats": repeats,
        "case_count": len(rows),
        "bug_count": len(bugs),
        "refactor_count": len(refactors),
        "disclaimer": _disclaimer(provider_name),
        "catchpr": {
            "bugs_caught": bugs_caught,
            "catch_rate": _rate(bugs_caught, len(bugs)),
            "false_alarms": false_alarms,
            "false_alarm_rate": _rate(false_alarms, len(refactors)),
            "comments": comments,
            "comments_per_pr": _rate(comments, len(rows)),
            "validated_catches": sum(int(row["catches"]) for row in rows),
            "estimated_prompt_tokens": prompt_chars // 4,
            "remote_calls": remote_calls,
            "api_cost_usd": 0.0 if provider_name == "mock" else None,
            "wall_time_seconds": round(elapsed, 3),
        },
        "baseline_readonly_review": {
            "bugs_flagged": baseline_bugs,
            "recall": _rate(baseline_bugs, len(bugs)),
            "false_alarms": baseline_false,
            "false_alarm_rate": _rate(baseline_false, len(refactors)),
            "comments_per_pr": _rate(baseline_bugs + baseline_false, len(rows)),
            "note": (
                "Recorded read-only reviews replayed by the same provider. "
                "In mock mode this column is not a measurement of a live model."
                if provider_name == "mock"
                else "Live read-only review using the same provider and model. Not executed."
            ),
        },
        "cases": rows,
    }


def markdown_table(summary: dict[str, Any]) -> str:
    catchpr = summary["catchpr"]
    baseline = summary["baseline_readonly_review"]
    mode = summary["mode"]
    return "\n".join(
        [
            f"| | catchpr ({mode}) | read-only review ({mode}) |",
            "| --- | ---: | ---: |",
            f"| Bug catch rate | {catchpr['catch_rate']:.0%} ({catchpr['bugs_caught']}/{summary['bug_count']}) | {baseline['recall']:.0%} ({baseline['bugs_flagged']}/{summary['bug_count']}) |",
            f"| False alarms on harmless diffs | {catchpr['false_alarm_rate']:.0%} ({catchpr['false_alarms']}/{summary['refactor_count']}) | {baseline['false_alarm_rate']:.0%} ({baseline['false_alarms']}/{summary['refactor_count']}) |",
            f"| Comments per PR | {catchpr['comments_per_pr']:.2f} | {baseline['comments_per_pr']:.2f} |",
            f"| API cost | {_cost(catchpr['api_cost_usd'])} | {_cost(catchpr['api_cost_usd'])} |",
            f"| Wall time | {catchpr['wall_time_seconds']:.1f}s | included |",
        ]
    )


def _cost(value: object) -> str:
    if value is None:
        return "not priced"
    return "$0.00"


def _rate(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return numerator / denominator


def _disclaimer(provider_name: str) -> str:
    if provider_name == "mock":
        return (
            "Mock mode replays hand-authored recordings through MockProvider. "
            "The catch rate and false-alarm rate are measured by executing those "
            "recorded tests in the sandbox. The read-only column replays a recorded "
            "verdict for each case. Neither column is a live-model result. "
            "api_cost_usd is 0 because no API was called. "
            "Run python -m catchbench.live to score a real model; those results are not published here."
        )
    return (
        "Live mode called the named provider. Cost is not converted to dollars "
        "because prices change; estimated_prompt_tokens counts characters/4 on remote calls."
    )


def _evaluate_recorded(payload: tuple[Case, int]) -> dict[str, Any]:
    case, repeats = payload
    return evaluate_case(case, provider=None, repeats=repeats)


def _write(repo: Path, filename: str, source: str) -> None:
    path = repo / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def cases_by_id(ids: list[str]) -> tuple[Case, ...]:
    wanted = set(ids)
    found = tuple(case for case in CASES if case.id in wanted)
    missing = wanted - {case.id for case in found}
    if missing:
        raise SystemExit(f"unknown case ids: {sorted(missing)}")
    return found
