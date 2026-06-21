from __future__ import annotations

import os
from typing import Any


DEFAULT_EMBEDDING_MODEL = "local_hash_bm25"
DEFAULT_RECOMMENDED_EMBEDDING_MODEL = "BAAI/bge-m3"
DEFAULT_RERANK_MODEL = "BAAI/bge-reranker-v2-m3"
DEFAULT_LLM_PROVIDER = "deepseek_openai_compatible"
DEFAULT_LLM_MODEL = "deepseek-v4-flash"


def model_profile_payload(index: dict[str, Any] | None = None) -> dict[str, Any]:
    index = index or {}
    rerank_enabled = env_bool("BAXTER_RAG_RERANK_ENABLED", False)
    return {
        "schema_version": "baxter_model_profile_v1",
        "current": {
            "retrieval_backend": index.get("backend") or "local_hybrid",
            "embedding_backend": "hash_dense_plus_bm25",
            "embedding_model": os.getenv("BAXTER_RAG_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
            "rerank_enabled": rerank_enabled,
            "rerank_model": os.getenv("BAXTER_RAG_RERANK_MODEL", DEFAULT_RERANK_MODEL) if rerank_enabled else "none",
            "llm_provider": DEFAULT_LLM_PROVIDER,
            "llm_model": os.getenv("LLM_MODEL", DEFAULT_LLM_MODEL),
        },
        "recommended": {
            "embedding_model": os.getenv("BAXTER_RAG_RECOMMENDED_EMBEDDING_MODEL", DEFAULT_RECOMMENDED_EMBEDDING_MODEL),
            "embedding_reason": "Multilingual Vietnamese/English technical retrieval; supports dense, sparse, and long-document modes.",
            "rerank_model": DEFAULT_RERANK_MODEL,
            "rerank_reason": "Optional multilingual cross-encoder reranker for top-k filtering after measured retrieval misses.",
            "llm_provider": DEFAULT_LLM_PROVIDER,
            "llm_model": DEFAULT_LLM_MODEL,
            "llm_reason": "OpenAI-compatible DeepSeek default for schema-bound cited answers, QA, and audit review.",
            "local_router_later": [
                "Keep current repository-owned retrieval/citation policy.",
                "Route private low-risk review to a local OpenAI-compatible server when configured.",
                "Keep API LLM calls behind explicit external snippet upload gates.",
            ],
        },
        "policy": {
            "qdrant_default": False,
            "rerank_default": False,
            "external_upload_default": False,
            "llm_outputs_are_source_truth": False,
        },
    }


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}
