"""Provider protocol and recording type."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from catchpr.models import Usage


@dataclass(frozen=True)
class Recording:
    contains: tuple[str, ...]
    response: str
    recording_id: str = ""
    prompt_hash: str = ""


class Provider(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def model(self) -> str: ...

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.0,
    ) -> str: ...

    def usage(self) -> Usage: ...
