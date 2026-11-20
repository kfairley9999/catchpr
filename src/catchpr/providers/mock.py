"""Replay recorded completions. Unknown prompts fall back to an empty, silent answer."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from catchpr.errors import ProviderError
from catchpr.jsonutil import as_dict, as_list, as_str, loads
from catchpr.models import Usage
from catchpr.providers.base import Recording

_FALLBACKS: tuple[tuple[str, str], ...] = (
    ("phase: mutant-test", '{"test": ""}'),
    ("phase: intent", '{"intent": "", "risks": []}'),
    ("phase: dodgy", '{"tests": []}'),
    ("phase: judge", '{"verdict": "abstain", "rationale": "no recording"}'),
    ("phase: baseline", '{"verdict": "clean", "summary": "no recording"}'),
)


def prompt_hash(prompt: str, system: str | None, temperature: float) -> str:
    digest = hashlib.sha256()
    digest.update(str(temperature).encode())
    digest.update(b"\0")
    digest.update((system or "").encode())
    digest.update(b"\0")
    digest.update(prompt.encode())
    return digest.hexdigest()


class MockProvider:
    """Recorded responses keyed by prompt hash or by required substrings.

    If nothing matches, the provider returns a valid empty payload for the
    phase tag in the prompt. Empty means the pipeline stays silent.
    """

    def __init__(
        self,
        recordings: list[Recording] | None = None,
        *,
        model: str = "recorded",
    ) -> None:
        self._recordings = list(recordings or [])
        self._model = model
        self._usage = Usage()

    @property
    def name(self) -> str:
        return "mock"

    @property
    def model(self) -> str:
        return self._model

    def usage(self) -> Usage:
        return self._usage

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.0,
    ) -> str:
        self._usage.logical_calls += 1
        self._usage.prompt_chars += len(prompt) + len(system or "")
        digest = prompt_hash(prompt, system, temperature)
        for recording in self._recordings:
            if recording.prompt_hash and recording.prompt_hash == digest:
                self._usage.completion_chars += len(recording.response)
                return recording.response
        best: Recording | None = None
        best_score = -1
        for recording in self._recordings:
            if not recording.contains:
                continue
            if all(piece in prompt for piece in recording.contains):
                score = sum(len(piece) for piece in recording.contains)
                if score > best_score:
                    best = recording
                    best_score = score
        if best is not None:
            self._usage.completion_chars += len(best.response)
            return best.response
        for marker, payload in _FALLBACKS:
            if marker in prompt:
                self._usage.completion_chars += len(payload)
                return payload
        payload = '{"tests": []}'
        self._usage.completion_chars += len(payload)
        return payload


def _recording_from_object(value: object, origin: str) -> Recording:
    data = as_dict(value)
    if data is None:
        raise ProviderError(f"{origin} recording must be an object")
    contains_raw = data.get("contains", [])
    contains: list[str] = []
    if contains_raw is None:
        contains_raw = []
    items = as_list(contains_raw)
    if items is None:
        text = as_str(contains_raw)
        if text is None:
            raise ProviderError(f"{origin} contains must be a list of strings")
        items = [text]
    for item in items:
        piece = as_str(item)
        if piece is None:
            raise ProviderError(f"{origin} contains must be a list of strings")
        contains.append(piece)
    response = as_str(data.get("response"))
    if response is None:
        raise ProviderError(f"{origin} recording is missing response")
    return Recording(
        contains=tuple(contains),
        response=response,
        recording_id=as_str(data.get("id")) or "",
        prompt_hash=as_str(data.get("prompt_hash")) or "",
    )


def load_recordings(path: Path) -> list[Recording]:
    files = sorted(path.glob("*.json")) if path.is_dir() else [path]
    recordings: list[Recording] = []
    for file in files:
        try:
            parsed = loads(file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProviderError(f"could not read recordings {file}: {exc}") from exc
        rows = as_list(parsed)
        if rows is None:
            rows = [parsed]
        for row in rows:
            recordings.append(_recording_from_object(row, str(file)))
    return recordings
