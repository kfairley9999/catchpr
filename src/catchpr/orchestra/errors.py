"""Exception hierarchy for the orchestrator.

The split matters: the engine decides whether to retry, escalate, queue for a
human, or stop the whole run purely from the exception class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from catchpr.orchestra.guard import EffectRequest


class OrchestraError(Exception):
    """Base class for every orchestrator error."""


class ConfigError(OrchestraError):
    """A pipeline, tier, or price is misconfigured. Never retried."""


class SchemaError(OrchestraError):
    """A typed handoff failed validation. Deterministic, so never retried."""


class TransientError(OrchestraError):
    """Retryable failure (network, 429, 5xx, timeout, malformed model output).

    ``retry_after`` is a server hint in seconds. ``maybe_billed`` is True when
    the request may have reached the provider, so the budget keeps the
    worst-case reservation instead of releasing it.
    """

    def __init__(
        self, message: str, *, retry_after: float | None = None, maybe_billed: bool = False
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.maybe_billed = maybe_billed


class StepTimeout(TransientError):
    """An agent attempt ran past its step timeout."""


class OutputValidationError(TransientError):
    """An agent (or the model behind it) produced output that failed its schema."""


class ProviderFatal(OrchestraError):
    """Non-retryable provider failure such as HTTP 400/401/403."""


class BudgetExceeded(OrchestraError):
    """A run-level token, dollar, or wall-clock cap would be exceeded. Stops the run."""


class PermissionDenied(OrchestraError):
    """A guard refused an action (network outside the provider, bad token)."""


class ApprovalRequired(OrchestraError):
    """A side effect needs a human approval token that was not supplied."""

    def __init__(
        self,
        request: EffectRequest,
        reason: str = "no matching approval token",
        *,
        others: tuple[EffectRequest, ...] = (),
    ) -> None:
        extra = f" (+{len(others)} more)" if others else ""
        super().__init__(
            f"approval required for {request.action} -> {request.target}{extra}: {reason}"
        )
        self.request = request
        self.reason = reason
        self.requests = (request, *others)


class Cancelled(OrchestraError):
    """The step this context belongs to already timed out or finished."""


class ReplayMismatch(OrchestraError):
    """Replay asked for a model call or effect that the recorded run never made."""
