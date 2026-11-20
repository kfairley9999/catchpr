"""Drop failures caused by a symbol the diff added, removed, or renamed."""

from __future__ import annotations

import re

from catchpr.models import Catch, DiffAnalysis, Rejection

_MISSING = (
    re.compile(r"NameError: name '([^']+)' is not defined"),
    re.compile(r"AttributeError: .* has no attribute '([^']+)'"),
    re.compile(r"ImportError: cannot import name '([^']+)'"),
    re.compile(r"cannot import name '([^']+)'"),
)


def symbol_failure(catch: Catch, analysis: DiffAnalysis) -> Rejection | None:
    text = "\n".join(run.combined() for run in catch.head_runs)
    names: list[str] = []
    for pattern in _MISSING:
        names.extend(pattern.findall(text))
    if not names:
        return None
    known = analysis.removed_names() | analysis.added_names()
    for name in names:
        if name in known:
            return Rejection(
                filter_name="symbols",
                reason=f"head failure is a missing or renamed symbol: {name}",
                test_id=catch.test.test_id,
                workflow=catch.test.workflow,
            )
    return None
