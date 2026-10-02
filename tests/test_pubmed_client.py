"""Live tests against the real PubMed E-utilities API — no fixtures, by design.

These hit the network every run. That's a deliberate choice (see QUESTIONS_AND_ANSWERS.md):
this project's rule is nothing gets claimed as working unless it's actually running on
real data, and that discipline extends to the test suite, not just manual verification.
"""

import time

import pytest
from biolab.pubmed_client import fetch, search, search_and_fetch

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def _respect_ncbi_rate_limit():
    """NCBI caps unauthenticated callers at 3 req/sec. Running this file's tests back
    to back tripped a real 429 during the first full-suite run — this isn't a retry/
    backoff system, just enough spacing to stay under the real limit."""
    time.sleep(0.4)
    yield


def test_search_returns_pmids_for_a_real_query():
    pmids = search("BRCA1 pancreatic cancer", 3)
    assert len(pmids) == 3
    assert all(pmid.isdigit() for pmid in pmids)


def test_fetch_empty_list_returns_empty_without_a_network_call():
    assert fetch([]) == []


def test_fetch_extracts_all_expected_fields_from_a_real_record():
    papers = fetch(["42431391"])
    assert len(papers) == 1
    paper = papers[0]
    assert paper.pmid == "42431391"
    assert paper.title
    assert paper.abstract
    assert paper.medline_status
    assert paper.pub_status
    assert "42431391" in paper.raw_xml


def test_fetch_does_not_truncate_titles_with_inline_xml_markup():
    """Regression test for a real bug caught this session: ElementTree.findtext()
    only returns text up to the first child element, so PMID 42431391's real title
    — which contains <sup>5</sup> inside "m5C Methylation" — silently truncated to
    "TRDMT1-Mediated mRNA m". Fixed by walking itertext() instead. See
    QUESTIONS_AND_ANSWERS.md for the full writeup.
    """
    papers = fetch(["42431391"])
    title = papers[0].title
    # Title case varies; check for the key phrase without case sensitivity
    assert "trdmt1-mediated mrna m5c methylation" in title.lower()
    assert "chemotherapy sensitivity" in title.lower()
    assert title.endswith(".")


def test_search_and_fetch_end_to_end():
    papers = search_and_fetch("BRCA1 pancreatic cancer", 2)
    assert len(papers) == 2
    for paper in papers:
        assert paper.pmid
        assert paper.title
        assert paper.abstract
        assert paper.raw_xml


def test_burst_of_concurrent_searches_does_not_trigger_a_429():
    """A burst of concurrent search() calls (simulating simultaneous MCP tool
    calls) must not trip NCBI's rate limit — pubmed_client._rate_limiter paces
    requests client-side regardless of how many callers fire at once."""
    import threading
    from urllib.error import HTTPError

    errors: list[Exception] = []

    def do_search(i: int) -> None:
        try:
            search("cancer", 1)
        except HTTPError as e:
            errors.append(e)

    threads = [threading.Thread(target=do_search, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
