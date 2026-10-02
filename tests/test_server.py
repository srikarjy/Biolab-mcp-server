"""One live end-to-end test: real MCP streamable-http client/server connection,
real PubMed call, real SQLite file. This is the only test in the suite that
proves the pieces actually work wired together, not just each module in
isolation — see QUESTIONS_AND_ANSWERS.md for why this exists alongside the
fixture-free unit tests.
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
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

pytestmark = pytest.mark.live

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.asynccontextmanager
async def _running_server(db_path: str):
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "biolab.server"],
        cwd=str(PROJECT_ROOT),
        env={
            **os.environ,
            "BIOLAB_DB_PATH": db_path,
            "BIOLAB_HOST": "127.0.0.1",
            "BIOLAB_PORT": str(port),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
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
            raise RuntimeError("server did not start in time")
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


async def _call_tool(db_path: str, tool_name: str, arguments: dict):
    async with (
        _running_server(db_path) as url, streamable_http_client(url) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        return await session.call_tool(tool_name, arguments)


async def _call_search_pubmed(db_path: str, arguments: dict):
    return await _call_tool(db_path, "search_pubmed", arguments)


def _row_count(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    return conn.execute("SELECT count(*) FROM retrievals").fetchone()[0]


@pytest.mark.asyncio
async def test_search_pubmed_live_end_to_end(tmp_path):
    db_path = str(tmp_path / "test.db")
    result = await _call_search_pubmed(
        db_path,
        {"query": "BRCA1 pancreatic cancer", "agent_id": "test:agent", "max_results": 2},
    )

    assert result.isError is False
    payload = json.loads(result.content[0].text)
    assert payload["query_echo"] == "BRCA1 pancreatic cancer"
    assert len(payload["papers"]) == 2
    for paper in payload["papers"]:
        assert paper["pmid"]
        assert paper["retrieval_id"]
        assert paper["title"]
        assert paper["abstract"]

    assert _row_count(db_path) == 2


@pytest.mark.asyncio
async def test_search_pubmed_empty_query_errors_and_writes_nothing(tmp_path):
    db_path = str(tmp_path / "test.db")
    result = await _call_search_pubmed(db_path, {"query": "", "agent_id": "test:agent"})

    assert result.isError is True
    assert _row_count(db_path) == 0


@pytest.mark.asyncio
async def test_search_pubmed_max_results_over_cap_errors_and_writes_nothing(tmp_path):
    db_path = str(tmp_path / "test.db")
    result = await _call_search_pubmed(
        db_path, {"query": "BRCA1", "agent_id": "test:agent", "max_results": 999}
    )

    assert result.isError is True
    assert _row_count(db_path) == 0


@pytest.mark.asyncio
async def test_search_europepmc_live_end_to_end(tmp_path):
    db_path = str(tmp_path / "test.db")
    result = await _call_tool(
        db_path,
        "search_europepmc",
        {"query": "BRCA1 pancreatic cancer", "agent_id": "test:agent", "max_results": 2},
    )

    assert result.isError is False
    payload = json.loads(result.content[0].text)
    assert len(payload["articles"]) == 2
    for article in payload["articles"]:
        assert article["retrieval_id"]
        assert article["title"]

    assert _row_count(db_path) == 2


@pytest.mark.asyncio
async def test_search_clinicaltrials_live_end_to_end(tmp_path):
    db_path = str(tmp_path / "test.db")
    result = await _call_tool(
        db_path,
        "search_clinicaltrials",
        {"query": "pancreatic cancer", "agent_id": "test:agent", "max_results": 2},
    )

    assert result.isError is False
    payload = json.loads(result.content[0].text)
    assert len(payload["studies"]) == 2
    for study in payload["studies"]:
        assert study["nct_id"].startswith("NCT")
        assert study["retrieval_id"]

    assert _row_count(db_path) == 2


@pytest.mark.asyncio
async def test_search_biorxiv_live_end_to_end(tmp_path):
    db_path = str(tmp_path / "test.db")
    result = await _call_tool(
        db_path,
        "search_biorxiv",
        {"category": "neuroscience", "agent_id": "test:agent", "max_results": 2},
    )

    assert result.isError is False
    payload = json.loads(result.content[0].text)
    assert len(payload["preprints"]) == 2
    for preprint in payload["preprints"]:
        assert preprint["retrieval_id"]
        assert preprint["title"]

    assert _row_count(db_path) == 2
