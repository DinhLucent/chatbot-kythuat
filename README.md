# Baxter IC Technical RAG Chatbot

Fresh-core MVP for Baxter IC technical manuals. Raw source files stay under
`documents/`; generated data is written to `artifacts/`, `staging/`, and
`logs/`.

Raw Baxter manuals are local/private inputs and are not committed to GitHub.
Place approved source files under `documents/IC/BAXTER` before running the
pipeline.

## Quick Start

```powershell
python -m baxter_rag.cli --root . pipeline
python -m baxter_rag.cli --root . pipeline-run
python -m baxter_rag.cli --root . quality-audit
python -m baxter_rag.cli --root . quality-loop --max-iterations 3 --review-limit 12
python -m baxter_rag.cli --root . semantic-review --limit 12
python -m baxter_rag.cli --root . chat "PrisMax co canh bao an toan nao truoc khi service?" --no-llm
.\scripts\start-app.ps1
```

Open `http://127.0.0.1:8090`.

## Pipeline

```text
documents/IC/BAXTER
-> inventory + taxonomy + manifest
-> MinerU or native PDF conversion
-> normalized Markdown + metadata + sections + citations + chunks
-> technical_intelligence.json
-> local hybrid index, optional Qdrant push
-> Vietnamese cited chat
```

## Guarded Automation

The agentic data pipeline is saved in
`docs/agentic_data_pipeline.md`.

```powershell
python -m baxter_rag.cli --root . pipeline-run
python -m baxter_rag.cli --root . pipeline-report
```

Artifacts are written to `artifacts/pipeline/runs/<run_id>/` and the latest
summary is available at `artifacts/pipeline/latest_report.json`.

`pipeline-run` now includes a corpus quality audit stage after normalization.
The audit is the loop-check layer: it scans all normalized documents and creates
deterministic queues for metadata enrichment, OCR retry, duplicate review,
encoding repair, chunk repair, citation repair, table/figure extraction, and
index rebuild. It does not mutate canonical Markdown.

```powershell
python -m baxter_rag.cli --root . quality-audit
python -m baxter_rag.cli --root . quality-audit-report
```

Optional stages are disabled by default:

- `PADDLEOCR_ENABLED=true` creates/uses OCR fallback routing for weak pages.
- `HYPER_EXTRACT_ENABLED=true` plus `HYPER_EXTRACT_ALLOW_EXTERNAL=true` allows
  structured extraction from selected normalized snippets.
- `APODEX_ENABLED=true` plus `APODEX_ALLOW_EXTERNAL=true` enables the validator
  probe against an OpenAI-compatible Apodex endpoint.
- `APODEX_SEMANTIC_REVIEW_ENABLED=true` enables Apodex semantic Markdown review.
  This writes patch proposals only and does not modify normalized Markdown.

## Local Apodex

Apodex GGUF Q4_K_M can run through llama.cpp on CPU for validator/review tasks:

```powershell
.\scripts\start-apodex.ps1
.\scripts\status-apodex.ps1
.\scripts\run-apodex-semantic-review.ps1 -DocId prismax_service_manual_aw8047 -Limit 2
.\scripts\run-quality-loop.ps1 -DocId prismax_service_manual_aw8047 -MaxIterations 2 -ReviewLimit 4 -UseApodex
.\scripts\stop-apodex.ps1
```

The local OpenAI-compatible endpoint is `http://127.0.0.1:8000/v1`.

## MinerU

MinerU was cloned to `tools/MinerU`. Install it only when you are ready for the
heavier document parser runtime:

```powershell
.\scripts\install-mineru.ps1 -PipelineOnly
.\scripts\start-mineru.ps1
```

If MinerU is not installed, the MVP uses native `pypdf` extraction for text-rich
PDFs and records the converter as `native_pdf`.

## API

- `POST /api/documents/inventory`
- `POST /api/documents/convert`
- `POST /api/documents/normalize`
- `POST /api/index/rebuild`
- `POST /api/chat/sessions`
- `POST /api/chat/messages`
- `GET /api/sources/{citation_id}`
- `GET /api/settings`
- `POST /api/settings/llm/test`
- `POST /api/feedback`
- `POST /api/pipeline/run`
- `GET /api/pipeline/report/latest`
- `GET /api/pipeline/state/latest`
- `POST /api/quality-audit/run`
- `GET /api/quality-audit/latest`
- `POST /api/reviews/apodex-semantic`
- `GET /api/reviews/apodex-semantic/latest`
- `POST /api/quality-loop/run`
- `GET /api/quality-loop/report/latest`

## Environment

Copy `.env.example` to `.env` and set `LLM_BASE_URL`, `LLM_API_KEY`, and
`LLM_MODEL` for DeepSeek or another OpenAI-compatible provider. The default
DeepSeek preset uses `https://api.deepseek.com` with `deepseek-v4-flash`.
Without an LLM key, chat uses extractive source-backed responses only.

Model choices are documented in `docs/model-selection.md`: local hybrid
hash+BM25 stays the active MVP baseline, `BAAI/bge-m3` is the recommended next
embedding benchmark, `BAAI/bge-reranker-v2-m3` is optional rerank, and DeepSeek
remains schema-bound and gated by explicit source-snippet upload policy.
