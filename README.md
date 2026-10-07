# Biolab MCP Server

[![Python Version](https://img.shields.io/badge/python-3.11%2B-blue)](https://python.org)
[![Go Version](https://img.shields.io/badge/go-1.23%2B-00ADD8)](https://golang.org)
[![MCP](https://img.shields.io/badge/MCP-1.28.1-purple)](https://modelcontextprotocol.io)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![PyPI](https://img.shields.io/pypi/v/biolab-mcp)](https://pypi.org/project/biolab-mcp/)
[![Docker](https://img.shields.io/badge/docker-ghcr.io%2Fsrikarjy%2Fbiolab--mcp-blue)](https://github.com/srikarjy/biolab-mcp-server/pkgs/container/biolab-mcp)

> *"AI agents querying biological databases leave no audit trail. Six months later, nobody can answer: what exact query returned this result, when, and was that paper peer-reviewed at the time? Biolab solves that."*

A **local-first scientific evidence workspace** with Python and Go clients. It sits between scientists or AI agents and PubMed, Europe PMC, ClinicalTrials.gov, UniProt, Open Targets, and bioRxiv/medRxiv. Every retrieval preserves its source response and returns a `retrieval_id`; projects, reviewed collections, evidence watches, and claim links build a traceable workflow on top.

**New to MCP?** It's an open standard that lets an AI assistant — Codex,
ChatGPT, Claude, Cursor, and others — call external tools during a conversation.
Add Biolab and the assistant can retrieve, preserve, organize, monitor, and cite
scientific evidence while keeping every result inspectable later.

## Use It Now — No Install

A hosted instance is available at `https://srikarjy025-biolab-mcp.hf.space/mcp`. Point your client at it and you're done. Hosted capabilities follow the version currently deployed there; run locally for the complete 0.4 workspace in this repository.

**Claude Code:**
```bash
claude mcp add --transport http biolab https://srikarjy025-biolab-mcp.hf.space/mcp
```

**Claude Desktop / Cursor** — add this to your MCP config file:
```json
{
  "mcpServers": {
    "biolab": {
      "url": "https://srikarjy025-biolab-mcp.hf.space/mcp"
    }
  }
}
```
(Add `"headers": {"Authorization": "Bearer <your key>"}` alongside `"url"` once you have a key — see the rate-limit note below.)

The 0.4 server exposes unified search, source-specific search, standard `search`/`fetch`, retrieval inspection, projects, collections, watches, claims, and reports. Every retrieval is written to a hash-chained audit trail you can inspect later (see [Audit Trail Schema](#audit-trail-schema-v2) below).

For cross-source research, `search_evidence` searches PubMed, Europe PMC,
ClinicalTrials.gov, UniProt, and Open Targets through one call while preserving
explicit results and errors for each source.

Also listed on the [official MCP Registry](https://registry.modelcontextprotocol.io) and [Smithery](https://smithery.ai/servers/srikarjy025/biolab-mcp) if you'd rather discover/install it from there.

**A note on rate limits.** The hosted server is shared and stays open — no signup required for casual use — but callers with no API key share one small, low-throughput budget (1 req/s to PubMed) so no single anonymous user can starve everyone else. If you're doing more than a handful of queries, ask for a key (below) and you get your own isolated, higher budget instead.

**Getting a key:**
```
Authorization: Bearer <your key>
```
Add that header in your client's MCP config (Claude Code: `claude mcp add --transport http biolab <url> --header "Authorization: Bearer <key>"`). Keys are issued with `biolab keys create <label>` — see [Managing API Keys](#managing-api-keys) below if you're running your own instance; otherwise ask the maintainer for one.

Want to run your own copy instead (local dev, your own storage, self-hosting)? Keep reading.

## The Problem

A drug discovery team uses an AI agent to research gene targets. The agent queries PubMed 200 times over three days and surfaces a paper claiming gene X is upregulated in pancreatic cancer. A scientist makes a decision based on that. Six months later, during FDA submission:

- What exact query returned that paper?
- What date was it retrieved?
- Was it peer-reviewed at retrieval time, or a preprint published later?
- Did the agent summarize it accurately, or hallucinate details?

Without Biolab, nobody can answer any of those questions. The retrieval is invisible.

## What Biolab Does

Biolab is an **interception and logging layer**, not a retrieval layer. It doesn't interpret evidence, rank it, or summarize it — it records what happened, verbatim, so an agent's claim can always be traced back to an unforgeable original.

```
Your AI Agent
    ↓  MCP tool call (e.g. search_pubmed)
Biolab MCP Server
    ↓  HTTP
Source API (PubMed, Europe PMC, ClinicalTrials.gov, bioRxiv/medRxiv)
    ↓  paper
Biolab writes a hash-chained retrieval record to the audit database
    ↓  paper + retrieval_id
Back to your agent
```

The agent gets the paper it asked for. Biolab gets a permanent, queryable, tamper-evident record of exactly what happened.

## Sources Supported

| Source | MCP Tool | CLI Command | Notes |
|--------|----------|-------------|-------|
| **PubMed** | `search_pubmed` | `biolab search` | E-utilities, full XML stored |
| **Europe PMC** | `search_europepmc` | `biolab search-europepmc` | Free, indexes bioRxiv/medRxiv |
| **ClinicalTrials.gov** | `search_clinicaltrials` | `biolab search-clinicaltrials` | API v2, condition-based search |
| **UniProt** | `search_evidence` | `biolab evidence` | Protein records and sequences |
| **Open Targets** | `search_evidence` | `biolab evidence` | Target, disease, and drug entities via GraphQL |
| **bioRxiv/medRxiv** | `search_biorxiv` | `biolab search-biorxiv` | Date-range pagination (API limit) |

All sources share a **single audit database** (SQLite locally, or [Turso](https://turso.tech) — a hosted, SQLite-compatible database — in production) with one source-agnostic schema.

## Build It Yourself

You don't need to know Python or Go to get this running locally — just follow these steps in order. All commands are run in a terminal.

### Prerequisites

- **Python 3.11 or newer** — check with `python3 --version`. Get it from [python.org](https://www.python.org/downloads/) if you don't have it.
- **Git** — to download (clone) the code. Check with `git --version`.

That's genuinely it for the Python path — no database server to install, no API keys required (PubMed works anonymously, just at a lower rate limit).

### 1. Get the code

```bash
git clone https://github.com/srikarjy/biolab-mcp-server.git
cd biolab-mcp-server
```

### 2. Install it

```bash
python3 -m venv .venv          # creates an isolated Python environment
source .venv/bin/activate      # on Windows: .venv\Scripts\activate
pip install -e ".[dev]"        # installs the package + test tools
# Add the optional remote Turso driver only if needed: pip install -e ".[turso]"
```

### 3. Try it

```bash
biolab demo --query "BRCA1 pancreatic cancer"
```

This searches PubMed for real, stores every result in a local `biolab.db` file (created automatically, no setup needed), and prints back the `retrieval_id` for each paper — the same ID an AI agent would get back over MCP.

### 4. Run the test suite (optional, confirms everything works)

```bash
pytest -m "not live and not benchmark" -v
```

Run `pytest -m live -v` separately when network access and local socket binding
are available. Those tests exercise real PubMed, Europe PMC, ClinicalTrials.gov,
and bioRxiv services; the default command stays deterministic and offline.

### 5. Run it as an MCP server (what an AI agent actually connects to)

```bash
python -m biolab.server
```

This starts an HTTP server on `http://localhost:8000/mcp` — point Claude Desktop, Claude Code, or Cursor at that URL exactly like in [Use It Now](#use-it-now--no-install), just with `localhost:8000` instead of the hosted URL.

### 6. Build the Docker image (optional)

If you'd rather not install Python locally at all:

```bash
docker build -f space/Dockerfile -t biolab-mcp .
docker run -p 8000:8000 biolab-mcp
```

(Storage defaults to an ephemeral file inside the container unless you set `TURSO_DATABASE_URL`/`TURSO_AUTH_TOKEN` — see [Environment Variables](#environment-variables) below.)

### Prefer a pre-built release?

```bash
pipx install biolab-mcp      # or: pip install biolab-mcp
```

```bash
# Or the Go binary, no Python required at all:
curl -L https://github.com/srikarjy/biolab-mcp-server/releases/latest/download/biolab_darwin_arm64.tar.gz | tar xz
./biolab search "BRCA1 pancreatic cancer" --max 3
```

## Usage

### CLI (Scientist-Friendly)
```bash
# Search several evidence sources in one audited workflow
biolab evidence "KRAS pancreatic cancer" \
  --sources pubmed,europepmc,clinicaltrials,uniprot,opentargets --max 5

# Search PubMed
biolab search "BRCA1 pancreatic cancer" --max 5

# Search Europe PMC
biolab search-europepmc "BRCA1 pancreatic cancer" --max 5

# Search ClinicalTrials.gov
biolab search-clinicaltrials "pancreatic cancer" --max 5

# List bioRxiv preprints (no free-text search - API limitation)
biolab search-biorxiv neuroscience --max 10
biolab search-biorxiv all --server medrxiv --max 10

# Retrieve full audit record
biolab get <retrieval_id>

# List recent retrievals
biolab list --source pubmed --limit 10

# Export for analysis
biolab export evidence.jsonl --source clinicaltrials

# Verify the complete tamper-evident audit chain
biolab verify --db biolab.db

# Run demo
biolab demo --query "BRCA1 pancreatic cancer"

# Run the offline portfolio demo (no network or scientific claims)
biolab portfolio-demo

# Run the real end-to-end scientist workflow against a local server
python scripts/scientist_workflow_demo.py --url http://127.0.0.1:8000

# Start the private local dashboard and REST API
biolab web
# Open http://127.0.0.1:8000

# Create a project, organize evidence, and trace a claim
biolab projects create "KRAS target review" --description "Pancreatic cancer evidence"
biolab collections create <project_id> "Reviewed evidence"
biolab collections add <collection_id> <retrieval_id> --relevance relevant --note "Reviewed"
biolab claims create <project_id> "KRAS is a therapeutic target"
biolab claims link <claim_id> <retrieval_id> --stance supports --assessment human_reviewed

# Monitor a source for added, removed, or revised records
biolab watches create <project_id> pubmed "KRAS pancreatic cancer"
biolab watches run-all

# Export a complete project report
biolab projects report <project_id> --output report.md
```

The dashboard binds to `127.0.0.1` by default. REST endpoints cover evidence,
projects, collections, watches, claims, and reports. Discover them at
`/openapi.json`; use `/health`, `/metrics`, and `/v1/verify` for operations.
Run `biolab web --require-auth` with keys created by `biolab keys create`, or use
`BIOLAB_REQUIRE_AUTH=true` on the unified MCP server, before exposing it remotely.

### MCP Tools (Agent-Friendly)
```json
// Search any source
{"name": "search_evidence", "arguments": {"query": "KRAS pancreatic cancer", "agent_id": "research:target-validation", "sources": ["pubmed", "europepmc", "clinicaltrials", "uniprot", "opentargets"], "max_results": 5}}
{"name": "search_pubmed", "arguments": {"query": "BRCA1 pancreatic cancer", "agent_id": "aletheia:advocate", "max_results": 5}}
{"name": "search_europepmc", "arguments": {"query": "BRCA1 pancreatic cancer", "agent_id": "aletheia:advocate", "max_results": 5}}
{"name": "search_clinicaltrials", "arguments": {"query": "pancreatic cancer", "agent_id": "aletheia:advocate", "max_results": 5}}
{"name": "search_biorxiv", "arguments": {"category": "neuroscience", "agent_id": "aletheia:advocate", "max_results": 5, "server": "biorxiv"}}

// Retrieve full audit record (works for ALL sources)
{"name": "get_retrieval", "arguments": {"retrieval_id": "uuid-from-search"}}
```

### BioClaw

A ready-to-copy BioClaw skill is included at
`integrations/bioclaw/biolab-evidence`. It connects BioClaw's containerized
research assistant to the local REST API and supports cross-source search,
retrieval inspection, and chain verification without extra Python packages.

### Codex and ChatGPT

- Start the complete local server with `python -m biolab.server`.
- Copy `integrations/codex/config.toml.example` into your Codex configuration and
  keep tool approval on `prompt` while developing.
- The portable Agent Plugin bundle is in `plugins/biolab/` (`plugin.json`,
  `mcp.json`, and its evidence-workspace skill). Change the remote URL in
  `mcp.json` to your HTTPS deployment before publishing or submitting it.
- The MCP server includes standard `search` and `fetch` knowledge tools plus the
  richer Biolab tools. Tool annotations disclose read-only, write, external-data,
  and idempotency behavior to clients.

ChatGPT/Codex can therefore search and preserve evidence, create project state,
run watches, and trace claims. The assistant proposes relationships; a scientist
must use `human_reviewed` when a relationship has actually been reviewed.

### REST and API gateways

One `python -m biolab.server` process serves MCP at `/mcp`, the dashboard at `/`,
and REST under `/v1`. The important routes are:

| Route | Purpose |
|---|---|
| `POST /v1/evidence/search` | Audited multi-source search |
| `GET /v1/retrievals/{id}` | Full preserved retrieval |
| `GET, POST /v1/projects` | List or create projects |
| `POST /v1/collections` | Create a reviewed collection |
| `GET, POST /v1/watches` | List or create monitoring queries |
| `POST /v1/watches/run-all` | Scheduled drift detection |
| `POST /v1/claims` | Create a claim without asserting truth |
| `GET /v1/claims/{id}` | Trace claim/evidence relationships |
| `GET /v1/projects/{id}/report` | JSON or `?format=markdown` report |
| `GET /openapi.json` | Gateway/API discovery document |

The zero-cost Caddy/Docker example in `deploy/gateway/` adds a reverse-proxy
boundary and persistent volume. It binds only to localhost by default. Add a real
domain, HTTPS, backups, and organization-specific authorization before public use.

### Python API
```python
from biolab.pubmed_client import search_and_fetch
from biolab.retrieval_log import write_retrieval, get_retrieval
from biolab.db import connect

conn = connect("biolab.db")
papers = search_and_fetch("BRCA1 pancreatic cancer", 3)
for p in papers:
    record = write_retrieval(conn, query="...", pmid=p.pmid, ...)
    print(record.retrieval_id)
```

## Managing API Keys

The server stays open to unauthenticated callers by design — but they all share one small, low-throughput rate-limit budget (see [Use It Now](#use-it-now--no-install)). Issuing someone a key gives them their own isolated, higher budget instead. This doesn't gate *access* — it's purely a fairness mechanism so one caller can't starve everyone else's share of PubMed's real rate limit.

```bash
# Issue a key — the raw key is shown once, save it immediately
biolab keys create alice

# List issued keys (never shows the raw key — only a hash is stored)
biolab keys list

# Revoke all of a label's active keys
biolab keys revoke alice
```

The caller sends the key back as `Authorization: Bearer <key>`. By default a
missing header uses the anonymous tier while an invalid/revoked key returns `401`.
Set `BIOLAB_REQUIRE_AUTH=true` to reject requests without a key as well.

## Environment Variables

All optional — the server runs with sensible defaults if you set none of these.

| Variable | Purpose | Default |
|----------|---------|---------|
| `BIOLAB_DB_PATH` | Local SQLite file path (ignored if `TURSO_DATABASE_URL` is set) | `biolab.db` |
| `TURSO_DATABASE_URL` | Optional remote [Turso](https://turso.tech) URL; requires `pip install "biolab-mcp[turso]"` | unset (uses local file) |
| `TURSO_AUTH_TOKEN` | Auth token for the Turso database above | unset |
| `BIOLAB_HOST` | Host the MCP server binds to; non-loopback requires `BIOLAB_REQUIRE_AUTH=true` or `BIOLAB_ALLOW_ANONYMOUS=true` | `127.0.0.1` |
| `BIOLAB_PORT` | Port the MCP server listens on | `8000` |
| `BIOLAB_REQUIRE_AUTH` | Require a valid Bearer API key on MCP/REST requests | `false` |
| `NCBI_API_KEY` | Raises the PubMed rate limit from 3 req/s to 10 req/s | unset (works fine without one) |

## Audit Trail Schema (v2)

```sql
CREATE TABLE retrievals (
    retrieval_id     TEXT PRIMARY KEY,  -- UUID
    source           TEXT NOT NULL,     -- pubmed, europepmc, trials, UniProt, Open Targets, etc.
    external_id      TEXT NOT NULL,     -- PMID, NCT ID, DOI, etc.
    query_text       TEXT NOT NULL,     -- exact query sent to source
    retrieved_at     TEXT NOT NULL,     -- ISO 8601 UTC
    agent_id         TEXT NOT NULL,     -- e.g. "aletheia:advocate"
    source_metadata  TEXT NOT NULL,     -- JSON: source-specific fields
    raw_response     TEXT NOT NULL,     -- verbatim XML/JSON from source
    snapshot         TEXT NOT NULL,     -- JSON: structured fields (title, abstract, authors, journal, DOI, pub types, MeSH/conditions)
    response_hash    TEXT NOT NULL,     -- SHA-256(prev_hash + raw_response + retrieval_id + retrieved_at)
    prev_hash        TEXT NOT NULL      -- response_hash of the previous row — makes this a hash chain
);
```

**Key properties:**
- One row per paper retrieval (not per query)
- Raw response stored verbatim — parsing bugs are recoverable
- **Hash-chained**, not just hashed: each row's hash covers the previous row's hash too, so deleting or editing any row — even in the database directly — breaks the chain for every row after it. Call `retrieval_log.verify_chain(conn)` to check the whole log; it returns exactly which row broke, if any.
- Background write queue serializes all writes through one path, so the chain stays consistent even under concurrent agent calls

## Architecture

```
biolab/
├── cli.py                   # Search plus scientist workspace commands
├── server.py                # Unified MCP + REST + dashboard process
├── evidence.py              # Shared multi-source orchestration and audit persistence
├── workspace.py             # Projects, collections, watches, claims, reports
├── web.py                   # Local dashboard + versioned REST API
├── db.py                    # Connection + schema (local SQLite or remote Turso)
├── models.py                 # RetrievalRecord dataclass
├── retrieval_log.py          # Only writer + background queue + hash chain
├── pubmed_client.py           # PubMed E-utilities wrapper + rate limiter
├── europepmc_client.py        # Europe PMC adapter
├── clinicaltrials_client.py   # ClinicalTrials.gov adapter
├── uniprot_client.py          # UniProt adapter
├── opentargets_client.py      # Open Targets GraphQL adapter
├── biorxiv_client.py          # bioRxiv/medRxiv adapter
└── migrations/                # Schema migration scripts

integrations/
├── bioclaw/                   # BioClaw evidence skill and REST client
└── codex/                     # Local MCP configuration template

plugins/biolab/                # Portable Codex/ChatGPT Agent Plugin bundle
deploy/gateway/                # Caddy + Docker Compose deployment template

space/                      # Files pushed to the hosted Hugging Face Space
├── Dockerfile                # Python-server-specific image (see repo-root Dockerfile for the Go one)
└── README.md                  # Space config (title, hosting metadata)
```

**Design principles:**
- Python + Go implementations (same interface, different runtimes)
- Shared core with CLI, MCP, REST, and web delivery surfaces
- Database, not log files — structured queries across time
- Hard-fail, never degrade — paper without `retrieval_id` is worse than error
- Offline acceptance tests plus explicit live connector tests
- Single-writer queue, not row-level locking — simplest thing that keeps the hash chain consistent under concurrency

## Development

```bash
# Python
pip install -e ".[dev]"
pytest -m "not live and not benchmark" -v

# Go
cd go-biolab
go test ./...
go build -o biolab ./cmd/cli
go build -o biolab-server ./cmd/server
```

## Deployment

| Target | Method |
|--------|--------|
| **Hosted (no install)** | https://srikarjy025-biolab-mcp.hf.space/mcp — capabilities depend on the deployed Space version |
| **Local** | `pipx install biolab-mcp` or download binary |
| **CI/CD** | GitHub Actions → PyPI (Trusted Publishing/OIDC) + GHCR + GitHub Releases |
| **Containers** | `docker pull ghcr.io/srikarjy/biolab-mcp:latest`, or build `space/Dockerfile` yourself |
| **Linux packages** | `.deb`, `.rpm`, `.apk` via goreleaser |
| **Discovery** | [MCP Registry](https://registry.modelcontextprotocol.io) · [Smithery](https://smithery.ai/servers/srikarjy025/biolab-mcp) |

**Student-cost path:** local SQLite, the CLI, dashboard, MCP server, BioClaw
client, and Caddy gateway require no paid service. Free hosted tiers may also work,
but their quotas and persistence policies can change; verify them before relying on
a public deployment.

## Roadmap

- [x] Added/removed/content-changed evidence monitoring via response hashes
- [x] Projects, collections, claims, reports, REST, dashboard, and agent packaging
- [ ] Provenance graph (cross-source linking by DOI)
- [ ] Nextflow/Snakemake plugins
- [ ] Rate limiting + caching (audit-safe)
- [ ] Organization accounts and fine-grained project authorization
- [ ] Retraction/correction feeds with durable alert delivery

## License

MIT — see [LICENSE](LICENSE)

## Author

**Srikar Jy** — [srikarjy025@gmail.com](mailto:srikarjy025@gmail.com)
