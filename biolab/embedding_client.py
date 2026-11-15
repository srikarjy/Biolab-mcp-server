"""OpenAI-compatible embedding client used by the optional pgvector RAG path."""

from __future__ import annotations

import json
import os
from urllib import request


def configured() -> bool:
    return bool(os.environ.get("BIOLAB_EMBEDDING_API_KEY") and os.environ.get("BIOLAB_EMBEDDING_API_URL"))


def embed(texts: list[str]) -> list[list[float]]:
    if not configured():
        raise RuntimeError("BIOLAB_EMBEDDING_API_KEY and BIOLAB_EMBEDDING_API_URL are required for pgvector RAG")
    payload = json.dumps({"model": os.environ.get("BIOLAB_EMBEDDING_MODEL", "text-embedding-3-small"), "input": texts}).encode()
    req = request.Request(
        os.environ["BIOLAB_EMBEDDING_API_URL"], data=payload,
        headers={"Authorization": f"Bearer {os.environ['BIOLAB_EMBEDDING_API_KEY']}", "Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=float(os.environ.get("BIOLAB_EMBEDDING_TIMEOUT_SECONDS", "30"))) as response:
        result = json.loads(response.read().decode())
    return [item["embedding"] for item in sorted(result["data"], key=lambda item: item["index"])]
