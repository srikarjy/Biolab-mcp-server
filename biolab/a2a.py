"""Small A2A JSON-RPC adapter over the audited evidence service.

This adapter intentionally keeps task execution synchronous for the first A2A
integration. It supports discovery, message/send, and tasks/get while returning
the same retrieval IDs and explicit source errors as the existing REST/MCP
surfaces.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from biolab import auth, inference, ranking, workspace
from biolab import evidence as evidence_service

A2A_PROTOCOL_VERSION = "1.0"
DEFAULT_SOURCES = ["pubmed", "europepmc"]


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def agent_card(base_url: str) -> dict[str, Any]:
    """Return the discoverable A2A Agent Card for this deployment."""
    base_path = os.environ.get("BIOLAB_A2A_BASE_PATH", "").rstrip("/")
    return {
        "name": "Biolab Evidence Agent",
        "description": (
            "Audited scientific evidence retrieval agent. It preserves source "
            "responses, retrieval IDs, explicit errors, and provenance."
        ),
        "version": "0.4.1",
        "supportedInterfaces": [{
            "url": f"{base_url.rstrip('/')}{base_path}/a2a",
            "protocolBinding": "JSONRPC",
            "protocolVersion": A2A_PROTOCOL_VERSION,
        }],
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": True,
            "fileDownload": False,
        },
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": [{
            "id": "audited-evidence-search",
            "name": "Audited evidence search",
            "description": (
                "Search supported scientific sources and return preserved "
                "retrieval IDs with explicit source errors."
            ),
            "tags": ["scientific-evidence", "provenance", "retrieval"],
            "examples": ["Find evidence about KRAS in pancreatic cancer."],
        }, {
            "id": "rank-and-cite",
            "name": "Rank and cite preserved evidence",
            "description": "Rank preserved retrievals and extract grounded claims with citation spans.",
            "tags": ["ranking", "citations", "human-review"],
        }],
    }


class A2ATaskStore:
    """Per-application in-memory task store for completed synchronous tasks."""

    def __init__(self) -> None:
        self._tasks: dict[str, dict[str, Any]] = {}

    def put(self, task: dict[str, Any]) -> None:
        self._tasks[task["id"]] = task

    def get(self, task_id: str) -> dict[str, Any] | None:
        return self._tasks.get(task_id)


def _store(request: Request) -> A2ATaskStore:
    tasks = getattr(request.app.state, "a2a_tasks", None)
    if tasks is None:
        tasks = A2ATaskStore()
        request.app.state.a2a_tasks = tasks
    return tasks


def _rpc_error(request_id: Any, code: int, message: str, data: Any = None) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": error})


def _text_from_message(message: dict[str, Any]) -> str:
    values: list[str] = []
    for part in message.get("parts", []):
        if not isinstance(part, dict):
            continue
        if isinstance(part.get("text"), str):
            values.append(part["text"])
    return "\n".join(value.strip() for value in values if value.strip())


def _search_options(params: dict[str, Any], message: dict[str, Any]) -> tuple[list[str], int]:
    metadata = params.get("metadata") or message.get("metadata") or {}
    sources = metadata.get("sources", DEFAULT_SOURCES)
    max_results = metadata.get("max_results", 5)
    if not isinstance(sources, list) or not all(isinstance(source, str) for source in sources):
        raise ValueError("metadata.sources must be a list of source names")
    if not isinstance(max_results, int) or isinstance(max_results, bool):
        raise TypeError("metadata.max_results must be an integer")
    return sources, max_results


def _task(message: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    task_id = str(uuid.uuid4())
    context_id = message.get("contextId") or str(uuid.uuid4())
    status_message = {
        "messageId": str(uuid.uuid4()),
        "contextId": context_id,
        "taskId": task_id,
        "role": "ROLE_AGENT",
        "parts": [{"text": "Evidence search completed.", "mediaType": "text/plain"}],
    }
    return {
        "id": task_id,
        "contextId": context_id,
        "status": {
            "state": "TASK_STATE_COMPLETED",
            "message": status_message,
            "timestamp": _now(),
        },
        "artifacts": [{
            "artifactId": str(uuid.uuid4()),
            "name": "audited-evidence-search-result",
            "description": "Biolab search result with retrieval IDs and source errors.",
            "parts": [{"data": result, "mediaType": "application/json"}],
        }],
        "history": [message, status_message],
        "metadata": {
            "scientificBoundary": (
                "Provenance and retrieval are recorded; scientific or clinical "
                "validation still requires human review."
            )
        },
    }


def _metadata(params: dict[str, Any], message: dict[str, Any]) -> dict[str, Any]:
    metadata = params.get("metadata") or message.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise TypeError("metadata must be an object")
    return metadata


def _derived_operation(request: Request, metadata: dict[str, Any]) -> dict[str, Any] | None:
    operation = metadata.get("operation")
    if operation == "rank":
        ids = metadata.get("retrieval_ids", [])
        if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
            raise ValueError("metadata.retrieval_ids must be a list of strings")
        return ranking.rank_retrievals(
            request.app.state.conn, str(metadata.get("question", "")), ids,
            created_by=f"a2a:{auth.current_identity.get()}",
            project_id=str(metadata["project_id"]) if metadata.get("project_id") else None,
            requested_limit=int(metadata.get("requested_limit", 5)),
        )
    if operation == "grounded_insights":
        ids = metadata.get("retrieval_ids", [])
        if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
            raise ValueError("metadata.retrieval_ids must be a list of strings")
        return inference.create_inference(
            request.app.state.conn, str(metadata.get("question", "")), ids,
            created_by=f"a2a:{auth.current_identity.get()}",
            project_id=str(metadata["project_id"]) if metadata.get("project_id") else None,
            ranking_run_id=str(metadata["ranking_run_id"]) if metadata.get("ranking_run_id") else None,
            max_insights=int(metadata.get("max_insights", 10)),
        )
    if operation == "project_report":
        report = workspace.get_project_report(request.app.state.conn, str(metadata.get("project_id", "")))
        if report is None:
            raise ValueError("project not found")
        return report
    if operation == "save_reviewed":
        candidate_ids = metadata.get("candidate_ids", [])
        if not isinstance(candidate_ids, list) or not all(isinstance(item, str) for item in candidate_ids):
            raise ValueError("metadata.candidate_ids must be a list of strings")
        return workspace.save_reviewed_candidates(
            request.app.state.conn, str(metadata.get("project_id", "")),
            str(metadata.get("ranking_run_id", "")), candidate_ids,
            owner_id=f"a2a:{auth.current_identity.get()}",
            override_note=str(metadata.get("override_note", "")),
        )
    return None


async def agent_card_endpoint(request: Request) -> JSONResponse:
    base_url = f"{request.url.scheme}://{request.url.netloc}"
    return JSONResponse(agent_card(base_url))


async def rpc_endpoint(request: Request) -> JSONResponse:  # noqa: PLR0911 - JSON-RPC dispatch
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return _rpc_error(None, -32700, "Parse error")

    request_id = payload.get("id") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
        return _rpc_error(request_id, -32600, "Invalid Request")

    method = payload.get("method")
    params = payload.get("params") or {}
    if not isinstance(method, str) or not isinstance(params, dict):
        return _rpc_error(request_id, -32600, "Invalid Request")

    if method in {"tasks/get", "GetTask"}:
        task_id = params.get("id")
        if not isinstance(task_id, str) or not task_id:
            return _rpc_error(request_id, -32602, "Task id is required")
        task = _store(request).get(task_id)
        if task is None:
            return _rpc_error(request_id, -32001, "Task not found", {"taskId": task_id})
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": task})

    if method not in {"message/send", "SendMessage"}:
        return _rpc_error(request_id, -32601, f"Method not found: {method}")

    message = params.get("message")
    if not isinstance(message, dict) or message.get("role") not in {"ROLE_USER", "user"}:
        return _rpc_error(request_id, -32602, "params.message with role ROLE_USER is required")
    query = _text_from_message(message)
    if not query:
        return _rpc_error(request_id, -32602, "message.parts must contain text")

    try:
        metadata = _metadata(params, message)
        result = _derived_operation(request, metadata)
        if result is None:
            sources, max_results = _search_options(params, message)
            result = evidence_service.search_evidence(
                request.app.state.conn, query=query,
                agent_id=f"a2a:{auth.current_identity.get()}",
                sources=sources, max_results=max_results,
            )
    except (TypeError, ValueError) as exc:
        return _rpc_error(request_id, -32602, str(exc))
    except Exception as exc:  # noqa: BLE001 - convert connector failures to task errors
        return _rpc_error(request_id, -32000, "Evidence search failed", {"error": str(exc)})

    task = _task(message, result)
    _store(request).put(task)
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": {"task": task}})


async def a2a_info_endpoint(request: Request) -> JSONResponse:
    """Explain the A2A endpoint when a person opens it in a browser."""
    base = str(request.base_url).rstrip("/")
    return JSONResponse({
        "service": "Biolab Evidence Agent",
        "protocol": "A2A JSON-RPC 1.0",
        "message": "Use POST with a JSON-RPC message/send or tasks/get request.",
        "agent_card": f"{base}/.well-known/agent-card.json",
        "methods": ["message/send", "tasks/get"],
        "example": {
            "jsonrpc": "2.0",
            "id": "search-1",
            "method": "message/send",
            "params": {"message": {"role": "user", "parts": [{"text": "Find evidence about KRAS"}]}},
        },
    })


def a2a_routes() -> list[Route]:
    return [
        Route("/.well-known/agent-card.json", agent_card_endpoint),
        Route("/a2a", a2a_info_endpoint, methods=["GET"]),
        Route("/a2a", rpc_endpoint, methods=["POST"]),
        Route("/a2a/v1", a2a_info_endpoint, methods=["GET"]),
        Route("/a2a/v1", rpc_endpoint, methods=["POST"]),
    ]
