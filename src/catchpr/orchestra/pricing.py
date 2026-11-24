"""Per-model token prices, held as Decimal so budget math never drifts.

Default DeepInfra prices come from the DeepInfra model catalog snapshot saved
in this workspace on 2026-11-22 (provefirst-deepinfra-pitch/src/models.json).
Prices change; override them in a tiers file before relying on dollar caps.
A model with no known price is refused (fail closed), never treated as free.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

MILLION = Decimal(1_000_000)
PRICE_SOURCE = "DeepInfra catalog snapshot 2026-11-22 (USD per 1M tokens)"


@dataclass(frozen=True)
class Price:
    usd_per_m_input: Decimal
    usd_per_m_output: Decimal

    def cost(self, input_tokens: int, output_tokens: int) -> Decimal:
        return (
            Decimal(input_tokens) * self.usd_per_m_input
            + Decimal(output_tokens) * self.usd_per_m_output
        ) / MILLION

    @classmethod
    def of(cls, usd_in: str | float, usd_out: str | float) -> Price:
        return cls(Decimal(str(usd_in)), Decimal(str(usd_out)))


DEEPINFRA_PRICES: dict[str, Price] = {
    "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo": Price.of("0.02", "0.04"),
    "meta-llama/Llama-3.3-70B-Instruct-Turbo": Price.of("0.10", "0.32"),
    "Qwen/Qwen3-32B": Price.of("0.08", "0.28"),
    "Qwen/Qwen3-235B-A22B-Instruct-2507": Price.of("0.09", "0.55"),
    "deepseek-ai/DeepSeek-V3.2": Price.of("0.26", "0.38"),
    "openai/gpt-oss-120b": Price.of("0.037", "0.17"),
    "openai/gpt-oss-20b": Price.of("0.03", "0.14"),
    # Vision-language models (scanned certificates)
    "Qwen/Qwen3-VL-235B-A22B-Instruct": Price.of("0.20", "0.88"),
    "Qwen/Qwen3-VL-30B-A3B-Instruct": Price.of("0.15", "0.60"),
    "google/gemma-3-27b-it": Price.of("0.08", "0.16"),
}
