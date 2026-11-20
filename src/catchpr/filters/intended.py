"""Drop a catch when the PR text says this behavior change is intended."""

from __future__ import annotations

import re

from catchpr.models import Catch, DiffAnalysis, Rejection

_NEGATION = re.compile(
    r"\b(not intended|unintentional|did not intend|didn't intend|"
    r"unintended|accidentally|regression)\b",
    re.IGNORECASE,
)
_KEYWORD = re.compile(
    r"\b(breaking change|intentionally|intended behavior|"
    r"intended behaviour|by design|on purpose)\b",
    re.IGNORECASE,
)
_FROM_TO = re.compile(
    r"\bfrom\s+['\"]?([^'\"\n.]{1,80}?)['\"]?\s+to\s+['\"]?([^'\"\n.]{1,80})",
    re.IGNORECASE,
)
_NOW = re.compile(
    r"\b(?:now returns|should now return|should now be|now equals|now yields)"
    r"\s+['\"]?([^'\"\n.]{1,80})",
    re.IGNORECASE,
)
_ASSERT = re.compile(r"assert\s+(.+?)\s*==\s*([^\n]+)")


def _clean(token: str) -> str:
    return token.strip().strip("'\"").rstrip(".,);")


def _same(left: str, right: str) -> bool:
    a = _clean(left)
    b = _clean(right)
    if a == b:
        return True
    try:
        return float(a) == float(b)
    except ValueError:
        return False


def assertion_pair(failure: str) -> tuple[str, str] | None:
    """Return (actual, expected) from a pytest assertion line, if one is present."""
    match = _ASSERT.search(failure)
    if match is None:
        return None
    return _clean(match.group(1)), _clean(match.group(2))


def intended_change(catch: Catch, analysis: DiffAnalysis) -> Rejection | None:
    text = f"{analysis.title}\n{analysis.body}"
    if _NEGATION.search(text):
        return None
    failure = "\n".join(run.combined() for run in catch.head_runs)
    pair = assertion_pair(failure)
    for match in _FROM_TO.finditer(text):
        old, new = _clean(match.group(1)), _clean(match.group(2))
        if pair and _same(new, pair[0]) and _same(old, pair[1]):
            return _hit(catch, f"PR text describes this change as from {old} to {new}")
    for match in _NOW.finditer(text):
        new = _clean(match.group(1))
        if pair and _same(new, pair[0]):
            return _hit(catch, f"PR text says the new value {new} is intended")
    if _KEYWORD.search(text) and _names_function(text, analysis):
        return _hit(catch, "PR text marks this function's change as intended")
    return None


def _names_function(text: str, analysis: DiffAnalysis) -> bool:
    for item in analysis.files:
        for qual in item.changed_qualnames:
            leaf = qual.rsplit(".", 1)[-1]
            if len(leaf) >= 3 and re.search(rf"\b{re.escape(leaf)}\b", text):
                return True
    return False


def _hit(catch: Catch, reason: str) -> Rejection:
    return Rejection(
        filter_name="intended",
        reason=reason,
        test_id=catch.test.test_id,
        workflow=catch.test.workflow,
    )
