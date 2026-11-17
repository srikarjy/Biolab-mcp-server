"""OpenAI-compatible embedding client used by the optional pgvector RAG path."""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any
from urllib import request

_local_model: Any = None
_local_model_name: str | None = None


def local_enabled() -> bool:
    return os.environ.get("BIOLAB_LOCAL_EMBEDDINGS", "").lower() in {"1", "true", "yes", "on"}


def free_enabled() -> bool:
    """Enable the dependency-free demo embedding fallback.

    This is a deterministic feature-hashing baseline, not a language model.
    """
    return os.environ.get("BIOLAB_FREE_EMBEDDINGS", "").lower() in {"1", "true", "yes", "on"}


def dimension() -> int:
    if os.environ.get("BIOLAB_EMBEDDING_DIMENSION"):
        return int(os.environ["BIOLAB_EMBEDDING_DIMENSION"])
    return 384 if local_enabled() or free_enabled() else 1536


def model_name() -> str:
    if local_enabled():
        return os.environ.get("BIOLAB_LOCAL_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
    if free_enabled():
        return "biolab-feature-hash-v1"
    return os.environ.get("BIOLAB_EMBEDDING_MODEL", "text-embedding-3-small")

def configured() -> bool:
    return local_enabled() or free_enabled() or bool(os.environ.get("BIOLAB_EMBEDDING_API_KEY") and os.environ.get("BIOLAB_EMBEDDING_API_URL"))


def _free_embed(texts: list[str]) -> list[list[float]]:
    """Return normalized signed feature-hash vectors with no network/model cost."""
    size = dimension()
    vectors: list[list[float]] = []
    for text in texts:
        tokens = re.findall(r"[a-z0-9]+", text.lower())
        features = tokens + [f"{tokens[i]} {tokens[i + 1]}" for i in range(len(tokens) - 1)]
        vector = [0.0] * size
        for feature in features:
            digest = hashlib.sha256(feature.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % size
            vector[index] += 1.0 if digest[4] & 1 else -1.0
        norm = sum(value * value for value in vector) ** 0.5
        vectors.append([value / norm for value in vector] if norm else vector)
    return vectors


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
    if free_enabled():
        return _free_embed(texts)
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
