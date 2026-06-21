from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .deduplication import build_duplicate_policy
from .settings import Settings
from .utils import clamp_text, normalized_text, read_json, repair_mojibake, short_hash, utc_now, write_json


DOC_SCHEMA_VERSION = "baxter_technical_enrichment_document_v1"
CORPUS_SCHEMA_VERSION = "baxter_technical_enrichment_corpus_v1"
RUN_SCHEMA_VERSION = "baxter_technical_enrichment_run_v1"

MAX_ENTITY_REFS = 16
MAX_CONTEXT_REFS = 120
MAX_RELATIONSHIPS = 200
MAX_INDEX_REFS = 24

PROCEDURE_BLOCK_TYPES = {
    "service_step",
    "maintenance_step",
    "calibration_step",
    "troubleshooting_fix",
    "troubleshooting_cause",
    "troubleshooting_symptom",
}
SAFETY_BLOCK_TYPES = {"warning_safety"}
TABLE_BLOCK_TYPES = {"spec_table", "parts_table"}

ALARM_CONTEXT_MARKERS = (
    "alarm",
    "error code",
    "malfunction alarm",
    "operator action",
    "service technician action",
    "corrective action",
    "cause",
    "remedy",
)

TOOL_PHRASES: dict[str, tuple[str, ...]] = {
    "combination wrench": ("combination wrench",),
    "wrench": (" wrench", "open-end", "box wrench"),
    "magnetic driver": ("magnetic driver",),
    "hex bit": ("hex bit", "hex bits"),
    "torx bit": ("torx bit", "torx bits", "torx"),
    "screwdriver": ("screwdriver", "driver"),
    "esd protection": ("esd", "finger cot"),
    "calibration weight": ("calibration weight", "weight set"),
    "syringe": ("syringe",),
    "digital manometer": ("digital manometer", "manometer"),
    "barometer": ("barometer",),
    "timer": ("timer", "stopwatch"),
    "multimeter": ("multimeter", "multi meter", "digital meter"),
    "temperature meter": ("temperature meter",),
    "loopback tool": ("loopback tool",),
    "peristaltic pump": ("peristaltic pump",),
    "pressure meter": ("pressure meter",),
    "jumper": ("jumper",),
    "fixture": ("fixture",),
    "ratchet strap": ("ratchet strap",),
    "back probe pins": ("back probe",),
}

TECHNICAL_TERMS: dict[str, tuple[str, ...]] = {
    "access pressure": ("access pressure",),
    "return pressure": ("return pressure",),
    "effluent": ("effluent",),
    "dialysate": ("dialysate",),
    "replacement fluid": ("replacement fluid",),
    "pre-blood pump": ("pre-blood pump", "pbp"),
    "blood leak detector": ("blood leak detector", "bld"),
    "air detector": ("air detector", "air bubble"),
    "syringe pump": ("syringe pump",),
    "warmer": ("warmer", "thermax"),
    "scale": ("scale", "scales"),
    "self-test": ("self-test", "sst"),
    "calibration": ("calibration", "calibrate"),
    "preventive maintenance": ("preventive maintenance", "pm checklist"),
    "software installation": ("software installation", "software update"),
    "service bulletin": ("service bulletin",),
    "disposable set": ("disposable set", "disposable"),
    "crrt": ("crrt", "continuous renal replacement"),
    "tpe": ("tpe", "therapeutic plasma exchange"),
}

UNIT_PATTERN = re.compile(
    r"\b(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>"
    r"mL/h|ml/h|mL/hr|ml/hr|mL/min|ml/min|mmHg|hPa|kPa|psi|"
    r"kg|g|mg|mL|ml|L|min|mins|sec|secs|s|hr|hrs|hour|hours|"
    r"Hz|kHz|VDC|VAC|V|mV|mA|A|MOhm|kOhm|Ohm|ohm|\u03a9|"
    r"\u00b0C|deg C|K|%)\b",
    flags=re.IGNORECASE,
)


def run_technical_enrichment(
    settings: Settings,
    *,
    doc_id: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    settings.ensure_dirs()
    run_id = build_run_id()
    run_dir = settings.artifacts_dir / "enrichment" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    doc_dirs = select_normalized_doc_dirs(settings, doc_id=doc_id, limit=limit)
    duplicate_policy = build_duplicate_policy(settings)
    excluded_doc_ids = set(str(item) for item in duplicate_policy.get("excluded_doc_ids", []))

    document_reports: list[dict[str, Any]] = []
    output_paths: dict[str, str] = {}
    for doc_dir in doc_dirs:
        report = build_document_enrichment(doc_dir, active_for_retrieval=doc_dir.name not in excluded_doc_ids)
        out_path = settings.artifacts_dir / "enrichment" / "documents" / doc_dir.name / "technical_enrichment.json"
        write_json(out_path, report)
        document_reports.append({**report, "enrichment_path": str(out_path)})
        output_paths[doc_dir.name] = str(out_path)

    corpus = build_corpus_enrichment(settings, document_reports, duplicate_policy)
    corpus_path = settings.artifacts_dir / "enrichment" / "technical_enrichment_latest.json"
    write_json(corpus_path, corpus)

    report_md_path = settings.artifacts_dir / "enrichment" / "technical_enrichment_latest.md"
    write_markdown_report(report_md_path, corpus)

    run_report = {
        "schema_version": RUN_SCHEMA_VERSION,
        "generated_at": utc_now(),
        "run_id": run_id,
        "status": "completed" if document_reports else "empty",
        "summary": corpus["summary"],
        "policy": corpus["policy"],
        "paths": {
            "corpus_latest": str(corpus_path),
            "report_markdown": str(report_md_path),
            "run_dir": str(run_dir),
            "document_enrichment_root": str(settings.artifacts_dir / "enrichment" / "documents"),
        },
        "document_paths": output_paths,
    }
    write_json(run_dir / "report.json", run_report)
    write_json(settings.artifacts_dir / "enrichment" / "latest_run.json", run_report)
    return run_report


def build_document_enrichment(doc_dir: Path, *, active_for_retrieval: bool) -> dict[str, Any]:
    metadata = read_json(doc_dir / "metadata.json", {})
    chunk_plan = read_json(doc_dir / "chunk_plan.json", {})
    citation_map = read_json(doc_dir / "citation_map.json", {})
    structure_sidecars = read_json(doc_dir / "structure_sidecars.json", {})

    document = metadata.get("document") if isinstance(metadata.get("document"), dict) else {}
    chunks = [item for item in chunk_plan.get("chunks", []) if isinstance(item, dict)]
    citations = {
        str(item.get("chunk_id")): item
        for item in citation_map.get("citations", [])
        if isinstance(item, dict) and item.get("chunk_id")
    }

    entity_maps: dict[str, dict[str, list[dict[str, Any]]]] = {
        "alarm_codes": defaultdict(list),
        "part_numbers": defaultdict(list),
        "units": defaultdict(list),
        "tools": defaultdict(list),
        "technical_terms": defaultdict(list),
    }
    warning_refs: list[dict[str, Any]] = []
    procedure_refs: list[dict[str, Any]] = []
    table_refs: list[dict[str, Any]] = []
    figure_refs: list[dict[str, Any]] = []
    relationships: list[dict[str, Any]] = []
    block_counts: Counter[str] = Counter()
    active_chunks = 0

    doc_code = str(document.get("doc_code") or "").upper()
    for chunk in chunks:
        if chunk.get("low_value"):
            continue
        active_chunks += 1
        block_type = str(chunk.get("block_type") or "prose")
        block_counts[block_type] += 1
        citation = citations.get(str(chunk.get("chunk_id")), {})
        content = repair_mojibake(str(chunk.get("content") or ""))
        norm = normalized_text(content)
        ref = build_chunk_ref(chunk, citation, content)

        if block_type in SAFETY_BLOCK_TYPES:
            warning_refs.append(ref)
        if block_type in PROCEDURE_BLOCK_TYPES:
            procedure_refs.append(ref)

        if is_alarm_context(chunk, norm):
            for code in supported_alarm_codes(chunk, doc_code):
                entity_maps["alarm_codes"][code].append(ref)
                append_relationship(
                    relationships,
                    "alarm_code_context",
                    code,
                    ref,
                    context_markers(content),
                )

        for part_number in supported_part_numbers(chunk, doc_code, content):
            entity_maps["part_numbers"][part_number].append(ref)

        for unit in extract_units(content):
            unit_ref = build_chunk_ref(chunk, citation, unit["evidence_preview"])
            unit_ref["value"] = unit["value"]
            entity_maps["units"][unit["unit"]].append(unit_ref)

        for tool in extract_tool_mentions(content, block_type, chunk.get("section_path")):
            tool_ref = build_chunk_ref(chunk, citation, tool["evidence_preview"])
            entity_maps["tools"][tool["tool"]].append(tool_ref)
            if block_type in PROCEDURE_BLOCK_TYPES:
                append_relationship(relationships, "tool_mentioned_in_procedure_context", tool["tool"], tool_ref, [])

        for term in extract_technical_terms(content):
            term_ref = build_chunk_ref(chunk, citation, term["evidence_preview"])
            entity_maps["technical_terms"][term["term"]].append(term_ref)

    for item in structure_sidecars.get("tables", []):
        if isinstance(item, dict):
            table_refs.append(build_sidecar_ref(item, "table_id", "markdown_preview"))
    for item in structure_sidecars.get("figures", []):
        if isinstance(item, dict):
            figure_refs.append(build_sidecar_ref(item, "figure_id", "caption_preview"))

    entities = {
        "alarm_codes": entity_list(entity_maps["alarm_codes"], "code"),
        "part_numbers": entity_list(entity_maps["part_numbers"], "part_number"),
        "units": entity_list(entity_maps["units"], "unit"),
        "tools": entity_list(entity_maps["tools"], "tool"),
        "technical_terms": entity_list(entity_maps["technical_terms"], "term"),
        "warnings": warning_refs[:MAX_CONTEXT_REFS],
        "procedures": procedure_refs[:MAX_CONTEXT_REFS],
        "tables": table_refs,
        "figures": figure_refs,
    }
    aliases = build_retrieval_aliases(document, entities)
    counts = {
        "chunk_count": len(chunks),
        "active_chunk_count": active_chunks,
        "alarm_code_count": len(entities["alarm_codes"]),
        "part_number_count": len(entities["part_numbers"]),
        "unit_count": len(entities["units"]),
        "tool_count": len(entities["tools"]),
        "technical_term_count": len(entities["technical_terms"]),
        "warning_chunk_count": len(warning_refs),
        "procedure_chunk_count": len(procedure_refs),
        "table_count": len(table_refs),
        "figure_count": len(figure_refs),
    }

    return {
        "schema_version": DOC_SCHEMA_VERSION,
        "generated_at": utc_now(),
        "doc_id": doc_dir.name,
        "active_for_retrieval": active_for_retrieval,
        "document": {
            "doc_id": document.get("doc_id") or doc_dir.name,
            "title": document.get("title"),
            "model": document.get("model"),
            "doc_type": document.get("doc_type"),
            "doc_code": document.get("doc_code"),
            "software_version": document.get("software_version"),
            "language": document.get("language"),
            "source_file": document.get("source_path"),
            "page_count": document.get("page_count"),
        },
        "source_artifacts": {
            "metadata_json": str(doc_dir / "metadata.json"),
            "chunk_plan_json": str(doc_dir / "chunk_plan.json"),
            "citation_map_json": str(doc_dir / "citation_map.json"),
            "structure_sidecars_json": str(doc_dir / "structure_sidecars.json"),
        },
        "counts": counts,
        "block_counts": dict(block_counts),
        "entities": entities,
        "relationships": relationships[:MAX_RELATIONSHIPS],
        "retrieval_hints": {
            "aliases": aliases,
            "high_signal_sections": high_signal_sections(procedure_refs, warning_refs, table_refs),
            "preferred_filters": {
                key: value
                for key, value in {
                    "model": document.get("model"),
                    "doc_type": document.get("doc_type"),
                    "software_version": document.get("software_version"),
                    "doc_code": document.get("doc_code"),
                }.items()
                if value
            },
        },
        "policy": {
            "source_derived_only": True,
            "llm_generated": False,
            "promotion_status": "derived_sidecar",
            "canonical_source_mutated": False,
            "note": "Derived from normalized chunks, citations, metadata, and structure sidecars only.",
        },
    }


def build_corpus_enrichment(
    settings: Settings,
    document_reports: list[dict[str, Any]],
    duplicate_policy: dict[str, Any],
) -> dict[str, Any]:
    indexes: dict[str, dict[str, dict[str, Any]]] = {
        "alarm_code": {},
        "part_number": {},
        "unit": {},
        "tool": {},
        "technical_term": {},
        "retrieval_alias": {},
    }
    documents: list[dict[str, Any]] = []
    total_counts: Counter[str] = Counter()

    for report in document_reports:
        counts = report.get("counts") if isinstance(report.get("counts"), dict) else {}
        total_counts.update({key: int(value or 0) for key, value in counts.items()})
        document = report.get("document") if isinstance(report.get("document"), dict) else {}
        documents.append(
            {
                "doc_id": report.get("doc_id"),
                "active_for_retrieval": bool(report.get("active_for_retrieval")),
                "model": document.get("model"),
                "doc_type": document.get("doc_type"),
                "doc_code": document.get("doc_code"),
                "software_version": document.get("software_version"),
                "counts": counts,
                "enrichment_path": report.get("enrichment_path"),
            }
        )
        if not report.get("active_for_retrieval"):
            continue
        entities = report.get("entities") if isinstance(report.get("entities"), dict) else {}
        add_entity_index(indexes["alarm_code"], entities.get("alarm_codes", []), "code", report)
        add_entity_index(indexes["part_number"], entities.get("part_numbers", []), "part_number", report)
        add_entity_index(indexes["unit"], entities.get("units", []), "unit", report)
        add_entity_index(indexes["tool"], entities.get("tools", []), "tool", report)
        add_entity_index(indexes["technical_term"], entities.get("technical_terms", []), "term", report)
        for alias in report.get("retrieval_hints", {}).get("aliases", []):
            add_alias_index(indexes["retrieval_alias"], str(alias), report)

    summary = {
        "document_count": len(document_reports),
        "active_document_count": sum(1 for item in document_reports if item.get("active_for_retrieval")),
        "deduplicated_excluded_doc_count": len(duplicate_policy.get("excluded_doc_ids", [])),
        **dict(total_counts),
    }
    return {
        "schema_version": CORPUS_SCHEMA_VERSION,
        "generated_at": utc_now(),
        "summary": summary,
        "policy": {
            "source_derived_only": True,
            "llm_generated": False,
            "canonical_source_mutated": False,
            "indexes_scope": "active_retrieval_documents_only",
            "duplicate_policy_schema": duplicate_policy.get("schema_version"),
            "duplicate_excluded_doc_ids": duplicate_policy.get("excluded_doc_ids", []),
        },
        "documents": sorted(documents, key=lambda item: str(item.get("doc_id") or "")),
        "indexes": indexes,
        "paths": {
            "document_enrichment_root": str(settings.artifacts_dir / "enrichment" / "documents"),
            "deduplication_policy_source": str(settings.artifacts_dir / "index" / "deduplication_policy.json"),
        },
    }


def select_normalized_doc_dirs(settings: Settings, *, doc_id: str | None, limit: int | None) -> list[Path]:
    normalized_root = settings.artifacts_dir / "normalized"
    doc_dirs = [path for path in sorted(normalized_root.glob("*")) if path.is_dir() and (path / "chunk_plan.json").exists()]
    if doc_id:
        requested = {item.strip() for item in doc_id.split(",") if item.strip()}
        doc_dirs = [path for path in doc_dirs if path.name in requested]
    if limit is not None:
        doc_dirs = doc_dirs[: max(0, limit)]
    return doc_dirs


def is_alarm_context(chunk: dict[str, Any], norm: str) -> bool:
    section_text = normalized_text(" ".join(str(item) for item in chunk.get("section_path") or []))
    haystack = f"{norm} {section_text}"
    return str(chunk.get("block_type") or "") == "alarm_code" and any(marker in haystack for marker in ALARM_CONTEXT_MARKERS)


def supported_alarm_codes(chunk: dict[str, Any], doc_code: str) -> list[str]:
    codes = []
    seen: set[str] = set()
    for raw in chunk.get("alarm_codes") or []:
        code = str(raw or "").strip().upper()
        if not code or code == doc_code or code in seen:
            continue
        if not is_plausible_alarm_code(code):
            continue
        seen.add(code)
        codes.append(code)
    return codes


def is_plausible_alarm_code(code: str) -> bool:
    return bool(re.fullmatch(r"(?:T\d{3,5}|\d{3,5})", code))


def supported_part_numbers(chunk: dict[str, Any], doc_code: str, text: str) -> list[str]:
    parts = []
    seen: set[str] = set()
    for raw in [*(chunk.get("part_numbers") or []), *extract_part_number_candidates(text, chunk)]:
        part = str(raw or "").strip().upper()
        if not part or part == doc_code or part in seen:
            continue
        if is_plausible_alarm_code(part) or re.fullmatch(r"L\d{4,5}", part):
            continue
        if re.fullmatch(r"SW\d+(?:\.\d+|\.X|\.XX)?", part):
            continue
        if is_likely_year(part):
            continue
        seen.add(part)
        parts.append(part)
    return parts[:24]


def extract_part_number_candidates(text: str, chunk: dict[str, Any]) -> list[str]:
    norm = normalized_text(text)
    section_norm = normalized_text(" ".join(str(item) for item in chunk.get("section_path") or []))
    context = f"{norm} {section_norm}"
    if not any(
        marker in context
        for marker in (
            "part number",
            "part no",
            "p/n",
            "spare",
            "catalog",
            "accessory",
            "tools",
            "equipment",
            "materials",
            "consumable",
        )
    ):
        return []
    out: list[str] = []
    seen: set[str] = set()
    patterns = (
        r"\b(?:SE|G|SC|AW|F)\s*[-_]?\s*\d{3,8}[A-Z]?\b",
        r"\b\d{5,8}[A-Z]?\b",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            code = re.sub(r"\s+", "", match.group(0).upper())
            if code not in seen:
                seen.add(code)
                out.append(code)
    return out[:48]


def is_likely_year(value: str) -> bool:
    if not re.fullmatch(r"\d{4}", value):
        return False
    number = int(value)
    return 1900 <= number <= 2099


def extract_units(text: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for match in UNIT_PATTERN.finditer(text):
        value = match.group("value").replace(",", ".")
        unit = normalize_unit(match.group("unit"))
        key = (value, unit)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "value": value,
                "unit": unit,
                "evidence_preview": snippet_around(text, match.start(), match.end()),
            }
        )
    return out[:32]


def normalize_unit(unit: str) -> str:
    value = unit.strip()
    lower = value.lower()
    replacements = {
        "ml/hr": "mL/h",
        "ml/h": "mL/h",
        "ml/min": "mL/min",
        "ohm": "Ohm",
        "mohm": "MOhm",
        "kohm": "kOhm",
        "\u03a9": "Ohm",
        "\u03c9": "Ohm",
        "a": "A",
        "ma": "mA",
        "v": "V",
        "mv": "mV",
        "vac": "VAC",
        "vdc": "VDC",
        "hz": "Hz",
        "khz": "kHz",
        "k": "K",
        "l": "L",
        "hr": "h",
        "hrs": "h",
        "hour": "h",
        "hours": "h",
        "sec": "s",
        "secs": "s",
        "s": "s",
        "mins": "min",
        "deg c": "\u00b0C",
        "\u00b0c": "\u00b0C",
    }
    if lower in replacements:
        return replacements[lower]
    if lower == "ml":
        return "mL"
    return value


def extract_tool_mentions(text: str, block_type: str, section_path: object) -> list[dict[str, str]]:
    norm = normalized_text(text)
    section_norm = normalized_text(" ".join(str(item) for item in section_path or []))
    tool_context = any(marker in f"{norm} {section_norm}" for marker in ("tool", "equipment", "fixture", "calibration"))
    if not tool_context and block_type not in TABLE_BLOCK_TYPES and block_type not in PROCEDURE_BLOCK_TYPES:
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for canonical, phrases in TOOL_PHRASES.items():
        for phrase in phrases:
            idx = norm.find(normalized_text(phrase))
            if idx >= 0 and canonical not in seen:
                seen.add(canonical)
                out.append({"tool": canonical, "evidence_preview": evidence_for_phrase(text, phrase)})
                break
    return out[:24]


def extract_technical_terms(text: str) -> list[dict[str, str]]:
    norm = normalized_text(text)
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for canonical, phrases in TECHNICAL_TERMS.items():
        for phrase in phrases:
            if normalized_text(phrase) in norm and canonical not in seen:
                seen.add(canonical)
                out.append({"term": canonical, "evidence_preview": evidence_for_phrase(text, phrase)})
                break
    return out[:24]


def evidence_for_phrase(text: str, phrase: str) -> str:
    match = re.search(re.escape(phrase), text, flags=re.IGNORECASE)
    if not match:
        return clamp_text(repair_mojibake(text), 360)
    return snippet_around(text, match.start(), match.end())


def snippet_around(text: str, start: int, end: int, radius: int = 220) -> str:
    clean = repair_mojibake(text)
    start = max(0, start)
    end = max(start, end)
    left = max(0, start - radius)
    right = min(len(clean), end + radius)
    return clamp_text(clean[left:right], 420)


def build_chunk_ref(chunk: dict[str, Any], citation: dict[str, Any], evidence_text: str) -> dict[str, Any]:
    return {
        "chunk_id": chunk.get("chunk_id"),
        "doc_id": chunk.get("doc_id"),
        "citation_id": citation.get("citation_id"),
        "citation": citation.get("display"),
        "page_start": citation.get("page_start", chunk.get("page_start")),
        "page_end": citation.get("page_end", chunk.get("page_end")),
        "section_path": citation.get("section_path") or chunk.get("section_path") or [],
        "line_start": citation.get("line_start", chunk.get("line_start")),
        "line_end": citation.get("line_end", chunk.get("line_end")),
        "source_file": citation.get("source_file") or chunk.get("source_path") or chunk.get("source_file"),
        "model": chunk.get("model"),
        "doc_type": chunk.get("doc_type"),
        "doc_code": chunk.get("doc_code"),
        "software_version": chunk.get("software_version"),
        "block_type": chunk.get("block_type"),
        "evidence_preview": clamp_text(repair_mojibake(evidence_text), 420),
    }


def build_sidecar_ref(item: dict[str, Any], id_key: str, preview_key: str) -> dict[str, Any]:
    return {
        id_key: item.get(id_key),
        "doc_id": item.get("doc_id"),
        "chunk_id": item.get("chunk_id"),
        "page_start": item.get("page_start"),
        "page_end": item.get("page_end"),
        "section_path": item.get("section_path") or [],
        "confidence": item.get("confidence"),
        "promotion_status": item.get("promotion_status"),
        "evidence_preview": clamp_text(repair_mojibake(str(item.get(preview_key) or item.get("raw_text_preview") or "")), 520),
    }


def entity_list(entity_refs: dict[str, list[dict[str, Any]]], key_name: str) -> list[dict[str, Any]]:
    rows = []
    for key, refs in entity_refs.items():
        rows.append({key_name: key, "ref_count": len(refs), "refs": refs[:MAX_ENTITY_REFS]})
    return sorted(rows, key=lambda item: (-int(item.get("ref_count") or 0), str(item.get(key_name) or "")))


def context_markers(text: str) -> list[str]:
    norm = normalized_text(text)
    return [marker for marker in ALARM_CONTEXT_MARKERS if marker in norm][:6]


def append_relationship(
    relationships: list[dict[str, Any]],
    relationship_type: str,
    target: str,
    ref: dict[str, Any],
    markers: list[str],
) -> None:
    if len(relationships) >= MAX_RELATIONSHIPS:
        return
    relationships.append(
        {
            "relationship_type": relationship_type,
            "target": target,
            "source": {
                "doc_id": ref.get("doc_id"),
                "chunk_id": ref.get("chunk_id"),
                "citation_id": ref.get("citation_id"),
                "page_start": ref.get("page_start"),
                "section_path": ref.get("section_path"),
            },
            "evidence_markers": markers,
            "confidence": "source_context_candidate",
        }
    )


def build_retrieval_aliases(document: dict[str, Any], entities: dict[str, Any]) -> list[str]:
    aliases: list[str] = []
    for key in ("model", "doc_type", "doc_code", "software_version", "title"):
        value = str(document.get(key) or "").strip()
        if value:
            aliases.append(value)
    for row in entities.get("technical_terms", [])[:20]:
        value = str(row.get("term") or "").strip()
        if value:
            aliases.append(value)
    for row in entities.get("tools", [])[:16]:
        value = str(row.get("tool") or "").strip()
        if value:
            aliases.append(value)
    return unique_sorted_aliases(aliases)


def unique_sorted_aliases(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        key = normalized_text(value)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(value)
    return sorted(out, key=lambda item: (len(item), item.lower()))[:48]


def high_signal_sections(
    procedure_refs: list[dict[str, Any]],
    warning_refs: list[dict[str, Any]],
    table_refs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    example: dict[str, dict[str, Any]] = {}
    for ref in [*procedure_refs, *warning_refs, *table_refs]:
        section_path = ref.get("section_path") if isinstance(ref.get("section_path"), list) else []
        section = " > ".join(str(item) for item in section_path[-3:]) or "Document"
        counts[section] += 1
        example.setdefault(
            section,
            {
                "section_path": section_path,
                "page_start": ref.get("page_start"),
                "citation": ref.get("citation"),
            },
        )
    return [
        {"section": section, "signal_count": count, **example.get(section, {})}
        for section, count in counts.most_common(16)
    ]


def add_entity_index(
    index: dict[str, dict[str, Any]],
    rows: object,
    key_name: str,
    report: dict[str, Any],
) -> None:
    if not isinstance(rows, list):
        return
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get(key_name) or "").strip()
        if not key:
            continue
        bucket_key = existing_case_insensitive_key(index, key) or key
        bucket = index.setdefault(bucket_key, {"ref_count": 0, "refs": []})
        refs = [ref for ref in row.get("refs", []) if isinstance(ref, dict)]
        bucket["ref_count"] += int(row.get("ref_count") or len(refs))
        for ref in refs[:MAX_INDEX_REFS]:
            if len(bucket["refs"]) >= MAX_INDEX_REFS:
                break
            bucket["refs"].append(compact_index_ref(ref, report))


def add_alias_index(index: dict[str, dict[str, Any]], alias: str, report: dict[str, Any]) -> None:
    key = alias.strip()
    if not key:
        return
    bucket_key = existing_case_insensitive_key(index, key) or key
    bucket = index.setdefault(bucket_key, {"document_count": 0, "documents": []})
    bucket["document_count"] += 1
    if len(bucket["documents"]) < MAX_INDEX_REFS:
        document = report.get("document") if isinstance(report.get("document"), dict) else {}
        bucket["documents"].append(
            {
                "doc_id": report.get("doc_id"),
                "model": document.get("model"),
                "doc_type": document.get("doc_type"),
                "doc_code": document.get("doc_code"),
                "software_version": document.get("software_version"),
            }
        )


def existing_case_insensitive_key(index: dict[str, Any], key: str) -> str | None:
    wanted = key.lower()
    for existing in index:
        if existing.lower() == wanted:
            return existing
    return None


def compact_index_ref(ref: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    return {
        "doc_id": ref.get("doc_id") or report.get("doc_id"),
        "chunk_id": ref.get("chunk_id"),
        "citation_id": ref.get("citation_id"),
        "page_start": ref.get("page_start"),
        "section_path": ref.get("section_path"),
        "model": ref.get("model"),
        "doc_type": ref.get("doc_type"),
        "doc_code": ref.get("doc_code"),
    }


def write_markdown_report(path: Path, corpus: dict[str, Any]) -> None:
    summary = corpus.get("summary") if isinstance(corpus.get("summary"), dict) else {}
    policy = corpus.get("policy") if isinstance(corpus.get("policy"), dict) else {}
    lines = [
        "# Technical Enrichment Report",
        "",
        f"- Generated at: `{corpus.get('generated_at')}`",
        f"- Documents: `{summary.get('document_count', 0)}`",
        f"- Active retrieval documents: `{summary.get('active_document_count', 0)}`",
        f"- Active chunks reviewed: `{summary.get('active_chunk_count', 0)}`",
        f"- Alarm codes: `{summary.get('alarm_code_count', 0)}`",
        f"- Part numbers: `{summary.get('part_number_count', 0)}`",
        f"- Units: `{summary.get('unit_count', 0)}`",
        f"- Tools: `{summary.get('tool_count', 0)}`",
        f"- Warnings: `{summary.get('warning_chunk_count', 0)}`",
        f"- Procedures: `{summary.get('procedure_chunk_count', 0)}`",
        f"- Tables: `{summary.get('table_count', 0)}`",
        f"- Figures: `{summary.get('figure_count', 0)}`",
        "",
        "## Policy",
        "",
        f"- Source derived only: `{policy.get('source_derived_only')}`",
        f"- LLM generated: `{policy.get('llm_generated')}`",
        f"- Canonical source mutated: `{policy.get('canonical_source_mutated')}`",
        f"- Index scope: `{policy.get('indexes_scope')}`",
        "",
        "## Notes",
        "",
        "- This is a retrieval sidecar. It does not change canonical documents or claim facts beyond cited normalized chunks.",
        "- Use the JSON sidecars for query planning, alias expansion, targeted QA, and future LLM review proposals.",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_run_id() -> str:
    return f"{utc_now().replace(':', '').replace('+', 'Z')}_{short_hash(utc_now(), 8)}"
