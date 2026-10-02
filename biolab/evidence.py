"""Shared evidence-search service used by every delivery surface.

The CLI, MCP server, future REST API, and external clients should call this
module instead of reimplementing connector orchestration and audit logging.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from biolab import (
    clinicaltrials_client,
    europepmc_client,
    opentargets_client,
    pubmed_client,
    retrieval_log,
    uniprot_client,
)

MAX_RESULTS_CAP = 50

@dataclass(frozen=True)
class Connector:
    """A source adapter with a search function and audit-input converter."""

    module: Any
    search_name: str
    converter_name: str

    def search(self, query: str, limit: int) -> list[Any]:
        return getattr(self.module, self.search_name)(query, limit)

    def to_retrieval_input(self, item: Any) -> dict:
        return getattr(self.module, self.converter_name)(item)


CONNECTORS = {
    # Module attributes are resolved at call time so deployments can wrap a
    # connector for caching, retries, or instrumentation without rebuilding it.
    "pubmed": Connector(pubmed_client, "search_and_fetch", "paper_to_retrieval_input"),
    "europepmc": Connector(europepmc_client, "search_and_fetch", "paper_to_retrieval_input"),
    "clinicaltrials": Connector(
        clinicaltrials_client, "search_and_fetch", "paper_to_retrieval_input"
    ),
    "uniprot": Connector(uniprot_client, "search_and_fetch", "entry_to_retrieval_input"),
    "opentargets": Connector(opentargets_client, "search", "hit_to_retrieval_input"),
}
SUPPORTED_SOURCES = tuple(CONNECTORS)


def _validate(query: str, agent_id: str, max_results: int) -> None:
    if not query.strip():
        raise ValueError("query must not be empty")
    if not agent_id.strip():
        raise ValueError("agent_id must not be empty")
    if not 1 <= max_results <= MAX_RESULTS_CAP:
        raise ValueError(f"max_results must be between 1 and {MAX_RESULTS_CAP}")


def _store_results(
    conn: Any,
    query: str,
    agent_id: str,
    items: list[Any],
    to_retrieval_input: Callable[[Any], dict],
) -> list[dict]:
    results = []
    for item in items:
        retrieval_input = to_retrieval_input(item)
        record = retrieval_log.write_retrieval(
            conn,
            query_text=query,
            external_id=retrieval_input["external_id"],
            agent_id=agent_id,
            source=retrieval_input["source"],
            source_metadata=retrieval_input["source_metadata"],
            raw_response=retrieval_input["raw_response"],
            snapshot=retrieval_input["snapshot"],
        )
        results.append(
            {
                "retrieval_id": record.retrieval_id,
                "source": record.source,
                "external_id": record.external_id,
                "retrieved_at": record.retrieved_at,
                "snapshot": json.loads(record.snapshot),
                "response_hash": record.response_hash,
                "content_hash": hashlib.sha256(
                    retrieval_input["raw_response"].encode("utf-8")
                ).hexdigest(),
            }
        )
    return results


def search_source(
    conn: Any,
    source: str,
    query: str,
    agent_id: str,
    max_results: int = 5,
) -> list[dict]:
    """Search one source and persist every returned item before returning it."""
    _validate(query, agent_id, max_results)

    connector = CONNECTORS.get(source)
    if connector is None:
        supported = ", ".join(SUPPORTED_SOURCES)
        raise ValueError(f"unsupported source {source!r}; choose from: {supported}")
    items = connector.search(query, max_results)
    return _store_results(conn, query, agent_id, items, connector.to_retrieval_input)


def search_evidence(
    conn: Any,
    query: str,
    agent_id: str,
    sources: list[str] | tuple[str, ...] = SUPPORTED_SOURCES,
    max_results: int = 5,
) -> dict:
    """Search multiple sources and return explicit per-source results and errors.

    A source outage does not erase successful, already-audited results from other
    sources. Partial failure is always visible in the returned ``errors`` object.
    """
    _validate(query, agent_id, max_results)
    if not sources:
        raise ValueError("at least one source is required")

    results: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}
    for source in dict.fromkeys(sources):
        try:
            results[source] = search_source(conn, source, query, agent_id, max_results)
        except Exception as exc:
            errors[source] = str(exc)

    return {
        "query_echo": query,
        "agent_id": agent_id,
        "sources": results,
        "errors": errors,
        "retrieval_count": sum(len(records) for records in results.values()),
    }
