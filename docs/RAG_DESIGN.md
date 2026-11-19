# Evidence-first parser RAG design

## Goal

Answer a scientist's question from reviewed document evidence without turning
similarity into truth. Every retrieved passage must remain traceable to a
parser run, document, page, block, section, and content hash.

The system has four separate stages:

```text
PDF -> parser blocks -> human review -> project index -> scoped retrieval -> cited context/answer
```

Parsing, indexing, retrieval, and generation are separate operations. A parser
success is not scientific validation; a high similarity score is not evidence
quality; a generated answer is not a biological conclusion.

## Data contract

The authoritative local record is `parsed_blocks`:

- `parser_run_id`, `document_id`, `source_hash`, and `parser_model`
- `block_id`, page number, block order, block type, bounding box, and text
- normalized `section_name`
- `content_hash`
- `review_state` (`pending`, `accepted`, or `excluded`) and review note

The pgvector projection is disposable and must contain:

- stable `block_id` as the vector key
- `project_id`, `document_id`, and `parser_run_id`
- embedding model, revision, dimension, and creation timestamp
- section, page, block type, text, and content hash

The projection can be rebuilt from accepted local blocks. It is never the
authoritative audit record.

## Indexing policy

1. New parser blocks start as `pending` and are never indexed.
2. A scientist accepts or excludes each block.
3. Only accepted blocks attached to a project are embedded.
4. Upserts use `block_id`, so retries cannot create duplicate chunks.
5. Exclusion deletes the block from the vector projection but preserves the
   local review record.
6. A content-hash or embedding-model change creates a new projection version;
   it does not silently reuse stale vectors.

The free default is a deterministic local embedding stub for tests. Production
can use a local Ollama-compatible embedding model or another OpenAI-compatible
endpoint. No paid provider is required by the architecture.

## Retrieval pipeline

`POST /v1/projects/{project_id}/rag/search` should execute:

1. Validate the question, `top_k`, and section filters.
2. Confirm project access before touching the vector store.
3. Embed the question using the configured model and dimension.
4. Filter by `project_id` and accepted/indexed content before similarity.
5. Apply optional section filters.
6. Retrieve a candidate set larger than the final answer set.
7. Remove near-duplicate blocks and enforce document diversity.
8. Expand selected blocks with adjacent same-document blocks when available.
9. Return ranked chunks plus page/block/content-hash citations.

The first implementation can use pgvector cosine distance. The next free
upgrade is hybrid retrieval: PostgreSQL full-text search plus vector search,
combined with a deterministic reciprocal-rank score. Do not add an LLM reranker
until retrieval evals show that it improves recall without leakage.

## Generation boundary

Retrieval should work without generation and return `disabled` when its
dependencies are unavailable. If generation is enabled, the model receives
only the selected context and a strict output contract:

```json
{
  "answer": "...",
  "citations": [
    {"block_id": "...", "page_number": 2, "content_hash": "..."}
  ],
  "unsupported": []
}
```

Citation validation must reject citations for blocks not present in the
retrieved context. Unsupported questions must produce an explicit
`insufficient_evidence` result rather than a plausible completion.

## Evaluation gate

Before calling the RAG useful, run a small fixture corpus containing known
answers, distractors, duplicate passages, excluded blocks, and two projects.
The gate checks:

- project isolation: zero cross-project hits
- review isolation: zero pending/excluded hits
- section filtering correctness
- stable idempotent re-indexing
- recall@k and MRR on known-answer questions
- citation block/page/hash validity
- explicit disabled and insufficient-evidence behavior

`biolab.rag_eval.evaluate_retrieval` is the deterministic gate used by the
fixture tests. It reports recall@k and MRR, but keeps project, review-state,
and section-filter violations separate. A relevance score never overrides a
safety failure.

## ECC implementation plan

1. **Contract hardening** — add projection metadata/content hashes and make
   accepted-block indexing/rebuild idempotent.
2. **Retrieval quality** — add candidate expansion, deduplication, document
   diversity, and deterministic ranking tests.
3. **Free local backend** — support local embeddings and the Docker pgvector
   harness without external credentials.
4. **Grounded generation** — add an optional local model adapter with strict
   citation validation and refusal behavior.
5. **Evaluation report** — publish fixture metrics and known limitations.

The current repository is between steps 1 and 2: parser review gating,
project-scoped pgvector storage, section filters, and basic search exist. The
next implementation unit is retrieval quality and citation-shaped results, not
another provider integration.
