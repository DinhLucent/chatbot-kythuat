# Brain-Human-First Project Criteria

Generated: 2026-06-21

Brain-human-first means the system and its agents reason like careful technical operators before behaving like a fast software pipeline.

```text
observe the user question or data failure
-> recall project decisions and source boundaries
-> inspect existing artifacts before proposing new layers
-> infer model/manual/safety scope
-> retrieve and validate source evidence
-> detect missing, ambiguous, or conflicting evidence
-> answer with citations, ask back, or abstain
-> turn failures into eval cases, reports, queues, or decision records
```

## Criterion 1: Imagine From Evidence

- Start with a real user need, failed question, weak artifact, or document limitation.
- Form a hypothesis before adding code or tool stages.
- Prefer better metadata, normalization, citations, retrieval, eval, and source preview.
- Prototype risky ideas behind explicit flags or branches.
- Keep the one workflow and source-of-truth boundaries intact.

## Criterion 2: Logic First

The reasoning chain is:

```text
question type
-> model/manual/scope inference
-> source-backed filters or clarification
-> hybrid retrieval
-> parent/source context
-> cited answer, visible extractive fallback, clarification, or no-source
-> logs, feedback, and eval
```

- Keep decisions visible in plans, reports, ledgers, traces, or result payloads.
- Do not hide ranking, filtering, tool failure, or fallback decisions.
- For technical/safety content, evidence quality beats fluent wording.
- A human must be able to trace every final claim back to source text.

## Criterion 3: Long-Run Completeness

- Full-corpus or long-running review is required when the claim is about corpus/data quality.
- Sampling is allowed for triage, debugging, and estimating cost only.
- A sample result must state the exact subset and the remaining gap.
- If a slow step is required, run it, resume it, or create a tracked follow-up artifact.
- Never claim "clean", "complete", or "validated" from a brief inspection.

## Criterion 4: Self-Evolution

Allowed improvement loops:

- Failed questions become eval cases.
- Weak retrieval traces become metadata/chunking fixes.
- OCR or conversion gaps become queue items.
- Quality-loop findings become ledgers, patch proposals, or review tasks.
- Human-approved decisions update roadmap or workflow docs.

Not allowed:

- Silent prompt drift.
- Hidden provider/model fallback.
- Learning technical facts from untrusted user text.
- Direct LLM mutation of normalized manuals without proposal/review artifacts.

## Criterion 5: Remove Bad Patterns

Remove or reject:

- Hidden answer shortcuts.
- Demo code that bypasses citations.
- Stale docs that describe an old workflow.
- Duplicate flags that imply unsupported modes.
- Generated data checked into source control.
- Per-case hardcoding when metadata or retrieval should carry the logic.
- "Looked briefly" conclusions for complex data.

## Acceptance Checklist

- Supports the core RAG blueprint.
- Preserves raw source and generated artifact boundaries.
- Improves evidence, metadata, retrieval, citations, eval, or UX.
- Makes uncertainty, contradiction, no-source, and tool failure visible.
- Uses full/resumable review when making corpus-level claims.
- Avoids hidden fallback and framework replacement.
- Leaves a rollback path.

## Priority Order

1. Grounded correctness.
2. Source and citation trust.
3. Long-run data completeness.
4. Human-inspectable logic.
5. Uncertainty and contradiction handling.
6. Metadata and schema quality.
7. Evaluation coverage.
8. Simplicity and removal of bad patterns.
9. UI polish.
10. Experimental capability.
