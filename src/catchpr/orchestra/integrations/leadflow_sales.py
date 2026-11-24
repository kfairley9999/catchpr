"""Wire the LeadFlow sales team (``leadflow_sales``) into catchpr.

One run = one prospect: researcher -> writer -> compliance -> scheduler.
The team is draft-only by construction (``may_send`` is always False). catchpr
adds budgets, audit, replay, and routing on top; actually sending a draft is a
separate approval-gated effect step that is deliberately not wired here.

* Model calls go through :class:`CatchprLeadflowProvider` (metered, audited,
  replayable). When the run is simulated the provider reports the name
  ``mock`` so the writer falls back to its deterministic template.
* A compliance verdict of ``block`` scores 0 and goes to catchpr's review
  queue instead of continuing, so a blocked draft is never scheduled.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from catchpr.orchestra.agents import adapt, current_context
from catchpr.orchestra.pipeline import Handoff, Pipeline, RetryPolicy, Step


class CatchprLeadflowProvider:
    """Implements ``leadflow_sales.llm.base.LLMProvider`` on top of ``ctx.llm``."""

    max_tokens = 900

    @property
    def name(self) -> str:
        try:
            return "mock" if current_context().provider.name == "mock" else "catchpr"
        except Exception:
            return "catchpr"

    @property
    def model(self) -> str:
        try:
            return current_context().tier.model
        except Exception:
            return "unbound"

    def complete(
        self, messages: list[Any], *, json_mode: bool = False, temperature: float = 0.2
    ) -> Any:
        from leadflow_sales.llm.base import LLMResult

        result = current_context().llm.call(
            [(m.role, m.content) for m in messages],
            max_tokens=self.max_tokens,
            temperature=temperature,
            json_mode=json_mode,
        )
        return LLMResult(
            text=result.text,
            model=result.model,
            provider=f"catchpr:{result.provider}",
            usage={"prompt_tokens": result.input_tokens, "completion_tokens": result.output_tokens},
        )


def build(
    *,
    catalog_paths: list[Path] | None = None,
    ledger_roots: list[Path] | None = None,
    opt_out_files: list[Path] | None = None,
    start_tier: str = "standard",
) -> Pipeline:
    from leadflow_sales.agents.compliance import ComplianceAgent
    from leadflow_sales.agents.researcher import Researcher
    from leadflow_sales.agents.scheduler import Scheduler
    from leadflow_sales.agents.writer import Writer
    from leadflow_sales.config import TIMEZONE, default_ledger_roots
    from leadflow_sales.ledgers.gmail_sent import load_gmail_sent
    from leadflow_sales.pipeline import DEFAULT_CATALOGS
    from leadflow_sales.schemas import (
        ComplianceInput,
        ComplianceOutput,
        ProspectSeed,
        Purpose,
        ResearcherInput,
        ResearcherOutput,
        SchedulerInput,
        WriterInput,
        WriterOutput,
    )

    class LeadJob(BaseModel):
        model_config = ConfigDict(extra="forbid")
        prospect: ProspectSeed
        purpose: Purpose = "cold_outreach"
        public_notes: str | None = None
        prior_thread_summary: str | None = None
        gmail_sent_path: str | None = None
        now: datetime | None = None

    llm = CatchprLeadflowProvider()
    catalogs = (
        catalog_paths if catalog_paths is not None else [p for p in DEFAULT_CATALOGS if p.is_file()]
    )
    researcher = Researcher(catalogs, llm=llm)
    writer = Writer(llm=llm)
    compliance = ComplianceAgent.from_roots(
        ledger_roots if ledger_roots is not None else default_ledger_roots(),
        opt_out_files=opt_out_files,
    )
    scheduler = Scheduler()

    def job(h: Handoff) -> LeadJob:
        assert isinstance(h.run_input, LeadJob)
        return h.run_input

    def to_researcher(h: Handoff) -> ResearcherInput:
        j = job(h)
        return ResearcherInput(prospect=j.prospect, public_notes=j.public_notes)

    def to_writer(h: Handoff) -> WriterInput:
        j, r = job(h), h.outputs["researcher"]
        assert isinstance(r, ResearcherOutput)
        return WriterInput(
            prospect=r.prospect, purpose=j.purpose, prior_thread_summary=j.prior_thread_summary
        )

    def to_compliance(h: Handoff) -> ComplianceInput:
        j, w = job(h), h.outputs["writer"]
        assert isinstance(w, WriterOutput)
        return ComplianceInput(
            draft=w.draft,
            prospect=j.prospect,
            purpose=j.purpose,
            gmail_sent=load_gmail_sent(j.gmail_sent_path),
        )

    def to_scheduler(h: Handoff) -> SchedulerInput:
        j, r, w, c = job(h), h.outputs["researcher"], h.outputs["writer"], h.outputs["compliance"]
        assert isinstance(r, ResearcherOutput) and isinstance(w, WriterOutput)
        assert isinstance(c, ComplianceOutput)
        return SchedulerInput(
            draft=w.draft, prospect=r.prospect, compliance=c, timezone=TIMEZONE, now=j.now
        )

    def compliance_confidence(out: BaseModel) -> float:
        assert isinstance(out, ComplianceOutput)
        return 1.0 if out.verdict == "allow_draft" else 0.0

    one_shot = RetryPolicy(max_attempts=1)
    return Pipeline(
        name="leadflow-sales-agents",
        input_model=LeadJob,
        version="leadflow_sales",
        steps=(
            Step(
                "researcher",
                adapt(researcher.run, name="leadflow.researcher"),
                bind=to_researcher,
                start_tier=start_tier,
            ),
            Step(
                "writer",
                adapt(writer.run, name="leadflow.writer"),
                after=("researcher",),
                bind=to_writer,
                start_tier=start_tier,
            ),
            Step(
                "compliance",
                adapt(compliance.run, name="leadflow.compliance"),
                after=("writer",),
                bind=to_compliance,
                retry=one_shot,
                escalate=False,
                min_confidence=0.5,
                confidence_of=compliance_confidence,
            ),
            Step(
                "scheduler",
                adapt(scheduler.run, name="leadflow.scheduler"),
                after=("researcher", "writer", "compliance"),
                bind=to_scheduler,
                retry=one_shot,
                escalate=False,
            ),
        ),
    )
