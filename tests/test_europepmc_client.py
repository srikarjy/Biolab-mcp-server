"""Live tests against the real Europe PMC REST API — no fixtures, by design. See
test_pubmed_client.py for why this project hits the network on every run."""

import pytest
from biolab.europepmc_client import paper_to_retrieval_input, search_and_fetch

pytestmark = pytest.mark.live


def test_search_and_fetch_returns_real_articles():
    articles = search_and_fetch("BRCA1 pancreatic cancer", 2)
    assert len(articles) == 2
    for article in articles:
        assert article.id
        assert article.title


def test_paper_to_retrieval_input_shapes_a_valid_record():
    articles = search_and_fetch("BRCA1 pancreatic cancer", 1)
    retrieval_input = paper_to_retrieval_input(articles[0])

    assert retrieval_input["source"] == "europepmc"
    assert retrieval_input["external_id"]
    assert retrieval_input["snapshot"]["title"] == articles[0].title
    assert "medline_status" in retrieval_input["source_metadata"]
    assert "pub_status" in retrieval_input["source_metadata"]
