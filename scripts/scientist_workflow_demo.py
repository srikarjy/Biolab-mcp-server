#!/usr/bin/env python3
"""Run a short, real Biolab scientist workflow against a REST deployment."""

import argparse
import json
from urllib.request import Request, urlopen


def call(base_url: str, method: str, path: str, payload: dict | None = None) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if body else {}
    request = Request(f"{base_url.rstrip('/')}{path}", data=body, headers=headers, method=method)
    with urlopen(request, timeout=90) as response:
        return json.loads(response.read())


def run(base_url: str) -> dict:
    project = call(base_url, "POST", "/v1/projects", {
        "name": "KRAS evidence demo",
        "description": "Auditable target-evidence walkthrough",
    })
    search = call(base_url, "POST", "/v1/evidence/search", {
        "query": "KRAS",
        "sources": ["uniprot", "opentargets"],
        "max_results": 1,
        "agent_id": "demo:scientist",
    })
    records = [record for source in search["sources"].values() for record in source]
    if not records:
        raise RuntimeError(f"No evidence returned: {search['errors']}")
    evidence = records[0]

    collection = call(base_url, "POST", "/v1/collections", {
        "project_id": project["project_id"],
        "name": "Reviewed target evidence",
    })
    call(base_url, "POST", f"/v1/collections/{collection['collection_id']}/items", {
        "retrieval_id": evidence["retrieval_id"],
        "relevance": "relevant",
        "note": "Included for demonstration; scientific interpretation still requires review.",
    })

    claim = call(base_url, "POST", "/v1/claims", {
        "project_id": project["project_id"],
        "claim": "KRAS has a traceable protein or target record.",
    })
    call(base_url, "POST", f"/v1/claims/{claim['claim_id']}/evidence", {
        "retrieval_id": evidence["retrieval_id"],
        "stance": "context",
        "assessment": "agent_proposed",
        "note": "Demo relationship only; not a biological or clinical conclusion.",
    })

    watch = call(base_url, "POST", "/v1/watches", {
        "project_id": project["project_id"],
        "source": "uniprot",
        "query": "KRAS",
        "agent_id": "demo:watch",
        "max_results": 1,
    })
    watch_run = call(base_url, "POST", f"/v1/watches/{watch['watch_id']}/run")
    report = call(base_url, "GET", f"/v1/projects/{project['project_id']}/report")
    verification = call(base_url, "GET", "/v1/verify")

    return {
        "project_id": project["project_id"],
        "retrieval_id": evidence["retrieval_id"],
        "sources": {name: len(items) for name, items in search["sources"].items()},
        "source_errors": search["errors"],
        "collection_id": collection["collection_id"],
        "claim_id": claim["claim_id"],
        "watch_id": watch["watch_id"],
        "watch_events": watch_run["events"],
        "report_counts": {
            "collections": len(report["collections"]),
            "claims": len(report["claims"]),
            "watches": len(report["watches"]),
        },
        "audit_chain": verification,
        "scientific_boundary": "Demonstrates provenance, not scientific or clinical validation.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    print(json.dumps(run(args.url), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
