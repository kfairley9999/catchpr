"""Shared fixtures for the orchestrator tests."""

from __future__ import annotations

import json
import random
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from catchpr.orchestra.agents import AgentContext
from catchpr.orchestra.budget import BudgetLimits
from catchpr.orchestra.engine import Orchestrator
from catchpr.orchestra.guard import EffectGate, EffectLedger
from catchpr.orchestra.pipeline import Pipeline
from catchpr.orchestra.providers import LLMRequest, MockProvider
from catchpr.orchestra.tiers import Tier, default_tiers, with_providers


class Num(BaseModel):
    value: int


class Scored(BaseModel):
    value: int
    confidence: float


def double(x: Num) -> Num:
    return Num(value=x.value * 2)


def llm_scored(x: Num, ctx: AgentContext) -> Scored:
    data = json.loads(ctx.llm.complete(f"score {x.value}", max_tokens=50))
    return Scored(value=int(data["value"]), confidence=float(data["confidence"]))


def scripted(by_model: dict[str, float], value: int = 7) -> Any:
    """Responder returning a fixed confidence per model substring."""

    def respond(request: LLMRequest) -> str:
        for needle, conf in by_model.items():
            if needle in request.model:
                return json.dumps({"value": value, "confidence": conf})
        return json.dumps({"value": value, "confidence": 0.99})

    return respond


class Sleeps(list[float]):
    def __call__(self, seconds: float) -> None:
        self.append(seconds)


def make_orch(
    tmp_path: Path,
    pipeline: Pipeline,
    provider: MockProvider | None = None,
    *,
    tiers: list[Tier] | None = None,
    limits: BudgetLimits | None = None,
    gate: EffectGate | None = None,
    max_parallel: int = 1,
    store_payloads: bool = True,
) -> tuple[Orchestrator, MockProvider, Sleeps]:
    provider = provider or MockProvider()
    sleeps = Sleeps()
    runs = tmp_path / "runs"
    orch = Orchestrator(
        pipeline,
        with_providers(tiers or default_tiers(), provider, simulated=True),
        runs_dir=runs,
        limits=limits or BudgetLimits(max_usd=Decimal("1"), max_tokens=1_000_000, max_seconds=None),
        gate=gate
        or EffectGate(None, EffectLedger(runs / "effects-ledger.jsonl"), pipeline.effects),
        sleep=sleeps,
        rng=random.Random(0),
        max_parallel=max_parallel,
        store_payloads=store_payloads,
    )
    return orch, provider, sleeps
