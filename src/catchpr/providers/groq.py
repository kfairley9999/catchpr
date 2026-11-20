"""Groq's OpenAI-compatible API. Reads GROQ_API_KEY. Never logs the key."""

from __future__ import annotations

import os

from catchpr.errors import ProviderError
from catchpr.providers.http import OpenAICompatibleProvider

DEFAULT_GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"


class GroqProvider(OpenAICompatibleProvider):
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = DEFAULT_GROQ_MODEL,
        min_interval: float = 0.25,
        max_retries: int = 5,
        timeout: float = 60.0,
    ) -> None:
        key = api_key if api_key is not None else os.environ.get("GROQ_API_KEY", "")
        if not key:
            raise ProviderError("GroqProvider requires GROQ_API_KEY or api_key=")
        super().__init__(
            name="groq",
            model=model,
            base_url=GROQ_BASE_URL,
            api_key=key,
            min_interval=min_interval,
            max_retries=max_retries,
            timeout=timeout,
        )
