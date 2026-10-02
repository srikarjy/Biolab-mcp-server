"""Thin wrapper over the Europe PMC REST API. No logging, no DB access — see retrieval_log.py."""

import hashlib
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

import defusedxml.ElementTree as SafeET  # parses untrusted network XML; guards against entity-expansion ("billion laughs") attacks

SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
FETCH_URL_TEMPLATE = "https://www.ebi.ac.uk/europepmc/webservices/rest/{id}/fullTextXML"
TIMEOUT_SECONDS = 15
RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.0


def _urlopen_with_retry(url: str):
    """Retry transient Europe PMC timeouts and 429/5xx service responses."""
    for attempt in range(RETRIES + 1):
        try:
            return urlopen(url, timeout=TIMEOUT_SECONDS)
        except (TimeoutError, URLError, HTTPError) as exc:
            retryable = not isinstance(exc, HTTPError) or exc.code == 429 or exc.code >= 500
            if not retryable or attempt == RETRIES:
                raise
            time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    raise RuntimeError("unreachable")


@dataclass
class EuropePMCArticle:
    id: str
    source: str
    pmid: str
    doi: str
    title: str
    author_string: str
    journal_title: str
    journal_iso_abbr: str
    issn: str
    pub_year: str
    pub_type: str
    abstract_text: str
    full_text_xml: str  # empty when the article has no open full text — best effort


def search(query: str, max_results: int) -> list:
    """Europe PMC search: query string -> list of <result> elements."""
    params = urlencode({
        "query": query,
        "format": "xml",
        "pageSize": max_results,
        "resultType": "lite",
    })
    with _urlopen_with_retry(f"{SEARCH_URL}?{params}") as resp:
        root = SafeET.fromstring(resp.read())
    return root.findall(".//resultList/result")


def fetch_full_text(article_id: str) -> str:
    """Best-effort full-text fetch. Most Europe PMC records don't have open full text,
    so a fetch failure here is expected, not an error worth propagating."""
    params = urlencode({"format": "xml"})
    url = FETCH_URL_TEMPLATE.format(id=article_id) + f"?{params}"
    try:
        with urlopen(url, timeout=TIMEOUT_SECONDS) as resp:
            return resp.read().decode("utf-8")
    except Exception:
        return ""


def search_and_fetch(query: str, max_results: int) -> list[EuropePMCArticle]:
    results = search(query, max_results)
    articles = []
    for result in results:
        article_id = result.findtext("id") or ""
        articles.append(EuropePMCArticle(
            id=article_id,
            source=result.findtext("source") or "",
            pmid=result.findtext("pmid") or "",
            doi=result.findtext("doi") or "",
            title=result.findtext("title") or "",
            author_string=result.findtext("authorString") or "",
            journal_title=result.findtext("journalTitle") or "",
            journal_iso_abbr=result.findtext("journalISOAbbr") or "",
            issn=result.findtext("issn") or "",
            pub_year=result.findtext("pubYear") or "",
            pub_type=result.findtext("pubType") or "",
            abstract_text=result.findtext("abstractText") or "",
            full_text_xml=fetch_full_text(article_id),
        ))
    return articles


def _parse_authors(author_string: str) -> list[dict]:
    """Author string format: "Smith J, Jones K, Brown L"."""
    if not author_string:
        return []
    authors = []
    for raw_part in author_string.split(", "):
        part = raw_part.strip()
        if not part:
            continue
        fields = part.split()
        if len(fields) >= 2:
            authors.append({"lastname": fields[0], "initials": " ".join(fields[1:])})
        else:
            authors.append({"lastname": part})
    return authors


def _parse_pub_types(pub_type: str) -> list[str]:
    """Europe PMC mixes ';' and ',' as separators within the same field."""
    if not pub_type:
        return []
    result = []
    for chunk in pub_type.split(";"):
        for raw_sub in chunk.split(","):
            sub = raw_sub.strip()
            if sub:
                result.append(sub)
    return result


def paper_to_retrieval_input(article: EuropePMCArticle) -> dict:
    """Convert EuropePMCArticle to the input format expected by retrieval_log.write_retrieval."""
    external_id = article.pmid or article.doi or article.id

    snapshot = {
        "title": article.title,
        "abstract": article.abstract_text,
        "authors": _parse_authors(article.author_string),
        "journal": {
            "title": article.journal_title,
            "iso_abbreviation": article.journal_iso_abbr,
            "issn": article.issn,
            "pub_date": article.pub_year,
        },
        "publication_types": _parse_pub_types(article.pub_type),
        "doi": article.doi,
    }

    # Europe PMC has no PubMed-style pub_status field; a hash of the raw response
    # stands in, so the source_metadata shape stays consistent across sources.
    full_text_hash = hashlib.sha256(article.full_text_xml.encode("utf-8")).hexdigest()
    source_metadata = {
        "medline_status": article.source,
        "pub_status": full_text_hash[:32],
    }

    return {
        "source": "europepmc",
        "external_id": external_id,
        "source_metadata": source_metadata,
        "raw_response": article.full_text_xml,
        "snapshot": snapshot,
    }
