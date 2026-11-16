import pytest
from biolab import embedding_client, rag


class FakeVectorStore:
    indexed: list[dict] = []
    search_args: tuple = ()

    def __init__(self):
        pass

    def upsert_parser_blocks(self, project_id, document_id, parser_run_id, blocks):
        self.indexed.extend(blocks)
        return len(blocks)

    def search_parser(self, project_id, vector, top_k, sections):
        type(self).search_args = (project_id, vector, top_k, sections)
        return [{"project_id": project_id, "section_name": sections[0] if sections else "unknown"}]


def test_project_parser_indexing_is_active_and_stable(monkeypatch):
    FakeVectorStore.indexed = []
    monkeypatch.setattr(rag.embedding_client, "configured", lambda: True)
    monkeypatch.setattr(rag, "PgVectorStore", FakeVectorStore)

    result = rag.index_parser_blocks(
        "project-1", "document-1", "run-1",
        [{"block_id": "block-1", "page_number": 2, "block_type": "paragraph", "section_name": "results", "text": "A result."}],
    )

    assert result == {"status": "active", "indexed_blocks": 1}
    assert FakeVectorStore.indexed[0]["block_id"] == "block-1"
    assert FakeVectorStore.indexed[0]["section_name"] == "results"


def test_project_parser_search_preserves_scope_and_section_filter(monkeypatch):
    monkeypatch.setattr(rag.embedding_client, "configured", lambda: True)
    monkeypatch.setattr(rag.embedding_client, "embed", lambda texts: [[0.1, 0.2] for _ in texts])
    monkeypatch.setattr(rag, "PgVectorStore", FakeVectorStore)

    result = rag.search_parser_context("project-1", "What changed?", 3, ["results"])

    assert result["status"] == "active"
    assert result["chunks"] == [{"project_id": "project-1", "section_name": "results"}]
    assert FakeVectorStore.search_args == ("project-1", [0.1, 0.2], 3, ["results"])


def test_project_parser_search_rejects_empty_section_name(monkeypatch):
    with pytest.raises(ValueError, match="sections"):
        rag.search_parser_context("project-1", "question", sections=["  "])


def test_local_embedding_configuration_uses_small_model_defaults(monkeypatch):
    monkeypatch.setenv("BIOLAB_LOCAL_EMBEDDINGS", "1")
    monkeypatch.delenv("BIOLAB_EMBEDDING_DIMENSION", raising=False)
    monkeypatch.delenv("BIOLAB_LOCAL_EMBEDDING_MODEL", raising=False)
    assert embedding_client.configured() is True
    assert embedding_client.dimension() == 384
    assert embedding_client.model_name() == "BAAI/bge-small-en-v1.5"
