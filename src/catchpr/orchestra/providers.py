"""Model providers.

Every provider takes an :class:`LLMRequest` and returns a :class:`Completion`
with token counts. Network calls run inside ``guard.trusted_io()`` so the
network guard lets them through while it blocks direct I/O from agent code.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import threading
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from catchpr.orchestra.errors import ProviderFatal, ReplayMismatch, TransientError
from catchpr.orchestra.guard import trusted_io
from catchpr.orchestra.hashing import digest

DEEPINFRA_BASE_URL = "https://api.deepinfra.com/v1/openai"
ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"
DEEPINFRA_KEY_VARS = ("DEEPINFRA_API_KEY", "DEEPINFRA_TOKEN")
# Conservative per-image prompt-token estimate used only for budget
# reservations; billed usage from the provider replaces it after the call.
IMAGE_TOKEN_ESTIMATE = 3000


@dataclass(frozen=True)
class Message:
    role: str
    content: str


@dataclass(frozen=True)
class LLMRequest:
    model: str
    messages: tuple[Message, ...]
    max_tokens: int
    temperature: float = 0.0
    json_mode: bool = False
    images_png: tuple[bytes, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """Audit form. Images are recorded by SHA-256, never as bytes."""
        data: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in self.messages],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "json_mode": self.json_mode,
        }
        if self.images_png:
            data["images_sha256"] = [hashlib.sha256(i).hexdigest() for i in self.images_png]
        return data

    def digest(self) -> str:
        return digest(self.as_dict())

    def prompt_chars(self) -> int:
        return sum(len(m.content) for m in self.messages)

    def estimated_input_tokens(self) -> int:
        return estimate_tokens(self.prompt_chars()) + IMAGE_TOKEN_ESTIMATE * len(self.images_png)


@dataclass(frozen=True)
class Completion:
    text: str
    input_tokens: int
    output_tokens: int
    model: str
    usage_reported: bool = True
    simulated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "model": self.model,
            "usage_reported": self.usage_reported,
            "simulated": self.simulated,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Completion:
        return cls(
            text=str(data["text"]),
            input_tokens=int(data["input_tokens"]),
            output_tokens=int(data["output_tokens"]),
            model=str(data["model"]),
            usage_reported=bool(data.get("usage_reported", True)),
            simulated=bool(data.get("simulated", False)),
        )


class LLMProvider(Protocol):
    @property
    def name(self) -> str: ...

    def complete(self, request: LLMRequest, *, timeout: float) -> Completion: ...


def estimate_tokens(chars: int) -> int:
    """Conservative token estimate (~3 chars/token) used for budget reservations."""
    return max(1, math.ceil(chars / 3))


def _parse_retry_after(headers: Mapping[str, str]) -> float | None:
    for key, value in headers.items():
        if key.lower() == "retry-after":
            try:
                parsed = float(value)
            except ValueError:
                return None
            return parsed if parsed >= 0 else None
    return None


class _HttpJsonProvider:
    """Shared HTTP plumbing: status mapping and JSON decoding."""

    def __init__(self, *, name: str, base_url: str, api_key: str) -> None:
        if not api_key:
            raise ProviderFatal(f"{name}: an API key is required")
        self._name = name
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    @property
    def name(self) -> str:
        return self._name

    def _headers(self) -> dict[str, str]:  # pragma: no cover - overridden
        raise NotImplementedError

    def _post(self, path: str, payload: Mapping[str, Any], timeout: float) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={**self._headers(), "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with trusted_io(), urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:300]
            status = int(exc.code)
            headers = dict(exc.headers.items()) if exc.headers else {}
            if status == 429 or status >= 500:
                raise TransientError(
                    f"{self._name} HTTP {status}: {body}",
                    retry_after=_parse_retry_after(headers),
                ) from exc
            raise ProviderFatal(f"{self._name} HTTP {status}: {body}") from exc
        except (TimeoutError, urllib.error.URLError, ConnectionError, OSError) as exc:
            raise TransientError(f"{self._name} request failed: {exc}", maybe_billed=True) from exc
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TransientError(f"{self._name} returned non-JSON", maybe_billed=True) from exc
        if not isinstance(parsed, dict):
            raise TransientError(f"{self._name} returned a non-object body", maybe_billed=True)
        return parsed


class OpenAICompatibleProvider(_HttpJsonProvider):
    """DeepInfra (and any OpenAI-compatible endpoint) chat completions."""

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"}

    def complete(self, request: LLMRequest, *, timeout: float) -> Completion:
        messages: list[dict[str, Any]] = [
            {"role": m.role, "content": m.content} for m in request.messages
        ]
        if request.images_png:
            last = messages[-1]
            last["content"] = [
                {"type": "text", "text": last["content"]},
                *(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64," + base64.b64encode(img).decode("ascii")
                        },
                    }
                    for img in request.images_png
                ),
            ]
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if request.json_mode:
            payload["response_format"] = {"type": "json_object"}
        body = self._post("/chat/completions", payload, timeout)
        try:
            text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise TransientError(
                f"{self._name}: response had no message", maybe_billed=True
            ) from exc
        if not isinstance(text, str):
            raise TransientError(f"{self._name}: message content was not text", maybe_billed=True)
        usage = body.get("usage")
        if isinstance(usage, dict) and "prompt_tokens" in usage:
            return Completion(
                text=text,
                input_tokens=int(usage.get("prompt_tokens", 0)),
                output_tokens=int(usage.get("completion_tokens", 0)),
                model=str(body.get("model", request.model)),
            )
        return Completion(
            text=text,
            input_tokens=request.estimated_input_tokens(),
            output_tokens=request.max_tokens,
            model=request.model,
            usage_reported=False,
        )


class AnthropicProvider(_HttpJsonProvider):
    """Anthropic Messages API. Only constructed when a tier explicitly enables it.

    Not exercised against the live API in this repository (no key exists yet);
    it is covered by tests against a local stub server.
    """

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self._api_key, "anthropic-version": ANTHROPIC_VERSION}

    def complete(self, request: LLMRequest, *, timeout: float) -> Completion:
        system = "\n\n".join(m.content for m in request.messages if m.role == "system")
        payload: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "messages": [
                {"role": m.role, "content": m.content}
                for m in request.messages
                if m.role != "system"
            ],
        }
        if request.images_png and payload["messages"]:
            last = payload["messages"][-1]
            last["content"] = [
                *(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": base64.b64encode(img).decode("ascii"),
                        },
                    }
                    for img in request.images_png
                ),
                {"type": "text", "text": last["content"]},
            ]
        if system:
            payload["system"] = system
        body = self._post("/messages", payload, timeout)
        blocks = body.get("content")
        if not isinstance(blocks, list):
            raise TransientError(f"{self._name}: response had no content", maybe_billed=True)
        text = "".join(
            str(b.get("text", ""))
            for b in blocks
            if isinstance(b, dict) and b.get("type") == "text"
        )
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        assert isinstance(usage, dict)
        return Completion(
            text=text,
            input_tokens=int(usage.get("input_tokens", request.estimated_input_tokens())),
            output_tokens=int(usage.get("output_tokens", request.max_tokens)),
            model=str(body.get("model", request.model)),
            usage_reported="input_tokens" in usage,
        )


Responder = Callable[[LLMRequest], str]


def _default_responder(request: LLMRequest) -> str:
    return json.dumps({"mock": True, "model": request.model})


@dataclass
class MockProvider:
    """Deterministic offline provider. Token counts are estimates (chars/4).

    ``faults`` is a queue of exceptions to raise on successive calls (``None``
    means "succeed this call"), for failure injection.
    """

    responder: Responder = _default_responder
    faults: list[BaseException | None] = field(default_factory=list)
    provider_name: str = "mock"
    calls: list[LLMRequest] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def name(self) -> str:
        return self.provider_name

    def complete(self, request: LLMRequest, *, timeout: float) -> Completion:
        with self._lock:
            self.calls.append(request)
            fault = self.faults.pop(0) if self.faults else None
        if fault is not None:
            raise fault
        text = self.responder(request)
        return Completion(
            text=text,
            input_tokens=max(1, math.ceil(request.prompt_chars() / 4))
            + IMAGE_TOKEN_ESTIMATE * len(request.images_png),
            output_tokens=min(request.max_tokens, max(1, math.ceil(len(text) / 4))),
            model=request.model,
            usage_reported=False,
            simulated=True,
        )


class ReplayProvider:
    """Serves recorded completions keyed by request digest. No network."""

    def __init__(self, recorded: Iterable[tuple[str, Completion]]) -> None:
        self._by_digest: dict[str, deque[Completion]] = {}
        self._lock = threading.Lock()
        for request_digest, completion in recorded:
            self._by_digest.setdefault(request_digest, deque()).append(completion)

    @property
    def name(self) -> str:
        return "replay"

    def complete(self, request: LLMRequest, *, timeout: float) -> Completion:
        with self._lock:
            queue = self._by_digest.get(request.digest())
            if not queue:
                raise ReplayMismatch(
                    f"replay: no recorded completion for request {request.digest()[:16]} "
                    f"(model {request.model})"
                )
            return queue.popleft()


def deepinfra_key(env: Mapping[str, str]) -> str | None:
    for var in DEEPINFRA_KEY_VARS:
        value = env.get(var, "").strip()
        if value:
            return value
    return None
