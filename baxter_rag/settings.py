from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    root: Path
    documents_dir: Path
    artifacts_dir: Path
    staging_dir: Path
    logs_dir: Path
    collection: str
    top_k: int
    max_context_chars: int
    llm_enrichment_enabled: bool

    @classmethod
    def from_env(cls, root: Path | None = None) -> "Settings":
        base = root or Path(os.getenv("BAXTER_RAG_ROOT", ".")).resolve()
        return cls(
            root=base,
            documents_dir=base / "documents",
            artifacts_dir=base / "artifacts",
            staging_dir=base / "staging",
            logs_dir=base / "logs",
            collection=os.getenv("BAXTER_RAG_COLLECTION", "ic_baxter"),
            top_k=_env_int("BAXTER_RAG_TOP_K", 8),
            max_context_chars=_env_int("BAXTER_RAG_MAX_CONTEXT_CHARS", 9000),
            llm_enrichment_enabled=_env_bool("BAXTER_RAG_LLM_ENRICHMENT_ENABLED", False),
        )

    def ensure_dirs(self) -> None:
        for path in (self.artifacts_dir, self.staging_dir, self.logs_dir):
            path.mkdir(parents=True, exist_ok=True)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("true", "1", "yes", "on")

