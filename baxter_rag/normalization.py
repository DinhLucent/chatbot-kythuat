from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, List

from pydantic import BaseModel, Field

from .conversion import convert_record
from .data_workbench import find_promoted_conversion_overlay
from .llm import OpenAICompatibleLLM, source_upload_allowed
from .llm_review import ChunkEnrichment as LLMChunkEnrichment, llm_audit_dir
from .metadata import record_with_inferred_doc_code
from .models import BlockType, ChunkRecord, Citation, DocumentRecord, SectionRecord
from .settings import Settings
from .utils import (
    keyword_codes,
    mojibake_score,
    normalized_text,
    read_json,
    repair_mojibake,
    short_hash,
    utc_now,
    write_json,
)


@dataclass
class NormalizeResult:
    doc_id: str
    status: str
    normalized_markdown: str
    metadata_json: str
    section_index_json: str
    citation_map_json: str
    chunk_plan_json: str
    quality_report_json: str
    chunk_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_manifest(
    settings: Settings,
    *,
    manifest_path: Path | None = None,
    limit: int | None = None,
    doc_id: str | None = None,
) -> dict[str, Any]:
    manifest_path = manifest_path or settings.artifacts_dir / "phase0" / "source_manifest.json"
    payload = read_json(manifest_path, {})
    records = [DocumentRecord(**item) for item in payload.get("records", [])]
    if doc_id:
        requested = {item.strip() for item in doc_id.split(",") if item.strip()}
        records = [record for record in records if record.doc_id in requested]
    if limit is not None:
        records = records[: max(0, limit)]

    results: list[NormalizeResult] = []
    all_chunks: list[dict[str, Any]] = []
    all_citations: list[dict[str, Any]] = []
    for record in records:
        result, chunks, citations = normalize_record(settings, record)
        results.append(result)
        all_chunks.extend(chunk.to_dict() for chunk in chunks if not chunk.low_value)
        all_citations.extend(citation.to_dict() for citation in citations)

    technical_intelligence = build_technical_intelligence(records, all_chunks, all_citations)
    tech_path = settings.artifacts_dir / "knowledge" / "technical_intelligence.json"
    write_json(tech_path, technical_intelligence)
    run_payload = {
        "schema_version": "baxter_normalization_run_v1",
        "generated_at": utc_now(),
        "results": [result.to_dict() for result in results],
        "summary": {
            "document_count": len(results),
            "chunk_count": sum(result.chunk_count for result in results),
            "technical_intelligence_path": str(tech_path),
        },
    }
    run_path = settings.artifacts_dir / "normalized" / "normalization_run_latest.json"
    write_json(run_path, run_payload)
    return {**run_payload, "normalization_run_path": str(run_path)}


def normalize_record(settings: Settings, record: DocumentRecord) -> tuple[NormalizeResult, list[ChunkRecord], list[Citation]]:
    record = record_with_inferred_doc_code(record)
    converted_dir = settings.artifacts_dir / "converted" / record.doc_id
    overlay = find_promoted_conversion_overlay(settings, record.doc_id)
    raw_md = Path(str(overlay.get("raw_markdown"))) if overlay else converted_dir / "raw.md"
    if not raw_md.exists() and not overlay:
        convert_record(settings, record, profile_name="technical_pdf_quality")
        overlay = find_promoted_conversion_overlay(settings, record.doc_id)
        raw_md = Path(str(overlay.get("raw_markdown"))) if overlay else converted_dir / "raw.md"
    if not raw_md.exists():
        raise FileNotFoundError(f"Converted markdown missing for {record.doc_id}: {raw_md}")

    text = repair_mojibake(raw_md.read_text(encoding="utf-8", errors="replace"))
    normalized = normalize_markdown(text, record)
    out_dir = settings.artifacts_dir / "normalized" / record.doc_id
    out_dir.mkdir(parents=True, exist_ok=True)
    normalized_path = out_dir / "normalized.md"
    normalized_path.write_text(normalized, encoding="utf-8")

    sections = build_sections(normalized, record.doc_id)
    chunks = build_chunks_v2(settings, normalized, record, sections, normalized_path)
    structure_sidecars = build_structure_sidecars(record, chunks, out_dir)
    citations = build_citations(record, chunks)
    quality = build_quality_report(record, normalized, chunks, citations)
    metadata = {
        "schema_version": "baxter_document_metadata_v1",
        "generated_at": utc_now(),
        "document": record.to_dict(),
        "artifact_paths": {
            "conversion_markdown": str(raw_md),
            "conversion_overlay_manifest": overlay.get("manifest_path") if overlay else None,
            "normalized_markdown": str(normalized_path),
            "section_index_json": str(out_dir / "section_index.json"),
            "citation_map_json": str(out_dir / "citation_map.json"),
            "chunk_plan_json": str(out_dir / "chunk_plan.json"),
            "quality_report_json": str(out_dir / "quality_report.json"),
            "structure_sidecars_json": str(out_dir / "structure_sidecars.json"),
        },
    }

    metadata_path = out_dir / "metadata.json"
    section_path = out_dir / "section_index.json"
    citation_path = out_dir / "citation_map.json"
    chunk_path = out_dir / "chunk_plan.json"
    quality_path = out_dir / "quality_report.json"
    structure_path = out_dir / "structure_sidecars.json"
    write_json(metadata_path, metadata)
    write_json(section_path, {"schema_version": "baxter_section_index_v1", "sections": [item.to_dict() for item in sections]})
    write_json(citation_path, {"schema_version": "baxter_citation_map_v1", "citations": [item.to_dict() for item in citations]})
    write_json(chunk_path, {"schema_version": "baxter_chunk_plan_v1", "chunks": [item.to_dict() for item in chunks]})
    write_json(quality_path, quality)
    write_json(structure_path, structure_sidecars)

    result = NormalizeResult(
        doc_id=record.doc_id,
        status="normalized",
        normalized_markdown=str(normalized_path),
        metadata_json=str(metadata_path),
        section_index_json=str(section_path),
        citation_map_json=str(citation_path),
        chunk_plan_json=str(chunk_path),
        quality_report_json=str(quality_path),
        chunk_count=len(chunks),
    )
    return result, chunks, citations


def normalize_markdown(text: str, record: DocumentRecord) -> str:
    lines: list[str] = [
        "---",
        f"doc_id: {record.doc_id}",
        f"brand: {record.brand}",
        f"business_unit: {record.business_unit}",
        f"model: {record.model}",
        f"doc_type: {record.doc_type}",
        f"doc_code: {record.doc_code}",
        f"software_version: {record.software_version}",
        f"language: {record.language}",
        "---",
        "",
        f"# {record.title or record.file_name or record.doc_id}",
        "",
    ]
    previous_blank = False
    for raw_line in text.splitlines():
        line = repair_mojibake(raw_line).replace("\x00", "").rstrip()
        line = repair_word_spacing_artifacts(line)
        if is_noise_line(line):
            continue
        line = normalize_heading(line)
        if line.strip() == "":
            if not previous_blank:
                lines.append("")
            previous_blank = True
            continue
        previous_blank = False
        lines.append(line)
    return "\n".join(lines).strip() + "\n"


def repair_word_spacing_artifacts(line: str) -> str:
    replacements = (
        (r"\bundersta nd\b", "understand"),
        (r"\bve rsion\b", "version"),
        (r"\bti me\b", "time"),
        (r"\bde scribed\b", "described"),
    )
    repaired = line
    for pattern, replacement in replacements:
        repaired = re.sub(pattern, replacement, repaired, flags=re.IGNORECASE)
    return repaired


def is_noise_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    normalized = normalized_text(stripped)
    if normalized in {"skip to content", "navigation menu"}:
        return True
    if re.fullmatch(r"[-_.\s]{6,}", stripped):
        return True
    return False


def normalize_heading(line: str) -> str:
    stripped = line.strip()
    if stripped.startswith("#") or stripped.startswith("<!-- page:"):
        return line
    numbered = re.match(r"^(\d{1,2}(?:\.\d{1,2}){0,4})\s+(.{3,160})$", stripped)
    if numbered and len(stripped) <= 180:
        title = numbered.group(2).strip(" .")
        if not re.search(r"\.{5,}", title):
            level = min(6, 2 + numbered.group(1).count("."))
            return f"{'#' * level} {numbered.group(1)} {title}"
    chapter = re.match(r"^(Chapter\s+\d+\.?.{0,140})$", stripped, flags=re.IGNORECASE)
    if chapter:
        return f"## {chapter.group(1).strip()}"
    return line


def build_sections(text: str, doc_id: str) -> list[SectionRecord]:
    lines = text.splitlines()
    current_page: int | None = None
    headings: list[dict[str, Any]] = []
    stack: list[tuple[int, str]] = []
    page_by_line: dict[int, int] = {}
    for index, line in enumerate(lines, start=1):
        page_match = re.search(r"<!--\s*page:(\d+)", line)
        if page_match:
            current_page = int(page_match.group(1))
        if current_page:
            page_by_line[index] = current_page
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if not match:
            continue
        level = len(match.group(1))
        title = match.group(2).strip()
        stack = [item for item in stack if item[0] < level]
        stack.append((level, title))
        headings.append({"line": index, "level": level, "title": title, "path": [item[1] for item in stack]})
    if not headings:
        return [
            SectionRecord(
                section_id=f"{doc_id}_section_root",
                doc_id=doc_id,
                title="Document",
                level=0,
                line_start=1,
                line_end=max(1, len(lines)),
                page_start=first_page(page_by_line, 1, len(lines)),
                page_end=last_page(page_by_line, 1, len(lines)),
                section_path=["Document"],
            )
        ]
    sections: list[SectionRecord] = []
    for idx, heading in enumerate(headings, start=1):
        next_line = headings[idx]["line"] - 1 if idx < len(headings) else len(lines)
        start = int(heading["line"])
        end = max(start, int(next_line))
        sections.append(
            SectionRecord(
                section_id=f"{doc_id}_section_{idx}",
                doc_id=doc_id,
                title=str(heading["title"]),
                level=int(heading["level"]),
                line_start=start,
                line_end=end,
                page_start=first_page(page_by_line, start, end),
                page_end=last_page(page_by_line, start, end),
                section_path=list(heading["path"]),
            )
        )
    return sections


class ChunkEnrichment(BaseModel):
    block_type: str = Field(description="Phân loại khối: alarm_code, troubleshooting_symptom, troubleshooting_cause, troubleshooting_fix, service_step, maintenance_step, calibration_step, warning_safety, spec_table, parts_table, wiring_diagram, figure_caption, software_note, prose.")
    context_summary: str = Field(description="Tóm tắt ngữ cảnh cực ngắn (1-2 câu) làm rõ mục đích phần tài liệu này.")
    semantic_keywords: List[str] = Field(description="Danh sách từ khóa kỹ thuật phụ trợ nổi bật trong đoạn văn.")


def build_chunks(
    settings: Settings,
    text: str,
    record: DocumentRecord,
    sections: list[SectionRecord],
    normalized_path: Path,
) -> list[ChunkRecord]:
    lines = text.splitlines()
    chunks: list[ChunkRecord] = []
    
    llm = None
    if settings.llm_enrichment_enabled:
        llm = OpenAICompatibleLLM()
        if not llm.configured:
            llm = None

    for section in sections:
        section_lines = lines[section.line_start - 1 : section.line_end]
        blocks = split_section_blocks(section_lines, base_line=section.line_start)
        for block_start, block_end, block_text in blocks:
            clean = block_text.strip()
            if not clean or len(clean) < 20:
                continue
            for part_start, part_end, part_text in split_large_block(clean, block_start, max_chars=1400):
                block_type = classify_block(part_text, record)
                alarm_codes = extract_alarm_codes(part_text)
                part_numbers = extract_part_numbers(part_text)
                content = part_text.strip()
                
                if llm is not None:
                    try:
                        system_prompt = (
                            "Bạn là chuyên gia phân tích tài liệu kỹ thuật Baxter. "
                            "Hãy đọc đoạn văn bản và trích xuất block_type, context_summary, semantic_keywords."
                        )
                        enrich_res: LLMChunkEnrichment = llm.complete_structured(
                            [
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": f"Đoạn văn:\n{part_text}"},
                            ],
                            LLMChunkEnrichment,
                            temperature=0.0,
                        )
                        try:
                            block_type = BlockType(enrich_res.block_type)
                        except ValueError:
                            pass
                        
                        context_str = f"[Context: {enrich_res.context_summary}]"
                        keywords_str = f"Keywords: {', '.join(enrich_res.semantic_keywords)}"
                        content = f"{context_str} {part_text.strip()}\n{keywords_str}"
                    except Exception:
                        pass

                low_value = is_low_value(part_text, block_type)
                chunk_id = f"{record.doc_id}_c{len(chunks) + 1:05d}"
                anchor_id = f"{record.doc_id}_a{len(chunks) + 1:05d}"
                chunks.append(
                    ChunkRecord(
                        chunk_id=chunk_id,
                        doc_id=record.doc_id,
                        content=content,
                        block_type=block_type,
                        section_path=section.section_path,
                        parent_section_id=section.section_id,
                        line_start=part_start,
                        line_end=part_end,
                        page_start=section.page_start,
                        page_end=section.page_end,
                        source_path=record.source_path,
                        source_file=record.file_name,
                        model=record.model,
                        doc_type=record.doc_type,
                        software_version=record.software_version,
                        doc_code=record.doc_code,
                        anchor_id=anchor_id,
                        alarm_codes=alarm_codes,
                        part_numbers=part_numbers,
                        low_value=low_value,
                    )
                )
    return chunks


def build_chunks_v2(
    settings: Settings,
    text: str,
    record: DocumentRecord,
    sections: list[SectionRecord],
    normalized_path: Path,
) -> list[ChunkRecord]:
    lines = text.splitlines()
    chunks: list[ChunkRecord] = []

    llm = None
    if settings.llm_enrichment_enabled:
        llm = OpenAICompatibleLLM()
        if not llm.configured or not source_upload_allowed(llm):
            llm = None

    for section in sections:
        section_lines = lines[section.line_start - 1 : section.line_end]
        blocks = split_section_blocks(section_lines, base_line=section.line_start)
        for block_start, _block_end, block_text in blocks:
            clean = block_text.strip()
            if not clean or len(clean) < 20:
                continue
            for part_start, part_end, part_text in split_large_block(clean, block_start, max_chars=1400):
                block_type = classify_block(part_text, record)
                alarm_codes = extract_alarm_codes(part_text)
                part_numbers = extract_part_numbers(part_text)
                chunk_id = f"{record.doc_id}_c{len(chunks) + 1:05d}"
                anchor_id = f"{record.doc_id}_a{len(chunks) + 1:05d}"
                semantic_summary = None
                semantic_keywords: list[str] = []
                llm_enrichment: dict[str, Any] = {}

                if llm is not None:
                    try:
                        user_payload = {
                            "doc_id": record.doc_id,
                            "chunk_id": chunk_id,
                            "model": record.model,
                            "doc_type": record.doc_type,
                            "section_path": section.section_path,
                            "line_start": part_start,
                            "line_end": part_end,
                            "text": part_text,
                        }
                        enrich_res: LLMChunkEnrichment = llm.complete_structured(
                            [
                                {
                                    "role": "system",
                                    "content": (
                                        "You review Baxter technical manual chunks for retrieval metadata only. "
                                        "Use only the supplied chunk text. Do not add service, safety, alarm, part, unit, or software facts that are not supported by the text."
                                    ),
                                },
                                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                            ],
                            LLMChunkEnrichment,
                            temperature=0.0,
                            max_tokens=900,
                            audit_dir=llm_audit_dir(settings, "normalization_enrichment"),
                            task_name="normalization_chunk_enrichment",
                            source_ids=[record.doc_id, chunk_id],
                            metadata={"doc_type": record.doc_type, "model": record.model},
                        )
                        try:
                            block_type = BlockType(enrich_res.block_type)
                        except ValueError:
                            pass
                        semantic_summary = enrich_res.context_summary
                        semantic_keywords = enrich_res.semantic_keywords[:20]
                        llm_enrichment = {
                            "status": "completed",
                            "source": "structured_llm",
                            "model": llm.model,
                            "risk_level": enrich_res.risk_level,
                            "requires_human_review": enrich_res.requires_human_review,
                        }
                    except Exception as exc:
                        llm_enrichment = {
                            "status": "failed",
                            "source": "structured_llm",
                            "error": str(exc)[:500],
                        }

                chunks.append(
                    ChunkRecord(
                        chunk_id=chunk_id,
                        doc_id=record.doc_id,
                        content=part_text.strip(),
                        block_type=block_type,
                        section_path=section.section_path,
                        parent_section_id=section.section_id,
                        line_start=part_start,
                        line_end=part_end,
                        page_start=section.page_start,
                        page_end=section.page_end,
                        source_path=record.source_path,
                        source_file=record.file_name,
                        model=record.model,
                        doc_type=record.doc_type,
                        software_version=record.software_version,
                        doc_code=record.doc_code,
                        anchor_id=anchor_id,
                        alarm_codes=alarm_codes,
                        part_numbers=part_numbers,
                        semantic_summary=semantic_summary,
                        semantic_keywords=semantic_keywords,
                        llm_enrichment=llm_enrichment,
                        low_value=is_low_value(part_text, block_type),
                    )
                )
    return chunks


def split_section_blocks(lines: list[str], *, base_line: int) -> list[tuple[int, int, str]]:
    blocks: list[tuple[int, int, str]] = []
    current: list[str] = []
    start_line = base_line
    for offset, line in enumerate(lines):
        line_no = base_line + offset
        if line.strip() == "":
            if current:
                blocks.append((start_line, line_no - 1, "\n".join(current)))
                current = []
            start_line = line_no + 1
            continue
        if not current:
            start_line = line_no
        current.append(line)
    if current:
        blocks.append((start_line, base_line + len(lines) - 1, "\n".join(current)))
    return blocks


def split_large_block(text: str, start_line: int, *, max_chars: int) -> list[tuple[int, int, str]]:
    if len(text) <= max_chars:
        line_count = max(1, text.count("\n") + 1)
        return [(start_line, start_line + line_count - 1, text)]
    sentences = re.split(r"(?<=[.!?。])\s+", text)
    parts: list[tuple[int, int, str]] = []
    current = ""
    current_start = start_line
    line_cursor = start_line
    for sentence in sentences:
        if current and len(current) + len(sentence) + 1 > max_chars:
            line_count = max(1, current.count("\n") + 1)
            parts.append((current_start, current_start + line_count - 1, current))
            line_cursor = current_start + line_count
            current_start = line_cursor
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        line_count = max(1, current.count("\n") + 1)
        parts.append((current_start, current_start + line_count - 1, current))
    return parts


def classify_block(text: str, record: DocumentRecord) -> BlockType:
    norm = normalized_text(text)
    if any(term in norm for term in ("warning", "caution", "canh bao", "chu y", "nguy hiem", "safety")):
        return BlockType.WARNING_SAFETY
    if any(term in norm for term in ("alarm", "bao dong", "ma loi", "error code", "malfunction alarm")):
        return BlockType.ALARM_CODE
    if any(term in norm for term in ("troubleshooting", "khac phuc", "cause", "nguyen nhan", "remedy", "corrective action")):
        if any(term in norm for term in ("cause", "nguyen nhan")):
            return BlockType.TROUBLESHOOTING_CAUSE
        if any(term in norm for term in ("remedy", "action", "khac phuc", "corrective")):
            return BlockType.TROUBLESHOOTING_FIX
        return BlockType.TROUBLESHOOTING_SYMPTOM
    if any(term in norm for term in ("calibration", "hieu chuan", "calibrate")):
        return BlockType.CALIBRATION_STEP
    if any(term in norm for term in ("maintenance", "bao tri", "preventive", "pm checklist")):
        return BlockType.MAINTENANCE_STEP
    if any(term in norm for term in ("replace", "remove", "install", "service", "thay", "lap dat", "thao", "kiem tra")):
        return BlockType.SERVICE_STEP
    if "|" in text and re.search(r"\|\s*-{2,}\s*\|", text):
        if any(term in norm for term in ("part", "spare", "catalogue", "catalog", "phu tung")):
            return BlockType.PARTS_TABLE
        return BlockType.SPEC_TABLE
    if any(term in norm for term in ("wiring", "diagram", "schematic", "so do")):
        return BlockType.WIRING_DIAGRAM
    if any(term in norm for term in ("figure", "hinh", "image", "illustration")):
        return BlockType.FIGURE_CAPTION
    if "software" in norm or re.search(r"\bsw\s*\d", norm):
        return BlockType.SOFTWARE_NOTE
    if record.doc_type == "service_bulletin":
        return BlockType.SOFTWARE_NOTE if "software" in norm else BlockType.SERVICE_STEP
    return BlockType.PROSE


def build_citations(record: DocumentRecord, chunks: list[ChunkRecord]) -> list[Citation]:
    citations: list[Citation] = []
    for chunk in chunks:
        page = f"p.{chunk.page_start}" if chunk.page_start else "line"
        section = " > ".join(chunk.section_path[-3:]) if chunk.section_path else "Document"
        display = f"{record.file_name or record.doc_id} | {page} | {section}"
        citations.append(
            Citation(
                citation_id=f"cit_{short_hash(chunk.chunk_id, 12)}",
                chunk_id=chunk.chunk_id,
                doc_id=record.doc_id,
                source_file=record.source_path,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                section_path=chunk.section_path,
                line_start=chunk.line_start,
                line_end=chunk.line_end,
                display=display,
                bbox=chunk.bbox,
            )
        )
    return citations


def build_quality_report(
    record: DocumentRecord,
    text: str,
    chunks: list[ChunkRecord],
    citations: list[Citation],
) -> dict[str, Any]:
    block_counts = Counter(chunk.block_type.value for chunk in chunks)
    locator_ratio = sum(1 for citation in citations if citation.page_start or citation.line_start) / max(1, len(citations))
    mojibake = mojibake_score(text)
    warnings: list[dict[str, str]] = []
    if mojibake > 10:
        warnings.append({"code": "mojibake_risk", "message": "Text contains possible encoding artifacts."})
    ocr_overlay_used = "conversion_overlay=true created_by=paddleocr_retry" in text
    unresolved_ocr = "empty_or_ocr_required" in text
    if (record.embedded_text_ratio or 0.0) < 0.05 and not (ocr_overlay_used and not unresolved_ocr):
        warnings.append({"code": "ocr_required", "message": "PDF appears to have weak embedded text."})
    if not citations:
        warnings.append({"code": "missing_citation_map", "message": "No citation records were generated."})
    return {
        "schema_version": "baxter_quality_report_v1",
        "generated_at": utc_now(),
        "doc_id": record.doc_id,
        "summary": {
            "char_count": len(text),
            "chunk_count": len(chunks),
            "citation_count": len(citations),
            "locator_ratio": round(locator_ratio, 4),
            "mojibake_score": mojibake,
            "block_counts": dict(block_counts),
            "ocr_overlay_used": ocr_overlay_used,
            "unresolved_ocr_marker_count": text.count("empty_or_ocr_required"),
            "blank_page_verified_count": text.count("blank_page_verified_by_ocr"),
        },
        "quality_gates": {
            "raw_source_immutable": True,
            "normalized_markdown_present": bool(text.strip()),
            "chunk_plan_present": bool(chunks),
            "citation_map_present": bool(citations),
            "source_locator_present": locator_ratio >= 0.75,
            "mojibake_under_review_threshold": mojibake <= 10,
            "technical_block_types_present": bool(block_counts),
        },
        "warnings": warnings,
    }


def build_technical_intelligence(
    records: list[DocumentRecord],
    chunks: list[dict[str, Any]],
    citations: list[dict[str, Any]],
) -> dict[str, Any]:
    citation_by_chunk = {item["chunk_id"]: item for item in citations}
    alarms = []
    parts = []
    bulletins = []
    software_versions: dict[str, set[str]] = {}
    safety = []
    for record in records:
        if record.software_version:
            software_versions.setdefault(record.model, set()).add(record.software_version)
        if record.doc_type == "service_bulletin":
            bulletins.append(
                {
                    "doc_id": record.doc_id,
                    "doc_code": record.doc_code,
                    "title": record.title,
                    "model": record.model,
                    "software_version": record.software_version,
                    "source_file": record.file_name,
                }
            )
    for chunk in chunks:
        citation = citation_by_chunk.get(chunk["chunk_id"], {})
        row = {
            "chunk_id": chunk["chunk_id"],
            "doc_id": chunk["doc_id"],
            "model": chunk.get("model"),
            "doc_type": chunk.get("doc_type"),
            "section_path": chunk.get("section_path", []),
            "citation": citation.get("display"),
            "text": str(chunk.get("content") or "")[:900],
        }
        if chunk.get("block_type") == BlockType.ALARM_CODE.value or chunk.get("alarm_codes"):
            alarms.append({**row, "alarm_codes": chunk.get("alarm_codes", [])})
        if chunk.get("part_numbers"):
            parts.append({**row, "part_numbers": chunk.get("part_numbers", [])})
        if chunk.get("block_type") == BlockType.WARNING_SAFETY.value:
            safety.append(row)
    return {
        "schema_version": "baxter_technical_intelligence_v1",
        "generated_at": utc_now(),
        "alarm_index": alarms,
        "service_bulletin_index": bulletins,
        "parts_code_index": parts,
        "software_version_index": {key: sorted(value) for key, value in software_versions.items()},
        "safety_warning_index": safety,
    }


def first_page(page_by_line: dict[int, int], start: int, end: int) -> int | None:
    for line in range(start, end + 1):
        if line in page_by_line:
            return page_by_line[line]
    return None


def last_page(page_by_line: dict[int, int], start: int, end: int) -> int | None:
    for line in range(end, start - 1, -1):
        if line in page_by_line:
            return page_by_line[line]
    return None


def section_for_line(sections: list[SectionRecord], line: int) -> SectionRecord | None:
    for section in sections:
        if section.line_start <= line <= section.line_end:
            return section
    return sections[-1] if sections else None


def extract_alarm_codes(text: str) -> list[str]:
    patterns = [
        r"\b(?:alarm|error|code)\s*[:#-]?\s*([A-Z]?\d{2,5}[A-Z]?)\b",
        r"\b[A-Z]{1,3}-?\d{2,5}\b",
    ]
    out: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            code = match.group(1) if match.lastindex else match.group(0)
            code = code.upper().replace(" ", "")
            if len(code) < 2 or code in seen:
                continue
            seen.add(code)
            out.append(code)
    return out[:12]


def extract_part_numbers(text: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for code in keyword_codes(text):
        if code not in seen:
            seen.add(code)
            out.append(code)
    return out[:16]


def build_structure_sidecars(record: DocumentRecord, chunks: list[ChunkRecord], out_dir: Path) -> dict[str, Any]:
    tables: list[dict[str, Any]] = []
    figures: list[dict[str, Any]] = []
    for chunk in chunks:
        if not chunk.low_value and is_table_candidate(chunk.content, chunk.block_type):
            table_id = f"tbl_{short_hash(chunk.chunk_id, 12)}"
            chunk.table_refs.append(table_id)
            tables.append(
                {
                    "table_id": table_id,
                    "doc_id": record.doc_id,
                    "chunk_id": chunk.chunk_id,
                    "page_start": chunk.page_start,
                    "page_end": chunk.page_end,
                    "section_path": chunk.section_path,
                    "source": "normalized_chunk_text",
                    "confidence": "candidate",
                    "markdown_preview": table_markdown_preview(chunk.content),
                    "raw_text_preview": chunk.content[:2000],
                    "promotion_status": "derived_sidecar",
                    "rollback_note": "Regenerate normalization artifacts to remove or update this sidecar.",
                }
            )
        if is_figure_candidate(chunk.content, chunk.block_type):
            figure_id = f"fig_{short_hash(chunk.chunk_id, 12)}"
            chunk.image_refs.append(figure_id)
            figures.append(
                {
                    "figure_id": figure_id,
                    "doc_id": record.doc_id,
                    "chunk_id": chunk.chunk_id,
                    "page_start": chunk.page_start,
                    "page_end": chunk.page_end,
                    "section_path": chunk.section_path,
                    "source": "normalized_chunk_text",
                    "confidence": "caption_or_reference_candidate",
                    "caption_preview": figure_caption_preview(chunk.content),
                    "raw_text_preview": chunk.content[:1200],
                    "promotion_status": "derived_sidecar",
                    "rollback_note": "Regenerate normalization artifacts to remove or update this sidecar.",
                }
            )
    return {
        "schema_version": "baxter_structure_sidecars_v1",
        "generated_at": utc_now(),
        "doc_id": record.doc_id,
        "tables": tables,
        "figures": figures,
        "counts": {
            "table_count": len(tables),
            "figure_count": len(figures),
        },
        "principle": "Sidecars are source-derived retrieval aids. They do not add facts beyond normalized chunk text.",
    }


def is_table_candidate(text: str, block_type: BlockType) -> bool:
    if block_type in {BlockType.SPEC_TABLE, BlockType.PARTS_TABLE}:
        return True
    norm = normalized_text(text)
    if any(marker in norm for marker in ("table ", "bang ", "bieu bang", "part number", "ma phu tung")):
        return True
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    structured_rows = 0
    for line in lines:
        if "|" in line:
            structured_rows += 1
        elif len(re.split(r"\s{2,}", line)) >= 3:
            structured_rows += 1
    if structured_rows >= 3:
        return True
    return is_matrix_like_table_text(lines, norm, block_type)


def is_matrix_like_table_text(lines: list[str], norm: str, block_type: BlockType) -> bool:
    if len(lines) < 8:
        return False
    phrases = (
        "parameter description",
        "alarm name",
        "temporary limit",
        "operator action",
        "service technician action",
        "possible values",
        "values displayed are",
        "status of the",
        "subtest alarm",
        "description signals",
        "interfacing board",
        "measurement signal",
        "voltage reference",
        "scale open switch",
        "corrective action",
        "recommended action",
        "cause remedy",
        "symptom cause",
        "call service",
        "override",
        "disables alarm",
        "clears when",
    )
    phrase_hits = sum(1 for phrase in phrases if phrase in norm)
    repeated = repeated_source_line_ratio(lines)
    short_ratio = sum(1 for line in lines if len(line) <= 90) / max(1, len(lines))
    code_like = sum(1 for line in lines if re.match(r"^[A-Z0-9][A-Z0-9./() -]{2,34}\.?\s*(?:$|[A-Z])", line))
    bullet_like = sum(1 for line in lines if is_bullet_like_line(line))
    group_labels = sum(1 for line in lines if re.match(r"^[A-Za-z][A-Za-z0-9 /+.-]{1,40}\s*:$", line))
    numeric_rows = sum(1 for line in lines if re.match(r"^[A-Za-z]{1,10}\d{0,4}\b", line) and len(re.findall(r"\d+", line)) >= 4)
    high_risk_table_context = block_type in {
        BlockType.ALARM_CODE,
        BlockType.SERVICE_STEP,
        BlockType.MAINTENANCE_STEP,
        BlockType.CALIBRATION_STEP,
        BlockType.TROUBLESHOOTING_CAUSE,
        BlockType.TROUBLESHOOTING_FIX,
        BlockType.TROUBLESHOOTING_SYMPTOM,
    }
    if phrase_hits >= 1 and (repeated >= 0.08 or short_ratio >= 0.65):
        return True
    if repeated >= 0.18 and (phrase_hits >= 1 or code_like >= 4):
        return True
    if repeated >= 0.18 and bullet_like >= 4 and group_labels >= 2:
        return True
    if numeric_rows >= 3:
        return True
    if repeated >= 0.18 and bullet_like >= 4 and high_risk_table_context:
        return True
    if block_type == BlockType.ALARM_CODE and repeated >= 0.25 and short_ratio >= 0.55:
        return True
    if high_risk_table_context and repeated >= 0.24 and short_ratio >= 0.65 and code_like >= 3:
        return True
    return False


def is_bullet_like_line(line: str) -> bool:
    stripped = line.strip()
    if re.match(r"^(?:[-*\u2022\u25cf]|\d+[.)])\s+", stripped):
        return True
    return stripped.startswith("\u00e2")


def repeated_source_line_ratio(lines: list[str]) -> float:
    normalized_lines = [re.sub(r"\s+", " ", line).strip() for line in lines if len(line.strip()) >= 8]
    if not normalized_lines:
        return 0.0
    counts = Counter(normalized_lines)
    repeated = sum(count - 1 for count in counts.values() if count > 1)
    return repeated / max(1, len(normalized_lines))


def is_figure_candidate(text: str, block_type: BlockType) -> bool:
    if block_type in {BlockType.FIGURE_CAPTION, BlockType.WIRING_DIAGRAM}:
        return True
    norm = normalized_text(text)
    return bool(re.search(r"\b(fig|figure|hinh|image|illustration|diagram|so do)\b", norm))


def table_markdown_preview(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    pipe_lines = [line for line in lines if "|" in line]
    if len(pipe_lines) >= 2:
        return "\n".join(pipe_lines[:20])
    rows = [re.split(r"\s{2,}", line) for line in lines if len(re.split(r"\s{2,}", line)) >= 2]
    if len(rows) < 2:
        return "\n".join(lines[:12])
    width = max(len(row) for row in rows[:20])
    padded = [row + [""] * (width - len(row)) for row in rows[:20]]
    header = padded[0]
    sep = ["---"] * width
    out = ["| " + " | ".join(header) + " |", "| " + " | ".join(sep) + " |"]
    for row in padded[1:]:
        out.append("| " + " | ".join(row) + " |")
    return "\n".join(out)


def figure_caption_preview(text: str) -> str:
    for line in text.splitlines():
        if is_figure_candidate(line, BlockType.PROSE):
            return line.strip()[:500]
    return text.strip()[:500]


def is_low_value(text: str, block_type: BlockType) -> bool:
    stripped = text.strip()
    if stripped.startswith("#") and "\n" not in stripped:
        return True
    norm = normalized_text(text)
    if len(norm) < 30:
        return True
    if any(marker in norm for marker in ("table of contents", "muc luc", "copyright", "all rights reserved", "legal manufacturer")):
        return True
    if dot_leader_toc_noise(text):
        return True
    return False


def dot_leader_toc_noise(text: str) -> bool:
    dot_leader_count = len(re.findall(r"\.{8,}", text))
    if dot_leader_count < 1:
        return False
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    toc_like = 0
    for line in lines:
        if re.search(r"\.{8,}\s*\d{1,4}\s*$", line):
            toc_like += 1
        elif re.match(r"^\d+(?:\.\d+){0,5}\s+.+\s+\d{1,4}$", line) and "." * 8 in line:
            toc_like += 1
    if dot_leader_count == 1 and toc_like >= 1 and len(text) <= 320:
        return True
    return toc_like >= 2
