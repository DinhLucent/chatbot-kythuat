from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from random import Random
from typing import Any

from .chat import ask
from .retrieval import load_chunks
from .settings import Settings
from .utils import keyword_codes, normalized_text, read_json, short_hash, tokenize, utc_now, write_json
from .workspaces import list_source_workspaces


AUDIT_BLOCK_ORDER = [
    "alarm_code",
    "troubleshooting_fix",
    "troubleshooting_cause",
    "troubleshooting_symptom",
    "service_step",
    "maintenance_step",
    "calibration_step",
    "warning_safety",
    "software_note",
    "prose",
]

BLOCK_EXPECTED_TERMS = {
    "alarm_code": ["alarm", "error", "loi", "pressure", "ap luc", "access", "return", "cause", "nguyen nhan", "corrective"],
    "troubleshooting_fix": ["fix", "corrective", "khac phuc", "replace", "thay", "service", "check", "kiem tra", "pressure"],
    "troubleshooting_cause": ["cause", "nguyen nhan", "possible", "pressure", "sensor", "pump"],
    "troubleshooting_symptom": ["symptom", "trieu chung", "alarm", "error", "loi", "pressure"],
    "service_step": ["service", "replace", "thay", "remove", "install", "procedure", "kiem tra"],
    "maintenance_step": ["maintenance", "bao tri", "pm", "preventive", "checklist", "replace", "thay"],
    "calibration_step": ["calibration", "hieu chuan", "calibrate", "test", "check", "kiem tra"],
    "warning_safety": ["warning", "canh bao", "caution", "chu y", "safety", "an toan", "hazard", "patient", "service"],
    "software_note": ["software", "sw", "version", "phien ban", "release", "update"],
    "prose": ["specification", "technical", "operation", "manual", "document"],
}

STOPWORDS = {
    "about",
    "above",
    "after",
    "before",
    "baxter",
    "ban",
    "chapter",
    "chuong",
    "dan",
    "document",
    "doi",
    "equipment",
    "following",
    "function",
    "huong",
    "loi",
    "manual",
    "model",
    "nhung",
    "page",
    "phien",
    "press",
    "program",
    "prismax",
    "prismaflex",
    "procedure",
    "recommended",
    "section",
    "service",
    "specifications",
    "sua",
    "system",
    "technical",
    "thich",
    "thuat",
    "there",
    "this",
    "trinh",
    "trong",
    "version",
    "with",
}

GENERIC_AUDIT_TERMS = {
    "approvals",
    "associated",
    "checklist",
    "description",
    "documentation",
    "function",
    "information",
    "loi",
    "maintenance",
    "nhung",
    "pm",
    "press",
    "program",
    "recommended",
    "release",
    "revision",
    "service",
    "signature",
    "signatures",
    "summary",
    "specifications",
    "technical",
    "thich",
    "thuat",
    "these",
    "trong",
    "version",
    "workflow",
    "executive",
    "provides",
}

GENERIC_HINT_TERMS = GENERIC_AUDIT_TERMS | {
    "alarm",
    "alarms",
    "caution",
    "cause",
    "corrective",
    "error",
    "patient",
    "replace",
    "release",
    "repair",
    "software",
    "sw",
    "troubleshooting",
    "update",
    "updates",
    "warning",
}


@dataclass
class EvalCase:
    case_id: str
    question: str
    expected_behavior: str
    tags: list[str]
    expected_model: str | None = None
    expected_doc_type: str | None = None
    expected_terms: list[str] | None = None
    expected_doc_id: str | None = None
    expected_chunk_id: str | None = None
    expected_block_type: str | None = None
    expected_doc_code: str | None = None
    source_workspace_id: str | None = None
    audit_basis: dict[str, Any] | None = None

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
            "{model} co alarm lien quan ap luc thi kiem tra nhung gi?",
            "answer_with_citation",
            ["seed", "alarm", "vi_en"],
            "service_manual",
            ["pressure"],
        ),
        (
            "Quy trinh service {model} khi thay linh kien can luu y gi?",
            "answer_with_citation",
            ["seed", "service"],
            "service_manual",
            ["service"],
        ),
        (
            "Tim thong tin PM checklist cua {model}.",
            "answer_with_citation",
            ["seed", "maintenance"],
            None,
            ["pm", "maintenance"],
        ),
        (
            "{model} SW {version} co tai lieu nao lien quan?",
            "answer_with_citation",
            ["seed", "software"],
            None,
            ["sw"],
        ),
        (
            "Thong so ky thuat quan trong cua {model} nam o dau?",
            "answer_with_citation",
            ["seed", "spec"],
            None,
            ["specification", "technical", "thong so"],
        ),
        (
            "So sanh service manual va operator manual cua {model}.",
            "answer_with_citation",
            ["seed", "compare"],
            None,
            ["service", "operator"],
        ),
        (
            "Loi Access Pressure tren {model} xu ly the nao?",
            "answer_with_citation",
            ["seed", "alarm", "english_term"],
            "service_manual",
            ["access", "pressure"],
        ),
        (
            "Co canh bao an toan nao can doc truoc khi service {model}?",
            "answer_with_citation",
            ["seed", "safety"],
            "service_manual",
            ["warning", "caution", "service"],
        ),
        (
            "Ma tai lieu {code} noi ve noi dung gi?",
            "answer_with_citation",
            ["seed", "exact_code"],
            None,
            [],
        ),
        (
            "May pha ca phe bao loi E99 thi lam sao?",
            "no_source",
            ["seed", "out_of_scope"],
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
                case_expected, case_doc_type, case_terms, case_model = seed_expectation(
                    model=model,
                    question=question,
                    expected=expected,
                    tags=tags,
                    expected_doc_type=expected_doc_type,
                    expected_terms=expected_terms,
                    doc_types_by_model=doc_types_by_model,
                )
                cases.append(
                    EvalCase(
                        case_id=f"baxter_seed_{idx:03d}",
                        question=question,
                        expected_behavior=case_expected,
                        tags=tags,
                        expected_model=case_model,
                        expected_doc_type=case_doc_type,
                        expected_terms=case_terms,
                    )
                )
                idx += 1
                if len(cases) >= count:
                    break
            if len(cases) >= count:
                break
    payload = {
        "schema_version": "baxter_eval_seed_v2",
        "generated_at": utc_now(),
        "case_count": len(cases),
        "doc_types": doc_types,
        "cases": [case.to_dict() for case in cases],
    }
    path = settings.root / "eval" / "baxter_seed_cases.json"
    write_json(path, payload)
    return {**payload, "path": str(path)}


def seed_expectation(
    *,
    model: str,
    question: str,
    expected: str,
    tags: list[str],
    expected_doc_type: str | None,
    expected_terms: list[str],
    doc_types_by_model: dict[str, set[str]],
) -> tuple[str, str | None, list[str], str | None]:
    model_docs = doc_types_by_model.get(model, set())
    case_model = model if model in question else None
    case_doc_type = expected_doc_type if expected_doc_type in model_docs else None
    case_terms = list(expected_terms)
    case_expected = expected
    if "maintenance" in tags and "checklist" not in model_docs:
        return "no_source", None, [], None
    if "service" in tags:
        case_doc_type = None
        case_terms = []
    if "spec" in tags:
        case_terms = ["specification"] if model == "TherMax" else ["thong so"]
    if "compare" in tags:
        case_terms = ["service"]
    if "safety" in tags:
        case_doc_type = None
        case_terms = ["service"]
    return case_expected, case_doc_type, case_terms, case_model


def generate_audit_cases(settings: Settings, *, count: int = 144, seed: int = 20260621) -> dict[str, Any]:
    chunks = [chunk for chunk in load_chunks(settings) if auditable_chunk(chunk)]
    rng = Random(seed)
    reserved = 5
    selected = stratified_audit_chunks(chunks, target=max(0, count - reserved), rng=rng)
    cases: list[EvalCase] = []
    for chunk in selected:
        cases.append(case_from_chunk(chunk, len(cases) + 1, seed=seed))

    cases.extend(workspace_scope_cases(chunks, start_index=len(cases) + 1, seed=seed, rng=rng))
    cases.extend(policy_cases(start_index=len(cases) + 1))
    if len(cases) > count:
        cases = cases[:count]

    payload = {
        "schema_version": "baxter_eval_audit_cases_v1",
        "generated_at": utc_now(),
        "seed": seed,
        "requested_count": count,
        "case_count": len(cases),
        "selection_strategy": "stratified_index_random_plus_workspace_and_policy_cases",
        "block_counts": dict(Counter(case_block_bucket(case) for case in cases)),
        "cases": [case.to_dict() for case in cases],
    }
    path = settings.artifacts_dir / "evaluation" / "audit_cases_latest.json"
    write_json(path, payload)
    return {**payload, "path": str(path)}


def run_eval(settings: Settings, *, cases_path: Path | None = None, limit: int | None = None) -> dict[str, Any]:
    cases_path = cases_path or settings.root / "eval" / "baxter_seed_cases.json"
    if not cases_path.exists():
        generate_seed_cases(settings)
    return run_case_suite(
        settings,
        cases_path=cases_path,
        limit=limit,
        suite="seed",
        latest_path=settings.root / "eval" / "baxter_eval_latest.json",
    )


def run_eval_audit(
    settings: Settings,
    *,
    count: int = 144,
    seed: int = 20260621,
    cases_path: Path | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    if cases_path is None:
        generated = generate_audit_cases(settings, count=count, seed=seed)
        cases_path = Path(str(generated["path"]))
    return run_case_suite(
        settings,
        cases_path=cases_path,
        limit=limit,
        suite="audit",
        latest_path=settings.artifacts_dir / "evaluation" / "audit_latest.json",
        metadata={"seed": seed, "requested_count": count},
    )


def run_case_suite(
    settings: Settings,
    *,
    cases_path: Path,
    suite: str,
    latest_path: Path,
    limit: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = read_json(cases_path, {})
    cases = payload.get("cases") or []
    if limit is not None:
        cases = cases[: max(0, limit)]

    run_id = build_eval_run_id(suite)
    run_dir = settings.artifacts_dir / "evaluation" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for case in cases:
        response = ask_for_case(settings, case)
        results.append(score_case(case, response))

    failed = sum(1 for item in results if not item["passed"])
    summary = {
        "schema_version": "baxter_eval_run_v2",
        "generated_at": utc_now(),
        "suite": suite,
        "run_id": run_id,
        "run_dir": str(run_dir),
        "cases_path": str(cases_path),
        "case_count": len(results),
        "passed": sum(1 for item in results if item["passed"]),
        "failed": failed,
        "pass_rate": round((len(results) - failed) / max(1, len(results)), 4) if results else None,
        "criteria_counts": criteria_counts(results),
        "root_cause_counts": root_cause_counts(results),
        "metadata": metadata or {},
        "results": results,
    }
    summary["root_cause_path"] = write_eval_root_cause(settings, summary, suite=suite, run_dir=run_dir)
    write_json(run_dir / "results.json", summary)
    write_json(latest_path, summary)
    return {**summary, "path": str(latest_path)}


def ask_for_case(settings: Settings, case: dict[str, Any]) -> dict[str, Any]:
    source_workspace_id = clean_str(case.get("source_workspace_id"))
    overrides = {"source_workspace_id": source_workspace_id} if source_workspace_id else {}
    return ask(
        settings,
        str(case.get("question") or ""),
        overrides=overrides,
        use_llm=False,
        source_workspace_id=source_workspace_id,
    )


def score_case(case: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    citations = response.get("citations") or []
    hits = response.get("hits") or []
    expected = str(case.get("expected_behavior") or "")
    expected_model = clean_str(case.get("expected_model"))
    expected_doc_type = clean_str(case.get("expected_doc_type"))
    expected_doc_id = clean_str(case.get("expected_doc_id"))
    expected_chunk_id = clean_str(case.get("expected_chunk_id"))
    expected_block_type = clean_str(case.get("expected_block_type"))
    expected_doc_code = clean_str(case.get("expected_doc_code"))
    source_workspace_id = clean_str(case.get("source_workspace_id"))
    required_terms = [normalized_text(item) for item in (case.get("expected_terms") or []) if normalized_text(item)]

    hit_models = hit_metadata_values(hits, "model")
    hit_doc_types = hit_metadata_values(hits, "doc_type")
    hit_doc_codes = hit_metadata_values(hits, "doc_code")
    hit_block_types = hit_metadata_values(hits, "block_type")
    hit_doc_ids = sorted({str(hit.get("doc_id") or "") for hit in hits if hit.get("doc_id")})
    hit_chunk_ids = [str(hit.get("chunk_id") or "") for hit in hits if hit.get("chunk_id")]
    evidence_text = normalized_text(
        " ".join(
            str(hit.get("content") or "") + " " + str(hit.get("expanded_context") or "")
            for hit in hits[:5]
        )
    )
    answer_text = normalized_text(response.get("answer") or "")
    answer_source = str(response.get("answer_source") or "")

    answer_expected = expected == "answer_with_citation"
    if answer_expected:
        behavior_passed = bool(citations) and answer_source not in {"no_source", "no_source_in_scope", "clarification"}
    elif expected == "clarification":
        behavior_passed = answer_source == "clarification"
    else:
        behavior_passed = answer_source in {"no_source", "clarification", "no_source_in_scope"}

    criteria: list[dict[str, Any]] = []
    criteria.append(
        criterion(
            "behavior_contract",
            behavior_passed,
            f"expected={expected} answer_source={answer_source}",
        )
    )
    if answer_expected:
        criteria.append(
            criterion(
                "citation_contract",
                bool(citations) and all(citation.get("citation_id") for citation in citations),
                f"citation_count={len(citations)}",
            )
        )
    else:
        criteria.append(
            criterion(
                "no_source_contract",
                not citations and not hits,
                f"citation_count={len(citations)} hit_count={len(hits)}",
            )
        )
    if expected_model:
        criteria.append(criterion("model_match", expected_model in hit_models, f"expected={expected_model} hits={hit_models}"))
    if source_workspace_id and expected_model:
        scoped_models = {str((hit.get("metadata") or {}).get("model") or "") for hit in hits if hit.get("metadata")}
        criteria.append(
            criterion(
                "workspace_scope",
                bool(hits) and scoped_models == {expected_model},
                f"workspace={source_workspace_id} expected_model={expected_model} hit_models={sorted(scoped_models)}",
            )
        )
    if expected_doc_type:
        criteria.append(
            criterion(
                "doc_type_match",
                expected_doc_type in hit_doc_types,
                f"expected={expected_doc_type} hits={hit_doc_types}",
            )
        )
    if expected_doc_id:
        criteria.append(
            criterion(
                "doc_id_match",
                expected_doc_id in hit_doc_ids,
                f"expected={expected_doc_id}",
                required=not bool(expected_doc_code),
            )
        )
    if expected_chunk_id:
        criteria.append(criterion("chunk_id_seen", expected_chunk_id in hit_chunk_ids, f"expected={expected_chunk_id}", required=False))
    if expected_block_type:
        criteria.append(
            criterion(
                "block_type_match",
                expected_block_type in hit_block_types,
                f"expected={expected_block_type} hits={hit_block_types}",
            )
        )
    if expected_doc_code:
        criteria.append(
            criterion(
                "doc_code_match",
                expected_doc_code in hit_doc_codes,
                f"expected={expected_doc_code} hits={hit_doc_codes}",
            )
        )
    if required_terms:
        term_hits = [term for term in required_terms if term_matches_text(term, evidence_text) or term_matches_text(term, answer_text)]
        threshold = min(2, len(required_terms))
        criteria.append(
            criterion(
                "expected_terms",
                len(term_hits) >= threshold,
                f"required={required_terms} matched={term_hits} threshold={threshold}",
                details={"required_terms": required_terms, "matched_terms": term_hits, "threshold": threshold},
            )
        )

    required_criteria = [item for item in criteria if item.get("required", True)]
    passed = all(item["passed"] for item in required_criteria)
    root_cause = None if passed else classify_eval_failure(criteria, expected_behavior=expected, response=response)
    return {
        "case_id": case.get("case_id"),
        "question": case.get("question"),
        "expected_behavior": expected,
        "tags": case.get("tags") or [],
        "expected_model": expected_model,
        "expected_doc_type": expected_doc_type,
        "expected_doc_id": expected_doc_id,
        "expected_chunk_id": expected_chunk_id,
        "expected_block_type": expected_block_type,
        "expected_doc_code": expected_doc_code,
        "expected_terms": required_terms,
        "source_workspace_id": source_workspace_id,
        "audit_basis": case.get("audit_basis") or {},
        "hit_models": hit_models,
        "hit_doc_types": hit_doc_types,
        "hit_doc_ids": hit_doc_ids[:10],
        "hit_block_types": hit_block_types,
        "hit_doc_codes": hit_doc_codes,
        "hit_chunk_ids": hit_chunk_ids[:10],
        "passed": passed,
        "score": round(sum(1 for item in criteria if item["passed"]) / max(1, len(criteria)), 4),
        "criteria": criteria,
        "answer_source": answer_source,
        "citation_count": len(citations),
        "hit_count": len(hits),
        "no_answer_reason": response.get("no_answer_reason"),
        "root_cause": root_cause,
    }


def auditable_chunk(chunk: dict[str, Any]) -> bool:
    content = str(chunk.get("content") or "").strip()
    norm = normalized_text(content)
    if len(content) < 80 or len(tokenize(content)) < 8:
        return False
    if "trang nay duoc chu y de trong" in norm or "intentionally blank" in norm:
        return False
    if str(chunk.get("model") or "") in {"", "Unknown"}:
        return False
    if not chunk.get("doc_type"):
        return False
    if str(chunk.get("block_type") or "prose") not in set(AUDIT_BLOCK_ORDER):
        return False
    if weak_software_note_chunk(chunk):
        return False
    terms = expected_terms_from_chunk(chunk)
    return len(terms) >= 2


def weak_software_note_chunk(chunk: dict[str, Any]) -> bool:
    if str(chunk.get("block_type") or "") != "software_note":
        return False
    norm = normalized_text(chunk.get("content") or "")
    software_signals = ("software", "sw ", " release", " revision", " update", " upgrade", "firmware")
    has_substantive_signal = any(signal in f" {norm} " for signal in software_signals) or bool(re.search(r"\bsw[0-9]", norm))
    if "program version" in norm and not has_substantive_signal:
        return True
    if ("service manual maintenance" in norm or "service manual - maintenance" in norm) and not has_substantive_signal:
        return True
    return False


def stratified_audit_chunks(chunks: list[dict[str, Any]], *, target: int, rng: Random) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {block: [] for block in AUDIT_BLOCK_ORDER}
    for chunk in chunks:
        buckets.setdefault(str(chunk.get("block_type") or "prose"), []).append(chunk)
    for bucket in buckets.values():
        rng.shuffle(bucket)

    selected: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    while len(selected) < target:
        progressed = False
        for block in AUDIT_BLOCK_ORDER:
            bucket = buckets.get(block) or []
            while bucket:
                chunk = bucket.pop()
                key = f"{chunk.get('doc_id')}:{chunk.get('block_type')}:{chunk.get('section_path')}"
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                selected.append(chunk)
                progressed = True
                break
            if len(selected) >= target:
                break
        if not progressed:
            break
    return selected


def case_from_chunk(chunk: dict[str, Any], index: int, *, seed: int) -> EvalCase:
    model = clean_str(chunk.get("model"))
    doc_type = clean_str(chunk.get("doc_type"))
    doc_code = clean_str(chunk.get("doc_code"))
    terms = expected_terms_from_chunk(chunk)
    raw_block_type = clean_str(chunk.get("block_type")) or "prose"
    block_type = canonical_expected_block_type(raw_block_type, terms)
    hint_terms = question_hint_terms(terms)
    hint = ", ".join(hint_terms) if hint_terms else (block_type or raw_block_type).replace("_", " ")
    doc_label = doc_type.replace("_", " ") if doc_type else "loaded source"
    code_part = f" document {doc_code}" if doc_code else ""
    block_prompt = {
        "alarm_code": "alarm or troubleshooting content",
        "troubleshooting_fix": "corrective action",
        "troubleshooting_cause": "possible cause",
        "troubleshooting_symptom": "symptom or alarm behavior",
        "service_step": "service procedure",
        "maintenance_step": "maintenance or PM guidance",
        "calibration_step": "calibration guidance",
        "warning_safety": "safety warning or caution",
        "software_note": "software note or version guidance",
        "prose": "technical information",
    }.get(block_type or "prose", "technical information")
    question = f"For {model}, in the {doc_label}{code_part}, what does the {block_prompt} say about {hint}?"
    return EvalCase(
        case_id=f"baxter_audit_{index:03d}",
        question=question,
        expected_behavior="answer_with_citation",
        tags=["audit_random", block_type or raw_block_type, doc_type or "unknown_doc_type"],
        expected_model=model,
        expected_doc_type=doc_type,
        expected_terms=terms,
        expected_doc_id=clean_str(chunk.get("doc_id")),
        expected_chunk_id=clean_str(chunk.get("chunk_id")),
        expected_block_type=block_type,
        expected_doc_code=doc_code,
        audit_basis={
            "source": "index_chunk",
            "seed": seed,
            "chunk_id": chunk.get("chunk_id"),
            "doc_id": chunk.get("doc_id"),
            "section_path": chunk.get("section_path") or [],
            "content_preview": str(chunk.get("content") or "")[:500],
        },
    )


def workspace_scope_cases(
    chunks: list[dict[str, Any]],
    *,
    start_index: int,
    seed: int,
    rng: Random,
) -> list[EvalCase]:
    cases: list[EvalCase] = []
    for workspace in list_source_workspaces():
        model = str(workspace.get("model") or "")
        pool = [chunk for chunk in chunks if str(chunk.get("model") or "") == model and chunk.get("doc_type")]
        if not pool:
            continue
        rng.shuffle(pool)
        chunk = pool[0]
        terms = expected_terms_from_chunk(chunk)
        hint = ", ".join(terms[:2]) if terms else str(chunk.get("block_type") or "technical source")
        case_index = start_index + len(cases)
        cases.append(
            EvalCase(
                case_id=f"baxter_audit_{case_index:03d}",
                question=f"In the selected {workspace.get('label')} workspace, find Baxter guidance about {hint}.",
                expected_behavior="answer_with_citation",
                tags=["audit_workspace_scope", str(workspace.get("workspace_id") or "")],
                expected_model=model,
                expected_doc_type=None,
                expected_terms=terms,
                expected_doc_id=None,
                expected_block_type=None,
                expected_doc_code=None,
                source_workspace_id=str(workspace.get("workspace_id") or ""),
                audit_basis={
                    "source": "workspace_scope",
                    "seed": seed,
                    "chunk_id": chunk.get("chunk_id"),
                    "doc_id": chunk.get("doc_id"),
                },
            )
        )
    return cases


def policy_cases(*, start_index: int) -> list[EvalCase]:
    return [
        EvalCase(
            case_id=f"baxter_audit_{start_index:03d}",
            question="May pha ca phe bao loi E99 thi sua nhu the nao?",
            expected_behavior="no_source",
            tags=["audit_policy", "out_of_scope"],
            expected_terms=[],
            audit_basis={"source": "policy_static", "reason": "non_baxter_device"},
        ),
        EvalCase(
            case_id=f"baxter_audit_{start_index + 1:03d}",
            question="Quy trinh bao tri va alarm pressure can xem nhu the nao?",
            expected_behavior="no_source",
            tags=["audit_policy", "missing_model_scope"],
            expected_terms=[],
            audit_basis={"source": "policy_static", "reason": "technical_query_without_model"},
        ),
    ]


def expected_terms_from_chunk(chunk: dict[str, Any]) -> list[str]:
    content = str(chunk.get("content") or "")
    norm = normalized_text(content)
    block_type = str(chunk.get("block_type") or "prose")
    doc_code = normalized_text(chunk.get("doc_code") or "")
    terms: list[str] = []
    for term in BLOCK_EXPECTED_TERMS.get(block_type, []):
        normalized = normalized_text(term)
        if normalized and normalized in norm and normalized not in GENERIC_AUDIT_TERMS:
            append_unique(terms, normalized)
    for code in keyword_codes(content):
        normalized = normalized_text(code)
        if normalized and normalized != doc_code and normalized.startswith("sw"):
            append_unique(terms, normalized)
    for token in tokenize(content):
        if len(terms) >= 5:
            break
        if weak_term(token):
            continue
        append_unique(terms, token)
    return terms[:5]


def question_hint_terms(terms: list[str]) -> list[str]:
    distinctive = [term for term in terms if not generic_hint_term(term)]
    if len(distinctive) >= 2:
        return distinctive[:3]
    return terms[:3]


def generic_hint_term(term: str) -> bool:
    normalized = normalized_text(term)
    if any(char.isdigit() for char in normalized) and normalized.startswith("sw"):
        return False
    return normalized in GENERIC_HINT_TERMS


def canonical_expected_block_type(raw_block_type: str, terms: list[str]) -> str | None:
    term_text = " ".join(terms)
    if any(term in term_text for term in ("software", "sw", "release", "phien ban")):
        return "software_note"
    if raw_block_type == "prose":
        return None
    return raw_block_type


def hit_metadata_values(hits: list[dict[str, Any]], key: str) -> list[str]:
    return sorted(
        {
            str((hit.get("metadata") or {}).get(key) or "")
            for hit in hits
            if (hit.get("metadata") or {}).get(key)
        }
    )


def criterion(name: str, passed: bool, reason: str, *, required: bool = True, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "passed": bool(passed),
        "required": required,
        "reason": reason,
        "details": details or {},
    }


def term_matches_text(term: str, text: str) -> bool:
    if term in text:
        return True
    return any(variant and variant in text for variant in software_term_variants(term))


def software_term_variants(term: str) -> set[str]:
    normalized = normalized_text(term)
    variants = {normalized}
    for match in re.finditer(r"\bsw\s*([0-9]+(?:[._-][0-9x]+)+)\b", normalized):
        version = match.group(1).replace("_", ".").replace("-", ".")
        variants.add(f"sw {version}")
        variants.add(f"software version {version}")
    return variants


def classify_eval_failure(
    criteria: list[dict[str, Any]],
    *,
    expected_behavior: str,
    response: dict[str, Any],
) -> str:
    failed = {item["name"]: item for item in criteria if item.get("required", True) and not item.get("passed")}
    if "behavior_contract" in failed:
        if expected_behavior == "no_source":
            return "policy_contract_gap"
        if response.get("no_answer_reason"):
            return "retrieval_miss"
        return "answer_contract_gap"
    if "no_source_contract" in failed:
        return "policy_contract_gap"
    if "workspace_scope" in failed:
        return "metadata_filter_gap"
    if "model_match" in failed:
        return "metadata_gap"
    if any(name in failed for name in ("doc_type_match", "doc_id_match", "doc_code_match", "block_type_match")):
        return "retrieval_miss"
    if "expected_terms" in failed:
        return "context_insufficient"
    if "citation_contract" in failed:
        return "citation_schema_gap"
    return "eval_gap"


def write_eval_root_cause(settings: Settings, summary: dict[str, Any], *, suite: str, run_dir: Path) -> str:
    counts: dict[str, int] = {}
    tag_counts: Counter[str] = Counter()
    failures = []
    for result in summary.get("results", []):
        root_cause = result.get("root_cause")
        if not root_cause:
            continue
        counts[root_cause] = counts.get(root_cause, 0) + 1
        for tag in result.get("tags") or []:
            tag_counts[str(tag)] += 1
        failures.append(result)
    payload = {
        "schema_version": "baxter_eval_root_cause_rollup_v2",
        "generated_at": utc_now(),
        "suite": suite,
        "run_id": summary.get("run_id"),
        "cases_path": summary.get("cases_path"),
        "case_count": summary.get("case_count"),
        "passed": summary.get("passed"),
        "failed": summary.get("failed"),
        "counts": counts,
        "tag_counts": dict(tag_counts),
        "failures": failures,
    }
    latest_path = settings.artifacts_dir / "root_cause" / f"{suite}_eval_latest_root_cause.json"
    write_json(latest_path, payload)
    write_json(run_dir / "root_cause.json", payload)
    return str(latest_path)


def criteria_counts(results: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for result in results:
        for item in result.get("criteria") or []:
            name = str(item.get("name") or "unknown")
            bucket = counts.setdefault(name, {"passed": 0, "failed": 0})
            bucket["passed" if item.get("passed") else "failed"] += 1
    return counts


def root_cause_counts(results: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for result in results:
        if result.get("root_cause"):
            counts[str(result["root_cause"])] += 1
    return dict(counts)


def build_eval_run_id(suite: str) -> str:
    stamp = utc_now().replace(":", "").replace("-", "").replace("+0000", "Z")
    return f"{suite}_{stamp}_{short_hash(suite + stamp, 6)}"


def clean_str(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def append_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def case_block_bucket(case: EvalCase) -> str:
    if case.expected_block_type:
        return case.expected_block_type
    if case.expected_behavior == "answer_with_citation":
        return "technical_general"
    return "policy"


def weak_term(token: str) -> bool:
    if token in GENERIC_AUDIT_TERMS:
        return True
    if any(model in token for model in ("prismax", "prismaflex", "thermax")):
        return True
    if len(token) < 5 or token.isnumeric() or token in STOPWORDS:
        return True
    if re.fullmatch(r"\d{4}(?:-\d{2}){0,2}", token):
        return True
    digits = sum(1 for char in token if char.isdigit())
    if digits and digits / max(1, len(token)) > 0.35 and not token.startswith("sw"):
        return True
    return False
