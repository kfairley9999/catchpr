"""Pipeline definition: typed steps wired into a DAG, validated before running."""

from __future__ import annotations

import random
import types
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel

from catchpr.orchestra.agents import AgentSpec
from catchpr.orchestra.errors import ConfigError
from catchpr.orchestra.guard import Executor


@dataclass(frozen=True)
class RetryPolicy:
    """Exponential backoff with bounded jitter. ``max_attempts`` is per tier."""

    max_attempts: int = 3
    base_delay: float = 0.5
    multiplier: float = 2.0
    max_delay: float = 8.0
    jitter: float = 0.1

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ConfigError("max_attempts must be at least 1")
        if self.base_delay < 0 or self.max_delay < 0 or self.multiplier < 1 or self.jitter < 0:
            raise ConfigError("retry delays must be non-negative and multiplier >= 1")

    def delay(self, attempt: int, rng: random.Random, retry_after: float | None = None) -> float:
        """Delay after failed ``attempt`` (1-based). A server hint wins, capped."""
        if retry_after is not None:
            return min(self.max_delay, retry_after)
        base = min(self.max_delay, self.base_delay * self.multiplier ** (attempt - 1))
        return base + rng.uniform(0, self.jitter * base)


@dataclass(frozen=True)
class Handoff:
    """What a ``bind`` function sees: the run input and finished upstream outputs."""

    run_input: BaseModel
    outputs: Mapping[str, BaseModel]


Binder = Callable[[Handoff], Any]


@dataclass(frozen=True)
class Step:
    id: str
    agent: AgentSpec
    after: tuple[str, ...] = ()
    bind: Binder | None = None
    timeout_s: float = 60.0
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    start_tier: str | None = None
    min_confidence: float | None = None
    escalate: bool = True
    allow_network: bool = False
    allow_subprocess: bool = False
    on_exhausted: Literal["review", "fail"] = "review"
    confidence_of: Callable[[BaseModel], float | None] | None = None
    # Dotted output paths that legitimately vary between identical runs (timings).
    # They are excluded from the stable digest that replay compares.
    volatile_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.id or not self.id.replace("_", "").replace("-", "").isalnum():
            raise ConfigError(f"step id {self.id!r} must be alphanumeric with - or _")
        if self.timeout_s <= 0:
            raise ConfigError(f"step {self.id}: timeout must be positive")
        if self.min_confidence is not None and not 0 <= self.min_confidence <= 1:
            raise ConfigError(f"step {self.id}: min_confidence must be within [0, 1]")


def _accepts_float(annotation: Any) -> bool:
    if annotation in (float, int):
        return True
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        return any(_accepts_float(a) for a in get_args(annotation) if a is not type(None))
    return False


def _required_fields(model: type[BaseModel]) -> set[str]:
    return {name for name, info in model.model_fields.items() if info.is_required()}


@dataclass(frozen=True)
class Pipeline:
    name: str
    input_model: type[BaseModel]
    steps: tuple[Step, ...]
    effects: Mapping[str, Executor] = field(default_factory=lambda: MappingProxyType({}))
    version: str = "0"

    def __post_init__(self) -> None:
        self.validate()

    def step(self, step_id: str) -> Step:
        for step in self.steps:
            if step.id == step_id:
                return step
        raise ConfigError(f"unknown step {step_id!r}")

    def validate(self) -> None:
        ids = [s.id for s in self.steps]
        if not ids:
            raise ConfigError(f"pipeline {self.name} has no steps")
        if len(set(ids)) != len(ids):
            raise ConfigError(f"pipeline {self.name} has duplicate step ids")
        known = set(ids)
        for step in self.steps:
            for dep in step.after:
                if dep not in known:
                    raise ConfigError(f"step {step.id} depends on unknown step {dep!r}")
                if dep == step.id:
                    raise ConfigError(f"step {step.id} depends on itself")
            if len(step.after) > 1 and step.bind is None:
                raise ConfigError(f"step {step.id} has several inputs and needs a bind function")
            if step.min_confidence is not None and step.confidence_of is None:
                info = step.agent.output_model.model_fields.get("confidence")
                if info is None or not _accepts_float(info.annotation):
                    raise ConfigError(
                        f"step {step.id} sets min_confidence but "
                        f"{step.agent.output_model.__name__} has no float 'confidence' field "
                        "and no confidence_of function"
                    )
            if step.bind is None:
                source = (
                    self.input_model
                    if not step.after
                    else self.step(step.after[0]).agent.output_model
                )
                missing = _required_fields(step.agent.input_model) - set(source.model_fields)
                if missing:
                    raise ConfigError(
                        f"handoff into {step.id}: {source.__name__} lacks required "
                        f"field(s) {sorted(missing)} of {step.agent.input_model.__name__}"
                    )
        self.levels()  # raises on cycles

    def levels(self) -> list[list[Step]]:
        """Topological layers (Kahn). Steps in a layer have no edges between them."""
        remaining = {s.id: set(s.after) for s in self.steps}
        order = {s.id: i for i, s in enumerate(self.steps)}
        layers: list[list[Step]] = []
        done: set[str] = set()
        while remaining:
            ready = sorted(
                (sid for sid, deps in remaining.items() if deps <= done), key=order.__getitem__
            )
            if not ready:
                raise ConfigError(f"pipeline {self.name} has a cycle among {sorted(remaining)}")
            layers.append([self.step(sid) for sid in ready])
            for sid in ready:
                del remaining[sid]
            done.update(ready)
        return layers

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "input_model": f"{self.input_model.__module__}.{self.input_model.__qualname__}",
            "steps": [
                {
                    "id": s.id,
                    "after": list(s.after),
                    "agent": s.agent.describe(),
                    "timeout_s": s.timeout_s,
                    "max_attempts": s.retry.max_attempts,
                    "start_tier": s.start_tier,
                    "min_confidence": s.min_confidence,
                    "escalate": s.escalate,
                    "allow_network": s.allow_network,
                    "allow_subprocess": s.allow_subprocess,
                }
                for s in self.steps
            ],
            "effects": sorted(self.effects),
        }
