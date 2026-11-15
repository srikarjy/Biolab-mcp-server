"""Optional cloud pgvector storage for disposable evidence chunks."""

from __future__ import annotations

import os
import re
import uuid
from typing import Any

from biolab import embedding_client


class PgVectorStore:
    """Small pgvector adapter; construction fails clearly when not configured."""

    def __init__(self, dsn: str | None = None, dimension: int | None = None) -> None:
        self.dsn = dsn or os.environ.get("DATABASE_URL") or os.environ.get("PGVECTOR_DATABASE_URL")
        self.dimension = dimension or int(os.environ.get("BIOLAB_EMBEDDING_DIMENSION", "1536"))
        if not self.dsn:
            raise RuntimeError("DATABASE_URL or PGVECTOR_DATABASE_URL is not configured")
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("pgvector storage requires: pip install 'biolab-mcp[pgvector]'") from exc
        self.conn = psycopg.connect(self.dsn)

    def ensure_schema(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute(
                f"""CREATE TABLE IF NOT EXISTS biolab_evidence_chunks (
                    chunk_id TEXT PRIMARY KEY, inference_run_id TEXT NOT NULL,
                    retrieval_id TEXT NOT NULL, source TEXT NOT NULL,
                    external_id TEXT NOT NULL, field_name TEXT NOT NULL,
                    chunk_text TEXT NOT NULL, embedding vector({self.dimension}) NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )"""
            )
            cur.execute(
                f"""CREATE TABLE IF NOT EXISTS biolab_parser_chunks (
                    chunk_id TEXT PRIMARY KEY, parser_run_id TEXT NOT NULL,
                    block_id TEXT NOT NULL, page_number INTEGER NOT NULL,
                    block_type TEXT NOT NULL, chunk_text TEXT NOT NULL,
                    embedding vector({self.dimension}) NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )"""
            )
        self.conn.commit()

    def upsert(self, chunks: list[dict[str, Any]]) -> int:
        self.ensure_schema()
        with self.conn.cursor() as cur:
            for chunk in chunks:
                cur.execute(
                    """INSERT INTO biolab_evidence_chunks
                       (chunk_id, inference_run_id, retrieval_id, source, external_id, field_name, chunk_text, embedding)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s::vector)
                       ON CONFLICT (chunk_id) DO UPDATE SET chunk_text=excluded.chunk_text, embedding=excluded.embedding""",
                    (chunk["chunk_id"], chunk["inference_run_id"], chunk["retrieval_id"], chunk["source"], chunk["external_id"], chunk["field_name"], chunk["chunk_text"], str(chunk["embedding"])),
                )
        self.conn.commit()
        return len(chunks)

    def search(self, embedding: list[float], top_k: int = 5) -> list[dict[str, Any]]:
        self.ensure_schema()
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT chunk_id, retrieval_id, source, external_id, field_name, chunk_text,
                          1 - (embedding <=> %s::vector) AS similarity
                   FROM biolab_evidence_chunks ORDER BY embedding <=> %s::vector LIMIT %s""",
                (str(embedding), str(embedding), top_k),
            )
            columns = [column.name for column in (cur.description or [])]
            return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]

    def flush_run(self, inference_run_id: str) -> int:
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM biolab_evidence_chunks WHERE inference_run_id = %s", (inference_run_id,))
            count = cur.rowcount
        self.conn.commit()
        return count

    def upsert_parser_blocks(self, parser_run_id: str, blocks: list[dict[str, Any]]) -> int:
        """Vectorize page-aware parser blocks without mixing them with claims."""
        self.ensure_schema()
        texts = [str(block["text"]).strip() for block in blocks if str(block.get("text", "")).strip()]
        if not texts:
            return 0
        vectors = embedding_client.embed(texts)
        with self.conn.cursor() as cur:
            for block, vector in zip(
                [block for block in blocks if str(block.get("text", "")).strip()], vectors, strict=True
            ):
                cur.execute(
                    """INSERT INTO biolab_parser_chunks
                       (chunk_id, parser_run_id, block_id, page_number, block_type, chunk_text, embedding)
                       VALUES (%s,%s,%s,%s,%s,%s,%s::vector)
                       ON CONFLICT (chunk_id) DO UPDATE SET chunk_text=excluded.chunk_text, embedding=excluded.embedding""",
                    (str(uuid.uuid4()), parser_run_id, block["block_id"], block["page_number"], block["block_type"], block["text"], str(vector)),
                )
        self.conn.commit()
        return len(texts)

    def flush_parser_run(self, parser_run_id: str) -> int:
        self.ensure_schema()
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM biolab_parser_chunks WHERE parser_run_id = %s", (parser_run_id,))
            count = cur.rowcount
        self.conn.commit()
        return count


def retrieve_context(question: str, inference_run_id: str, evidence: list[dict[str, Any]], top_k: int = 8) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Index source sentences and return the pgvector-selected evidence set."""
    if not embedding_client.configured():
        return evidence, {"status": "disabled", "reason": "embedding provider not configured"}
    store = PgVectorStore()
    chunks: list[dict[str, Any]] = []
    for item in evidence:
        for field_name in ("title", "abstract"):
            text = str(item["snapshot"].get(field_name, ""))
            for match in re.finditer(r"[^.!?]+(?:[.!?]+|$)", text):
                chunk_text = match.group(0).strip()
                if not chunk_text:
                    continue
                chunks.append({
                    "chunk_id": str(uuid.uuid4()), "inference_run_id": inference_run_id,
                    "retrieval_id": item["retrieval_id"], "source": item["source"],
                    "external_id": item["external_id"], "field_name": field_name,
                    "chunk_text": chunk_text,
                })
    vectors = embedding_client.embed([chunk["chunk_text"] for chunk in chunks] + [question])
    for chunk, vector in zip(chunks, vectors[:-1], strict=True):
        chunk["embedding"] = vector
    store.upsert(chunks)
    hits = store.search(vectors[-1], top_k)
    selected_ids = {hit["retrieval_id"] for hit in hits}
    selected = [item for item in evidence if item["retrieval_id"] in selected_ids]
    return selected or evidence, {"status": "active", "indexed_chunks": len(chunks), "selected_chunks": len(hits)}


def flush_context(inference_run_id: str) -> int:
    """Delete cloud vector chunks for a run; return zero when pgvector is not configured."""
    if not (os.environ.get("DATABASE_URL") or os.environ.get("PGVECTOR_DATABASE_URL")):
        return 0
    return PgVectorStore().flush_run(inference_run_id)


def index_parser_blocks(parser_run_id: str, blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """Index parser output when the optional embedding and pgvector services exist."""
    if not embedding_client.configured():
        return {"status": "disabled", "reason": "embedding provider not configured", "indexed_blocks": 0}
    try:
        count = PgVectorStore().upsert_parser_blocks(parser_run_id, blocks)
    except RuntimeError as exc:
        return {"status": "disabled", "reason": str(exc), "indexed_blocks": 0}
    return {"status": "active", "indexed_blocks": count}


def flush_parser_context(parser_run_id: str) -> int:
    """Delete parser-run vectors; return zero when pgvector is not configured."""
    if not (os.environ.get("DATABASE_URL") or os.environ.get("PGVECTOR_DATABASE_URL")):
        return 0
    return PgVectorStore().flush_parser_run(parser_run_id)
