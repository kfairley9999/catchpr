"""Live escalation demo: same COI pipeline, extractor threshold raised to 0.95."""

import dataclasses
import os
from decimal import Decimal
from pathlib import Path

from catchpr.orchestra import BudgetLimits, Orchestrator, build_tierset
from catchpr.orchestra.examples import coi
from catchpr.orchestra.pipeline import Pipeline
from catchpr.orchestra.trace import html_trace, text_trace

base = coi.build(outbox=Path("runs/outbox"))
steps = tuple(
    dataclasses.replace(s, min_confidence=0.95) if s.id == "extractor" else s for s in base.steps
)
p = Pipeline(base.name + "-strict", base.input_model, steps, base.effects, base.version)
orch = Orchestrator(
    p,
    build_tierset(mode="deepinfra", env=os.environ),
    runs_dir=Path("runs"),
    limits=BudgetLimits(max_usd=Decimal("0.02"), max_tokens=20000, max_seconds=180),
)
r = orch.run(coi.SAMPLE_INPUT)
(r.run_dir / "trace.html").write_text(html_trace(r.run_dir))
print(text_trace(r.run_dir))
