# Biolab MCP Server Onboarding Guide

## Overview

Biolab is a local-first scientific evidence workspace. It exposes audited
retrieval and workflow operations through a Python CLI, MCP server, REST API,
local dashboard, and a parallel Go client/server. Retrievals preserve source
responses and metadata in SQLite or optional Turso/libSQL storage, then connect
projects, reviewed collections, watches, claims, and reports to immutable
retrieval IDs.

The system records evidence and explicit human/agent assessments; it does not
rank papers as true, validate biological conclusions, or provide clinical
advice.

## Tech Stack

| Layer | Technology |
| --- | --- |
| Primary language | Python 3.11+ |
| Python packaging | setuptools / `pyproject.toml` |
| MCP | `mcp==1.28.1`, FastMCP |
| HTTP/API | Starlette, Uvicorn |
| CLI | Typer, Rich |
| Storage | SQLite by default; optional Turso/libSQL |
| Testing | pytest, pytest-asyncio |
| Quality | Ruff, mypy |
| Secondary implementation | Go 1.25, Cobra, mcp-go, sqlx, modernc SQLite |
| Delivery | GitHub Actions, PyPI, Docker/Space, GoReleaser |

## Architecture

```text
scientist / agent
       |
  CLI | MCP | REST | dashboard
       |
 evidence.py + workspace.py + executions.py
       |              |
 connector clients   projects / collections / watches / claims / reports
       |
 public scientific APIs
       |
 retrieval_log.py and execution chain
       |
 SQLite/libSQL audit database + artifact store
```

Connectors parse external services into source-neutral retrieval inputs. They do
not write the database. `evidence.py` coordinates searches and preserves both
successful records and explicit per-source errors. `retrieval_log.py` is the
single retrieval write boundary and maintains the retrieval hash chain.
`workspace.py` stores scientist workflow state by reference to retrieval IDs.
`executions.py` provides versioned tool execution records, output hashes,
replay, and a separate execution hash chain.

## Key Entry Points

- `biolab/server.py` — FastMCP tools plus the hosted ASGI app at `/mcp`, REST
  routes, dashboard routes, and A2A JSON-RPC at `/a2a`.
- `biolab/a2a.py` — A2A Agent Card, synchronous task adapter, and JSON-RPC
  dispatch.
- `biolab/cli.py` — `biolab` Typer commands for evidence, workspace, keys,
  executions, demos, exports, and integrity verification.
- `biolab/evidence.py` — connector registry and audited multi-source search.
- `biolab/retrieval_log.py` — append-only retrieval persistence and chain
  verification.
- `biolab/workspace.py` — projects, collections, watches, claims, and reports.
- `biolab/executions.py` — deterministic tool execution records and artifacts.
- `biolab/db.py` — schema creation, SQLite/Turso target selection, and upgrades.
- `biolab/web.py` — standalone Starlette dashboard and REST API.
- `biolab/tools.py` — built-in execution tools, including sequence statistics
  and optional mmseqs2 discovery.
- `go-biolab/cmd/` and `go-biolab/internal/` — parallel Go CLI/server and
  retrieval implementation.

## Request Lifecycle

For an MCP `search_evidence` call:

1. FastMCP dispatches to `biolab.server.search_evidence`.
2. The caller identity is bound through `auth.bind_agent_id`; a self-declared
   identity is not trusted when authenticated request context is available.
3. `evidence.search_evidence` selects the connector registry entries and calls
   each source client.
4. Each connector converts source data into a normalized retrieval input.
5. `retrieval_log.write_retrieval` stores the raw response, snapshot, metadata,
   response hash, and previous-chain hash in SQLite/libSQL.
6. The response returns retrieval IDs, source results, and explicit errors.

The web API uses the same shared services. `biolab web` creates a standalone
Starlette app; `python -m biolab.server` creates the unified MCP/REST/dashboard
process.

## Directory Map

- `biolab/` — Python package and all shared services.
- `biolab/migrations/` — one-shot schema/migration utilities.
- `tests/` — Python unit, integration, fault-injection, CLI, web, and connector
  tests; network/socket tests are marked `live`.
- `go-biolab/` — Go implementation and its tests.
- `scripts/` — scientist workflow and release helpers.
- `docs/` — scientist-facing workflow documentation.
- `integrations/` — BioClaw, Codex, and OpenAI integration packages.
- `plugins/` — plugin manifests and packaged integration assets.
- `deploy/gateway/` — gateway deployment template.
- `space/` — container/deployment metadata for hosted deployment.
- `.github/workflows/` — Python/Go CI and PyPI publishing.

## Conventions

- Python modules and functions use `snake_case`; classes use `PascalCase`.
- Shared service modules receive an explicit database connection rather than
  opening hidden connections inside business logic.
- Retrievals and executions are append-only and hash chained.
- Source clients parse data; persistence belongs to the shared service layer.
- Errors and partial source failures are returned explicitly.
- Tests use `pytest`; network and socket-dependent cases are marked `live`.
- Ruff is configured for Python 3.11, 100-column lines, and import sorting.
- Recent commits use short imperative/conventional prefixes such as `feat:` and
  `fix:`.

## Common Tasks

```bash
# Install
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Run the deterministic Python suite
pytest -m "not live and not benchmark" -v

# Run quality checks
ruff check .
mypy biolab/

# Use the CLI
biolab demo --query "BRCA1 pancreatic cancer"
biolab verify --db biolab.db

# Run local servers
biolab web
python -m biolab.server

# Run Go checks from its module directory
cd go-biolab && go test ./...
```

## Where to Look

| I want to... | Look at... |
| --- | --- |
| Add a scientific source | `biolab/*_client.py`, then register it in `biolab/evidence.py` |
| Add an MCP tool | `biolab/server.py` |
| Add a CLI command | `biolab/cli.py` |
| Add a REST endpoint | `biolab/web.py` |
| Change persistence/schema | `biolab/db.py` and `biolab/retrieval_log.py` |
| Add scientist workflow state | `biolab/workspace.py` and schema in `biolab/db.py` |
| Add an executable tool | `biolab/tools.py` and `biolab/executions.py` |
| Add a Python test | `tests/test_<module>.py` |
| Add a Go feature | `go-biolab/internal/` with a corresponding Go test |
| Change release behavior | `.github/workflows/`, `Dockerfile`, `space/`, `.goreleaser.yml` |
