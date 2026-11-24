"""catchpr.orchestra: a reliability and orchestration layer for typed agent pipelines.

Public API::

    from catchpr.orchestra import (
        Pipeline, Step, RetryPolicy, adapt, load_agent, AgentContext,
        current_context, current_llm, Orchestrator, BudgetLimits, build_tierset,
    )
"""

from catchpr.orchestra.agents import (
    AgentContext,
    AgentSpec,
    adapt,
    current_context,
    current_llm,
    load_agent,
)
from catchpr.orchestra.budget import BudgetLimits
from catchpr.orchestra.engine import Orchestrator, RunResult, StepResult
from catchpr.orchestra.guard import EffectGate, EffectLedger, EffectRequest
from catchpr.orchestra.pipeline import Handoff, Pipeline, RetryPolicy, Step
from catchpr.orchestra.tiers import Tier, TierSet, build_tierset, default_tiers

__all__ = [
    "AgentContext",
    "AgentSpec",
    "BudgetLimits",
    "EffectGate",
    "EffectLedger",
    "EffectRequest",
    "Handoff",
    "Orchestrator",
    "Pipeline",
    "RetryPolicy",
    "RunResult",
    "Step",
    "StepResult",
    "Tier",
    "TierSet",
    "adapt",
    "build_tierset",
    "current_context",
    "current_llm",
    "default_tiers",
    "load_agent",
]
