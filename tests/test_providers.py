from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from catchpr.errors import ProviderError
from catchpr.providers.cache import CachingProvider, cache_key
from catchpr.providers.groq import GroqProvider
from catchpr.providers.http import OpenAICompatibleProvider, extract_message, retry_delay
from catchpr.providers.mock import MockProvider, load_recordings
from catchpr.providers.throttle import Throttle


class _Scripted(BaseHTTPRequestHandler):
    script: ClassVar[list[tuple[int, str]]] = []
    seen: ClassVar[list[bytes]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        _Scripted.seen.append(self.rfile.read(length))
        status, body = _Scripted.script.pop(0)
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        if status == 429:
            self.send_header("Retry-After", "0")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: object) -> None:
        return


def _server(script: list[tuple[int, str]]) -> tuple[ThreadingHTTPServer, str]:
    _Scripted.script = list(script)
    _Scripted.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Scripted)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}/v1"


def test_mock_matches_longest_contains_and_falls_back(tmp_path: Path) -> None:
    path = tmp_path / "recordings.json"
    path.write_text(
        json.dumps(
            [
                {"id": "short", "contains": ["phase: intent"], "response": "short"},
                {
                    "id": "long",
                    "contains": ["phase: intent", "def apply_discount"],
                    "response": "long",
                },
                {"prompt_hash": "deadbeef", "response": "hashed", "contains": []},
            ]
        ),
        encoding="utf-8",
    )
    provider = MockProvider(load_recordings(path))
    assert provider.complete("phase: intent\ndef apply_discount():\n    pass") == "long"
    assert provider.complete("phase: dodgy\n") == '{"tests": []}'
    assert (
        provider.complete("phase: baseline\n") == '{"verdict": "clean", "summary": "no recording"}'
    )
    assert provider.complete("no phase at all") == '{"tests": []}'
    assert provider.usage().logical_calls == 4
    assert provider.usage().remote_calls == 0
    folder = tmp_path / "dir"
    folder.mkdir()
    (folder / "a.json").write_text(
        json.dumps({"contains": "only", "response": "one"}),
        encoding="utf-8",
    )
    loaded = load_recordings(folder)
    assert loaded[0].response == "one"
    with pytest.raises(ProviderError):
        load_recordings(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    with pytest.raises(ProviderError):
        load_recordings(bad)
    shape = tmp_path / "shape.json"
    shape.write_text(json.dumps({"contains": [1], "response": "x"}), encoding="utf-8")
    with pytest.raises(ProviderError):
        load_recordings(shape)


def test_throttle_spaces_calls() -> None:
    slept: list[float] = []
    throttle = Throttle(0.2, sleep=slept.append)
    throttle.wait()
    throttle.wait()
    assert slept and slept[0] > 0
    with pytest.raises(ValueError):
        Throttle(-1)


def test_retry_and_extract_message() -> None:
    assert retry_delay({"Retry-After": "0"}, 0) >= 0
    assert retry_delay({"retry-after": "nope"}, 0) >= 0.5
    assert extract_message('{"choices":[{"message":{"content":"hello"}}]}') == "hello"
    with pytest.raises(ProviderError):
        extract_message("[]")
    with pytest.raises(ProviderError):
        extract_message('{"choices":[]}')
    with pytest.raises(ProviderError):
        extract_message("not-json")


def test_openai_compatible_retries_429_then_caches(tmp_path: Path) -> None:
    ok = json.dumps({"choices": [{"message": {"content": "pong"}}]})
    server, base = _server([(429, '{"error":"slow"}'), (200, ok), (200, ok)])
    try:
        provider = OpenAICompatibleProvider(
            name="openai",
            model="test-model",
            base_url=base,
            api_key="test-key",
            min_interval=0,
            max_retries=2,
            timeout=5,
        )
        cached = CachingProvider(provider, tmp_path / "cache")
        assert cached.complete("ping", system="sys") == "pong"
        assert cached.complete("ping", system="sys") == "pong"
        assert cached.usage().cache_hits == 1
        assert provider.usage().retries == 1
        assert provider.usage().remote_calls == 2
        assert len(_Scripted.seen) == 2
        # Corrupt cache is ignored.
        key = cache_key(provider, "other", None, 0.0)
        broken = tmp_path / "cache" / key[:2] / f"{key}.json"
        broken.parent.mkdir(parents=True, exist_ok=True)
        broken.write_text("{", encoding="utf-8")
        server.shutdown()
    finally:
        server.shutdown()


def test_openai_errors_and_groq_config(tmp_path: Path) -> None:
    server, base = _server([(400, '{"error":"no"}'), (200, "not-json")])
    provider = OpenAICompatibleProvider(
        name="openai",
        model="m",
        base_url=base,
        api_key="k",
        max_retries=0,
        timeout=5,
    )
    with pytest.raises(ProviderError):
        provider.complete("x")
    with pytest.raises(ProviderError):
        OpenAICompatibleProvider(name="openai", model="m", base_url="", api_key="k")
    with pytest.raises(ProviderError):
        OpenAICompatibleProvider(name="openai", model="m", base_url=base, api_key="")
    with pytest.raises(ProviderError):
        GroqProvider(api_key="")
    groq = GroqProvider(api_key="secret", min_interval=0, model="llama-test")
    assert groq.name == "groq"
    assert groq.model == "llama-test"
    assert "secret" not in groq.model
    server.shutdown()
