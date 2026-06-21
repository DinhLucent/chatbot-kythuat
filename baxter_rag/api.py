from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .chat import ask, create_session, source_preview
from .conversion import convert_manifest
from .evaluation import generate_seed_cases, run_eval
from .inventory import run_inventory
from .llm import DEFAULT_LLM_MODEL, OpenAICompatibleLLM, is_local_llm_endpoint, safe_base_url
from .model_profile import model_profile_payload
from .normalization import normalize_manifest
from .pipeline import PipelineOptions, run_automation_pipeline
from .quality_audit import QualityAuditOptions, run_quality_audit
from .quality_loop import QualityLoopOptions, run_quality_loop
from .retrieval import rebuild_index, search
from .semantic_review import SemanticReviewOptions, run_apodex_semantic_review
from .settings import Settings
from .utils import read_json, utc_now, write_json
from .workspaces import list_source_workspaces


app = FastAPI(title="Baxter IC Technical RAG", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class InventoryRequest(BaseModel):
    relative_root: str = "documents/IC/BAXTER"


class ConvertRequest(BaseModel):
    manifest_path: str | None = None
    profile: str | None = None
    limit: int | None = None
    doc_id: str | None = None


class NormalizeRequest(BaseModel):
    manifest_path: str | None = None
    limit: int | None = None
    doc_id: str | None = None


class RebuildRequest(BaseModel):
    push_qdrant: bool | None = None


class SessionRequest(BaseModel):
    source_workspace_id: str | None = None
    model: str | None = None
    doc_type: str | None = None
    software_version: str | None = None


class ChatRequest(BaseModel):
    question: str = Field(min_length=1)
    session_id: str | None = None
    source_workspace_id: str | None = None
    model: str | None = None
    doc_type: str | None = None
    software_version: str | None = None
    use_llm: bool = True
    allow_external_llm: bool | None = None


class FeedbackRequest(BaseModel):
    session_id: str | None = None
    question: str
    answer: str | None = None
    rating: str
    comment: str | None = None
    citation_ids: list[str] = []


class SearchRequest(BaseModel):
    query: str
    filters: dict[str, Any] = {}
    limit: int | None = None


class LLMTestRequest(BaseModel):
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    timeout_seconds: float | None = None


class PipelineRunRequest(BaseModel):
    relative_root: str = "documents/IC/BAXTER"
    profile: str | None = None
    limit: int | None = None
    doc_id: str | None = None
    rebuild: bool = True
    eval_count: int = 96
    eval_limit: int | None = None
    enable_paddleocr: bool | None = None
    enable_hyper_extract: bool | None = None
    enable_llm_review: bool | None = None
    allow_external_llm: bool | None = None
    enable_apodex: bool | None = None
    enable_quality_audit: bool = True


class QualityAuditRequest(BaseModel):
    doc_id: str | None = None
    include_low_risk: bool = True
    max_chunk_issues_per_doc: int = 40
    strict: bool = False


class SemanticReviewRequest(BaseModel):
    doc_id: str | None = None
    limit: int = 12
    enable_llm_review: bool | None = None
    enable_apodex: bool | None = None
    allow_external: bool | None = None


class QualityLoopRequest(BaseModel):
    doc_id: str | None = None
    max_iterations: int = 3
    review_limit: int = 12
    enable_llm_review: bool | None = None
    enable_apodex: bool | None = None
    allow_external: bool | None = None
    apply_mode: str = "proposal_only"
    stop_when_no_candidates: bool = True


def settings() -> Settings:
    return Settings.from_env(Path.cwd())


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def infer_llm_provider(base_url: str, model: str = "") -> str:
    lowered = f"{base_url} {model}".lower()
    if "deepseek" in lowered:
        return "deepseek"
    if is_local_llm_endpoint(base_url):
        return "local_openai_compatible"
    if "openai" in lowered:
        return "openai_compatible"
    return "openai_compatible"


def llm_settings_payload() -> dict[str, Any]:
    base_url = os.getenv("LLM_BASE_URL", "").strip()
    model = os.getenv("LLM_MODEL", DEFAULT_LLM_MODEL).strip() or DEFAULT_LLM_MODEL
    api_key_set = bool(os.getenv("LLM_API_KEY", "").strip())
    return {
        "base_url": safe_base_url(base_url),
        "model": model,
        "timeout_seconds": float(os.getenv("LLM_TIMEOUT_SECONDS", "60") or "60"),
        "configured": bool(base_url and model and api_key_set),
        "api_key_set": api_key_set,
        "provider_preset": infer_llm_provider(base_url, model),
        "external_upload_allowed": env_bool("LLM_EXTERNAL_UPLOAD_ALLOWED", False),
        "semantic_review_enabled": env_bool("LLM_SEMANTIC_REVIEW_ENABLED", False),
        "answer_qa_enabled": env_bool("LLM_ANSWER_QA_ENABLED", False),
        "enrichment_enabled": env_bool("BAXTER_RAG_LLM_ENRICHMENT_ENABLED", False),
        "local_endpoint": is_local_llm_endpoint(base_url) if base_url else False,
    }


def provider_presets_payload() -> list[dict[str, Any]]:
    return [
        {
            "id": "deepseek",
            "label": "DeepSeek API",
            "recommended": True,
            "base_url": "https://api.deepseek.com",
            "recommended_model": DEFAULT_LLM_MODEL,
            "models": ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner"],
            "note": "OpenAI-compatible API. Use flash for everyday cited answers, pro for harder reasoning.",
        },
        {
            "id": "openai_compatible",
            "label": "OpenAI-compatible",
            "recommended": False,
            "base_url": "",
            "recommended_model": "",
            "models": [],
            "note": "Use for any provider that supports /chat/completions.",
        },
        {
            "id": "ollama",
            "label": "Ollama local",
            "recommended": False,
            "base_url": "http://127.0.0.1:11434/v1",
            "recommended_model": "qwen3:4b",
            "models": ["qwen3:4b", "qwen3:8b"],
            "note": "Private local fallback; set a dummy API key if your OpenAI-compatible server requires the header.",
        },
        {
            "id": "lmstudio",
            "label": "LM Studio local",
            "recommended": False,
            "base_url": "http://127.0.0.1:1234/v1",
            "recommended_model": "local-model",
            "models": ["local-model"],
            "note": "Useful for offline experiments with GGUF models.",
        },
    ]


def settings_payload() -> dict[str, Any]:
    s = settings()
    index = read_json(s.artifacts_dir / "index" / "index_meta.json", {})
    inventory = read_json(s.artifacts_dir / "phase0" / "data_readiness_report.json", {})
    return {
        "llm": llm_settings_payload(),
        "provider_presets": provider_presets_payload(),
        "retrieval": {
            "collection": s.collection,
            "top_k": s.top_k,
            "max_context_chars": s.max_context_chars,
            "backend": index.get("backend") or "local_hybrid",
            "qdrant": index.get("qdrant", {}),
        },
        "inventory": inventory,
        "index": index,
        "model_profile": model_profile_payload(index),
        "workspaces": list_source_workspaces(),
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/status")
def status() -> dict[str, Any]:
    s = settings()
    payload = settings_payload()
    return {
        "status": "ok",
        "root": str(s.root),
        "llm": payload["llm"],
        "inventory": payload["inventory"],
        "index": payload["index"],
        "workspaces": payload["workspaces"],
    }


@app.get("/api/workspaces")
def api_workspaces() -> dict[str, Any]:
    return {"workspaces": list_source_workspaces()}


@app.get("/api/settings")
def api_settings() -> dict[str, Any]:
    return settings_payload()


@app.post("/api/settings/llm/test")
def api_llm_test(payload: LLMTestRequest) -> dict[str, Any]:
    base_url = (payload.base_url if payload.base_url is not None else os.getenv("LLM_BASE_URL", "")).strip()
    model = (payload.model if payload.model is not None else os.getenv("LLM_MODEL", DEFAULT_LLM_MODEL)).strip()
    api_key = payload.api_key if payload.api_key is not None and payload.api_key.strip() else os.getenv("LLM_API_KEY", "")
    timeout = payload.timeout_seconds if payload.timeout_seconds is not None else float(os.getenv("LLM_TIMEOUT_SECONDS", "60") or "60")
    safe_payload = {
        "base_url": safe_base_url(base_url),
        "model": model,
        "provider_preset": infer_llm_provider(base_url, model),
        "api_key_source": "request" if payload.api_key else "environment",
    }
    if not base_url or not model:
        return {**safe_payload, "ok": False, "status": "not_configured", "reason": "base_url_or_model_missing"}
    if not api_key.strip():
        return {**safe_payload, "ok": False, "status": "not_configured", "reason": "api_key_missing"}
    client = OpenAICompatibleLLM(
        base_url=base_url,
        api_key=api_key.strip(),
        model=model,
        timeout=timeout,
        provider_name=infer_llm_provider(base_url, model),
    )
    try:
        reply = client.complete(
            [
                {"role": "system", "content": "Reply with exactly OK."},
                {"role": "user", "content": "Connection test"},
            ],
            temperature=0,
            max_tokens=8,
        )
        return {
            **safe_payload,
            "ok": True,
            "status": "passed",
            "response_preview": reply[:200],
            "usage": client.usage.to_dict(),
        }
    except Exception as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        text = getattr(getattr(exc, "response", None), "text", "")
        return {
            **safe_payload,
            "ok": False,
            "status": "failed",
            "http_status": status_code,
            "error": str(exc),
            "response_preview": str(text)[:300],
        }


@app.post("/api/documents/inventory")
def api_inventory(payload: InventoryRequest) -> dict[str, Any]:
    return run_inventory(settings(), relative_root=payload.relative_root).to_dict()


@app.post("/api/documents/convert")
def api_convert(payload: ConvertRequest) -> dict[str, Any]:
    return convert_manifest(
        settings(),
        manifest_path=Path(payload.manifest_path) if payload.manifest_path else None,
        profile=payload.profile,
        limit=payload.limit,
        doc_id=payload.doc_id,
    )


@app.post("/api/documents/normalize")
def api_normalize(payload: NormalizeRequest) -> dict[str, Any]:
    return normalize_manifest(
        settings(),
        manifest_path=Path(payload.manifest_path) if payload.manifest_path else None,
        limit=payload.limit,
        doc_id=payload.doc_id,
    )


@app.post("/api/index/rebuild")
def api_rebuild(payload: RebuildRequest) -> dict[str, Any]:
    return rebuild_index(settings(), push_qdrant=payload.push_qdrant)


@app.post("/api/index/search")
def api_search(payload: SearchRequest) -> dict[str, Any]:
    hits = search(settings(), payload.query, filters=payload.filters, limit=payload.limit)
    return {"hits": [hit.to_dict() for hit in hits]}


@app.post("/api/chat/sessions")
def api_session(payload: SessionRequest) -> dict[str, Any]:
    return create_session(
        settings(),
        model=payload.model,
        doc_type=payload.doc_type,
        software_version=payload.software_version,
        source_workspace_id=payload.source_workspace_id,
    ).to_dict()


@app.post("/api/chat/messages")
def api_chat(payload: ChatRequest) -> dict[str, Any]:
    overrides = {
        "model": payload.model,
        "doc_type": payload.doc_type,
        "software_version": payload.software_version,
        "source_workspace_id": payload.source_workspace_id,
    }
    return ask(
        settings(),
        payload.question,
        session_id=payload.session_id,
        overrides=overrides,
        use_llm=payload.use_llm,
        allow_external_llm=payload.allow_external_llm,
        source_workspace_id=payload.source_workspace_id,
    )


@app.get("/api/sources/{citation_id}")
def api_source(citation_id: str) -> dict[str, Any]:
    return source_preview(settings(), citation_id)


@app.post("/api/feedback")
def api_feedback(payload: FeedbackRequest) -> dict[str, Any]:
    s = settings()
    path = s.artifacts_dir / "feedback" / "feedback.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {**payload.model_dump(), "created_at": utc_now()}
    with path.open("a", encoding="utf-8") as handle:
        import json

        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return {"ok": True, "feedback_path": str(path)}


@app.post("/api/eval/seed")
def api_eval_seed(count: int = 96) -> dict[str, Any]:
    return generate_seed_cases(settings(), count=count)


@app.post("/api/eval/run")
def api_eval_run(limit: int | None = None) -> dict[str, Any]:
    return run_eval(settings(), limit=limit)


@app.post("/api/pipeline/run")
def api_pipeline_run(payload: PipelineRunRequest) -> dict[str, Any]:
    return run_automation_pipeline(
        settings(),
        PipelineOptions(
            relative_root=payload.relative_root,
            profile=payload.profile,
            limit=payload.limit,
            doc_id=payload.doc_id,
            rebuild=payload.rebuild,
            eval_count=payload.eval_count,
            eval_limit=payload.eval_limit,
            enable_paddleocr=payload.enable_paddleocr,
            enable_hyper_extract=payload.enable_hyper_extract,
            enable_llm_review=payload.enable_llm_review,
            allow_external_llm=payload.allow_external_llm,
            enable_apodex=payload.enable_apodex,
            enable_quality_audit=payload.enable_quality_audit,
        ),
    )


@app.get("/api/pipeline/report/latest")
def api_pipeline_report_latest() -> dict[str, Any]:
    return read_json(settings().artifacts_dir / "pipeline" / "latest_report.json", {})


@app.get("/api/pipeline/state/latest")
def api_pipeline_state_latest() -> dict[str, Any]:
    return read_json(settings().artifacts_dir / "pipeline" / "latest_state.json", {})


@app.post("/api/quality-audit/run")
def api_quality_audit(payload: QualityAuditRequest) -> dict[str, Any]:
    return run_quality_audit(
        settings(),
        QualityAuditOptions(
            doc_id=payload.doc_id,
            include_low_risk=payload.include_low_risk,
            max_chunk_issues_per_doc=payload.max_chunk_issues_per_doc,
            strict=payload.strict,
        ),
    )


@app.get("/api/quality-audit/latest")
def api_quality_audit_latest() -> dict[str, Any]:
    return read_json(settings().artifacts_dir / "quality_audit" / "latest_report.json", {})


@app.post("/api/reviews/apodex-semantic")
def api_apodex_semantic_review(payload: SemanticReviewRequest) -> dict[str, Any]:
    return run_semantic_review_from_payload(payload)


@app.post("/api/reviews/llm-semantic")
def api_llm_semantic_review(payload: SemanticReviewRequest) -> dict[str, Any]:
    return run_semantic_review_from_payload(payload)


def run_semantic_review_from_payload(payload: SemanticReviewRequest) -> dict[str, Any]:
    return run_apodex_semantic_review(
        settings(),
        SemanticReviewOptions(
            doc_id=payload.doc_id,
            limit=payload.limit,
            enable_llm_review=payload.enable_llm_review,
            enable_apodex=payload.enable_apodex,
            allow_external=payload.allow_external,
        ),
    )


@app.get("/api/reviews/apodex-semantic/latest")
def api_apodex_semantic_review_latest() -> dict[str, Any]:
    return read_json(settings().artifacts_dir / "reviews" / "apodex_semantic_latest.json", {})


@app.get("/api/reviews/llm-semantic/latest")
def api_llm_semantic_review_latest() -> dict[str, Any]:
    return read_json(settings().artifacts_dir / "reviews" / "llm_semantic_latest.json", {})


@app.post("/api/quality-loop/run")
def api_quality_loop(payload: QualityLoopRequest) -> dict[str, Any]:
    return run_quality_loop(
        settings(),
        QualityLoopOptions(
            doc_id=payload.doc_id,
            max_iterations=payload.max_iterations,
            review_limit=payload.review_limit,
            enable_llm_review=payload.enable_llm_review,
            enable_apodex=payload.enable_apodex,
            allow_external=payload.allow_external,
            apply_mode=payload.apply_mode,
            stop_when_no_candidates=payload.stop_when_no_candidates,
        ),
    )


@app.get("/api/quality-loop/report/latest")
def api_quality_loop_latest() -> dict[str, Any]:
    return read_json(settings().artifacts_dir / "quality_loop" / "latest_report.json", {})


def main() -> None:
    import uvicorn

    host = os.getenv("APP_HOST", "127.0.0.1")
    port = int(os.getenv("APP_PORT", "8088") or "8088")
    uvicorn.run("baxter_rag.api:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
