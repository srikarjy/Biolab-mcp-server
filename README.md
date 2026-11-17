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

## What you can do

1. Search PubMed and other life-science sources.
2. Create a project and select papers for it.
3. Parse a selected paper into page-aware blocks.
4. Review blocks and index accepted blocks in pgvector.
5. Search the project RAG and save notes with citations.
6. Download the parsed paper JSON.

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
GET  /v1/parser/runs/{parser_run_id}/download
POST /v1/projects/{project_id}/rag/notes
```

The download route returns an attachment such as `paper.parsed.json`. It
contains parsed blocks and metadata. The original PDF is not retained by the
parser endpoint.

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

For production deployments, configure a durable database and keep API keys in
the deployment environment. Never commit credentials to this repository.
