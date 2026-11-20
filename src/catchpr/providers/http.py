"""OpenAI-compatible chat completions with throttle and 429 retry."""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from collections.abc import Mapping

from catchpr.errors import ProviderError
from catchpr.jsonutil import as_dict, as_list, as_str, loads
from catchpr.models import Usage
from catchpr.providers.throttle import Throttle


class OpenAICompatibleProvider:
    def __init__(
        self,
        *,
        name: str,
        model: str,
        base_url: str,
        api_key: str,
        min_interval: float = 0.0,
        max_retries: int = 5,
        timeout: float = 60.0,
    ) -> None:
        if not api_key:
            raise ProviderError(f"{name} requires an API key")
        if not base_url:
            raise ProviderError(f"{name} requires a base URL")
        self._name = name
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._max_retries = max_retries
        self._timeout = timeout
        self._throttle = Throttle(min_interval)
        self._usage = Usage()

    @property
    def name(self) -> str:
        return self._name

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
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = json.dumps(
            {"model": self._model, "temperature": temperature, "messages": messages}
        ).encode()
        url = f"{self._base_url}/chat/completions"
        attempt = 0
        while True:
            self._throttle.wait()
            self._usage.logical_calls += 1
            self._usage.remote_calls += 1
            prompt_chars = len(prompt) + len(system or "")
            self._usage.prompt_chars += prompt_chars
            self._usage.remote_prompt_chars += prompt_chars
            status, headers, raw = self._post(url, payload)
            if status == 429 and attempt < self._max_retries:
                self._usage.retries += 1
                delay = retry_delay(headers, attempt)
                time.sleep(delay)
                attempt += 1
                continue
            if status >= 400:
                raise ProviderError(f"{self._name} HTTP {status}: {raw[:500]}")
            content = extract_message(raw)
            self._usage.completion_chars += len(content)
            return content

    def _post(self, url: str, payload: bytes) -> tuple[int, Mapping[str, str], str]:
        request = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
                return int(response.status), dict(response.headers.items()), body
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return int(exc.code), dict(exc.headers.items()), body
        except urllib.error.URLError as exc:
            raise ProviderError(f"{self._name} request failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise ProviderError(f"{self._name} request timed out") from exc


def retry_delay(headers: Mapping[str, str], attempt: int) -> float:
    retry_after = _header(headers, "retry-after")
    if retry_after is not None:
        try:
            parsed = float(retry_after)
        except ValueError:
            parsed = -1
        if parsed >= 0:
            return parsed + _jitter()
    backoff = min(20.0, 0.5 * float(2**attempt))
    return backoff + _jitter()


def _jitter() -> float:
    return random.uniform(0.0, 0.05)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


def extract_message(raw: str) -> str:
    try:
        parsed = as_dict(loads(raw))
    except json.JSONDecodeError as exc:
        raise ProviderError(f"provider returned non-JSON: {exc}") from exc
    if parsed is None:
        raise ProviderError("provider returned a non-object body")
    choices = as_list(parsed.get("choices"))
    if not choices:
        raise ProviderError("provider response had no choices")
    first = as_dict(choices[0])
    if first is None:
        raise ProviderError("provider choice was not an object")
    message = as_dict(first.get("message"))
    if message is None:
        raise ProviderError("provider choice had no message")
    content = as_str(message.get("content"))
    if content is None:
        raise ProviderError("provider message content was empty")
    return content
