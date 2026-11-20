"""Disk cache for completions, keyed by provider, model, and prompt."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from catchpr.jsonutil import as_dict, as_str, loads
from catchpr.models import Usage
from catchpr.providers.base import Provider


def cache_key(provider: Provider, prompt: str, system: str | None, temperature: float) -> str:
    digest = hashlib.sha256()
    digest.update(provider.name.encode())
    digest.update(b"\0")
    digest.update(provider.model.encode())
    digest.update(b"\0")
    digest.update(str(temperature).encode())
    digest.update(b"\0")
    digest.update((system or "").encode())
    digest.update(b"\0")
    digest.update(prompt.encode())
    return digest.hexdigest()


class CachingProvider:
    def __init__(self, inner: Provider, cache_dir: Path) -> None:
        self._inner = inner
        self._cache_dir = cache_dir
        self._hits = 0
        self._logical = 0
        self._prompt_chars = 0

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def model(self) -> str:
        return self._inner.model

    def usage(self) -> Usage:
        inner = self._inner.usage()
        return Usage(
            logical_calls=self._logical,
            remote_calls=inner.remote_calls,
            cache_hits=self._hits,
            prompt_chars=self._prompt_chars,
            remote_prompt_chars=inner.remote_prompt_chars,
            completion_chars=inner.completion_chars,
            retries=inner.retries,
        )

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.0,
    ) -> str:
        self._logical += 1
        self._prompt_chars += len(prompt) + len(system or "")
        key = cache_key(self._inner, prompt, system, temperature)
        path = self._cache_dir / key[:2] / f"{key}.json"
        cached = _read(path)
        if cached is not None:
            self._hits += 1
            return cached
        text = self._inner.complete(prompt, system=system, temperature=temperature)
        _write(path, text)
        return text


def _read(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        parsed = as_dict(loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return None
    if parsed is None:
        return None
    return as_str(parsed.get("response"))


def _write(path: Path, response: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"response": response}), encoding="utf-8")
