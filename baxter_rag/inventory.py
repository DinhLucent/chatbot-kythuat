from __future__ import annotations

import re
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .metadata import infer_doc_code_from_values
from .models import DocumentRecord
from .settings import Settings
from .utils import keyword_codes, safe_filename, sha256_path, short_hash, utc_now, write_json


BAXTER_ROOT = Path("documents") / "IC" / "BAXTER"
SUPPORTED_SUFFIXES = {".pdf", ".docx", ".xlsx", ".pptx", ".txt", ".md"}


@dataclass
class InventoryResult:
    corpus_inventory_path: str
    taxonomy_map_path: str
    source_manifest_path: str
    data_readiness_report_path: str
    document_count: int
    package_count: int
    staged_file_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_inventory(settings: Settings, *, relative_root: str = str(BAXTER_ROOT)) -> InventoryResult:
    settings.ensure_dirs()
    source_root = settings.root / relative_root
    if not source_root.exists():
        raise FileNotFoundError(f"Source corpus not found: {source_root}")

    phase_dir = settings.artifacts_dir / "phase0"
    staging_packages = settings.staging_dir / "packages"
    documents: list[DocumentRecord] = []
    packages: list[dict[str, Any]] = []
    staged_files: list[dict[str, Any]] = []

    for path in sorted(source_root.rglob("*")):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix == ".zip":
            package_info, extracted = stage_zip_package(settings, path, staging_packages)
            packages.append(package_info)
            staged_files.extend(extracted)
            for item in extracted:
                extracted_path = Path(item["staged_path"])
                if extracted_path.suffix.lower() in SUPPORTED_SUFFIXES:
                    documents.append(build_record(settings, extracted_path, source_package=str(path)))
            continue
        if suffix in SUPPORTED_SUFFIXES:
            documents.append(build_record(settings, path))

    uniquify_doc_ids(documents)
    taxonomy = build_taxonomy(documents)
    readiness = build_readiness_report(documents, packages, staged_files)
    inventory_payload = {
        "schema_version": "baxter_corpus_inventory_v1",
        "generated_at": utc_now(),
        "source_root": str(source_root),
        "documents": [record.to_dict() for record in documents],
        "packages": packages,
        "staged_files": staged_files,
    }
    manifest_payload = {
        "schema_version": "baxter_source_manifest_v1",
        "generated_at": utc_now(),
        "records": [record.to_dict() for record in documents],
    }

    corpus_inventory_path = phase_dir / "corpus_inventory.json"
    taxonomy_map_path = phase_dir / "taxonomy_map.json"
    source_manifest_path = phase_dir / "source_manifest.json"
    data_readiness_report_path = phase_dir / "data_readiness_report.json"
    write_json(corpus_inventory_path, inventory_payload)
    write_json(taxonomy_map_path, taxonomy)
    write_json(source_manifest_path, manifest_payload)
    write_json(data_readiness_report_path, readiness)

    return InventoryResult(
        corpus_inventory_path=str(corpus_inventory_path),
        taxonomy_map_path=str(taxonomy_map_path),
        source_manifest_path=str(source_manifest_path),
        data_readiness_report_path=str(data_readiness_report_path),
        document_count=len(documents),
        package_count=len(packages),
        staged_file_count=len(staged_files),
    )


def uniquify_doc_ids(records: list[DocumentRecord]) -> None:
    seen: dict[str, int] = {}
    for record in records:
        base = record.doc_id
        count = seen.get(base, 0) + 1
        seen[base] = count
        if count == 1:
            continue
        suffix = short_hash(f"{record.source_path}:{record.checksum}:{count}", 6)
        record.doc_id = f"{base}-{suffix}"


def stage_zip_package(settings: Settings, zip_path: Path, staging_root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    package_id = f"pkg_{safe_filename(zip_path.stem, 48)}_{short_hash(str(zip_path), 8)}"
    target_root = staging_root / package_id
    target_root.mkdir(parents=True, exist_ok=True)
    extracted: list[dict[str, Any]] = []
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            member_name = info.filename.replace("\\", "/")
            safe_parts = [safe_filename(part, 80) for part in member_name.split("/") if part and part not in {".", ".."}]
            if not safe_parts:
                continue
            staged_path = target_root.joinpath(*safe_parts)
            staged_path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, staged_path.open("wb") as destination:
                destination.write(source.read())
            extracted.append(
                {
                    "package_id": package_id,
                    "source_package": str(zip_path),
                    "zip_member": member_name,
                    "staged_path": str(staged_path),
                    "suffix": staged_path.suffix.lower(),
                    "size_bytes": info.file_size,
                    "checksum": sha256_path(staged_path),
                }
            )
    package_info = {
        "package_id": package_id,
        "source_path": str(zip_path),
        "file_name": zip_path.name,
        "size_bytes": zip_path.stat().st_size,
        "checksum": sha256_path(zip_path),
        "member_count": len(extracted),
        "staged_root": str(target_root),
    }
    return package_info, extracted


def build_record(settings: Settings, path: Path, *, source_package: str | None = None) -> DocumentRecord:
    relative = path.relative_to(settings.root) if path.is_relative_to(settings.root) else path
    parts = [part.upper() for part in relative.parts]
    machine_line = infer_machine_line(path, parts)
    model = infer_model(path, machine_line)
    doc_type = infer_doc_type(path)
    pdf_stats = pdf_text_stats(path) if path.suffix.lower() == ".pdf" else {}
    title = infer_title(path, pdf_stats)
    doc_code = infer_doc_code(path, title=title, sample_text=str(pdf_stats.get("sample_text") or ""))
    software_version = infer_software_version(path)
    revision = infer_revision(path)
    language = infer_language(path)
    checksum = sha256_path(path)
    doc_id = build_doc_id(path, model, doc_type, doc_code, checksum)
    return DocumentRecord(
        doc_id=doc_id,
        brand="BAXTER",
        business_unit="IC",
        machine_line=machine_line,
        model=model,
        doc_type=doc_type,
        doc_code=doc_code,
        software_version=software_version,
        revision=revision,
        language=language,
        status="active",
        source_path=str(path),
        checksum=checksum,
        source_package=source_package,
        file_name=path.name,
        file_ext=path.suffix.lower(),
        size_bytes=path.stat().st_size,
        page_count=pdf_stats.get("page_count"),
        embedded_text_ratio=pdf_stats.get("embedded_text_ratio"),
        title=title,
        extra={
            "relative_path": str(relative),
            "keyword_codes": keyword_codes(path.name),
            "sample_text": pdf_stats.get("sample_text", ""),
        },
    )


def pdf_text_stats(path: Path) -> dict[str, Any]:
    try:
        from pypdf import PdfReader
    except Exception as exc:
        return {"pdf_error": f"pypdf_unavailable: {exc}"}
    try:
        reader = PdfReader(str(path))
        page_count = len(reader.pages)
        sample_chars = 0
        sample_text_parts: list[str] = []
        sample_pages = min(page_count, 8)
        for page in reader.pages[:sample_pages]:
            text = (page.extract_text() or "").strip()
            sample_chars += len(text)
            if text and len(" ".join(sample_text_parts)) < 1400:
                sample_text_parts.append(" ".join(text.split())[:600])
        expected = max(1, sample_pages * 600)
        ratio = min(1.0, sample_chars / expected)
        return {
            "page_count": page_count,
            "embedded_text_ratio": round(ratio, 4),
            "sample_text_chars": sample_chars,
            "sample_text": "\n".join(sample_text_parts)[:1800],
        }
    except Exception as exc:
        return {"pdf_error": repr(exc)}


def infer_machine_line(path: Path, parts: list[str]) -> str:
    text = " ".join([path.name.upper(), *parts])
    if "PRISMAFLEX" in text:
        return "C-PRISMAFLEX"
    if "PRISMAX" in text:
        return "PRISMAX"
    if "THERMAX" in text:
        return "THERMAX"
    return "UNKNOWN"


def infer_model(path: Path, machine_line: str) -> str:
    text = path.name.upper()
    if "THERMAX" in text:
        return "TherMax"
    if "PRISMAX" in text or machine_line == "PRISMAX":
        return "PrisMax"
    if "PRISMAFLEX" in text or machine_line == "C-PRISMAFLEX":
        return "Prismaflex"
    return machine_line.title() if machine_line != "UNKNOWN" else "Unknown"


def infer_doc_type(path: Path) -> str:
    text = path.name.lower()
    parent = str(path.parent).lower()
    if "service bulletin" in parent or "tsb" in text or re.search(r"\bl\d{4}\b", text):
        return "service_bulletin"
    if "service_manual" in text or "service manual" in text:
        return "service_manual"
    if "operator" in text or "_op_" in text or "operator" in parent or "om_" in text:
        return "operator_manual"
    if "addendum" in text:
        return "addendum"
    if "checklist" in text:
        return "checklist"
    if "spare" in text or "parts" in text:
        return "parts"
    if "install" in text or "installation" in text:
        return "installation"
    return "technical_document"


def infer_doc_code(path: Path, *, title: str = "", sample_text: str = "") -> str:
    return infer_doc_code_from_values(path.name, title, sample_text)


def infer_software_version(path: Path) -> str:
    text = path.name
    match = re.search(r"(?:SW[_ -]?)?(\d+(?:\.\d+|\.x|\.xx))", text, flags=re.IGNORECASE)
    if match:
        return match.group(1).upper()
    return ""


def infer_revision(path: Path) -> str:
    text = path.name
    match = re.search(r"\b(?:Rev|Revision)[_ -]?([A-Z0-9.-]+)\b", text, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(r"\b([A-Z])(?=\.[Pp][Dd][Ff]$)", text)
    return match.group(1) if match else ""


def infer_language(path: Path) -> str:
    text = path.name.upper()
    if "_VI" in text or "VI_" in text or "VI-" in text or "VI." in text:
        return "vi"
    if any(marker in text for marker in ("_EL_", "_KO_", "_TR_", "EL_VI_KO_TR")):
        return "multi"
    return "en"


def infer_title(path: Path, stats: dict[str, Any]) -> str:
    sample = str(stats.get("sample_text") or "").strip()
    if sample:
        first = sample.splitlines()[0].strip()
        if 8 <= len(first) <= 160:
            return first
    return path.stem


def build_doc_id(path: Path, model: str, doc_type: str, doc_code: str, checksum: str) -> str:
    code = doc_code or short_hash(checksum, 8).upper()
    return safe_filename(f"{model}_{doc_type}_{code}", 72).lower()


def build_taxonomy(records: list[DocumentRecord]) -> dict[str, Any]:
    taxonomy: dict[str, Any] = {
        "schema_version": "baxter_taxonomy_v1",
        "generated_at": utc_now(),
        "business_units": {},
    }
    unit = taxonomy["business_units"].setdefault("IC", {"brands": {}})
    brand = unit["brands"].setdefault("BAXTER", {"machine_lines": {}})
    for record in records:
        line = brand["machine_lines"].setdefault(record.machine_line, {"models": {}})
        model = line["models"].setdefault(record.model, {"doc_types": {}, "software_versions": set()})
        model["doc_types"][record.doc_type] = model["doc_types"].get(record.doc_type, 0) + 1
        if record.software_version:
            model["software_versions"].add(record.software_version)
    for line in brand["machine_lines"].values():
        for model in line["models"].values():
            model["software_versions"] = sorted(model["software_versions"])
    return taxonomy


def build_readiness_report(
    records: list[DocumentRecord],
    packages: list[dict[str, Any]],
    staged_files: list[dict[str, Any]],
) -> dict[str, Any]:
    missing_text = [
        record.doc_id
        for record in records
        if record.file_ext == ".pdf" and (record.embedded_text_ratio or 0.0) < 0.05
    ]
    missing_code = [record.doc_id for record in records if not record.doc_code]
    duplicates: dict[str, list[str]] = {}
    for record in records:
        duplicates.setdefault(record.checksum, []).append(record.doc_id)
    duplicate_groups = [items for items in duplicates.values() if len(items) > 1]
    return {
        "schema_version": "baxter_data_readiness_v1",
        "generated_at": utc_now(),
        "summary": {
            "document_count": len(records),
            "package_count": len(packages),
            "staged_file_count": len(staged_files),
            "pdf_page_count": sum(record.page_count or 0 for record in records),
            "missing_doc_code_count": len(missing_code),
            "weak_embedded_text_count": len(missing_text),
            "duplicate_checksum_groups": len(duplicate_groups),
        },
        "quality_gates": {
            "raw_source_immutable": True,
            "zip_staged_outside_documents": True,
            "manifest_created": bool(records),
            "needs_conversion": True,
            "needs_normalization": True,
            "needs_index": True,
        },
        "issues": {
            "missing_doc_code": missing_code[:50],
            "weak_embedded_text": missing_text[:50],
            "duplicate_checksum_groups": duplicate_groups[:20],
        },
        "next_actions": [
            "Run conversion using MinerU where available; native PDF fallback is acceptable for text-rich PDFs.",
            "Normalize into technical manual block types and citation maps.",
            "Build hybrid retrieval index and run benchmark.",
        ],
    }
