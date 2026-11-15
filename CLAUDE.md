# Project Instructions

## Tech Stack

- Python 3.11+, FastMCP/MCP 1.28.1, Starlette, Uvicorn, Typer, Rich.
- SQLite by default; optional Turso/libSQL through the `turso` extra.
- Go 1.25 parallel client/server under `go-biolab/`.

## Code Style

- Use `snake_case` for Python files/functions and `PascalCase` for classes.
- Keep connectors focused on parsing and normalization; shared services own
  persistence and workflow behavior.
- Pass database connections explicitly into service functions.
- Preserve append-only retrieval/execution records, hashes, raw responses,
  snapshots, timestamps, caller identity, and explicit error states.
- Do not claim scientific or clinical validation from retrieval or model
  execution plumbing alone.

## Testing

```bash
pytest -m "not live and not benchmark" -v
ruff check .
mypy biolab/
```

Network/socket tests are marked `live`; benchmarks are marked `benchmark` and
are excluded from normal CI. Go checks run from `go-biolab/` with `go test
./...`.

## Build and Run

```bash
pip install -e ".[dev]"
biolab demo --query "BRCA1 pancreatic cancer"
biolab web
python -m biolab.server
```

The default local database is `biolab.db`. Remote storage requires the optional
Turso dependency and `TURSO_DATABASE_URL`/`TURSO_AUTH_TOKEN`.

## Project Structure

- `biolab/server.py` — MCP and unified hosted app.
- `biolab/cli.py` — CLI.
- `biolab/evidence.py` and `biolab/*_client.py` — audited source retrieval.
- `biolab/retrieval_log.py` — retrieval hash chain.
- `biolab/executions.py` and `biolab/tools.py` — execution provenance.
- `biolab/workspace.py` — projects, collections, watches, claims, reports.
- `biolab/web.py` — dashboard and REST API.
- `tests/` — Python tests; `go-biolab/` — Go implementation.

See `ONBOARDING.md` for the architecture map and `TODO.md` for the current
backlog and verification baseline.
