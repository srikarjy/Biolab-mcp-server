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
EMBEDDING_SERIALIZATION_VERSION = "embedding-text-v1"


def baidu_enabled() -> bool:
    return bool(os.environ.get("BAIDU_QIANFAN_API_KEY")) or os.environ.get("BIOLAB_EMBEDDING_PROVIDER", "").lower() == "baidu"


def nvidia_enabled() -> bool:
    return bool(os.environ.get("NVIDIA_API_KEY")) or os.environ.get("BIOLAB_EMBEDDING_PROVIDER", "").lower() == "nvidia"


def local_enabled() -> bool:
    return os.environ.get("BIOLAB_LOCAL_EMBEDDINGS", "").lower() in {"1", "true", "yes", "on"}


def free_enabled() -> bool:
    """Enable the dependency-free demo embedding fallback.

    This is a deterministic feature-hashing baseline, not a language model.
    """
    return os.environ.get("BIOLAB_FREE_EMBEDDINGS", "").lower() in {"1", "true", "yes", "on"}


def provider() -> str:
    """Resolve one embedding provider with explicit configuration taking priority."""
    explicit = os.environ.get("BIOLAB_EMBEDDING_PROVIDER", "").strip().lower()
    if explicit in {"nvidia", "baidu", "local", "free", "custom"}:
        return explicit
    for name, enabled in (
        ("local", local_enabled()), ("free", free_enabled()),
        ("baidu", baidu_enabled()), ("nvidia", nvidia_enabled()),
    ):
        if enabled:
            return name
    custom = bool(os.environ.get("BIOLAB_EMBEDDING_API_KEY") and os.environ.get("BIOLAB_EMBEDDING_API_URL"))
    return "custom" if custom else "none"


def dimension() -> int:
    if os.environ.get("BIOLAB_EMBEDDING_DIMENSION"):
        return int(os.environ["BIOLAB_EMBEDDING_DIMENSION"])
    if provider() in {"local", "free", "baidu"}:
        return 384
    if provider() == "nvidia":
        return int(os.environ.get("NVIDIA_EMBEDDING_DIMENSION", "2048"))
    return 1536


def model_name() -> str:
    selected = provider()
    if selected == "local":
        return os.environ.get("BIOLAB_LOCAL_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
    if selected == "free":
        return "biolab-feature-hash-v1"
    if selected == "baidu":
        return os.environ.get("BAIDU_QIANFAN_EMBEDDING_MODEL", "embedding-v1")
    if selected == "nvidia":
        return os.environ.get("NVIDIA_EMBEDDING_MODEL", "nvidia/nemotron-3.5-embed-1b")
    return os.environ.get("BIOLAB_EMBEDDING_MODEL", "text-embedding-3-small")


def fingerprint() -> str:
    """Identify the exact vector contract used for indexing and querying."""
    payload = {
        "provider": provider(),
        "model": model_name(),
        "revision": os.environ.get("BIOLAB_EMBEDDING_REVISION", "unspecified"),
        "dimension": dimension(),
        "normalization": "l2",
        "serialization": EMBEDDING_SERIALIZATION_VERSION,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def configured() -> bool:
    return provider() != "none"


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
    if not texts:
        return []
    selected = provider()
    if selected == "local":
        return _local_embed(texts)
    if selected == "free":
        return _free_embed(texts)
    if not configured():
        raise RuntimeError("configure local embeddings or BIOLAB_EMBEDDING_API_KEY and BIOLAB_EMBEDDING_API_URL for pgvector RAG")
    default_endpoint = "https://qianfan.baidubce.com/v2/embeddings" if selected == "baidu" else "https://integrate.api.nvidia.com/v1/embeddings" if selected == "nvidia" else ""
    endpoint = os.environ.get("BIOLAB_EMBEDDING_API_URL", default_endpoint)
    if selected == "baidu":
        api_key = os.environ.get("BAIDU_QIANFAN_API_KEY", "")
    elif selected == "nvidia":
        api_key = os.environ.get("NVIDIA_API_KEY", "")
    else:
        api_key = os.environ["BIOLAB_EMBEDDING_API_KEY"]
    model = model_name()
    batch_size = 16 if selected == "baidu" else len(texts)
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start:start + batch_size]
        payload = json.dumps({"model": model, "input": batch}).encode()
        req = request.Request(
            endpoint, data=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(req, timeout=float(os.environ.get("BIOLAB_EMBEDDING_TIMEOUT_SECONDS", "30"))) as response:
            result = json.loads(response.read().decode())
        vectors.extend(item["embedding"] for item in sorted(result["data"], key=lambda item: item["index"]))
    if len(vectors) != len(texts):
        raise ValueError("embedding provider returned the wrong number of vectors")
    if any(len(vector) != dimension() for vector in vectors):
        raise ValueError(f"embedding provider returned vectors with dimension {dimension()}")
    return vectors
