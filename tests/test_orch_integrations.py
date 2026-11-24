"""Runs the real agent teams under catchpr with the mock provider.

Skipped unless ``coi_agents`` and ``leadflow_sales`` are importable (put
/workspace/coi-agents and /workspace/leadflow-sales-agents/src on PYTHONPATH).
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from catchpr.orchestra.audit import verify_run
from catchpr.orchestra.engine import Orchestrator
from catchpr.orchestra.replay import replay_run
from catchpr.orchestra.tiers import build_tierset

DEV_PDF = Path("/workspace/acord25-benchmark/pdfs/coi_01.pdf")
REQS = Path("/workspace/coi-agents/examples/la_agency_requirements.json")


def test_coi_agents_team(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("coi_agents")
    if not DEV_PDF.exists() or not REQS.exists():
        pytest.skip("dev certificate or requirements not available")
    monkeypatch.setenv("COI_AGENTS_VAR", str(tmp_path / "var"))
    from catchpr.orchestra.integrations import coi_agents as wiring

    inbox = tmp_path / "in"
    inbox.mkdir()
    shutil.copy(DEV_PDF, inbox / DEV_PDF.name)
    job = wiring.job_from_pdf(inbox / DEV_PDF.name, REQS)
    pipeline = wiring.build(llm_policy="always")
    orch = Orchestrator(pipeline, build_tierset(mode="mock"), runs_dir=tmp_path / "runs")
    result = orch.run(job)
    assert result.steps["extractor"].status == "succeeded"
    assert result.steps["checker"].status == "succeeded"
    assert result.steps["reviewer"].status in ("succeeded", "needs_review")
    assert verify_run(result.run_dir).ok
    # Simulated answers must not land in the team's response cache.
    assert not list((tmp_path / "var").rglob("llm/*.json"))
    assert replay_run(
        result.run_dir, wiring.build(llm_policy="always"), work_dir=tmp_path / "rp"
    ).match


def test_leadflow_sales_team(tmp_path: Path) -> None:
    pytest.importorskip("leadflow_sales")
    from catchpr.orchestra.integrations import leadflow_sales as wiring

    sent = tmp_path / "sent.json"
    sent.write_text(json.dumps([]))
    pipeline = wiring.build(catalog_paths=[], ledger_roots=[])
    job = {
        "prospect": {
            "agency_name": "Example Agency (synthetic)",
            "email": "owner@example-agency.example.com",
            "city": "Los Angeles",
            "state": "CA",
        },
        "now": "2026-11-23T20:00:00-08:00",
    }
    orch = Orchestrator(
        pipeline, build_tierset(mode="mock", env=dict(os.environ)), runs_dir=tmp_path / "runs"
    )
    result = orch.run(job)
    assert result.steps["researcher"].status == "succeeded"
    assert result.steps["writer"].status == "succeeded"
    # No Gmail Sent export -> the team's compliance blocks -> catchpr routes to a human.
    assert result.steps["compliance"].status == "needs_review"
    assert result.steps["scheduler"].status == "skipped"
    assert verify_run(result.run_dir).ok
