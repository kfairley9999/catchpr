from __future__ import annotations

import json
import threading
import time
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel
from tests.orch_support import Num, Scored, double, llm_scored, make_orch, scripted

from catchpr.orchestra.agents import AgentContext, adapt, current_context, current_llm
from catchpr.orchestra.audit import read_events, verify_run
from catchpr.orchestra.budget import BudgetLimits
from catchpr.orchestra.errors import (
    Cancelled,
    ConfigError,
    OutputValidationError,
    ProviderFatal,
    TransientError,
)
from catchpr.orchestra.pipeline import Handoff, Pipeline, RetryPolicy, Step
from catchpr.orchestra.providers import MockProvider

FAST_RETRY = RetryPolicy(max_attempts=3, base_delay=0.5, multiplier=2, jitter=0)


def events_of(result_dir: Path, kind: str) -> list[dict]:  # type: ignore[type-arg]
    return [e for e in read_events(result_dir / "audit.jsonl") if e["event"] == kind]


def scored_pipeline(**step_kwargs) -> Pipeline:  # type: ignore[no-untyped-def]
    kwargs = {"retry": FAST_RETRY, **step_kwargs}
    return Pipeline(
        "scored",
        Num,
        (Step("prep", adapt(double)), Step("score", adapt(llm_scored), after=("prep",), **kwargs)),
    )


def test_happy_path_typed_outputs_and_audit(tmp_path: Path) -> None:
    orch, provider, _ = make_orch(tmp_path, scored_pipeline(), MockProvider(responder=scripted({})))
    result = orch.run({"value": 2})
    assert result.status == "succeeded"
    assert result.output("prep") == Num(value=4)
    out = result.output("score")
    assert isinstance(out, Scored) and out.value == 7
    assert len(provider.calls) == 1
    assert provider.calls[0].messages[-1].content == "score 4"
    assert verify_run(result.run_dir).ok
    summary = json.loads((result.run_dir / "summary.json").read_text())
    assert summary["head_hash"] == result.head_hash
    assert summary["simulated"] is True
    call = events_of(result.run_dir, "llm_call")[0]
    assert call["model"] == "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo"
    assert Decimal(call["cost_usd"]) > 0 and call["simulated"] is True
    assert result.steps["score"].tokens == call["input_tokens"] + call["output_tokens"]


def test_invalid_run_input_fails_without_running(tmp_path: Path) -> None:
    orch, provider, _ = make_orch(tmp_path, scored_pipeline())
    result = orch.run({"value": "not a number"})
    assert result.status == "failed"
    assert {s.status for s in result.steps.values()} == {"not_run"}
    assert provider.calls == []
    assert verify_run(result.run_dir).ok


def test_bad_handoff_fails_step_and_skips_downstream(tmp_path: Path) -> None:
    def bad_bind(h: Handoff) -> dict[str, object]:
        return {"value": "oops"}

    p = Pipeline(
        "h",
        Num,
        (
            Step("a", adapt(double)),
            Step("b", adapt(double), after=("a",), bind=bad_bind),
            Step("c", adapt(double), after=("b",)),
        ),
    )
    orch, _, _ = make_orch(tmp_path, p)
    result = orch.run({"value": 1})
    assert result.steps["b"].status == "failed"
    assert "handoff into b" in result.steps["b"].error
    assert result.steps["c"].status == "skipped"
    assert result.status == "failed"
    assert events_of(result.run_dir, "handoff_invalid")


def test_bind_exception_is_a_handoff_error(tmp_path: Path) -> None:
    def boom(h: Handoff) -> Num:
        raise KeyError("x")

    p = Pipeline("h", Num, (Step("a", adapt(double), bind=boom),))
    orch, _, _ = make_orch(tmp_path, p)
    assert orch.run({"value": 1}).steps["a"].error.startswith("bind for a raised KeyError")


def test_transient_faults_retry_with_backoff(tmp_path: Path) -> None:
    provider = MockProvider(
        responder=scripted({}),
        faults=[TransientError("502"), TransientError("timeout"), None],
    )
    orch, _, sleeps = make_orch(tmp_path, scored_pipeline(), provider)
    result = orch.run({"value": 1})
    assert result.status == "succeeded"
    assert result.steps["score"].attempts == 3
    assert result.steps["score"].tiers_tried == ["fast"]
    assert sleeps == [0.5, 1.0]
    assert len(events_of(result.run_dir, "llm_error")) == 2


def test_retry_after_hint_is_honoured(tmp_path: Path) -> None:
    provider = MockProvider(responder=scripted({}), faults=[TransientError("429", retry_after=2.5)])
    orch, _, sleeps = make_orch(tmp_path, scored_pipeline(), provider)
    assert orch.run({"value": 1}).status == "succeeded"
    assert sleeps == [2.5]


def test_fatal_provider_error_is_not_retried(tmp_path: Path) -> None:
    provider = MockProvider(faults=[ProviderFatal("HTTP 401")])
    orch, _, sleeps = make_orch(tmp_path, scored_pipeline(), provider)
    result = orch.run({"value": 1})
    assert result.steps["score"].status == "failed"
    assert result.steps["score"].attempts == 1
    assert sleeps == []


def test_retries_exhausted_then_escalates_then_review(tmp_path: Path) -> None:
    provider = MockProvider(faults=[TransientError("down")] * 20)
    orch, _, _ = make_orch(tmp_path, scored_pipeline(), provider)
    result = orch.run({"value": 1})
    step = result.steps["score"]
    assert step.status == "needs_review"
    assert step.tiers_tried == ["fast", "standard", "strong"]
    assert step.attempts == 9
    skipped = events_of(result.run_dir, "tier_skipped")
    assert [e["tier"] for e in skipped] == ["claude"]
    assert "is approved" in skipped[0]["reason"]
    queue = [
        json.loads(line) for line in (result.run_dir / "review.jsonl").read_text().splitlines()
    ]
    assert queue[0]["kind"] == "review" and queue[0]["candidate_output"] is None
    assert result.status == "needs_review"


def test_low_confidence_escalates_to_stronger_tier(tmp_path: Path) -> None:
    provider = MockProvider(responder=scripted({"8B": 0.4, "70B": 0.95}))
    orch, _, _ = make_orch(tmp_path, scored_pipeline(min_confidence=0.8), provider)
    result = orch.run({"value": 1})
    step = result.steps["score"]
    assert step.status == "succeeded"
    assert step.tiers_tried == ["fast", "standard"]
    assert step.confidence == 0.95
    assert [c.model for c in provider.calls] == [
        "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo",
        "meta-llama/Llama-3.3-70B-Instruct-Turbo",
    ]
    (esc,) = events_of(result.run_dir, "escalate")
    assert esc["from_tier"] == "fast" and esc["to_tier"] == "standard"


def test_low_confidence_everywhere_goes_to_human_with_candidate(tmp_path: Path) -> None:
    provider = MockProvider(responder=scripted({"": 0.3}))
    orch, _, _ = make_orch(tmp_path, scored_pipeline(min_confidence=0.8), provider)
    result = orch.run({"value": 1})
    step = result.steps["score"]
    assert step.status == "needs_review"
    assert step.confidence == 0.3 and step.output is None
    item = result.review_items[0]
    assert item["candidate_output"] == {"value": 7, "confidence": 0.3}
    assert "below 0.80 on strong" in item["reason"]


def test_on_exhausted_fail_and_no_escalation(tmp_path: Path) -> None:
    provider = MockProvider(responder=scripted({"": 0.3}))
    p = scored_pipeline(min_confidence=0.8, escalate=False, on_exhausted="fail")
    orch, _, _ = make_orch(tmp_path, p, provider)
    result = orch.run({"value": 1})
    assert result.steps["score"].status == "failed"
    assert result.steps["score"].tiers_tried == ["fast"]
    assert result.review_items == []


def test_wrong_output_type_is_retryable(tmp_path: Path) -> None:
    calls = {"n": 0}

    def flaky(x: Num) -> Num:
        calls["n"] += 1
        if calls["n"] == 1:
            return Scored(value=1, confidence=1)  # type: ignore[return-value]
        if calls["n"] == 2:
            return {"value": "x"}  # type: ignore[return-value]
        return {"value": 5}  # type: ignore[return-value]

    p = Pipeline("o", Num, (Step("a", adapt(flaky), retry=FAST_RETRY),))
    orch, _, _ = make_orch(tmp_path, p)
    result = orch.run({"value": 1})
    assert result.status == "succeeded" and result.output("a") == Num(value=5)
    errors = [e["error"] for e in events_of(result.run_dir, "attempt_error")]
    assert errors == ["OutputValidationError", "OutputValidationError"]


def test_generic_agent_exception_is_retried(tmp_path: Path) -> None:
    calls = {"n": 0}

    def sometimes(x: Num) -> Num:
        calls["n"] += 1
        if calls["n"] < 2:
            raise ValueError("model said something odd")
        return x

    p = Pipeline("g", Num, (Step("a", adapt(sometimes), retry=FAST_RETRY),))
    orch, _, _ = make_orch(tmp_path, p)
    assert orch.run({"value": 1}).steps["a"].attempts == 2


def test_timeout_abandons_attempt_and_late_calls_are_refused(tmp_path: Path) -> None:
    late: dict[str, BaseException] = {}
    done = threading.Event()

    def slow(x: Num, ctx: AgentContext) -> Num:
        if ctx.attempt == 1:
            time.sleep(0.4)
            try:
                ctx.llm.complete("too late")
            except Cancelled as exc:
                late["err"] = exc
            try:
                ctx.effect("email.send", "a@example.com", {"x": 1})
            except Cancelled as exc:
                late["effect"] = exc
            done.set()
        return x

    p = Pipeline("t", Num, (Step("a", adapt(slow), timeout_s=0.1, retry=FAST_RETRY),))
    orch, provider, _ = make_orch(tmp_path, p)
    result = orch.run({"value": 3})
    assert result.status == "succeeded"
    assert result.steps["a"].attempts == 2
    (err,) = events_of(result.run_dir, "attempt_error")
    assert err["error"] == "StepTimeout"
    assert done.wait(2)
    assert isinstance(late["err"], Cancelled) and isinstance(late["effect"], Cancelled)
    assert provider.calls == []


def test_dollar_budget_refuses_before_sending(tmp_path: Path) -> None:
    limits = BudgetLimits(max_usd=Decimal("0.000001"), max_tokens=10_000, max_seconds=None)
    p = Pipeline(
        "b",
        Num,
        (
            Step("score", adapt(llm_scored), retry=FAST_RETRY),
            Step("after", adapt(double), after=()),
        ),
    )
    orch, provider, _ = make_orch(tmp_path, p, limits=limits)
    result = orch.run({"value": 1})
    assert result.status == "budget_exceeded"
    assert result.steps["score"].status == "budget_exceeded"
    assert result.steps["score"].attempts == 1  # never retried
    assert provider.calls == []
    assert Decimal(result.budget["spent_usd"]) == 0


def test_budget_stop_marks_later_layers_not_run(tmp_path: Path) -> None:
    limits = BudgetLimits(max_usd=Decimal("1"), max_tokens=10, max_seconds=None)
    p = Pipeline(
        "b",
        Num,
        (
            Step("score", adapt(llm_scored)),
            Step("next", adapt(double), after=("score",), bind=lambda h: h.run_input),
        ),
    )
    orch, provider, _ = make_orch(tmp_path, p, limits=limits)
    result = orch.run({"value": 1})
    assert result.steps["score"].status == "budget_exceeded"
    assert result.steps["next"].status in ("skipped", "not_run")
    assert provider.calls == []


def test_wall_clock_budget(tmp_path: Path) -> None:
    def slow(x: Num) -> Num:
        time.sleep(0.5)
        return x

    limits = BudgetLimits(max_usd=Decimal("1"), max_tokens=100, max_seconds=0.1)
    p = Pipeline("w", Num, (Step("a", adapt(slow), timeout_s=30),))
    orch, _, _ = make_orch(tmp_path, p, limits=limits)
    result = orch.run({"value": 1})
    assert result.status == "budget_exceeded"
    assert "wall-clock" in result.steps["a"].error


def test_maybe_billed_failure_is_charged_worst_case(tmp_path: Path) -> None:
    provider = MockProvider(faults=[TransientError("reset", maybe_billed=True)] * 3)
    p = scored_pipeline(escalate=False)
    orch, _, _ = make_orch(tmp_path, p, provider)
    result = orch.run({"value": 1})
    assert Decimal(result.budget["spent_usd"]) > 0
    assert result.budget["reserved_usd"] == "0"
    errors = events_of(result.run_dir, "llm_error")
    assert all(e["charged_worst_case"] for e in errors)


def test_random_fault_injection_never_breaks_invariants(tmp_path: Path) -> None:
    import random

    for seed in range(15):
        rng = random.Random(seed)
        faults: list[BaseException | None] = [
            rng.choice([None, None, TransientError("x"), TransientError("y", maybe_billed=True)])
            for _ in range(30)
        ]
        limits = BudgetLimits(max_usd=Decimal("0.00002"), max_tokens=100_000, max_seconds=None)
        provider = MockProvider(responder=scripted({"8B": rng.random()}), faults=faults)
        orch, _, _ = make_orch(
            tmp_path / str(seed), scored_pipeline(min_confidence=0.5), provider, limits=limits
        )
        result = orch.run({"value": seed})
        assert Decimal(result.budget["spent_usd"]) <= limits.max_usd
        assert result.budget["reserved_usd"] == "0"
        report = verify_run(result.run_dir)
        assert report.ok, report.errors
        assert result.status in ("succeeded", "needs_review", "budget_exceeded")


def test_parallel_layer_runs_concurrently(tmp_path: Path) -> None:
    barrier = threading.Barrier(2, timeout=5)

    def meet(x: Num) -> Num:
        barrier.wait()  # deadlocks (then times out) unless both run at once
        return Num(value=x.value + 1)

    def join(h: Handoff) -> Num:
        b, c = h.outputs["b"], h.outputs["c"]
        assert isinstance(b, Num) and isinstance(c, Num)
        return Num(value=b.value + c.value)

    p = Pipeline(
        "diamond",
        Num,
        (
            Step("a", adapt(double)),
            Step("b", adapt(meet), after=("a",)),
            Step("c", adapt(meet), after=("a",)),
            Step("d", adapt(double), after=("b", "c"), bind=join),
        ),
    )
    orch, _, _ = make_orch(tmp_path, p, max_parallel=2)
    result = orch.run({"value": 1})
    assert result.status == "succeeded"
    assert result.output("d") == Num(value=12)


def test_context_free_agent_can_use_current_llm(tmp_path: Path) -> None:
    def plain(x: Num) -> Scored:
        assert current_context().step_id == "a"
        data = json.loads(current_llm().complete("hi", max_tokens=20))
        return Scored(value=data["value"], confidence=data["confidence"])

    p = Pipeline("c", Num, (Step("a", adapt(plain)),))
    orch, provider, _ = make_orch(tmp_path, p, MockProvider(responder=scripted({})))
    assert orch.run({"value": 1}).status == "succeeded"
    assert len(provider.calls) == 1
    with pytest.raises(ConfigError):
        current_context()


def test_complete_json_validates_and_strips_fences(tmp_path: Path) -> None:
    answers = iter(['```json\n{"value": 3, "confidence": 0.9}\n```', "not json"])

    def agent(x: Num, ctx: AgentContext) -> Scored:
        return ctx.llm.complete_json("q", Scored, max_tokens=20)

    p = Pipeline("j", Num, (Step("a", adapt(agent), retry=RetryPolicy(max_attempts=1)),))
    orch, _, _ = make_orch(tmp_path, p, MockProvider(responder=lambda r: next(answers)))
    assert orch.run({"value": 1}).output("a") == Scored(value=3, confidence=0.9)
    orch2, _, _ = make_orch(tmp_path / "2", p, MockProvider(responder=lambda r: "nope"))
    result = orch2.run({"value": 1})
    assert "OutputValidationError" in result.steps["a"].error
    assert OutputValidationError.__name__ in result.steps["a"].error


def test_unknown_start_tier_is_rejected_up_front(tmp_path: Path) -> None:
    p = Pipeline("u", Num, (Step("a", adapt(double), start_tier="gpt9"),))
    with pytest.raises(ConfigError, match="unknown tier"):
        make_orch(tmp_path, p)


def test_disabled_start_tier_moves_up(tmp_path: Path) -> None:
    p = Pipeline("u", Num, (Step("a", adapt(llm_scored), start_tier="claude"),))
    orch, _, _ = make_orch(tmp_path, p, MockProvider(responder=scripted({})))
    result = orch.run({"value": 1})
    assert result.steps["a"].status == "needs_review"
    assert result.steps["a"].tiers_tried == []
    assert events_of(result.run_dir, "tier_skipped")[0]["tier"] == "claude"


def test_invalid_override_fails_step(tmp_path: Path) -> None:
    p = Pipeline("o", Num, (Step("a", adapt(double)),))
    orch, _, _ = make_orch(tmp_path, p)
    result = orch.run({"value": 1}, overrides={"a": ({"value": "x"}, "human:test")})
    assert result.steps["a"].status == "failed"
    assert "override" in result.steps["a"].error


def test_orchestrator_rejects_bad_parallelism(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        make_orch(tmp_path, Pipeline("p", Num, (Step("a", adapt(double)),)), max_parallel=0)


def test_llm_rejects_nonpositive_max_tokens(tmp_path: Path) -> None:
    def agent(x: Num, ctx: AgentContext) -> Num:
        ctx.llm.complete("x", max_tokens=0)
        return x

    p = Pipeline("m", Num, (Step("a", adapt(agent)),))
    orch, _, _ = make_orch(tmp_path, p)
    assert orch.run({"value": 1}).steps["a"].status == "failed"


class Note(BaseModel):
    ok: bool


def test_notes_are_audited(tmp_path: Path) -> None:
    def agent(x: Num, ctx: AgentContext) -> Note:
        ctx.note("checked", rows=3)
        return Note(ok=True)

    p = Pipeline("n", Num, (Step("a", adapt(agent)),))
    orch, _, _ = make_orch(tmp_path, p)
    result = orch.run({"value": 1})
    (note,) = events_of(result.run_dir, "note")
    assert note["message"] == "checked" and note["rows"] == 3
