# Biolab: End-to-End User Guide

Biolab is useful when a person or agent needs scientific evidence that can be
inspected later. The normal flow is:

```text
connect → search → preserve → review → link to a claim → monitor → report → verify
```

Biolab records what an external source returned and how it was used. It does
not decide that a paper is correct, prove a biological mechanism, or provide
clinical advice.

## Choose a path

### Fastest: use the unified hosted platform

Point an MCP client at:

```text
https://biolab-a2a.vercel.app/mcp
```

Claude Code:

```bash
claude mcp add --transport http biolab \
  https://biolab-a2a.vercel.app/mcp
```

Claude Desktop, Cursor, or another MCP client:

```json
{
  "mcpServers": {
    "biolab": {
      "url": "https://biolab-a2a.vercel.app/mcp"
    }
  }
}
```

Then ask the agent to search for a topic, preserve the result, create a
project, add reviewed evidence, link a claim, and produce a report. The client
will call tools such as `search_evidence`, `get_retrieval`,
`create_project`, `add_evidence_to_collection`, `link_claim_evidence`, and
`get_project_report`.

For an agent that speaks A2A, use the hosted Agent Card at
`https://biolab-a2a.vercel.app/.well-known/agent-card.json`, then send a
JSON-RPC `message/send` request to `https://biolab-a2a.vercel.app/a2a`.
Biolab returns a completed A2A task whose artifact contains the same
`retrieval_id`-based evidence result used by MCP and REST. See
[A2A integration](A2A.md) for the request shape.

Hosted capabilities follow the deployed version. Use the local path below when
you need the exact code in this repository, private storage, or the full 0.4
workspace.

## Local path: install and start

```bash
git clone https://github.com/srikarjy/biolab-mcp-server.git
cd biolab-mcp-server
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Start the local dashboard and REST API:

```bash
biolab web
```

Open `http://127.0.0.1:8000`. The same process exposes the dashboard, REST
routes, `/health`, `/metrics`, `/openapi.json`, and `/v1/verify`.

For an MCP endpoint instead, run:

```bash
python -m biolab.server
```

The MCP endpoint is then `http://127.0.0.1:8000/mcp`.

## First real search

In a second terminal, from the repository with the virtual environment active:

```bash
biolab search "BRCA1 pancreatic cancer" --max 5
```

The command calls PubMed, stores the raw response and normalized snapshot in
`biolab.db`, and prints a `retrieval_id`. Keep that ID: it is the stable link
between the source response, a reviewed collection, a claim, and a report.

Inspect the preserved record and verify the audit chain:

```bash
biolab get <retrieval_id>
biolab verify --db biolab.db
```

For a multi-source search:

```bash
biolab evidence "KRAS pancreatic cancer" \
  --sources pubmed,europepmc,clinicaltrials,uniprot,opentargets \
  --max 5
```

Partial source errors are returned explicitly. They are not silently converted
into successful evidence.

## Complete scientist workflow

The repository includes a runnable workflow that exercises the real REST path:

```bash
python scripts/scientist_workflow_demo.py \
  --url http://127.0.0.1:8000
```

It performs this end-to-end sequence:

1. Creates a `KRAS evidence demo` project.
2. Searches UniProt and Open Targets.
3. Preserves the source responses as retrieval records.
4. Creates a reviewed evidence collection.
5. Adds one retrieval with a relevance label and review note.
6. Creates a claim and links the retrieval as contextual evidence.
7. Creates and runs an evidence watch.
8. Generates a project report.
9. Verifies the complete retrieval hash chain.

The JSON output contains the project, retrieval, collection, claim, and watch
IDs. Those IDs can be used to inspect every step through the API or CLI. The
demo note deliberately says that the relationship is provenance, not biological
or clinical validation.

## The same workflow through the CLI

The CLI is convenient for scripts and reproducible terminal work:

```bash
biolab projects create "KRAS target review" \
  --description "Pancreatic cancer evidence"
biolab collections create <project_id> "Reviewed evidence"
biolab collections add <collection_id> <retrieval_id> \
  --relevance relevant --note "Reviewed by scientist"
biolab claims create <project_id> "KRAS is a therapeutic target"
biolab claims link <claim_id> <retrieval_id> \
  --stance supports --assessment human_reviewed
biolab projects report <project_id> --output kras-report.md
biolab verify --db biolab.db
```

To monitor a query over time:

```bash
biolab watches create <project_id> pubmed "KRAS pancreatic cancer"
biolab watches run-all
biolab watches events <watch_id>
```

Watch events describe added, removed, or changed evidence. They do not assert
that a change is scientifically important.

## REST shape for applications

The local API is intentionally simple enough for a script or small internal
tool:

```bash
curl -s http://127.0.0.1:8000/health

curl -s -X POST http://127.0.0.1:8000/v1/evidence/search \
  -H 'content-type: application/json' \
  -d '{"query":"KRAS","sources":["uniprot"],"max_results":1,"agent_id":"demo:app"}'

curl -s http://127.0.0.1:8000/v1/verify
```

The complete route list is available at `http://127.0.0.1:8000/openapi.json`.

## Offline demonstration

When network access is unavailable, run:

```bash
biolab portfolio-demo
```

This uses synthetic in-memory data. It proves that records can be persisted,
retrieved, hash-verified, and that tampering is detected. It makes no scientific
claims and does not contact an external source.

## What a user can trust

After a successful run, a user can trust that Biolab has recorded:

- the source and query used;
- the retrieval timestamp and caller identity;
- the raw response and normalized snapshot;
- response and chain hashes;
- explicit source errors or missing results;
- review labels and claim/evidence relationships;
- project reports and watch events.

A user must still independently review the evidence before treating a claim as
scientifically or clinically established.

## Storage and deployment notes

Local use writes to `biolab.db`, which is ignored by Git. For remote storage,
install the optional extra and set the Turso variables:

```bash
pip install -e ".[turso]"
export TURSO_DATABASE_URL='libsql://...'
export TURSO_AUTH_TOKEN='...'
```

Before exposing an instance publicly, enable API-key authentication, configure
backups and retention, and perform an organization-specific security and
compliance review. The included gateway is not a HIPAA deployment profile.
