import os

import pytest
from biolab import embedding_client, rag

pytestmark = pytest.mark.integration


def test_pgvector_parser_index_and_project_scoped_search(monkeypatch):
    if os.environ.get("BIOLAB_PGVECTOR_INTEGRATION") != "1":
        pytest.skip("set BIOLAB_PGVECTOR_INTEGRATION=1 to run the local pgvector check")

    dsn = os.environ.get("PGVECTOR_DATABASE_URL")
    if not dsn:
        pytest.skip("PGVECTOR_DATABASE_URL is required")

    monkeypatch.setenv("BIOLAB_EMBEDDING_DIMENSION", "3")
    monkeypatch.setattr(embedding_client, "configured", lambda: True)
    monkeypatch.setattr(
        embedding_client,
        "embed",
        lambda texts: [[1.0, 0.0, 0.0] if "result" in text.lower() else [0.0, 1.0, 0.0] for text in texts],
    )

    store = rag.PgVectorStore(dsn=dsn, dimension=3)
    store.flush_parser_run("integration-run")
    store.upsert_parser_blocks(
        "project-a", "document-a", "integration-run",
        [{"block_id": "integration-block", "page_number": 2, "block_type": "paragraph", "section_name": "results", "text": "A result."}],
    )
    store.upsert_parser_blocks(
        "project-b", "document-b", "other-run",
        [{"block_id": "other-block", "page_number": 1, "block_type": "paragraph", "section_name": "methods", "text": "A result."}],
    )

    hits = store.search_parser("project-a", [1.0, 0.0, 0.0], top_k=5, sections=["results"])

    assert [hit["block_id"] for hit in hits] == ["integration-block"]
    assert hits[0]["project_id"] == "project-a"
    assert hits[0]["section_name"] == "results"
    store.flush_parser_run("integration-run")
    store.flush_parser_run("other-run")
    store.conn.close()
