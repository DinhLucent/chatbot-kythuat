from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .data_workbench import build_workbench_run_id, promote_conversion_overlay, workbench_root
from .models import DocumentRecord
from .settings import Settings
from .utils import read_json, repair_mojibake, sha256_path, utc_now, write_json


def run_ocr_retry_candidates(
    settings: Settings,
    candidates: list[dict[str, Any]],
    *,
    parent_run_dir: Path | None = None,
    doc_id: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    settings.ensure_dirs()
    run_id = build_workbench_run_id("ocr")
    run_dir = (parent_run_dir / "data_workbench_ocr" / run_id) if parent_run_dir else workbench_root(settings) / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    records = manifest_records_by_doc_id(settings)
    filtered = filter_candidates(candidates, doc_id=doc_id, limit=limit)
    results: list[dict[str, Any]] = []
    for candidate in filtered:
        candidate_doc_id = str(candidate.get("doc_id") or "")
        record = records.get(candidate_doc_id)
        if not record:
            results.append(
                {
                    "doc_id": candidate_doc_id,
                    "status": "skipped",
                    "reason": "manifest_record_missing",
                    "candidate": candidate,
                }
            )
            continue
        try:
            results.append(run_ocr_retry_for_record(settings, record, candidate, run_id=run_id, run_dir=run_dir))
        except Exception as exc:
            results.append(
                {
                    "doc_id": candidate_doc_id,
                    "status": "failed",
                    "reason": type(exc).__name__,
                    "error": str(exc),
                    "candidate": candidate,
                }
            )

    summary = {
        "schema_version": "baxter_ocr_retry_run_v1",
        "generated_at": utc_now(),
        "run_id": run_id,
        "run_dir": str(run_dir),
        "candidate_count": len(filtered),
        "processed_count": len(results),
        "promoted_count": sum(1 for item in results if item.get("status") == "promoted"),
        "failed_count": sum(1 for item in results if item.get("status") == "failed"),
        "skipped_count": sum(1 for item in results if item.get("status") == "skipped"),
        "results": results,
    }
    write_json(run_dir / "report.json", summary)
    write_ocr_markdown_report(run_dir / "report.md", summary)
    write_json(workbench_root(settings) / "latest_ocr_retry_report.json", summary)
    return summary


def run_ocr_retry_for_record(
    settings: Settings,
    record: DocumentRecord,
    candidate: dict[str, Any],
    *,
    run_id: str,
    run_dir: Path,
) -> dict[str, Any]:
    source = Path(record.source_path)
    if source.suffix.lower() != ".pdf":
        return {"doc_id": record.doc_id, "status": "skipped", "reason": "source_not_pdf", "candidate": candidate}
    if not source.exists():
        return {"doc_id": record.doc_id, "status": "failed", "reason": "source_missing", "source_path": str(source)}

    doc_dir = run_dir / record.doc_id
    image_dir = doc_dir / "pages"
    ocr_dir = doc_dir / "paddleocr"
    overlay_dir = doc_dir / "overlay"
    for path in (image_dir, ocr_dir, overlay_dir):
        path.mkdir(parents=True, exist_ok=True)

    raw_md = settings.artifacts_dir / "converted" / record.doc_id / "raw.md"
    raw_text = raw_md.read_text(encoding="utf-8", errors="replace") if raw_md.exists() else ""
    page_count = int(candidate.get("page_count") or record.page_count or 0)
    target_pages = choose_ocr_pages(candidate, raw_text=raw_text, page_count=page_count)
    if not target_pages:
        return {"doc_id": record.doc_id, "status": "skipped", "reason": "no_target_pages", "candidate": candidate}

    rendered = render_pdf_pages(source, image_dir, target_pages)
    if not rendered:
        return {"doc_id": record.doc_id, "status": "failed", "reason": "no_pages_rendered", "candidate": candidate}

    cli = shutil.which(os.getenv("PADDLEOCR_EXECUTABLE", "paddleocr"))
    if not cli:
        return {"doc_id": record.doc_id, "status": "failed", "reason": "paddleocr_cli_not_found", "candidate": candidate}

    ocr_command = build_paddleocr_command(cli, image_dir, ocr_dir)
    command_result = run_command(ocr_command, cwd=settings.root)
    write_json(
        doc_dir / "paddleocr_command.json",
        {
            "command": ocr_command,
            "returncode": command_result.returncode,
            "stdout": command_result.stdout[-12000:],
            "stderr": command_result.stderr[-12000:],
        },
    )
    if command_result.returncode != 0:
        return {
            "doc_id": record.doc_id,
            "status": "failed",
            "reason": "paddleocr_failed",
            "returncode": command_result.returncode,
            "stderr_tail": command_result.stderr[-2000:],
            "candidate": candidate,
        }

    page_results = collect_page_ocr_results(ocr_dir, target_pages)
    existing_pages = split_native_markdown_pages(raw_text)
    overlay_text = build_overlay_markdown(record, page_count, target_pages, existing_pages, page_results)
    overlay_path = overlay_dir / "raw.md"
    overlay_path.write_text(overlay_text, encoding="utf-8")

    quality = summarize_ocr_quality(page_results, overlay_text)
    write_json(doc_dir / "ocr_quality.json", quality)
    promoted = promote_conversion_overlay(
        settings,
        doc_id=record.doc_id,
        run_id=run_id,
        candidate_markdown=overlay_path,
        source_path=record.source_path,
        source_checksum=record.checksum,
        created_by="paddleocr_retry",
        evidence={
            "candidate": candidate,
            "target_pages": target_pages,
            "rendered_pages": [str(item) for item in rendered],
            "ocr_output_dir": str(ocr_dir),
            "quality": quality,
        },
        rollback_note="Remove or replace this promoted conversion overlay, then rerun normalize/rebuild/eval.",
    )
    return {
        "doc_id": record.doc_id,
        "status": "promoted",
        "target_pages": target_pages,
        "overlay_path": str(overlay_path),
        "overlay_checksum": sha256_path(overlay_path),
        "promotion_manifest": promoted.get("manifest_path"),
        "quality": quality,
    }


def manifest_records_by_doc_id(settings: Settings) -> dict[str, DocumentRecord]:
    manifest = read_json(settings.artifacts_dir / "phase0" / "source_manifest.json", {})
    records: dict[str, DocumentRecord] = {}
    for item in manifest.get("records", []) if isinstance(manifest, dict) else []:
        if isinstance(item, dict) and item.get("doc_id"):
            record = DocumentRecord(**item)
            records[record.doc_id] = record
    return records


def filter_candidates(candidates: list[dict[str, Any]], *, doc_id: str | None, limit: int | None) -> list[dict[str, Any]]:
    out = [item for item in candidates if isinstance(item, dict) and item.get("doc_id")]
    if doc_id:
        requested = {item.strip() for item in doc_id.split(",") if item.strip()}
        out = [item for item in out if str(item.get("doc_id")) in requested]
    if limit is not None:
        out = out[: max(0, limit)]
    return out


def choose_ocr_pages(candidate: dict[str, Any], *, raw_text: str, page_count: int) -> list[int]:
    reasons = set(candidate.get("reasons", []) if isinstance(candidate.get("reasons"), list) else [])
    if {"weak_embedded_text", "low_chars_per_page"} & reasons:
        return list(range(1, max(1, page_count) + 1))
    pages = []
    for marker in raw_text.splitlines():
        if "empty_or_ocr_required" not in marker:
            continue
        match = __import__("re").search(r"page:(\d+)", marker)
        if match:
            pages.append(int(match.group(1)))
    return sorted(set(pages))


def render_pdf_pages(source: Path, image_dir: Path, pages: list[int]) -> list[Path]:
    try:
        import pypdfium2 as pdfium
    except Exception as exc:
        raise RuntimeError("pypdfium2 is required for OCR page rendering") from exc

    scale = float(os.getenv("PADDLEOCR_RENDER_SCALE", "2.5") or "2.5")
    rendered: list[Path] = []
    pdf = pdfium.PdfDocument(str(source))
    for page_number in pages:
        index = page_number - 1
        if index < 0 or index >= len(pdf):
            continue
        page = pdf[index]
        bitmap = page.render(scale=scale)
        image = bitmap.to_pil()
        out = image_dir / f"page_{page_number:03d}.png"
        image.save(out)
        rendered.append(out)
    return rendered


def build_paddleocr_command(cli: str, image_dir: Path, ocr_dir: Path) -> list[str]:
    return [
        cli,
        "ocr",
        "-i",
        str(image_dir),
        "--save_path",
        str(ocr_dir),
        "--device",
        os.getenv("PADDLEOCR_DEVICE", "cpu"),
        "--lang",
        os.getenv("PADDLEOCR_LANG", "en"),
        "--use_doc_orientation_classify",
        os.getenv("PADDLEOCR_USE_DOC_ORIENTATION", "false"),
        "--use_doc_unwarping",
        os.getenv("PADDLEOCR_USE_DOC_UNWARPING", "false"),
        "--use_textline_orientation",
        os.getenv("PADDLEOCR_USE_TEXTLINE_ORIENTATION", "false"),
    ]


def run_command(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault("DISABLE_MODEL_SOURCE_CHECK", "True")
    timeout = int(os.getenv("PADDLEOCR_TIMEOUT_SECONDS", "1800") or "1800")
    return subprocess.run(command, cwd=str(cwd), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def collect_page_ocr_results(ocr_dir: Path, target_pages: list[int]) -> dict[int, dict[str, Any]]:
    min_score = float(os.getenv("PADDLEOCR_MIN_SCORE", "0.45") or "0.45")
    results: dict[int, dict[str, Any]] = {}
    for page_number in target_pages:
        json_path = ocr_dir / f"page_{page_number:03d}_res.json"
        payload = read_json(json_path, {})
        texts = payload.get("rec_texts", []) if isinstance(payload, dict) else []
        scores = payload.get("rec_scores", []) if isinstance(payload, dict) else []
        lines: list[str] = []
        line_scores: list[float] = []
        for index, text in enumerate(texts if isinstance(texts, list) else []):
            score = float(scores[index]) if isinstance(scores, list) and index < len(scores) else 1.0
            cleaned = repair_mojibake(text).strip()
            if cleaned and score >= min_score:
                lines.append(cleaned)
                line_scores.append(score)
        results[page_number] = {
            "json_path": str(json_path),
            "line_count": len(lines),
            "text": "\n".join(lines).strip(),
            "avg_score": round(sum(line_scores) / max(1, len(line_scores)), 6),
            "min_score": round(min(line_scores), 6) if line_scores else 0.0,
        }
    return results


def split_native_markdown_pages(raw_text: str) -> dict[int, str]:
    import re

    pages: dict[int, list[str]] = {}
    current: int | None = None
    for line in raw_text.splitlines():
        match = re.search(r"<!--\s*page:(\d+)", line)
        if match:
            current = int(match.group(1))
            pages.setdefault(current, [])
            continue
        if current is not None:
            pages.setdefault(current, []).append(line)
    return {page: "\n".join(lines).strip() for page, lines in pages.items()}


def build_overlay_markdown(
    record: DocumentRecord,
    page_count: int,
    target_pages: list[int],
    existing_pages: dict[int, str],
    ocr_pages: dict[int, dict[str, Any]],
) -> str:
    target_set = set(target_pages)
    lines = [
        f"# {record.title or record.file_name or record.doc_id}",
        "",
        "<!-- baxter:data_workbench conversion_overlay=true created_by=paddleocr_retry -->",
        "",
    ]
    for page_number in range(1, max(1, page_count) + 1):
        if page_number in target_set:
            text = str(ocr_pages.get(page_number, {}).get("text") or "").strip()
            lines.extend([f"<!-- page:{page_number} ocr:paddleocr -->", f"## Page {page_number}", ""])
            if text:
                lines.extend([text, ""])
            else:
                lines.extend([f"<!-- page:{page_number} blank_page_verified_by_ocr -->", ""])
            continue
        existing = existing_pages.get(page_number, "").strip()
        lines.extend([f"<!-- page:{page_number} -->"])
        if existing:
            lines.extend([existing, ""])
        else:
            lines.extend([f"## Page {page_number}", "", f"<!-- page:{page_number} empty_or_ocr_required -->", ""])
    return "\n".join(lines).strip() + "\n"


def summarize_ocr_quality(page_results: dict[int, dict[str, Any]], overlay_text: str) -> dict[str, Any]:
    texts = [str(item.get("text") or "") for item in page_results.values()]
    return {
        "schema_version": "baxter_ocr_quality_v1",
        "generated_at": utc_now(),
        "ocr_page_count": len(page_results),
        "ocr_nonempty_page_count": sum(1 for text in texts if text.strip()),
        "ocr_empty_page_count": sum(1 for text in texts if not text.strip()),
        "ocr_line_count": sum(int(item.get("line_count") or 0) for item in page_results.values()),
        "overlay_char_count": len(overlay_text),
        "avg_page_score": round(sum(float(item.get("avg_score") or 0.0) for item in page_results.values()) / max(1, len(page_results)), 6),
    }


def write_ocr_markdown_report(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# OCR Retry Report",
        "",
        f"- Run id: `{report.get('run_id')}`",
        f"- Candidates: `{report.get('candidate_count')}`",
        f"- Promoted: `{report.get('promoted_count')}`",
        f"- Failed: `{report.get('failed_count')}`",
        f"- Skipped: `{report.get('skipped_count')}`",
        "",
        "| Doc | Status | Pages | Overlay chars | Reason |",
        "|---|---|---:|---:|---|",
    ]
    for item in report.get("results", []):
        quality = item.get("quality", {}) if isinstance(item.get("quality"), dict) else {}
        lines.append(
            f"| `{item.get('doc_id')}` | `{item.get('status')}` | "
            f"{len(item.get('target_pages', [])) if isinstance(item.get('target_pages'), list) else 0} | "
            f"{quality.get('overlay_char_count', '')} | `{item.get('reason', '')}` |"
        )
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
