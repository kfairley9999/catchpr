"""Drop import and collection failures. They are not logic catches."""

from __future__ import annotations

import re

from catchpr.models import Catch, Rejection

_IMPORT = re.compile(r"(ModuleNotFoundError|ImportError\b|ImportError: No module named)")


def import_failure(catch: Catch) -> Rejection | None:
    text = "\n".join(run.combined() for run in catch.head_runs)
    match = _IMPORT.search(text)
    if match is None:
        return None
    return Rejection(
        filter_name="imports",
        reason=f"head failure is an import error ({match.group(1)})",
        test_id=catch.test.test_id,
        workflow=catch.test.workflow,
    )
