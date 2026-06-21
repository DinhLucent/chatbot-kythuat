from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .models import DocumentRecord
from .settings import Settings
from .utils import read_json, repair_mojibake, safe_filename, sha256_path, utc_now, write_json


@dataclass(frozen=True)
class MinerUProfile:
    name: str
    method: str
    backend: str
    lang: str
    table: bool
    formula: bool
    image_analysis: bool
    effort: str | None = None
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


PROFILES: dict[str, MinerUProfile] = {
    "text_pdf_fast": MinerUProfile(
        name="text_pdf_fast",
        method="auto",
        backend="pipeline",
        lang="ch",
        table=True,
        formula=True,
        image_analysis=True,
        description="CPU-first profile for embedded-text manuals.",
    ),
    "technical_pdf_quality": MinerUProfile(
        name="technical_pdf_quality",
        method="auto",
        backend="pipeline",
        lang="ch",
        table=True,
        formula=True,
        image_analysis=True,
        effort="medium",
        description="Quality profile for service manuals with tables/figures.",
    ),
    "ocr_vi_review": MinerUProfile(
        name="ocr_vi_review",
        method="ocr",
        backend="pipeline",
        lang="ch",
        table=True,
        formula=True,
        image_analysis=True,
        description="Review profile for pages with weak embedded text or Vietnamese OCR risk.",
    ),
}


@dataclass
class ConversionResult:
    doc_id: str
    status: str
    converter: str
    profile: str
    output_markdown: str
    artifact_index: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def convert_manifest(
    settings: Settings,
    *,
    manifest_path: Path | None = None,
    profile: str | None = None,
    limit: int | None = None,
    doc_id: str | None = None,
) -> dict[str, Any]:
    settings.ensure_dirs()
    manifest_path = manifest_path or settings.artifacts_dir / "phase0" / "source_manifest.json"
    payload = read_json(manifest_path, {})
    records = [DocumentRecord(**item) for item in payload.get("records", [])]
    if doc_id:
        requested = {item.strip() for item in doc_id.split(",") if item.strip()}
        records = [record for record in records if record.doc_id in requested]
    if limit is not None:
        records = records[: max(0, limit)]

    results: list[ConversionResult] = []
    for record in records:
        selected_profile = profile or choose_profile(record)
        try:
            result = convert_record(settings, record, profile_name=selected_profile)
        except Exception as exc:
            result = ConversionResult(
                doc_id=record.doc_id,
                status="failed",
                converter="none",
                profile=selected_profile,
                output_markdown="",
                artifact_index="",
                error=str(exc),
            )
        results.append(result)

    summary = {
        "schema_version": "baxter_conversion_run_v1",
        "generated_at": utc_now(),
        "manifest_path": str(manifest_path),
        "results": [result.to_dict() for result in results],
        "summary": {
            "total": len(results),
            "converted": sum(1 for result in results if result.status == "converted"),
            "failed": sum(1 for result in results if result.status == "failed"),
            "native_fallback": sum(1 for result in results if result.converter == "native_pdf"),
            "mineru": sum(1 for result in results if result.converter == "mineru"),
        },
    }
    path = settings.artifacts_dir / "converted" / "conversion_run_latest.json"
    write_json(path, summary)
    return {**summary, "conversion_run_path": str(path)}


def convert_record(settings: Settings, record: DocumentRecord, *, profile_name: str) -> ConversionResult:
    source = Path(record.source_path)
    out_dir = settings.artifacts_dir / "converted" / record.doc_id
    out_dir.mkdir(parents=True, exist_ok=True)
    output_md = out_dir / "raw.md"
    artifact_root = out_dir / "sidecars"
    profile = PROFILES.get(profile_name, PROFILES["text_pdf_fast"])

    mineru_error: str | None = None
    if source.suffix.lower() in {".pdf", ".docx", ".xlsx", ".pptx"}:
        try:
            if mineru_available():
                run_mineru(source, output_md, artifact_root, profile)
                index_path = build_artifact_index(record, output_md, artifact_root, converter="mineru", profile=profile, error=None)
                return ConversionResult(record.doc_id, "converted", "mineru", profile.name, str(output_md), str(index_path))
        except Exception as exc:
            mineru_error = str(exc)

    if source.suffix.lower() == ".pdf":
        native_pdf_to_markdown(source, output_md)
        index_path = build_artifact_index(record, output_md, artifact_root, converter="native_pdf", profile=profile, error=mineru_error)
        return ConversionResult(record.doc_id, "converted", "native_pdf", profile.name, str(output_md), str(index_path), mineru_error)

    if source.suffix.lower() in {".md", ".txt"}:
        text = source.read_text(encoding="utf-8", errors="replace")
        output_md.write_text(repair_mojibake(text).strip() + "\n", encoding="utf-8")
        index_path = build_artifact_index(record, output_md, artifact_root, converter="copy", profile=profile, error=mineru_error)
        return ConversionResult(record.doc_id, "converted", "copy", profile.name, str(output_md), str(index_path), mineru_error)

    raise RuntimeError(f"No converter available for {source.suffix}. MinerU error: {mineru_error}")


def choose_profile(record: DocumentRecord) -> str:
    if record.file_ext == ".pdf" and (record.embedded_text_ratio or 0.0) < 0.05:
        return "ocr_vi_review"
    if record.doc_type in {"service_manual", "service_bulletin", "parts", "installation"}:
        return "technical_pdf_quality"
    return "text_pdf_fast"


def mineru_available() -> bool:
    enabled = os.getenv("MINERU_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
    return enabled and shutil.which(os.getenv("MINERU_EXECUTABLE", "mineru")) is not None


def run_mineru(source: Path, output_md: Path, artifact_root: Path, profile: MinerUProfile) -> None:
    mineru_bin = shutil.which(os.getenv("MINERU_EXECUTABLE", "mineru"))
    if not mineru_bin:
        raise RuntimeError("MinerU CLI not found")
    artifact_root.mkdir(parents=True, exist_ok=True)
    staged_source = stage_ascii_source(source, artifact_root / "_input")
    command = [
        mineru_bin,
        "--path",
        str(staged_source),
        "--output",
        str(artifact_root),
        "--method",
        profile.method,
        "--backend",
        profile.backend,
        "--lang",
        profile.lang,
        "--table",
        str(profile.table).lower(),
        "--formula",
        str(profile.formula).lower(),
        "--image-analysis",
        str(profile.image_analysis).lower(),
    ]
    if profile.effort:
        command.extend(["--effort", profile.effort])
    api_url = os.getenv("MINERU_API_URL", "").strip()
    if api_url:
        command.extend(["--api-url", api_url])
    timeout = int(os.getenv("MINERU_TIMEOUT_SECONDS", "1800") or "1800")
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    markdown_files = sorted(artifact_root.rglob("*.md"), key=lambda item: item.stat().st_mtime, reverse=True)
    if result.returncode != 0 and not markdown_files:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "MinerU conversion failed")
    if not markdown_files:
        raise RuntimeError("MinerU did not produce Markdown")
    shutil.copy2(markdown_files[0], output_md)


def stage_ascii_source(source: Path, input_dir: Path) -> Path:
    input_dir.mkdir(parents=True, exist_ok=True)
    staged = input_dir / f"{safe_filename(source.stem, 56)}-{sha256_path(source)[:8]}{source.suffix.lower()}"
    shutil.copy2(source, staged)
    return staged


def native_pdf_to_markdown(source: Path, output_md: Path) -> None:
    try:
        from pypdf import PdfReader
    except Exception as exc:
        raise RuntimeError("pypdf is required for native PDF fallback") from exc
    reader = PdfReader(str(source))
    lines = [f"# {source.stem}", ""]
    for index, page in enumerate(reader.pages, start=1):
        text = repair_mojibake(page.extract_text() or "").strip()
        if not text:
            lines.extend([f"<!-- page:{index} empty_or_ocr_required -->", ""])
            continue
        lines.extend([f"<!-- page:{index} -->", f"## Page {index}", "", text, ""])
    output_md.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def build_artifact_index(
    record: DocumentRecord,
    output_md: Path,
    artifact_root: Path,
    *,
    converter: str,
    profile: MinerUProfile,
    error: str | None,
) -> Path:
    artifact_root.mkdir(parents=True, exist_ok=True)
    artifacts: list[dict[str, Any]] = []
    for path in sorted(artifact_root.rglob("*")):
        if path.is_file():
            artifacts.append(
                {
                    "path": str(path),
                    "name": path.name,
                    "suffix": path.suffix.lower(),
                    "size_bytes": path.stat().st_size,
                    "checksum": sha256_path(path),
                    "type": classify_artifact(path),
                }
            )
    payload = {
        "schema_version": "baxter_conversion_artifact_index_v1",
        "generated_at": utc_now(),
        "doc_id": record.doc_id,
        "source_path": record.source_path,
        "source_checksum": record.checksum,
        "converter": converter,
        "profile": profile.to_dict(),
        "output_markdown": str(output_md),
        "output_markdown_checksum": sha256_path(output_md) if output_md.exists() else None,
        "mineru_error": error,
        "sidecar_summary": {
            "artifact_count": len(artifacts),
            "image_count": sum(1 for item in artifacts if item["type"] == "image"),
            "json_count": sum(1 for item in artifacts if item["type"] == "json"),
            "has_content_list": any("content_list" in item["name"].lower() for item in artifacts),
            "has_middle_json": any("middle" in item["name"].lower() for item in artifacts),
            "has_model_json": any("model" in item["name"].lower() for item in artifacts),
        },
        "artifacts": artifacts,
        "duration_note": "Conversion is deterministic artifact generation; raw source is not modified.",
    }
    index_path = output_md.parent / "artifact_index.json"
    write_json(index_path, payload)
    return index_path


def classify_artifact(path: Path) -> str:
    name = path.name.lower()
    if name.endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")):
        return "image"
    if name.endswith(".json"):
        return "json"
    if name.endswith(".md"):
        return "markdown"
    if name.endswith(".pdf"):
        return "pdf"
    return "other"
