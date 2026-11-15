# Biolab MCP Server TODO

This is the implementation backlog produced during ECC onboarding. Items are
ordered by risk and usefulness, not by a promise of completion in this change.

## Baseline verified

- [x] Python offline suite: `103 passed, 28 deselected` with
      `pytest -m "not live and not benchmark"`.
- [x] Ruff lint: `ruff check .` passes.
- [x] MyPy: `mypy biolab/` passes.
- [ ] Ruff formatting: 26 existing files would be reformatted. Treat as a
      separate mechanical cleanup so it is easy to review and revert.
- [x] Go tests: rerun with an explicit writable `GOCACHE`; the initial global
      cache restriction was environmental, not a reported code failure.
- [x] A2A compatibility layer: Agent Card, JSON-RPC `message/send`, and
      `tasks/get` covered by offline acceptance tests.

## P0 — release and correctness confidence

- [x] Deploy an alternative public Vercel target. Production health, Agent Card,
      and A2A POST verification pass at `biolab-a2a.vercel.app/api`.

- [ ] Add a CI job or documented command that runs live connector tests on a
      controlled schedule, separate from deterministic pull-request CI.
- [x] Test the unified hosted process end to end: `/api/mcp`, REST health,
      dashboard, auth enforcement, retrieval persistence, and chain verification.
- [x] Align normal CI with the deterministic release gate by excluding live
      network tests; live connector coverage remains a separate scheduled task.
- [x] Add a PyPI release tag/version consistency check.
- [ ] Verify the Go and Python implementations agree on retrieval schema,
      hash-chain semantics, and CLI/server behavior for the supported overlap.
- [ ] Add an explicit deployment smoke check for the containerized Space and
      gateway paths, including persistent storage configuration.
- [ ] Add A2A streaming, durable task storage, and multi-turn continuation when
      a real partner-agent integration requires them.

## P1 — maintainability and user safety

- [ ] Consolidate the schema/migration story: document the current schema
      version, migration order, and safe backup/upgrade procedure.
- [ ] Add tests for malformed or adversarial source responses at every
      connector boundary, including partial multi-source failures.
- [ ] Add an operator runbook for API keys, rate limits, `TURSO_*` settings,
      exposure checks, backups, and hash-chain incident response.
- [ ] Configure Turso credentials and verify durable hosted persistence; this
      requires an operator-owned Turso database and token.
- [ ] Document the exact boundary between evidence retrieval, execution
      provenance, scientist assessment, and biological/clinical validation.
- [ ] Decide whether to reformat the existing codebase with Ruff, then make the
      choice part of CI rather than leaving formatting advisory.

## P2 — product evolution

- [ ] Add the next source adapter only through the connector registry and a
      source-specific audit-input test (candidate: ChEMBL, PDB, GEO/cellxgene).
- [ ] Add durable watch delivery with retries and a dead-letter path.
- [ ] Add retraction/correction tracking and richer evidence-quality review
      fields.
- [ ] Evaluate organization/project authorization before adding multi-user
      hosting; do not treat API-key identity binding as full authorization.
- [ ] Revisit Postgres only after measured concurrent-write requirements justify
      the operational cost over SQLite/libSQL.

## Working rule

Every new scientific or execution capability should preserve provenance,
missing parameters, refusal/error reasons, negative results, and the distinction
between a successful integration call and independently validated scientific
results.
