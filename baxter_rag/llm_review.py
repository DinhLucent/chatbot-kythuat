from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .llm import OpenAICompatibleLLM, env_flag
from .settings import Settings


RiskLevel = Literal["low", "medium", "high", "critical"]
ReviewDecision = Literal[
    "keep",
    "propose_patch",
    "remove_noise",
    "split",
    "merge",
    "needs_ocr",
    "needs_parser_retry",
    "needs_human_review",
    "reject",
]
SafeResponseAction = Literal["allow", "revise", "extractive_fallback", "clarify", "no_source", "block"]
FailureRootCause = Literal[
    "none",
    "source_missing",
    "conversion_loss",
    "normalization_noise",
    "metadata_gap",
    "retrieval_miss",
    "context_insufficient",
    "llm_drift",
    "citation_error",
    "policy_block",
    "eval_gap",
]


class EvidenceSpan(BaseModel):
    source_id: str = Field(description="Chunk id, citation id, page id, or other source identifier.")
    citation_id: str | None = Field(None, description="Citation id when available.")
    quote: str = Field(description="Exact source text span supporting the judgment. Empty only when evidence is absent.")
    line_start: int | None = Field(None, description="Starting line when available.")
    line_end: int | None = Field(None, description="Ending line when available.")


class ProposedAction(BaseModel):
    action_type: str = Field(description="Machine-readable action such as ocr_retry, parser_retry, human_review_patch, safe_patch_candidate, answer_revision, or eval_case.")
    target_id: str | None = Field(None, description="Chunk id, doc id, citation id, or answer id affected by the action.")
    description: str = Field(description="Short action summary.")
    risk_level: RiskLevel = Field(description="Risk level of the action.")


class SemanticEdit(BaseModel):
    type: Literal["replace", "remove", "add_note", "split", "merge"] = Field(description="Edit type.")
    before: str = Field(description="Exact text span from the supplied evidence when applicable.")
    after: str = Field(description="Proposed replacement or resulting text.")
    evidence_span: str = Field(description="Exact supporting source span from the supplied evidence.")
    risk: RiskLevel = Field(description="Risk of this edit.")


class SemanticMarkdownProposal(BaseModel):
    task_name: str = Field(description="Task name, normally semantic_markdown_review_propose_patch.")
    source_ids: list[str] = Field(description="Source ids used by the proposal.")
    evidence_spans: list[EvidenceSpan] = Field(description="Exact supporting spans from the supplied evidence.")
    decision: ReviewDecision = Field(description="Structured review decision.")
    confidence: float = Field(ge=0.0, le=1.0, description="Model confidence from 0 to 1.")
    risk_level: RiskLevel = Field(description="Overall risk level.")
    reasoning_summary: str = Field(description="Short explanation; do not include hidden chain of thought.")
    proposed_actions: list[ProposedAction] = Field(description="Actions proposed by the reviewer.")
    blocked_reason: str | None = Field(None, description="Reason the model cannot safely propose a patch.")
    requires_human_review: bool = Field(description="True for service, safety, alarm, maintenance, calibration, uncertain, or risky edits.")
    proposal_type: str = Field(description="Type of proposal, e.g. markdown_cleanup, ocr_retry, parser_retry, or no_change.")
    before_reference: str = Field(description="Reference to the before source span or candidate id.")
    after_preview: str = Field(description="Proposed after preview, empty if no patch is proposed.")
    citation_impact: str = Field(description="How the proposal affects citations.")
    rollback_note: str = Field(description="How to reverse or ignore this proposal.")
    verdict: ReviewDecision = Field(description="Legacy-compatible verdict for quality loop action planning.")
    rationale: str = Field(description="Short Vietnamese or English rationale.")
    proposed_markdown: str = Field(description="Proposed Markdown, empty unless evidence supports a patch.")
    edits: list[SemanticEdit] = Field(description="Evidence-backed edits.")
    cannot_fix_reason: str = Field(description="Reason no fix can be proposed, empty otherwise.")


class GroundedClaim(BaseModel):
    claim: str = Field(description="Answer claim.")
    supporting_source_ids: list[str] = Field(description="Chunk or citation ids that support the claim.")
    support_level: Literal["direct", "partial", "unsupported"] = Field(description="Support level from retrieved evidence.")


class CitationError(BaseModel):
    citation_id: str | None = Field(None, description="Citation id or chunk id with a problem.")
    error_type: Literal["missing", "wrong_source", "unsupported_quote", "weak_support"] = Field(description="Citation problem type.")
    detail: str = Field(description="Short description of the error.")


class AnswerGroundingReview(BaseModel):
    task_name: str = Field(description="Task name, normally answer_grounding_review.")
    source_ids: list[str] = Field(description="Source ids supplied to the reviewer.")
    evidence_spans: list[EvidenceSpan] = Field(description="Exact source spans used for judgment.")
    decision: Literal["supported", "partially_supported", "unsupported", "insufficient_context", "policy_block"] = Field(description="Overall grounding decision.")
    confidence: float = Field(ge=0.0, le=1.0, description="Model confidence from 0 to 1.")
    risk_level: RiskLevel = Field(description="Risk level of the answer.")
    reasoning_summary: str = Field(description="Short explanation; do not include hidden chain of thought.")
    proposed_actions: list[ProposedAction] = Field(description="Actions to make the answer safe.")
    blocked_reason: str | None = Field(None, description="Reason answer should be blocked or replaced.")
    requires_human_review: bool = Field(description="True when high-risk or citation support is uncertain.")
    grounded_claims: list[GroundedClaim] = Field(description="Claims and support levels.")
    unsupported_claims: list[str] = Field(description="Claims not directly supported by retrieved evidence.")
    citation_errors: list[CitationError] = Field(description="Citation support problems.")
    missing_context: list[str] = Field(description="Missing evidence needed to answer safely.")
    safe_response_action: SafeResponseAction = Field(description="Action the app should take.")
    root_cause: FailureRootCause = Field(description="Likely root cause if the answer is unsafe.")


class ChunkEnrichment(BaseModel):
    block_type: str = Field(description="One block type from the Baxter block taxonomy.")
    context_summary: str = Field(description="Short retrieval-oriented summary grounded only in the chunk text.")
    semantic_keywords: list[str] = Field(description="Technical keywords found in or directly implied by the chunk text.")
    source_ids: list[str] = Field(description="Chunk ids or doc ids reviewed.")
    risk_level: RiskLevel = Field(description="Risk level for using this enrichment as retrieval metadata.")
    requires_human_review: bool = Field(description="True when enrichment touches service, safety, alarm, maintenance, or calibration meaning.")


def llm_audit_dir(settings: Settings, *parts: str) -> Path:
    return settings.logs_dir.joinpath("llm_calls", *parts)


def review_audit_dir(settings: Settings, *parts: str) -> Path:
    return settings.artifacts_dir.joinpath("reviews", "llm_calls", *parts)


def configured_llm(provider_name: str = "deepseek") -> OpenAICompatibleLLM:
    return OpenAICompatibleLLM(provider_name=provider_name)


def configured_apodex_llm() -> OpenAICompatibleLLM:
    return OpenAICompatibleLLM(
        base_url=os.getenv("APODEX_BASE_URL", "").strip(),
        api_key=os.getenv("APODEX_API_KEY", "").strip(),
        model=os.getenv("APODEX_MODEL", "apodex/Apodex-1.0-mini"),
        timeout=float(os.getenv("APODEX_TIMEOUT_SECONDS", "60") or "60"),
        provider_name="apodex_openai_compatible",
    )


def llm_stage_enabled(option_value: bool | None, env_name: str) -> bool:
    if option_value is not None:
        return bool(option_value)
    return env_flag(env_name, False)


def answer_qa_enabled() -> bool:
    return env_flag("LLM_ANSWER_QA_ENABLED", False)
