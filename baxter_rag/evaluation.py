from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .chat import ask
from .settings import Settings
from .utils import normalized_text, read_json, utc_now, write_json


@dataclass
class EvalCase:
    case_id: str
    question: str
    expected_behavior: str
    tags: list[str]
    expected_model: str | None = None
    expected_doc_type: str | None = None
    expected_terms: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def generate_seed_cases(settings: Settings, *, count: int = 96) -> dict[str, Any]:
    manifest = read_json(settings.artifacts_dir / "phase0" / "source_manifest.json", {})
    records = manifest.get("records") or []
    discovered_models = {record.get("model") for record in records if record.get("model")}
    preferred_models = ["PrisMax", "Prismaflex", "TherMax"]
    models = [model for model in preferred_models if model in discovered_models]
    if not models:
        models = sorted(model for model in discovered_models if model != "Unknown")
    doc_types = sorted({record.get("doc_type") for record in records if record.get("doc_type")})
    cases: list[EvalCase] = []
    templates = [
        (
            "{model} có alarm liên quan áp lực thì kiểm tra những gì?",
            "answer_with_citation",
            ["alarm", "vi_en"],
            "service_manual",
            ["pressure"],
        ),
        (
            "Quy trình service {model} khi thay linh kiện cần lưu ý gì?",
            "answer_with_citation",
            ["service"],
            "service_manual",
            ["service"],
        ),
        (
            "Tìm thông tin PM checklist của {model}.",
            "answer_with_citation",
            ["maintenance"],
            None,
            ["pm", "maintenance"],
        ),
        (
            "{model} SW {version} có tài liệu nào liên quan?",
            "answer_with_citation",
            ["software"],
            None,
            ["sw"],
        ),
        (
            "Thông số kỹ thuật quan trọng của {model} nằm ở đâu?",
            "answer_with_citation",
            ["spec"],
            None,
            ["specification", "technical", "thong so"],
        ),
        (
            "So sánh tài liệu service manual và operator manual của {model}.",
            "answer_with_citation",
            ["compare"],
            None,
            ["service", "operator"],
        ),
        (
            "Lỗi Access Pressure trên {model} xử lý thế nào?",
            "answer_with_citation",
            ["alarm", "english_term"],
            "service_manual",
            ["access", "pressure"],
        ),
        (
            "Có cảnh báo an toàn nào cần đọc trước khi service {model}?",
            "answer_with_citation",
            ["safety"],
            "service_manual",
            ["warning", "caution", "service"],
        ),
        (
            "Mã tài liệu {code} nói về nội dung gì?",
            "answer_with_citation",
            ["exact_code"],
            None,
            [],
        ),
        (
            "Máy pha cà phê báo lỗi E99 thì làm sao?",
            "no_source",
            ["out_of_scope"],
            None,
            [],
        ),
    ]
    record_codes = [
        record.get("doc_code") or record.get("doc_id")
        for record in records
        if record.get("doc_code") or record.get("doc_id")
    ]
    versions_by_model: dict[str, list[str]] = {}
    doc_types_by_model: dict[str, set[str]] = {}
    for record in records:
        model = record.get("model")
        version = record.get("software_version")
        doc_type = record.get("doc_type")
        if model and version:
            versions_by_model.setdefault(model, [])
            if version not in versions_by_model[model]:
                versions_by_model[model].append(version)
        if model and doc_type:
            doc_types_by_model.setdefault(model, set()).add(str(doc_type))
    versions = [record.get("software_version") for record in records if record.get("software_version")] or ["3.x", "8.xx"]
    idx = 1
    while len(cases) < count:
        for model in models or ["PrisMax", "Prismaflex"]:
            for template, expected, tags, expected_doc_type, expected_terms in templates:
                code = record_codes[(idx - 1) % len(record_codes)] if record_codes else "AW8047"
                version_pool = versions_by_model.get(model) or versions
                version = version_pool[(idx - 1) % len(version_pool)]
                question = template.format(model=model, code=code, version=version)
                cases.append(
                    EvalCase(
                        case_id=f"baxter_seed_{idx:03d}",
                        question=question,
                        expected_behavior=expected,
                        tags=tags,
                        expected_model=model if model in question else None,
                        expected_doc_type=expected_doc_type
                        if expected_doc_type in doc_types_by_model.get(model, set())
                        else None,
                        expected_terms=expected_terms,
                    )
                )
                idx += 1
                if len(cases) >= count:
                    break
            if len(cases) >= count:
                break
    payload = {
        "schema_version": "baxter_eval_seed_v1",
        "generated_at": utc_now(),
        "case_count": len(cases),
        "doc_types": doc_types,
        "cases": [case.to_dict() for case in cases],
    }
    path = settings.root / "eval" / "baxter_seed_cases.json"
    write_json(path, payload)
    return {**payload, "path": str(path)}


def run_eval(settings: Settings, *, cases_path: Path | None = None, limit: int | None = None) -> dict[str, Any]:
    cases_path = cases_path or settings.root / "eval" / "baxter_seed_cases.json"
    payload = read_json(cases_path, {})
    cases = payload.get("cases") or []
    if limit is not None:
        cases = cases[: max(0, limit)]
    results: list[dict[str, Any]] = []
    for case in cases:
        response = ask(settings, str(case.get("question") or ""), use_llm=False)
        citations = response.get("citations") or []
        hits = response.get("hits") or []
        expected = case.get("expected_behavior")
        expected_model = case.get("expected_model")
        expected_doc_type = case.get("expected_doc_type")
        required_terms = [normalized_text(item) for item in (case.get("expected_terms") or []) if item]
        hit_models = hit_metadata_values(hits, "model")
        hit_doc_types = hit_metadata_values(hits, "doc_type")
        evidence_text = normalized_text(" ".join(str(hit.get("content") or "") for hit in hits[:5]))

        if expected == "answer_with_citation":
            behavior_passed = bool(citations)
        else:
            behavior_passed = response.get("answer_source") in {"no_source", "clarification", "no_source_in_scope"}
        model_passed = not expected_model or expected_model in hit_models
        doc_type_passed = not expected_doc_type or expected_doc_type in hit_doc_types
        terms_passed = not required_terms or any(term in evidence_text for term in required_terms)
        passed = behavior_passed and model_passed and doc_type_passed and terms_passed
        root_cause = None if passed else classify_eval_failure(
            expected_behavior=str(expected or ""),
            behavior_passed=behavior_passed,
            model_passed=model_passed,
            doc_type_passed=doc_type_passed,
            terms_passed=terms_passed,
            response=response,
        )
        results.append(
            {
                "case_id": case.get("case_id"),
                "question": case.get("question"),
                "expected_behavior": expected,
                "expected_model": expected_model,
                "expected_doc_type": expected_doc_type,
                "expected_terms": required_terms,
                "hit_models": hit_models,
                "hit_doc_types": hit_doc_types,
                "passed": passed,
                "answer_source": response.get("answer_source"),
                "citation_count": len(citations),
                "no_answer_reason": response.get("no_answer_reason"),
                "root_cause": root_cause,
            }
        )
    summary = {
        "schema_version": "baxter_eval_run_v1",
        "generated_at": utc_now(),
        "cases_path": str(cases_path),
        "case_count": len(results),
        "passed": sum(1 for item in results if item["passed"]),
        "failed": sum(1 for item in results if not item["passed"]),
        "results": results,
    }
    summary["root_cause_path"] = write_eval_root_cause(settings, summary)
    out_path = settings.root / "eval" / "baxter_eval_latest.json"
    write_json(out_path, summary)
    return {**summary, "path": str(out_path)}


def hit_metadata_values(hits: list[dict[str, Any]], key: str) -> list[str]:
    return sorted(
        {
            str((hit.get("metadata") or {}).get(key) or "")
            for hit in hits
            if (hit.get("metadata") or {}).get(key)
        }
    )


def classify_eval_failure(
    *,
    expected_behavior: str,
    behavior_passed: bool,
    model_passed: bool,
    doc_type_passed: bool,
    terms_passed: bool,
    response: dict[str, Any],
) -> str:
    if not behavior_passed:
        if response.get("no_answer_reason"):
            return "retrieval_miss"
        if expected_behavior == "no_source":
            return "policy_block"
        return "context_insufficient"
    if not model_passed:
        return "metadata_gap"
    if not doc_type_passed:
        return "retrieval_miss"
    if not terms_passed:
        return "context_insufficient"
    return "eval_gap"


def write_eval_root_cause(settings: Settings, summary: dict[str, Any]) -> str:
    counts: dict[str, int] = {}
    failures = []
    for result in summary.get("results", []):
        root_cause = result.get("root_cause")
        if not root_cause:
            continue
        counts[root_cause] = counts.get(root_cause, 0) + 1
        failures.append(result)
    payload = {
        "schema_version": "baxter_eval_root_cause_rollup_v1",
        "generated_at": utc_now(),
        "cases_path": summary.get("cases_path"),
        "case_count": summary.get("case_count"),
        "passed": summary.get("passed"),
        "failed": summary.get("failed"),
        "counts": counts,
        "failures": failures,
    }
    out_path = settings.artifacts_dir / "root_cause" / "eval_latest_root_cause.json"
    write_json(out_path, payload)
    return str(out_path)
