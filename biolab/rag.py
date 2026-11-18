"""Optional cloud pgvector storage for disposable evidence chunks."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from biolab import embedding_client


def query_key(question: str, top_k: int, sections: list[str] | None = None, mode: str = "search", corpus_fingerprint: str = "", embedding_fingerprint: str | None = None) -> str:
    """Return a stable cache key for one project-scoped RAG operation."""
    payload = {"mode": mode, "question": question.casefold().strip(), "top_k": top_k, "sections": sections or [], "corpus": corpus_fingerprint, "embedding": embedding_fingerprint or embedding_client.fingerprint(), "algorithm": "project-rag-v2"}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def storage_status() -> dict[str, Any]:
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("PGVECTOR_DATABASE_URL")
    configured = bool(dsn)
    connected = False
    schema_ready = False
    if dsn:
        try:
            import psycopg
            with psycopg.connect(_normalize_dsn(dsn)) as conn, conn.cursor() as cur:
                cur.execute("SELECT 1")
                connected = cur.fetchone() == (1,)
                cur.execute("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')")
                schema_ready = bool(cur.fetchone()[0])
        except Exception:
            connected = False
            schema_ready = False
    read_write_verified = connected and schema_ready
    return {
        "backend": "neon-pgvector" if configured else "not-configured",
        "configured": configured,
        "connected": connected,
        "schema_ready": schema_ready,
        "read_write_verified": read_write_verified,
        "durable_claim_allowed": bool(configured and connected and schema_ready),
        "durable": bool(configured and connected and schema_ready),
        "scope": "accepted parser chunks and embeddings",
        "warning": "Configure and verify PGVECTOR_DATABASE_URL for durable vector storage." if not configured or not connected or not schema_ready else "",
    }


def _normalize_dsn(dsn: str | None) -> str | None:
    """Remove connection options unsupported by older local libpq builds."""
    if not dsn or not dsn.startswith(("postgres://", "postgresql://")):
        return dsn
    parsed = urlsplit(dsn)
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "channel_binding"]
    if not any(key == "connect_timeout" for key, _value in query):
        query.append(("connect_timeout", os.environ.get("BIOLAB_PGVECTOR_CONNECT_TIMEOUT_SECONDS", "10")))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


class PgVectorStore:
    """Small pgvector adapter; construction fails clearly when not configured."""

    def __init__(self, dsn: str | None = None, dimension: int | None = None) -> None:
        self.dsn = _normalize_dsn(dsn or os.environ.get("DATABASE_URL") or os.environ.get("PGVECTOR_DATABASE_URL"))
        self.dimension = dimension or embedding_client.dimension()
        self.embedding_model = embedding_client.model_name()
        self.embedding_revision = os.environ.get("BIOLAB_EMBEDDING_REVISION", "unspecified")
        self.embedding_fingerprint = embedding_client.fingerprint()
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
                    chunk_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    document_id TEXT NOT NULL, parser_run_id TEXT NOT NULL,
                    block_id TEXT NOT NULL, page_number INTEGER NOT NULL,
                    section_name TEXT NOT NULL, block_type TEXT NOT NULL, chunk_text TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{{}}',
                    content_hash TEXT NOT NULL, review_state TEXT NOT NULL DEFAULT 'accepted',
                    embedding_model TEXT NOT NULL, embedding_revision TEXT NOT NULL,
                    embedding_fingerprint TEXT NOT NULL DEFAULT '',
                    embedding_dimension INTEGER NOT NULL,
                    embedding vector({self.dimension}) NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )"""
            )
            cur.execute("ALTER TABLE biolab_parser_chunks ADD COLUMN IF NOT EXISTS project_id TEXT")
            cur.execute("ALTER TABLE biolab_parser_chunks ADD COLUMN IF NOT EXISTS document_id TEXT")
            cur.execute("ALTER TABLE biolab_parser_chunks ADD COLUMN IF NOT EXISTS section_name TEXT DEFAULT 'unknown'")
            cur.execute("ALTER TABLE biolab_parser_chunks ADD COLUMN IF NOT EXISTS metadata_json TEXT DEFAULT '{}'")
            cur.execute("ALTER TABLE biolab_parser_chunks ADD COLUMN IF NOT EXISTS content_hash TEXT DEFAULT ''")
            cur.execute("ALTER TABLE biolab_parser_chunks ADD COLUMN IF NOT EXISTS review_state TEXT DEFAULT 'accepted'")
            cur.execute("ALTER TABLE biolab_parser_chunks ADD COLUMN IF NOT EXISTS embedding_model TEXT DEFAULT ''")
            cur.execute("ALTER TABLE biolab_parser_chunks ADD COLUMN IF NOT EXISTS embedding_revision TEXT DEFAULT ''")
            cur.execute("ALTER TABLE biolab_parser_chunks ADD COLUMN IF NOT EXISTS embedding_fingerprint TEXT DEFAULT ''")
            cur.execute("ALTER TABLE biolab_parser_chunks ADD COLUMN IF NOT EXISTS embedding_dimension INTEGER")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_biolab_parser_chunks_project ON biolab_parser_chunks(project_id)")
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

    def upsert_parser_blocks(self, project_id: str, document_id: str, parser_run_id: str, blocks: list[dict[str, Any]]) -> int:
        """Vectorize page-aware parser blocks without mixing them with claims."""
        self.ensure_schema()
        indexed_blocks = [block for block in blocks if str(block.get("text", "")).strip()]
        texts = [str(block["text"]).strip() for block in indexed_blocks]
        if not texts:
            return 0
        vectors = embedding_client.embed(texts)
        if len(vectors) != len(indexed_blocks):
            raise ValueError("embedding provider returned the wrong number of vectors")
        if any(len(vector) != self.dimension for vector in vectors):
            raise ValueError(f"embedding dimension must be {self.dimension}")
        with self.conn.cursor() as cur:
            for block, vector in zip(
                indexed_blocks, vectors, strict=True
            ):
                content_hash = block.get("content_hash") or hashlib.sha256(str(block["text"]).encode()).hexdigest()
                cur.execute(
                    """INSERT INTO biolab_parser_chunks
                       (chunk_id, project_id, document_id, parser_run_id, block_id, page_number,
                       section_name, block_type, chunk_text, metadata_json, content_hash, review_state,
                        embedding_model, embedding_revision, embedding_fingerprint, embedding_dimension, embedding)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector)
                       ON CONFLICT (chunk_id) DO UPDATE SET
                         project_id=excluded.project_id, document_id=excluded.document_id,
                         parser_run_id=excluded.parser_run_id, page_number=excluded.page_number,
                         section_name=excluded.section_name, block_type=excluded.block_type,
                         chunk_text=excluded.chunk_text, metadata_json=excluded.metadata_json, content_hash=excluded.content_hash,
                         review_state=excluded.review_state, embedding_model=excluded.embedding_model,
                         embedding_revision=excluded.embedding_revision,
                         embedding_fingerprint=excluded.embedding_fingerprint,
                         embedding_dimension=excluded.embedding_dimension, embedding=excluded.embedding""",
                    (block["block_id"], project_id, document_id, parser_run_id, block["block_id"],
                     block["page_number"], block.get("section_name", "unknown"), block["block_type"],
                     block["text"], json.dumps(block.get("metadata", {}), sort_keys=True), content_hash, "accepted", self.embedding_model,
                     self.embedding_revision, self.embedding_fingerprint, self.dimension, str(vector)),
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

    def flush_parser_block(self, block_id: str) -> int:
        self.ensure_schema()
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM biolab_parser_chunks WHERE block_id = %s", (block_id,))
            count = cur.rowcount
        self.conn.commit()
        return count

    def search_parser(self, project_id: str, embedding: list[float], top_k: int = 8, sections: list[str] | None = None) -> list[dict[str, Any]]:
        self.ensure_schema()
        with self.conn.cursor() as cur:
            section_filter = ""
            params: list[Any] = [str(embedding), project_id, self.embedding_fingerprint]
            if sections:
                section_filter = " AND section_name = ANY(%s)"
                params.append(sections)
            params.extend([str(embedding), top_k])
            cur.execute(
                f"""SELECT chunk_id, project_id, document_id, parser_run_id, block_id,
                          page_number, section_name, block_type, chunk_text, content_hash,
                          metadata_json,
                          embedding_model, embedding_revision, embedding_fingerprint, embedding_dimension,
                          1 - (embedding <=> %s::vector) AS similarity
                   FROM biolab_parser_chunks
                   WHERE project_id = %s AND embedding_fingerprint = %s AND review_state = 'accepted'{section_filter}
                   ORDER BY embedding <=> %s::vector LIMIT %s""",
                params,
            )
            columns = [column.name for column in (cur.description or [])]
            return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


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


def index_parser_blocks(project_id: str, document_id: str, parser_run_id: str, blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """Index parser output when the optional embedding and pgvector services exist."""
    if not embedding_client.configured():
        return {"status": "disabled", "reason": "embedding provider not configured", "indexed_blocks": 0}
    try:
        count = PgVectorStore().upsert_parser_blocks(project_id, document_id, parser_run_id, blocks)
    except RuntimeError as exc:
        return {"status": "disabled", "reason": str(exc), "indexed_blocks": 0}
    except Exception as exc:
        return {"status": "unavailable", "reason": str(exc), "indexed_blocks": 0}
    return {"status": "active", "indexed_blocks": count}


def flush_parser_context(parser_run_id: str) -> int:
    """Delete parser-run vectors; return zero when pgvector is not configured."""
    if not (os.environ.get("DATABASE_URL") or os.environ.get("PGVECTOR_DATABASE_URL")):
        return 0
    return PgVectorStore().flush_parser_run(parser_run_id)


def flush_parser_block(block_id: str) -> int:
    if not (os.environ.get("DATABASE_URL") or os.environ.get("PGVECTOR_DATABASE_URL")):
        return 0
    return PgVectorStore().flush_parser_block(block_id)


def select_diverse_chunks(chunks: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
    """Remove duplicate passages and prefer one hit per document first."""
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for chunk in chunks:
        key = str(chunk.get("content_hash") or chunk.get("chunk_text", "")).strip().casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(chunk)
    selected: list[dict[str, Any]] = []
    documents: set[str] = set()
    for chunk in unique:
        document_id = str(chunk.get("document_id", ""))
        if document_id in documents:
            continue
        documents.add(document_id)
        selected.append(chunk)
        if len(selected) == top_k:
            return selected
    for chunk in unique:
        if chunk in selected:
            continue
        selected.append(chunk)
        if len(selected) == top_k:
            break
    return selected


def search_parser_context(project_id: str, question: str, top_k: int = 8, sections: list[str] | None = None) -> dict[str, Any]:
    if not question.strip():
        raise ValueError("question must not be empty")
    if top_k < 1 or top_k > 50:
        raise ValueError("top_k must be between 1 and 50")
    if sections is not None and (not sections or any(not section.strip() for section in sections)):
        raise ValueError("sections must contain non-empty names")
    if not embedding_client.configured():
        return {"status": "disabled", "reason": "embedding provider not configured", "chunks": []}
    vector = embedding_client.embed([question])[0]
    candidate_limit = min(50, max(top_k, top_k * 3))
    chunks = select_diverse_chunks(PgVectorStore().search_parser(project_id, vector, candidate_limit, sections), top_k)
    for chunk in chunks:
        chunk["citation"] = {
            "chunk_id": chunk.get("chunk_id"),
            "document_id": chunk.get("document_id"),
            "parser_run_id": chunk.get("parser_run_id"),
            "block_id": chunk.get("block_id"),
            "page_number": chunk.get("page_number"),
            "section_name": chunk.get("section_name"),
            "content_hash": chunk.get("content_hash"),
        }
    return {
        "status": "active", "sections": sections or [],
        "embedding_provider": embedding_client.provider(),
        "embedding_model": embedding_client.model_name(),
        "embedding_dimension": embedding_client.dimension(),
        "embedding_fingerprint": embedding_client.fingerprint(),
        "chunks": chunks,
    }


def answer_parser_context(project_id: str, question: str, top_k: int = 8, sections: list[str] | None = None) -> dict[str, Any]:
    """Return an extractive, citation-linked answer without inventing facts."""
    result = search_parser_context(project_id, question, top_k, sections)
    if result.get("status") != "active":
        return result | {"answer": "", "grounded": False, "citations": []}
    question_terms = set(re.findall(r"[a-z0-9]+", question.lower()))
    candidates: list[tuple[int, dict[str, Any], str]] = []
    for chunk in result.get("chunks", []):
        for raw_sentence in re.split(r"(?<=[.!?])\s+", str(chunk.get("chunk_text", ""))):
            sentence = raw_sentence.strip()
            if not sentence:
                continue
            terms = set(re.findall(r"[a-z0-9]+", sentence.lower()))
            overlap = len(question_terms & terms)
            if overlap:
                candidates.append((overlap, chunk, sentence))
    candidates.sort(key=lambda item: item[0], reverse=True)
    selected: list[tuple[dict[str, Any], str]] = []
    seen: set[str] = set()
    for _score, chunk, sentence in candidates:
        key = sentence.casefold()
        if key in seen:
            continue
        seen.add(key)
        selected.append((chunk, sentence))
        if len(selected) == 3:
            break
    if not selected:
        return result | {
            "answer": "The reviewed papers do not contain enough matching evidence to answer this question.",
            "grounded": False, "citations": [], "answer_spans": [],
        }
    citations = [chunk.get("citation", {}) for chunk, _sentence in selected]
    answer = " ".join(sentence for _chunk, sentence in selected)
    answer_spans = [{"text": sentence, "citation": chunk.get("citation", {})} for chunk, sentence in selected]
    return result | {"answer": answer, "grounded": True, "citations": citations, "answer_spans": answer_spans}
