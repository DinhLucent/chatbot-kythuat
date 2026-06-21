# ADR-0002: Source Workspaces For Machine-Scoped Chat

## Status

Accepted

## Date

2026-06-21

## Context

The company currently relies most heavily on PrisMax and Prismaflex. The
chatbot should not force the AI to infer the correct machine family from the
entire Baxter corpus on every turn when the user already knows which machine
they are working with.

AnythingLLM provides a useful product pattern: a workspace owns a document set,
while separate threads can share that document set without sharing chat history.
Pathway `llm-app` provides a useful data pattern: retrieval can be scoped by
folders, files, or connected document sources, and live indexing can be studied
later for changing sources.

The Baxter project still needs to preserve repository-owned ingestion,
normalization, citation maps, retrieval policy, fallback behavior, eval, and
quality ledgers.

## Decision

Add a first-class **source workspace** concept to the target architecture.

A source workspace is a named, auditable group of source documents and metadata
filters. Initial workspaces should be:

- `prismax`: PrisMax manuals, bulletins, addenda, and related normalized chunks.
- `prismaflex`: Prismaflex manuals, bulletins, addenda, and related normalized
  chunks.

In the runtime model:

- A source workspace constrains retrieval.
- A chat session/thread stores conversation history and the selected workspace.
- Most user questions search only inside the selected workspace.
- Cross-machine comparison must be explicit and logged as a broader scope.
- If the selected workspace has no supporting evidence, the system returns
  no-source-in-scope or asks to change scope; it must not silently search the
  whole corpus to make an answer appear.

## MVP Shape

Keep the first implementation deliberately small:

```text
Workspace: prismax
  -> Thread: user-created chat thread
    -> Messages and cited answers

Workspace: prismaflex
  -> Thread: user-created chat thread
    -> Messages and cited answers
```

For the current narrow Baxter IC corpus, a workspace only needs:

- stable `workspace_id`;
- display name;
- deterministic retrieval filters or allowed document ids;
- basic thread list;
- chat history and audit logs under each thread.

Do not add a broad settings surface in the MVP. Per-workspace LLM provider,
embedder, vector database, reranker, agent tools, user roles, document upload,
or connector settings can wait until there is a measured need.

## Implementation Direction

Do not create separate framework-owned RAG systems for each machine.

Prefer a shared repository-owned index with deterministic metadata filters:

- `source_workspace_id`
- `allowed_doc_ids`
- `model`
- `doc_type`
- `software_version`
- `doc_code`

Per-workspace materialized indexes, Qdrant collections, or Pathway sidecars may
be considered later only when eval, corpus size, or latency show a measured
need.

The user experience should expose source workspaces clearly:

- choose PrisMax or Prismaflex before asking technical questions;
- show the selected workspace in every answer payload/UI state;
- let the user start multiple threads under the same workspace;
- make cross-workspace compare a deliberate mode, not an implicit fallback.
- avoid exposing settings that do not change current Baxter answer behavior.

## Consequences

### Positive

- Reduces wrong-machine retrieval and model/doc-type confusion.
- Makes scale to future machine families straightforward.
- Improves auditability because each answer records the selected source scope.
- Matches how technical users naturally work: one machine family at a time.
- Keeps the current citation and no-source contract intact.

### Negative

- Requires UI/API/session changes before the behavior is visible.
- Some questions may require a scope-change prompt when users ask broadly.
- Eval cases should be grouped by source workspace, adding test matrix size.

## Boundaries

Borrow from AnythingLLM:

- workspace/thread vocabulary;
- document-set isolation;
- chat history separation under a shared workspace document set.

Do not borrow:

- general chat mode that answers from model knowledge;
- agent behavior that bypasses source evidence;
- broad workspace settings that are unnecessary for the current narrow corpus;
- framework-owned ranking, citation, answer, or eval policy.

Borrow from Pathway `llm-app`:

- source/folder/file filtering patterns;
- live-indexing concepts for future changing data sources;
- optional retriever-sidecar ideas after benchmark proof.

Do not borrow:

- replacement of the current pipeline;
- hidden backend ownership of citation maps, answer policy, or data ledgers.

## Validation Plan

1. Add source-workspace metadata to sessions, request payloads, logs, and eval
   cases.
2. Run PrisMax-only and Prismaflex-only eval subsets.
3. Verify wrong-machine answers decrease without increasing no-source mistakes.
4. Confirm all final answers still include citations or visible no-source /
   clarification behavior.
5. Add cross-workspace comparison evals only after the single-workspace path is
   stable.

## References

- AnythingLLM document/workspace behavior: https://docs.anythingllm.com/chatting-with-documents/introduction
- AnythingLLM chat modes: https://docs.anythingllm.com/features/chat-modes
- Pathway `llm-app`: https://github.com/pathwaycom/llm-app
- Pathway question-answering RAG template: https://pathway.com/developers/templates/rag/demo-question-answering
