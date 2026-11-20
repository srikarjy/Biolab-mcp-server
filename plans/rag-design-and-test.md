# Project RAG Design and Test Contract

## Retrieval design

```text
selected paper
  -> parser blocks
  -> human accepts blocks
  -> pgvector embeddings (project + content hash)
  -> vector candidates
                         + accepted graph facts from Turso
                         -> cited hybrid response
```

The RAG is evidence retrieval, not an autonomous scientific validator.

### Trust boundaries

- Pending or excluded parser blocks are never indexed as trusted chunks.
- Every returned vector chunk carries `project_id`, parser run, block, page,
  section, and content hash.
- Graph context is limited to accepted/edited nodes and edges. Pending graph
  proposals cannot affect answers.
- The answer generator is extractive by default: it selects sentences from
  retrieved chunks and returns their citations. If no passage matches, it
  abstains.
- Cache keys include the question, mode, section filter, corpus fingerprint,
  embedding fingerprint, project context, and accepted graph fingerprint.

### Query sequence

1. Validate the project and question.
2. Build a cache key from corpus, embedding, context, and graph versions.
3. Retrieve accepted parser chunks from pgvector, bounded by `top_k`.
4. Retrieve accepted graph facts lexically relevant to the question, bounded by
   the same limit.
5. Return vector passages and graph facts as separate cited evidence channels.
6. Build an extractive answer only from matching vector sentences.
7. Record graph IDs, grounded state, and cache status in the audit ledger.

## Test phases

### Unit contract

- query keys separate search and answer modes;
- corpus, embedding, context, and graph changes invalidate cache keys;
- duplicate passages are removed and documents are diversified;
- section filters and `top_k` bounds are enforced;
- disabled embeddings return an explicit non-grounded state;
- no matching passage produces abstention, not a fabricated answer.

### Storage adapter contract

- only accepted blocks are sent to the vector adapter;
- project ID and embedding fingerprint are preserved;
- vector search is filtered by project, embedding fingerprint, review state,
  and optional section;
- wrong embedding dimensions fail before persistence.

### Hybrid integration contract

- accepted graph facts appear beside vector chunks;
- pending/rejected graph facts do not appear;
- graph changes invalidate cached RAG results;
- audit events include graph edge IDs used by the response;
- an empty graph does not prevent vector-only answers.

### End-to-end release gate

```bash
ruff check biolab tests
pytest -q tests/test_rag.py tests/test_rag_eval.py tests/test_rag_pgvector.py
pytest -q tests/test_graph.py tests/test_graph_web.py -k 'graph or rag'
pytest -q -m 'not live and not benchmark'
```

The live gate additionally requires a configured vector provider and a
three-paper project flow. Without a configured provider, the expected result
is an explicit disabled response, not a claimed successful vector search.
