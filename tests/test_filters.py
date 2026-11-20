from __future__ import annotations

from catchpr.filters.environment import environment_failure, environment_source_reason
from catchpr.filters.imports import import_failure
from catchpr.filters.intended import assertion_pair, intended_change
from catchpr.filters.judge import judge_catch
from catchpr.filters.symbols import symbol_failure
from catchpr.models import (
    Catch,
    DiffAnalysis,
    FileChange,
    GeneratedTest,
    ProcResult,
    Symbol,
)
from catchpr.providers.mock import MockProvider


def _proc(text: str, *, passed: bool = False, timed_out: bool = False) -> ProcResult:
    return ProcResult(passed, 0 if passed else 1, "", text, timed_out, 0.01)


def _catch(source: str = "def test_x():\n    assert True\n", failure: str = "") -> Catch:
    test = GeneratedTest("intent-aware", "t1", "risk", source, None, "billing.py")
    return Catch(
        test, (_proc("1 passed", passed=True),), (_proc(failure),), None, "billing.py", 1, "cmd"
    )


def _analysis(body: str = "", title: str = "Update billing") -> DiffAnalysis:
    symbol = Symbol(
        "apply_discount", "apply_discount", "function", 1, 2, "def apply_discount():\n    return 1"
    )
    removed = Symbol(
        "_format_total", "_format_total", "function", 1, 2, "def _format_total():\n    return 1"
    )
    added = Symbol(
        "_render_total", "_render_total", "function", 1, 2, "def _render_total():\n    return 1"
    )
    change = FileChange(
        path="billing.py",
        status="modified",
        old_path=None,
        base_source="",
        head_source="",
        base_symbols=(symbol, removed),
        head_symbols=(symbol, added),
        changed_qualnames=("apply_discount",),
        added_qualnames=("_render_total",),
        removed_qualnames=("_format_total",),
        renamed=(("_format_total", "_render_total"),),
    )
    return DiffAnalysis(".", "a" * 40, "b" * 40, title, body, (change,))


def test_symbol_filter_matches_renamed_name_only() -> None:
    analysis = _analysis()
    failure = "E   ImportError: cannot import name '_format_total' from 'billing'"
    hit = symbol_failure(_catch(failure=failure), analysis)
    assert hit is not None and hit.filter_name == "symbols"
    assert symbol_failure(_catch(failure="AssertionError: assert 1 == 2"), analysis) is None
    assert (
        symbol_failure(_catch(failure="NameError: name 'unrelated' is not defined"), analysis)
        is None
    )


def test_intended_filter_requires_a_declared_change() -> None:
    catch = _catch(failure="E       AssertionError: assert 4 == 3")
    assert assertion_pair("assert 4 == 3") == ("4", "3")
    assert assertion_pair("no assertion") is None
    hit = intended_change(
        catch,
        _analysis(body="This breaking change makes apply_discount go from 3 to 4."),
    )
    assert hit is not None and hit.filter_name == "intended"
    now = intended_change(catch, _analysis(body="apply_discount should now return 4."))
    assert now is not None
    keyword = intended_change(
        catch,
        _analysis(body="The change to apply_discount is intentional, by design."),
    )
    assert keyword is not None
    negated = intended_change(
        catch,
        _analysis(body="This is not intended. apply_discount accidentally returns 4."),
    )
    assert negated is None
    unrelated = intended_change(catch, _analysis(body="Refactor the helper."))
    assert unrelated is None
    keyword_without_name = intended_change(
        _catch(failure="E       AssertionError: assert 4 == 3"),
        _analysis(title="misc", body="This is intentional and by design."),
    )
    # The function name is not in that text, so the keyword alone does not filter.
    assert keyword_without_name is None


def test_environment_and_import_filters() -> None:
    assert (
        environment_source_reason("import random\nrandom.random()\n")
        == "test depends on randomness"
    )
    assert environment_source_reason("import time\nx = time.time()\n") == "test depends on time"
    assert (
        environment_source_reason("import os\nos.environ['A']\n") == "test depends on environment"
    )
    assert environment_source_reason("import datetime\ndatetime.now()\n") == "test depends on time"
    assert environment_source_reason("from uuid import uuid4\n") is None
    assert environment_source_reason("def test_ok():\n    assert 1 == 1\n") is None
    assert environment_failure(_catch(failure="NotImplementedError")) is not None
    assert environment_failure(_catch(failure="Permission denied")) is not None
    assert environment_failure(_catch(failure="URLError: timed out")) is not None
    timed = _catch(failure="still running")
    timed.head_runs[0]  # type: ignore[index]
    timeout = Catch(
        timed.test,
        timed.base_runs,
        (_proc("slow", timed_out=True),),
        None,
        "billing.py",
        1,
        "cmd",
    )
    assert environment_failure(timeout) is not None
    assert environment_failure(_catch(failure="AssertionError: assert 1 == 2")) is None
    assert import_failure(_catch(failure="ModuleNotFoundError: No module named 'nope'")) is not None
    assert import_failure(_catch(failure="AssertionError")) is None


def test_judge_filter() -> None:
    analysis = _analysis(body="change the discount")
    catch = _catch(failure="AssertionError: assert 1 == 2")
    bug = MockProvider(
        [],
    )
    # Fallback for phase: judge is abstain, which stays silent.
    abstain = judge_catch(catch, analysis, bug)
    assert abstain is not None and abstain.filter_name == "judge"
    recorded = MockProvider(
        recordings=[],
    )
    from catchpr.providers.base import Recording

    provider = MockProvider(
        [
            Recording(("phase: judge",), '{"verdict": "bug", "rationale": "real"}'),
        ]
    )
    assert judge_catch(catch, analysis, provider) is None
    false_positive = MockProvider(
        [Recording(("phase: judge",), '{"verdict": "false_positive", "rationale": "intended"}')]
    )
    hit = judge_catch(catch, analysis, false_positive)
    assert hit is not None and "intended" in hit.reason
    broken = MockProvider([Recording(("phase: judge",), "not json")])
    bad = judge_catch(catch, analysis, broken)
    assert bad is not None and "abstained" in bad.reason
    assert recorded  # silence unused-lint by using the name above
