from __future__ import annotations

import json
import math
import os
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .deduplication import build_duplicate_policy, write_duplicate_policy
from .settings import Settings
from .utils import cosine, hash_embedding, iter_jsonl, normalized_text, read_json, tokenize, utc_now, write_json, write_jsonl


@dataclass
class SearchHit:
    chunk_id: str
    doc_id: str
    content: str
    score: float
    dense_score: float
    sparse_score: float
    dense_rank: int | None
    sparse_rank: int | None
    fused_rank: int
    metadata_boosts: dict[str, float] = field(default_factory=dict)
    citation: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    expanded_context: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def rebuild_index(settings: Settings, *, push_qdrant: bool | None = None) -> dict[str, Any]:
    settings.ensure_dirs()
    normalized_root = settings.artifacts_dir / "normalized"
    chunks: list[dict[str, Any]] = []
    citations_by_chunk: dict[str, dict[str, Any]] = {}
    metadata_by_doc: dict[str, dict[str, Any]] = {}
    duplicate_policy = build_duplicate_policy(settings)
    excluded_doc_ids = set(duplicate_policy.get("excluded_doc_ids", []))

    for metadata_path in sorted(normalized_root.glob("*/metadata.json")):
        payload = read_json(metadata_path, {})
        document = payload.get("document") if isinstance(payload.get("document"), dict) else {}
        if document:
            metadata_by_doc[str(document.get("doc_id"))] = document
    for citation_path in sorted(normalized_root.glob("*/citation_map.json")):
        payload = read_json(citation_path, {})
        for item in payload.get("citations", []):
            if isinstance(item, dict) and item.get("chunk_id"):
                citations_by_chunk[str(item["chunk_id"])] = item
    for chunk_path in sorted(normalized_root.glob("*/chunk_plan.json")):
        if chunk_path.parent.name in excluded_doc_ids:
            continue
        payload = read_json(chunk_path, {})
        for item in payload.get("chunks", []):
            if not isinstance(item, dict) or item.get("low_value"):
                continue
            item["citation"] = citations_by_chunk.get(str(item.get("chunk_id")), {})
            item["document"] = metadata_by_doc.get(str(item.get("doc_id")), {})
            item["retrieval_text"] = retrieval_text(item)
            item["dense_vector"] = hash_embedding(str(item.get("retrieval_text") or item.get("content") or ""))
            chunks.append(item)

    index_dir = settings.artifacts_dir / "index"
    chunks_path = index_dir / "chunks.jsonl"
    meta_path = index_dir / "index_meta.json"
    write_jsonl(chunks_path, chunks)
    meta = {
        "schema_version": "baxter_hybrid_index_v1",
        "generated_at": utc_now(),
        "collection": settings.collection,
        "chunk_count": len(chunks),
        "backend": "local_hybrid",
        "qdrant_enabled": bool(push_qdrant if push_qdrant is not None else env_bool("QDRANT_ENABLED", False)),
        "chunks_path": str(chunks_path),
        "block_counts": dict(Counter(str(item.get("block_type") or "prose") for item in chunks)),
        "model_counts": dict(Counter(str(item.get("model") or "") for item in chunks)),
        "doc_type_counts": dict(Counter(str(item.get("doc_type") or "") for item in chunks)),
    }
    enrichment_path = settings.artifacts_dir / "enrichment" / "technical_enrichment_latest.json"
    if enrichment_path.exists():
        meta["technical_enrichment_path"] = str(enrichment_path)
    qdrant_result = maybe_push_qdrant(settings, chunks, enabled=meta["qdrant_enabled"])
    meta["qdrant"] = qdrant_result
    meta["deduplication_policy_path"] = write_duplicate_policy(settings, duplicate_policy)
    meta["deduplicated_doc_count"] = len(excluded_doc_ids)
    write_json(meta_path, meta)
    return {**meta, "index_meta_path": str(meta_path)}


def search(
    settings: Settings,
    query: str,
    *,
    filters: dict[str, Any] | None = None,
    limit: int | None = None,
    expand_parent: bool = True,
) -> list[SearchHit]:
    filters = filters or {}
    limit = limit or settings.top_k
    chunks = load_chunks(settings)
    candidates = [chunk for chunk in chunks if matches_filters(chunk, filters)]
    if not candidates:
        return []
    bm25 = BM25([str(chunk.get("retrieval_text") or chunk.get("content") or "") for chunk in candidates])
    query_dense = hash_embedding(query)
    dense_ranked: list[tuple[int, float]] = []
    sparse_ranked: list[tuple[int, float]] = []
    for index, chunk in enumerate(candidates):
        dense_ranked.append((index, cosine(query_dense, chunk.get("dense_vector") or [])))
        sparse_ranked.append((index, bm25.score(query, index)))
    dense_sorted = sorted(dense_ranked, key=lambda item: item[1], reverse=True)
    sparse_sorted = sorted(sparse_ranked, key=lambda item: item[1], reverse=True)
    dense_rank = {idx: rank for rank, (idx, _score) in enumerate(dense_sorted, start=1)}
    sparse_rank = {idx: rank for rank, (idx, _score) in enumerate(sparse_sorted, start=1)}
    dense_scores = dict(dense_ranked)
    sparse_scores = dict(sparse_ranked)
    fused = rrf(dense_sorted, sparse_sorted)

    hits: list[SearchHit] = []
    for fused_rank, (idx, score) in enumerate(fused[: max(limit * 3, limit)], start=1):
        chunk = candidates[idx]
        boosts = metadata_boosts(query, chunk, filters)
        final_score = score + sum(boosts.values())
        hit = SearchHit(
            chunk_id=str(chunk.get("chunk_id")),
            doc_id=str(chunk.get("doc_id")),
            content=str(chunk.get("content") or ""),
            score=round(final_score, 6),
            dense_score=round(dense_scores.get(idx, 0.0), 6),
            sparse_score=round(sparse_scores.get(idx, 0.0), 6),
            dense_rank=dense_rank.get(idx),
            sparse_rank=sparse_rank.get(idx),
            fused_rank=fused_rank,
            metadata_boosts=boosts,
            citation=chunk.get("citation") if isinstance(chunk.get("citation"), dict) else {},
            metadata={key: chunk.get(key) for key in ("model", "doc_type", "software_version", "doc_code", "block_type", "section_path")},
        )
        if expand_parent:
            hit.expanded_context = parent_context(candidates, chunk, max_chars=2400)
        hits.append(hit)
    hits.sort(key=lambda item: item.score, reverse=True)
    return hits[:limit]


def load_chunks(settings: Settings) -> list[dict[str, Any]]:
    path = settings.artifacts_dir / "index" / "chunks.jsonl"
    return list(iter_jsonl(path))


def retrieval_text(chunk: dict[str, Any]) -> str:
    parts = [str(chunk.get("content") or "")]
    summary = str(chunk.get("semantic_summary") or "").strip()
    if summary:
        parts.append(f"LLM retrieval summary: {summary}")
    keywords = chunk.get("semantic_keywords")
    if isinstance(keywords, list) and keywords:
        parts.append("LLM retrieval keywords: " + " ".join(str(item) for item in keywords[:30]))
    return "\n".join(part for part in parts if part.strip())


def matches_filters(chunk: dict[str, Any], filters: dict[str, Any]) -> bool:
    for key, value in filters.items():
        if value in (None, "", [], {}):
            continue
        raw = chunk.get(key)
        if key == "software_version" and not raw:
            continue
        if isinstance(value, list):
            if str(raw) not in {str(item) for item in value}:
                return False
        elif str(raw).lower() != str(value).lower():
            return False
    return True


def metadata_boosts(query: str, chunk: dict[str, Any], filters: dict[str, Any]) -> dict[str, float]:
    boosts: dict[str, float] = {}
    q = normalized_text(query)
    for key, weight in (("model", 0.08), ("doc_type", 0.05), ("software_version", 0.08), ("doc_code", 0.16)):
        value = str(chunk.get(key) or "")
        if value and normalized_text(value) in q:
            boosts[key] = weight
        if filters.get(key) and str(filters[key]).lower() == value.lower():
            boosts[f"filter_{key}"] = max(boosts.get(key, 0.0), weight)
    block_type = str(chunk.get("block_type") or "")
    if "alarm" in q and block_type == "alarm_code":
        boosts["alarm_block"] = 0.12
    if any(term in q for term in ("bao tri", "maintenance", "pm checklist")) and block_type == "maintenance_step":
        boosts["maintenance_block"] = 0.1
    if any(term in q for term in ("hieu chuan", "calibration")) and block_type == "calibration_step":
        boosts["calibration_block"] = 0.1
    if any(term in q for term in ("canh bao", "safety", "warning")) and block_type == "warning_safety":
        boosts["safety_block"] = 0.1
    return boosts


def rrf(*rankings: list[tuple[int, float]], k: int = 60) -> list[tuple[int, float]]:
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, (idx, raw_score) in enumerate(ranking, start=1):
            if raw_score <= 0:
                continue
            scores[idx] = scores.get(idx, 0.0) + (1.0 / (k + rank))
    return sorted(scores.items(), key=lambda item: item[1], reverse=True)


def parent_context(chunks: list[dict[str, Any]], selected: dict[str, Any], *, max_chars: int) -> str:
    parent_id = selected.get("parent_section_id")
    doc_id = selected.get("doc_id")
    peers = [
        chunk
        for chunk in chunks
        if chunk.get("doc_id") == doc_id and chunk.get("parent_section_id") == parent_id
    ]
    peers.sort(key=lambda item: int(item.get("line_start") or 0))
    text = "\n\n".join(str(item.get("content") or "") for item in peers)
    if len(text) <= max_chars:
        return text
    selected_text = str(selected.get("content") or "")
    if len(selected_text) >= max_chars:
        return selected_text[:max_chars]
    remain = max_chars - len(selected_text) - 20
    prefix = text[: max(0, remain // 2)]
    suffix = text[-max(0, remain // 2) :]
    return f"{prefix}\n\n[...]\n\n{selected_text}\n\n[...]\n\n{suffix}"


def maybe_push_qdrant(settings: Settings, chunks: list[dict[str, Any]], *, enabled: bool) -> dict[str, Any]:
    if not enabled:
        return {"enabled": False, "status": "skipped"}
    try:
        from qdrant_client import QdrantClient, models
    except Exception as exc:
        return {"enabled": True, "status": "unavailable", "error": f"qdrant_client missing: {exc}"}
    try:
        url = os.getenv("QDRANT_URL", "http://127.0.0.1:6333")
        api_key = os.getenv("QDRANT_API_KEY") or None
        collection = os.getenv("QDRANT_COLLECTION", settings.collection)
        client = QdrantClient(url=url, api_key=api_key)
        dim = len(chunks[0].get("dense_vector") or []) if chunks else 256
        client.recreate_collection(
            collection_name=collection,
            vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE),
        )
        points = []
        for idx, chunk in enumerate(chunks):
            payload = {key: value for key, value in chunk.items() if key != "dense_vector"}
            points.append(models.PointStruct(id=idx + 1, vector=chunk.get("dense_vector") or [], payload=payload))
        if points:
            client.upsert(collection_name=collection, points=points)
        return {"enabled": True, "status": "pushed", "collection": collection, "point_count": len(points)}
    except Exception as exc:
        return {"enabled": True, "status": "failed", "error": str(exc)}


class BM25:
    def __init__(self, documents: list[str], *, k1: float = 1.5, b: float = 0.75) -> None:
        self.documents = [tokenize(document) for document in documents]
        self.k1 = k1
        self.b = b
        self.avgdl = sum(len(document) for document in self.documents) / max(1, len(self.documents))
        self.df: Counter[str] = Counter()
        for document in self.documents:
            self.df.update(set(document))
        self.n = len(self.documents)

    def score(self, query: str, doc_index: int) -> float:
        terms = tokenize(query)
        document = self.documents[doc_index]
        if not document or not terms:
            return 0.0
        counts = Counter(document)
        score = 0.0
        dl = len(document)
        for term in terms:
            if term not in counts:
                continue
            df = self.df.get(term, 0)
            idf = math.log(1 + (self.n - df + 0.5) / (df + 0.5))
            tf = counts[term]
            denom = tf + self.k1 * (1 - self.b + self.b * dl / max(1.0, self.avgdl))
            score += idf * (tf * (self.k1 + 1)) / denom
        return score


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}
