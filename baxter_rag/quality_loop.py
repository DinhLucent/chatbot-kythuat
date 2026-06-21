from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .quality_audit import QualityAuditOptions, run_quality_audit
from .semantic_review import SemanticReviewOptions, run_apodex_semantic_review, select_semantic_review_candidates
from .settings import Settings
from .utils import iter_jsonl, read_json, short_hash, utc_now, write_json, write_jsonl


@dataclass(frozen=True)
class QualityLoopOptions:
    doc_id: str | None = None
    max_iterations: int = 3
    review_limit: int = 12
    enable_llm_review: bool | None = None
    enable_apodex: bool | None = None
    allow_external: bool | None = None
    apply_mode: str = "proposal_only"
    stop_when_no_candidates: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_quality_loop(settings: Settings, options: QualityLoopOptions | None = None) -> dict[str, Any]:
    options = options or QualityLoopOptions()
    settings.ensure_dirs()
    run_id = build_loop_id()
    loop_dir = settings.artifacts_dir / "quality_loop" / "runs" / run_id
    loop_dir.mkdir(parents=True, exist_ok=True)

    iterations: list[dict[str, Any]] = []
    ledger_rows: list[dict[str, Any]] = []
    seen_action_fingerprints: set[str] = set()
    baseline = collect_loop_snapshot(settings, doc_id=options.doc_id)

    for iteration in range(1, max(1, options.max_iterations) + 1):
        before = collect_loop_snapshot(settings, doc_id=options.doc_id)
        candidates = select_semantic_review_candidates(settings, doc_id=options.doc_id, limit=options.review_limit)
        audit = run_quality_audit(
            settings,
            QualityAuditOptions(
                doc_id=options.doc_id,
                include_low_risk=True,
                max_chunk_issues_per_doc=max(40, options.review_limit * 3),
            ),
        )
        audit_issue_rows = list(iter_jsonl(Path(str(audit.get("issues_path") or ""))))
        if not candidates and not audit_issue_rows and options.stop_when_no_candidates:
            iterations.append(
                {
                    "iteration": iteration,
                    "status": "stopped",
                    "reason": "no_candidates",
                    "before": before,
                    "candidate_count": 0,
                    "audit_issue_count": 0,
                }
            )
            break

        review = run_apodex_semantic_review(
            settings,
            SemanticReviewOptions(
                doc_id=options.doc_id,
                limit=options.review_limit,
                enable_llm_review=options.enable_llm_review,
                enable_apodex=options.enable_apodex,
                allow_external=options.allow_external,
            ),
        )
        proposal_rows = list(iter_jsonl(Path(str(review.get("proposal_path") or ""))))
        audit_actions = build_audit_action_plan(audit_issue_rows, iteration=iteration)
        semantic_actions = build_action_plan(proposal_rows, iteration=iteration, apply_mode=options.apply_mode)
        actions = audit_actions + semantic_actions
        new_actions: list[dict[str, Any]] = []
        for action in actions:
            fingerprint = action_fingerprint(action)
            action["fingerprint"] = fingerprint
            if fingerprint in seen_action_fingerprints:
                action["status"] = "duplicate_in_current_loop"
                continue
            seen_action_fingerprints.add(fingerprint)
            new_actions.append(action)
        ledger_rows.extend(new_actions)

        iteration_dir = loop_dir / f"iteration_{iteration:03d}"
        iteration_dir.mkdir(parents=True, exist_ok=True)
        write_json(iteration_dir / "audit_report.json", audit)
        write_json(iteration_dir / "review_report.json", review)
        write_jsonl(iteration_dir / "action_plan.jsonl", new_actions)
        write_json(iteration_dir / "queues.json", build_queues(new_actions))

        after = collect_loop_snapshot(settings, doc_id=options.doc_id)
        status = "completed"
        reason = "iteration_completed"
        if not new_actions:
            status = "stopped"
            reason = "no_new_actions_after_dedupe"
        elif no_new_action(new_actions):
            status = "stopped"
            reason = "no_new_actionable_proposals"
        iterations.append(
            {
                "iteration": iteration,
                "status": status,
                "reason": reason,
                "before": before,
                "after": after,
                "review_run_id": review.get("run_id"),
                "review_status": review.get("status"),
                "review_summary": review.get("summary", {}),
                "audit_run_id": audit.get("run_id"),
                "audit_scorecard": audit.get("scorecard", {}),
                "audit_issue_count": len(audit_issue_rows),
                "semantic_candidate_count": review.get("candidate_count"),
                "candidate_count": len(audit_issue_rows) + int(review.get("candidate_count") or 0),
                "action_counts": dict(Counter(action["action_type"] for action in new_actions)),
                "queue_counts": queue_counts(new_actions),
                "paths": {
                    "audit_report": str(iteration_dir / "audit_report.json"),
                    "review_report": str(iteration_dir / "review_report.json"),
                    "action_plan": str(iteration_dir / "action_plan.jsonl"),
                    "queues": str(iteration_dir / "queues.json"),
                },
            }
        )
        if status == "stopped":
            break

    write_jsonl(loop_dir / "ledger.jsonl", ledger_rows)
    write_json(loop_dir / "queues.json", build_queues(ledger_rows))
    final_snapshot = collect_loop_snapshot(settings, doc_id=options.doc_id)
    report = {
        "schema_version": "baxter_quality_loop_v1",
        "generated_at": utc_now(),
        "run_id": run_id,
        "status": "completed",
        "root": str(settings.root),
        "loop_dir": str(loop_dir),
        "options": options.to_dict(),
        "baseline": baseline,
        "final_snapshot": final_snapshot,
        "iterations": iterations,
        "ledger_path": str(loop_dir / "ledger.jsonl"),
        "queues_path": str(loop_dir / "queues.json"),
        "report_markdown": str(loop_dir / "report.md"),
        "operating_principle": (
            "The loop may run for hours. It stages deterministic actions and review proposals. "
            "Canonical normalized data is not changed unless a promotion gate accepts an evidence-backed action."
        ),
    }
    write_json(loop_dir / "report.json", report)
    write_json(settings.artifacts_dir / "quality_loop" / "latest_report.json", report)
    write_markdown_report(loop_dir / "report.md", report)
    return report


def build_action_plan(proposals: list[dict[str, Any]], *, iteration: int, apply_mode: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in proposals:
        proposal = item.get("proposal") if isinstance(item.get("proposal"), dict) else {}
        verdict = str(proposal.get("verdict") or "unknown")
        guardrail_flags = proposal.get("guardrail_flags") if isinstance(proposal.get("guardrail_flags"), list) else []
        requires_human_review = bool(proposal.get("requires_human_review", True))
        action_type = action_type_for_verdict(verdict, proposal)
        can_auto_apply = can_auto_apply_action(proposal, action_type=action_type, apply_mode=apply_mode)
        row = {
            "action_id": build_action_id(item, iteration),
            "iteration": iteration,
            "created_at": utc_now(),
            "candidate_id": item.get("candidate_id"),
            "doc_id": item.get("doc_id"),
            "chunk_id": item.get("chunk_id"),
            "model_ok": item.get("ok"),
            "dry_run": bool(item.get("dry_run")),
            "verdict": verdict,
            "action_type": action_type,
            "status": "auto_apply_ready" if can_auto_apply else "queued",
            "can_auto_apply": can_auto_apply,
            "requires_human_review": requires_human_review,
            "guardrail_status": proposal.get("guardrail_status", "unknown"),
            "guardrail_flags": guardrail_flags,
            "risk": highest_edit_risk(proposal),
            "confidence": proposal.get("confidence"),
            "reason": proposal.get("cannot_fix_reason") or proposal.get("rationale") or "",
            "proposal": proposal,
        }
        rows.append(row)
    return rows


def build_audit_action_plan(issues: list[dict[str, Any]], *, iteration: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for issue in issues:
        action_type = str(issue.get("action_type") or "model_review_needed")
        requires_human_review = bool(issue.get("requires_human_review", True))
        row = {
            "action_id": build_audit_action_id(issue, iteration),
            "iteration": iteration,
            "created_at": utc_now(),
            "candidate_id": issue.get("issue_id"),
            "doc_id": issue.get("doc_id"),
            "chunk_id": issue.get("chunk_id"),
            "page_start": issue.get("page_start"),
            "model_ok": None,
            "dry_run": False,
            "verdict": "audit_issue",
            "rule_id": issue.get("rule_id"),
            "action_type": action_type,
            "status": "queued",
            "can_auto_apply": False,
            "requires_human_review": requires_human_review,
            "guardrail_status": "audit_gate",
            "guardrail_flags": [] if not requires_human_review else ["promotion_gate_required"],
            "risk": issue.get("risk") or issue.get("severity") or "medium",
            "confidence": 1.0,
            "reason": issue.get("proposed_action") or "",
            "issue": issue,
        }
        rows.append(row)
    return rows


def action_type_for_verdict(verdict: str, proposal: dict[str, Any]) -> str:
    if verdict == "needs_ocr":
        return "ocr_retry"
    if verdict in {"propose_patch", "remove_noise", "split", "merge"}:
        if proposal.get("requires_human_review"):
            return "human_review_patch"
        return "safe_patch_candidate"
    if verdict == "keep":
        return "no_change"
    if verdict == "reject":
        return "reject_proposal"
    return "model_review_needed"


def can_auto_apply_action(proposal: dict[str, Any], *, action_type: str, apply_mode: str) -> bool:
    if apply_mode != "safe_auto_patch":
        return False
    if action_type != "safe_patch_candidate":
        return False
    if proposal.get("requires_human_review"):
        return False
    if proposal.get("guardrail_flags"):
        return False
    if highest_edit_risk(proposal) != "low":
        return False
    edits = proposal.get("edits") if isinstance(proposal.get("edits"), list) else []
    allowed = {"replace", "remove", "split"}
    return bool(edits) and all(isinstance(edit, dict) and edit.get("type") in allowed for edit in edits)


def highest_edit_risk(proposal: dict[str, Any]) -> str:
    order = {"low": 1, "medium": 2, "high": 3, "critical": 4}
    risks = []
    for edit in proposal.get("edits", []) if isinstance(proposal.get("edits"), list) else []:
        if isinstance(edit, dict):
            risks.append(str(edit.get("risk") or "medium"))
    if not risks:
        return "unknown"
    return max(risks, key=lambda item: order.get(item, 2))


def build_queues(actions: list[dict[str, Any]]) -> dict[str, Any]:
    queues: dict[str, list[dict[str, Any]]] = {
        "ocr_retry": [],
        "model_review_needed": [],
        "human_review_patch": [],
        "safe_patch_candidate": [],
        "auto_apply_ready": [],
        "no_change": [],
        "reject_proposal": [],
    }
    for action in actions:
        action_type = str(action.get("action_type") or "model_review_needed")
        queues.setdefault(action_type, []).append(slim_action(action))
        if action.get("can_auto_apply"):
            queues["auto_apply_ready"].append(slim_action(action))
    return {
        "generated_at": utc_now(),
        "counts": {key: len(value) for key, value in queues.items()},
        "queues": queues,
    }


def slim_action(action: dict[str, Any]) -> dict[str, Any]:
    return {
        "action_id": action.get("action_id"),
        "rule_id": action.get("rule_id"),
        "action_type": action.get("action_type"),
        "doc_id": action.get("doc_id"),
        "chunk_id": action.get("chunk_id"),
        "page_start": action.get("page_start"),
        "verdict": action.get("verdict"),
        "status": action.get("status"),
        "risk": action.get("risk"),
        "requires_human_review": action.get("requires_human_review"),
        "guardrail_flags": action.get("guardrail_flags"),
        "reason": str(action.get("reason") or "")[:500],
    }


def queue_counts(actions: list[dict[str, Any]]) -> dict[str, int]:
    queues = build_queues(actions)
    return dict(queues.get("counts", {}))


def no_new_action(actions: list[dict[str, Any]]) -> bool:
    return not any(action.get("action_type") not in {"no_change", "reject_proposal"} for action in actions)


def collect_loop_snapshot(settings: Settings, *, doc_id: str | None) -> dict[str, Any]:
    candidates = select_semantic_review_candidates(settings, doc_id=doc_id, limit=100000)
    quality_reports = []
    normalized_root = settings.artifacts_dir / "normalized"
    for report_path in normalized_root.glob("*/quality_report.json"):
        payload = read_json(report_path, {})
        if not isinstance(payload, dict) or not payload:
            continue
        if doc_id and payload.get("doc_id") != doc_id:
            continue
        quality_reports.append(payload)
    warnings = Counter()
    failed_gates = Counter()
    for report in quality_reports:
        for warning in report.get("warnings", []) if isinstance(report.get("warnings"), list) else []:
            if isinstance(warning, dict):
                warnings[str(warning.get("code") or "unknown")] += 1
        gates = report.get("quality_gates") if isinstance(report.get("quality_gates"), dict) else {}
        for key, value in gates.items():
            if value is False:
                failed_gates[key] += 1
    return {
        "generated_at": utc_now(),
        "doc_id": doc_id,
        "quality_report_count": len(quality_reports),
        "semantic_candidate_count": len(candidates),
        "semantic_candidate_reasons": dict(Counter(reason for item in candidates for reason in item.get("reasons", []))),
        "warnings": dict(warnings),
        "failed_gates": dict(failed_gates),
    }


def write_markdown_report(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# Data Quality Loop Report",
        "",
        f"- Run id: `{report.get('run_id')}`",
        f"- Status: `{report.get('status')}`",
        f"- Apply mode: `{report.get('options', {}).get('apply_mode')}`",
        f"- Baseline candidates: `{report.get('baseline', {}).get('semantic_candidate_count')}`",
        f"- Final candidates: `{report.get('final_snapshot', {}).get('semantic_candidate_count')}`",
        "",
        "## Iterations",
        "",
        "| Iteration | Status | Reason | Audit Issues | Semantic Candidates | Score | Queues |",
        "|---:|---|---|---:|---:|---:|---|",
    ]
    for iteration in report.get("iterations", []):
        queues = iteration.get("queue_counts", {})
        queue_note = ", ".join(f"{key}={value}" for key, value in queues.items() if value)
        scorecard = iteration.get("audit_scorecard", {}) if isinstance(iteration.get("audit_scorecard"), dict) else {}
        lines.append(
            f"| {iteration.get('iteration')} | `{iteration.get('status')}` | `{iteration.get('reason')}` | "
            f"{iteration.get('audit_issue_count', 0)} | {iteration.get('semantic_candidate_count', 0)} | "
            f"{scorecard.get('quality_score', '')} | {queue_note} |"
        )
    lines.extend(
        [
            "",
            "## Rules",
            "",
            "- Raw documents and normalized artifacts are not modified by this loop.",
            "- Every proposed edit is written to a ledger with model, evidence, risk, and guardrail status.",
            "- Service, alarm, safety, maintenance, and calibration edits require human review.",
            "- OCR gaps are routed to an OCR retry queue, not repaired by language-model guessing.",
        ]
    )
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def build_action_id(item: dict[str, Any], iteration: int) -> str:
    seed = f"{iteration}:{item.get('candidate_id')}:{item.get('chunk_id')}:{item.get('doc_id')}"
    return f"qa_{short_hash(seed, 14)}"


def build_audit_action_id(issue: dict[str, Any], iteration: int) -> str:
    seed = f"audit:{iteration}:{issue.get('issue_id')}:{issue.get('rule_id')}:{issue.get('doc_id')}:{issue.get('chunk_id')}"
    return f"qa_{short_hash(seed, 14)}"


def action_fingerprint(action: dict[str, Any]) -> str:
    seed = "|".join(
        str(action.get(key) or "")
        for key in ("verdict", "rule_id", "action_type", "doc_id", "chunk_id", "page_start", "reason")
    )
    return short_hash(seed, 20)


def build_loop_id() -> str:
    stamp = utc_now().replace(":", "").replace("-", "").replace("+0000", "Z")
    return f"loop_{stamp}_{short_hash(stamp, 6)}"
