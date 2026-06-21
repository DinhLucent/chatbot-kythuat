from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from .settings import Settings
from .utils import read_json, sha256_path, short_hash, utc_now, write_json


def workbench_root(settings: Settings) -> Path:
    return settings.artifacts_dir / "data_workbench"


def build_workbench_run_id(prefix: str) -> str:
    stamp = utc_now().replace(":", "").replace("-", "").replace("+0000", "Z")
    return f"{prefix}_{stamp}_{short_hash(stamp, 6)}"


def promoted_conversion_overlay_dir(settings: Settings, doc_id: str) -> Path:
    return workbench_root(settings) / "promoted" / "conversion_overlays" / doc_id


def find_promoted_conversion_overlay(settings: Settings, doc_id: str) -> dict[str, Any] | None:
    manifest_path = promoted_conversion_overlay_dir(settings, doc_id) / "manifest.json"
    manifest = read_json(manifest_path, {})
    if not isinstance(manifest, dict) or not manifest:
        return None
    raw_markdown = Path(str(manifest.get("raw_markdown") or ""))
    if not raw_markdown.exists():
        return None
    return {**manifest, "manifest_path": str(manifest_path)}


def promote_conversion_overlay(
    settings: Settings,
    *,
    doc_id: str,
    run_id: str,
    candidate_markdown: Path,
    source_path: str,
    source_checksum: str | None,
    created_by: str,
    evidence: dict[str, Any],
    rollback_note: str,
) -> dict[str, Any]:
    if not candidate_markdown.exists():
        raise FileNotFoundError(f"Conversion overlay candidate missing: {candidate_markdown}")

    dest_dir = promoted_conversion_overlay_dir(settings, doc_id)
    archive_dir = dest_dir / "archive"
    dest_dir.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir(parents=True, exist_ok=True)

    previous_manifest = find_promoted_conversion_overlay(settings, doc_id)
    previous_raw = Path(str(previous_manifest.get("raw_markdown"))) if previous_manifest else None
    archived_previous: str | None = None
    if previous_raw and previous_raw.exists():
        archived = archive_dir / f"raw_{short_hash(utc_now() + doc_id, 10)}.md"
        shutil.copy2(previous_raw, archived)
        archived_previous = str(archived)

    raw_dest = dest_dir / "raw.md"
    shutil.copy2(candidate_markdown, raw_dest)
    manifest = {
        "schema_version": "baxter_conversion_overlay_v1",
        "generated_at": utc_now(),
        "doc_id": doc_id,
        "run_id": run_id,
        "created_by": created_by,
        "source_path": source_path,
        "source_checksum": source_checksum,
        "raw_markdown": str(raw_dest),
        "raw_markdown_checksum": sha256_path(raw_dest),
        "candidate_markdown": str(candidate_markdown),
        "candidate_markdown_checksum": sha256_path(candidate_markdown),
        "previous_overlay_manifest": previous_manifest.get("manifest_path") if previous_manifest else None,
        "archived_previous_raw": archived_previous,
        "evidence": evidence,
        "rollback_note": rollback_note,
        "promotion_status": "promoted",
    }
    manifest_path = dest_dir / "manifest.json"
    write_json(manifest_path, manifest)
    append_jsonl(
        workbench_root(settings) / "promoted" / "ledger.jsonl",
        {
            "schema_version": "baxter_workbench_promotion_event_v1",
            "event_id": f"promote_{short_hash(doc_id + run_id + utc_now(), 14)}",
            "created_at": utc_now(),
            "artifact_type": "conversion_overlay",
            "doc_id": doc_id,
            "run_id": run_id,
            "manifest_path": str(manifest_path),
            "raw_markdown": str(raw_dest),
            "created_by": created_by,
            "rollback_note": rollback_note,
        },
    )
    return {**manifest, "manifest_path": str(manifest_path)}


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
