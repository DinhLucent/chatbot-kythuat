# Agent Instructions

## Mandatory AI Agent Onboarding
- Every AI agent in this repository MUST read the project ideology and workflow contract before analyzing, planning, editing, reviewing, or proposing architecture.
- This is mandatory even for small tasks because local changes must not drift away from source-backed Baxter technical RAG.

Read in this order:

1. [Project Soul](SOUL.md)
2. [Core RAG Blueprint And Roadmap](docs/core-rag-blueprint-roadmap.md)
3. [Brain-Human-First Project Criteria](docs/brain-human-first-criteria.md)
4. [AI Answer Workflow](docs/ai-answer-workflow.md)
5. [Core RAG Workflow Decision Log](docs/core-rag-workflow-decision-log.md)
6. [Agent Skill Map](docs/agent-skill-map.md)

These documents are the operating ideology of the project:

- The soul defines why the project exists and what it refuses.
- The blueprint defines where the system is going.
- Brain-human-first defines how agents must think, including long-run completeness.
- The answer workflow defines the measured runtime answer path and visible fallback contract.
- The decision log records why this direction was chosen.
- The skill map explains which optional skills may support work without changing direction.

All future work must support this ideology. Do not replace the core workflow with a monolithic RAG framework, do not add hidden fallback answer paths, do not invent technical facts, and do not skip required long-running data review for speed.

If a request conflicts with these documents, preserve the project ideology first and ask for a new explicit human-approved decision record before changing direction.

## Current Scope
- Current work is governance, constitution, agent rules, and architecture blueprints.
- Do not implement framework migration, sidecars, or platform expansion unless the user explicitly asks for that next step.
- Keep rules minimal: add guidance only when it changes agent behavior or prevents repeated mistakes.
- For LLM-assisted data/audit architecture, read `docs/llm-assisted-data-and-audit-blueprint.md` before changing workflow policy.

## Package Manager
- Use Python 3.11+ with root `pyproject.toml`: `python -m pip install -e .`
- CLI entrypoints: `python -m baxter_rag.cli --root . ...` or `baxter-rag`.

## Verification Commands
- File-scoped checks are for code-only edits; data/workflow claims require pipeline or eval evidence.

| Task | Command |
| --- | --- |
| Syntax check | `python -m py_compile baxter_rag/chat.py` |
| No-LLM chat smoke | `python -m baxter_rag.cli --root . chat "PrisMax co canh bao an toan nao truoc khi service?" --no-llm` |
| Eval subset | `python -m baxter_rag.cli --root . eval-run --limit 5` |
| Full guarded pipeline | `python -m baxter_rag.cli --root . pipeline-run` |
| Pipeline subset | `python -m baxter_rag.cli --root . pipeline-run --doc-id <doc_id> --eval-limit 5` |
| Serve UI/API | `.\scripts\start-app.ps1` |

## CodeGraph
- Use `codegraph_*` tools for structural questions; use `rg`/file reads for literal text, docs, config, and logs.
- If CodeGraph reports stale files, read only the listed pending files before editing or answering.

## Commit Attribution
AI commits MUST include:
```text
Co-Authored-By: (the agent model's name and attribution byline)
```
