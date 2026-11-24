# Biolab MCP Server

[![CI](https://github.com/srikarjy/biolab-mcp-server/actions/workflows/ci.yml/badge.svg)](https://github.com/srikarjy/biolab-mcp-server/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](pyproject.toml)

Biolab is a small, local-first research workspace for finding papers, saving
evidence, parsing selected papers, and asking questions over a paper-specific
RAG index.

It keeps source responses, retrieval IDs, parser blocks, citations, and review
decisions so results can be inspected later. It is a research tool, not medical
advice or proof that a scientific claim is true.

Live demo: [biolab-a2a.vercel.app](https://biolab-a2a.vercel.app/)

## Tech stack

- Python 3.11+, Starlette, Typer, and MCP
- SQLite/libSQL for local project and provenance data
- Neon Postgres with `pgvector` for hosted RAG vectors
- NVIDIA NIM hosted embeddings (`nemotron-3.5-embed-1b`) with a free offline fallback
- PubMed, bioRxiv, medRxiv, and ClinicalTrials.gov connectors
- Vercel serverless deployment

## What you can do

1. Search PubMed and other life-science sources.
2. Create a project and select papers for it.
3. Parse a selected paper into page-aware blocks.
4. Review blocks and index accepted blocks in pgvector.
5. Search the project RAG and save notes with citations.
6. Download the parsed paper JSON.
7. Audit the selected project's hash-chained history.

The browser has two main pages:

- `/` — dashboard for projects, search, and evidence.
- `/projects/<project_id>` — one project’s papers, parser runs, RAG search,
  notes, and downloads.

## Install

Requirements: Python 3.11+ and Git.

```bash
git clone https://github.com/srikarjy/biolab-mcp-server.git
cd biolab-mcp-server
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Run locally

Start the dashboard and REST API:

```bash
biolab web
```

Open <http://127.0.0.1:8000>.

Start the MCP server instead:

```bash
python -m biolab.server
```

The MCP endpoint is <http://127.0.0.1:8000/mcp>.

For ChatGPT, Claude, and other research-client connection instructions, see
[External AI integrations](docs/EXTERNAL_AI_INTEGRATIONS.md).

## Basic workflow

Search for papers:

```bash
biolab search "BRCA1 pancreatic cancer" --max 5
```

Or use the dashboard to create a project and select a retrieved paper. The
project page then lets you parse it, review blocks, search the RAG, save notes,
and download the parsed JSON.

Useful REST routes:

```text
POST /v1/evidence/search
POST /v1/projects
GET  /v1/projects/{project_id}/report
POST /v1/projects/{project_id}/retrievals
POST /v1/projects/{project_id}/retrievals/{retrieval_id}/parse
GET  /v1/projects/{project_id}/parser-runs
POST /v1/projects/{project_id}/rag/search
GET  /v1/projects/{project_id}/audit
GET  /v1/parser/runs/{parser_run_id}/download
POST /v1/projects/{project_id}/rag/notes
```

The download route returns an attachment such as `paper.parsed.json`. It
contains parsed blocks and metadata. The original PDF is not retained by the
parser endpoint.

## Paper-to-RAG workflow

After searching, accept the papers you want and choose a project in the project
picker. The selected papers appear on that project's second page. There you can
select one or more papers, parse them, review their blocks, accept only useful
blocks for RAG, and query the resulting paper-specific index.

The project page stores exact questions and successful RAG results in a small
project-scoped cache. Repeating the same question with the same sections and
result limit returns the cached evidence and is marked `cached: true`.

`Generate grounded insights` is for the older evidence-ranking workflow: it
extracts citation-backed claims from preserved retrievals. `Save accepted
papers to project` saves reviewed ranking candidates into the selected project;
it is separate from the project-page parser/RAG flow. The project picker now
makes that destination explicit.

The architecture is:

```text
MCP/source connectors -> retrieval ledger -> project selection
    -> parser blocks -> human review -> NVIDIA embeddings
    -> Neon pgvector -> similarity search -> cached cited passages/notes
```

ChatGPT does not permanently learn the papers. When connected to this MCP
server, it can call the exposed tools, retrieve the selected project's evidence,
and receive the cited passages. The project database and audit history preserve
what was queried and used for later review.

## Free local RAG

The free path runs embeddings on your computer with BGE-small. It does not
need an OpenAI, DeepSeek, or paid embedding key.

Install the optional packages:

```bash
pip install -e ".[pgvector,local-embeddings]"
```

Start local pgvector:

```bash
export POSTGRES_PASSWORD=localtest
docker compose -f docker-compose.rag.yml up -d
```

Enable local embeddings:

```bash
export BIOLAB_LOCAL_EMBEDDINGS=1
export BIOLAB_LOCAL_EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
export BIOLAB_EMBEDDING_DIMENSION=384
export PGVECTOR_DATABASE_URL=postgresql://biolab:localtest@127.0.0.1:54329/biolab
```

Run the vector integration test:

```bash
BIOLAB_PGVECTOR_INTEGRATION=1 \
pytest tests/test_rag_pgvector.py -m integration -v
```

Stop the local database when finished:

```bash
docker compose -f docker-compose.rag.yml down
```

Only blocks marked `accepted` are added to the project index. Search is scoped
to the selected project and can be filtered by sections such as `methods` or
`results`.

### Hosted demo without a paid embedding API

The hosted demo can use `biolab-feature-hash-v1`, a deterministic lexical
baseline that requires no model download or API key. Set
`BIOLAB_FREE_EMBEDDINGS=1` with `PGVECTOR_DATABASE_URL`. This is for workflow
demonstrations, not a validated scientific embedding model. For better local
semantic retrieval, use BGE-small as shown above.

### Baidu Qianfan embeddings

To use Baidu's 384-dimensional `Embedding-V1` model, set the API key in the
deployment environment. The client batches requests in groups of 16 to match
Qianfan's documented limit:

```bash
export BIOLAB_EMBEDDING_PROVIDER=baidu
export BAIDU_QIANFAN_API_KEY="your-qianfan-api-key"
export BAIDU_QIANFAN_EMBEDDING_MODEL=embedding-v1
export BIOLAB_EMBEDDING_DIMENSION=384
```

After switching providers, rebuild the project indexes; vectors from different
embedding models must not be mixed. Keep the key out of Git and browser code.

### NVIDIA hosted embeddings

NVIDIA's free Build endpoint supports `nvidia/nemotron-3.5-embed-1b` for
multilingual text and document retrieval. The hosted model returns normalized
2048-dimensional vectors through an OpenAI-compatible endpoint:

```bash
export BIOLAB_EMBEDDING_PROVIDER=nvidia
export NVIDIA_API_KEY="your-build-nvidia-key"
export NVIDIA_EMBEDDING_MODEL=nvidia/nemotron-3.5-embed-1b
export NVIDIA_EMBEDDING_DIMENSION=2048
```

After switching from another provider, rebuild the project indexes. NVIDIA
lists this as a free endpoint, but access is still subject to its API trial
terms and quotas.

If the NVIDIA key is not configured, the hosted demo uses the deterministic
feature-hash fallback so the complete project workflow remains demonstrable.

## Tests

Run the normal test suite:

```bash
pytest -q
```

Run the RAG-focused tests:

```bash
pytest tests/test_rag.py tests/test_document_parser.py tests/test_inference.py tests/test_web.py -q
```

Run checks used by CI:

```bash
ruff check app.py biolab tests
mypy biolab
```

## Storage and security

Local data is stored in `biolab.db` by default. Hosted and standalone APIs
require authentication by default. Anonymous access is only for an explicit,
isolated demo:

```bash
biolab web --allow-anonymous
```

The retrieval log is hash-chained for tamper evidence. It is not an immutable
ledger and does not replace backups, access control, or scientific review.

Each project also has a separate **Audit project history** action. It verifies
and displays project events such as paper selection, parsing, parser review,
RAG queries, index rebuilds, and saved notes.

To detect truncation or rewriting across a backup, create a signed checkpoint
with a secret that is not stored in the database:

```bash
export BIOLAB_ANCHOR_KEY="$(openssl rand -hex 32)"
biolab anchor retrievals.anchor.json --chain retrievals
biolab verify-anchor retrievals.anchor.json --chain retrievals
```

Keep the anchor file and `BIOLAB_ANCHOR_KEY` in separate protected storage. A
valid chain means the stored records are internally consistent; it does not
prove that a paper is scientifically correct.

For production deployments, configure a durable database and keep API keys in
the deployment environment. Never commit credentials to this repository.
