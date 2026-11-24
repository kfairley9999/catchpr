from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from catchpr.orchestra.budget import Budget, BudgetLimits, money
from catchpr.orchestra.errors import (
    BudgetExceeded,
    ConfigError,
    ProviderFatal,
    ReplayMismatch,
    TransientError,
)
from catchpr.orchestra.pricing import DEEPINFRA_PRICES, Price
from catchpr.orchestra.providers import (
    AnthropicProvider,
    Completion,
    LLMRequest,
    Message,
    MockProvider,
    OpenAICompatibleProvider,
    ReplayProvider,
    deepinfra_key,
)
from catchpr.orchestra.tiers import (
    Tier,
    TierSet,
    build_tierset,
    default_tiers,
    load_tiers,
    tier_from_dict,
)

REQ = LLMRequest(
    "m", (Message("system", "be brief"), Message("user", "hello")), max_tokens=50, json_mode=True
)


class Stub:
    """Local HTTP server whose next responses are scripted."""

    def __init__(self) -> None:
        self.responses: list[tuple[int, dict[str, str], str]] = []
        self.seen: list[dict[str, Any]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                stub.seen.append(
                    {
                        "path": self.path,
                        "headers": dict(self.headers),
                        "body": json.loads(self.rfile.read(length)),
                    }
                )
                status, headers, body = stub.responses.pop(0)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *args: Any) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, args=(0.02,), daemon=True).start()


@pytest.fixture()
def stub() -> Iterator[Stub]:
    s = Stub()
    yield s
    s.server.shutdown()


def openai_ok(text: str = "hi", usage: bool = True) -> str:
    body: dict[str, Any] = {"model": "m", "choices": [{"message": {"content": text}}]}
    if usage:
        body["usage"] = {"prompt_tokens": 11, "completion_tokens": 3}
    return json.dumps(body)


def test_openai_compatible_success_and_payload(stub: Stub) -> None:
    p = OpenAICompatibleProvider(name="deepinfra", base_url=stub.url + "/", api_key="sk-test")
    stub.responses = [(200, {}, openai_ok()), (200, {}, openai_ok(usage=False))]
    c = p.complete(REQ, timeout=5)
    assert (c.text, c.input_tokens, c.output_tokens, c.usage_reported) == ("hi", 11, 3, True)
    sent = stub.seen[0]
    assert sent["path"] == "/chat/completions"
    assert sent["headers"]["Authorization"] == "Bearer sk-test"
    assert sent["body"]["response_format"] == {"type": "json_object"}
    assert sent["body"]["messages"][0] == {"role": "system", "content": "be brief"}
    estimated = p.complete(REQ, timeout=5)
    assert not estimated.usage_reported and estimated.output_tokens == 50


@pytest.mark.parametrize(
    ("status", "headers", "body", "error", "retry_after", "billed"),
    [
        (429, {"Retry-After": "3"}, "{}", TransientError, 3.0, False),
        (429, {"Retry-After": "soon"}, "{}", TransientError, None, False),
        (503, {}, "down", TransientError, None, False),
        (401, {}, "bad key", ProviderFatal, None, False),
        (200, {}, "not json", TransientError, None, True),
        (200, {}, "[1]", TransientError, None, True),
        (200, {}, '{"choices": []}', TransientError, None, True),
        (200, {}, '{"choices": [{"message": {"content": 5}}]}', TransientError, None, True),
    ],
)
def test_openai_error_mapping(
    stub: Stub,
    status: int,
    headers: dict[str, str],
    body: str,
    error: type[Exception],
    retry_after: float | None,
    billed: bool,
) -> None:
    p = OpenAICompatibleProvider(name="deepinfra", base_url=stub.url, api_key="k")
    stub.responses = [(status, headers, body)]
    with pytest.raises(error) as info:
        p.complete(REQ, timeout=5)
    if isinstance(info.value, TransientError):
        assert info.value.retry_after == retry_after
        assert info.value.maybe_billed is billed


def test_connection_failure_is_transient_and_maybe_billed() -> None:
    p = OpenAICompatibleProvider(name="x", base_url="http://127.0.0.1:9", api_key="k")
    with pytest.raises(TransientError) as info:
        p.complete(REQ, timeout=2)
    assert info.value.maybe_billed
    with pytest.raises(ProviderFatal):
        OpenAICompatibleProvider(name="x", base_url="http://x", api_key="")


def test_anthropic_provider(stub: Stub) -> None:
    p = AnthropicProvider(name="anthropic", base_url=stub.url, api_key="ak")
    stub.responses = [
        (
            200,
            {},
            json.dumps(
                {
                    "model": "claude-x",
                    "content": [{"type": "text", "text": "yo"}],
                    "usage": {"input_tokens": 9, "output_tokens": 2},
                }
            ),
        ),
        (200, {}, json.dumps({"content": "bad"})),
    ]
    c = p.complete(REQ, timeout=5)
    assert (c.text, c.input_tokens, c.output_tokens, c.model) == ("yo", 9, 2, "claude-x")
    sent = stub.seen[0]
    assert (
        sent["path"] == "/messages"
        and {k.lower(): v for k, v in sent["headers"].items()}["x-api-key"] == "ak"
    )
    assert sent["body"]["system"] == "be brief"
    assert sent["body"]["messages"] == [{"role": "user", "content": "hello"}]
    with pytest.raises(TransientError):
        p.complete(REQ, timeout=5)


def test_mock_and_replay_providers() -> None:
    mock = MockProvider(faults=[TransientError("x")])
    with pytest.raises(TransientError):
        mock.complete(REQ, timeout=1)
    c = mock.complete(REQ, timeout=1)
    assert c.simulated and json.loads(c.text)["mock"] is True
    replay = ReplayProvider(
        [(REQ.digest(), c), (REQ.digest(), Completion.from_dict({**c.as_dict(), "text": "2"}))]
    )
    assert replay.complete(REQ, timeout=1).text == c.text
    assert replay.complete(REQ, timeout=1).text == "2"
    with pytest.raises(ReplayMismatch):
        replay.complete(REQ, timeout=1)


def test_pricing_and_budget_math() -> None:
    price = DEEPINFRA_PRICES["meta-llama/Llama-3.3-70B-Instruct-Turbo"]
    assert price.cost(1_000_000, 1_000_000) == Decimal("0.42")
    assert Price.of(1, 2).cost(500, 250) == Decimal("0.001")
    budget = Budget(BudgetLimits(max_usd=Decimal("0.01"), max_tokens=1000, max_seconds=None))
    r = budget.reserve(600, Decimal("0.006"))
    with pytest.raises(BudgetExceeded, match="token"):
        budget.reserve(500, Decimal("0"))
    with pytest.raises(BudgetExceeded, match="dollar"):
        budget.reserve(10, Decimal("0.005"))
    assert budget.settle(r, 700, Decimal("0.001")) is True  # overshoot flagged
    r2 = budget.reserve(100, Decimal("0.001"))
    budget.release(r2)
    snap = budget.snapshot()
    assert snap["spent_tokens"] == 700 and snap["overshoots"] == 1 and snap["reserved_usd"] == "0"
    assert money(Decimal("0E-8")) == "0" and money(Decimal("10")) == "10"
    with pytest.raises(ValueError):
        Budget(BudgetLimits(max_usd=Decimal("-1")))
    clock = iter([0.0, 5.0, 5.0])
    timed = Budget(BudgetLimits(max_seconds=1), clock=lambda: next(clock))
    with pytest.raises(BudgetExceeded, match="wall-clock"):
        timed.check_time()


def test_tierset_modes_and_claude_gate() -> None:
    auto = build_tierset(env={})
    assert auto.simulated and "fell back to the mock provider" in auto.notes[0]
    assert not auto.get("claude").enabled
    assert "is approved" in auto.get("claude").disabled_reason
    assert "claude" not in auto.providers
    assert build_tierset(mode="mock", env={"DEEPINFRA_API_KEY": "k"}).simulated
    with pytest.raises(ConfigError, match="DEEPINFRA_API_KEY"):
        build_tierset(mode="deepinfra", env={})
    live = build_tierset(env={"DEEPINFRA_TOKEN": "k"})
    assert not live.simulated and isinstance(live.providers["fast"], OpenAICompatibleProvider)
    assert deepinfra_key({"DEEPINFRA_API_KEY": "  "}) is None

    claude_on = [
        *default_tiers()[:3],
        tier_from_dict(
            {"name": "claude", "provider": "anthropic", "model": "claude-x", "enabled": True}
        ),
    ]
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        build_tierset(claude_on, env={"DEEPINFRA_API_KEY": "k"})
    with pytest.raises(ConfigError, match="without a price"):
        build_tierset(claude_on, env={"DEEPINFRA_API_KEY": "k", "ANTHROPIC_API_KEY": "a"})
    priced = [
        *claude_on[:3],
        tier_from_dict(
            {
                "name": "claude",
                "provider": "anthropic",
                "model": "c",
                "usd_per_m_input": 3,
                "usd_per_m_output": 15,
            }
        ),
    ]
    ts = build_tierset(priced, env={"DEEPINFRA_API_KEY": "k", "ANTHROPIC_API_KEY": "a"})
    assert isinstance(ts.providers["claude"], AnthropicProvider)
    assert ts.next_enabled("strong")[0] == ts.get("claude")
    assert ts.next_enabled("claude") == (None, [])


def test_tier_validation(tmp_path: Path) -> None:
    unpriced = Tier("x", "deepinfra", "unknown/model", None)
    with pytest.raises(ConfigError, match="no price"):
        TierSet([unpriced], {"x": MockProvider()})
    with pytest.raises(ConfigError, match="unique"):
        TierSet([default_tiers()[0]] * 2, {"fast": MockProvider()})
    with pytest.raises(ConfigError, match="no provider"):
        TierSet([default_tiers()[0]], {})
    with pytest.raises(ConfigError, match="at least one"):
        TierSet([default_tiers()[3]], {})
    with pytest.raises(ConfigError, match="unknown provider"):
        tier_from_dict({"name": "a", "provider": "openai", "model": "m"})
    with pytest.raises(ConfigError, match="missing"):
        tier_from_dict({"name": "a"})
    path = tmp_path / "tiers.json"
    path.write_text(
        json.dumps(
            {"tiers": [{"name": "only", "provider": "deepinfra", "model": "Qwen/Qwen3-32B"}]}
        )
    )
    (only,) = load_tiers(path)
    assert only.price == DEEPINFRA_PRICES["Qwen/Qwen3-32B"]
    path.write_text("{}")
    with pytest.raises(ConfigError):
        load_tiers(path)
    mock_tier = tier_from_dict(
        {"name": "m", "provider": "mock", "model": "m", "usd_per_m_input": 0, "usd_per_m_output": 0}
    )
    ts = build_tierset([mock_tier], env={"DEEPINFRA_API_KEY": "k"})
    assert isinstance(ts.providers["m"], MockProvider)


def test_image_payloads(stub: Stub) -> None:
    req = LLMRequest(
        "vl", (Message("system", "s"), Message("user", "read")), 20, images_png=(b"img",)
    )
    stub.responses = [
        (200, {}, openai_ok()),
        (200, {}, json.dumps({"content": [{"type": "text", "text": "ok"}]})),
    ]
    OpenAICompatibleProvider(name="d", base_url=stub.url, api_key="k").complete(req, timeout=5)
    parts = stub.seen[0]["body"]["messages"][-1]["content"]
    assert parts[0] == {"type": "text", "text": "read"}
    assert parts[1]["image_url"]["url"] == "data:image/png;base64,aW1n"
    c = AnthropicProvider(name="a", base_url=stub.url, api_key="k").complete(req, timeout=5)
    blocks = stub.seen[1]["body"]["messages"][-1]["content"]
    assert blocks[0]["source"]["data"] == "aW1n" and blocks[1]["text"] == "read"
    assert not c.usage_reported
