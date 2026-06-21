from __future__ import annotations

from collections import defaultdict
from typing import Any

from .metadata import infer_doc_code_from_record
from .settings import Settings
from .utils import read_json, utc_now, write_json


def build_duplicate_policy(settings: Settings) -> dict[str, Any]:
    manifest = read_json(settings.artifacts_dir / "phase0" / "source_manifest.json", {})
    records = [item for item in manifest.get("records", []) if isinstance(item, dict) and item.get("checksum")]
    groups_by_checksum: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        groups_by_checksum[str(record.get("checksum"))].append(record)

    groups: list[dict[str, Any]] = []
    excluded_doc_ids: list[str] = []
    for checksum, group in sorted(groups_by_checksum.items()):
        if len(group) < 2:
            continue
        canonical = choose_canonical_record(group)
        excluded = sorted(str(item.get("doc_id")) for item in group if item.get("doc_id") != canonical.get("doc_id"))
        excluded_doc_ids.extend(excluded)
        groups.append(
            {
                "checksum": checksum,
                "canonical_doc_id": canonical.get("doc_id"),
                "excluded_doc_ids": excluded,
                "members": [
                    {
                        "doc_id": item.get("doc_id"),
                        "model": item.get("model"),
                        "doc_type": item.get("doc_type"),
                        "doc_code": infer_doc_code_from_record(item),
                        "file_name": item.get("file_name"),
                        "source_path": item.get("source_path"),
                    }
                    for item in sorted(group, key=lambda row: str(row.get("doc_id")))
                ],
                "reason": "Exact checksum duplicate; canonical selected by metadata completeness and stable filename/doc_id ordering.",
                "rollback_note": "Remove this policy or rebuild the index without duplicate exclusion to restore all duplicate chunks.",
            }
        )
    return {
        "schema_version": "baxter_duplicate_policy_v1",
        "generated_at": utc_now(),
        "groups": groups,
        "excluded_doc_ids": sorted(set(excluded_doc_ids)),
        "group_count": len(groups),
        "excluded_count": len(set(excluded_doc_ids)),
    }


def choose_canonical_record(group: list[dict[str, Any]]) -> dict[str, Any]:
    return sorted(group, key=canonical_score)[0]


def canonical_score(record: dict[str, Any]) -> tuple[int, int, str]:
    score = 0
    if str(record.get("model") or "").lower() in {"unknown", ""}:
        score += 20
    if str(record.get("doc_type") or "") == "technical_document":
        score += 10
    if not infer_doc_code_from_record(record):
        score += 8
    file_name = str(record.get("file_name") or "")
    if "-1-" in file_name or "_1" in file_name:
        score += 2
    if "-2-" in file_name or "_2" in file_name:
        score += 3
    return (score, len(file_name), str(record.get("doc_id") or ""))


def write_duplicate_policy(settings: Settings, policy: dict[str, Any]) -> str:
    path = settings.artifacts_dir / "index" / "deduplication_policy.json"
    write_json(path, policy)
    return str(path)
