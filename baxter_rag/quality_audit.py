from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .data_workbench import find_promoted_conversion_overlay
from .deduplication import build_duplicate_policy
from .metadata import infer_doc_code_from_record
from .settings import Settings
from .utils import iter_jsonl, mojibake_score, normalized_text, read_json, short_hash, utc_now, write_json, write_jsonl


HIGH_RISK_BLOCKS = {"alarm_code", "warning_safety", "service_step", "maintenance_step", "calibration_step"}
TECHNICAL_DOC_TYPES = {"service_manual", "operator_manual", "service_bulletin", "technical_document", "checklist"}


@dataclass(frozen=True)
class QualityAuditOptions:
    doc_id: str | None = None
    include_low_risk: bool = True
    max_chunk_issues_per_doc: int = 40
    strict: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_quality_audit(settings: Settings, options: QualityAuditOptions | None = None) -> dict[str, Any]:
    options = options or QualityAuditOptions()
    settings.ensure_dirs()
    run_id = build_audit_id()
    run_dir = settings.artifacts_dir / "quality_audit" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    manifest = read_json(settings.artifacts_dir / "phase0" / "source_manifest.json", {})
    readiness = read_json(settings.artifacts_dir / "phase0" / "data_readiness_report.json", {})
    index_meta = read_json(settings.artifacts_dir / "index" / "index_meta.json", {})
    records = [item for item in manifest.get("records", []) if isinstance(item, dict)]
    if options.doc_id:
        records = [item for item in records if item.get("doc_id") == options.doc_id]

    issues: list[dict[str, Any]] = []
    seen_issue_keys: set[str] = set()

    def add_issue(issue: dict[str, Any]) -> None:
        if options.doc_id and issue.get("doc_id") not in {None, options.doc_id}:
            return
        if not options.include_low_risk and issue.get("severity") == "low":
            return
        key = "|".join(
            str(issue.get(part) or "")
            for part in ("rule_id", "doc_id", "chunk_id", "page_start", "action_type", "evidence_hash")
        )
        if key in seen_issue_keys:
            return
        seen_issue_keys.add(key)
        issue.setdefault("schema_version", "baxter_quality_audit_issue_v1")
        issue.setdefault("created_at", utc_now())
        issue.setdefault("issue_id", f"issue_{short_hash(key, 14)}")
        issue.setdefault("requires_human_review", True)
        issue.setdefault("can_auto_apply", False)
        issue.setdefault("risk", issue.get("severity", "medium"))
        issues.append(issue)

    audit_readiness(settings, readiness, manifest, add_issue)
    for record in records:
        audit_document(settings, record, add_issue, max_chunk_issues=options.max_chunk_issues_per_doc)
    audit_index(settings, index_meta, records, add_issue)

    queues = build_issue_queues(issues)
    scorecard = build_scorecard(settings, run_id, options, records, issues, queues, index_meta)
    report = {
        "schema_version": "baxter_quality_audit_report_v1",
        "generated_at": utc_now(),
        "run_id": run_id,
        "status": "completed",
        "root": str(settings.root),
        "run_dir": str(run_dir),
        "options": options.to_dict(),
        "scorecard": scorecard,
        "issues_path": str(run_dir / "issues.jsonl"),
        "queues_path": str(run_dir / "queues.json"),
        "scorecard_path": str(run_dir / "scorecard.json"),
        "report_markdown": str(run_dir / "report.md"),
        "rules_contract": {
            "non_destructive": True,
            "raw_source_mutation": False,
            "canonical_markdown_mutation": False,
            "promotion_gate_required": True,
            "llm_can_only_propose": True,
        },
    }

    write_jsonl(run_dir / "issues.jsonl", issues)
    write_json(run_dir / "queues.json", queues)
    write_json(run_dir / "scorecard.json", scorecard)
    write_json(run_dir / "report.json", report)
    write_json(settings.artifacts_dir / "quality_audit" / "latest_report.json", report)
    write_json(settings.artifacts_dir / "quality_audit" / "latest_scorecard.json", scorecard)
    write_markdown_report(run_dir / "report.md", report, issues)
    return report


def audit_readiness(settings: Settings, readiness: dict[str, Any], manifest: dict[str, Any], add_issue: Any) -> None:
    issues = readiness.get("issues") if isinstance(readiness.get("issues"), dict) else {}
    records = {item.get("doc_id"): item for item in manifest.get("records", []) if isinstance(item, dict)}
    duplicate_policy = build_duplicate_policy(settings)
    resolved_duplicate_groups = {
        tuple(sorted([str(group.get("canonical_doc_id")), *[str(item) for item in group.get("excluded_doc_ids", [])]]))
        for group in duplicate_policy.get("groups", [])
        if isinstance(group, dict)
    }

    for doc_id in issues.get("missing_doc_code", []) if isinstance(issues.get("missing_doc_code"), list) else []:
        record = records.get(doc_id, {})
        inferred = infer_doc_code_from_record(record)
        if inferred:
            continue
        add_issue(
            issue_payload(
                rule_id="metadata.doc_code.missing",
                category="add",
                action_type="metadata_enrichment",
                severity="medium",
                doc_id=doc_id,
                source_path=record.get("source_path"),
                evidence={
                    "file_name": record.get("file_name"),
                    "title": record.get("title"),
                    "keyword_codes": (record.get("extra") or {}).get("keyword_codes", []),
                },
                proposed_action="Extract or confirm doc_code from filename, title, first pages, or package metadata.",
                requires_human_review=True,
            )
        )

    for doc_id in issues.get("weak_embedded_text", []) if isinstance(issues.get("weak_embedded_text"), list) else []:
        record = records.get(doc_id, {})
        overlay = find_promoted_conversion_overlay(settings, str(doc_id))
        if promoted_overlay_resolves_ocr(overlay, require_nonempty=True):
            continue
        add_issue(
            issue_payload(
                rule_id="conversion.embedded_text.weak",
                category="add",
                action_type="ocr_retry",
                severity="critical",
                doc_id=doc_id,
                source_path=record.get("source_path"),
                evidence={"embedded_text_ratio": record.get("embedded_text_ratio"), "page_count": record.get("page_count")},
                proposed_action="Render weak pages and run OCR before normalization; never fill missing text from an LLM.",
                requires_human_review=False,
            )
        )

    groups = issues.get("duplicate_checksum_groups") if isinstance(issues.get("duplicate_checksum_groups"), list) else []
    for group in groups:
        if not isinstance(group, list) or len(group) < 2:
            continue
        if tuple(sorted(str(item) for item in group)) in resolved_duplicate_groups:
            continue
        add_issue(
            issue_payload(
                rule_id="source.duplicate_checksum",
                category="remove",
                action_type="deduplicate_review",
                severity="medium",
                doc_id=str(group[0]),
                evidence={"duplicate_doc_ids": group},
                proposed_action="Choose one canonical active document and mark duplicates excluded from active retrieval.",
                requires_human_review=True,
            )
        )


def promoted_overlay_resolves_ocr(overlay: dict[str, Any] | None, *, require_nonempty: bool) -> bool:
    if not overlay:
        return False
    raw_markdown = Path(str(overlay.get("raw_markdown") or ""))
    if not raw_markdown.exists():
        return False
    text = raw_markdown.read_text(encoding="utf-8", errors="ignore")
    if "empty_or_ocr_required" in text:
        return False
    evidence = overlay.get("evidence") if isinstance(overlay.get("evidence"), dict) else {}
    quality = evidence.get("quality") if isinstance(evidence.get("quality"), dict) else {}
    if require_nonempty:
        return int(quality.get("ocr_nonempty_page_count") or 0) > 0
    return bool(quality)


def audit_document(settings: Settings, record: dict[str, Any], add_issue: Any, *, max_chunk_issues: int) -> None:
    doc_id = str(record.get("doc_id") or "")
    if not doc_id:
        return
    normalized_dir = settings.artifacts_dir / "normalized" / doc_id
    converted_dir = settings.artifacts_dir / "converted" / doc_id
    quality_report = read_json(normalized_dir / "quality_report.json", {})
    chunk_plan = read_json(normalized_dir / "chunk_plan.json", {})
    citation_map = read_json(normalized_dir / "citation_map.json", {})
    artifact_index = read_json(converted_dir / "artifact_index.json", {})
    overlay = find_promoted_conversion_overlay(settings, doc_id)
    raw_md_path = Path(str(overlay.get("raw_markdown"))) if overlay else converted_dir / "raw.md"
    raw_text = raw_md_path.read_text(encoding="utf-8", errors="ignore") if raw_md_path.exists() else ""

    if not quality_report:
        add_issue(
            issue_payload(
                rule_id="normalization.quality_report.missing",
                category="add",
                action_type="normalize_missing",
                severity="high",
                doc_id=doc_id,
                source_path=record.get("source_path"),
                evidence={"normalized_dir": str(normalized_dir)},
                proposed_action="Run conversion and normalization for this document before indexing.",
                requires_human_review=False,
            )
        )
        return

    summary = quality_report.get("summary") if isinstance(quality_report.get("summary"), dict) else {}
    gates = quality_report.get("quality_gates") if isinstance(quality_report.get("quality_gates"), dict) else {}
    warnings = quality_report.get("warnings") if isinstance(quality_report.get("warnings"), list) else []

    for warning in warnings:
        if not isinstance(warning, dict):
            continue
        code = str(warning.get("code") or "unknown")
        if code == "ocr_required":
            if promoted_overlay_resolves_ocr(overlay, require_nonempty=True):
                continue
            add_issue(
                issue_payload(
                    rule_id="normalization.ocr_required",
                    category="add",
                    action_type="ocr_retry",
                    severity="critical",
                    doc_id=doc_id,
                    source_path=record.get("source_path"),
                    evidence={"warning": warning},
                    proposed_action="Run OCR fallback and regenerate normalized artifacts.",
                    requires_human_review=False,
                )
            )
        elif code == "mojibake_risk":
            add_issue(
                issue_payload(
                    rule_id="normalization.encoding_mojibake",
                    category="repair",
                    action_type="semantic_review",
                    severity="high" if str(record.get("language") or "") == "vi" else "medium",
                    doc_id=doc_id,
                    source_path=record.get("source_path"),
                    evidence={"warning": warning, "mojibake_score": summary.get("mojibake_score")},
                    proposed_action="Repair encoding only when source spans match exactly; otherwise rerun converter.",
                    requires_human_review=True,
                )
            )

    for gate, value in gates.items():
        if value is False:
            add_issue(
                issue_payload(
                    rule_id=f"quality_gate.{gate}",
                    category="validate",
                    action_type="gate_failure_review",
                    severity="high",
                    doc_id=doc_id,
                    source_path=record.get("source_path"),
                    evidence={"gate": gate, "value": value},
                    proposed_action="Resolve the failed quality gate before promoting this document to trusted RAG data.",
                    requires_human_review=True,
                )
            )

    chunk_count = int(summary.get("chunk_count") or 0)
    citation_count = int(summary.get("citation_count") or 0)
    if chunk_count and citation_count and chunk_count != citation_count:
        add_issue(
            issue_payload(
                rule_id="citation.count_mismatch",
                category="repair",
                action_type="citation_repair",
                severity="high",
                doc_id=doc_id,
                source_path=record.get("source_path"),
                evidence={"chunk_count": chunk_count, "citation_count": citation_count},
                proposed_action="Regenerate citation_map and verify every active chunk has page/section lineage.",
                requires_human_review=False,
            )
        )

    locator_ratio = float(summary.get("locator_ratio") or 0.0)
    if locator_ratio < 0.98:
        add_issue(
            issue_payload(
                rule_id="citation.locator_ratio.low",
                category="repair",
                action_type="citation_repair",
                severity="high",
                doc_id=doc_id,
                source_path=record.get("source_path"),
                evidence={"locator_ratio": locator_ratio},
                proposed_action="Regenerate source locator map; answers must cite file, page, and section.",
                requires_human_review=False,
            )
        )

    empty_pages = re.findall(r"<!-- page:(\d+) empty_or_ocr_required -->", raw_text)
    if empty_pages:
        add_issue(
            issue_payload(
                rule_id="conversion.empty_pages",
                category="add",
                action_type="ocr_retry",
                severity="critical",
                doc_id=doc_id,
                source_path=record.get("source_path"),
                page_start=int(empty_pages[0]),
                evidence={"empty_pages": [int(page) for page in empty_pages], "empty_page_count": len(empty_pages)},
                proposed_action="OCR the listed pages and rerun normalization; do not synthesize missing content.",
                requires_human_review=False,
            )
        )

    audit_layout_extraction(record, artifact_index, raw_text, chunk_plan, add_issue)
    audit_chunks(record, chunk_plan, citation_map, add_issue, max_chunk_issues=max_chunk_issues)


def audit_layout_extraction(
    record: dict[str, Any],
    artifact_index: dict[str, Any],
    raw_text: str,
    chunk_plan: dict[str, Any],
    add_issue: Any,
) -> None:
    doc_id = str(record.get("doc_id") or "")
    doc_type = str(record.get("doc_type") or "")
    chunks = chunk_plan.get("chunks") if isinstance(chunk_plan.get("chunks"), list) else []
    table_refs = sum(len(chunk.get("table_refs") or []) for chunk in chunks if isinstance(chunk, dict))
    image_refs = sum(len(chunk.get("image_refs") or []) for chunk in chunks if isinstance(chunk, dict))
    mentions_table = bool(re.search(r"\b(table|bảng|bang)\b", raw_text, flags=re.IGNORECASE))
    mentions_figure = bool(re.search(r"\b(fig(?:ure)?|hình|hinh)\b", raw_text, flags=re.IGNORECASE))
    converter = str(artifact_index.get("converter") or "")

    if doc_type in TECHNICAL_DOC_TYPES and mentions_table and table_refs == 0:
        add_issue(
            issue_payload(
                rule_id="layout.table_refs.missing",
                category="add",
                action_type="table_figure_extraction",
                severity="medium",
                doc_id=doc_id,
                source_path=record.get("source_path"),
                evidence={"converter": converter, "mentions_table": True, "table_refs": table_refs},
                proposed_action="Rerun with MinerU/table extraction profile or build table sidecars from page spans.",
                requires_human_review=False,
            )
        )

    if doc_type in {"service_manual", "operator_manual", "service_bulletin"} and mentions_figure and image_refs == 0:
        add_issue(
            issue_payload(
                rule_id="layout.image_refs.missing",
                category="add",
                action_type="table_figure_extraction",
                severity="medium",
                doc_id=doc_id,
                source_path=record.get("source_path"),
                evidence={"converter": converter, "mentions_figure": True, "image_refs": image_refs},
                proposed_action="Extract figures/images and bind captions to page citations.",
                requires_human_review=False,
            )
        )


def audit_chunks(
    record: dict[str, Any],
    chunk_plan: dict[str, Any],
    citation_map: dict[str, Any],
    add_issue: Any,
    *,
    max_chunk_issues: int,
) -> None:
    doc_id = str(record.get("doc_id") or "")
    source_path = record.get("source_path")
    chunks = chunk_plan.get("chunks") if isinstance(chunk_plan.get("chunks"), list) else []
    citations = citation_map.get("citations") if isinstance(citation_map.get("citations"), list) else []
    citation_chunk_ids = {item.get("chunk_id") for item in citations if isinstance(item, dict)}
    per_doc_added = 0

    block_counts = Counter(str(chunk.get("block_type") or "unknown") for chunk in chunks if isinstance(chunk, dict))
    audit_chunk_order(record, chunks, add_issue)
    if block_counts.get("alarm_code", 0) > 0:
        suspect_alarm_count = 0
        for chunk in chunks:
            if not isinstance(chunk, dict) or chunk.get("block_type") != "alarm_code":
                continue
            if chunk.get("low_value"):
                continue
            text = str(chunk.get("content") or "")
            norm = normalized_text(text)
            if not chunk.get("alarm_codes") and not any(term in norm for term in ("alarm", "warning", "caution", "canh bao", "bao dong")):
                suspect_alarm_count += 1
        if suspect_alarm_count:
            add_issue(
                issue_payload(
                    rule_id="chunk.block_type.alarm_suspect",
                    category="repair",
                    action_type="chunk_repair",
                    severity="medium",
                    doc_id=doc_id,
                    source_path=source_path,
                    evidence={"suspect_alarm_chunks": suspect_alarm_count},
                    proposed_action="Review block classifier; do not let copyright/front-matter chunks become alarm_code.",
                    requires_human_review=True,
                )
            )

    for chunk in chunks:
        if per_doc_added >= max_chunk_issues:
            break
        if not isinstance(chunk, dict):
            continue
        chunk_id = str(chunk.get("chunk_id") or "")
        text = str(chunk.get("content") or "")
        block_type = str(chunk.get("block_type") or "prose")
        if not chunk_id:
            continue

        if chunk_id not in citation_chunk_ids:
            add_issue(
                chunk_issue(
                    rule_id="citation.chunk_missing",
                    category="repair",
                    action_type="citation_repair",
                    severity="high",
                    record=record,
                    chunk=chunk,
                    evidence={"chunk_id": chunk_id},
                    proposed_action="Add citation entry or exclude the chunk from retrieval until lineage is restored.",
                    requires_human_review=False,
                )
            )
            per_doc_added += 1
            continue

        if chunk.get("page_start") is None and not chunk.get("low_value"):
            add_issue(
                chunk_issue(
                    rule_id="citation.page_locator.missing",
                    category="repair",
                    action_type="citation_repair",
                    severity="medium",
                    record=record,
                    chunk=chunk,
                    evidence={"line_start": chunk.get("line_start"), "section_path": chunk.get("section_path")},
                    proposed_action="Recover page locator from converter sidecars before using this chunk for citations.",
                    requires_human_review=False,
                )
            )
            per_doc_added += 1
            continue

        if block_type in HIGH_RISK_BLOCKS and len(text) > 3500:
            add_issue(
                chunk_issue(
                    rule_id="chunk.high_risk.too_long",
                    category="repair",
                    action_type="chunk_repair",
                    severity="medium",
                    record=record,
                    chunk=chunk,
                    evidence={"char_count": len(text), "block_type": block_type},
                    proposed_action="Split by procedure step or subsection while preserving order and citation.",
                    requires_human_review=True,
                )
            )
            per_doc_added += 1
            continue

        if mojibake_score(text) > 0 and block_type in HIGH_RISK_BLOCKS:
            add_issue(
                chunk_issue(
                    rule_id="chunk.high_risk.encoding_artifact",
                    category="repair",
                    action_type="semantic_review",
                    severity="high",
                    record=record,
                    chunk=chunk,
                    evidence={"mojibake_score": mojibake_score(text), "block_type": block_type},
                    proposed_action="Repair encoding only with exact evidence spans; service/alarm/safety content needs review.",
                    requires_human_review=True,
                )
            )
            per_doc_added += 1
            continue

        if (
            repeated_line_ratio(text) > 0.18
            and len(text) > 600
            and not chunk.get("table_refs")
            and not chunk.get("image_refs")
            and not repeated_callout_label(text)
        ):
            add_issue(
                chunk_issue(
                    rule_id="chunk.repeated_lines",
                    category="remove",
                    action_type="chunk_repair",
                    severity="low",
                    record=record,
                    chunk=chunk,
                    evidence={"repeated_line_ratio": round(repeated_line_ratio(text), 3)},
                    proposed_action="Remove repeated headers/footers only if exact duplicate lines are verified.",
                    requires_human_review=block_type in HIGH_RISK_BLOCKS,
                )
            )
            per_doc_added += 1


def audit_chunk_order(record: dict[str, Any], chunks: list[Any], add_issue: Any) -> None:
    previous_page: int | None = None
    previous_line: int | None = None
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        page = chunk.get("page_start")
        line = chunk.get("line_start")
        page_int = page if isinstance(page, int) else None
        line_int = line if isinstance(line, int) else None
        if previous_page is not None and page_int is not None and page_int < previous_page:
            add_issue(
                chunk_issue(
                    rule_id="chunk.order.page_regression",
                    category="reorder",
                    action_type="chunk_repair",
                    severity="medium",
                    record=record,
                    chunk=chunk,
                    evidence={"previous_page": previous_page, "current_page": page_int},
                    proposed_action="Reorder chunks by source page/line before indexing or explain intentional parent-context expansion.",
                    requires_human_review=True,
                )
            )
            return
        if previous_line is not None and line_int is not None and line_int < previous_line:
            add_issue(
                chunk_issue(
                    rule_id="chunk.order.line_regression",
                    category="reorder",
                    action_type="chunk_repair",
                    severity="medium",
                    record=record,
                    chunk=chunk,
                    evidence={"previous_line": previous_line, "current_line": line_int},
                    proposed_action="Reorder chunks by normalized source line before indexing.",
                    requires_human_review=True,
                )
            )
            return
        if page_int is not None:
            previous_page = page_int
        if line_int is not None:
            previous_line = line_int


def audit_index(settings: Settings, index_meta: dict[str, Any], records: list[dict[str, Any]], add_issue: Any) -> None:
    chunk_count = int(index_meta.get("chunk_count") or 0)
    if not chunk_count:
        add_issue(
            issue_payload(
                rule_id="index.empty",
                category="add",
                action_type="index_rebuild",
                severity="critical",
                doc_id=None,
                evidence={"index_meta_path": str(settings.artifacts_dir / "index" / "index_meta.json")},
                proposed_action="Rebuild the retrieval index after conversion and normalization.",
                requires_human_review=False,
            )
        )
    active_docs = len([item for item in records if item.get("status") == "active"])
    if active_docs and chunk_count < active_docs:
        add_issue(
            issue_payload(
                rule_id="index.suspiciously_small",
                category="validate",
                action_type="index_rebuild",
                severity="high",
                doc_id=None,
                evidence={"active_docs": active_docs, "index_chunk_count": chunk_count},
                proposed_action="Rebuild index and verify every active normalized document contributes chunks.",
                requires_human_review=False,
            )
        )


def issue_payload(
    *,
    rule_id: str,
    category: str,
    action_type: str,
    severity: str,
    doc_id: str | None,
    evidence: dict[str, Any],
    proposed_action: str,
    source_path: Any = None,
    page_start: int | None = None,
    chunk_id: str | None = None,
    requires_human_review: bool = True,
) -> dict[str, Any]:
    evidence_hash = short_hash(f"{rule_id}:{doc_id}:{chunk_id}:{page_start}:{evidence}", 16)
    return {
        "rule_id": rule_id,
        "category": category,
        "action_type": action_type,
        "severity": severity,
        "risk": severity,
        "doc_id": doc_id,
        "chunk_id": chunk_id,
        "page_start": page_start,
        "source_path": str(source_path or ""),
        "evidence": evidence,
        "evidence_hash": evidence_hash,
        "proposed_action": proposed_action,
        "requires_human_review": requires_human_review,
        "can_auto_apply": False,
    }


def chunk_issue(
    *,
    rule_id: str,
    category: str,
    action_type: str,
    severity: str,
    record: dict[str, Any],
    chunk: dict[str, Any],
    evidence: dict[str, Any],
    proposed_action: str,
    requires_human_review: bool,
) -> dict[str, Any]:
    return issue_payload(
        rule_id=rule_id,
        category=category,
        action_type=action_type,
        severity=severity,
        doc_id=str(record.get("doc_id") or ""),
        chunk_id=str(chunk.get("chunk_id") or ""),
        page_start=chunk.get("page_start") if isinstance(chunk.get("page_start"), int) else None,
        source_path=record.get("source_path"),
        evidence={
            **evidence,
            "block_type": chunk.get("block_type"),
            "section_path": chunk.get("section_path"),
            "line_start": chunk.get("line_start"),
            "line_end": chunk.get("line_end"),
        },
        proposed_action=proposed_action,
        requires_human_review=requires_human_review,
    )


def build_issue_queues(issues: list[dict[str, Any]]) -> dict[str, Any]:
    queues: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for issue in issues:
        action_type = str(issue.get("action_type") or "model_review_needed")
        item = slim_issue(issue)
        queues[action_type].append(item)
        if issue.get("requires_human_review"):
            queues["human_review"].append(item)
    return {
        "generated_at": utc_now(),
        "counts": {key: len(value) for key, value in sorted(queues.items())},
        "queues": dict(sorted(queues.items())),
    }


def build_scorecard(
    settings: Settings,
    run_id: str,
    options: QualityAuditOptions,
    records: list[dict[str, Any]],
    issues: list[dict[str, Any]],
    queues: dict[str, Any],
    index_meta: dict[str, Any],
) -> dict[str, Any]:
    severity_counts = Counter(str(item.get("severity") or "unknown") for item in issues)
    action_counts = Counter(str(item.get("action_type") or "unknown") for item in issues)
    category_counts = Counter(str(item.get("category") or "unknown") for item in issues)
    doc_issue_counts = Counter(str(item.get("doc_id") or "__global__") for item in issues)
    score = quality_score(severity_counts)
    return {
        "schema_version": "baxter_data_quality_scorecard_v1",
        "generated_at": utc_now(),
        "run_id": run_id,
        "root": str(settings.root),
        "doc_scope": options.doc_id or "all",
        "document_count": len(records),
        "documents_with_issues": len([doc for doc in doc_issue_counts if doc != "__global__"]),
        "issue_count": len(issues),
        "quality_score": score,
        "severity_counts": dict(severity_counts),
        "category_counts": dict(category_counts),
        "action_counts": dict(action_counts),
        "queue_counts": queues.get("counts", {}),
        "top_documents": [
            {"doc_id": doc_id, "issue_count": count}
            for doc_id, count in doc_issue_counts.most_common(20)
            if doc_id != "__global__"
        ],
        "index": {
            "chunk_count": index_meta.get("chunk_count"),
            "backend": index_meta.get("backend"),
            "qdrant_enabled": index_meta.get("qdrant_enabled"),
        },
        "promotion_policy": {
            "ocr_retry": "Regenerate artifacts, then re-run audit and eval.",
            "metadata_enrichment": "Can update metadata overlay after exact source evidence exists.",
            "deduplicate_review": "Requires canonical document decision before excluding duplicates.",
            "semantic_review": "LLM proposals only; high-risk content needs human review.",
            "chunk_repair": "Must preserve source order and citation lineage.",
            "table_figure_extraction": "Prefer MinerU/layout sidecars over LLM reconstruction.",
        },
    }


def quality_score(severity_counts: Counter[str]) -> float:
    score = 100.0
    score -= min(45.0, severity_counts.get("critical", 0) * 12.0)
    score -= min(30.0, severity_counts.get("high", 0) * 3.0)
    score -= min(18.0, severity_counts.get("medium", 0) * 0.8)
    score -= min(7.0, severity_counts.get("low", 0) * 0.2)
    return round(max(0.0, score), 2)


def slim_issue(issue: dict[str, Any]) -> dict[str, Any]:
    return {
        "issue_id": issue.get("issue_id"),
        "rule_id": issue.get("rule_id"),
        "doc_id": issue.get("doc_id"),
        "chunk_id": issue.get("chunk_id"),
        "page_start": issue.get("page_start"),
        "severity": issue.get("severity"),
        "risk": issue.get("risk"),
        "requires_human_review": issue.get("requires_human_review"),
        "proposed_action": str(issue.get("proposed_action") or "")[:500],
    }


def repeated_line_ratio(text: str) -> float:
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if len(line) >= 8]
    if not lines:
        return 0.0
    counts = Counter(lines)
    repeated = sum(count - 1 for count in counts.values() if count > 1)
    return repeated / max(1, len(lines))


def repeated_callout_label(text: str) -> bool:
    lines = [re.sub(r"\s+", " ", line).strip(" !:.-").upper() for line in text.splitlines()]
    lines = [line for line in lines if line]
    labels = {"WARNING", "CAUTION", "NOTE", "NOTICE", "DANGER", "IMPORTANT"}
    counts = Counter(lines)
    repeated_labels = sum(count - 1 for line, count in counts.items() if line in labels and count > 1)
    return repeated_labels > 0 and repeated_labels == sum(count - 1 for count in counts.values() if count > 1)


def write_markdown_report(path: Path, report: dict[str, Any], issues: list[dict[str, Any]]) -> None:
    scorecard = report.get("scorecard", {})
    lines = [
        "# Corpus Quality Audit",
        "",
        f"- Run id: `{report.get('run_id')}`",
        f"- Status: `{report.get('status')}`",
        f"- Scope: `{scorecard.get('doc_scope')}`",
        f"- Documents: `{scorecard.get('document_count')}`",
        f"- Documents with issues: `{scorecard.get('documents_with_issues')}`",
        f"- Issues: `{scorecard.get('issue_count')}`",
        f"- Quality score: `{scorecard.get('quality_score')}`",
        "",
        "## Counts",
        "",
        f"- Severity: `{scorecard.get('severity_counts')}`",
        f"- Actions: `{scorecard.get('action_counts')}`",
        f"- Queues: `{scorecard.get('queue_counts')}`",
        "",
        "## Top Documents",
        "",
        "| Doc | Issues |",
        "|---|---:|",
    ]
    for row in scorecard.get("top_documents", [])[:20]:
        lines.append(f"| `{row.get('doc_id')}` | {row.get('issue_count')} |")
    lines.extend(
        [
            "",
            "## Sample Issues",
            "",
            "| Severity | Rule | Action | Doc | Chunk/Page |",
            "|---|---|---|---|---|",
        ]
    )
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    for issue in sorted(issues, key=lambda item: (severity_order.get(str(item.get("severity")), 9), str(item.get("doc_id"))))[:40]:
        loc = issue.get("chunk_id") or (f"p.{issue.get('page_start')}" if issue.get("page_start") else "")
        lines.append(
            f"| `{issue.get('severity')}` | `{issue.get('rule_id')}` | `{issue.get('action_type')}` | "
            f"`{issue.get('doc_id')}` | `{loc}` |"
        )
    lines.extend(
        [
            "",
            "## Loop-Check Contract",
            "",
            "- This audit is non-destructive.",
            "- Add/remove/repair/reorder actions are queued, not silently applied.",
            "- OCR gaps must be repaired by OCR/conversion reruns, not LLM guessing.",
            "- Service, alarm, safety, maintenance, and calibration edits require promotion gates.",
        ]
    )
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def load_latest_issues(settings: Settings) -> list[dict[str, Any]]:
    latest = read_json(settings.artifacts_dir / "quality_audit" / "latest_report.json", {})
    path = Path(str(latest.get("issues_path") or ""))
    return list(iter_jsonl(path))


def build_audit_id() -> str:
    stamp = utc_now().replace(":", "").replace("-", "").replace("+0000", "Z")
    return f"audit_{stamp}_{short_hash(stamp, 6)}"
