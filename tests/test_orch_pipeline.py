from __future__ import annotations

import random

import pytest
from pydantic import BaseModel
from tests.orch_support import Num, Scored, double, llm_scored

from catchpr.orchestra.agents import adapt, load_agent
from catchpr.orchestra.errors import ConfigError
from catchpr.orchestra.pipeline import Handoff, Pipeline, RetryPolicy, Step


class Other(BaseModel):
    name: str


def to_other(x: Num) -> Other:
    return Other(name=str(x.value))


def test_adapt_infers_models_and_context_arity() -> None:
    spec = adapt(llm_scored)
    assert spec.input_model is Num and spec.output_model is Scored
    assert spec.takes_context
    assert not adapt(double).takes_context


def test_adapt_rejects_untyped_and_wrong_arity() -> None:
    def untyped(x):  # type: ignore[no-untyped-def]
        return x

    def three(a: Num, b: int, c: int) -> Num:
        return a

    def returns_int(x: Num) -> int:
        return 1

    with pytest.raises(ConfigError, match="input type"):
        adapt(untyped)
    with pytest.raises(ConfigError, match="must take"):
        adapt(three)
    with pytest.raises(ConfigError, match="return type"):
        adapt(returns_int)
    with pytest.raises(ConfigError, match="not callable"):
        adapt(42)  # type: ignore[arg-type]


def test_load_agent_by_reference() -> None:
    spec = load_agent("catchpr.orchestra.examples.coi:intake")
    assert spec.name == "catchpr.orchestra.examples.coi:intake"
    assert spec.output_model.__name__ == "IntakeResult"
    with pytest.raises(ConfigError, match="cannot import"):
        load_agent("no_such_module_xyz:run")
    with pytest.raises(ConfigError, match="no attribute"):
        load_agent("catchpr.orchestra.examples.coi")


def _p(*steps: Step, input_model: type[BaseModel] = Num) -> Pipeline:
    return Pipeline("t", input_model, tuple(steps))


def test_validation_errors() -> None:
    a = adapt(double)
    with pytest.raises(ConfigError, match="no steps"):
        _p()
    with pytest.raises(ConfigError, match="duplicate"):
        _p(Step("a", a), Step("a", a))
    with pytest.raises(ConfigError, match="unknown step"):
        _p(Step("a", a, after=("zzz",)))
    with pytest.raises(ConfigError, match="itself"):
        _p(Step("a", a, after=("a",)))
    with pytest.raises(ConfigError, match="cycle"):
        _p(Step("a", a, after=("b",)), Step("b", a, after=("a",)))
    with pytest.raises(ConfigError, match="bind function"):
        _p(Step("a", a), Step("b", a), Step("c", a, after=("a", "b")))
    with pytest.raises(ConfigError, match="confidence"):
        _p(Step("a", a, min_confidence=0.5))
    with pytest.raises(ConfigError, match="lacks required"):
        _p(Step("a", adapt(to_other)), Step("b", a, after=("a",)))
    with pytest.raises(ConfigError, match="alphanumeric"):
        Step("bad id!", a)
    with pytest.raises(ConfigError, match="timeout"):
        Step("a", a, timeout_s=0)
    with pytest.raises(ConfigError, match="within"):
        Step("a", adapt(llm_scored), min_confidence=1.5)


def test_levels_are_topological_layers() -> None:
    a = adapt(double)

    def join(h: Handoff) -> Num:
        b, c = h.outputs["b"], h.outputs["c"]
        assert isinstance(b, Num) and isinstance(c, Num)
        return Num(value=b.value + c.value)

    p = _p(
        Step("a", a),
        Step("b", a, after=("a",)),
        Step("c", a, after=("a",)),
        Step("d", a, after=("b", "c"), bind=join),
    )
    assert [[s.id for s in layer] for layer in p.levels()] == [["a"], ["b", "c"], ["d"]]
    assert p.describe()["steps"][3]["after"] == ["b", "c"]
    with pytest.raises(ConfigError):
        p.step("nope")


class MaybeScored(BaseModel):
    confidence: float | None = None


def test_min_confidence_accepts_optional_float() -> None:
    def f(x: Num) -> MaybeScored:
        return MaybeScored()

    _p(Step("a", adapt(f), min_confidence=0.5))


def test_retry_policy_backoff() -> None:
    policy = RetryPolicy(base_delay=0.5, multiplier=2, max_delay=3, jitter=0)
    rng = random.Random(1)
    assert [policy.delay(n, rng) for n in (1, 2, 3, 4)] == [0.5, 1.0, 2.0, 3.0]
    assert policy.delay(1, rng, retry_after=10) == 3
    assert policy.delay(1, rng, retry_after=0.2) == 0.2
    jittery = RetryPolicy(base_delay=1, jitter=0.5)
    for _ in range(50):
        assert 1 <= jittery.delay(1, rng) <= 1.5
    with pytest.raises(ConfigError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ConfigError):
        RetryPolicy(multiplier=0.5)
