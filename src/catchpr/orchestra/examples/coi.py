"""Stub COI back-office team: intake -> extractor -> checker -> reviewer.

Data is synthetic. The extractor is the only model-backed step. In the demo
its mock responses are scripted per tier (low confidence on ``fast``, good on
``standard``) so the escalation path is visible; real models decide this for
themselves when you run with DeepInfra.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from catchpr.orchestra.agents import AgentContext, adapt
from catchpr.orchestra.guard import EffectRequest
from catchpr.orchestra.pipeline import Pipeline, RetryPolicy, Step
from catchpr.orchestra.providers import LLMRequest


class Requirements(BaseModel):
    gl_each_occurrence_min: int = Field(ge=0)
    auto_combined_single_min: int = Field(ge=0)
    additional_insured_required: bool
    must_be_valid_on: date


class CoiEmail(BaseModel):
    message_id: str
    sender: str
    subject: str
    attachment_text: str = Field(min_length=20)
    requirements: Requirements


class IntakeResult(BaseModel):
    doc_id: str
    sender: str
    certificate_text: str
    requirements: Requirements


class ExtractedFields(BaseModel):
    insured: str | None = None
    carrier: str | None = None
    policy_number: str | None = None
    gl_each_occurrence: int | None = None
    auto_combined_single: int | None = None
    additional_insured: bool | None = None
    expires: date | None = None
    confidence: float = Field(ge=0, le=1)


class Extraction(ExtractedFields):
    doc_id: str
    sender: str
    requirements: Requirements
    model: str


class CheckResult(BaseModel):
    doc_id: str
    sender: str
    insured: str | None
    passed: bool
    issues: list[str]
    confidence: float = 1.0


class ReviewDecision(BaseModel):
    doc_id: str
    decision: Literal["accept", "request_fix"]
    issues: list[str]
    notice: dict[str, Any] | None = None


def intake(email: CoiEmail) -> IntakeResult:
    text = re.sub(r"[ \t]+", " ", email.attachment_text).strip()
    return IntakeResult(
        doc_id=f"coi-{email.message_id}",
        sender=email.sender,
        certificate_text=text,
        requirements=email.requirements,
    )


EXTRACT_SYSTEM = (
    "You extract fields from an ACORD 25 certificate of liability insurance. "
    "Return JSON with keys insured, carrier, policy_number, gl_each_occurrence (int USD), "
    "auto_combined_single (int USD), additional_insured (bool), expires (YYYY-MM-DD), "
    "and confidence (0-1, your honest certainty that every field is right). Use null "
    "for anything you cannot read."
)


def extractor(doc: IntakeResult, ctx: AgentContext) -> Extraction:
    fields = ctx.llm.complete_json(
        f"Certificate text:\n{doc.certificate_text}",
        ExtractedFields,
        system=EXTRACT_SYSTEM,
        max_tokens=400,
    )
    return Extraction(
        **fields.model_dump(),
        doc_id=doc.doc_id,
        sender=doc.sender,
        requirements=doc.requirements,
        model=ctx.llm.model,
    )


def checker(ex: Extraction) -> CheckResult:
    req = ex.requirements
    issues: list[str] = []
    if ex.gl_each_occurrence is None:
        issues.append("GL each-occurrence limit is missing")
    elif ex.gl_each_occurrence < req.gl_each_occurrence_min:
        issues.append(
            f"GL each occurrence ${ex.gl_each_occurrence:,} is below the "
            f"${req.gl_each_occurrence_min:,} requirement"
        )
    if ex.auto_combined_single is None:
        issues.append("Auto combined single limit is missing")
    elif ex.auto_combined_single < req.auto_combined_single_min:
        issues.append(
            f"Auto CSL ${ex.auto_combined_single:,} is below the "
            f"${req.auto_combined_single_min:,} requirement"
        )
    if req.additional_insured_required and ex.additional_insured is not True:
        issues.append("Additional insured endorsement is not shown")
    if ex.expires is None or ex.expires < req.must_be_valid_on:
        issues.append(f"Policy is not shown as valid on {req.must_be_valid_on.isoformat()}")
    return CheckResult(
        doc_id=ex.doc_id,
        sender=ex.sender,
        insured=ex.insured,
        passed=not issues,
        issues=issues,
    )


def reviewer(check: CheckResult, ctx: AgentContext) -> ReviewDecision:
    if check.passed:
        return ReviewDecision(doc_id=check.doc_id, decision="accept", issues=[])
    notice = {
        "to": check.sender,
        "subject": f"Certificate {check.doc_id}: corrections needed",
        "body": "Thanks for the certificate. Before we can accept it we need:\n- "
        + "\n- ".join(check.issues),
    }
    # Outbound email is a side effect: it runs only with a human approval token.
    ctx.effect("email.send", check.sender, notice)
    return ReviewDecision(
        doc_id=check.doc_id, decision="request_fix", issues=check.issues, notice=notice
    )


def local_outbox(directory: Path) -> Any:
    """Demo executor for ``email.send``: writes the message to a local folder.
    It never contacts a mail server."""

    def execute(request: EffectRequest) -> dict[str, Any]:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{request.request_id}.json"
        path.write_text(json.dumps(request.payload, indent=2, sort_keys=True), encoding="utf-8")
        return {"delivered_to": "local-outbox", "path": str(path)}

    return execute


def build(outbox: Path | None = None) -> Pipeline:
    executors = {"email.send": local_outbox(outbox or Path("runs") / "outbox")}
    return Pipeline(
        name="coi-backoffice",
        input_model=CoiEmail,
        version="stub-1",
        effects=executors,
        steps=(
            Step(
                "intake",
                adapt(intake, name="coi.intake"),
                timeout_s=10,
                retry=RetryPolicy(max_attempts=1),
                escalate=False,
            ),
            Step(
                "extractor",
                adapt(extractor, name="coi.extractor"),
                after=("intake",),
                timeout_s=60,
                min_confidence=0.85,
                start_tier="fast",
            ),
            Step(
                "checker",
                adapt(checker, name="coi.checker"),
                after=("extractor",),
                timeout_s=10,
                retry=RetryPolicy(max_attempts=1),
                escalate=False,
            ),
            Step(
                "reviewer",
                adapt(reviewer, name="coi.reviewer"),
                after=("checker",),
                timeout_s=30,
                retry=RetryPolicy(max_attempts=1),
                escalate=False,
            ),
        ),
    )


SAMPLE_INPUT: dict[str, Any] = {
    "message_id": "demo-0001",
    "sender": "certs@example-vendor.example",
    "subject": "COI for Example Plumbing LLC",
    "attachment_text": (
        "CERTIFICATE OF LIABILITY INSURANCE (ACORD 25) DATE 11/20/2026\n"
        "INSURED: Example Plumbing LLC, 100 Sample St, Los Angeles CA\n"
        "INSURER A: Example Mutual Insurance Co  POLICY NUMBER: GL-0000-1234\n"
        "COMMERCIAL GENERAL LIABILITY  EACH OCCURRENCE $ 500,000  GENERAL AGGREGATE $ 1,000,000\n"
        "AUTOMOBILE LIABILITY  COMBINED SINGLE LIMIT $ 1,000,000\n"
        "ADDL INSD: Y   POLICY EXP 06/30/2027"
    ),
    "requirements": {
        "gl_each_occurrence_min": 1_000_000,
        "auto_combined_single_min": 1_000_000,
        "additional_insured_required": True,
        "must_be_valid_on": "2026-12-31",
    },
}


def demo_responder(request: LLMRequest) -> str:
    """Scripted mock answers: the small model is unsure, the 70B model is not."""
    if "8B" in request.model:
        return json.dumps(
            {
                "insured": "Example Plumbing LLC",
                "carrier": "Example Mutual Insurance Co",
                "policy_number": "GL-0000-1234",
                "gl_each_occurrence": None,
                "auto_combined_single": 1_000_000,
                "additional_insured": True,
                "expires": "2027-06-30",
                "confidence": 0.55,
            }
        )
    return json.dumps(
        {
            "insured": "Example Plumbing LLC",
            "carrier": "Example Mutual Insurance Co",
            "policy_number": "GL-0000-1234",
            "gl_each_occurrence": 500_000,
            "auto_combined_single": 1_000_000,
            "additional_insured": True,
            "expires": "2027-06-30",
            "confidence": 0.93,
        }
    )
