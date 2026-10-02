# Biolab 0.4 status and architecture

Last updated: 2026-10-02

Biolab is now a local-first scientific evidence workspace, not only a PubMed MCP
demo. It gives scientists and agents one traceable workflow for retrieving,
organizing, monitoring, and citing external evidence. It is research software;
it does not validate scientific conclusions or provide clinical advice.

## Complete in 0.4

- Five unified connectors: PubMed, Europe PMC, ClinicalTrials.gov, UniProt, and
  Open Targets. The existing bioRxiv/medRxiv tool remains available separately.
- Verbatim source responses, normalized snapshots, source metadata, timestamps,
  caller identity, response hashes, and a database-wide hash chain.
- A connector registry so another source needs only a search function and an
  audit-input converter.
- Projects, reviewed evidence collections, explicit relevance labels, claims,
  evidence stance, and human/agent assessment state.
- Saved evidence watches with added, removed, and content-changed events.
- Project reports in JSON and Markdown.
- CLI, MCP, REST, and a dependency-free local scientist dashboard backed by the
  same services and SQLite database.
- Optional API-key enforcement, identity binding, health, metrics, OpenAPI
  discovery, and a Caddy/Docker gateway template.
- Codex/ChatGPT Agent Plugin packaging and standard `search`/`fetch` MCP tools.
- A BioClaw skill and zero-dependency REST client.
- Offline demo, integrity verification, package metadata, and test coverage.

## Architecture boundaries

```text
scientist / Codex / ChatGPT / BioClaw / another agent
                     |
       CLI | MCP | REST | local dashboard
                     |
     evidence.py + workspace.py
        |                    |
 connector registry     projects / collections /
        |                watches / claims / reports
 public scientific APIs         |
        +---------- retrieval_log.py
                         |
               SQLite/libSQL audit database
```

- Connector modules perform network parsing and produce a source-neutral audit
  input. They do not write the database.
- `retrieval_log.py` is the single retrieval write boundary. A failed write is
  surfaced; evidence is never returned as successfully preserved when it was not.
- `evidence.py` coordinates connectors, persists each result, and reports partial
  source failures explicitly.
- `workspace.py` owns scientist workflow state but references immutable retrieval
  IDs rather than copying source evidence.
- All delivery surfaces call these shared services; behavior should not diverge
  between CLI, web, or MCP.

## Intentional product decisions

- CLI plus local web is the default free-student deployment. The CLI is ideal for
  automation and reproducibility; the web UI makes exploration accessible.
- Remote hosting is optional. One process serves `/mcp`, `/v1/*`, `/`, and health.
- SQLite remains the default because it is free and portable. A mounted volume is
  required in containers. libSQL/Turso support remains available for remote use.
- Retrievals are append-only events. Re-fetching the same external identifier
  creates another record because source content and retrieval time can differ.
- Biolab records evidence and explicit assessments; it does not silently rank a
  paper as true, infer clinical validity, or replace scientist review.
- ChatGPT custom components are optional. Version 0.4 ships tool-first integration
  and a full local dashboard so the core workflow does not depend on a proprietary UI.

## Operations

- Private local: `biolab web`, then open `http://127.0.0.1:8000`.
- Unified hosted process: `python -m biolab.server`.
- Authenticated local API: create a key, then use `biolab web --require-auth`.
- Scheduled monitoring: run `biolab watches run-all` from cron or a free CI timer.
- Gateway template: `docker compose -f deploy/gateway/compose.yml up --build`.

Before a public or regulated deployment, add organization-specific authorization,
backups, retention policies, observability, TLS/domain configuration, and a formal
security/compliance review. The included gateway is not a HIPAA deployment profile.

## Next version candidates, not 0.4 promises

- More source adapters (ChEMBL, PDB, GEO/cellxgene).
- Organization accounts and fine-grained project authorization.
- Durable webhook delivery with retry/dead-letter handling.
- Retraction/correction feeds and richer evidence-quality review forms.
- Postgres when concurrent write volume justifies its operational cost.
