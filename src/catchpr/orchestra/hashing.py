"""Canonical JSON and SHA-256 digests used by the audit log and handoffs."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel


def to_jsonable(value: Any) -> Any:
    """Convert pydantic models (and containers of them) into plain JSON data."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    return value


def canonical(value: Any) -> bytes:
    """Deterministic encoding: sorted keys, no whitespace, NaN rejected."""
    return json.dumps(
        to_jsonable(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(value: Any) -> str:
    return sha256_hex(canonical(value))
