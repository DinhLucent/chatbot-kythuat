from __future__ import annotations

from typing import Any


SOURCE_WORKSPACES: dict[str, dict[str, Any]] = {
    "prismax": {
        "workspace_id": "prismax",
        "label": "PrisMax",
        "model": "PrisMax",
        "description": "PrisMax manuals, bulletins, addenda, and related source-backed chunks.",
    },
    "prismaflex": {
        "workspace_id": "prismaflex",
        "label": "Prismaflex",
        "model": "Prismaflex",
        "description": "Prismaflex manuals, bulletins, addenda, and related source-backed chunks.",
    },
}


def list_source_workspaces() -> list[dict[str, Any]]:
    return [dict(item) for item in SOURCE_WORKSPACES.values()]


def get_source_workspace(workspace_id: str | None) -> dict[str, Any] | None:
    if not workspace_id:
        return None
    return SOURCE_WORKSPACES.get(str(workspace_id).strip().lower())


def workspace_model(workspace_id: str | None) -> str | None:
    workspace = get_source_workspace(workspace_id)
    if not workspace:
        return None
    return str(workspace.get("model") or "") or None
