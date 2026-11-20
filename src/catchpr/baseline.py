"""Read-only review prompt. The same provider as the generators, and no execution.

This is the benchmark baseline: a model that only reads the diff and says
bug or clean. It is not a catch. catchpr does not comment from this verdict.
"""

from __future__ import annotations

from catchpr.diff import render_analysis
from catchpr.errors import ParseError
from catchpr.jsonutil import as_str, parse_model_json
from catchpr.models import DiffAnalysis
from catchpr.providers.base import Provider

BASELINE_SYSTEM = """You are a read-only code reviewer. You cannot run code.
Decide whether the pull request introduces a functional bug.
Respond with one JSON object and no markdown.
Schema: {"verdict": "bug" | "clean", "summary": string}
Use bug only for a behavior change that looks unintended.
Use clean for refactors, renames of internals, and intended changes.
"""


def baseline_review(analysis: DiffAnalysis, provider: Provider) -> tuple[str, str]:
    prompt = "phase: baseline\n" + render_analysis(analysis)
    raw = provider.complete(prompt, system=BASELINE_SYSTEM, temperature=0.0)
    try:
        data = parse_model_json(raw)
    except ParseError:
        return "clean", "baseline response was not JSON"
    verdict = (as_str(data.get("verdict")) or "clean").strip().lower()
    if verdict not in {"bug", "clean"}:
        verdict = "clean"
    summary = as_str(data.get("summary")) or ""
    return verdict, summary
