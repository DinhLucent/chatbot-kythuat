from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import requests

from .llm import OpenAICompatibleLLM, source_upload_allowed
from .llm_review import SemanticMarkdownProposal, configured_apodex_llm, review_audit_dir
from .settings import Settings
from .utils import clamp_text, mojibake_score, read_json, short_hash, utc_now, write_json, write_jsonl


@dataclass(frozen=True)
class SemanticReviewOptions:
    doc_id: str | None = None
    limit: int = 12
    enable_llm_review: bool | None = None
    enable_apodex: bool | None = None
    allow_external: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_apodex_semantic_review(settings: Settings, options: SemanticReviewOptions | None = None) -> dict[str, Any]:
    options = options or SemanticReviewOptions()
    settings.ensure_dirs()
    run_id = build_review_id()
    out_dir = settings.artifacts_dir / "reviews" / "apodex_semantic" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    candidates = select_semantic_review_candidates(settings, doc_id=options.doc_id, limit=options.limit)
    requests_payload = [build_review_request(candidate) for candidate in candidates]
    write_jsonl(out_dir / "requests.jsonl", requests_payload)

    provider = select_semantic_review_provider(options)
    enabled = bool(provider.get("enabled"))
    allow_external = bool(provider.get("allow_external"))
    llm = provider.get("llm")
    base_url = str(provider.get("base_url") or "")
    api_key = str(provider.get("api_key") or "")
    model = str(provider.get("model") or "")
    proposals: list[dict[str, Any]] = []

    status = "skipped"
    reason = ""
    if not candidates:
        reason = "no_candidates"
    elif not enabled:
        reason = "semantic_review_llm_disabled"
    elif not isinstance(llm, OpenAICompatibleLLM):
        status = "warning"
        reason = "llm_provider_unavailable"
    elif not source_upload_allowed(llm, allow_external):
        reason = "external_llm_not_allowed"
    elif not llm.base_url:
        status = "warning"
        reason = "llm_base_url_missing"
    elif not llm.api_key:
        status = "warning"
        reason = "llm_api_key_missing"
    else:
        status = "completed"
        reason = "model_review_completed"
        for payload in requests_payload:
            proposals.append(call_structured_semantic_review(settings, llm, payload, provider))
            time.sleep(float(os.getenv("LLM_REVIEW_SLEEP_SECONDS", os.getenv("APODEX_REVIEW_SLEEP_SECONDS", "0")) or "0"))

    if not proposals:
        proposals = [dry_run_proposal(payload, reason) for payload in requests_payload]
    write_jsonl(out_dir / "proposals.jsonl", proposals)

    summary = summarize_proposals(proposals)
    report = {
        "schema_version": "baxter_apodex_semantic_review_v1",
        "generated_at": utc_now(),
        "run_id": run_id,
        "status": status,
        "reason": reason,
        "root": str(settings.root),
        "options": options.to_dict(),
        "provider": provider.get("provider"),
        "model": model,
        "base_url": base_url,
        "external_upload_allowed": allow_external,
        "raw_documents_sent_to_model": False,
        "candidate_count": len(candidates),
        "request_path": str(out_dir / "requests.jsonl"),
        "proposal_path": str(out_dir / "proposals.jsonl"),
        "summary": summary,
        "important_rule": "Proposals are not applied automatically. Only evidence-backed edits should be promoted into normalized artifacts after human review.",
    }
    write_json(out_dir / "report.json", report)
    write_json(settings.artifacts_dir / "reviews" / "apodex_semantic_latest.json", report)
    write_json(settings.artifacts_dir / "reviews" / "llm_semantic_latest.json", report)
    write_markdown_report(out_dir / "report.md", report, candidates, proposals)
    return {**report, "report_markdown": str(out_dir / "report.md")}


def select_semantic_review_candidates(settings: Settings, *, doc_id: str | None, limit: int) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    normalized_root = settings.artifacts_dir / "normalized"
    for chunk_path in sorted(normalized_root.glob("*/chunk_plan.json")):
        payload = read_json(chunk_path, {})
        chunks = payload.get("chunks") or []
        if not chunks:
            continue
        current_doc_id = str(chunks[0].get("doc_id") or chunk_path.parent.name)
        if doc_id and current_doc_id != doc_id:
            continue
        citation_map = load_citation_by_chunk(chunk_path.parent)
        quality = read_json(chunk_path.parent / "quality_report.json", {})
        for index, chunk in enumerate(chunks):
            if not isinstance(chunk, dict) or chunk.get("low_value"):
                continue
            candidate = score_chunk_for_review(chunk, chunks, index, citation_map, quality)
            if candidate:
                candidates.append(candidate)
    candidates.sort(key=lambda item: (item["priority"], item["risk_score"]), reverse=True)
    return candidates[: max(0, limit)]


def score_chunk_for_review(
    chunk: dict[str, Any],
    chunks: list[dict[str, Any]],
    index: int,
    citation_map: dict[str, dict[str, Any]],
    quality: dict[str, Any],
) -> dict[str, Any] | None:
    text = str(chunk.get("content") or "")
    reasons: list[str] = []
    risk_score = 0
    has_structure_refs = bool(chunk.get("table_refs") or chunk.get("image_refs"))
    if "empty_or_ocr_required" in text:
        reasons.append("empty_or_ocr_required")
        risk_score += 80
    if re.search(r"\.{8,}", text):
        reasons.append("toc_or_dot_leader_noise")
        risk_score += 16
    if len(text) > 2200 and not has_structure_refs and chunk.get("block_type") in {"service_step", "warning_safety", "alarm_code"}:
        reasons.append("long_high_value_chunk")
        risk_score += 12
    if repeated_line_ratio(text) > 0.18 and len(text) > 600 and not has_structure_refs and not repeated_callout_label(text):
        reasons.append("repeated_lines")
        risk_score += 18
    if re.search(r"\b(?:understa nd|ve rsion|ti me|de scribed|furnish)\b", text, flags=re.IGNORECASE):
        reasons.append("word_spacing_artifact")
        risk_score += 20
    if chunk.get("block_type") in {"alarm_code", "warning_safety", "service_step", "calibration_step"}:
        risk_score += 4
    failed_gates = [
        key
        for key, value in (quality.get("quality_gates") if isinstance(quality.get("quality_gates"), dict) else {}).items()
        if value is False
    ]
    if failed_gates:
        reasons.append("doc_quality_gate_failed")
        risk_score += 10
    if risk_score < 16:
        return None
    before = chunks[index - 1].get("content") if index > 0 and isinstance(chunks[index - 1], dict) else ""
    after = chunks[index + 1].get("content") if index + 1 < len(chunks) and isinstance(chunks[index + 1], dict) else ""
    citation = citation_map.get(str(chunk.get("chunk_id"))) or {}
    priority = 2
    if "empty_or_ocr_required" in reasons:
        priority = 4
    elif chunk.get("block_type") in {"warning_safety", "alarm_code", "service_step"}:
        priority = 3
    return {
        "candidate_id": f"sem_{short_hash(str(chunk.get('chunk_id')) + '|'.join(reasons), 12)}",
        "chunk_id": chunk.get("chunk_id"),
        "doc_id": chunk.get("doc_id"),
        "model": chunk.get("model"),
        "doc_type": chunk.get("doc_type"),
        "block_type": chunk.get("block_type"),
        "section_path": chunk.get("section_path", []),
        "citation": citation,
        "priority": priority,
        "risk_score": risk_score,
        "reasons": reasons,
        "evidence": {
            "before": clamp_text(str(before), 1200),
            "current": clamp_text(text, 3000),
            "after": clamp_text(str(after), 1200),
        },
    }


def build_review_request(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": candidate["candidate_id"],
        "chunk_id": candidate["chunk_id"],
        "doc_id": candidate["doc_id"],
        "task": "semantic_markdown_review_propose_patch",
        "review_rules": [
            "Return JSON only.",
            "Do not add facts that are not present in evidence.before/current/after.",
            "Preserve alarm codes, model names, part numbers, software versions, numbers, units, warnings, cautions, and step order unless evidence proves an OCR/conversion artifact.",
            "If evidence is missing or pages are empty, return verdict='needs_ocr' and do not invent content.",
            "Use proposed_markdown only for clarity, de-duplication, spacing cleanup, or boilerplate removal supported by evidence.",
            "Set requires_human_review=true for any service/safety/alarm change.",
        ],
        "required_json_schema": {
            "verdict": "keep | propose_patch | remove_noise | split | merge | needs_ocr | reject",
            "confidence": "0.0-1.0",
            "rationale": "short Vietnamese explanation",
            "proposed_markdown": "string or empty",
            "edits": [
                {
                    "type": "replace | remove | add_note | split | merge",
                    "before": "exact text span from evidence when applicable",
                    "after": "proposed text",
                    "evidence_span": "exact supporting text from evidence",
                    "risk": "low | medium | high",
                }
            ],
            "cannot_fix_reason": "string or empty",
            "requires_human_review": "boolean",
        },
        "candidate": candidate,
    }


def select_semantic_review_provider(options: SemanticReviewOptions) -> dict[str, Any]:
    llm_enabled = option_enabled(options.enable_llm_review, "LLM_SEMANTIC_REVIEW_ENABLED")
    apodex_enabled = option_enabled(options.enable_apodex, "APODEX_SEMANTIC_REVIEW_ENABLED")
    if llm_enabled:
        llm = OpenAICompatibleLLM(provider_name="deepseek_openai_compatible")
        return {
            "provider": "deepseek_openai_compatible",
            "enabled": True,
            "allow_external": option_enabled(options.allow_external, "LLM_EXTERNAL_UPLOAD_ALLOWED"),
            "llm": llm,
            "base_url": llm.base_url,
            "api_key": "present" if llm.api_key else "",
            "model": llm.model,
        }
    if apodex_enabled:
        llm = configured_apodex_llm()
        return {
            "provider": "apodex_openai_compatible",
            "enabled": True,
            "allow_external": option_enabled(options.allow_external, "APODEX_ALLOW_EXTERNAL"),
            "llm": llm,
            "base_url": llm.base_url,
            "api_key": "present" if llm.api_key else "",
            "model": llm.model,
        }
    return {
        "provider": "none",
        "enabled": False,
        "allow_external": option_enabled(options.allow_external, "LLM_EXTERNAL_UPLOAD_ALLOWED"),
        "llm": None,
        "base_url": "",
        "api_key": "",
        "model": "",
    }


def call_structured_semantic_review(
    settings: Settings,
    llm: OpenAICompatibleLLM,
    payload: dict[str, Any],
    provider: dict[str, Any],
) -> dict[str, Any]:
    max_tokens = int(os.getenv("LLM_REVIEW_MAX_TOKENS", os.getenv("APODEX_REVIEW_MAX_TOKENS", "2048")) or "2048")
    result: dict[str, Any] = {
        "candidate_id": payload.get("candidate_id"),
        "chunk_id": payload.get("chunk_id"),
        "doc_id": payload.get("doc_id"),
        "provider": provider.get("provider"),
        "model": llm.model,
        "ok": False,
        "created_at": utc_now(),
    }
    try:
        review: SemanticMarkdownProposal = llm.complete_structured(
            [
                {
                    "role": "system",
                    "content": (
                        "You are a verification-first technical manual reviewer. "
                        "Return a schema-bound proposal only when every edit is supported by supplied evidence. "
                        "Never invent missing page text, procedure steps, alarms, part numbers, units, or service guidance."
                    ),
                },
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            SemanticMarkdownProposal,
            temperature=0,
            max_tokens=max_tokens,
            audit_dir=review_audit_dir(settings, "semantic_review"),
            task_name="semantic_markdown_review_propose_patch",
            source_ids=[str(payload.get("doc_id") or ""), str(payload.get("chunk_id") or "")],
            metadata={"candidate_id": payload.get("candidate_id"), "provider": provider.get("provider")},
        )
        proposal = enforce_proposal_guardrails(review.model_dump(), payload)
        result.update({"ok": True, "proposal": proposal})
    except Exception as exc:
        result.update(
            {
                "ok": False,
                "error": str(exc)[:1000],
                "proposal": dry_run_proposal(payload, f"structured_llm_error: {str(exc)[:300]}")["proposal"],
            }
        )
    return result


def call_apodex_review(base_url: str, api_key: str, model: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/chat/completions"
    timeout = int(os.getenv("APODEX_TIMEOUT_SECONDS", "60") or "60")
    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a verification-first technical manual reviewer. "
                        "You propose Markdown repair patches only when every edit is supported by supplied evidence."
                    ),
                },
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "temperature": 0,
            "max_tokens": int(os.getenv("APODEX_REVIEW_MAX_TOKENS", "2048") or "2048"),
        },
        timeout=timeout,
    )
    result: dict[str, Any] = {
        "candidate_id": payload.get("candidate_id"),
        "chunk_id": payload.get("chunk_id"),
        "doc_id": payload.get("doc_id"),
        "status_code": response.status_code,
        "ok": response.ok,
        "created_at": utc_now(),
    }
    try:
        data = response.json()
        content = str(data.get("choices", [{}])[0].get("message", {}).get("content") or "")
    except Exception:
        content = response.text
    result["raw_response"] = content[:6000]
    result["proposal"] = parse_json_object(content)
    if not result["proposal"]:
        result["ok"] = False
        result["error"] = "invalid_json_response"
    else:
        result["proposal"] = enforce_proposal_guardrails(result["proposal"], payload)
    return result


def dry_run_proposal(payload: dict[str, Any], reason: str) -> dict[str, Any]:
    candidate = payload.get("candidate", {}) if isinstance(payload.get("candidate"), dict) else {}
    verdict = "needs_human_review"
    cannot_fix = reason
    if "empty_or_ocr_required" in candidate.get("reasons", []):
        verdict = "needs_ocr"
        cannot_fix = "Converted Markdown contains empty pages. A text-only validator must not invent missing page content."
    return {
        "candidate_id": payload.get("candidate_id"),
        "chunk_id": payload.get("chunk_id"),
        "doc_id": payload.get("doc_id"),
        "ok": False,
        "created_at": utc_now(),
        "dry_run": True,
        "proposal": {
            "task_name": "semantic_markdown_review_propose_patch",
            "source_ids": [str(payload.get("doc_id") or ""), str(payload.get("chunk_id") or "")],
            "evidence_spans": [],
            "decision": verdict,
            "verdict": verdict,
            "confidence": 0,
            "risk_level": "high",
            "reasoning_summary": "Semantic review was not executed; request payload was saved for later model review.",
            "proposed_actions": [],
            "blocked_reason": cannot_fix,
            "proposal_type": "dry_run",
            "before_reference": str(payload.get("candidate_id") or ""),
            "after_preview": "",
            "citation_impact": "none",
            "rollback_note": "Ignore this dry-run proposal or rerun with an allowed configured model.",
            "rationale": "Apodex semantic review was not executed; request payload was saved for later model review.",
            "proposed_markdown": "",
            "edits": [],
            "cannot_fix_reason": cannot_fix,
            "requires_human_review": True,
        },
    }


def summarize_proposals(proposals: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    human_review = 0
    guardrails: dict[str, int] = {}
    for item in proposals:
        proposal = item.get("proposal") if isinstance(item.get("proposal"), dict) else {}
        verdict = str(proposal.get("verdict") or "unknown")
        counts[verdict] = counts.get(verdict, 0) + 1
        if proposal.get("requires_human_review"):
            human_review += 1
        for flag in proposal.get("guardrail_flags", []) if isinstance(proposal.get("guardrail_flags"), list) else []:
            guardrails[str(flag)] = guardrails.get(str(flag), 0) + 1
    return {
        "proposal_count": len(proposals),
        "verdict_counts": counts,
        "requires_human_review": human_review,
        "guardrail_flags": guardrails,
    }


def enforce_proposal_guardrails(proposal: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    candidate = payload.get("candidate") if isinstance(payload.get("candidate"), dict) else {}
    evidence = candidate.get("evidence") if isinstance(candidate.get("evidence"), dict) else {}
    evidence_blob = "\n".join(str(evidence.get(key) or "") for key in ("before", "current", "after"))
    block_type = str(candidate.get("block_type") or "")
    flags: list[str] = []
    high_risk_blocks = {"alarm_code", "warning_safety", "service_step", "maintenance_step", "calibration_step"}

    if block_type in high_risk_blocks and proposal.get("verdict") not in {"keep", "needs_ocr", "reject"}:
        proposal["requires_human_review"] = True
        flags.append("forced_human_review_high_risk_block")

    edits = proposal.get("edits") if isinstance(proposal.get("edits"), list) else []
    for edit in edits:
        if not isinstance(edit, dict):
            flags.append("invalid_edit_shape")
            proposal["requires_human_review"] = True
            continue
        evidence_span = str(edit.get("evidence_span") or "").strip()
        before = str(edit.get("before") or "").strip()
        after = str(edit.get("after") or "").strip()
        if evidence_span and evidence_span not in evidence_blob:
            flags.append("evidence_span_not_exact")
            proposal["requires_human_review"] = True
            edit["risk"] = max_risk(str(edit.get("risk") or "medium"), "high")
        if before and before not in evidence_blob:
            flags.append("before_span_not_exact")
            proposal["requires_human_review"] = True
            edit["risk"] = max_risk(str(edit.get("risk") or "medium"), "high")
        if mojibake_score(after) > 0:
            flags.append("proposed_text_has_encoding_artifact")
            proposal["requires_human_review"] = True
            edit["risk"] = max_risk(str(edit.get("risk") or "medium"), "medium")

    proposed = str(proposal.get("proposed_markdown") or "")
    if proposed and mojibake_score(proposed) > 0:
        flags.append("proposed_markdown_has_encoding_artifact")
        proposal["requires_human_review"] = True
    if proposal.get("verdict") == "propose_patch" and not edits:
        flags.append("patch_without_edits")
        proposal["requires_human_review"] = True

    existing_flags = proposal.get("guardrail_flags") if isinstance(proposal.get("guardrail_flags"), list) else []
    proposal["guardrail_flags"] = sorted({*(str(flag) for flag in existing_flags), *flags})
    proposal["guardrail_status"] = "needs_human_review" if proposal.get("requires_human_review") else "machine_check_passed"
    return proposal


def max_risk(current: str, minimum: str) -> str:
    order = {"low": 1, "medium": 2, "high": 3, "critical": 4}
    current_key = current if current in order else "medium"
    minimum_key = minimum if minimum in order else "medium"
    return current_key if order[current_key] >= order[minimum_key] else minimum_key


def write_markdown_report(path: Path, report: dict[str, Any], candidates: list[dict[str, Any]], proposals: list[dict[str, Any]]) -> None:
    lines = [
        "# LLM Semantic Review",
        "",
        f"- Run id: `{report['run_id']}`",
        f"- Status: `{report['status']}`",
        f"- Reason: `{report['reason']}`",
        f"- Provider: `{report.get('provider')}`",
        f"- Candidates: `{report['candidate_count']}`",
        f"- Raw documents sent to model: `{report['raw_documents_sent_to_model']}`",
        "",
        "## Summary",
        "",
        json.dumps(report["summary"], ensure_ascii=False, indent=2),
        "",
        "## Candidate Preview",
        "",
    ]
    proposal_by_candidate = {item.get("candidate_id"): item for item in proposals}
    for candidate in candidates[:20]:
        proposal = proposal_by_candidate.get(candidate["candidate_id"], {}).get("proposal", {})
        lines.extend(
            [
                f"### {candidate['candidate_id']}",
                "",
                f"- Chunk: `{candidate.get('chunk_id')}`",
                f"- Doc: `{candidate.get('doc_id')}`",
                f"- Reasons: `{', '.join(candidate.get('reasons', []))}`",
                f"- Verdict: `{proposal.get('verdict', 'unknown')}`",
                f"- Human review: `{proposal.get('requires_human_review', True)}`",
                "",
            ]
        )
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def load_citation_by_chunk(doc_dir: Path) -> dict[str, dict[str, Any]]:
    payload = read_json(doc_dir / "citation_map.json", {})
    return {
        str(item.get("chunk_id")): item
        for item in payload.get("citations", [])
        if isinstance(item, dict) and item.get("chunk_id")
    }


def repeated_line_ratio(text: str) -> float:
    lines = [line.strip() for line in text.splitlines() if len(line.strip()) >= 8]
    if not lines:
        return 0.0
    return 1.0 - (len(set(lines)) / len(lines))


def repeated_callout_label(text: str) -> bool:
    lines = [re.sub(r"\s+", " ", line).strip(" !:.-").upper() for line in text.splitlines()]
    lines = [line for line in lines if line]
    labels = {"WARNING", "CAUTION", "NOTE", "NOTICE", "DANGER", "IMPORTANT"}
    counts: dict[str, int] = {}
    for line in lines:
        counts[line] = counts.get(line, 0) + 1
    repeated_total = sum(count - 1 for count in counts.values() if count > 1)
    repeated_labels = sum(count - 1 for line, count in counts.items() if line in labels and count > 1)
    return repeated_labels > 0 and repeated_labels == repeated_total


def parse_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if not text:
        return {}
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            return {}
        try:
            data = json.loads(match.group(0))
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}


def build_review_id() -> str:
    stamp = utc_now().replace(":", "").replace("-", "").replace("+0000", "Z")
    return f"review_{stamp}_{short_hash(stamp, 6)}"


def option_enabled(value: bool | None, env_name: str) -> bool:
    if value is not None:
        return bool(value)
    raw = os.getenv(env_name)
    if raw is None or not raw.strip():
        return False
    return raw.strip().lower() not in {"0", "false", "no", "off"}
