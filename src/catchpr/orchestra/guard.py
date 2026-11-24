"""Permission guards.

Two independent layers:

1. **Approval tokens for side effects.** An agent may only cause an outbound
   effect (email, calendar invite, CRM write, ...) through ``ctx.effect``. The
   gate executes it only when a human-issued token matches the exact action,
   target, and payload hash, has a valid Ed25519 signature, is unexpired, and
   its nonce is unused. The orchestrator holds only the *public* key, so
   neither agent code nor model output running in this process can mint a
   token. Executed requests are recorded, so a retry or a second run with the
   same payload is deduplicated instead of sent twice.

2. **Network/process guard (defense in depth).** While agent code runs, a
   ``sys.addaudithook`` hook refuses socket connects, DNS lookups, and process
   spawns unless the call is inside the metered model client or a trusted
   effect executor. Python audit hooks are not a sandbox: native extensions,
   ``ctypes``, or threads the agent starts itself can bypass them. Treat this as
   a tripwire that turns accidental direct I/O into a loud failure.
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import json
import os
import secrets
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from catchpr.orchestra.errors import ApprovalRequired, ConfigError, PermissionDenied
from catchpr.orchestra.hashing import canonical, digest

TOKEN_PREFIX = "cpa1"

# --------------------------------------------------------------------------
# Network / process guard
# --------------------------------------------------------------------------


@dataclass
class Scope:
    """Guard state for one agent attempt. Violations are kept even if the agent
    catches the PermissionDenied, so the engine still fails the step."""

    step_id: str
    allow_network: bool
    allow_subprocess: bool = False
    violations: list[str] = field(default_factory=list)


_AGENT_SCOPE: ContextVar[Scope | None] = ContextVar("catchpr_agent_scope", default=None)
_TRUSTED_IO: ContextVar[bool] = ContextVar("catchpr_trusted_io", default=False)
_NETWORK_EVENTS = (
    "socket.connect",
    "socket.getaddrinfo",
    "socket.gethostbyname",
    "socket.sendto",
)
_PROCESS_EVENTS = (
    "subprocess.Popen",
    "os.system",
    "os.exec",
    "os.posix_spawn",
    "os.spawn",
    "os.fork",
    "os.forkpty",
    "pty.spawn",
)
_hook_lock = threading.Lock()
_hook_installed = False


def _audit_hook(event: str, _args: tuple[Any, ...]) -> None:
    scope = _AGENT_SCOPE.get()
    if scope is None or _TRUSTED_IO.get():
        return
    blocked = (not scope.allow_network and event.startswith(_NETWORK_EVENTS)) or (
        not scope.allow_subprocess and event.startswith(_PROCESS_EVENTS)
    )
    if blocked:
        message = f"agent step '{scope.step_id}' attempted '{event}' outside ctx.llm/ctx.effect"
        scope.violations.append(message)
        raise PermissionDenied(message)


def install_network_guard() -> None:
    """Install the audit hook once per process. It is inert outside agent scope."""
    global _hook_installed
    with _hook_lock:
        if not _hook_installed:
            sys.addaudithook(_audit_hook)
            _hook_installed = True


@contextlib.contextmanager
def agent_scope(
    step_id: str, *, allow_network: bool, allow_subprocess: bool = False
) -> Iterator[Scope]:
    install_network_guard()
    scope = Scope(step_id, allow_network, allow_subprocess)
    scope_token = _AGENT_SCOPE.set(scope)
    try:
        yield scope
    finally:
        _AGENT_SCOPE.reset(scope_token)


@contextlib.contextmanager
def trusted_io() -> Iterator[None]:
    """Mark the metered provider call or a trusted effect executor."""
    token = _TRUSTED_IO.set(True)
    try:
        yield
    finally:
        _TRUSTED_IO.reset(token)


# --------------------------------------------------------------------------
# Keys and tokens
# --------------------------------------------------------------------------


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def generate_keypair(directory: Path) -> tuple[Path, Path]:
    """Write approver_ed25519 (0600) and approver_ed25519.pub. Refuses to overwrite."""
    directory.mkdir(parents=True, exist_ok=True)
    priv_path = directory / "approver_ed25519"
    pub_path = directory / "approver_ed25519.pub"
    if priv_path.exists() or pub_path.exists():
        raise ConfigError(f"refusing to overwrite existing approver key in {directory}")
    key = Ed25519PrivateKey.generate()
    priv = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    fd = os.open(priv_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(priv)
    pub_path.write_bytes(pub)
    return priv_path, pub_path


def load_private_key(path: Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ConfigError(f"{path} is not an Ed25519 private key")
    return key


def load_public_key(path: Path) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(path.read_bytes())
    if not isinstance(key, Ed25519PublicKey):
        raise ConfigError(f"{path} is not an Ed25519 public key")
    return key


@dataclass(frozen=True)
class EffectRequest:
    action: str
    target: str
    payload: Any

    @property
    def payload_sha256(self) -> str:
        return digest(self.payload)

    @property
    def request_id(self) -> str:
        return digest(
            {"action": self.action, "target": self.target, "payload": self.payload_sha256}
        )[:24]


@dataclass(frozen=True)
class ApprovalClaims:
    action: str
    target: str
    payload_sha256: str
    expires_at: int
    nonce: str
    approver: str
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "v": 1,
            "action": self.action,
            "target": self.target,
            "payload_sha256": self.payload_sha256,
            "expires_at": self.expires_at,
            "nonce": self.nonce,
            "approver": self.approver,
            "note": self.note,
        }


def issue_token(
    key: Ed25519PrivateKey,
    request: EffectRequest,
    *,
    approver: str,
    ttl_seconds: int = 3600,
    note: str = "",
    now: float | None = None,
) -> str:
    if not approver.strip():
        raise ConfigError("an approval must name the human approver")
    if ttl_seconds <= 0:
        raise ConfigError("approval TTL must be positive")
    issued = time.time() if now is None else now
    claims = ApprovalClaims(
        action=request.action,
        target=request.target,
        payload_sha256=request.payload_sha256,
        expires_at=int(issued) + ttl_seconds,
        nonce=secrets.token_hex(16),
        approver=approver,
        note=note,
    )
    body = canonical(claims.as_dict())
    signature = key.sign(body)
    return f"{TOKEN_PREFIX}.{_b64e(body)}.{_b64e(signature)}"


def verify_token(key: Ed25519PublicKey, token: str, *, now: float | None = None) -> ApprovalClaims:
    parts = token.strip().split(".")
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        raise PermissionDenied("malformed approval token")
    try:
        body = _b64d(parts[1])
        signature = _b64d(parts[2])
    except (ValueError, TypeError) as exc:
        raise PermissionDenied("malformed approval token encoding") from exc
    try:
        key.verify(signature, body)
    except InvalidSignature as exc:
        raise PermissionDenied("approval token signature is invalid") from exc
    try:
        raw = json.loads(body)
        claims = ApprovalClaims(
            action=str(raw["action"]),
            target=str(raw["target"]),
            payload_sha256=str(raw["payload_sha256"]),
            expires_at=int(raw["expires_at"]),
            nonce=str(raw["nonce"]),
            approver=str(raw["approver"]),
            note=str(raw.get("note", "")),
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise PermissionDenied("approval token claims are malformed") from exc
    if raw.get("v") != 1:
        raise PermissionDenied("unsupported approval token version")
    current = time.time() if now is None else now
    if current >= claims.expires_at:
        raise PermissionDenied("approval token expired")
    return claims


def load_tokens(path: Path) -> list[str]:
    """A tokens file is JSON: a list of strings, or {"tokens": [...]}."""
    data = json.loads(path.read_text(encoding="utf-8"))
    tokens = data.get("tokens") if isinstance(data, dict) else data
    if not isinstance(tokens, list) or not all(isinstance(t, str) for t in tokens):
        raise ConfigError(f"{path}: expected a JSON list of approval tokens")
    return list(tokens)


# --------------------------------------------------------------------------
# Durable ledger: used nonces and executed effect requests
# --------------------------------------------------------------------------


class EffectLedger:
    """Append-only JSONL shared across runs and processes (flock-protected).

    A nonce is recorded *before* the executor runs, so a crash mid-send can
    never lead to a second send with the same token (at-most-once).
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        with self._lock:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def _append(self, record: Mapping[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(record), sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def executed(self, request_id: str) -> dict[str, Any] | None:
        for record in self._read():
            if record.get("kind") == "result" and record.get("request_id") == request_id:
                return record
        return None

    def claim(
        self, *, nonce: str, request_id: str, run_id: str, step_id: str, approver: str
    ) -> str:
        """Atomically consume a nonce for a request.

        Returns ``"ok"``, ``"nonce_used"`` (token already spent), or
        ``"request_claimed"`` (this exact effect was already claimed, possibly
        by another process, so it must not run again).
        """
        with self._locked():
            for record in self._read():
                if record.get("kind") != "nonce":
                    continue
                if record.get("request_id") == request_id:
                    return "request_claimed"
                if record.get("nonce") == nonce:
                    return "nonce_used"
            self._append(
                {
                    "kind": "nonce",
                    "nonce": nonce,
                    "request_id": request_id,
                    "run_id": run_id,
                    "step_id": step_id,
                    "approver": approver,
                    "ts": time.time(),
                }
            )
            return "ok"

    def record_result(self, request_id: str, result: Mapping[str, Any]) -> None:
        with self._locked():
            self._append({"kind": "result", "request_id": request_id, "result": dict(result)})


Executor = Callable[[EffectRequest], Mapping[str, Any]]


@dataclass
class EffectOutcome:
    request: EffectRequest
    result: dict[str, Any]
    approver: str
    deduplicated: bool = False


@dataclass
class EffectGate:
    """Decides whether a requested side effect may run.

    ``public_key=None`` means no token can ever verify, so every effect is
    queued for approval. ``executors`` maps action names to trusted callables;
    an action with no executor is refused even with a valid token.
    """

    public_key: Ed25519PublicKey | None
    ledger: EffectLedger
    executors: Mapping[str, Executor] = field(default_factory=dict)
    tokens: list[str] = field(default_factory=list)
    replay_results: Mapping[str, dict[str, Any]] | None = None
    clock: Callable[[], float] = time.time

    def perform(self, request: EffectRequest, *, run_id: str, step_id: str) -> EffectOutcome:
        if self.replay_results is not None:
            recorded = self.replay_results.get(request.request_id)
            if recorded is None:
                raise ApprovalRequired(
                    request, "replay: effect was not executed in the original run"
                )
            return EffectOutcome(request, dict(recorded), approver="replay", deduplicated=True)

        previous = self.ledger.executed(request.request_id)
        if previous is not None:
            return EffectOutcome(
                request, dict(previous.get("result", {})), approver="ledger", deduplicated=True
            )

        candidates = self._matching_claims(request)
        if not candidates:
            raise ApprovalRequired(request)
        executor = self.executors.get(request.action)
        if executor is None:
            raise PermissionDenied(
                f"no trusted executor is registered for action '{request.action}'"
            )
        for claims in candidates:
            status = self.ledger.claim(
                nonce=claims.nonce,
                request_id=request.request_id,
                run_id=run_id,
                step_id=step_id,
                approver=claims.approver,
            )
            if status == "nonce_used":
                continue
            if status == "request_claimed":
                previous = self.ledger.executed(request.request_id)
                if previous is not None:
                    return EffectOutcome(
                        request,
                        dict(previous.get("result", {})),
                        approver="ledger",
                        deduplicated=True,
                    )
                raise PermissionDenied(
                    f"effect {request.request_id} was claimed but has no recorded result "
                    "(in flight elsewhere or interrupted); refusing to send it again"
                )
            with trusted_io():
                result = dict(executor(request))
            self.ledger.record_result(request.request_id, result)
            return EffectOutcome(request, result, approver=claims.approver)
        raise ApprovalRequired(request, "every matching approval token was already used")

    def _matching_claims(self, request: EffectRequest) -> list[ApprovalClaims]:
        if self.public_key is None:
            return []
        matches: list[ApprovalClaims] = []
        for token in self.tokens:
            try:
                claims = verify_token(self.public_key, token, now=self.clock())
            except PermissionDenied:
                continue
            if (
                claims.action == request.action
                and claims.target == request.target
                and claims.payload_sha256 == request.payload_sha256
            ):
                matches.append(claims)
        return matches
