# Core RAG Workflow Decision Log

Generated: 2026-06-21

## Current Disposition

APPROVED. The project keeps one repository-owned Baxter IC technical RAG workflow with immutable raw manuals, generated artifacts, local hybrid retrieval, optional Qdrant, Vietnamese cited chat, and guarded long-running data quality loops.

2026-06-21 update: APPROVED. Pathway `llm-app`, RAGFlow, Kotaemon, AnythingLLM, Dify, and GraphRAG/m_flow are not the production core. The current Baxter repository-owned pipeline remains the core. External systems may be used only as references, sidecars, or benchmarks after they preserve citation, fallback, eval, raw-source, and long-run data-quality contracts. See `docs/adr/0001-rag-core-framework-selection.md`.

2026-06-21 update: APPROVED. LLM-assisted data review, answer QA, and audit
root-cause analysis are part of the target architecture, but only as
schema-bound, logged proposals or validators. DeepSeek/API LLMs may be used for
hard reasoning only when external upload is explicitly allowed and snippets are
minimized. See `docs/llm-assisted-data-and-audit-blueprint.md`.

2026-06-21 update: APPROVED. Implement DeepSeek/OpenAI-compatible structured
call points across query planning, cited answer composition, optional answer QA,
normalization enrichment metadata, and semantic data review. These calls must
write audit artifacts, respect explicit external-upload gates for source
snippets, and remain proposals or validators rather than source truth.

2026-06-21 update: APPROVED. AI Agent CLI tools such as Codex may participate
as bounded QA, investigation, repair, eval-expansion, and documentation workers
when a task requires tool use across multiple artifacts or code paths. They are
not hot-path answer engines and their outputs are not source truth without
evidence, promotion gates, verification, and review.

2026-06-21 update: APPROVED. Add source workspaces as the target UX/retrieval
scope model, starting with separate PrisMax and Prismaflex workspaces. This
borrows AnythingLLM's workspace/thread separation and Pathway `llm-app`'s
source filtering ideas while preserving the repository-owned Baxter pipeline.
See `docs/adr/0002-source-workspaces.md`.

## Decisions

| Decision | Alternatives | Resolution |
| --- | --- | --- |
| Raw manuals remain immutable | Edit or clean files under `documents/` | Generated data goes to `artifacts/`, `staging/`, and `logs/`. |
| Keep repo-owned RAG contracts | Adopt a monolithic RAG/chatbot framework | Frameworks may be studied, but ranking, citation, answer, and eval policy stay local. |
| Use native PDF as MVP path | Require MinerU before any run | Native extraction keeps the MVP usable; MinerU remains primary heavy parser when installed. |
| Use local hybrid index first | Push every run to Qdrant | Local index is the baseline; Qdrant is optional for measured scale/retrieval needs. |
| Allow visible extractive fallback | Hide LLM failure or fail every no-key run | Extractive fallback is allowed only with `answer_source` and citations. |
| Treat optional AI tools as controlled stages | Let tools mutate canonical data directly | PaddleOCR, Hyper-Extract, Apodex, and semantic review produce plans, proposals, validators, queues, or reports. |
| Use AI Agent CLI as bounded workers | Treat agents as autonomous source-truth owners or hot-path answerers | Codex-like agents may inspect, patch, test, and report within explicit scope; evidence and promotion gates decide. |
| Require long-run completeness for data claims | Check a few files and declare clean | Sampling is triage only; corpus claims require full/resumable runs or stated remaining gaps. |
| Use schema-bound LLM decisions | Free-form LLM text drives routing or QA | LLM outputs that affect data, audit, answer, or evaluation must be structured, logged, and reviewable. |
| Add source workspaces | Let each question search the entire corpus or adopt AnythingLLM/Pathway as the core | PrisMax and Prismaflex become first-priority retrieval scopes. Sessions store selected scope; retrieval filters by scope; cross-scope search is explicit and logged. |
| Classify failure root cause | Treat every failure as generic model weakness | QA must separate data, conversion, normalization, metadata, retrieval, context, citation, LLM drift, policy, and eval gaps. |

## Understanding Lock

- The project answers technical manual questions, not general medical or service advice.
- Source evidence is Baxter IC manuals and generated artifacts derived from them.
- Technical correctness and citation trust outrank speed.
- No-source, ambiguity, contradiction, and tool failure must be visible.
- Long-running data cleanup is expected when documents are long, messy, or structurally complex.

## Open Risks

- Some existing answers may rely on visible extractive fallback when LLM is unavailable.
- Native PDF extraction can miss layout/table/OCR content; quality reports and queues must expose this.
- Current eval is seed-based and should grow from real failures.
- Optional external tools require strict allow flags and review artifacts.
- API LLM review can leak sensitive/copyrighted snippets if upload policy and minimization are ignored.
- Root-cause labels can be wrong; they are audit signals, not final truth without evidence.

## Decision Update Rule

Create or update this file before changing:

- core retrieval backend or ranking policy;
- answer/fallback/citation behavior;
- raw-source or generated-artifact boundaries;
- optional AI-tool authority;
- long-running data quality requirements;
- external upload policy.
- schema-bound LLM routing, answer QA, or root-cause audit authority.
- source workspace scope, cross-workspace search, or session-bound retrieval filters.
