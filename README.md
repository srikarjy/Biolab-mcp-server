# Biolab MCP Server

Biolab is a local-first scientific evidence workspace for Python and Go. It
retrieves records from life-science sources, preserves the returned payload and
metadata, and makes each retrieval inspectable through MCP, REST, A2A, CLI, and
a small dashboard.

Biolab records provenance. It does not decide that a paper is correct, prove a
biological mechanism, rank evidence, or provide clinical advice.

## What is implemented

- Source adapters for PubMed, Europe PMC, ClinicalTrials.gov, UniProt, Open
  Targets, and bioRxiv/medRxiv.
- A retrieval log with raw responses, normalized snapshots, retrieval IDs, and
  a hash chain.
- Projects, reviewed collections, claims, evidence watches, and project reports.
- Python CLI and server, Go audit-chain client, REST API, MCP tools, A2A
  endpoint, and a browser dashboard.
- Local SQLite storage by default, with optional Turso/libSQL storage.
- Human-review fields that keep provenance separate from scientific validation.

## Live demo

The current hosted demo is:

```text
https://biolab-a2a.vercel.app/
```

The deployment exposes these surfaces from one domain:

| Surface | URL | Purpose |
| --- | --- | --- |
| Dashboard | `https://biolab-a2a.vercel.app/` | Create projects, search, inspect results, and verify the chain |
| MCP | `https://biolab-a2a.vercel.app/mcp` | Streamable HTTP MCP endpoint |
| REST | `https://biolab-a2a.vercel.app/v1/...` | Application and automation API |
| OpenAPI | `https://biolab-a2a.vercel.app/openapi.json` | REST contract |
| Agent Card | `https://biolab-a2a.vercel.app/.well-known/agent-card.json` | A2A discovery metadata |
| A2A | `https://biolab-a2a.vercel.app/a2a` | JSON-RPC agent endpoint |
| Health | `https://biolab-a2a.vercel.app/health` | Runtime health check |
| Metrics | `https://biolab-a2a.vercel.app/metrics` | Runtime metrics |

### Connect an MCP client

Claude Code:

```bash
claude mcp add --transport http biolab \
  https://biolab-a2a.vercel.app/mcp
```

Claude Desktop, Cursor, or another compatible client:

```json
{
  "mcpServers": {
    "biolab": {
      "url": "https://biolab-a2a.vercel.app/mcp"
    }
  }
}
```

The hosted demo allows anonymous access for low-volume use. It is not a
production multi-tenant service. Without Turso variables, Vercel uses an
ephemeral `/tmp` database; projects and evidence can disappear when the
serverless instance is replaced.

## Why preserve retrievals?

An agent may later need to answer:

- Which exact query was sent?
- Which source returned the record, and when?
- What raw response and normalized fields did the agent receive?
- Which evidence was reviewed or linked to a claim?
- Has the local audit chain been altered since retrieval?

Biolab returns a `retrieval_id` and stores the source response before returning
the result. A later reviewer can inspect the record instead of relying on an
untraceable summary.

## Audit boundary

The retrieval log is hash-chained: each record includes the previous record's
hash and the current record's audited fields. Verification can identify a
broken link or altered row.

This is tamper evidence, not an immutable ledger. An operator still needs
access control, backups, external anchors, and review procedures for a serious
production deployment. Biolab does not validate the truth of a source response
or a claim derived from it.

## Local installation

Requirements:

- Python 3.11 or newer
- Git

```bash
git clone https://github.com/srikarjy/biolab-mcp-server.git
cd biolab-mcp-server
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\\Scripts\\activate
pip install -e ".[dev]"
```

Run the CLI demo:

```bash
biolab demo --query "BRCA1 pancreatic cancer"
```

Run the private local dashboard and REST API:

```bash
biolab web
```

Open `http://127.0.0.1:8000`.

Run the MCP server directly:

```bash
python -m biolab.server
```

The MCP endpoint is then `http://127.0.0.1:8000/mcp`.

## First evidence pass

Single-source PubMed search:

```bash
biolab search "BRCA1 pancreatic cancer" --max 5
```

Multi-source search:

```bash
biolab evidence "KRAS pancreatic cancer" \
  --sources pubmed,europepmc,clinicaltrials,uniprot,opentargets \
  --max 5
```

Inspect a preserved result and verify the chain:

```bash
biolab get <retrieval_id>
biolab verify --db biolab.db
```

Rank preserved results with an explainable, deterministic scorer through REST:

```bash
curl -s -X POST http://127.0.0.1:8000/v1/rankings \
  -H 'content-type: application/json' \
  -d '{"question":"KRAS randomized trial", "retrieval_ids":["<retrieval_id>"]}'
```

The response contains the rank, total score, component scores, matched terms,
missing facets, and reason codes. The score describes fit to the query; it is
not a scientific truth or clinical-quality score.

The multi-source operation preserves source-specific results and explicit
source errors. A returned record is preserved evidence, not an assertion that
the result is relevant or scientifically correct.

Extract citation-safe grounded insights from preserved results:

```bash
curl -s -X POST http://127.0.0.1:8000/v1/inferences \
  -H 'content-type: application/json' \
  -d '{"question":"KRAS pancreatic cancer", "retrieval_ids":["<retrieval_id>"], "max_insights":5}'
```

The parser is deterministic and provider-free. It extracts only sentences
present in preserved title or abstract fields, stores exact field offsets, and
returns a citation locator plus excerpt hash for every insight. It is an
evidence extraction aid, not an LLM-generated conclusion or scientific
validation. Use `GET /v1/inferences/{inference_run_id}` to inspect a run and
`POST /v1/inferences/{inference_run_id}/flush` to remove temporary parser
chunks while retaining derived claims and citations.

## Organize reviewed evidence

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
```

The same project report is available as JSON (default), Markdown, or a
server-rendered PDF:

```text
GET /v1/projects/{project_id}/report
GET /v1/projects/{project_id}/report?format=markdown
GET /v1/projects/{project_id}/report?format=pdf
```

Exports are checksummed and recorded. PDF generation is text-focused and
server-side, so it does not depend on browser state.

The hosted demo can parse the snapshots returned by the current browser request
even when a serverless invocation cannot see the prior ephemeral SQLite row.
Those runs are labeled `request-context` and are not durable; configure Turso
for durable retrievals, ranking runs, and inference history.

The `human_reviewed` assessment is an explicit workflow value. It must not be
used to imply that Biolab itself performed the review.

Monitor a source over time:

```bash
biolab watches create <project_id> pubmed "KRAS pancreatic cancer"
biolab watches run-all
biolab watches events <watch_id>
```

Watch events describe source changes. They do not decide whether a change is
scientifically important.

## Supported sources

| Source | Main tool or command | Stored material |
| --- | --- | --- |
| PubMed | `search_pubmed`, `biolab search` | E-utilities response and normalized paper fields |
| Europe PMC | `search_europepmc`, `biolab search-europepmc` | Europe PMC response and normalized fields |
| ClinicalTrials.gov | `search_clinicaltrials`, `biolab search-clinicaltrials` | API response and trial fields |
| UniProt | `search_evidence`, `biolab evidence` | Protein records and normalized fields |
| Open Targets | `search_evidence`, `biolab evidence` | Target, disease, and drug records returned by the adapter |
| bioRxiv/medRxiv | `search_biorxiv`, `biolab search-biorxiv` | Preprint records returned by the adapter |

External APIs can change, rate-limit requests, or return partial failures. The
runtime reports those failures rather than treating them as successful evidence.

## REST and A2A

Important REST routes:

```text
POST /v1/evidence/search
POST /v1/rankings
GET  /v1/rankings/{ranking_run_id}
POST /v1/inferences
GET  /v1/inferences/{inference_run_id}
POST /v1/inferences/{inference_run_id}/flush
POST /v1/parser/parse
GET  /v1/parser/runs/{parser_run_id}
POST /v1/parser/runs/{parser_run_id}/flush
GET  /v1/retrievals/{retrieval_id}
GET  /v1/projects
POST /v1/projects
GET  /v1/projects/{project_id}/report
POST /v1/collections
GET  /v1/watches
POST /v1/watches
POST /v1/watches/run-all
POST /v1/claims
GET  /v1/claims/{claim_id}
GET  /v1/verify
```

For A2A clients, discover the service from:

```text
GET  /.well-known/agent-card.json
POST /a2a
```

The A2A endpoint accepts the supported JSON-RPC task shape documented in
[`docs/A2A.md`](docs/A2A.md).

A2A `message/send` also accepts `metadata.operation` values `rank`,
`grounded_insights`, and `project_report`; these call the same ranking,
inference, and report services used by REST and MCP. File downloads are
explicitly reported as unsupported in the Agent Card; request report JSON or
use the REST PDF endpoint when the client supports binary downloads.

## Storage and deployment

Local deployments use SQLite:

```text
BIOLAB_DB_PATH=biolab.db
```

DeepSeek and cloud RAG are opt-in production capabilities:

```text
DEEPSEEK_API_KEY=...
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat
DATABASE_URL=postgresql://...          # Postgres with pgvector enabled
BIOLAB_EMBEDDING_API_URL=https://.../v1/embeddings
BIOLAB_EMBEDDING_API_KEY=...
BIOLAB_EMBEDDING_MODEL=text-embedding-3-small
BIOLAB_EMBEDDING_DIMENSION=1536
HF_TOKEN=...
HF_PARSER_ENDPOINT=https://your-inference-endpoint
HF_PARSER_MODEL=nvidia/Nemotron-3.5-Parse
```

Parse a PDF through the Hugging Face backend by sending base64 content to
`POST /v1/parser/parse`. The response contains page-aware blocks with block
types, bounding boxes, content hashes, and the selected parser model. Inspect
it with `GET /v1/parser/runs/{parser_run_id}` and remove disposable parsed
blocks with `POST /v1/parser/runs/{parser_run_id}/flush`. The parser endpoint
must be a Hugging Face Inference Endpoint or Space wrapper capable of rendering
PDF pages for the selected model.

When the optional pgvector path is configured (`DATABASE_URL` or
`PGVECTOR_DATABASE_URL`, plus `BIOLAB_EMBEDDING_API_URL` and
`BIOLAB_EMBEDDING_API_KEY`), each non-empty parsed block is also stored in the
separate `biolab_parser_chunks` vector table with its parser run, page number,
block type, and text. Flushing a parser run removes both its local parsed blocks
and its cloud vector chunks while preserving parser-run metadata and reviewed
citations.

When `DEEPSEEK_API_KEY` is present, inference uses DeepSeek’s structured
grounded-generation path. When the database and embedding provider are also
configured, source chunks are indexed in `biolab_evidence_chunks` and the
question is narrowed through pgvector similarity before DeepSeek sees the
context. Citation validation rejects claims whose retrieval ID, field, or
quoted span cannot be matched to the supplied evidence. Without these
variables, the dashboard uses the transparent local extractor and labels
request-context runs as non-durable.

Parsed documents can be assigned to a project folder by including `project_id`
in `POST /v1/parser/parse`. Use `GET /v1/projects/{project_id}/parser-runs` to
list that project’s parsed documents and `GET /v1/parser/runs/{parser_run_id}/download`
to download the page-aware parsed JSON. Project ownership is checked at each
read, download, and flush boundary; the raw PDF is not retained by this parser
endpoint.

Project-scoped semantic retrieval is available at
`POST /v1/projects/{project_id}/rag/search`. It searches only vectors belonging
to that project and returns document, parser-run, block, page, section, and
similarity metadata. The endpoint reports `disabled` until both an embedding
provider and pgvector database are configured; it never silently falls back to
cross-project or unindexed data.

For durable hosted storage, install the optional Turso dependency and configure
both variables:

```bash
pip install -e ".[turso]"
export TURSO_DATABASE_URL="libsql://your-database.turso.io"
export TURSO_AUTH_TOKEN="your-token"
```

The Vercel adapter installs the Turso extra, but it does not create a database
or supply credentials. Configure `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` in
the deployment environment before treating the hosted service as durable.

Other useful variables:

| Variable | Purpose | Default |
| --- | --- | --- |
| `BIOLAB_DB_PATH` | Local SQLite path | `biolab.db` |
| `BIOLAB_ARTIFACT_DIR` | Execution artifact directory | `artifacts` |
| `TURSO_DATABASE_URL` | Optional remote libSQL/Turso URL | unset |
| `TURSO_AUTH_TOKEN` | Token for the Turso URL | unset |
| `BIOLAB_HOST` | Bind address | `127.0.0.1` locally |
| `BIOLAB_PORT` | HTTP port | `8000` |
| `BIOLAB_REQUIRE_AUTH` | Require a valid bearer key | `false` |
| `NCBI_API_KEY` | Higher PubMed request limit | unset |

The repository includes a Vercel adapter in `app.py`, route rewrites in
`vercel.json`, and deployment notes in
[`docs/VERCEL_DEPLOYMENT.md`](docs/VERCEL_DEPLOYMENT.md).

## Testing and development

The default CI-safe suite excludes live-network and benchmark tests:

```bash
pytest -m "not live and not benchmark" -v
ruff check .
mypy biolab
```

Live connector tests require network access and may be rate-limited. They are
not evidence that a source API will remain available or return unchanged data.

The project also contains:

- [`ONBOARDING.md`](ONBOARDING.md) — architecture and contributor entry points
- [`docs/END_TO_END_USER_GUIDE.md`](docs/END_TO_END_USER_GUIDE.md) — user workflow
- [`docs/A2A.md`](docs/A2A.md) — A2A request and discovery details
- [`docs/COMPLEX_PROBLEM_LOG.md`](docs/COMPLEX_PROBLEM_LOG.md) — investigation log
- [`TODO.md`](TODO.md) — backlog and verification baseline

## Release status

The repository source currently declares version `0.4.1`. The hosted deployment
and source tree are separate from package publication: do not assume that the
latest repository commit is already available on PyPI. Check the package index
and release workflow before installing a specific version.

The Hugging Face Space is not the canonical deployment path for this project.
Use the Vercel URL above for the current hosted demo, or run the application
locally for private storage and full control.

## License

MIT. See [`LICENSE`](LICENSE).
