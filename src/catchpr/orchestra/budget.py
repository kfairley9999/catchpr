"""Run budgets: tokens, dollars, and wall-clock seconds.

Every model call first *reserves* its worst case (estimated prompt tokens plus
``max_tokens`` of output at the tier's price). If the reservation would cross
a cap, the call is refused before any request is sent. After the call the
reservation is settled to the provider-reported usage. So the dollar cap holds
even with calls in flight in parallel, as long as the provider honours
``max_tokens`` and its prompt token count stays near the conservative
estimate; an overshoot past the estimate is charged in full and flagged.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from catchpr.orchestra.errors import BudgetExceeded


def money(value: Decimal) -> str:
    """Plain decimal string without exponent noise ('0', '0.00004424')."""
    return format(value.normalize(), "f")


@dataclass(frozen=True)
class BudgetLimits:
    max_usd: Decimal = Decimal("0.50")
    max_tokens: int = 200_000
    max_seconds: float | None = 900.0

    def describe(self) -> dict[str, Any]:
        return {
            "max_usd": money(self.max_usd),
            "max_tokens": self.max_tokens,
            "max_seconds": self.max_seconds,
        }


@dataclass(frozen=True)
class Reservation:
    tokens: int
    usd: Decimal


class Budget:
    def __init__(self, limits: BudgetLimits, clock: Callable[[], float] = time.monotonic) -> None:
        if limits.max_usd < 0 or limits.max_tokens < 0:
            raise ValueError("budget limits must be non-negative")
        self.limits = limits
        self._clock = clock
        self._start = clock()
        self._lock = threading.Lock()
        self.spent_usd = Decimal(0)
        self.spent_tokens = 0
        self.reserved_usd = Decimal(0)
        self.reserved_tokens = 0
        self.overshoots = 0

    def elapsed(self) -> float:
        return self._clock() - self._start

    def remaining_seconds(self) -> float | None:
        if self.limits.max_seconds is None:
            return None
        return self.limits.max_seconds - self.elapsed()

    def check_time(self) -> None:
        remaining = self.remaining_seconds()
        if remaining is not None and remaining <= 0:
            raise BudgetExceeded(f"wall-clock budget of {self.limits.max_seconds}s exhausted")

    def reserve(self, tokens: int, usd: Decimal) -> Reservation:
        self.check_time()
        with self._lock:
            if self.spent_tokens + self.reserved_tokens + tokens > self.limits.max_tokens:
                raise BudgetExceeded(
                    f"token budget: {self.spent_tokens} spent + {self.reserved_tokens} reserved "
                    f"+ {tokens} requested > {self.limits.max_tokens}"
                )
            if self.spent_usd + self.reserved_usd + usd > self.limits.max_usd:
                raise BudgetExceeded(
                    f"dollar budget: ${self.spent_usd} spent + ${self.reserved_usd} reserved "
                    f"+ ${usd} requested > ${self.limits.max_usd}"
                )
            self.reserved_tokens += tokens
            self.reserved_usd += usd
            return Reservation(tokens, usd)

    def settle(self, reservation: Reservation, tokens: int, usd: Decimal) -> bool:
        """Replace a reservation with actual usage. Returns True on overshoot."""
        with self._lock:
            self.reserved_tokens -= reservation.tokens
            self.reserved_usd -= reservation.usd
            self.spent_tokens += tokens
            self.spent_usd += usd
            overshoot = tokens > reservation.tokens or usd > reservation.usd
            if overshoot:
                self.overshoots += 1
            return overshoot

    def release(self, reservation: Reservation) -> None:
        with self._lock:
            self.reserved_tokens -= reservation.tokens
            self.reserved_usd -= reservation.usd

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "spent_usd": money(self.spent_usd),
                "spent_tokens": self.spent_tokens,
                "reserved_usd": money(self.reserved_usd),
                "reserved_tokens": self.reserved_tokens,
                "elapsed_s": round(self.elapsed(), 3),
                "overshoots": self.overshoots,
                **{f"limit_{k}": v for k, v in self.limits.describe().items()},
            }
