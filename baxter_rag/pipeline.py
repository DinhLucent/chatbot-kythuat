from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .conversion import convert_manifest, mineru_available
from .enrichment import run_technical_enrichment
from .evaluation import generate_seed_cases, run_eval, run_eval_audit
from .inventory import run_inventory
from .llm import DEFAULT_LLM_MODEL
from .normalization import normalize_manifest
from .ocr_retry import run_ocr_retry_candidates
from .quality_audit import QualityAuditOptions, run_quality_audit
from .retrieval import rebuild_index
from .semantic_review import SemanticReviewOptions, run_apodex_semantic_review
from .settings import Settings
from .utils import read_json, short_hash, utc_now, write_json


DEFAULT_RELATIVE_ROOT = "documents/IC/BAXTER"


@dataclass(frozen=True)
class PipelineOptions:
    relative_root: str = DEFAULT_RELATIVE_ROOT
    profile: str | None = None
    limit: int | None = None
    doc_id: str | None = None
    rebuild: bool = True
    eval_count: int = 96
    eval_limit: int | None = None
    eval_audit_count: int = 144
    eval_audit_limit: int | None = None
    enable_eval_audit: bool = True
    enable_paddleocr: bool | None = None
    enable_hyper_extract: bool | None = None
    enable_llm_review: bool | None = None
    allow_external_llm: bool | None = None
    enable_apodex: bool | None = None
    enable_quality_audit: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StageResult:
    name: str
    status: str
    started_at: str
    finished_at: str
    duration_seconds: float
    summary: dict[str, Any] = field(default_factory=dict)
    output_paths: dict[str, str] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_automation_pipeline(settings: Settings, options: PipelineOptions | None = None) -> dict[str, Any]:
    options = options or PipelineOptions()
    settings.ensure_dirs()
    run_id = build_run_id()
    run_dir = settings.artifacts_dir / "pipeline" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    baseline = collect_quality_snapshot(settings)
    stages: list[StageResult] = []
    ocr_candidates: list[dict[str, Any]] = []

    def persist(status: str) -> None:
        payload = build_state_payload(settings, run_id, run_dir, options, status, baseline, stages)
        write_json(run_dir / "state.json", payload)
        write_json(settings.artifacts_dir / "pipeline" / "latest_state.json", payload)

    def add_stage(
        name: str,
        runner: Callable[[], tuple[str, dict[str, Any], dict[str, str]]],
        *,
        required: bool = True,
    ) -> StageResult:
        stage = timed_stage(name, runner)
        stages.append(stage)
        persist("failed" if stage.status == "failed" else "running")
        if required and stage.status == "failed":
            raise PipelineAbort(stage.error or f"Required stage failed: {name}")
        return stage

    try:
        persist("running")
        add_stage("preflight", lambda: preflight_stage(settings, options), required=True)
        add_stage(
            "inventory",
            lambda: inventory_stage(settings, options),
            required=True,
        )
        conversion = add_stage(
            "convert",
            lambda: convert_stage(settings, options),
            required=True,
        )
        gate = add_stage(
            "conversion_quality_gate",
            lambda: conversion_gate_stage(settings, run_dir, conversion.summary),
            required=False,
        )
        ocr_candidates = list(gate.summary.get("ocr_candidates", []))
        add_stage(
            "paddleocr_fallback",
            lambda: paddleocr_stage(settings, options, run_dir, ocr_candidates),
            required=False,
        )
        add_stage(
            "normalize",
            lambda: normalize_stage(settings, options),
            required=True,
        )
        add_stage(
            "normalization_quality_gate",
            lambda: normalization_gate_stage(settings, run_dir),
            required=False,
        )
        add_stage(
            "technical_enrichment",
            lambda: technical_enrichment_stage(settings, options),
            required=False,
        )
        if options.enable_quality_audit:
            add_stage(
                "quality_audit",
                lambda: quality_audit_stage(settings, options),
                required=False,
            )
        add_stage(
            "llm_data_review",
            lambda: llm_data_review_stage(settings, options),
            required=False,
        )
        add_stage(
            "hyper_extract",
            lambda: hyper_extract_stage(settings, options, run_dir),
            required=False,
        )
        if options.rebuild:
            add_stage(
                "rebuild_index",
                lambda: rebuild_index_stage(settings),
                required=True,
            )
        add_stage(
            "eval_seed",
            lambda: eval_seed_stage(settings, options),
            required=False,
        )
        add_stage(
            "eval_run",
            lambda: eval_run_stage(settings, options),
            required=False,
        )
        if options.enable_eval_audit:
            add_stage(
                "eval_audit",
                lambda: eval_audit_stage(settings, options),
                required=False,
            )
        add_stage(
            "deterministic_validator",
            lambda: deterministic_validator_stage(settings, run_dir),
            required=False,
        )
        add_stage(
            "apodex_validator",
            lambda: apodex_validator_stage(settings, options, run_dir),
            required=False,
        )
        final_snapshot = collect_quality_snapshot(settings)
        report = build_report(settings, run_id, run_dir, options, baseline, final_snapshot, stages)
        write_json(run_dir / "report.json", report)
        write_report_markdown(run_dir / "report.md", report)
        write_json(settings.artifacts_dir / "pipeline" / "latest_report.json", report)
        persist("completed")
        return report
    except PipelineAbort as exc:
        final_snapshot = collect_quality_snapshot(settings)
        report = build_report(settings, run_id, run_dir, options, baseline, final_snapshot, stages, error=str(exc))
        write_json(run_dir / "report.json", report)
        write_report_markdown(run_dir / "report.md", report)
        write_json(settings.artifacts_dir / "pipeline" / "latest_report.json", report)
        persist("failed")
        return report


def timed_stage(name: str, runner: Callable[[], tuple[str, dict[str, Any], dict[str, str]]]) -> StageResult:
    started = utc_now()
    start_time = time.perf_counter()
    try:
        status, summary, output_paths = runner()
        error = None
    except Exception as exc:
        status = "failed"
        summary = {}
        output_paths = {}
        error = str(exc)
    finished = utc_now()
    return StageResult(
        name=name,
        status=status,
        started_at=started,
        finished_at=finished,
        duration_seconds=round(time.perf_counter() - start_time, 3),
        summary=summary,
        output_paths=output_paths,
        error=error,
    )


def preflight_stage(settings: Settings, options: PipelineOptions) -> tuple[str, dict[str, Any], dict[str, str]]:
    hyper_python = settings.root / "tools" / "Hyper-Extract" / ".venv" / "Scripts" / "python.exe"
    hyper_cli = shutil.which(os.getenv("HYPER_EXTRACT_EXECUTABLE", "he"))
    paddle_cli = shutil.which(os.getenv("PADDLEOCR_EXECUTABLE", "paddleocr"))
    summary = {
        "raw_source_root": str(settings.root / options.relative_root),
        "tools": {
            "mineru_repo": path_exists(settings.root / "tools" / "MinerU"),
            "paddleocr_repo": path_exists(settings.root / "tools" / "PaddleOCR"),
            "hyper_extract_repo": path_exists(settings.root / "tools" / "Hyper-Extract"),
            "llm_app_repo": path_exists(settings.root / "tools" / "llm-app"),
        },
        "runtime": {
            "mineru_enabled": env_bool("MINERU_ENABLED", False),
            "mineru_available": mineru_available(),
            "paddleocr_enabled": option_enabled(options.enable_paddleocr, "PADDLEOCR_ENABLED"),
            "paddleocr_cli": paddle_cli,
            "hyper_extract_enabled": option_enabled(options.enable_hyper_extract, "HYPER_EXTRACT_ENABLED"),
            "hyper_extract_cli": hyper_cli,
            "hyper_extract_venv_python": str(hyper_python) if hyper_python.exists() else "",
            "hyper_extract_has_api_key": has_any_env("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LLM_API_KEY"),
            "llm_review_enabled": option_enabled(options.enable_llm_review, "LLM_SEMANTIC_REVIEW_ENABLED"),
            "llm_base_url": os.getenv("LLM_BASE_URL", ""),
            "llm_model": os.getenv("LLM_MODEL", DEFAULT_LLM_MODEL),
            "llm_has_api_key": bool(os.getenv("LLM_API_KEY", "")),
            "llm_answer_qa_enabled": env_bool("LLM_ANSWER_QA_ENABLED", False),
            "eval_audit_enabled": options.enable_eval_audit,
            "eval_audit_count": options.eval_audit_count,
            "eval_audit_seed": env_int("BAXTER_RAG_EVAL_AUDIT_SEED", 20260621),
            "apodex_enabled": option_enabled(options.enable_apodex, "APODEX_ENABLED"),
            "apodex_base_url": os.getenv("APODEX_BASE_URL", ""),
            "apodex_has_api_key": bool(os.getenv("APODEX_API_KEY", "")),
        },
        "safety": {
            "external_hyper_extract_allowed": env_bool("HYPER_EXTRACT_ALLOW_EXTERNAL", False),
            "external_llm_allowed": option_enabled(options.allow_external_llm, "LLM_EXTERNAL_UPLOAD_ALLOWED"),
            "external_apodex_allowed": env_bool("APODEX_ALLOW_EXTERNAL", False),
            "raw_documents_sent_to_llm": False,
        },
    }
    missing = [name for name, exists in summary["tools"].items() if not exists]
    if missing:
        summary["warnings"] = [{"code": "tool_repo_missing", "items": missing}]
        return "warning", summary, {}
    return "passed", summary, {}


def inventory_stage(settings: Settings, options: PipelineOptions) -> tuple[str, dict[str, Any], dict[str, str]]:
    result = run_inventory(settings, relative_root=options.relative_root).to_dict()
    return "passed", result, {
        "corpus_inventory": result["corpus_inventory_path"],
        "source_manifest": result["source_manifest_path"],
        "data_readiness_report": result["data_readiness_report_path"],
    }


def convert_stage(settings: Settings, options: PipelineOptions) -> tuple[str, dict[str, Any], dict[str, str]]:
    result = convert_manifest(
        settings,
        profile=options.profile,
        limit=options.limit,
        doc_id=options.doc_id,
    )
    failed = int(result.get("summary", {}).get("failed") or 0)
    status = "passed" if failed == 0 else "failed"
    return status, result, {"conversion_run": str(settings.artifacts_dir / "converted" / "conversion_run_latest.json")}


def conversion_gate_stage(
    settings: Settings,
    run_dir: Path,
    conversion_summary: dict[str, Any],
) -> tuple[str, dict[str, Any], dict[str, str]]:
    manifest = read_json(settings.artifacts_dir / "phase0" / "source_manifest.json", {})
    records = {str(item.get("doc_id")): item for item in manifest.get("records", []) if isinstance(item, dict)}
    candidates: list[dict[str, Any]] = []
    failed = []
    char_counts: list[int] = []
    for result in conversion_summary.get("results", []):
        if not isinstance(result, dict):
            continue
        doc_id = str(result.get("doc_id") or "")
        if result.get("status") == "failed":
            failed.append(doc_id)
            continue
        raw_md = Path(str(result.get("output_markdown") or ""))
        text = raw_md.read_text(encoding="utf-8", errors="replace") if raw_md.exists() else ""
        char_counts.append(len(text))
        record = records.get(doc_id, {})
        reasons = []
        if float(record.get("embedded_text_ratio") or 0.0) < 0.05 and str(record.get("file_ext")) == ".pdf":
            reasons.append("weak_embedded_text")
        empty_pages = text.count("empty_or_ocr_required")
        if empty_pages:
            reasons.append("empty_pages")
        page_count = int(record.get("page_count") or 0)
        if page_count and len(text) / max(1, page_count) < 120:
            reasons.append("low_chars_per_page")
        if reasons:
            candidates.append(
                {
                    "doc_id": doc_id,
                    "source_path": record.get("source_path"),
                    "page_count": page_count,
                    "embedded_text_ratio": record.get("embedded_text_ratio"),
                    "empty_pages": empty_pages,
                    "char_count": len(text),
                    "reasons": reasons,
                    "suggested_tool": "PaddleOCR page fallback or MinerU OCR profile",
                }
            )
    out_path = run_dir / "ocr_candidates.json"
    write_json(out_path, {"generated_at": utc_now(), "candidates": candidates})
    summary = {
        "failed_conversion_count": len(failed),
        "failed_doc_ids": failed[:50],
        "ocr_candidate_count": len(candidates),
        "ocr_candidates": candidates,
        "avg_converted_chars": round(sum(char_counts) / max(1, len(char_counts)), 2),
    }
    status = "failed" if failed else "warning" if candidates else "passed"
    return status, summary, {"ocr_candidates": str(out_path)}


def paddleocr_stage(
    settings: Settings,
    options: PipelineOptions,
    run_dir: Path,
    candidates: list[dict[str, Any]],
) -> tuple[str, dict[str, Any], dict[str, str]]:
    enabled = option_enabled(options.enable_paddleocr, "PADDLEOCR_ENABLED")
    plan_path = run_dir / "paddleocr_plan.json"
    cli = shutil.which(os.getenv("PADDLEOCR_EXECUTABLE", "paddleocr"))
    payload = {
        "generated_at": utc_now(),
        "enabled": enabled,
        "cli": cli,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "mode": "local_page_ocr_retry",
        "note": "When enabled and PADDLEOCR_RUN=true, this renders selected PDF pages, runs local PaddleOCR, and promotes conversion overlays through data_workbench.",
    }
    write_json(plan_path, payload)
    if not candidates:
        return "skipped", {**payload, "reason": "no_ocr_candidates"}, {"paddleocr_plan": str(plan_path)}
    if not enabled:
        return "skipped", {**payload, "reason": "PADDLEOCR_ENABLED_false"}, {"paddleocr_plan": str(plan_path)}
    if not cli:
        return "warning", {**payload, "reason": "paddleocr_cli_not_found"}, {"paddleocr_plan": str(plan_path)}
    if options.enable_paddleocr is not True and not env_bool("PADDLEOCR_RUN", False):
        return "skipped", {**payload, "reason": "PADDLEOCR_RUN_false"}, {"paddleocr_plan": str(plan_path)}
    result = run_ocr_retry_candidates(settings, candidates, parent_run_dir=run_dir, doc_id=options.doc_id, limit=options.limit)
    status = "passed" if result.get("promoted_count") else "warning"
    if result.get("failed_count"):
        status = "warning"
    return status, {**payload, "reason": "ocr_retry_completed", "ocr_retry": result}, {
        "paddleocr_plan": str(plan_path),
        "ocr_retry_report": str(Path(str(result.get("run_dir") or "")) / "report.json"),
    }


def normalize_stage(settings: Settings, options: PipelineOptions) -> tuple[str, dict[str, Any], dict[str, str]]:
    result = normalize_manifest(settings, limit=options.limit, doc_id=options.doc_id)
    return "passed", result, {"normalization_run": str(settings.artifacts_dir / "normalized" / "normalization_run_latest.json")}


def normalization_gate_stage(settings: Settings, run_dir: Path) -> tuple[str, dict[str, Any], dict[str, str]]:
    reports = load_quality_reports(settings)
    failed_gates: Counter[str] = Counter()
    warnings: Counter[str] = Counter()
    locator_ratios = []
    mojibake_scores = []
    for report in reports:
        summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
        locator_ratios.append(float(summary.get("locator_ratio") or 0.0))
        mojibake_scores.append(int(summary.get("mojibake_score") or 0))
        gates = report.get("quality_gates") if isinstance(report.get("quality_gates"), dict) else {}
        for key, value in gates.items():
            if value is False:
                failed_gates[key] += 1
        for warning in report.get("warnings", []):
            if isinstance(warning, dict):
                warnings[str(warning.get("code") or "unknown")] += 1
    gate_path = run_dir / "normalization_quality_gate.json"
    summary = {
        "document_report_count": len(reports),
        "avg_locator_ratio": round(sum(locator_ratios) / max(1, len(locator_ratios)), 4),
        "max_mojibake_score": max(mojibake_scores or [0]),
        "failed_gates": dict(failed_gates),
        "warnings": dict(warnings),
    }
    write_json(gate_path, {"generated_at": utc_now(), **summary})
    status = "warning" if failed_gates or warnings else "passed"
    return status, summary, {"normalization_quality_gate": str(gate_path)}


def technical_enrichment_stage(settings: Settings, options: PipelineOptions) -> tuple[str, dict[str, Any], dict[str, str]]:
    result = run_technical_enrichment(settings, doc_id=options.doc_id, limit=options.limit)
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    paths = result.get("paths") if isinstance(result.get("paths"), dict) else {}
    status = "passed" if int(summary.get("document_count") or 0) > 0 else "warning"
    return status, summary, {
        "technical_enrichment": str(paths.get("corpus_latest") or ""),
        "technical_enrichment_report": str(paths.get("report_markdown") or ""),
        "technical_enrichment_run": str(paths.get("run_dir") or ""),
    }


def quality_audit_stage(settings: Settings, options: PipelineOptions) -> tuple[str, dict[str, Any], dict[str, str]]:
    report = run_quality_audit(
        settings,
        QualityAuditOptions(
            doc_id=options.doc_id,
            max_chunk_issues_per_doc=40,
            include_low_risk=True,
        ),
    )
    scorecard = report.get("scorecard", {}) if isinstance(report.get("scorecard"), dict) else {}
    severity = scorecard.get("severity_counts", {}) if isinstance(scorecard.get("severity_counts"), dict) else {}
    issue_count = int(scorecard.get("issue_count") or 0)
    status = "warning" if issue_count else "passed"
    if int(severity.get("critical") or 0) and options.doc_id:
        status = "warning"
    return status, scorecard, {
        "quality_audit_report": str(report.get("report_markdown") or ""),
        "quality_audit_issues": str(report.get("issues_path") or ""),
        "quality_audit_queues": str(report.get("queues_path") or ""),
    }


def llm_data_review_stage(settings: Settings, options: PipelineOptions) -> tuple[str, dict[str, Any], dict[str, str]]:
    limit = int(os.getenv("LLM_DATA_REVIEW_LIMIT", "12") or "12")
    report = run_apodex_semantic_review(
        settings,
        SemanticReviewOptions(
            doc_id=options.doc_id,
            limit=limit,
            enable_llm_review=options.enable_llm_review,
            allow_external=options.allow_external_llm,
        ),
    )
    summary = {
        "provider": report.get("provider"),
        "status": report.get("status"),
        "reason": report.get("reason"),
        "model": report.get("model"),
        "candidate_count": report.get("candidate_count"),
        "summary": report.get("summary", {}),
        "external_upload_allowed": report.get("external_upload_allowed"),
    }
    status = "passed" if report.get("status") == "completed" else "skipped" if report.get("status") == "skipped" else "warning"
    return status, summary, {
        "llm_semantic_review_report": str(report.get("report_markdown") or ""),
        "llm_semantic_review_requests": str(report.get("request_path") or ""),
        "llm_semantic_review_proposals": str(report.get("proposal_path") or ""),
    }


def hyper_extract_stage(
    settings: Settings,
    options: PipelineOptions,
    run_dir: Path,
) -> tuple[str, dict[str, Any], dict[str, str]]:
    enabled = option_enabled(options.enable_hyper_extract, "HYPER_EXTRACT_ENABLED")
    allow_external = env_bool("HYPER_EXTRACT_ALLOW_EXTERNAL", False)
    candidate_path = run_dir / "hyper_extract_candidates.json"
    candidates = select_hyper_extract_candidates(settings)
    has_key = has_any_env("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LLM_API_KEY")
    hyper_python = settings.root / "tools" / "Hyper-Extract" / ".venv" / "Scripts" / "python.exe"
    cli = shutil.which(os.getenv("HYPER_EXTRACT_EXECUTABLE", "he"))
    payload = {
        "generated_at": utc_now(),
        "enabled": enabled,
        "allow_external": allow_external,
        "has_api_key": has_key,
        "cli": cli,
        "venv_python": str(hyper_python) if hyper_python.exists() else "",
        "candidate_count": len(candidates),
        "templates": ["failure_case", "operation_flow", "safety_control", "equipment_topology"],
        "candidates": candidates[:100],
    }
    write_json(candidate_path, payload)
    if not candidates:
        return "skipped", {**payload, "reason": "no_structured_candidates"}, {"hyper_extract_candidates": str(candidate_path)}
    if not enabled:
        return "skipped", {**payload, "reason": "HYPER_EXTRACT_ENABLED_false"}, {"hyper_extract_candidates": str(candidate_path)}
    if not allow_external:
        return "skipped", {**payload, "reason": "external_llm_not_allowed"}, {"hyper_extract_candidates": str(candidate_path)}
    if not has_key:
        return "warning", {**payload, "reason": "api_key_missing"}, {"hyper_extract_candidates": str(candidate_path)}
    if not cli and not hyper_python.exists():
        return "warning", {**payload, "reason": "hyper_extract_runtime_missing"}, {"hyper_extract_candidates": str(candidate_path)}
    return "warning", {**payload, "reason": "runtime_available_but_execution_requires_manual_template_mapping"}, {
        "hyper_extract_candidates": str(candidate_path)
    }


def rebuild_index_stage(settings: Settings) -> tuple[str, dict[str, Any], dict[str, str]]:
    result = rebuild_index(settings)
    status = "passed" if int(result.get("chunk_count") or 0) > 0 else "failed"
    return status, result, {"index_meta": str(settings.artifacts_dir / "index" / "index_meta.json")}


def eval_seed_stage(settings: Settings, options: PipelineOptions) -> tuple[str, dict[str, Any], dict[str, str]]:
    result = generate_seed_cases(settings, count=options.eval_count)
    return "passed", result, {"eval_seed": str(settings.root / "eval" / "baxter_seed_cases.json")}


def eval_run_stage(settings: Settings, options: PipelineOptions) -> tuple[str, dict[str, Any], dict[str, str]]:
    result = run_eval(settings, limit=options.eval_limit)
    failed = int(result.get("failed") or 0)
    status = "passed" if failed == 0 else "warning"
    return status, result, {"eval_latest": str(settings.root / "eval" / "baxter_eval_latest.json")}


def eval_audit_stage(settings: Settings, options: PipelineOptions) -> tuple[str, dict[str, Any], dict[str, str]]:
    seed = env_int("BAXTER_RAG_EVAL_AUDIT_SEED", 20260621)
    result = run_eval_audit(
        settings,
        count=options.eval_audit_count,
        seed=seed,
        limit=options.eval_audit_limit,
    )
    failed = int(result.get("failed") or 0)
    status = "passed" if failed == 0 and int(result.get("case_count") or 0) else "warning"
    return status, result, {
        "eval_audit_latest": str(settings.artifacts_dir / "evaluation" / "audit_latest.json"),
        "eval_audit_cases": str(settings.artifacts_dir / "evaluation" / "audit_cases_latest.json"),
        "eval_audit_root_cause": str(settings.artifacts_dir / "root_cause" / "audit_eval_latest_root_cause.json"),
    }


def deterministic_validator_stage(settings: Settings, run_dir: Path) -> tuple[str, dict[str, Any], dict[str, str]]:
    index_meta = read_json(settings.artifacts_dir / "index" / "index_meta.json", {})
    eval_latest = read_json(settings.root / "eval" / "baxter_eval_latest.json", {})
    audit_latest = read_json(settings.artifacts_dir / "evaluation" / "audit_latest.json", {})
    missing_source = 0
    citation_count = 0
    for citation_path in sorted((settings.artifacts_dir / "normalized").glob("*/citation_map.json")):
        payload = read_json(citation_path, {})
        for citation in payload.get("citations", []):
            if not isinstance(citation, dict):
                continue
            citation_count += 1
            source = citation.get("source_file")
            if source and not Path(str(source)).exists():
                missing_source += 1
    eval_count = int(eval_latest.get("case_count") or 0)
    eval_failed = int(eval_latest.get("failed") or 0)
    audit_count = int(audit_latest.get("case_count") or 0)
    audit_failed = int(audit_latest.get("failed") or 0)
    audit_pass_rate = float(audit_latest.get("pass_rate") or 0.0)
    audit_min_pass_rate = env_float("BAXTER_RAG_EVAL_AUDIT_MIN_PASS_RATE", 0.9)
    audit_root_cause_path = settings.artifacts_dir / "root_cause" / "audit_eval_latest_root_cause.json"
    audit_root_cause_present = audit_root_cause_path.exists()
    summary = {
        "index_chunk_count": int(index_meta.get("chunk_count") or 0),
        "citation_count": citation_count,
        "missing_source_file_citations": missing_source,
        "eval_case_count": eval_count,
        "eval_failed": eval_failed,
        "eval_audit_case_count": audit_count,
        "eval_audit_failed": audit_failed,
        "eval_audit_pass_rate": audit_pass_rate,
        "eval_audit_min_pass_rate": audit_min_pass_rate,
        "eval_audit_root_cause_present": audit_root_cause_present,
        "no_raw_document_external_upload": True,
    }
    path = run_dir / "deterministic_validator.json"
    write_json(path, {"generated_at": utc_now(), **summary})
    audit_ready = audit_count and audit_pass_rate >= audit_min_pass_rate and audit_root_cause_present
    status = "passed" if citation_count and not missing_source and not eval_failed and audit_ready else "warning"
    return status, summary, {"deterministic_validator": str(path)}


def apodex_validator_stage(
    settings: Settings,
    options: PipelineOptions,
    run_dir: Path,
) -> tuple[str, dict[str, Any], dict[str, str]]:
    enabled = option_enabled(options.enable_apodex, "APODEX_ENABLED")
    allow_external = env_bool("APODEX_ALLOW_EXTERNAL", False)
    base_url = os.getenv("APODEX_BASE_URL", "").strip()
    api_key = os.getenv("APODEX_API_KEY", "").strip()
    model = os.getenv("APODEX_MODEL", "apodex/Apodex-1.0-mini")
    path = run_dir / "apodex_validator_plan.json"
    payload = {
        "generated_at": utc_now(),
        "enabled": enabled,
        "allow_external": allow_external,
        "base_url": base_url,
        "has_api_key": bool(api_key),
        "model": model,
        "purpose": "source-support validator for retrieved snippets and citations",
    }
    write_json(path, payload)
    if not enabled:
        return "skipped", {**payload, "reason": "APODEX_ENABLED_false"}, {"apodex_validator_plan": str(path)}
    if not allow_external:
        return "skipped", {**payload, "reason": "external_validator_not_allowed"}, {"apodex_validator_plan": str(path)}
    if not base_url:
        return "warning", {**payload, "reason": "APODEX_BASE_URL_missing"}, {"apodex_validator_plan": str(path)}
    if not api_key:
        return "warning", {**payload, "reason": "APODEX_API_KEY_missing"}, {"apodex_validator_plan": str(path)}
    probe = probe_openai_compatible_model(base_url, api_key, model)
    status = "passed" if probe.get("ok") else "warning"
    return status, {**payload, "probe": probe}, {"apodex_validator_plan": str(path)}


def select_hyper_extract_candidates(settings: Settings) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    useful_blocks = {
        "alarm_code",
        "troubleshooting_symptom",
        "troubleshooting_cause",
        "troubleshooting_fix",
        "service_step",
        "maintenance_step",
        "calibration_step",
        "warning_safety",
        "parts_table",
        "software_note",
    }
    for chunk_path in sorted((settings.artifacts_dir / "normalized").glob("*/chunk_plan.json")):
        payload = read_json(chunk_path, {})
        for chunk in payload.get("chunks", []):
            if not isinstance(chunk, dict) or chunk.get("low_value"):
                continue
            block_type = str(chunk.get("block_type") or "")
            if block_type not in useful_blocks:
                continue
            content = str(chunk.get("content") or "")
            if len(content) < 80:
                continue
            candidates.append(
                {
                    "chunk_id": chunk.get("chunk_id"),
                    "doc_id": chunk.get("doc_id"),
                    "block_type": block_type,
                    "model": chunk.get("model"),
                    "doc_type": chunk.get("doc_type"),
                    "section_path": chunk.get("section_path", []),
                    "text_preview": content[:500],
                }
            )
    return candidates


def collect_quality_snapshot(settings: Settings) -> dict[str, Any]:
    conversion = read_json(settings.artifacts_dir / "converted" / "conversion_run_latest.json", {})
    normalization = read_json(settings.artifacts_dir / "normalized" / "normalization_run_latest.json", {})
    index_meta = read_json(settings.artifacts_dir / "index" / "index_meta.json", {})
    eval_latest = read_json(settings.root / "eval" / "baxter_eval_latest.json", {})
    audit_latest = read_json(settings.artifacts_dir / "evaluation" / "audit_latest.json", {})
    reports = load_quality_reports(settings)
    locator_ratios = [
        float(report.get("summary", {}).get("locator_ratio") or 0.0)
        for report in reports
        if isinstance(report.get("summary"), dict)
    ]
    warnings = Counter()
    failed_gates = Counter()
    for report in reports:
        for warning in report.get("warnings", []):
            if isinstance(warning, dict):
                warnings[str(warning.get("code") or "unknown")] += 1
        gates = report.get("quality_gates") if isinstance(report.get("quality_gates"), dict) else {}
        for key, value in gates.items():
            if value is False:
                failed_gates[key] += 1
    seed_eval_count = int(eval_latest.get("case_count") or 0)
    seed_eval_passed = int(eval_latest.get("passed") or 0)
    seed_eval_failed = int(eval_latest.get("failed") or 0)
    audit_eval_count = int(audit_latest.get("case_count") or 0)
    audit_eval_passed = int(audit_latest.get("passed") or 0)
    audit_eval_failed = int(audit_latest.get("failed") or 0)
    eval_count = seed_eval_count + audit_eval_count
    eval_passed = seed_eval_passed + audit_eval_passed
    conversion_total = int(conversion.get("summary", {}).get("total") or 0)
    conversion_failed = int(conversion.get("summary", {}).get("failed") or 0)
    chunk_count = int(index_meta.get("chunk_count") or 0)
    avg_locator = sum(locator_ratios) / max(1, len(locator_ratios))
    eval_pass_rate = eval_passed / max(1, eval_count) if eval_count else None
    seed_eval_pass_rate = seed_eval_passed / max(1, seed_eval_count) if seed_eval_count else None
    audit_eval_pass_rate = audit_eval_passed / max(1, audit_eval_count) if audit_eval_count else None
    conversion_success_rate = (conversion_total - conversion_failed) / max(1, conversion_total) if conversion_total else None
    score = pipeline_quality_score(
        conversion_success_rate=conversion_success_rate,
        avg_locator_ratio=avg_locator if locator_ratios else None,
        eval_pass_rate=eval_pass_rate,
        index_chunk_count=chunk_count,
        failed_gate_count=sum(failed_gates.values()),
    )
    return {
        "generated_at": utc_now(),
        "conversion_total": conversion_total,
        "conversion_failed": conversion_failed,
        "conversion_success_rate": conversion_success_rate,
        "normalized_document_count": int(normalization.get("summary", {}).get("document_count") or 0),
        "normalized_chunk_count": int(normalization.get("summary", {}).get("chunk_count") or 0),
        "index_chunk_count": chunk_count,
        "quality_report_count": len(reports),
        "avg_locator_ratio": round(avg_locator, 4) if locator_ratios else None,
        "quality_warnings": dict(warnings),
        "failed_quality_gates": dict(failed_gates),
        "eval_case_count": eval_count,
        "eval_passed": eval_passed,
        "eval_failed": seed_eval_failed + audit_eval_failed,
        "eval_pass_rate": round(eval_pass_rate, 4) if eval_pass_rate is not None else None,
        "eval_seed_case_count": seed_eval_count,
        "eval_seed_passed": seed_eval_passed,
        "eval_seed_failed": seed_eval_failed,
        "eval_seed_pass_rate": round(seed_eval_pass_rate, 4) if seed_eval_pass_rate is not None else None,
        "eval_audit_case_count": audit_eval_count,
        "eval_audit_passed": audit_eval_passed,
        "eval_audit_failed": audit_eval_failed,
        "eval_audit_pass_rate": round(audit_eval_pass_rate, 4) if audit_eval_pass_rate is not None else None,
        "pipeline_quality_score": score,
    }


def build_report(
    settings: Settings,
    run_id: str,
    run_dir: Path,
    options: PipelineOptions,
    baseline: dict[str, Any],
    final_snapshot: dict[str, Any],
    stages: list[StageResult],
    *,
    error: str | None = None,
) -> dict[str, Any]:
    stage_counts = Counter(stage.status for stage in stages)
    comparison = compare_snapshots(baseline, final_snapshot)
    optional_used = {
        "paddleocr": any(stage.name == "paddleocr_fallback" and stage.status == "passed" for stage in stages),
        "hyper_extract": any(stage.name == "hyper_extract" and stage.status == "passed" for stage in stages),
        "llm_data_review": any(stage.name == "llm_data_review" and stage.status == "passed" for stage in stages),
        "apodex": any(stage.name == "apodex_validator" and stage.status == "passed" for stage in stages),
    }
    assessment = assess_quality_change(comparison, optional_used, stages)
    return {
        "schema_version": "baxter_agentic_pipeline_report_v1",
        "generated_at": utc_now(),
        "run_id": run_id,
        "status": "failed" if error else "completed",
        "error": error,
        "root": str(settings.root),
        "run_dir": str(run_dir),
        "options": options.to_dict(),
        "stage_counts": dict(stage_counts),
        "stages": [stage.to_dict() for stage in stages],
        "baseline_snapshot": baseline,
        "final_snapshot": final_snapshot,
        "comparison": comparison,
        "optional_tools_used": optional_used,
        "quality_assessment": assessment,
        "report_markdown": str(run_dir / "report.md"),
    }


def build_state_payload(
    settings: Settings,
    run_id: str,
    run_dir: Path,
    options: PipelineOptions,
    status: str,
    baseline: dict[str, Any],
    stages: list[StageResult],
) -> dict[str, Any]:
    return {
        "schema_version": "baxter_agentic_pipeline_state_v1",
        "updated_at": utc_now(),
        "status": status,
        "run_id": run_id,
        "root": str(settings.root),
        "run_dir": str(run_dir),
        "options": options.to_dict(),
        "baseline_snapshot": baseline,
        "stages": [stage.to_dict() for stage in stages],
    }


def write_report_markdown(path: Path, report: dict[str, Any]) -> None:
    stages = report.get("stages", [])
    final = report.get("final_snapshot", {})
    baseline = report.get("baseline_snapshot", {})
    comparison = report.get("comparison", {})
    assessment = report.get("quality_assessment", {})
    lines = [
        "# Agentic Data Pipeline Report",
        "",
        f"- Run id: `{report.get('run_id')}`",
        f"- Status: `{report.get('status')}`",
        f"- Pipeline quality score: `{final.get('pipeline_quality_score')}`",
        f"- Eval pass rate: `{final.get('eval_pass_rate')}`",
        f"- Seed eval: `{final.get('eval_seed_passed')}/{final.get('eval_seed_case_count')}`",
        f"- Audit eval: `{final.get('eval_audit_passed')}/{final.get('eval_audit_case_count')}`",
        f"- Index chunks: `{final.get('index_chunk_count')}`",
        "",
        "## Quality Change",
        "",
        f"- Verdict: {assessment.get('verdict')}",
        f"- Reason: {assessment.get('reason')}",
        f"- Baseline score: `{baseline.get('pipeline_quality_score')}`",
        f"- Final score: `{final.get('pipeline_quality_score')}`",
        f"- Score delta: `{comparison.get('pipeline_quality_score_delta')}`",
        f"- Eval pass rate delta: `{comparison.get('eval_pass_rate_delta')}`",
        "",
        "## Stages",
        "",
        "| Stage | Status | Seconds | Notes |",
        "|---|---:|---:|---|",
    ]
    for stage in stages:
        summary = stage.get("summary") if isinstance(stage.get("summary"), dict) else {}
        note = summary.get("reason") or stage.get("error") or ""
        if not note and stage.get("name") == "convert":
            note = f"converted={summary.get('summary', {}).get('converted')} failed={summary.get('summary', {}).get('failed')}"
        if not note and stage.get("name") == "eval_run":
            note = f"passed={summary.get('passed')} failed={summary.get('failed')}"
        if not note and stage.get("name") == "eval_audit":
            note = f"passed={summary.get('passed')} failed={summary.get('failed')} pass_rate={summary.get('pass_rate')}"
        lines.append(f"| `{stage.get('name')}` | `{stage.get('status')}` | `{stage.get('duration_seconds')}` | {note} |")
    lines.extend(
        [
            "",
            "## Pros",
            "",
            "- End-to-end run state is reproducible and saved per run.",
            "- Optional AI tools are gated, so raw manuals are not sent out accidentally.",
            "- OCR and structured extraction are selected by quality signals instead of being run on every file.",
            "- Reports make conversion, citation, retrieval, and eval failures visible.",
            "",
            "## Cons",
            "",
            "- PaddleOCR, Hyper-Extract, and Apodex do not improve answer quality unless their runtimes and permissions are explicitly enabled.",
            "- The first MVP still depends on the existing converter quality for tables and figures.",
            "- Extra validation increases runtime and operational complexity.",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def compare_snapshots(baseline: dict[str, Any], final: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "pipeline_quality_score",
        "conversion_success_rate",
        "avg_locator_ratio",
        "index_chunk_count",
        "eval_pass_rate",
        "eval_failed",
    ]
    out: dict[str, Any] = {}
    for key in keys:
        before = baseline.get(key)
        after = final.get(key)
        if isinstance(before, (int, float)) and isinstance(after, (int, float)):
            out[f"{key}_delta"] = round(after - before, 4)
        else:
            out[f"{key}_delta"] = None
    return out


def assess_quality_change(
    comparison: dict[str, Any],
    optional_used: dict[str, bool],
    stages: list[StageResult],
) -> dict[str, str]:
    score_delta = comparison.get("pipeline_quality_score_delta")
    eval_delta = comparison.get("eval_pass_rate_delta")
    used_any_optional = any(optional_used.values())
    audit_stage = next((stage for stage in stages if stage.name == "eval_audit"), None)
    if audit_stage and audit_stage.status == "warning":
        return {
            "verdict": "expanded_qa_found_risks",
            "reason": "The index-derived audit found answer, retrieval, metadata, or citation failures. Review the audit root-cause report before widening usage.",
        }
    if audit_stage and audit_stage.status == "passed":
        return {
            "verdict": "expanded_qa_passed",
            "reason": "Seed eval and index-derived audit both passed, including workspace and policy cases.",
        }
    if isinstance(eval_delta, (int, float)) and eval_delta > 0:
        return {
            "verdict": "measured_answer_quality_improved",
            "reason": "The eval pass rate improved after the automated run.",
        }
    if isinstance(score_delta, (int, float)) and score_delta > 0 and not used_any_optional:
        return {
            "verdict": "operational_quality_improved",
            "reason": "The run improved completeness or index/readiness metrics, but optional AI/OCR tools were not active.",
        }
    skipped = [stage.name for stage in stages if stage.status == "skipped"]
    if not used_any_optional:
        return {
            "verdict": "no_measured_answer_quality_gain_yet",
            "reason": "Optional OCR/extraction/validator stages were skipped or only planned, so retrieval quality is expected to stay close to baseline.",
        }
    return {
        "verdict": "needs_human_review",
        "reason": f"Optional tools ran: {', '.join(key for key, value in optional_used.items() if value)}. Review citations and eval failures.",
    }


def pipeline_quality_score(
    *,
    conversion_success_rate: float | None,
    avg_locator_ratio: float | None,
    eval_pass_rate: float | None,
    index_chunk_count: int,
    failed_gate_count: int,
) -> float:
    score = 0.0
    if conversion_success_rate is not None:
        score += 25.0 * conversion_success_rate
    if avg_locator_ratio is not None:
        score += 20.0 * min(1.0, max(0.0, avg_locator_ratio))
    if eval_pass_rate is not None:
        score += 35.0 * eval_pass_rate
    if index_chunk_count > 0:
        score += 20.0
    score -= min(20.0, failed_gate_count * 2.0)
    return round(max(0.0, min(100.0, score)), 2)


def load_quality_reports(settings: Settings) -> list[dict[str, Any]]:
    reports = []
    for path in sorted((settings.artifacts_dir / "normalized").glob("*/quality_report.json")):
        payload = read_json(path, {})
        if isinstance(payload, dict) and payload:
            reports.append(payload)
    return reports


def probe_openai_compatible_model(base_url: str, api_key: str, model: str) -> dict[str, Any]:
    try:
        import requests
    except Exception as exc:
        return {"ok": False, "error": f"requests_unavailable: {exc}"}
    url = base_url.rstrip("/") + "/chat/completions"
    timeout = int(os.getenv("APODEX_TIMEOUT_SECONDS", "30") or "30")
    try:
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": "You validate if answers are supported by cited text."},
                    {"role": "user", "content": "Return JSON: {\"ok\": true}"},
                ],
                "temperature": 0,
                "max_tokens": 32,
            },
            timeout=timeout,
        )
        return {"ok": response.ok, "status_code": response.status_code, "text_preview": response.text[:300]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def build_run_id() -> str:
    stamp = utc_now().replace(":", "").replace("-", "").replace("+0000", "Z")
    return f"run_{stamp}_{short_hash(stamp, 6)}"


def option_enabled(value: bool | None, env_name: str) -> bool:
    if value is not None:
        return bool(value)
    return env_bool(env_name, False)


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def has_any_env(*names: str) -> bool:
    return any(bool(os.getenv(name, "").strip()) for name in names)


def path_exists(path: Path) -> bool:
    return path.exists()


class PipelineAbort(RuntimeError):
    pass
