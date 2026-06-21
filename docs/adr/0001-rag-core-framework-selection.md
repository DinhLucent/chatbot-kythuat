# ADR-0001: RAG Core Framework Selection

## Status

Accepted

## Date

2026-06-21

## Context

The project needs a durable core for a Baxter IC technical-manual chatbot. Candidate systems include Pathway `llm-app`, RAGFlow, the previous `demo-chatbot-engineerworkflow`, Kotaemon, AnythingLLM, Dify, and graph/m_flow-style RAG systems.

The current project already has a small repository-owned pipeline:

```text
raw manuals
-> manifest
-> conversion
-> normalized Markdown + metadata + citations + chunks
-> technical intelligence
-> local hybrid index / optional Qdrant
-> Vietnamese cited chat
-> eval, logs, feedback, quality-loop reports
```

The key risk is letting an attractive all-in-one platform own the exact things this project must keep auditable: source normalization, citation maps, data cleanup ledgers, retrieval policy, answer/fallback behavior, and eval quality.

## Decision Drivers

- Technical answers must be source-backed and conservative.
- Long-running data cleanup must be first-class, resumable, and reportable.
- Raw manuals must remain immutable.
- Citation maps and source previews must remain inspectable.
- The answer path must expose no-source, clarification, and fallback behavior.
- Optional tools must improve measured failures without taking over policy ownership.
- The project must stay usable on a CPU-first local Windows workflow.

## Considered Options

### Current Baxter Core + Demo Workflow Ideology

- Pros: Owns source, artifacts, retrieval policy, citations, fallback labels, eval, and quality loops.
- Pros: Easy to add domain-specific metadata for alarms, safety, service, maintenance, calibration, parts, and software versions.
- Cons: Requires more local engineering than adopting a platform.

### Pathway `llm-app`

- Pros: Strong live data synchronization, ready RAG templates, HTTP endpoints, built-in vector/hybrid search, private RAG option.
- Pros: Useful as a future live-indexing sidecar for SharePoint, Google Drive, S3, Kafka, PostgreSQL, or fast-changing folders.
- Cons: Its value proposition is to unify backend, retrieval, cache, and API layers; that conflicts with keeping local ownership of citation and answer policy.
- Cons: Windows use often points toward Docker, and templates are not tailored to Baxter citation/quality-ledger requirements.

### RAGFlow

- Pros: Strong deep document understanding, template chunking, grounded citations, human chunk visualization, heterogeneous documents.
- Pros: Good reference for parsing UX, chunk inspection, and citation workflows.
- Cons: Heavy platform surface and agent/workflow ownership can obscure local policy and artifact boundaries.

### `demo-chatbot-engineerworkflow`

- Pros: Best fit for project ideology: one measured answer workflow, brain-human-first criteria, decision logs, no hidden fallback.
- Cons: Domain-specific SOP/procedure assumptions must be translated, not copied directly.

### Kotaemon

- Pros: Clean RAG UI, source preview, citation UX, developer-extensible pipeline.
- Cons: Better as UX/pipeline reference than canonical Baxter backend.

### AnythingLLM / Dify

- Pros: Fast demos, multi-user/app UX, agents, no-code workflow, broad integrations.
- Cons: Too broad and product-oriented for core policy ownership; high risk of hidden routing, fallback, or agent behavior.

### GraphRAG / m_flow

- Pros: Useful for multi-hop relationships, troubleshooting dependency graphs, and global summaries.
- Cons: Expensive/complex indexing; should be benchmarked only after baseline retrieval failures are measured.

## Decision

Keep the **current Baxter repository-owned RAG pipeline as the core**.

Use the previous demo project's workflow ideology as the governing operating model, now localized in this repository as `SOUL.md` plus the five operating documents.

Treat external systems as references, sidecars, or benchmarks:

- Pathway: optional live indexing/retriever sidecar or benchmark, not answer-policy core.
- RAGFlow: parsing/chunk/citation UX reference and possible offline benchmark, not runtime owner.
- Kotaemon: source preview and citation UI reference.
- AnythingLLM/Dify: demo and workflow inspiration only.
- GraphRAG/m_flow: experimental benchmark for multi-hop technical relations only after eval evidence justifies it.

## Consequences

### Positive

- The project keeps control over source boundaries, citations, fallback, eval, and long-running data cleanup.
- External tools can still be harvested for strong ideas without framework lock-in.
- Future migrations require measured proof instead of attractiveness or speed.

### Negative

- More local implementation work remains in this repository.
- Pathway/RAGFlow features must be integrated selectively instead of adopted wholesale.
- Sidecar experiments need adapters and eval harnesses.

## Validation Plan

Before promoting any external system beyond reference status:

1. Run the same Baxter eval set against current core and candidate system.
2. Compare citation correctness, no-source behavior, retrieval coverage, and source preview quality.
3. Confirm raw manuals remain immutable and generated artifacts remain inspectable.
4. Confirm long-running data review produces ledgers/reports, not only live index state.
5. Confirm rollback preserves current artifacts and answer workflow.

## References

- Pathway `llm-app`: https://github.com/pathwaycom/llm-app
- RAGFlow: https://github.com/infiniflow/ragflow
- Kotaemon: https://github.com/Cinnamon/kotaemon
- AnythingLLM: https://github.com/Mintplex-Labs/anything-llm
- Dify: https://dify.ai/
- Microsoft GraphRAG: https://github.com/microsoft/graphrag
