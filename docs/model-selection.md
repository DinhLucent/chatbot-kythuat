# Model Selection

Generated: 2026-06-21

This project keeps local hybrid retrieval as the working baseline. Model choices
below are recommended next steps, not hidden runtime dependencies.

## Current Default

| Stage | Current | Status |
| --- | --- | --- |
| Sparse retrieval | Local BM25 | Active default |
| Dense retrieval | Local hash embedding | Active MVP baseline |
| Rerank | None | Disabled until benchmarked |
| Answer / QA LLM | DeepSeek/OpenAI-compatible when configured | Optional, gated by source upload policy |

## Recommended Profile

| Stage | Model | Why |
| --- | --- | --- |
| Embedding | `BAAI/bge-m3` | Multilingual Vietnamese/English retrieval, long-context friendly, supports dense/sparse/multi-vector modes. |
| Rerank | `BAAI/bge-reranker-v2-m3` | Optional multilingual cross-encoder rerank for top-k candidates after measured retrieval misses. |
| API LLM | `deepseek-v4-flash` | Current DeepSeek OpenAI-compatible default for schema-bound query planning, cited answers, answer QA, and root-cause review. |
| Local LLM later | OpenAI-compatible local server | Private low-risk review and fallback experiments after the same schema/audit contract is preserved. |

## Gates

- Keep `BAXTER_RAG_RERANK_ENABLED=false` until eval shows a retrieval precision
  failure that reranking fixes.
- Keep external upload disabled unless `LLM_EXTERNAL_UPLOAD_ALLOWED=true` is an
  explicit operator decision.
- Do not replace citation maps, source preview, answer fallback labels, or eval
  policy with provider-owned behavior.
- If `deepseek-chat` or `deepseek-reasoner` appears in old configs, migrate to
  `deepseek-v4-flash` or `deepseek-v4-pro`.

## Source Notes

- DeepSeek API docs list `deepseek-v4-flash` and `deepseek-v4-pro`, with
  OpenAI-format base URL `https://api.deepseek.com`, JSON output support, and a
  deprecation date for legacy names.
- BGE-M3 model card describes dense, sparse, and multi-vector retrieval,
  multilingual support for more than 100 languages, and a recommended hybrid
  retrieval plus reranking pipeline.
- BGE reranker docs describe `BAAI/bge-reranker-v2-m3` as a multilingual
  lightweight reranker suitable for efficiency-sensitive reranking.

References:

- https://api-docs.deepseek.com/quick_start/pricing
- https://huggingface.co/BAAI/bge-m3
- https://huggingface.co/BAAI/bge-reranker-v2-m3
