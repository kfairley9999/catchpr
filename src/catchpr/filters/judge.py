"""Optional LLM assessor. Abstain and false_positive both stay silent.

The judge sees the diff, the test, and the head failure. It does not execute
anything. A missing or unparseable answer is treated as abstain so a confused
model cannot force a comment.
"""

from __future__ import annotations

from catchpr.diff import render_analysis
from catchpr.errors import ParseError, ProviderError
from catchpr.jsonutil import as_str, parse_model_json
from catchpr.models import Catch, DiffAnalysis, Rejection
from catchpr.providers.base import Provider

JUDGE_SYSTEM = """You judge whether a weak catch is a bug or a false positive.
A weak catch is a test that passes on the parent and fails on the pull request.
Respond with one JSON object and no markdown.
Schema: {"verdict": "bug" | "false_positive" | "abstain", "rationale": string}
Use false_positive when the change is intended, the test is brittle, or the
failure is an import, rename, timeout, or environment problem.
Use bug only when the failure shows an unintended behavior change.
Use abstain when you cannot tell.
"""


def judge_catch(catch: Catch, analysis: DiffAnalysis, provider: Provider) -> Rejection | None:
    failure = "\n".join(run.combined() for run in catch.head_runs)[:8000]
    prompt = "\n".join(
        [
            "phase: judge",
            f"workflow: {catch.test.workflow}",
            f"test_id: {catch.test.test_id}",
            "test:",
            catch.test.source,
            "head_failure:",
            failure,
            "",
            render_analysis(analysis),
        ]
    )
    try:
        raw = provider.complete(prompt, system=JUDGE_SYSTEM, temperature=0.0)
        data = parse_model_json(raw)
    except (ParseError, ProviderError) as exc:
        return _hit(catch, f"judge abstained: {exc}")
    verdict = (as_str(data.get("verdict")) or "abstain").strip().lower()
    rationale = as_str(data.get("rationale")) or ""
    if verdict == "bug":
        return None
    if verdict == "false_positive":
        return _hit(catch, rationale or "judge marked false positive")
    return _hit(catch, rationale or "judge abstained")


def _hit(catch: Catch, reason: str) -> Rejection:
    return Rejection(
        filter_name="judge",
        reason=reason,
        test_id=catch.test.test_id,
        workflow=catch.test.workflow,
    )
