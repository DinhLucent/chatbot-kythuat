# Core RAG Blueprint And Roadmap

Generated: 2026-06-21

This document is the project direction lock for the Baxter IC technical RAG chatbot. Every architecture, retrieval, data-pipeline, citation, evaluation, or AI-tool change must support this direction unless a later human-approved decision record supersedes it.

## North Star

Build a CPU-first technical RAG chatbot that answers Vietnamese questions about Baxter IC manuals from governed source documents with citations, auditable data artifacts, and measurable quality.

## Current Scope

- Current work is governance: ideology, constitution, agent rules, and decision boundaries.
- External systems are references only until a human explicitly starts an implementation or benchmark task.
- Avoid adding process layers that do not change agent behavior, evidence quality, or decision safety.
- Keep the first source-workspace UX narrow: PrisMax and Prismaflex workspaces, each with user-created threads. Do not add broad workspace settings until they change retrieval, citation, eval, or answer behavior.
- For LLM-assisted data/audit architecture, use `docs/llm-assisted-data-and-audit-blueprint.md`.

The core path is:

```text
documents/IC/BAXTER raw manuals
-> inventory + taxonomy + manifest
-> MinerU or native PDF conversion
-> normalized Markdown + metadata + sections + citations + chunks
-> technical_intelligence.json
-> local hybrid index, optional Qdrant push
-> Vietnamese cited chat
-> logs, feedback, eval, root-cause reports, and quality reports
```

## Non-Negotiable Decisions

- `documents/` is immutable raw source.
- Generated data belongs only under `artifacts/`, `staging/`, or `logs/`.
- Keep the repository-owned conversion, normalization, chunking, retrieval, citation, answer, and evaluation contracts.
- Treat source workspaces as deterministic retrieval scopes, not separate framework-owned RAG systems. Initial priority workspaces are PrisMax and Prismaflex.
- MinerU is the primary heavy parser when installed; native PDF conversion is an explicit MVP path, not a hidden quality claim.
- Local hybrid retrieval is the default baseline; Qdrant is optional until measured need justifies it.
- All answerable technical responses must be grounded in retrieved source evidence and citations.
- Missing source evidence must produce no-source or clarification, not invented service guidance.
- LLM, OCR, Hyper-Extract, and Apodex outputs are proposals or validators, not source of truth.
- Long-running data quality work is first-class. Sampling is triage only, not full validation.
- DeepSeek, local LLMs, Apodex, or any OpenAI-compatible model may support hard reasoning only through schema-bound, logged, reviewable calls.
- Data, retrieval, citation, answer, and model failures must be classified by root cause during audit/QA instead of being treated as a generic "bad answer."

## Forbidden Direction Changes

- Do not replace the core with a monolithic RAG/chatbot framework that owns ranking, citation, answer, or eval policy.
- Do not add hidden fallback answer paths to make demos pass.
- Do not claim corpus quality from a quick sample when full or resumable review is required.
- Do not invent alarms, warnings, part numbers, units, service steps, clinical guidance, or software behavior.
- Do not upload full manuals or snippets externally unless explicit allow flags and project policy permit it.
- Do not let optional tools mutate normalized source directly without proposal, ledger, and review artifacts.

## Reference Boundaries

| Reference | Allowed Use | Not Allowed |
| --- | --- | --- |
| Pathway `llm-app` | Study live data connectors, document indexing, private RAG, adaptive retrieval, file/folder/source filtering, and optional retriever-sidecar patterns. | Do not let Pathway own citation maps, answer/fallback policy, or the canonical data-quality ledger. |
| RAGFlow | Study deep document understanding, chunk visualization, citation UX, and parser/chunking options. | Do not replace the local pipeline or move ranking/answer policy into RAGFlow. |
| `demo-chatbot-engineerworkflow` | Reuse the operating ideology: one measured workflow, decision logs, brain-human-first reasoning, no hidden fallback. | Do not copy SOP/procedure assumptions directly into Baxter technical-manual logic. |
| Kotaemon | Borrow source preview, citation inspection, and RAG UI ideas. | Do not adopt it as the backend core without eval proof. |
| AnythingLLM / Dify | Use for demos, UX/workflow vocabulary, source-workspace/thread separation, or integration inspiration. | Do not make no-code/agent workflow own production policy. |
| GraphRAG / m_flow | Benchmark hard multi-hop technical relationships after baseline failures are measured. | Do not enable by default before it beats the current workflow on eval and cost. |

## Core Data Model

Keep improving metadata around technical meaning:

- `doc_id`, `doc_code`, `source_file`, `model`, `doc_type`, `software_version`
- `source_workspace_id`, `allowed_doc_ids`, cross-workspace mode, selected scope reason
- `section_path`, `parent_section_id`, `block_type`, `chunk_id`
- `citation_id`, `line_start`, `line_end`, source preview fields
- technical entities: alarms, warnings, maintenance steps, calibration steps, parts, tools, units, procedures
- quality fields: converter, OCR need, low-value marker, review queue, eval tags
- audit fields: root-cause class, proposal risk, promotion status, rollback note, human-review requirement

## Roadmap

### Phase 0: Baseline Integrity

- Preserve raw source immutability and generated artifact boundaries.
- Keep the quick MVP usable with native PDF extraction and local hybrid search.
- Maintain clear reports for inventory, conversion, normalization, indexing, and eval.

### Phase 1: Data Quality And Citations

- Strengthen normalization, sectioning, citation maps, and chunk plans across the full corpus.
- Use long-running quality loops when document cleanup requires full review.
- Treat OCR gaps, weak citations, and noisy chunks as tracked queue items, not hand-waved defects.
- Route difficult cleanup to deterministic tools, local LLMs, API LLMs, or human review based on risk and evidence.

### Phase 2: Retrieval Quality

- Add first-class source workspaces so PrisMax and Prismaflex chats retrieve from their selected machine scope by default.
- Improve metadata filters, query expansion, parent-context expansion, and block-type ranking.
- Measure failures by question type: alarm, service, maintenance, calibration, safety, specs, compare, exact code.
- Use Qdrant only when local index metrics show the need.

### Phase 3: Answer And Evaluation

- Keep Vietnamese answers citation-grounded and conservative for service/safety content.
- Make LLM fallback visible through `answer_source` and `llm_error`; never hide provider failure.
- Expand eval cases from real failures and user feedback.
- Log answer failures by data quality, retrieval miss, context insufficiency, citation error, LLM drift, policy block, or eval gap.

### Phase 4: Controlled Optional Tools

- Enable MinerU, PaddleOCR, Hyper-Extract, and Apodex only when their cost and risk match a measured bottleneck.
- Keep all AI-generated repairs as proposals unless guardrails approve exact low-risk cleanup.
- Add DeepSeek/API LLM review only behind explicit external-upload policy, minimized snippets, structured schemas, and saved audit artifacts.

## Change Gate

Before adding a framework, model, tool, data layer, shortcut, or answer-stage abstraction, answer:

1. Does it strengthen source quality, retrieval, citations, eval, or user trust?
2. Does it preserve repository ownership of ranking, citation, answer, and eval policy?
3. Does it avoid hidden fallback behavior?
4. Does it improve a measured failure mode?
5. Does it leave inspectable artifacts for long-running data work?
6. Does it have a rollback path that preserves raw manuals and run history?

If any answer is no, do not implement it.
