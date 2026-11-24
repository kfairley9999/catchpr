"""The orchestrator: runs a validated pipeline with retries, timeouts, budgets,
confidence routing, approval-gated effects, and a hash-chained audit log."""

from __future__ import annotations

import contextvars
import json
import os
import random
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from catchpr.orchestra.agents import AgentContext, bind_context
from catchpr.orchestra.audit import AuditLog, BlobStore, utc_now
from catchpr.orchestra.budget import Budget, BudgetLimits, money
from catchpr.orchestra.errors import (
    ApprovalRequired,
    BudgetExceeded,
    ConfigError,
    OrchestraError,
    OutputValidationError,
    PermissionDenied,
    ProviderFatal,
    ReplayMismatch,
    SchemaError,
    StepTimeout,
    TransientError,
)
from catchpr.orchestra.guard import EffectGate, EffectLedger, agent_scope
from catchpr.orchestra.hashing import digest, to_jsonable
from catchpr.orchestra.pipeline import Handoff, Pipeline, Step
from catchpr.orchestra.tiers import Tier, TierSet

StepStatus = Literal[
    "succeeded",
    "needs_review",
    "awaiting_approval",
    "failed",
    "skipped",
    "budget_exceeded",
    "not_run",
]
RunStatus = Literal["succeeded", "needs_review", "awaiting_approval", "failed", "budget_exceeded"]

# Errors that a retry or a stronger model cannot fix.
_FATAL = (PermissionDenied, ConfigError, ProviderFatal, ReplayMismatch, SchemaError)


@dataclass
class StepResult:
    step_id: str
    status: StepStatus
    output: BaseModel | None = None
    output_sha256: str | None = None
    stable_sha256: str | None = None
    source: str = "agent"
    tiers_tried: list[str] = field(default_factory=list)
    attempts: int = 0
    error: str = ""
    confidence: float | None = None
    cost_usd: Decimal = field(default_factory=lambda: Decimal(0))
    tokens: int = 0
    review_item: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "output_sha256": self.output_sha256,
            "stable_sha256": self.stable_sha256,
            "source": self.source,
            "tiers_tried": self.tiers_tried,
            "attempts": self.attempts,
            "error": self.error,
            "confidence": self.confidence,
            "cost_usd": money(self.cost_usd),
            "tokens": self.tokens,
            "review_item": self.review_item,
        }


@dataclass
class RunResult:
    run_id: str
    run_dir: Path
    status: RunStatus
    steps: dict[str, StepResult]
    budget: dict[str, Any]
    notes: list[str]
    review_items: list[dict[str, Any]]
    head_hash: str

    def output(self, step_id: str) -> BaseModel | None:
        return self.steps[step_id].output


Override = tuple[Any, str]  # (output data, source label)


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(3)}"


class Orchestrator:
    def __init__(
        self,
        pipeline: Pipeline,
        tiers: TierSet,
        *,
        runs_dir: Path,
        limits: BudgetLimits | None = None,
        gate: EffectGate | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        max_parallel: int = 1,
        store_payloads: bool = True,
        pipeline_ref: str | None = None,
        utc_clock: Callable[[], str] = utc_now,
    ) -> None:
        if max_parallel < 1:
            raise ConfigError("max_parallel must be at least 1")
        self.pipeline = pipeline
        self.tiers = tiers
        self.runs_dir = runs_dir
        self.limits = limits or BudgetLimits()
        self.gate = gate or EffectGate(
            public_key=None,
            ledger=EffectLedger(runs_dir / "effects-ledger.jsonl"),
            executors=pipeline.effects,
        )
        self.sleep = sleep
        self.rng = rng or random.Random()
        self.max_parallel = max_parallel
        self.store_payloads = store_payloads
        self.pipeline_ref = pipeline_ref
        self.utc_clock = utc_clock
        for step in pipeline.steps:
            if step.start_tier is not None:
                tiers.get(step.start_tier)  # unknown tier -> ConfigError now, not mid-run

    # ------------------------------------------------------------------ run

    def run(
        self,
        raw_input: Any,
        *,
        run_id: str | None = None,
        overrides: Mapping[str, Override] | None = None,
        parent_run: str | None = None,
    ) -> RunResult:
        run_id = run_id or new_run_id()
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        os.chmod(run_dir, 0o700)
        blobs = BlobStore(run_dir / "blobs", enabled=self.store_payloads)
        audit = AuditLog(run_dir / "audit.jsonl", run_id, blobs, clock=self.utc_clock)
        budget = Budget(self.limits)
        state = _RunState(run_id, run_dir, audit, budget, dict(overrides or {}))
        try:
            return self._run(state, raw_input, parent_run)
        finally:
            audit.close()

    def _run(self, state: _RunState, raw_input: Any, parent_run: str | None) -> RunResult:
        audit = state.audit
        try:
            run_input = self.pipeline.input_model.model_validate(to_jsonable(raw_input))
        except ValidationError as exc:
            input_error: str | None = (
                f"run input failed {self.pipeline.input_model.__name__}: {exc}"
            )
            run_input = None
        else:
            input_error = None
        audit.emit(
            "run_start",
            pipeline=self.pipeline.describe(),
            pipeline_ref=self.pipeline_ref,
            parent_run=parent_run,
            input_sha256=audit.ref(to_jsonable(raw_input)),
            limits=self.limits.describe(),
            tiers=[t.describe() for t in self.tiers.tiers],
            notes=self.tiers.notes,
            simulated=self.tiers.simulated,
            payloads_stored=self.store_payloads,
            overrides=sorted(state.overrides),
        )
        results: dict[str, StepResult] = {}
        if run_input is None:
            for step in self.pipeline.steps:
                results[step.id] = StepResult(step.id, "not_run", error="invalid run input")
            return self._finish(state, results, "failed", input_error, raw_input)

        stop = threading.Event()
        for layer in self.pipeline.levels():
            runnable: list[Step] = []
            for step in layer:
                if stop.is_set():
                    results[step.id] = StepResult(step.id, "not_run", error="run stopped")
                    continue
                blocked = [d for d in step.after if results[d].status != "succeeded"]
                if blocked:
                    results[step.id] = StepResult(
                        step.id, "skipped", error=f"blocked by {', '.join(blocked)}"
                    )
                    audit.emit("step_skipped", step=step.id, blocked_by=blocked)
                    continue
                runnable.append(step)
            if not runnable:
                continue
            outputs = {sid: r.output for sid, r in results.items() if r.output is not None}

            def task(step: Step, outputs: dict[str, BaseModel] = outputs) -> StepResult:
                result = self._run_step(state, step, run_input, outputs)
                if result.status == "budget_exceeded":
                    stop.set()
                return result

            if self.max_parallel == 1 or len(runnable) == 1:
                for step in runnable:
                    results[step.id] = (
                        task(step)
                        if not stop.is_set()
                        else StepResult(step.id, "not_run", error="run stopped")
                    )
            else:
                with ThreadPoolExecutor(max_workers=self.max_parallel) as pool:
                    futures = {
                        step.id: pool.submit(contextvars.copy_context().run, task, step)
                        for step in runnable
                    }
                    for sid, future in futures.items():
                        results[sid] = future.result()
        statuses = {r.status for r in results.values()}
        status: RunStatus
        if "budget_exceeded" in statuses:
            status = "budget_exceeded"
        elif "failed" in statuses:
            status = "failed"
        elif "needs_review" in statuses:
            status = "needs_review"
        elif "awaiting_approval" in statuses:
            status = "awaiting_approval"
        else:
            status = "succeeded"
        return self._finish(state, results, status, None, raw_input)

    def _finish(
        self,
        state: _RunState,
        results: dict[str, StepResult],
        status: RunStatus,
        error: str | None,
        raw_input: Any,
    ) -> RunResult:
        budget = state.budget.snapshot()
        record = state.audit.emit(
            "run_end",
            status=status,
            error=error,
            budget=budget,
            steps={sid: r.status for sid, r in results.items()},
        )
        summary = {
            "run_id": state.run_id,
            "status": status,
            "error": error,
            "pipeline": self.pipeline.describe(),
            "pipeline_ref": self.pipeline_ref,
            "input_sha256": digest(to_jsonable(raw_input)),
            "steps": {sid: r.as_dict() for sid, r in results.items()},
            "budget": budget,
            "notes": self.tiers.notes,
            "simulated": self.tiers.simulated,
            "payloads_stored": self.store_payloads,
            "review_items": [item["id"] for item in state.review_items],
            "head_hash": record["hash"],
        }
        _write_json(state.run_dir / "summary.json", summary)
        return RunResult(
            state.run_id,
            state.run_dir,
            status,
            results,
            budget,
            list(self.tiers.notes),
            state.review_items,
            record["hash"],
        )

    # ----------------------------------------------------------------- steps

    def _run_step(
        self,
        state: _RunState,
        step: Step,
        run_input: BaseModel,
        outputs: dict[str, BaseModel],
    ) -> StepResult:
        audit = state.audit
        spec = step.agent
        result = StepResult(step.id, "failed")

        if step.id in state.overrides:
            data, source = state.overrides[step.id]
            try:
                output = spec.output_model.model_validate(to_jsonable(data))
            except ValidationError as exc:
                result.error = f"override for {step.id} failed {spec.output_model.__name__}: {exc}"
                audit.emit("step_end", step=step.id, status="failed", error=result.error)
                return result
            result.status, result.output, result.source = "succeeded", output, source
            result.output_sha256 = audit.ref(output)
            result.stable_sha256 = stable_digest(output, step.volatile_fields)
            audit.emit(
                "step_override", step=step.id, source=source, output_sha256=result.output_sha256
            )
            audit.emit(
                "step_end",
                step=step.id,
                status="succeeded",
                source=source,
                output_sha256=result.output_sha256,
            )
            return result

        try:
            agent_input = self._handoff(step, run_input, outputs)
        except SchemaError as exc:
            result.error = str(exc)
            audit.emit("handoff_invalid", step=step.id, error=result.error)
            audit.emit("step_end", step=step.id, status="failed", error=result.error)
            return result
        input_sha = audit.ref(agent_input)
        audit.emit("step_start", step=step.id, input_sha256=input_sha, after=list(step.after))

        tier, skipped = self.tiers.first_enabled(step.start_tier)
        for rung in skipped:
            audit.emit("tier_skipped", step=step.id, tier=rung.name, reason=rung.disabled_reason)
        last_output: BaseModel | None = None
        last_reason = "no enabled tier"
        attempt_no = 0
        while tier is not None:
            result.tiers_tried.append(tier.name)
            for attempt in range(1, step.retry.max_attempts + 1):
                attempt_no += 1
                result.attempts = attempt_no
                audit.emit(
                    "attempt_start",
                    step=step.id,
                    attempt=attempt_no,
                    tier=tier.name,
                    model=tier.model,
                    provider=self.tiers.provider(tier).name,
                )
                started = time.monotonic()
                ctx: AgentContext | None = None
                try:
                    state.budget.check_time()
                    ctx = self._make_ctx(state, step, tier, attempt_no)
                    output = self._attempt(state, step, agent_input, ctx)
                except ApprovalRequired as exc:
                    self._count(result, ctx)
                    return self._queue_approval(state, step, result, exc, input_sha)
                except BudgetExceeded as exc:
                    self._count(result, ctx)
                    result.status, result.error = "budget_exceeded", str(exc)
                    audit.emit(
                        "attempt_error",
                        step=step.id,
                        attempt=attempt_no,
                        tier=tier.name,
                        error="BudgetExceeded",
                        message=str(exc),
                        retryable=False,
                    )
                    audit.emit("step_end", **self._end_fields(result))
                    return result
                except _FATAL as exc:
                    self._count(result, ctx)
                    result.status, result.error = "failed", f"{type(exc).__name__}: {exc}"
                    audit.emit(
                        "attempt_error",
                        step=step.id,
                        attempt=attempt_no,
                        tier=tier.name,
                        error=type(exc).__name__,
                        message=str(exc)[:500],
                        retryable=False,
                    )
                    audit.emit("step_end", **self._end_fields(result))
                    return result
                except Exception as exc:
                    self._count(result, ctx)
                    retry_after = exc.retry_after if isinstance(exc, TransientError) else None
                    last_reason = f"{type(exc).__name__}: {str(exc)[:300]}"
                    more = attempt < step.retry.max_attempts
                    delay = step.retry.delay(attempt, self.rng, retry_after) if more else 0.0
                    audit.emit(
                        "attempt_error",
                        step=step.id,
                        attempt=attempt_no,
                        tier=tier.name,
                        error=type(exc).__name__,
                        message=str(exc)[:500],
                        retryable=more,
                        backoff_s=round(delay, 3),
                        latency_ms=round((time.monotonic() - started) * 1000, 1),
                    )
                    if more:
                        self.sleep(delay)
                    continue
                self._count(result, ctx)
                confidence = _confidence(output, step)
                out_sha = audit.ref(output)
                audit.emit(
                    "attempt_ok",
                    step=step.id,
                    attempt=attempt_no,
                    tier=tier.name,
                    output_sha256=out_sha,
                    confidence=confidence,
                    latency_ms=round((time.monotonic() - started) * 1000, 1),
                    llm_calls=ctx.stats.calls if ctx else 0,
                )
                if (
                    step.min_confidence is not None
                    and confidence is not None
                    and confidence < step.min_confidence
                ):
                    last_output = output
                    last_reason = (
                        f"confidence {confidence:.2f} below "
                        f"{step.min_confidence:.2f} on {tier.name}"
                    )
                    audit.emit(
                        "low_confidence",
                        step=step.id,
                        tier=tier.name,
                        confidence=confidence,
                        threshold=step.min_confidence,
                    )
                    break  # same tier at temperature 0 would say the same; escalate
                result.status, result.output = "succeeded", output
                result.output_sha256, result.confidence = out_sha, confidence
                result.stable_sha256 = stable_digest(output, step.volatile_fields)
                audit.emit("step_end", **self._end_fields(result))
                return result
            if not step.escalate:
                break
            nxt, skipped = self.tiers.next_enabled(tier.name)
            for rung in skipped:
                audit.emit(
                    "tier_skipped",
                    step=step.id,
                    tier=rung.name,
                    reason=rung.disabled_reason or "disabled",
                )
            if nxt is not None:
                audit.emit(
                    "escalate",
                    step=step.id,
                    from_tier=tier.name,
                    to_tier=nxt.name,
                    reason=last_reason,
                )
            tier = nxt

        result.error = last_reason
        if last_output is not None:
            result.output_sha256 = audit.ref(last_output)
            result.confidence = _confidence(last_output, step)
        if step.on_exhausted == "fail":
            result.status = "failed"
            audit.emit("step_end", **self._end_fields(result))
            return result
        item = {
            "id": f"rv-{state.run_id}-{step.id}",
            "kind": "review",
            "run_id": state.run_id,
            "step": step.id,
            "reason": last_reason,
            "tiers_tried": result.tiers_tried,
            "input_sha256": input_sha,
            "candidate_output": to_jsonable(last_output) if last_output is not None else None,
            "candidate_output_sha256": result.output_sha256,
            "output_model": step.agent.describe()["output_model"],
            "created": self.utc_clock(),
        }
        state.add_review(item)
        result.status, result.review_item = "needs_review", item["id"]
        audit.emit("review_queued", step=step.id, item=item["id"], reason=last_reason)
        audit.emit("step_end", **self._end_fields(result))
        return result

    def _handoff(
        self, step: Step, run_input: BaseModel, outputs: Mapping[str, BaseModel]
    ) -> BaseModel:
        model = step.agent.input_model
        try:
            if step.bind is not None:
                visible = {d: outputs[d] for d in step.after}
                raw = step.bind(Handoff(run_input, visible))
            elif step.after:
                raw = outputs[step.after[0]]
            else:
                raw = run_input
            if isinstance(raw, model):
                raw = raw.model_dump(mode="python")
            return model.model_validate(to_jsonable(raw) if isinstance(raw, BaseModel) else raw)
        except ValidationError as exc:
            raise SchemaError(f"handoff into {step.id} failed {model.__name__}: {exc}") from exc
        except Exception as exc:
            raise SchemaError(f"bind for {step.id} raised {type(exc).__name__}: {exc}") from exc

    def _make_ctx(self, state: _RunState, step: Step, tier: Tier, attempt: int) -> AgentContext:
        remaining = state.budget.remaining_seconds()
        timeout = step.timeout_s if remaining is None else max(0.0, min(step.timeout_s, remaining))
        return AgentContext(
            run_id=state.run_id,
            step_id=step.id,
            attempt=attempt,
            tier=tier,
            provider=self.tiers.provider(tier),
            budget=state.budget,
            audit=state.audit,
            gate=self.gate,
            deadline=time.monotonic() + timeout,
        )

    def _attempt(
        self, state: _RunState, step: Step, agent_input: BaseModel, ctx: AgentContext
    ) -> BaseModel:
        box: dict[str, Any] = {}
        spec = step.agent

        def target() -> None:
            bind_context(ctx)
            try:
                with agent_scope(
                    step.id,
                    allow_network=step.allow_network,
                    allow_subprocess=step.allow_subprocess,
                ) as scope:
                    box["scope"] = scope
                    box["value"] = (
                        spec.fn(agent_input, ctx) if spec.takes_context else spec.fn(agent_input)
                    )
            except BaseException as exc:
                box["error"] = exc

        worker = threading.Thread(
            target=contextvars.copy_context().run,
            args=(target,),
            name=f"agent-{step.id}-{ctx.attempt}",
            daemon=True,
        )
        worker.start()
        worker.join(max(0.0, ctx.deadline - time.monotonic()))
        if worker.is_alive():
            ctx.cancel()  # any later ctx.llm / ctx.effect call from that thread raises
            remaining = state.budget.remaining_seconds()
            if remaining is not None and remaining <= 0:
                raise BudgetExceeded(f"wall-clock budget ran out during step {step.id}")
            raise StepTimeout(f"step {step.id} exceeded {step.timeout_s}s (thread abandoned)")
        ctx.cancel()
        scope = box.get("scope")
        if scope is not None and scope.violations:
            raise PermissionDenied(scope.violations[0])
        if "error" in box:
            error = box["error"]
            if isinstance(error, Exception):
                raise error
            raise OrchestraError(f"agent raised {type(error).__name__}")  # pragma: no cover
        return self._validate_output(step, box.get("value"))

    @staticmethod
    def _validate_output(step: Step, value: Any) -> BaseModel:
        model = step.agent.output_model
        if isinstance(value, BaseModel) and not isinstance(value, model):
            raise OutputValidationError(
                f"{step.id} returned {type(value).__name__}, expected {model.__name__}"
            )
        try:
            data = value.model_dump(mode="python") if isinstance(value, BaseModel) else value
            return model.model_validate(data)
        except ValidationError as exc:
            raise OutputValidationError(f"{step.id} output failed {model.__name__}: {exc}") from exc

    @staticmethod
    def _count(result: StepResult, ctx: AgentContext | None) -> None:
        if ctx is not None:
            result.cost_usd += ctx.stats.usd
            result.tokens += ctx.stats.tokens

    def _queue_approval(
        self,
        state: _RunState,
        step: Step,
        result: StepResult,
        exc: ApprovalRequired,
        input_sha: str,
    ) -> StepResult:
        ids: list[str] = []
        for request in exc.requests:
            item = {
                "id": f"ap-{state.run_id}-{step.id}-{request.request_id[:8]}",
                "kind": "approval",
                "run_id": state.run_id,
                "step": step.id,
                "reason": exc.reason,
                "action": request.action,
                "target": request.target,
                "payload": to_jsonable(request.payload),
                "payload_sha256": request.payload_sha256,
                "request_id": request.request_id,
                "input_sha256": input_sha,
                "created": self.utc_clock(),
            }
            state.add_review(item)
            ids.append(item["id"])
            state.audit.emit(
                "approval_required",
                step=step.id,
                item=item["id"],
                action=request.action,
                target=request.target,
                request_id=request.request_id,
                payload_sha256=state.audit.ref(request.payload),
                reason=exc.reason,
            )
        result.status, result.review_item = "awaiting_approval", ",".join(ids)
        result.error = str(exc)
        state.audit.emit("step_end", **self._end_fields(result))
        return result

    @staticmethod
    def _end_fields(result: StepResult) -> dict[str, Any]:
        return {"step": result.step_id, **result.as_dict()}

    # --------------------------------------------------------------- resume

    def resume(self, run_dir: Path, *, run_id: str | None = None) -> RunResult:
        """Re-run a stopped run: finished steps and human resolutions are reused,
        everything else (including steps awaiting approval) runs again."""
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        if not summary.get("payloads_stored"):
            raise ConfigError("cannot resume a run recorded without payloads")
        blobs = BlobStore(run_dir / "blobs", enabled=False)
        raw_input = blobs.get(summary["input_sha256"])
        overrides: dict[str, Override] = {}
        for sid, info in summary["steps"].items():
            if info["status"] == "succeeded" and info.get("output_sha256"):
                overrides[sid] = (blobs.get(info["output_sha256"]), f"resumed:{summary['run_id']}")
        for sid, resolution in load_resolutions(run_dir).items():
            overrides[sid] = (resolution["output"], f"human:{resolution['reviewer']}")
        return self.run(raw_input, run_id=run_id, overrides=overrides, parent_run=summary["run_id"])


def stable_digest(output: BaseModel, volatile: tuple[str, ...]) -> str:
    """Digest of the output with volatile dotted paths removed."""
    data = to_jsonable(output)
    for path in volatile:
        node: Any = data
        parts = path.split(".")
        for part in parts[:-1]:
            node = node.get(part) if isinstance(node, dict) else None
        if isinstance(node, dict):
            node.pop(parts[-1], None)
    return digest(data)


def _confidence(output: BaseModel, step: Step) -> float | None:
    if step.confidence_of is not None:
        try:
            value: Any = step.confidence_of(output)
        except Exception:
            return 0.0  # fail closed: an unreadable confidence routes up, never through
    else:
        value = getattr(output, "confidence", None)
    return float(value) if isinstance(value, (int, float)) else None


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp, path)


def load_resolutions(run_dir: Path) -> dict[str, dict[str, Any]]:
    path = run_dir / "resolutions.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must be a JSON object keyed by step id")
    return data


class _RunState:
    def __init__(
        self,
        run_id: str,
        run_dir: Path,
        audit: AuditLog,
        budget: Budget,
        overrides: dict[str, Override],
    ) -> None:
        self.run_id = run_id
        self.run_dir = run_dir
        self.audit = audit
        self.budget = budget
        self.overrides = overrides
        self.review_items: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def add_review(self, item: dict[str, Any]) -> None:
        with self._lock:
            self.review_items.append(item)
            path = self.run_dir / "review.jsonl"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(item, sort_keys=True, ensure_ascii=False) + "\n")
