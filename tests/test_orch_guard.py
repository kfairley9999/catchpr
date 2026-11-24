from __future__ import annotations

import contextlib
import json
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from tests.orch_support import Num, make_orch

from catchpr.orchestra.agents import AgentContext, adapt
from catchpr.orchestra.errors import ApprovalRequired, ConfigError, PermissionDenied
from catchpr.orchestra.guard import (
    EffectGate,
    EffectLedger,
    EffectRequest,
    _b64e,
    generate_keypair,
    issue_token,
    load_private_key,
    load_public_key,
    load_tokens,
    verify_token,
)
from catchpr.orchestra.hashing import canonical
from catchpr.orchestra.pipeline import Pipeline, RetryPolicy, Step

REQ = EffectRequest("email.send", "owner@agency-a.example.com", {"subject": "Hi", "body": "x"})


@pytest.fixture()
def key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


class Recorder:
    def __init__(self) -> None:
        self.calls: list[EffectRequest] = []

    def __call__(self, request: EffectRequest) -> dict[str, Any]:
        self.calls.append(request)
        return {"ok": True, "n": len(self.calls)}


def gate_for(
    tmp_path: Path, key: Ed25519PrivateKey | None, tokens: list[str], rec: Recorder
) -> EffectGate:
    return EffectGate(
        key.public_key() if key else None,
        EffectLedger(tmp_path / "ledger.jsonl"),
        {"email.send": rec},
        tokens,
    )


def test_token_roundtrip_and_rejections(key: Ed25519PrivateKey) -> None:
    token = issue_token(key, REQ, approver="Kevin", ttl_seconds=60, now=1000)
    claims = verify_token(key.public_key(), token, now=1001)
    assert claims.payload_sha256 == REQ.payload_sha256 and claims.approver == "Kevin"
    with pytest.raises(PermissionDenied, match="expired"):
        verify_token(key.public_key(), token, now=1060)
    with pytest.raises(PermissionDenied, match="signature"):
        verify_token(Ed25519PrivateKey.generate().public_key(), token, now=1001)
    prefix, body, sig = token.split(".")
    forged = json.loads(__import__("base64").urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    forged["target"] = "attacker@example.com"
    with pytest.raises(PermissionDenied, match="signature"):
        verify_token(key.public_key(), f"{prefix}.{_b64e(canonical(forged))}.{sig}", now=1001)
    for bad in ("nope", "cpa1.a.b.c", "cpa2.x.y", "cpa1.!!!.###"):
        with pytest.raises(PermissionDenied):
            verify_token(key.public_key(), bad, now=1001)
    v2 = canonical({**forged, "v": 2})
    with pytest.raises(PermissionDenied, match="version"):
        verify_token(key.public_key(), f"cpa1.{_b64e(v2)}.{_b64e(key.sign(v2))}", now=1001)
    junk = b'{"v":1}'
    with pytest.raises(PermissionDenied, match="malformed"):
        verify_token(key.public_key(), f"cpa1.{_b64e(junk)}.{_b64e(key.sign(junk))}", now=1001)
    with pytest.raises(ConfigError):
        issue_token(key, REQ, approver=" ")
    with pytest.raises(ConfigError):
        issue_token(key, REQ, approver="Kevin", ttl_seconds=0)


def test_gate_without_key_or_token_queues(tmp_path: Path, key: Ed25519PrivateKey) -> None:
    rec = Recorder()
    with pytest.raises(ApprovalRequired):
        gate_for(tmp_path, None, [], rec).perform(REQ, run_id="r", step_id="s")
    with pytest.raises(ApprovalRequired):
        gate_for(tmp_path, key, [], rec).perform(REQ, run_id="r", step_id="s")
    assert rec.calls == []


def test_token_is_bound_to_exact_payload_and_target(tmp_path: Path, key: Ed25519PrivateKey) -> None:
    rec = Recorder()
    token = issue_token(key, REQ, approver="Kevin")
    gate = gate_for(tmp_path, key, [token], rec)
    for other in (
        EffectRequest(REQ.action, REQ.target, {"subject": "Hi", "body": "changed"}),
        EffectRequest(REQ.action, "someone-else@example.com", REQ.payload),
        EffectRequest("crm.write", REQ.target, REQ.payload),
    ):
        with pytest.raises(ApprovalRequired):
            gate.perform(other, run_id="r", step_id="s")
    assert rec.calls == []


def test_valid_token_executes_once_then_deduplicates(
    tmp_path: Path, key: Ed25519PrivateKey
) -> None:
    rec = Recorder()
    token = issue_token(key, REQ, approver="Kevin")
    gate = gate_for(tmp_path, key, [token], rec)
    first = gate.perform(REQ, run_id="r1", step_id="s")
    assert first.result == {"ok": True, "n": 1} and first.approver == "Kevin"
    # Same gate, a fresh gate with the same ledger, even a second valid token:
    again = gate.perform(REQ, run_id="r2", step_id="s")
    fresh = gate_for(tmp_path, key, [issue_token(key, REQ, approver="Kevin")], rec)
    third = fresh.perform(REQ, run_id="r3", step_id="s")
    assert again.deduplicated and third.deduplicated
    assert len(rec.calls) == 1


def test_spent_nonce_and_inflight_claims(tmp_path: Path, key: Ed25519PrivateKey) -> None:
    ledger = EffectLedger(tmp_path / "l.jsonl")
    assert ledger.claim(nonce="n1", request_id="a", run_id="r", step_id="s", approver="k") == "ok"
    assert (
        ledger.claim(nonce="n1", request_id="b", run_id="r", step_id="s", approver="k")
        == "nonce_used"
    )
    assert (
        ledger.claim(nonce="n2", request_id="a", run_id="r", step_id="s", approver="k")
        == "request_claimed"
    )
    # A claimed request with no recorded result (crash mid-send) is never re-sent.
    rec = Recorder()
    token = issue_token(key, REQ, approver="Kevin")
    gate = EffectGate(key.public_key(), ledger, {"email.send": rec}, [token])
    ledger.claim(nonce="zz", request_id=REQ.request_id, run_id="r", step_id="s", approver="k")
    with pytest.raises(PermissionDenied, match="refusing to send it again"):
        gate.perform(REQ, run_id="r", step_id="s")
    assert rec.calls == []


def test_every_matching_token_spent(tmp_path: Path, key: Ed25519PrivateKey) -> None:
    token = issue_token(key, REQ, approver="Kevin")
    claims = verify_token(key.public_key(), token)
    ledger = EffectLedger(tmp_path / "l.jsonl")
    ledger.claim(nonce=claims.nonce, request_id="other", run_id="r", step_id="s", approver="k")
    gate = EffectGate(key.public_key(), ledger, {"email.send": Recorder()}, [token])
    with pytest.raises(ApprovalRequired, match="already used"):
        gate.perform(REQ, run_id="r", step_id="s")


def test_missing_executor_fails_closed_without_spending(
    tmp_path: Path, key: Ed25519PrivateKey
) -> None:
    token = issue_token(key, REQ, approver="Kevin")
    gate = EffectGate(key.public_key(), EffectLedger(tmp_path / "l.jsonl"), {}, [token])
    with pytest.raises(PermissionDenied, match="no trusted executor"):
        gate.perform(REQ, run_id="r", step_id="s")
    assert not (tmp_path / "l.jsonl").exists() or "nonce" not in (tmp_path / "l.jsonl").read_text()


def test_replay_gate_never_executes(tmp_path: Path) -> None:
    rec = Recorder()
    gate = EffectGate(
        None,
        EffectLedger(tmp_path / "l.jsonl"),
        {"email.send": rec},
        replay_results={REQ.request_id: {"ok": "recorded"}},
    )
    assert gate.perform(REQ, run_id="r", step_id="s").result == {"ok": "recorded"}
    with pytest.raises(ApprovalRequired, match="replay"):
        gate.perform(EffectRequest("email.send", "x@example.com", {}), run_id="r", step_id="s")
    assert rec.calls == []


def test_keypair_files(tmp_path: Path) -> None:
    priv, pub = generate_keypair(tmp_path / "k")
    assert stat.S_IMODE(priv.stat().st_mode) == 0o600
    key = load_private_key(priv)
    assert load_public_key(pub).public_bytes_raw() == key.public_key().public_bytes_raw()
    with pytest.raises(ConfigError, match="overwrite"):
        generate_keypair(tmp_path / "k")


def test_load_tokens_formats(tmp_path: Path) -> None:
    (tmp_path / "a.json").write_text('["t1"]')
    (tmp_path / "b.json").write_text('{"tokens": ["t2"]}')
    (tmp_path / "c.json").write_text('{"tokens": [1]}')
    assert load_tokens(tmp_path / "a.json") == ["t1"]
    assert load_tokens(tmp_path / "b.json") == ["t2"]
    with pytest.raises(ConfigError):
        load_tokens(tmp_path / "c.json")


# ------------------------------------------------------------ engine-level


def sender(x: Num, ctx: AgentContext) -> Num:
    ctx.effect(REQ.action, REQ.target, REQ.payload)
    return x


def test_engine_queues_approval_and_executor_not_called(tmp_path: Path) -> None:
    rec = Recorder()
    p = Pipeline("e", Num, (Step("send", adapt(sender)),), effects={"email.send": rec})
    orch, _, _ = make_orch(tmp_path, p)
    result = orch.run({"value": 1})
    assert result.status == "awaiting_approval"
    assert result.steps["send"].attempts == 1  # approval is not retried
    (item,) = result.review_items
    assert item["kind"] == "approval" and item["payload"] == REQ.payload
    assert item["request_id"] == REQ.request_id
    assert rec.calls == []


def test_engine_executes_with_token(tmp_path: Path, key: Ed25519PrivateKey) -> None:
    rec = Recorder()
    p = Pipeline("e", Num, (Step("send", adapt(sender)),), effects={"email.send": rec})
    gate = EffectGate(
        key.public_key(),
        EffectLedger(tmp_path / "l.jsonl"),
        p.effects,
        [issue_token(key, REQ, approver="Kevin")],
    )
    orch, _, _ = make_orch(tmp_path, p, gate=gate)
    assert orch.run({"value": 1}).status == "succeeded"
    assert len(rec.calls) == 1


def test_batch_effects_run_approved_and_queue_the_rest(
    tmp_path: Path, key: Ed25519PrivateKey
) -> None:
    rec = Recorder()
    other = EffectRequest("email.send", "b@example.com", {"body": "b"})
    third = EffectRequest("email.send", "c@example.com", {"body": "c"})

    def batch(x: Num, ctx: AgentContext) -> Num:
        ctx.effects([(r.action, r.target, r.payload) for r in (REQ, other, third)])
        return x

    p = Pipeline("b", Num, (Step("send", adapt(batch)),), effects={"email.send": rec})
    gate = EffectGate(
        key.public_key(),
        EffectLedger(tmp_path / "l.jsonl"),
        p.effects,
        [issue_token(key, REQ, approver="Kevin")],
    )
    orch, _, _ = make_orch(tmp_path, p, gate=gate)
    result = orch.run({"value": 1})
    assert result.status == "awaiting_approval"
    assert [c.target for c in rec.calls] == [REQ.target]
    assert {i["target"] for i in result.review_items} == {"b@example.com", "c@example.com"}


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_network_guard_blocks_direct_io_even_if_swallowed(tmp_path: Path) -> None:
    port = _closed_port()

    def sneaky(x: Num) -> Num:
        # Swallowing the error must not hide the violation.
        with contextlib.suppress(Exception):
            socket.create_connection(("127.0.0.1", port), timeout=1)
        return x

    p = Pipeline("n", Num, (Step("a", adapt(sneaky), retry=RetryPolicy(max_attempts=3)),))
    orch, _, _ = make_orch(tmp_path, p)
    result = orch.run({"value": 1})
    assert result.steps["a"].status == "failed"
    assert result.steps["a"].attempts == 1
    assert "PermissionDenied" in result.steps["a"].error and "socket." in result.steps["a"].error


def test_network_guard_blocks_subprocess(tmp_path: Path) -> None:
    def shell(x: Num) -> Num:
        subprocess.run([sys.executable, "-c", "print(1)"], check=True)
        return x

    p = Pipeline("n", Num, (Step("a", adapt(shell)),))
    orch, _, _ = make_orch(tmp_path, p)
    result = orch.run({"value": 1})
    assert result.steps["a"].status == "failed"
    assert "subprocess.Popen" in result.steps["a"].error


def test_allow_network_step_and_guard_inert_outside_agents(tmp_path: Path) -> None:
    port = _closed_port()

    def direct(x: Num) -> Num:
        socket.create_connection(("127.0.0.1", port), timeout=1)
        return x

    p = Pipeline(
        "n",
        Num,
        (
            Step(
                "a",
                adapt(direct),
                allow_network=True,
                escalate=False,
                retry=RetryPolicy(max_attempts=1),
                on_exhausted="fail",
            ),
        ),
    )
    orch, _, _ = make_orch(tmp_path, p)
    result = orch.run({"value": 1})
    assert "ConnectionRefusedError" in result.steps["a"].error
    with pytest.raises(ConnectionRefusedError):  # the hook is installed but inert here
        socket.create_connection(("127.0.0.1", port), timeout=1)


def test_tokens_expire_by_clock(tmp_path: Path, key: Ed25519PrivateKey) -> None:
    token = issue_token(key, REQ, approver="Kevin", ttl_seconds=5, now=time.time() - 10)
    with pytest.raises(ApprovalRequired):
        gate_for(tmp_path, key, [token], Recorder()).perform(REQ, run_id="r", step_id="s")
