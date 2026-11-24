"""Model tiers: an ordered escalation ladder of (provider, model, price).

The default ladder runs entirely on DeepInfra. A ``claude`` rung exists but is
disabled; it stays disabled until a tiers file sets ``enabled: true``, gives a
price, and ANTHROPIC_API_KEY is present. Disabled rungs are skipped during
escalation and the skip is written to the audit log.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from catchpr.orchestra.errors import ConfigError
from catchpr.orchestra.pricing import DEEPINFRA_PRICES, Price
from catchpr.orchestra.providers import (
    ANTHROPIC_BASE_URL,
    DEEPINFRA_BASE_URL,
    AnthropicProvider,
    LLMProvider,
    MockProvider,
    OpenAICompatibleProvider,
    Responder,
    deepinfra_key,
)

ProviderKind = Literal["deepinfra", "anthropic", "mock"]
Mode = Literal["auto", "deepinfra", "mock"]
CLAUDE_DISABLED_REASON = "disabled until an Anthropic API key is approved"


@dataclass(frozen=True)
class Tier:
    name: str
    provider: ProviderKind
    model: str
    price: Price | None
    enabled: bool = True
    disabled_reason: str = ""

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": self.provider,
            "model": self.model,
            "enabled": self.enabled,
            "disabled_reason": self.disabled_reason,
            "usd_per_m_input": None if self.price is None else str(self.price.usd_per_m_input),
            "usd_per_m_output": None if self.price is None else str(self.price.usd_per_m_output),
        }


def default_tiers() -> list[Tier]:
    def di(name: str, model: str) -> Tier:
        return Tier(name, "deepinfra", model, DEEPINFRA_PRICES[model])

    return [
        di("fast", "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo"),
        di("standard", "meta-llama/Llama-3.3-70B-Instruct-Turbo"),
        di("strong", "deepseek-ai/DeepSeek-V3.2"),
        Tier(
            "claude",
            "anthropic",
            "claude-sonnet-4-5",
            price=None,
            enabled=False,
            disabled_reason=CLAUDE_DISABLED_REASON,
        ),
    ]


def load_tiers(path: Path) -> list[Tier]:
    """Load a tiers file: {"tiers": [{name, provider, model, enabled, usd_per_m_input, ...}]}."""
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("tiers") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ConfigError(f'{path}: expected {{"tiers": [...]}} with at least one tier')
    return [tier_from_dict(row) for row in rows]


def tier_from_dict(row: Mapping[str, Any]) -> Tier:
    try:
        name = str(row["name"])
        provider = str(row["provider"])
        model = str(row["model"])
    except KeyError as exc:
        raise ConfigError(f"tier is missing {exc}") from exc
    if provider not in ("deepinfra", "anthropic", "mock"):
        raise ConfigError(f"tier {name}: unknown provider {provider!r}")
    price: Price | None = None
    if row.get("usd_per_m_input") is not None and row.get("usd_per_m_output") is not None:
        price = Price.of(row["usd_per_m_input"], row["usd_per_m_output"])
    elif provider == "deepinfra":
        price = DEEPINFRA_PRICES.get(model)
    return Tier(
        name=name,
        provider=provider,  # type: ignore[arg-type]
        model=model,
        price=price,
        enabled=bool(row.get("enabled", True)),
        disabled_reason=str(row.get("disabled_reason", "")),
    )


@dataclass
class TierSet:
    tiers: list[Tier]
    providers: dict[str, LLMProvider]
    notes: list[str] = field(default_factory=list)
    simulated: bool = False

    def __post_init__(self) -> None:
        names = [t.name for t in self.tiers]
        if len(set(names)) != len(names):
            raise ConfigError("tier names must be unique")
        for tier in self.tiers:
            if tier.enabled:
                if tier.name not in self.providers:
                    raise ConfigError(f"enabled tier {tier.name} has no provider")
                if tier.price is None:
                    raise ConfigError(
                        f"tier {tier.name} ({tier.model}) has no price; refusing to run "
                        "with an unknown cost"
                    )
        if not any(t.enabled for t in self.tiers):
            raise ConfigError("at least one tier must be enabled")

    def get(self, name: str) -> Tier:
        for tier in self.tiers:
            if tier.name == name:
                return tier
        raise ConfigError(f"unknown tier {name!r}")

    def first_enabled(self, start: str | None = None) -> tuple[Tier | None, list[Tier]]:
        """The start tier, or the next enabled one after it, plus skipped rungs."""
        index = 0 if start is None else self.tiers.index(self.get(start))
        skipped: list[Tier] = []
        for tier in self.tiers[index:]:
            if tier.enabled:
                return tier, skipped
            skipped.append(tier)
        return None, skipped

    def next_enabled(self, current: str) -> tuple[Tier | None, list[Tier]]:
        index = self.tiers.index(self.get(current))
        if index + 1 >= len(self.tiers):
            return None, []
        return self.first_enabled(self.tiers[index + 1].name)

    def provider(self, tier: Tier) -> LLMProvider:
        return self.providers[tier.name]


def build_tierset(
    tiers: Sequence[Tier] | None = None,
    *,
    mode: Mode = "auto",
    env: Mapping[str, str] | None = None,
    responder: Responder | None = None,
) -> TierSet:
    """Attach providers to tiers.

    ``auto`` uses DeepInfra when a key is in the environment and otherwise
    falls back to the mock provider, recording a note that costs are simulated.
    """
    env = {} if env is None else env
    ladder = list(default_tiers() if tiers is None else tiers)
    notes: list[str] = []
    key = deepinfra_key(env)
    use_mock = mode == "mock" or (mode == "auto" and key is None)
    if mode == "deepinfra" and key is None:
        raise ConfigError("provider 'deepinfra' requested but DEEPINFRA_API_KEY is not set")
    if use_mock:
        notes.append(
            "mock provider: no DeepInfra call was made; tokens and dollars are simulated"
            if mode == "mock"
            else "DEEPINFRA_API_KEY not found; fell back to the mock provider "
            "(tokens and dollars are simulated)"
        )
    mock = MockProvider(responder=responder) if responder else MockProvider()
    providers: dict[str, LLMProvider] = {}
    resolved: list[Tier] = []
    for tier in ladder:
        if not tier.enabled:
            resolved.append(tier)
            continue
        if tier.provider == "anthropic":
            anthropic_key = env.get("ANTHROPIC_API_KEY", "").strip()
            if not anthropic_key:
                raise ConfigError(
                    f"tier {tier.name} enables Anthropic but ANTHROPIC_API_KEY is not set"
                )
            if tier.price is None:
                raise ConfigError(f"tier {tier.name} enables Anthropic without a price")
            providers[tier.name] = (
                mock
                if use_mock
                else AnthropicProvider(
                    name="anthropic", base_url=ANTHROPIC_BASE_URL, api_key=anthropic_key
                )
            )
        elif tier.provider == "mock" or use_mock:
            providers[tier.name] = mock
        else:
            assert key is not None
            providers[tier.name] = OpenAICompatibleProvider(
                name="deepinfra",
                base_url=env.get("DEEPINFRA_BASE_URL", DEEPINFRA_BASE_URL),
                api_key=key,
            )
        resolved.append(tier)
    return TierSet(resolved, providers, notes=notes, simulated=use_mock)


def with_providers(tiers: Sequence[Tier], provider: LLMProvider, simulated: bool) -> TierSet:
    """Same ladder, one provider for every enabled tier (tests, replay)."""
    ladder = list(tiers)
    return TierSet(ladder, {t.name: provider for t in ladder if t.enabled}, simulated=simulated)
