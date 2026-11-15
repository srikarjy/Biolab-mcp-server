from biolab import db, document_parser, hf_parser


def test_parser_run_persists_page_aware_blocks_and_flushes(monkeypatch, tmp_path):
    conn = db.connect(str(tmp_path / "parser.db"))
    monkeypatch.setattr(hf_parser, "parse_document", lambda content, filename: {
        "parser_model": "nvidia/Nemotron-3.5-Parse", "status": "succeeded",
        "source_hash": "abc", "block_count": 1,
        "blocks": [{"block_id": "b1", "page_number": 2, "block_index": 0, "block_type": "paragraph", "text": "KRAS evidence", "bbox": [1, 2, 3, 4]}],
    })
    monkeypatch.setattr(document_parser.rag, "index_parser_blocks", lambda parser_run_id, blocks: {"status": "disabled", "indexed_blocks": 0})
    monkeypatch.setattr(document_parser.rag, "flush_parser_context", lambda parser_run_id: 0)
    run = document_parser.create_parser_run(conn, b"pdf", "paper.pdf", "scientist")
    assert run["parser_model"] == "nvidia/Nemotron-3.5-Parse"
    assert document_parser.get_parser_run(conn, run["parser_run_id"])["blocks"][0]["page_number"] == 2
    flushed = document_parser.flush_parser_run(conn, run["parser_run_id"])
    assert flushed["flushed_parsed_blocks"] == 1
    assert document_parser.get_parser_run(conn, run["parser_run_id"])["blocks"] == []
