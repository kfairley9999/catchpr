from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.orch_support import make_orch

from catchpr.orchestra.agents import adapt
from catchpr.orchestra.audit import AuditLog, BlobStore, read_events, verify_run
from catchpr.orchestra.engine import Orchestrator
from catchpr.orchestra.errors import ConfigError, TransientError
from catchpr.orchestra.examples import coi
from catchpr.orchestra.pipeline import Pipeline, Step
from catchpr.orchestra.providers import MockProvider
from catchpr.orchestra.replay import replay_run


def coi_run(tmp_path: Path, provider: MockProvider | None = None, **kw):  # type: ignore[no-untyped-def]
    pipeline = coi.build(outbox=tmp_path / "outbox")
    orch, prov, _ = make_orch(
        tmp_path, pipeline, provider or MockProvider(responder=coi.demo_responder), **kw
    )
    return orch, orch.run(coi.SAMPLE_INPUT), prov


def test_coi_demo_shape(tmp_path: Path) -> None:
    _, result, _ = coi_run(tmp_path)
    assert result.status == "awaiting_approval"
    assert result.steps["extractor"].tiers_tried == ["fast", "standard"]
    check = result.output("checker")
    assert isinstance(check, coi.CheckResult) and not check.passed
    assert "below the $1,000,000 requirement" in check.issues[0]


def test_verify_detects_edits_deletions_and_blob_tampering(tmp_path: Path) -> None:
    _, result, _ = coi_run(tmp_path)
    run_dir = result.run_dir
    assert verify_run(run_dir).ok
    log = run_dir / "audit.jsonl"
    original = log.read_text()
    lines = original.splitlines()

    edited = json.loads(lines[7])
    edited["cost_usd"] = "0"
    log.write_text("\n".join([*lines[:7], json.dumps(edited), *lines[8:]]) + "\n")
    report = verify_run(run_dir)
    assert not report.ok and any("hash mismatch" in e for e in report.errors)

    log.write_text("\n".join([*lines[:3], *lines[4:]]) + "\n")
    assert any("chain broken" in e for e in verify_run(run_dir).errors)

    log.write_text("\n".join(lines[:-1]) + "\n")
    assert any("truncated" in e for e in verify_run(run_dir).errors)

    log.write_text(original)
    blob = next((run_dir / "blobs").glob("*.json"))
    blob.write_text('{"tampered": true}')
    assert any("corrupted" in e for e in verify_run(run_dir).errors)
    blob.unlink()
    assert any("missing" in e for e in verify_run(run_dir).errors)


def test_verify_handles_missing_and_garbage(tmp_path: Path) -> None:
    assert not verify_run(tmp_path).ok
    (tmp_path / "audit.jsonl").write_text("{not json\n")
    assert "not valid JSONL" in verify_run(tmp_path).errors[0]


def test_audit_log_refuses_to_overwrite(tmp_path: Path) -> None:
    blobs = BlobStore(tmp_path / "b")
    log = AuditLog(tmp_path / "a.jsonl", "r", blobs)
    log.emit("x")
    log.close()
    with pytest.raises(FileExistsError):
        AuditLog(tmp_path / "a.jsonl", "r", blobs)


def test_hash_only_mode_cannot_replay_or_resume(tmp_path: Path) -> None:
    orch, result, _ = coi_run(tmp_path, store_payloads=False)
    assert verify_run(result.run_dir).ok
    assert not (result.run_dir / "blobs").exists()
    with pytest.raises(ConfigError):
        replay_run(result.run_dir, coi.build(outbox=tmp_path / "o"))
    with pytest.raises(ConfigError):
        orch.resume(result.run_dir)


def test_replay_matches_even_after_transient_faults(tmp_path: Path) -> None:
    provider = MockProvider(
        responder=coi.demo_responder, faults=[TransientError("503"), None, TransientError("x")]
    )
    _, result, _ = coi_run(tmp_path, provider)
    report = replay_run(result.run_dir, coi.build(outbox=tmp_path / "o"), work_dir=tmp_path / "rp")
    assert report.match, report.differences
    assert set(report.steps) == {"intake", "extractor", "checker", "reviewer"}


def test_replay_detects_changed_agent_behaviour(tmp_path: Path) -> None:
    _, result, _ = coi_run(tmp_path)

    def lenient_checker(ex: coi.Extraction) -> coi.CheckResult:
        return coi.CheckResult(
            doc_id=ex.doc_id, sender=ex.sender, insured=ex.insured, passed=True, issues=[]
        )

    original = coi.build(outbox=tmp_path / "o")
    steps = tuple(
        Step("checker", adapt(lenient_checker), after=("extractor",)) if s.id == "checker" else s
        for s in original.steps
    )
    changed = Pipeline(original.name, original.input_model, steps, original.effects)
    report = replay_run(result.run_dir, changed, work_dir=tmp_path / "rp")
    assert not report.match
    assert any(d.startswith("checker") for d in report.differences)


def test_replay_reports_unrecorded_model_calls(tmp_path: Path) -> None:
    _, result, _ = coi_run(tmp_path)

    def other_extractor(doc: coi.IntakeResult, ctx) -> coi.Extraction:  # type: ignore[no-untyped-def]
        fields = ctx.llm.complete_json("a different prompt", coi.ExtractedFields, max_tokens=10)
        return coi.Extraction(
            **fields.model_dump(),
            doc_id=doc.doc_id,
            sender=doc.sender,
            requirements=doc.requirements,
            model="x",
        )

    original = coi.build(outbox=tmp_path / "o")
    steps = tuple(
        Step("extractor", adapt(other_extractor), after=("intake",)) if s.id == "extractor" else s
        for s in original.steps
    )
    changed = Pipeline(original.name, original.input_model, steps, original.effects)
    report = replay_run(result.run_dir, changed, work_dir=tmp_path / "rp")
    assert not report.match
    assert "ReplayMismatch" in report.differences[0]


def test_resume_with_human_resolution(tmp_path: Path) -> None:
    unsure = MockProvider(responder=lambda r: json.dumps({"confidence": 0.2}))
    orch, result, _ = coi_run(tmp_path, unsure)
    assert result.steps["extractor"].status == "needs_review"
    assert result.steps["checker"].status == "skipped"
    human = {
        "insured": "Example Plumbing LLC",
        "gl_each_occurrence": 2_000_000,
        "auto_combined_single": 1_000_000,
        "additional_insured": True,
        "expires": "2027-06-30",
        "confidence": 1.0,
        "doc_id": "coi-demo-0001",
        "sender": "certs@example-vendor.example",
        "model": "human",
        "requirements": coi.SAMPLE_INPUT["requirements"],
    }
    (result.run_dir / "resolutions.json").write_text(
        json.dumps({"extractor": {"output": human, "reviewer": "Kevin"}})
    )
    resumed = orch.resume(result.run_dir)
    assert resumed.status == "succeeded"
    assert resumed.steps["extractor"].source == "human:Kevin"
    assert resumed.steps["intake"].source.startswith("resumed:")
    decision = resumed.output("reviewer")
    assert isinstance(decision, coi.ReviewDecision) and decision.decision == "accept"
    events = read_events(resumed.run_dir / "audit.jsonl")
    assert events[0]["parent_run"] == result.run_id


def test_resolutions_file_must_be_object(tmp_path: Path) -> None:
    orch, result, _ = coi_run(tmp_path)
    (result.run_dir / "resolutions.json").write_text("[]")
    with pytest.raises(ConfigError):
        orch.resume(result.run_dir)


def test_replay_requires_run_start(tmp_path: Path) -> None:
    (tmp_path / "audit.jsonl").write_text("")
    with pytest.raises(ConfigError):
        replay_run(tmp_path, coi.build(outbox=tmp_path / "o"))
    assert isinstance(Orchestrator, type)
