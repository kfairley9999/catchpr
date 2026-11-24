"""Agent adapter interface.

An agent is any callable ``run(inp: InModel) -> OutModel`` or
``run(inp: InModel, ctx: AgentContext) -> OutModel`` where both models are
pydantic ``BaseModel`` subclasses. :func:`adapt` reads the models from the
type hints, so the COI and LeadFlow modules plug in without changes.

Agents that do not take ``ctx`` can still reach the metered model client and
the effect gate through :func:`current_context` / :func:`current_llm`.
"""

from __future__ import annotations

import importlib
import inspect
import json
import threading
import time
import typing
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from catchpr.orchestra.audit import AuditLog
from catchpr.orchestra.budget import Budget, money
from catchpr.orchestra.errors import (
    Cancelled,
    ConfigError,
    OutputValidationError,
    TransientError,
)
from catchpr.orchestra.guard import EffectGate, EffectRequest
from catchpr.orchestra.pricing import DEEPINFRA_PRICES, Price
from catchpr.orchestra.providers import LLMProvider, LLMRequest, Message
from catchpr.orchestra.tiers import Tier

T = TypeVar("T", bound=BaseModel)

_CURRENT: ContextVar[AgentContext | None] = ContextVar("catchpr_agent_ctx", default=None)


@dataclass(frozen=True)
class AgentSpec:
    name: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    fn: Callable[..., Any]
    takes_context: bool
    version: str = "0"

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "input_model": _qualname(self.input_model),
            "output_model": _qualname(self.output_model),
        }


def _qualname(obj: Any) -> str:
    return f"{obj.__module__}.{obj.__qualname__}"


def _is_model(obj: Any) -> bool:
    return isinstance(obj, type) and issubclass(obj, BaseModel)


def adapt(
    fn: Callable[..., Any],
    *,
    name: str | None = None,
    input_model: type[BaseModel] | None = None,
    output_model: type[BaseModel] | None = None,
    version: str = "0",
) -> AgentSpec:
    """Wrap a typed ``run`` function as an agent, inferring models from hints."""
    if not callable(fn):
        raise ConfigError(f"agent {fn!r} is not callable")
    params = [
        p
        for p in inspect.signature(fn).parameters.values()
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    label = name or getattr(fn, "__module__", "agent") + "." + getattr(fn, "__name__", "run")
    if not params:
        raise ConfigError(f"agent {label} must take (input) or (input, ctx)")
    try:
        hints = typing.get_type_hints(fn)
    except Exception as exc:  # unresolved forward refs etc.
        if input_model is None or output_model is None:
            raise ConfigError(f"cannot resolve type hints for {fn!r}: {exc}") from exc
        hints = {}  # explicit models given; the context is detected by the name "ctx"
    # The second parameter receives the context only if it is annotated
    # AgentContext or named ``ctx``. Any other extra parameter (for example
    # ``provider=None`` in an existing agent) must have a default and is left alone.
    takes_context = False
    for extra in params[1:]:
        is_ctx = extra is params[1] and (
            hints.get(extra.name) is AgentContext or extra.name == "ctx"
        )
        if is_ctx:
            takes_context = True
        elif extra.default is inspect.Parameter.empty:
            raise ConfigError(
                f"agent {label} must take (input) or (input, ctx); "
                f"parameter {extra.name!r} has no default"
            )
    in_model = input_model or hints.get(params[0].name)
    out_model = output_model or hints.get("return")
    if not _is_model(in_model):
        raise ConfigError(f"agent {label}: input type must be a pydantic BaseModel")
    if not _is_model(out_model):
        raise ConfigError(f"agent {label}: return type must be a pydantic BaseModel")
    assert isinstance(in_model, type) and isinstance(out_model, type)
    return AgentSpec(label, in_model, out_model, fn, takes_context, version)


def load_agent(ref: str, **kwargs: Any) -> AgentSpec:
    """Load ``package.module:function`` (``:run`` is the default attribute)."""
    module_name, _, attr = ref.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ConfigError(f"cannot import agent module {module_name!r}: {exc}") from exc
    fn = getattr(module, attr or "run", None)
    if fn is None:
        raise ConfigError(f"{module_name} has no attribute {attr or 'run'!r}")
    version = kwargs.pop("version", getattr(module, "__version__", "0"))
    return adapt(fn, name=kwargs.pop("name", ref), version=str(version), **kwargs)


@dataclass
class CallStats:
    calls: int = 0
    tokens: int = 0
    usd: Decimal = field(default_factory=lambda: Decimal(0))


@dataclass(frozen=True)
class CallResult:
    text: str
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    latency_s: float
    simulated: bool
    usage_reported: bool


class LLMClient:
    """The metered model client handed to agents for the current tier."""

    def __init__(self, ctx: AgentContext) -> None:
        self._ctx = ctx

    @property
    def model(self) -> str:
        return self._ctx.tier.model

    @property
    def tier(self) -> str:
        return self._ctx.tier.name

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
        json_mode: bool = False,
    ) -> str:
        """One metered model call on the current tier. Returns the text."""
        messages = [("system", system)] if system else []
        return self.call(
            [*messages, ("user", prompt)],
            max_tokens=max_tokens,
            temperature=temperature,
            json_mode=json_mode,
        ).text

    def call(
        self,
        messages: list[tuple[str, str]],
        *,
        max_tokens: int = 512,
        temperature: float = 0.0,
        json_mode: bool = False,
        images_png: tuple[bytes, ...] = (),
        model: str | None = None,
    ) -> CallResult:
        """Metered call with full usage details.

        ``model`` pins a specific model on the tier's provider (for example a
        vision model for scanned pages). It must have a known price, and a
        pinned model does not change when the step escalates, so prefer the
        tier model wherever the task allows.
        """
        ctx = self._ctx
        ctx.ensure_active()
        if max_tokens <= 0:
            raise ConfigError("max_tokens must be positive")
        if not messages:
            raise ConfigError("at least one message is required")
        chosen = ctx.tier.model if model is None else model
        price = ctx.tier.price if model is None else ctx.pinned_price(model)
        assert price is not None  # TierSet guarantees enabled tiers have a price
        request = LLMRequest(
            model=chosen,
            messages=tuple(Message(role, content) for role, content in messages),
            max_tokens=max_tokens,
            temperature=temperature,
            json_mode=json_mode,
            images_png=tuple(images_png),
        )
        est_in = request.estimated_input_tokens()
        reservation = ctx.budget.reserve(est_in + max_tokens, price.cost(est_in, max_tokens))
        req_sha = ctx.audit.ref(request.as_dict())
        started = time.monotonic()
        timeout = max(0.1, ctx.deadline - time.monotonic())
        try:
            completion = ctx.provider.complete(request, timeout=timeout)
        except TransientError as exc:
            if exc.maybe_billed:
                ctx.budget.settle(reservation, reservation.tokens, reservation.usd)
            else:
                ctx.budget.release(reservation)
            ctx.audit.emit(
                "llm_error",
                step=ctx.step_id,
                attempt=ctx.attempt,
                tier=ctx.tier.name,
                provider=ctx.provider.name,
                model=chosen,
                request_sha256=req_sha,
                error=type(exc).__name__,
                message=str(exc)[:300],
                charged_worst_case=exc.maybe_billed,
                latency_ms=round((time.monotonic() - started) * 1000, 1),
            )
            raise
        except BaseException:
            ctx.budget.settle(reservation, reservation.tokens, reservation.usd)
            raise
        latency_s = time.monotonic() - started
        tokens = completion.input_tokens + completion.output_tokens
        usd = price.cost(completion.input_tokens, completion.output_tokens)
        overshoot = ctx.budget.settle(reservation, tokens, usd)
        ctx.stats.calls += 1
        ctx.stats.tokens += tokens
        ctx.stats.usd += usd
        ctx.audit.emit(
            "llm_call",
            step=ctx.step_id,
            attempt=ctx.attempt,
            tier=ctx.tier.name,
            provider=ctx.provider.name,
            model=completion.model,
            pinned_model=model is not None,
            request_sha256=req_sha,
            request_digest=request.digest(),
            response_sha256=ctx.audit.ref(completion.as_dict()),
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            cost_usd=money(usd),
            latency_ms=round(latency_s * 1000, 1),
            usage_reported=completion.usage_reported,
            simulated=completion.simulated,
            budget_overshoot=overshoot,
            discarded=ctx.cancelled,
        )
        ctx.ensure_active()  # a call that returns after a timeout is not used
        return CallResult(
            text=completion.text,
            model=completion.model,
            provider=ctx.provider.name,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            cost_usd=usd,
            latency_s=latency_s,
            simulated=completion.simulated,
            usage_reported=completion.usage_reported,
        )

    def complete_json(self, prompt: str, model: type[T], **kwargs: Any) -> T:
        """Ask for JSON and validate it. Bad output raises a retryable error."""
        text = self.complete(prompt, json_mode=True, **kwargs)
        try:
            return model.model_validate(json.loads(_strip_fences(text)))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise OutputValidationError(
                f"model output did not match {model.__name__}: {str(exc)[:200]}"
            ) from exc


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    return stripped.strip()


class AgentContext:
    """Per-attempt context: current tier, metered LLM, effect gate, audit notes."""

    def __init__(
        self,
        *,
        run_id: str,
        step_id: str,
        attempt: int,
        tier: Tier,
        provider: LLMProvider,
        budget: Budget,
        audit: AuditLog,
        gate: EffectGate,
        deadline: float,
    ) -> None:
        self.run_id = run_id
        self.step_id = step_id
        self.attempt = attempt
        self.tier = tier
        self.provider = provider
        self.budget = budget
        self.audit = audit
        self.gate = gate
        self.deadline = deadline
        self.stats = CallStats()
        self._cancel = threading.Event()
        self.llm = LLMClient(self)

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def cancel(self) -> None:
        self._cancel.set()

    def pinned_price(self, model: str) -> Price:
        if self.tier.provider not in ("deepinfra", "mock"):
            raise ConfigError(
                f"model pinning is only supported on DeepInfra tiers, not {self.tier.name}"
            )
        if model == self.tier.model and self.tier.price is not None:
            return self.tier.price
        price = DEEPINFRA_PRICES.get(model)
        if price is None:
            raise ConfigError(f"pinned model {model!r} has no known price; refusing to run")
        return price

    def ensure_active(self) -> None:
        if self._cancel.is_set():
            raise Cancelled(f"step {self.step_id} attempt {self.attempt} is no longer active")

    def effect(self, action: str, target: str, payload: Any) -> dict[str, Any]:
        """Request a side effect. Runs only with a matching human approval token."""
        self.ensure_active()
        request = EffectRequest(action=action, target=target, payload=payload)
        payload_sha = self.audit.ref(payload)
        outcome = self.gate.perform(request, run_id=self.run_id, step_id=self.step_id)
        self.audit.emit(
            "effect_executed",
            step=self.step_id,
            attempt=self.attempt,
            action=action,
            target=target,
            request_id=request.request_id,
            payload_sha256=payload_sha,
            approver=outcome.approver,
            deduplicated=outcome.deduplicated,
            result=outcome.result,
        )
        return outcome.result

    def effects(self, items: list[tuple[str, str, Any]]) -> list[dict[str, Any] | None]:
        """Request several effects at once (e.g. a batch of emails).

        Approved ones execute; if any lack approval, one ApprovalRequired
        listing all of them is raised after the approved ones ran, so a human
        can approve the whole batch in one pass. Executed effects are
        deduplicated by the ledger when the step is resumed.
        """
        from catchpr.orchestra.errors import ApprovalRequired

        results: list[dict[str, Any] | None] = []
        missing: list[ApprovalRequired] = []
        for action, target, payload in items:
            try:
                results.append(self.effect(action, target, payload))
            except ApprovalRequired as exc:
                missing.append(exc)
                results.append(None)
        if missing:
            first = missing[0]
            raise ApprovalRequired(
                first.request, first.reason, others=tuple(m.request for m in missing[1:])
            )
        return results

    def note(self, message: str, **data: Any) -> None:
        self.audit.emit("note", step=self.step_id, attempt=self.attempt, message=message, **data)


def current_context() -> AgentContext:
    ctx = _CURRENT.get()
    if ctx is None:
        raise ConfigError("no agent context: this code is not running under the orchestrator")
    return ctx


def current_llm() -> LLMClient:
    return current_context().llm


def bind_context(ctx: AgentContext) -> Any:
    return _CURRENT.set(ctx)
