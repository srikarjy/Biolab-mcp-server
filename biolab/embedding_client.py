"""OpenAI-compatible embedding client used by the optional pgvector RAG path."""

from __future__ import annotations

import json
import os
from typing import Any
from urllib import request

_local_model: Any = None
_local_model_name: str | None = None


def local_enabled() -> bool:
    return os.environ.get("BIOLAB_LOCAL_EMBEDDINGS", "").lower() in {"1", "true", "yes", "on"}


def dimension() -> int:
    if os.environ.get("BIOLAB_EMBEDDING_DIMENSION"):
        return int(os.environ["BIOLAB_EMBEDDING_DIMENSION"])
    return 384 if local_enabled() else 1536


def model_name() -> str:
    return os.environ.get("BIOLAB_LOCAL_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5") if local_enabled() else os.environ.get("BIOLAB_EMBEDDING_MODEL", "text-embedding-3-small")

def configured() -> bool:
    return local_enabled() or bool(os.environ.get("BIOLAB_EMBEDDING_API_KEY") and os.environ.get("BIOLAB_EMBEDDING_API_URL"))


def _local_embed(texts: list[str]) -> list[list[float]]:
    global _local_model, _local_model_name
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise RuntimeError("local embeddings require: pip install 'biolab-mcp[local-embeddings]'") from exc
    name = model_name()
    if _local_model is None or _local_model_name != name:
        _local_model = SentenceTransformer(name, device=os.environ.get("BIOLAB_LOCAL_EMBEDDING_DEVICE", "cpu"))
        _local_model_name = name
    vectors = _local_model.encode(texts, normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False)
    return vectors.tolist()


def embed(texts: list[str]) -> list[list[float]]:
    if local_enabled():
        return _local_embed(texts)
    if not configured():
        raise RuntimeError("configure local embeddings or BIOLAB_EMBEDDING_API_KEY and BIOLAB_EMBEDDING_API_URL for pgvector RAG")
    payload = json.dumps({"model": os.environ.get("BIOLAB_EMBEDDING_MODEL", "text-embedding-3-small"), "input": texts}).encode()
    req = request.Request(
        os.environ["BIOLAB_EMBEDDING_API_URL"], data=payload,
        headers={"Authorization": f"Bearer {os.environ['BIOLAB_EMBEDDING_API_KEY']}", "Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=float(os.environ.get("BIOLAB_EMBEDDING_TIMEOUT_SECONDS", "30"))) as response:
        result = json.loads(response.read().decode())
    return [item["embedding"] for item in sorted(result["data"], key=lambda item: item["index"])]
