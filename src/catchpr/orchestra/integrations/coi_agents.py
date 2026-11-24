"""Wire the COI back-office team (``coi_agents``) into catchpr.

One run = one certificate. Intake (folder or mailbox -> documents) stays a
batch step you run first with ``coi_agents.agents.intake.run``; each
``CertificateDocument`` it returns becomes the input of one catchpr run.

    extractor -> checker -> reviewer

* Every model call the extractor makes goes through :class:`CatchprCoiProvider`,
  so it is metered against the run budget, written to the audit log, and
  replayable. Text calls use the current tier's model, so escalation moves to
  a bigger model. Vision calls (scanned pages) keep the extractor's own vision
  model, which must be in the price table.
* The team's extractor keeps an on-disk response cache (under
  ``COI_AGENTS_VAR``). A cache hit makes no model call, so it is not metered
  or audited as an ``llm_call``; the extractor output still lists the original
  usage in ``llm_calls``. Point ``COI_AGENTS_VAR`` at a fresh directory when
  you want every call metered.
* The extractor may shell out to Tesseract for OCR, so that step allows
  subprocesses. Network stays blocked: direct HTTP from the team code fails.
* Confidence routing: an extraction that still has validator flags after the
  model cross-check scores 0.5, so it escalates to the next tier and then lands
  in catchpr's human review queue. A reviewer decision of ``human_review``
  also lands there, with the reviewer output attached as the candidate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from catchpr.orchestra.agents import adapt, current_context
from catchpr.orchestra.pipeline import Handoff, Pipeline, RetryPolicy, Step


class CatchprCoiProvider:
    """Implements ``coi_agents.providers.base.LLMProvider`` on top of ``ctx.llm``."""

    def __init__(self, text_model_policy: Literal["tier", "agent"] = "tier") -> None:
        self.text_model_policy = text_model_policy

    @property
    def name(self) -> str:
        try:
            ctx = current_context()
        except Exception:
            return "catchpr"
        # The team caches non-mock responses on disk; never cache simulated ones.
        return "mock" if ctx.provider.name == "mock" else "catchpr"

    def available(self) -> bool:
        try:
            current_context()
        except Exception:
            return False
        return True

    def complete(self, req: Any) -> Any:
        from coi_agents.providers.base import LLMResponse

        ctx = current_context()
        pin = None
        if req.images_png or self.text_model_policy == "agent":
            pin = req.model
        result = ctx.llm.call(
            [("system", req.system), ("user", req.user_text)],
            max_tokens=req.max_tokens,
            temperature=req.temperature,
            json_mode=req.json_mode,
            images_png=tuple(req.images_png),
            model=pin,
        )
        return LLMResponse(
            text=result.text,
            model=result.model,
            # "mock" keeps simulated answers out of the team's on-disk response cache.
            provider="mock" if result.simulated else f"catchpr:{result.provider}",
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=float(result.cost_usd),
            latency_s=round(result.latency_s, 3),
            estimated=result.simulated or not result.usage_reported,
        )


def build(
    *,
    text_model_policy: Literal["tier", "agent"] = "tier",
    llm_policy: Literal["never", "scans_and_flagged", "always"] = "scans_and_flagged",
    start_tier: str = "standard",
    extractor_timeout_s: float = 180.0,
) -> Pipeline:
    from coi_agents.agents import checker
    from coi_agents.agents.extractor import Extractor
    from coi_agents.agents.reviewer import Reviewer
    from coi_agents.schemas import (
        AgencyRequirements,
        CertificateDocument,
        CheckerInput,
        CheckerOutput,
        ExtractorInput,
        ExtractorOutput,
        ReviewerConfig,
        ReviewerInput,
        ReviewerOutput,
    )

    class CoiJob(BaseModel):
        model_config = ConfigDict(extra="forbid")
        document: CertificateDocument
        requirements: AgencyRequirements
        reviewer_config: ReviewerConfig = ReviewerConfig()

    provider = CatchprCoiProvider(text_model_policy)

    def extract(inp: ExtractorInput) -> ExtractorOutput:
        return Extractor(provider).run(inp)

    def to_extractor(h: Handoff) -> ExtractorInput:
        job = h.run_input
        assert isinstance(job, CoiJob)
        return ExtractorInput(document=job.document, llm_policy=llm_policy)

    def to_checker(h: Handoff) -> CheckerInput:
        job, ext = h.run_input, h.outputs["extractor"]
        assert isinstance(job, CoiJob) and isinstance(ext, ExtractorOutput)
        return CheckerInput(certificate=ext.certificate, requirements=job.requirements)

    def to_reviewer(h: Handoff) -> ReviewerInput:
        job, ext, chk = h.run_input, h.outputs["extractor"], h.outputs["checker"]
        assert isinstance(job, CoiJob)
        assert isinstance(ext, ExtractorOutput) and isinstance(chk, CheckerOutput)
        return ReviewerInput(extraction=ext, check=chk, config=job.reviewer_config)

    def extraction_confidence(out: BaseModel) -> float:
        assert isinstance(out, ExtractorOutput)
        return 0.5 if out.validator_flags else 1.0

    def review_confidence(out: BaseModel) -> float:
        assert isinstance(out, ReviewerOutput)
        return 1.0 if out.decision.value == "straight_through" else 0.0

    return Pipeline(
        name="coi-agents",
        input_model=CoiJob,
        version="coi_agents",
        steps=(
            Step(
                "extractor",
                adapt(
                    extract,
                    name="coi_agents.extractor",
                    input_model=ExtractorInput,
                    output_model=ExtractorOutput,
                ),
                bind=to_extractor,
                timeout_s=extractor_timeout_s,
                start_tier=start_tier,
                min_confidence=0.9,
                confidence_of=extraction_confidence,
                allow_subprocess=True,
                volatile_fields=("elapsed_s",),
            ),
            Step(
                "checker",
                adapt(checker.run, name="coi_agents.checker"),
                after=("extractor",),
                bind=to_checker,
                retry=RetryPolicy(max_attempts=1),
                escalate=False,
                on_exhausted="fail",
            ),
            Step(
                "reviewer",
                adapt(Reviewer(None).run, name="coi_agents.reviewer"),
                after=("extractor", "checker"),
                bind=to_reviewer,
                retry=RetryPolicy(max_attempts=1),
                escalate=False,
                min_confidence=0.5,
                confidence_of=review_confidence,
            ),
        ),
    )


def job_from_pdf(pdf: Path, requirements_json: Path) -> dict[str, Any]:
    """Convenience: build one CoiJob from a PDF path via the team's intake agent."""
    import json

    from coi_agents.agents.intake import run as intake_run
    from coi_agents.schemas import FolderSource, IntakeInput

    out = intake_run(IntakeInput(source=FolderSource(path=str(pdf.parent))))
    docs = [d for d in out.documents if d.origin.original_name == pdf.name]
    if not docs:
        raise ValueError(f"intake did not accept {pdf}: {out.skipped}")
    return {
        "document": docs[0].model_dump(mode="json"),
        "requirements": json.loads(requirements_json.read_text(encoding="utf-8")),
    }
