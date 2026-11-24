# Knowledge Graph Testing Phases

The graph release is not complete until every phase below has a recorded
result. Tests that require public services or browser control are opt-in and
must not be represented as passed by local unit tests.

## Phase 1 — static and schema checks

Purpose: catch syntax, imports, migrations, foreign-key and project-isolation
errors before runtime.

```bash
ruff check biolab tests
python -m compileall -q biolab
pytest -q tests/test_graph_schema.py
```

## Phase 2 — service unit tests

Purpose: verify normalization, idempotency, provenance, review gates, bounded
expansion, rejected-state behavior, and access control without HTTP.

```bash
pytest -q tests/test_graph.py
```

## Phase 3 — REST integration tests

Purpose: verify the browser/API contract, status codes, safe project isolation,
audit integration, and traceable accepted edges.

```bash
pytest -q tests/test_graph_web.py
```

## Phase 4 — MCP contract tests

Purpose: verify tools are discoverable and that MCP exposes graph reads and
proposal creation, but no self-accept/review tool.

```bash
pytest -q tests/test_graph_mcp_contract.py
```

## Phase 5 — regression suite

Purpose: ensure the graph schema's foreign-key enforcement does not break the
existing parser, inference, RAG, project, audit, or auth paths.

```bash
pytest -q -m 'not live and not benchmark'
```

## Phase 6 — local live MCP end-to-end

Purpose: exercise a real streamable-HTTP MCP server process and a real SQLite
file. This phase must create a project, retrieve/parse evidence, propose graph
facts, review them, expand the accepted graph, trace provenance, and verify the
project audit ledger.

```bash
pytest -q -m live tests/test_server.py
```

Network-dependent retrieval tests may be unavailable in a restricted
environment; report them as blocked by environment, not as application passes.

## Phase 7 — browser verification

Purpose: open the project page and verify the visible flow: selected papers,
parse/review, RAG query, graph proposal panel, review buttons, accepted-only
expansion, audit, and download. This phase requires a connected browser
control surface and cannot be proven by `TestClient`.

Release gate: Phases 1–5 pass; Phase 6 passes or has an explicitly documented
network/socket blocker; Phase 7 is observed in the deployed browser before
claiming end-to-end UI completion.
