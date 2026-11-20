"""Drop tests that depend on time, randomness, or the environment.

Also treats infrastructure failures (timeouts, permissions, NotImplementedError,
a broken test runner) as non-bugs. Those are the paper's broken_test_runner,
flakiness, and not_implemented_exception patterns in deterministic form.
"""

from __future__ import annotations

import re

from catchpr.models import Catch, Rejection

_SOURCE = (
    (re.compile(r"\brandom\."), "randomness"),
    (re.compile(r"\bsecrets\."), "randomness"),
    (re.compile(r"\bos\.urandom\b"), "randomness"),
    (re.compile(r"\buuid\.uuid4\b"), "randomness"),
    (re.compile(r"\btime\.time\b|\btime\.time_ns\b|\btime\.monotonic\b"), "time"),
    (re.compile(r"\bdatetime\.now\b|\bdatetime\.utcnow\b|\bdate\.today\b"), "time"),
    (re.compile(r"\bos\.environ\b|\bgetenv\b"), "environment"),
    (re.compile(r"\bsocket\."), "network"),
)

_FAILURE = (
    (re.compile(r"NotImplementedError"), "failure is NotImplementedError"),
    (re.compile(r"PermissionError|Permission denied"), "environment permission error"),
    (
        re.compile(
            r"ConnectionError|URLError|gaierror|Network is unreachable|Temporary failure in name"
        ),
        "failure is a network error",
    ),
    (re.compile(r"INTERNALERROR"), "pytest itself failed"),
)


def environment_source_reason(source: str) -> str | None:
    for pattern, kind in _SOURCE:
        if pattern.search(source):
            return f"test depends on {kind}"
    return None


def environment_failure(catch: Catch) -> Rejection | None:
    if any(run.timed_out for run in catch.head_runs):
        return _hit(catch, "head run timed out")
    text = "\n".join(run.combined() for run in catch.head_runs)
    for pattern, reason in _FAILURE:
        if pattern.search(text):
            return _hit(catch, reason)
    return None


def _hit(catch: Catch, reason: str) -> Rejection:
    return Rejection(
        filter_name="environment",
        reason=reason,
        test_id=catch.test.test_id,
        workflow=catch.test.workflow,
    )
