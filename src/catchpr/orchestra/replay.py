"""Deterministic replay of a recorded run.

Replay re-executes the *current* agent code against the run's recorded input,
serving every model call from the recorded responses (matched by request
digest) and every effect from the recorded results. Nothing touches the
network and no effect executes. It then compares each step's status and output
hash with the original. A mismatch means the agent code, prompts, or pipeline
changed behaviour since the run was recorded.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from catchpr.orchestra.audit import BlobStore, read_events
from catchpr.orchestra.budget import BudgetLimits
from catchpr.orchestra.engine import Orchestrator, Override
from catchpr.orchestra.errors import ConfigError
from catchpr.orchestra.guard import EffectGate, EffectLedger
from catchpr.orchestra.pipeline import Pipeline
from catchpr.orchestra.pricing import Price
from catchpr.orchestra.providers import Completion, ReplayProvider
from catchpr.orchestra.tiers import Tier, TierSet


@dataclass
class ReplayReport:
    original_run: str
    replay_run: str
    match: bool
    steps: dict[str, dict[str, Any]] = field(default_factory=dict)
    differences: list[str] = field(default_factory=list)


def _tier_from_record(row: dict[str, Any]) -> Tier:
    price = None
    if row.get("usd_per_m_input") is not None and row.get("usd_per_m_output") is not None:
        price = Price.of(row["usd_per_m_input"], row["usd_per_m_output"])
    return Tier(
        name=row["name"],
        provider=row["provider"],
        model=row["model"],
        price=price,
        enabled=bool(row["enabled"]),
        disabled_reason=row.get("disabled_reason", ""),
    )


def replay_run(run_dir: Path, pipeline: Pipeline, *, work_dir: Path | None = None) -> ReplayReport:
    events = read_events(run_dir / "audit.jsonl")
    if not events or events[0].get("event") != "run_start":
        raise ConfigError(f"{run_dir} has no run_start record")
    start = events[0]
    if not start.get("payloads_stored"):
        raise ConfigError("this run was recorded without payloads; it cannot be replayed")
    blobs = BlobStore(run_dir / "blobs", enabled=False)
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))

    recorded: list[tuple[str, Completion]] = []
    overrides: dict[str, Override] = {}
    effects: dict[str, dict[str, Any]] = {}
    for ev in events:
        kind = ev.get("event")
        if kind == "llm_call" and not ev.get("discarded"):
            recorded.append(
                (ev["request_digest"], Completion.from_dict(blobs.get(ev["response_sha256"])))
            )
        elif kind == "step_override":
            overrides[ev["step"]] = (blobs.get(ev["output_sha256"]), ev["source"])
        elif kind == "effect_executed":
            effects[ev["request_id"]] = dict(ev.get("result") or {})

    tiers = [_tier_from_record(row) for row in start["tiers"]]
    provider = ReplayProvider(recorded)
    tierset = TierSet(
        tiers,
        {t.name: provider for t in tiers if t.enabled},
        notes=["replay: responses served from the recorded run"],
        simulated=bool(start.get("simulated")),
    )
    limits = start["limits"]
    root = work_dir or Path(tempfile.mkdtemp(prefix="catchpr-replay-"))
    gate = EffectGate(
        public_key=None, ledger=EffectLedger(root / "replay-ledger.jsonl"), replay_results=effects
    )
    orch = Orchestrator(
        pipeline,
        tierset,
        runs_dir=root,
        gate=gate,
        sleep=lambda _s: None,
        limits=BudgetLimits(
            max_usd=Decimal(limits["max_usd"]),
            max_tokens=int(limits["max_tokens"]),
            max_seconds=None,
        ),
    )
    result = orch.run(
        blobs.get(start["input_sha256"]), overrides=overrides, parent_run=summary["run_id"]
    )
    report = ReplayReport(summary["run_id"], result.run_id, True)
    order = [s["id"] for s in summary["pipeline"]["steps"]]
    for sid in [s for s in order if s in summary["steps"]]:
        info = summary["steps"][sid]
        new = result.steps.get(sid)
        new_status = new.status if new else "missing"
        key = "stable_sha256" if info.get("stable_sha256") else "output_sha256"
        new_sha = (
            (new.stable_sha256 if key == "stable_sha256" else new.output_sha256) if new else None
        )
        same = new_status == info["status"] and new_sha == info.get(key)
        report.steps[sid] = {
            "original": {"status": info["status"], "digest": info.get(key)},
            "replay": {"status": new_status, "digest": new_sha},
            "compared": key,
            "match": same,
        }
        if not same:
            report.match = False
            report.differences.append(
                f"{sid}: {info['status']}/{str(info.get(key))[:12]} -> "
                f"{new_status}/{str(new_sha)[:12]}"
                + (f" ({new.error})" if new is not None and new.error and not same else "")
            )
    return report
