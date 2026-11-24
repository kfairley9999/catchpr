"""Stub LeadFlow sales team: researcher -> writer -> compliance -> scheduler.

Prospects are synthetic (example.com domains). The compliance step enforces
the team's outreach rules deterministically: at most one follow-up per
contact, every message links its landing page, and no guarantee language.
The scheduler can only queue sends; each send is an approval-gated effect.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from catchpr.orchestra.agents import AgentContext, adapt
from catchpr.orchestra.examples.coi import local_outbox
from catchpr.orchestra.pipeline import Handoff, Pipeline, RetryPolicy, Step
from catchpr.orchestra.providers import LLMRequest

BANNED_PHRASES = ("guarantee", "guaranteed", "risk-free", "act now")
MAX_TOUCHES = 2  # first email plus one follow-up


class LedgerEntry(BaseModel):
    email: str
    sent_count: int = Field(ge=0)


class ResearchRequest(BaseModel):
    region: str
    vertical: str
    count: int = Field(ge=1, le=25)
    landing_page: str = Field(pattern=r"^https://")
    send_ledger: list[LedgerEntry] = []


class Prospect(BaseModel):
    company: str
    email: str
    reason: str


class ProspectList(BaseModel):
    prospects: list[Prospect]
    landing_page: str
    confidence: float = Field(ge=0, le=1)


class Draft(BaseModel):
    email: str
    subject: str
    body: str


class Drafts(BaseModel):
    drafts: list[Draft]
    landing_page: str


class Approved(BaseModel):
    approved: list[Draft]
    rejected: dict[str, str]


class Scheduled(BaseModel):
    queued: list[str]
    sent: list[str]


def researcher(req: ResearchRequest, ctx: AgentContext) -> ProspectList:
    raw = ctx.llm.complete(
        f"List {req.count} {req.vertical} businesses in {req.region} as JSON "
        '{"prospects":[{"company","email","reason"}],"confidence":0-1}.',
        max_tokens=600,
        json_mode=True,
    )
    data = json.loads(raw)
    return ProspectList(
        prospects=data["prospects"][: req.count],
        landing_page=req.landing_page,
        confidence=data["confidence"],
    )


def writer(plist: ProspectList, ctx: AgentContext) -> Drafts:
    drafts: list[Draft] = []
    for p in plist.prospects:
        body = ctx.llm.complete(
            f"Write a 3-sentence intro to {p.company} ({p.reason}). "
            f"Link {plist.landing_page}. No hype.",
            max_tokens=200,
        )
        drafts.append(Draft(email=p.email, subject=f"Quick idea for {p.company}", body=body))
    return Drafts(drafts=drafts, landing_page=plist.landing_page)


def bind_compliance(h: Handoff) -> dict[str, Any]:
    run_input = h.run_input
    assert isinstance(run_input, ResearchRequest)
    drafts = h.outputs["writer"]
    assert isinstance(drafts, Drafts)
    return {
        "drafts": drafts.drafts,
        "landing_page": drafts.landing_page,
        "ledger": run_input.send_ledger,
    }


class ComplianceInput(BaseModel):
    drafts: list[Draft]
    landing_page: str
    ledger: list[LedgerEntry]


def compliance(inp: ComplianceInput) -> Approved:
    touches = {e.email.lower(): e.sent_count for e in inp.ledger}
    approved: list[Draft] = []
    rejected: dict[str, str] = {}
    for d in inp.drafts:
        lowered = d.body.lower()
        if touches.get(d.email.lower(), 0) >= MAX_TOUCHES:
            rejected[d.email] = "already sent the first email and one follow-up"
        elif inp.landing_page not in d.body:
            rejected[d.email] = "message does not link the landing page"
        elif any(p in lowered for p in BANNED_PHRASES):
            rejected[d.email] = "message uses guarantee/hype language"
        else:
            approved.append(d)
    return Approved(approved=approved, rejected=rejected)


def scheduler(batch: Approved, ctx: AgentContext) -> Scheduled:
    # Every send is approval-gated; the whole batch is queued for one review pass.
    ctx.effects([("email.send", d.email, d.model_dump()) for d in batch.approved])
    emails = [d.email for d in batch.approved]
    return Scheduled(queued=emails, sent=emails)


def build(outbox: Any = None) -> Pipeline:
    from pathlib import Path

    return Pipeline(
        name="leadflow-sales",
        input_model=ResearchRequest,
        version="stub-1",
        effects={"email.send": local_outbox(outbox or Path("runs") / "outbox")},
        steps=(
            Step(
                "researcher",
                adapt(researcher, name="leadflow.researcher"),
                start_tier="standard",
                min_confidence=0.6,
            ),
            Step(
                "writer",
                adapt(writer, name="leadflow.writer"),
                after=("researcher",),
                start_tier="fast",
            ),
            Step(
                "compliance",
                adapt(compliance, name="leadflow.compliance"),
                after=("writer",),
                bind=bind_compliance,
                retry=RetryPolicy(max_attempts=1),
                escalate=False,
                on_exhausted="fail",
            ),
            Step(
                "scheduler",
                adapt(scheduler, name="leadflow.scheduler"),
                after=("compliance",),
                retry=RetryPolicy(max_attempts=1),
                escalate=False,
            ),
        ),
    )


SAMPLE_INPUT: dict[str, Any] = {
    "region": "Los Angeles, CA",
    "vertical": "independent insurance agency",
    "count": 3,
    "landing_page": "https://kfairley9999.github.io/coi-demo-site/",
    "send_ledger": [{"email": "owner@agency-c.example.com", "sent_count": 2}],
}


def demo_responder(request: LLMRequest) -> str:
    prompt = request.messages[-1].content
    if prompt.startswith("List "):
        return json.dumps(
            {
                "prospects": [
                    {
                        "company": "Agency A (synthetic)",
                        "email": "owner@agency-a.example.com",
                        "reason": "handles contractor COIs by hand",
                    },
                    {
                        "company": "Agency B (synthetic)",
                        "email": "ops@agency-b.example.com",
                        "reason": "posted about certificate backlog",
                    },
                    {
                        "company": "Agency C (synthetic)",
                        "email": "owner@agency-c.example.com",
                        "reason": "previous contact",
                    },
                ],
                "confidence": 0.8,
            }
        )
    company = prompt.split(" to ", 1)[1].split(" (", 1)[0]
    if company.startswith("Agency B"):
        return f"Hi {company}, we guarantee faster certificate checks. Want a demo?"
    link = prompt.split("Link ", 1)[1].split(". ", 1)[0]
    return (
        f"Hi {company}, we built a certificate checker for agencies that review "
        f"contractor COIs by hand. It flags missing limits in seconds: {link} "
        "Open to a 15-minute look next week?"
    )
