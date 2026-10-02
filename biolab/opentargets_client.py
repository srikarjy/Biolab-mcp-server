"""Thin Open Targets Platform GraphQL adapter with no database knowledge."""

import json
from dataclasses import dataclass
from urllib.request import Request, urlopen

GRAPHQL_URL = "https://api.platform.opentargets.org/api/v4/graphql"
TIMEOUT_SECONDS = 20

SEARCH_QUERY = """
query SearchBiolab($query: String!) {
  search(queryString: $query) {
    total
    hits {
      entity
      score
      object {
        ... on Target { id approvedSymbol approvedName }
        ... on Disease { id name }
        ... on Drug { id name drugType }
      }
    }
  }
}
"""


@dataclass
class OpenTargetsHit:
    entity: str
    object_id: str
    name: str
    score: float
    full_json: str


def search(query: str, max_results: int) -> list[OpenTargetsHit]:
    body = json.dumps({"query": SEARCH_QUERY, "variables": {"query": query}}).encode("utf-8")
    request = Request(
        GRAPHQL_URL,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "biolab-mcp/0.3"},
        method="POST",
    )
    with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        raw_response = response.read().decode("utf-8")
    payload = json.loads(raw_response)
    if payload.get("errors"):
        raise RuntimeError(f"Open Targets GraphQL error: {payload['errors'][0].get('message', 'unknown error')}")

    hits = []
    for raw_hit in payload.get("data", {}).get("search", {}).get("hits", []):
        obj = raw_hit.get("object") or {}
        object_id = obj.get("id")
        if not object_id:
            continue
        name = obj.get("approvedSymbol") or obj.get("name") or obj.get("approvedName") or object_id
        hits.append(
            OpenTargetsHit(
                entity=raw_hit.get("entity", ""),
                object_id=object_id,
                name=name,
                score=float(raw_hit.get("score") or 0),
                full_json=raw_response,
            )
        )
        if len(hits) >= max_results:
            break
    return hits


def hit_to_retrieval_input(hit: OpenTargetsHit) -> dict:
    snapshot = {
        "title": hit.name,
        "abstract": "",
        "authors": [],
        "journal": {"title": "Open Targets Platform"},
        "publication_types": [hit.entity] if hit.entity else [],
        "doi": "",
        "entity": hit.entity,
        "score": hit.score,
        "canonical_url": f"https://platform.opentargets.org/{hit.entity}/{hit.object_id}",
    }
    return {
        "source": "opentargets",
        "external_id": hit.object_id,
        "source_metadata": {"entity": hit.entity, "score": hit.score},
        "raw_response": hit.full_json,
        "snapshot": snapshot,
    }
