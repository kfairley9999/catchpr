"""Structured, tamper-evident audit log and content-addressed blob store.

``audit.jsonl`` holds one JSON object per event. Each record carries ``seq``,
the previous record's hash (``prev``), and its own ``hash`` over the canonical
encoding of every other field, so editing, dropping, or reordering a line is
detected by :func:`verify_run`. Inputs, outputs, and model responses are
referenced by SHA-256; with payload storage on, the bytes live in
``blobs/<sha256>.json`` (0600), which is what makes replay possible. With
payload storage off, only hashes are kept and replay is unavailable.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from catchpr.orchestra.hashing import canonical, digest, sha256_hex, to_jsonable

GENESIS = "0" * 64


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class BlobStore:
    def __init__(self, directory: Path, enabled: bool = True) -> None:
        self.directory = directory
        self.enabled = enabled
        if enabled:
            directory.mkdir(parents=True, exist_ok=True)

    def put(self, value: Any) -> str:
        """Store a JSON payload; return the SHA-256 of its canonical form.

        The file keeps the original key order (agents may depend on mapping
        order, and replay must feed them identical input); integrity is checked
        by re-canonicalising on read.
        """
        plain = to_jsonable(value)
        sha = sha256_hex(canonical(plain))
        if self.enabled:
            path = self.directory / f"{sha}.json"
            if not path.exists():
                data = json.dumps(plain, ensure_ascii=False, allow_nan=False).encode("utf-8")
                tmp = self.directory / f".{sha}.{os.getpid()}.{threading.get_ident()}.tmp"
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                os.replace(tmp, path)
        return sha

    def get(self, sha: str) -> Any:
        path = self.directory / f"{sha}.json"
        try:
            value = json.loads(path.read_bytes())
        except json.JSONDecodeError as exc:
            raise ValueError(f"blob {sha} is corrupted") from exc
        if digest(value) != sha:
            raise ValueError(f"blob {sha} is corrupted")
        return value

    def has(self, sha: str) -> bool:
        return (self.directory / f"{sha}.json").exists()


class AuditLog:
    def __init__(
        self,
        path: Path,
        run_id: str,
        blobs: BlobStore,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self.path = path
        self.run_id = run_id
        self.blobs = blobs
        self._clock = clock
        self._lock = threading.Lock()
        self._seq = 0
        self._prev = GENESIS
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self._handle = os.fdopen(fd, "w", encoding="utf-8")

    def ref(self, value: Any) -> str:
        """Store a payload (if enabled) and return its SHA-256."""
        return self.blobs.put(to_jsonable(value))

    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            record: dict[str, Any] = {
                "seq": self._seq,
                "ts": self._clock(),
                "run_id": self.run_id,
                "event": event,
                **to_jsonable(fields),
                "prev": self._prev,
            }
            record["hash"] = digest(record)
            self._handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
            self._handle.flush()
            self._seq += 1
            self._prev = record["hash"]
            return record

    def close(self) -> None:
        with self._lock:
            if not self._handle.closed:
                self._handle.flush()
                os.fsync(self._handle.fileno())
                self._handle.close()


def read_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


@dataclass
class VerifyReport:
    ok: bool
    events: int
    blobs_checked: int
    errors: list[str] = field(default_factory=list)


_REF_KEYS = ("input_sha256", "output_sha256", "request_sha256", "response_sha256", "payload_sha256")


def verify_run(run_dir: Path) -> VerifyReport:
    """Check the hash chain, sequence numbers, and every referenced blob."""
    errors: list[str] = []
    path = run_dir / "audit.jsonl"
    if not path.exists():
        return VerifyReport(False, 0, 0, [f"{path} not found"])
    try:
        events = read_events(path)
    except json.JSONDecodeError as exc:
        return VerifyReport(False, 0, 0, [f"audit.jsonl is not valid JSONL: {exc}"])
    prev = GENESIS
    blobs = BlobStore(run_dir / "blobs", enabled=False)
    payloads_stored = bool(events) and bool(events[0].get("payloads_stored", False))
    checked = 0
    for index, record in enumerate(events):
        claimed = record.get("hash")
        body = {k: v for k, v in record.items() if k != "hash"}
        if record.get("seq") != index:
            errors.append(f"line {index + 1}: seq {record.get('seq')} != {index}")
        if record.get("prev") != prev:
            errors.append(f"line {index + 1}: chain broken (prev does not match)")
        if digest(body) != claimed:
            errors.append(f"line {index + 1}: record hash mismatch (edited)")
        prev = str(claimed)
        if payloads_stored:
            for key in _REF_KEYS:
                sha = record.get(key)
                if isinstance(sha, str):
                    checked += 1
                    if not blobs.has(sha):
                        errors.append(f"line {index + 1}: blob {sha[:12]} for {key} is missing")
                        continue
                    try:
                        blobs.get(sha)
                    except ValueError:
                        errors.append(f"line {index + 1}: blob {sha[:12]} is corrupted")
    if events and events[-1].get("event") != "run_end":
        errors.append("log does not end with run_end (truncated or interrupted run)")
    return VerifyReport(not errors, len(events), checked, errors)
