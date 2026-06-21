from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .chat import ask
from .conversion import convert_manifest
from .enrichment import run_technical_enrichment
from .evaluation import generate_seed_cases, run_eval, run_eval_audit
from .inventory import run_inventory
from .normalization import normalize_manifest
from .ocr_retry import run_ocr_retry_candidates
from .pipeline import PipelineOptions, run_automation_pipeline
from .quality_audit import QualityAuditOptions, run_quality_audit
from .quality_loop import QualityLoopOptions, run_quality_loop
from .retrieval import rebuild_index, search
from .semantic_review import SemanticReviewOptions, run_apodex_semantic_review
from .settings import Settings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Baxter IC technical RAG pipeline")
    parser.add_argument("--root", default=".", help="Workspace root")
    sub = parser.add_subparsers(dest="command", required=True)

    p_inventory = sub.add_parser("inventory", help="Scan raw Baxter corpus")
    p_inventory.add_argument("--relative-root", default="documents/IC/BAXTER")

    p_convert = sub.add_parser("convert", help="Convert manifest documents")
    p_convert.add_argument("--manifest-path")
    p_convert.add_argument("--profile")
    p_convert.add_argument("--limit", type=int)
    p_convert.add_argument("--doc-id")

    p_normalize = sub.add_parser("normalize", help="Normalize converted docs")
    p_normalize.add_argument("--manifest-path")
    p_normalize.add_argument("--limit", type=int)
    p_normalize.add_argument("--doc-id")

    p_rebuild = sub.add_parser("rebuild-index", help="Rebuild local/Qdrant index")
    p_rebuild.add_argument("--push-qdrant", action="store_true")

    p_search = sub.add_parser("search", help="Search index")
    p_search.add_argument("query")
    p_search.add_argument("--model")
    p_search.add_argument("--doc-type")
    p_search.add_argument("--limit", type=int, default=8)

    p_chat = sub.add_parser("chat", help="Ask a question")
    p_chat.add_argument("question")
    p_chat.add_argument("--session-id")
    p_chat.add_argument("--model")
    p_chat.add_argument("--doc-type")
    p_chat.add_argument("--software-version")
    p_chat.add_argument("--no-llm", action="store_true")
    p_chat.add_argument("--allow-external-llm", action="store_true")

    p_eval_seed = sub.add_parser("eval-seed", help="Generate seed eval cases")
    p_eval_seed.add_argument("--count", type=int, default=96)

    p_eval = sub.add_parser("eval-run", help="Run seed eval")
    p_eval.add_argument("--cases-path")
    p_eval.add_argument("--limit", type=int)

    p_eval_audit = sub.add_parser("eval-audit", help="Run index-derived random/workspace/policy answer audit")
    p_eval_audit.add_argument("--count", type=int, default=144)
    p_eval_audit.add_argument("--seed", type=int, default=20260621)
    p_eval_audit.add_argument("--cases-path")
    p_eval_audit.add_argument("--limit", type=int)

    p_pipeline_run = sub.add_parser("pipeline-run", help="Run guarded end-to-end automation pipeline")
    p_pipeline_run.add_argument("--relative-root", default="documents/IC/BAXTER")
    p_pipeline_run.add_argument("--profile")
    p_pipeline_run.add_argument("--limit", type=int)
    p_pipeline_run.add_argument("--doc-id")
    p_pipeline_run.add_argument("--eval-count", type=int, default=96)
    p_pipeline_run.add_argument("--eval-limit", type=int)
    p_pipeline_run.add_argument("--eval-audit-count", type=int, default=144)
    p_pipeline_run.add_argument("--eval-audit-limit", type=int)
    p_pipeline_run.add_argument("--no-eval-audit", action="store_true")
    p_pipeline_run.add_argument("--no-rebuild", action="store_true")
    p_pipeline_run.add_argument("--enable-paddleocr", action="store_true")
    p_pipeline_run.add_argument("--enable-hyper-extract", action="store_true")
    p_pipeline_run.add_argument("--enable-llm-review", action="store_true")
    p_pipeline_run.add_argument("--allow-external-llm", action="store_true")
    p_pipeline_run.add_argument("--enable-apodex", action="store_true")
    p_pipeline_run.add_argument("--no-quality-audit", action="store_true")

    sub.add_parser("pipeline-report", help="Show latest automation pipeline report")
    p_quality_audit = sub.add_parser("quality-audit", help="Run full corpus loop-check audit")
    p_quality_audit.add_argument("--doc-id")
    p_quality_audit.add_argument("--max-chunk-issues-per-doc", type=int, default=40)
    p_quality_audit.add_argument("--no-low-risk", action="store_true")
    p_quality_audit.add_argument("--strict", action="store_true")
    sub.add_parser("quality-audit-report", help="Show latest corpus quality audit report")

    p_semantic_review = sub.add_parser("semantic-review", help="Create LLM semantic Markdown review proposals")
    p_semantic_review.add_argument("--doc-id")
    p_semantic_review.add_argument("--limit", type=int, default=12)
    p_semantic_review.add_argument("--enable-llm-review", action="store_true")
    p_semantic_review.add_argument("--enable-apodex", action="store_true")
    p_semantic_review.add_argument("--allow-external", action="store_true")

    p_quality_loop = sub.add_parser("quality-loop", help="Run iterative data quality loop")
    p_quality_loop.add_argument("--doc-id")
    p_quality_loop.add_argument("--max-iterations", type=int, default=3)
    p_quality_loop.add_argument("--review-limit", type=int, default=12)
    p_quality_loop.add_argument("--enable-llm-review", action="store_true")
    p_quality_loop.add_argument("--enable-apodex", action="store_true")
    p_quality_loop.add_argument("--allow-external", action="store_true")
    p_quality_loop.add_argument("--apply-mode", choices=["proposal_only", "safe_auto_patch"], default="proposal_only")
    p_quality_loop.add_argument("--keep-going", action="store_true")

    p_ocr_retry = sub.add_parser("ocr-retry", help="Run local OCR retry for queued conversion gaps")
    p_ocr_retry.add_argument("--doc-id")
    p_ocr_retry.add_argument("--candidates-path")
    p_ocr_retry.add_argument("--limit", type=int)

    p_enrich = sub.add_parser("enrich-data", help="Build source-derived technical enrichment sidecars")
    p_enrich.add_argument("--doc-id")
    p_enrich.add_argument("--limit", type=int)

    sub.add_parser("pipeline", help="Run inventory, convert, normalize, rebuild-index, eval-seed")
    sub.add_parser("serve", help="Start local API/UI")

    args = parser.parse_args(argv)
    settings = Settings.from_env(Path(args.root).resolve())
    settings.ensure_dirs()

    if args.command == "inventory":
        emit(run_inventory(settings, relative_root=args.relative_root).to_dict())
    elif args.command == "convert":
        emit(
            convert_manifest(
                settings,
                manifest_path=Path(args.manifest_path) if args.manifest_path else None,
                profile=args.profile,
                limit=args.limit,
                doc_id=args.doc_id,
            )
        )
    elif args.command == "normalize":
        emit(
            normalize_manifest(
                settings,
                manifest_path=Path(args.manifest_path) if args.manifest_path else None,
                limit=args.limit,
                doc_id=args.doc_id,
            )
        )
    elif args.command == "rebuild-index":
        emit(rebuild_index(settings, push_qdrant=args.push_qdrant))
    elif args.command == "search":
        filters = {key: value for key, value in {"model": args.model, "doc_type": args.doc_type}.items() if value}
        emit({"hits": [hit.to_dict() for hit in search(settings, args.query, filters=filters, limit=args.limit)]})
    elif args.command == "chat":
        emit(
            ask(
                settings,
                args.question,
                session_id=args.session_id,
                overrides={"model": args.model, "doc_type": args.doc_type, "software_version": args.software_version},
                use_llm=not args.no_llm,
                allow_external_llm=True if args.allow_external_llm else None,
            )
        )
    elif args.command == "eval-seed":
        emit(generate_seed_cases(settings, count=args.count))
    elif args.command == "eval-run":
        emit(run_eval(settings, cases_path=Path(args.cases_path) if args.cases_path else None, limit=args.limit))
    elif args.command == "eval-audit":
        emit(
            run_eval_audit(
                settings,
                count=args.count,
                seed=args.seed,
                cases_path=Path(args.cases_path) if args.cases_path else None,
                limit=args.limit,
            )
        )
    elif args.command == "pipeline-run":
        emit(
            run_automation_pipeline(
                settings,
                PipelineOptions(
                    relative_root=args.relative_root,
                    profile=args.profile,
                    limit=args.limit,
                    doc_id=args.doc_id,
                    rebuild=not args.no_rebuild,
                    eval_count=args.eval_count,
                    eval_limit=args.eval_limit,
                    eval_audit_count=args.eval_audit_count,
                    eval_audit_limit=args.eval_audit_limit,
                    enable_eval_audit=not args.no_eval_audit,
                    enable_paddleocr=True if args.enable_paddleocr else None,
                    enable_hyper_extract=True if args.enable_hyper_extract else None,
                    enable_llm_review=True if args.enable_llm_review else None,
                    allow_external_llm=True if args.allow_external_llm else None,
                    enable_apodex=True if args.enable_apodex else None,
                    enable_quality_audit=not args.no_quality_audit,
                ),
            )
        )
    elif args.command == "pipeline-report":
        emit(read_latest_pipeline_report(settings))
    elif args.command == "quality-audit":
        emit(
            run_quality_audit(
                settings,
                QualityAuditOptions(
                    doc_id=args.doc_id,
                    max_chunk_issues_per_doc=args.max_chunk_issues_per_doc,
                    include_low_risk=not args.no_low_risk,
                    strict=args.strict,
                ),
            )
        )
    elif args.command == "quality-audit-report":
        emit(read_latest_quality_audit_report(settings))
    elif args.command == "semantic-review":
        emit(
            run_apodex_semantic_review(
                settings,
                SemanticReviewOptions(
                    doc_id=args.doc_id,
                    limit=args.limit,
                    enable_llm_review=True if args.enable_llm_review else None,
                    enable_apodex=True if args.enable_apodex else None,
                    allow_external=True if args.allow_external else None,
                ),
            )
        )
    elif args.command == "quality-loop":
        emit(
            run_quality_loop(
                settings,
                QualityLoopOptions(
                    doc_id=args.doc_id,
                    max_iterations=args.max_iterations,
                    review_limit=args.review_limit,
                    enable_llm_review=True if args.enable_llm_review else None,
                    enable_apodex=True if args.enable_apodex else None,
                    allow_external=True if args.allow_external else None,
                    apply_mode=args.apply_mode,
                    stop_when_no_candidates=not args.keep_going,
                ),
            )
        )
    elif args.command == "ocr-retry":
        emit(
            run_ocr_retry_candidates(
                settings,
                load_ocr_candidates(settings, Path(args.candidates_path) if args.candidates_path else None),
                doc_id=args.doc_id,
                limit=args.limit,
            )
        )
    elif args.command == "enrich-data":
        emit(run_technical_enrichment(settings, doc_id=args.doc_id, limit=args.limit))
    elif args.command == "pipeline":
        steps: dict[str, Any] = {}
        steps["inventory"] = run_inventory(settings).to_dict()
        steps["convert"] = convert_manifest(settings)
        steps["normalize"] = normalize_manifest(settings)
        steps["enrichment"] = run_technical_enrichment(settings)
        steps["index"] = rebuild_index(settings)
        steps["eval_seed"] = generate_seed_cases(settings, count=96)
        steps["eval_run"] = run_eval(settings)
        steps["eval_audit"] = run_eval_audit(settings)
        emit({"ok": True, "steps": steps})
    elif args.command == "serve":
        from .api import main as serve_main

        serve_main()


def emit(payload: Any) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def read_latest_pipeline_report(settings: Settings) -> dict[str, Any]:
    from .utils import read_json

    return read_json(settings.artifacts_dir / "pipeline" / "latest_report.json", {})


def read_latest_quality_audit_report(settings: Settings) -> dict[str, Any]:
    from .utils import read_json

    return read_json(settings.artifacts_dir / "quality_audit" / "latest_report.json", {})


def load_ocr_candidates(settings: Settings, candidates_path: Path | None) -> list[dict[str, Any]]:
    from .utils import read_json

    if candidates_path:
        payload = read_json(candidates_path, {})
    else:
        report = read_latest_pipeline_report(settings)
        stage = next((item for item in report.get("stages", []) if item.get("name") == "conversion_quality_gate"), {})
        payload = stage.get("summary", {}) if isinstance(stage, dict) else {}
    candidates = payload.get("ocr_candidates", []) if isinstance(payload, dict) else []
    return [item for item in candidates if isinstance(item, dict)]


if __name__ == "__main__":
    main()
