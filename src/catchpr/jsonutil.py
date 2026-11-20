"""JSON helpers that stay strict under mypy."""

from __future__ import annotations

import json
import re

from catchpr.errors import ParseError


def loads(text: str) -> object:
    value: object = json.loads(text)
    return value


def as_dict(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    out: dict[str, object] = {}
    for key, item in value.items():
        if isinstance(key, str):
            out[key] = item
    return out


def as_list(value: object) -> list[object] | None:
    if isinstance(value, list):
        return list(value)
    return None


def as_str(value: object) -> str | None:
    if isinstance(value, str):
        return value
    return None


def require_dict(value: object, what: str) -> dict[str, object]:
    parsed = as_dict(value)
    if parsed is None:
        raise ParseError(f"{what} must be a JSON object")
    return parsed


def parse_model_json(text: str) -> dict[str, object]:
    """Parse a model response, stripping a single markdown fence if present."""
    stripped = text.strip()
    fence = re.match(r"^```(?:json)?\s*", stripped)
    if fence:
        stripped = stripped[fence.end() :]
        if stripped.endswith("```"):
            stripped = stripped[: stripped.rfind("```")]
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        raise ParseError("model response did not contain a JSON object")
    try:
        value = loads(stripped[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ParseError(f"model response was not valid JSON: {exc}") from exc
    return require_dict(value, "model response")
