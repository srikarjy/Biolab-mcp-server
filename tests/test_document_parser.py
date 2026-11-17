import pytest
from biolab import db, document_parser, hf_parser, rag


def test_section_classifier_keeps_scientific_sections_and_noise_explicit():
    assert document_parser.classify_section({"block_type": "heading", "text": "Results"}) == "results"
    assert document_parser.classify_section({"block_type": "table", "text": "Table 1"}) == "table"
    assert document_parser.classify_section({"block_type": "paragraph", "text": "A measured result."}) == "unknown"
    blocks = document_parser.assign_sections([
        {"block_type": "heading", "text": "Methods"},
        {"block_type": "paragraph", "text": "Participants were enrolled."},
        {"block_type": "heading", "text": "Results"},
        {"block_type": "paragraph", "text": "The endpoint improved."},
    ])
    assert [block["section_name"] for block in blocks] == ["methods", "methods", "results", "results"]


def test_local_text_parser_works_without_hugging_face_credentials():
    parsed = hf_parser.parse_document(b"Methods were recorded. Results improved.", "paper.txt")
    assert parsed["parser_model"] == "pdftotext-local"
    assert parsed["status"] == "succeeded-local"
    assert parsed["block_count"] == 2


def test_local_chunks_are_page_aware_bounded_and_deterministic():
    content = "Methods were recorded.\n\nResults improved." + " long" * 500 + "\fDiscussion is separate."

    first = hf_parser.fallback_text_blocks(content)
    second = hf_parser.fallback_text_blocks(content)

    assert first == second
    assert [block["page_number"] for block in first[:-1]] == [1, 1, 1, 1]
    assert first[-1]["page_number"] == 2
    assert all(len(block["text"]) <= hf_parser.MAX_CHUNK_CHARACTERS for block in first)
    assert all(block["block_id"].startswith("chunk-") for block in first)
    assert first[-1]["text"] == "Discussion is separate."


def test_normalized_parser_blocks_have_stable_ids():
    raw = {"pages": [{"page": 3, "blocks": [{"type": "paragraph", "text": "  Results  improved.  "}]}]}

    first = hf_parser._normalize(raw)
    second = hf_parser._normalize(raw)

    assert first == second
    assert first[0]["text"] == "Results improved."
    assert first[0]["block_id"].startswith("chunk-")


def test_parser_run_persists_page_aware_blocks_and_flushes(monkeypatch, tmp_path):
    conn = db.connect(str(tmp_path / "parser.db"))
    monkeypatch.setattr(hf_parser, "parse_document", lambda content, filename: {
        "parser_model": "nvidia/Nemotron-3.5-Parse", "status": "succeeded",
        "source_hash": "abc", "block_count": 1,
        "blocks": [{"block_id": "b1", "page_number": 2, "block_index": 0, "block_type": "paragraph", "text": "KRAS evidence", "bbox": [1, 2, 3, 4]}],
    })
    monkeypatch.setattr(document_parser.rag, "index_parser_blocks", lambda project_id, document_id, parser_run_id, blocks: {"status": "disabled", "indexed_blocks": 0})
    monkeypatch.setattr(document_parser.rag, "flush_parser_context", lambda parser_run_id: 0)
    run = document_parser.create_parser_run(conn, b"pdf", "paper.pdf", "scientist")
    assert run["parser_model"] == "nvidia/Nemotron-3.5-Parse"
    assert document_parser.get_parser_run(conn, run["parser_run_id"])["blocks"][0]["page_number"] == 2
    flushed = document_parser.flush_parser_run(conn, run["parser_run_id"])
    assert flushed["preserved_parsed_blocks"] == 1
    assert document_parser.get_parser_run(conn, run["parser_run_id"])["blocks"][0]["text"] == "KRAS evidence"


def test_accepted_unassigned_block_is_not_written_to_project_rag(monkeypatch, tmp_path):
    conn = db.connect(str(tmp_path / "unassigned.db"))
    monkeypatch.setattr(hf_parser, "parse_document", lambda content, filename: {
        "parser_model": "test-parser", "status": "succeeded", "source_hash": "abc", "block_count": 1,
        "blocks": [{"block_id": "b1", "page_number": 1, "block_index": 0, "block_type": "paragraph", "text": "Evidence", "bbox": None}],
    })
    def fail_if_indexed(*args, **kwargs):
        raise AssertionError("unassigned parser output must not enter project RAG")
    monkeypatch.setattr(document_parser.rag, "index_parser_blocks", fail_if_indexed)
    run = document_parser.create_parser_run(conn, b"pdf", "paper.pdf", "scientist")

    result = document_parser.review_block(conn, run["parser_run_id"], "b1", "accepted", "scientist")

    assert result["vector_status"] == {
        "status": "not_indexed", "reason": "project required for project-scoped RAG", "indexed_blocks": 0,
    }


def test_search_parser_context_rejects_invalid_limits(monkeypatch):
    monkeypatch.setattr(rag.embedding_client, "configured", lambda: False)
    with pytest.raises(ValueError, match="top_k"):
        rag.search_parser_context("project", "question", 0)
    with pytest.raises(ValueError, match="sections"):
        rag.search_parser_context("project", "question", 8, [""])


def test_rebuild_indexes_only_accepted_blocks(monkeypatch, tmp_path):
    conn = db.connect(str(tmp_path / "rebuild.db"))
    project = document_parser.workspace.create_project(conn, "Review", owner_id="scientist")
    conn.execute(
        "INSERT INTO parser_runs (parser_run_id, project_id, document_id, filename, parser_model, source_hash, status, block_count, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("run-1", project["project_id"], "doc-1", "paper.pdf", "test", "source", "succeeded", 2, "scientist", "now"),
    )
    for block_id, state in (("accepted", "accepted"), ("excluded", "excluded")):
        conn.execute(
            "INSERT INTO parsed_blocks (block_id, parser_run_id, page_number, block_index, block_type, section_name, text, bbox, content_hash, review_state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (block_id, "run-1", 1, 0, "paragraph", "results", block_id, "null", block_id + "-hash", state, "now"),
        )
    conn.commit()
    captured = {}
    monkeypatch.setattr(
        document_parser.rag,
        "index_parser_blocks",
        lambda *args: captured.update(args=args) or {"status": "active", "indexed_blocks": 1},
    )

    result = document_parser.rebuild_parser_index(conn, "run-1", "scientist")

    assert result == {"status": "active", "indexed_blocks": 1, "accepted_blocks": 1}
    assert [block["block_id"] for block in captured["args"][3]] == ["accepted"]
