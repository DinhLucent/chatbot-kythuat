from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from .llm import LLMUnavailable, OpenAICompatibleLLM, answer_system_prompt, build_answer_prompt, source_upload_allowed
from .llm_review import AnswerGroundingReview, answer_qa_enabled, llm_audit_dir
from .retrieval import SearchHit, search
from .settings import Settings
from .utils import clamp_text, normalized_text, read_json, utc_now, write_json
from .workspaces import get_source_workspace, workspace_model


class QueryPlan(BaseModel):
    model: Optional[str] = Field(None, description="Dòng máy thiết bị (ví dụ: PrisMax, Prismaflex, TherMax) hoặc null nếu không thể xác định.")
    doc_type: Optional[str] = Field(None, description="Loại tài liệu (ví dụ: service_manual, operator_manual, service_bulletin, addendum) hoặc null nếu không xác định.")
    software_version: Optional[str] = Field(None, description="Phiên bản phần mềm (ví dụ: SW 3.x, SW 4.x) hoặc null.")
    alarm_code: Optional[str] = Field(None, description="Mã cảnh báo lỗi kỹ thuật (ví dụ: 101, 81) hoặc null.")
    question_type: str = Field(description="Phân loại câu hỏi: troubleshooting, maintenance, calibration, safety, compare, spec_lookup, technical_lookup.")
    retrieval_query: str = Field(description="Câu truy vấn tối ưu nhất bằng tiếng Anh hoặc tiếng Việt để tìm kiếm thông tin kỹ thuật.")
    needs_clarification: bool = Field(description="True nếu câu hỏi là về vấn đề kỹ thuật nhưng không rõ model thiết bị và hệ thống cần làm rõ. Ngược lại là False.")
    clarification_message: Optional[str] = Field(None, description="Thông điệp hướng dẫn User chọn dòng máy nếu needs_clarification là True.")


class CitationReference(BaseModel):
    chunk_id: str = Field(description="ID của chunk được sử dụng để trả lời (ví dụ: doc_id_c00001).")
    quote: str = Field(description="Trích đoạn văn bản gốc trong chunk chứng minh trực tiếp cho phần câu trả lời.")


class ChatAnswer(BaseModel):
    answer: str = Field(description="Câu trả lời đầy đủ bằng tiếng Việt mạch lạc, dễ hiểu. Bắt buộc đính kèm các trích dẫn dạng [1], [2] tương ứng với các phần tử trong citations.")
    citations: List[CitationReference] = Field(description="Danh sách các nguồn trích dẫn hỗ trợ trực tiếp câu trả lời.")


@dataclass
class ChatSession:
    session_id: str
    created_at: str
    updated_at: str
    brand: str = "BAXTER"
    business_unit: str = "IC"
    source_workspace_id: str | None = None
    model: str | None = None
    doc_type: str | None = None
    software_version: str | None = None
    current_alarm: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def create_session(
    settings: Settings,
    *,
    model: str | None = None,
    doc_type: str | None = None,
    software_version: str | None = None,
    source_workspace_id: str | None = None,
) -> ChatSession:
    workspace = get_source_workspace(source_workspace_id)
    workspace_id = str(workspace["workspace_id"]) if workspace else None
    scoped_model = model or workspace_model(workspace_id)
    session = ChatSession(
        session_id=f"sess_{uuid4().hex[:16]}",
        created_at=utc_now(),
        updated_at=utc_now(),
        source_workspace_id=workspace_id,
        model=scoped_model or None,
        doc_type=doc_type or None,
        software_version=software_version or None,
    )
    sessions = load_sessions(settings)
    sessions[session.session_id] = session.to_dict()
    save_sessions(settings, sessions)
    return session


def load_session(settings: Settings, session_id: str | None) -> ChatSession:
    sessions = load_sessions(settings)
    if session_id and session_id in sessions:
        return ChatSession(**sessions[session_id])
    return create_session(settings)


def ask(
    settings: Settings,
    question: str,
    *,
    session_id: str | None = None,
    overrides: dict[str, Any] | None = None,
    use_llm: bool = True,
    allow_external_llm: bool | None = None,
    source_workspace_id: str | None = None,
) -> dict[str, Any]:
    session = load_session(settings, session_id)
    overrides = dict(overrides or {})
    apply_workspace_scope(session, overrides, source_workspace_id)
    llm = OpenAICompatibleLLM()
    plan = plan_query(settings, question, session, overrides, llm=llm if use_llm else None)
    if use_llm and llm.configured and "plan_source" not in plan:
        plan["plan_source"] = "llm"
    if plan.get("needs_clarification"):
        answer = plan["clarification_message"]
        payload = response_payload(
            session,
            question,
            answer,
            [],
            [],
            plan,
            answer_source="clarification",
            llm_usage={},
            no_answer_reason="missing_scope",
        )
        persist_turn(settings, session, question, payload)
        return payload

    hits = search(settings, plan["retrieval_query"], filters=plan["filters"], limit=settings.top_k)
    if not hits:
        answer = "Mình không thấy nguồn phù hợp trong tài liệu Baxter đã nạp. Anh/chị có thể bổ sung model, mã alarm, mã tài liệu hoặc phiên bản phần mềm."
        payload = response_payload(
            session,
            question,
            answer,
            [],
            [],
            plan,
            answer_source="no_source",
            llm_usage={},
            no_answer_reason="no_retrieval_hits",
        )
        if session.source_workspace_id:
            payload["answer_source"] = "no_source_in_scope"
            payload["no_answer_reason"] = "no_retrieval_hits_in_workspace"
        persist_turn(settings, session, question, payload)
        return payload

    context_blocks = context_blocks_from_hits(hits)
    llm_error = None
    answer_qa: dict[str, Any] | None = None
    citations = []
    llm_answer_allowed = use_llm and llm.configured and source_upload_allowed(llm, allow_external_llm)
    if use_llm and llm.configured and not source_upload_allowed(llm, allow_external_llm):
        llm_error = "external_llm_not_allowed"

    if llm_answer_allowed:
        try:
            chat_answer: ChatAnswer = llm.complete_structured(
                [
                    {"role": "system", "content": answer_system_prompt()},
                    {"role": "user", "content": build_answer_prompt(question, context_blocks)},
                ],
                ChatAnswer,
                temperature=0,
                max_tokens=1600,
                audit_dir=llm_audit_dir(settings, "chat_answer"),
                task_name="chat_cited_answer",
                source_ids=[hit.chunk_id for hit in hits],
                metadata={"session_id": session.session_id, "question_type": plan.get("question_type")},
            )
            answer = chat_answer.answer

            citations = []
            hit_by_chunk_id = {hit.chunk_id: hit for hit in hits}
            for ref in chat_answer.citations:
                hit = hit_by_chunk_id.get(ref.chunk_id)
                if hit and hit.citation:
                    cit = dict(hit.citation)
                    cit["quote"] = ref.quote
                    citations.append(cit)
            
            if not citations:
                citations = [hit.citation for hit in hits if hit.citation]

            answer_source = "llm"
            answer_qa = maybe_review_answer_grounding(
                settings,
                llm,
                question=question,
                answer=answer,
                context_blocks=context_blocks,
                citations=citations,
                allow_external_llm=allow_external_llm,
                session_id=session.session_id,
            )
            if answer_qa and should_replace_answer(answer_qa):
                llm_error = f"answer_qa_rejected: {answer_qa.get('decision')} / {answer_qa.get('root_cause')}"
                answer = extractive_answer(question, hits)
                citations = [hit.citation for hit in hits if hit.citation]
                answer_source = "extractive_fallback"
        except Exception as exc:
            llm_error = str(exc)
            answer = extractive_answer(question, hits)
            citations = [hit.citation for hit in hits if hit.citation]
            answer_source = "extractive_fallback"
    else:
        answer = extractive_answer(question, hits)
        citations = [hit.citation for hit in hits if hit.citation]
        answer_source = "extractive_no_llm"

    payload = response_payload(
        session,
        question,
        answer,
        hits,
        citations,
        plan,
        answer_source=answer_source,
        llm_usage=llm.usage.to_dict() if use_llm and llm.configured else {},
        llm_error=llm_error,
        answer_qa=answer_qa,
    )
    persist_turn(settings, session, question, payload)
    return payload


def apply_workspace_scope(
    session: ChatSession,
    overrides: dict[str, Any],
    source_workspace_id: str | None,
) -> None:
    workspace = get_source_workspace(source_workspace_id or overrides.get("source_workspace_id") or session.source_workspace_id)
    if not workspace:
        return
    workspace_id = str(workspace["workspace_id"])
    model = str(workspace.get("model") or "")
    session.source_workspace_id = workspace_id
    if model:
        session.model = model
        overrides["model"] = model
    overrides["source_workspace_id"] = workspace_id


def plan_query(
    settings: Settings,
    question: str,
    session: ChatSession,
    overrides: dict[str, Any],
    llm: OpenAICompatibleLLM | None = None,
) -> dict[str, Any]:
    if llm is not None and llm.configured:
        try:
            available_models = [item for item in available_values(settings, "model") if item != "Unknown"]
            available_doc_types = [item for item in available_values(settings, "doc_type") if item != "Unknown"]
            
            system_prompt = (
                "Bạn là trợ lý phân tích câu hỏi kỹ thuật Baxter. "
                "Hãy trích xuất các thông tin sau và trả về QueryPlan. "
                f"Các dòng máy hiện có: {', '.join(available_models)}. "
                f"Các loại tài liệu: {', '.join(available_doc_types)}."
            )
            user_prompt = f"Lịch sử chat:\n{json.dumps(session.history[-4:], ensure_ascii=False)}\n\nCâu hỏi: {question}"
            
            plan_res: QueryPlan = llm.complete_structured(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                QueryPlan,
                temperature=0.0,
                max_tokens=900,
                audit_dir=llm_audit_dir(settings, "query_planning"),
                task_name="chat_query_plan",
                source_ids=[session.session_id],
                metadata={"available_models": available_models, "available_doc_types": available_doc_types},
            )
            
            filters: dict[str, Any] = {}
            model = overrides.get("model") or plan_res.model or session.model
            doc_type = overrides.get("doc_type") or plan_res.doc_type or session.doc_type
            software_version = overrides.get("software_version") or plan_res.software_version or session.software_version
            alarm = plan_res.alarm_code
            
            if model and model in available_models:
                filters["model"] = model
                session.model = model
            if doc_type and doc_type in available_doc_types:
                filters["doc_type"] = doc_type
                session.doc_type = doc_type
            if software_version:
                filters["software_version"] = software_version
                session.software_version = software_version
            if alarm:
                session.current_alarm = alarm
                
            if plan_res.needs_clarification and not model and len(available_models) > 1:
                return {
                    "question_type": plan_res.question_type,
                    "filters": filters,
                    "retrieval_query": question,
                    "needs_clarification": True,
                    "clarification_message": plan_res.clarification_message or "Anh/chị đang hỏi cho model nào? Chọn Prismaflex hoặc PrisMax để mình tra đúng manual/service bulletin.",
                    "options": [{"label": item, "value": item} for item in available_models],
                }
                
            return {
                "question_type": plan_res.question_type,
                "filters": filters,
                "retrieval_query": plan_res.retrieval_query,
                "needs_clarification": False,
                "detected": {"model": model, "doc_type": doc_type, "software_version": software_version, "alarm": alarm},
            }
        except Exception as exc:
            plan = plan_query_regex(settings, question, session, overrides)
            plan["plan_source"] = "regex_fallback"
            plan["llm_plan_error"] = str(exc)
            return plan

    plan = plan_query_regex(settings, question, session, overrides)
    plan["plan_source"] = "regex_no_llm"
    return plan


def plan_query_regex(settings: Settings, question: str, session: ChatSession, overrides: dict[str, Any]) -> dict[str, Any]:
    q = normalized_text(question)
    filters: dict[str, Any] = {}
    question_type = infer_question_type(q)
    model = overrides.get("model") or detect_model(question) or session.model
    auto_doc_type = None if question_type == "compare" else detect_doc_type(question)
    session_doc_type = None if question_type == "compare" else session.doc_type
    doc_type = overrides.get("doc_type") or auto_doc_type or session_doc_type
    software_version = overrides.get("software_version") or detect_software_version(question) or session.software_version
    alarm = detect_alarm(question)
    if model:
        filters["model"] = model
        session.model = model
    if doc_type:
        filters["doc_type"] = doc_type
        session.doc_type = doc_type
    if software_version:
        filters["software_version"] = software_version
        session.software_version = software_version
    if alarm:
        session.current_alarm = alarm

    available_models = [item for item in available_values(settings, "model") if item != "Unknown"]
    generic_technical = any(term in q for term in ("loi", "alarm", "bao tri", "pm", "checklist", "service", "thay", "calibration", "hieu chuan"))
    if generic_technical and not model and len(available_models) > 1:
        return {
            "question_type": infer_question_type(q),
            "filters": filters,
            "retrieval_query": question,
            "needs_clarification": True,
            "clarification_message": "Anh/chị đang hỏi cho model nào? Chọn Prismaflex hoặc PrisMax để mình tra đúng manual/service bulletin.",
            "options": [{"label": item, "value": item} for item in available_models],
        }

    expanded = expand_query_terms(question)
    return {
        "question_type": question_type,
        "filters": filters,
        "retrieval_query": expanded,
        "needs_clarification": False,
        "detected": {"model": model, "doc_type": doc_type, "software_version": software_version, "alarm": alarm},
    }


def detect_model(question: str) -> str | None:
    q = normalized_text(question)
    if "prismax" in q:
        return "PrisMax"
    if "prismaflex" in q:
        return "Prismaflex"
    if "thermax" in q:
        return "TherMax"
    return None


def detect_doc_type(question: str) -> str | None:
    q = normalized_text(question)
    if "service bulletin" in q or "tsb" in q:
        return "service_bulletin"
    if "service manual" in q:
        return "service_manual"
    if "operator" in q or "huong dan van hanh" in q:
        return "operator_manual"
    if "addendum" in q or "phu luc" in q:
        return "addendum"
    return None


def detect_software_version(question: str) -> str | None:
    match = re.search(r"\b(?:sw\s*)?(\d+(?:\.\d+|\.x|\.xx))\b", question, flags=re.IGNORECASE)
    return match.group(1).upper() if match else None


def detect_alarm(question: str) -> str | None:
    match = re.search(r"\b(?:alarm|loi|error|ma loi)\s*[:#-]?\s*([A-Z]?\d{2,5}[A-Z]?)\b", question, flags=re.IGNORECASE)
    return match.group(1).upper() if match else None


def infer_question_type(q: str) -> str:
    if any(term in q for term in ("canh bao", "warning", "caution", "safety", "an toan")):
        return "safety"
    if any(term in q for term in ("alarm", "loi", "error", "khac phuc")):
        return "troubleshooting"
    if any(term in q for term in ("bao tri", "maintenance", "pm")):
        return "maintenance"
    if any(term in q for term in ("hieu chuan", "calibration")):
        return "calibration"
    if any(term in q for term in ("so sanh", "khac nhau", "compare")):
        return "compare"
    if any(term in q for term in ("thong so", "spec", "parameter")):
        return "spec_lookup"
    return "technical_lookup"


def expand_query_terms(question: str) -> str:
    q = question
    expansions = {
        "lỗi": "alarm error troubleshooting corrective action",
        "loi": "alarm error troubleshooting corrective action",
        "xử lý": "troubleshooting corrective action service actions",
        "xu ly": "troubleshooting corrective action service actions",
        "access pressure": "access pressure excessive access pressure pressure alarms return pressure",
        "áp lực": "pressure access pressure return pressure",
        "ap luc": "pressure access pressure return pressure",
        "bơm": "pump",
        "bom": "pump",
        "thay": "replace remove install service",
        "bảo trì": "maintenance PM checklist preventive",
        "bao tri": "maintenance PM checklist preventive",
        "hiệu chuẩn": "calibration calibrate",
        "hieu chuan": "calibration calibrate",
        "thông số": "specification specifications technical data operating conditions safety specifications",
        "thong so": "specification specifications technical data operating conditions safety specifications",
        "tài liệu nào": "document manual bulletin software version sw revision",
        "tai lieu nao": "document manual bulletin software version sw revision",
    }
    q_norm = normalized_text(q)
    extra = [value for key, value in expansions.items() if normalized_text(key) in q_norm]
    if "service bulletin" not in q_norm and "tsb" not in q_norm:
        service_terms = ("service", "bao tri", "maintenance", "repair", "thay", "replace")
        safety_terms = ("canh bao", "warning", "caution", "safety", "an toan")
        if any(term in q_norm for term in service_terms):
            extra.append("service manual maintenance repair service mode authorized service technician")
        if any(term in q_norm for term in safety_terms):
            extra.append(
                "service manual warning caution safety hazard before performing procedure "
                "patient not connected authorized service technician electrical safety testing"
            )
    return " ".join([q, *extra]).strip()


def context_blocks_from_hits(hits: list[SearchHit]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for index, hit in enumerate(hits, start=1):
        text = hit.expanded_context or hit.content
        blocks.append(
            {
                "label": f"[{index}]",
                "chunk_id": hit.chunk_id,
                "source": hit.citation.get("display") or hit.citation.get("source_file"),
                "metadata": hit.metadata,
                "text": clamp_text(text, 2200),
            }
        )
    return blocks


def extractive_answer(question: str, hits: list[SearchHit]) -> str:
    lines = [
        "Mình tìm thấy các nguồn liên quan trong kho Baxter đã nạp. Chưa có LLM API hoặc LLM lỗi, nên mình chỉ tóm tắt theo đoạn nguồn, không suy diễn thêm.",
        "",
    ]
    if any(str(hit.metadata.get("block_type")) == "warning_safety" for hit in hits[:3]):
        lines.append("Lưu ý an toàn: với thao tác service/clinical risk, cần đối chiếu manual chính thức và quy trình an toàn của Baxter trước khi thực hiện.")
        lines.append("")
    for index, hit in enumerate(hits[:4], start=1):
        source = hit.citation.get("display") or hit.citation.get("source_file") or hit.doc_id
        lines.append(f"[{index}] {source}")
        lines.append(clamp_text(hit.content, 700))
        lines.append("")
    return "\n".join(lines).strip()


def maybe_review_answer_grounding(
    settings: Settings,
    llm: OpenAICompatibleLLM,
    *,
    question: str,
    answer: str,
    context_blocks: list[dict[str, Any]],
    citations: list[dict[str, Any]],
    allow_external_llm: bool | None,
    session_id: str,
) -> dict[str, Any] | None:
    if not answer_qa_enabled():
        return None
    if not source_upload_allowed(llm, allow_external_llm):
        return {
            "status": "skipped",
            "reason": "external_llm_not_allowed",
            "safe_response_action": "allow",
        }
    payload = {
        "question": question,
        "answer": answer,
        "citations": citations,
        "context": context_blocks,
        "review_rules": [
            "Use only context blocks and citations supplied in this payload.",
            "List every unsupported answer claim.",
            "Flag citation errors when a citation does not directly support the claim.",
            "Choose extractive_fallback when support is weak for service, safety, alarm, maintenance, or calibration guidance.",
        ],
    }
    try:
        review: AnswerGroundingReview = llm.complete_structured(
            [
                {
                    "role": "system",
                    "content": (
                        "You are a citation-support validator for Baxter technical-manual answers. "
                        "Do not answer the user. Judge whether the supplied answer is grounded in the supplied sources."
                    ),
                },
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            AnswerGroundingReview,
            temperature=0,
            max_tokens=1400,
            audit_dir=llm_audit_dir(settings, "answer_grounding_review"),
            task_name="answer_grounding_review",
            source_ids=[str(block.get("chunk_id") or "") for block in context_blocks],
            metadata={"session_id": session_id},
        )
        data = review.model_dump()
        data["status"] = "completed"
        return data
    except Exception as exc:
        return {"status": "failed", "error": str(exc)[:1000], "safe_response_action": "allow"}


def should_replace_answer(answer_qa: dict[str, Any]) -> bool:
    if answer_qa.get("status") != "completed":
        return False
    action = str(answer_qa.get("safe_response_action") or "")
    decision = str(answer_qa.get("decision") or "")
    if action in {"extractive_fallback", "no_source", "block"}:
        return True
    if decision in {"unsupported", "policy_block"}:
        return True
    return bool(answer_qa.get("unsupported_claims") or answer_qa.get("citation_errors"))


def response_payload(
    session: ChatSession,
    question: str,
    answer: str,
    hits: list[SearchHit],
    citations: list[dict[str, Any]],
    plan: dict[str, Any],
    *,
    answer_source: str,
    llm_usage: dict[str, Any],
    llm_error: str | None = None,
    no_answer_reason: str | None = None,
    answer_qa: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": "success",
        "session_id": session.session_id,
        "answer": answer,
        "answer_source": answer_source,
        "no_answer_reason": no_answer_reason,
        "llm_error": llm_error,
            "llm_usage": llm_usage,
            "answer_qa": answer_qa,
            "citations": citations,
            "hits": [hit.to_dict() for hit in hits],
            "query_plan": plan,
            "source_workspace_id": session.source_workspace_id,
            "created_at": utc_now(),
        }


def persist_turn(settings: Settings, session: ChatSession, question: str, payload: dict[str, Any]) -> None:
    sessions = load_sessions(settings)
    session.updated_at = utc_now()
    session.history.append(
        {
            "question": question,
            "answer_source": payload.get("answer_source"),
            "source_workspace_id": payload.get("source_workspace_id"),
            "citation_count": len(payload.get("citations") or []),
            "created_at": payload.get("created_at"),
        }
    )
    session.history = session.history[-40:]
    sessions[session.session_id] = session.to_dict()
    save_sessions(settings, sessions)
    log_dir = settings.logs_dir / "chat"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{session.session_id}.jsonl"
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"question": question, "response": payload}, ensure_ascii=False) + "\n")


def load_sessions(settings: Settings) -> dict[str, Any]:
    return read_json(settings.artifacts_dir / "runtime" / "sessions.json", {}) or {}


def save_sessions(settings: Settings, sessions: dict[str, Any]) -> None:
    write_json(settings.artifacts_dir / "runtime" / "sessions.json", sessions)


def available_values(settings: Settings, key: str) -> list[str]:
    path = settings.artifacts_dir / "index" / "index_meta.json"
    meta = read_json(path, {})
    if key == "model":
        return sorted(item for item in (meta.get("model_counts") or {}).keys() if item)
    chunks_path = settings.artifacts_dir / "index" / "chunks.jsonl"
    values: set[str] = set()
    if chunks_path.exists():
        with chunks_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                value = payload.get(key)
                if value:
                    values.add(str(value))
    return sorted(values)


def source_preview(settings: Settings, citation_id: str) -> dict[str, Any]:
    for citation_path in sorted((settings.artifacts_dir / "normalized").glob("*/citation_map.json")):
        payload = read_json(citation_path, {})
        for citation in payload.get("citations", []):
            if citation.get("citation_id") == citation_id:
                normalized_path = citation_path.parent / "normalized.md"
                lines = normalized_path.read_text(encoding="utf-8", errors="replace").splitlines() if normalized_path.exists() else []
                start = max(1, int(citation.get("line_start") or 1) - 6)
                end = min(len(lines), int(citation.get("line_end") or start) + 6)
                return {
                    "citation": citation,
                    "preview": "\n".join(lines[start - 1 : end]),
                    "normalized_path": str(normalized_path),
                    "line_start": start,
                    "line_end": end,
                }
    return {"error": "citation_not_found", "citation_id": citation_id}
