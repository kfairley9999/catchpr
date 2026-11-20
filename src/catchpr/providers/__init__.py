"""Model providers. MockProvider is the default and needs no API key."""

from __future__ import annotations

from catchpr.providers.base import Provider, Recording
from catchpr.providers.cache import CachingProvider
from catchpr.providers.groq import GroqProvider
from catchpr.providers.http import OpenAICompatibleProvider
from catchpr.providers.mock import MockProvider, load_recordings

__all__ = [
    "CachingProvider",
    "GroqProvider",
    "MockProvider",
    "OpenAICompatibleProvider",
    "Provider",
    "Recording",
    "load_recordings",
]
