#!/usr/bin/env python3
"""Dependency-free BioClaw client for the local Biolab REST API."""

import argparse
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_URL = "http://host.docker.internal:8000"


def request_json(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    base_url = os.environ.get("BIOLAB_URL", DEFAULT_URL).rstrip("/")
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    api_key = os.environ.get("BIOLAB_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    request = Request(f"{base_url}{path}", data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read())
    except HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        try:
            error = json.loads(response_body)
        except json.JSONDecodeError:
            error = {"error": response_body or str(exc)}
        return exc.code, error
    except URLError as exc:
        return 0, {"error": f"Biolab is unreachable: {exc.reason}"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Call the Biolab evidence API")
    subparsers = parser.add_subparsers(dest="command", required=True)

    search = subparsers.add_parser("search", help="Search and preserve evidence")
    search.add_argument("query")
    search.add_argument(
        "--sources",
        default="pubmed,europepmc,clinicaltrials,uniprot,opentargets",
        help="Comma-separated sources",
    )
    search.add_argument("--max-results", type=int, default=5)
    search.add_argument("--agent-id", default="bioclaw:research")

    get = subparsers.add_parser("get", help="Retrieve one audit record")
    get.add_argument("retrieval_id")
    subparsers.add_parser("verify", help="Verify the audit chain")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "search":
        sources = [source.strip() for source in args.sources.split(",") if source.strip()]
        status, result = request_json(
            "POST",
            "/v1/evidence/search",
            {
                "query": args.query,
                "sources": sources,
                "max_results": args.max_results,
                "agent_id": args.agent_id,
            },
        )
    elif args.command == "get":
        status, result = request_json("GET", f"/v1/retrievals/{args.retrieval_id}")
    else:
        status, result = request_json("GET", "/v1/verify")

    print(json.dumps(result, indent=2, sort_keys=True))
    if status < 200 or status >= 300:
        return 1
    if args.command == "verify" and result.get("valid") is not True:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
