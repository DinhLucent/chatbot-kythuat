# Agent Skill Map

Generated: 2026-06-21

This map is secondary to `AGENTS.md`, `SOUL.md`, the blueprint, brain-human-first criteria, answer workflow, and decision log. Skills can support work but must not override project ideology.

## Skill Policy

- Read mandatory onboarding docs first.
- Use the smallest relevant skill set for the task.
- If a skill suggests hidden fallback, framework takeover, direct source mutation, or shortcut validation, reject that part.
- If a named skill is unavailable, follow the local docs and proceed with ordinary tools.

## Recommended Skill Use

| Work Area | Skill | Use When |
| --- | --- | --- |
| Agent docs | `agents-md` | Updating `AGENTS.md`, `CLAUDE.md`, or onboarding rules. |
| RAG/retrieval design | RAG or vector DB skills, if available | Chunking, metadata, hybrid search, Qdrant, retrieval quality. |
| Evaluation | `advanced-evaluation` or `agent-evaluation` | Eval cases, answer quality gates, behavioral regressions. |
| Architecture decisions | `architecture-decision-records`, `architect-review` | Workflow, fallback, framework, graph, or external-tool policy changes. |
| Security/API | `api-security-best-practices`, security audit skills | Auth, secrets, external upload, deployment risk. |
| UI/API verification | browser/API testing skills | Frontend/API smoke checks after behavior changes. |

## Trigger Matrix

| If The Task Mentions | Start With |
| --- | --- |
| `AGENTS.md`, agent rules, onboarding | `agents-md` |
| data quality, OCR, normalization, long run | Blueprint + brain-human-first + pipeline docs |
| LLM-assisted data review, DeepSeek, root-cause logs, answer QA | `docs/llm-assisted-data-and-audit-blueprint.md` + answer workflow + architecture review |
| retrieval, Qdrant, ranking, filters | Blueprint + answer workflow + vector/RAG skill if available |
| citations, source preview, no-source | Answer workflow + evaluation skill if available |
| fallback, framework, external tools | Decision log + architecture review |
| commit/push | Git guidance and commit attribution rules |

## Project-Specific Rule

The best skill is still the project ideology:

```text
project soul
-> blueprint
-> brain-human-first reasoning
-> one source-backed workflow
-> long-run data completeness
-> evidence/citation/eval
-> smallest supporting skill
```
