"""Local streamable-HTTP MCP graph workflow.

This is opt-in because it binds a local socket and starts the real server
process. It is the MCP half of the graph release gate.
"""

import asyncio
import contextlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from biolab import db, workspace
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

pytestmark = pytest.mark.live
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextlib.asynccontextmanager
async def _server(db_path):
    port = _free_port()
    process = subprocess.Popen(
        [sys.executable, "-m", "biolab.server"], cwd=PROJECT_ROOT,
        env={**os.environ, "BIOLAB_DB_PATH": str(db_path), "BIOLAB_HOST": "127.0.0.1", "BIOLAB_PORT": str(port)},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        for _ in range(100):
            try:
                httpx.get(url, timeout=0.5)
                break
            except httpx.ConnectError:
                await asyncio.sleep(0.1)
        else:
            raise RuntimeError("MCP server did not start")
        yield url
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def _seed(db_path):
    conn = db.connect(str(db_path))
    project = workspace.create_project(conn, "MCP graph workflow", owner_id="mcp:user")
    conn.execute(
        """INSERT INTO parser_runs
           (parser_run_id, project_id, filename, parser_model, source_hash, status, block_count, created_by, created_at)
           VALUES ('mcp-run', ?, 'paper.pdf', 'test-parser', 'hash', 'completed', 1, 'mcp:user', '2026-01-01T00:00:00Z')""",
        (project["project_id"],),
    )
    conn.execute(
        """INSERT INTO parsed_blocks
           (block_id, parser_run_id, page_number, block_index, block_type, section_name, text, review_state, content_hash, created_at)
           VALUES ('mcp-block', 'mcp-run', 1, 0, 'paragraph', 'results', 'KRAS activates MAPK.', 'accepted', 'mcp-hash', '2026-01-01T00:00:00Z')""",
    )
    conn.commit()
    conn.close()
    return project["project_id"]


@pytest.mark.asyncio
async def test_mcp_graph_propose_review_and_trace(tmp_path):
    db_path = tmp_path / "mcp-graph.db"
    project_id = _seed(db_path)
    async with _server(db_path) as url, streamable_http_client(url) as (read, write, _), ClientSession(read, write) as session:
        await session.initialize()
        source = [{"block_id": "mcp-block", "parser_run_id": "mcp-run", "page_number": 1, "content_hash": "mcp-hash", "excerpt": "KRAS activates MAPK."}]
        extraction = await session.call_tool("extract_project_graph", {"project_id": project_id, "parser_run_id": "mcp-run"})
        assert extraction.isError is False, extraction.content
        proposal = await session.call_tool("propose_graph_entity", {"project_id": project_id, "node_type": "gene", "canonical_name": "KRAS", "provenance": source})
        assert proposal.isError is False, proposal.content
        payload = json.loads(proposal.content[0].text)
        pending = await session.call_tool("list_graph_proposals", {"project_id": project_id, "status": "pending"})
        assert len(json.loads(pending.content[0].text)["proposals"]) >= 3
        stats = await session.call_tool("get_graph_stats", {"project_id": project_id})
        assert json.loads(stats.content[0].text)["pending_proposals"] >= 3
        trace = await session.call_tool("trace_graph_entity", {"project_id": project_id, "entity_id": payload["node_id"]})
        assert json.loads(trace.content[0].text)["provenance"][0]["block_id"] == "mcp-block"

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT status FROM graph_proposals").fetchone()[0] == "pending"
