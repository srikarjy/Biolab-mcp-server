# Biolab A2A Integration

Biolab now exposes a narrow Agent2Agent (A2A) JSON-RPC surface alongside its
existing MCP, REST, and CLI interfaces.

## Discovery

The Agent Card is available at:

```text
/.well-known/agent-card.json
```

The JSON-RPC endpoint is:

```text
/a2a
```

Opening `/a2a` in a browser returns a small JSON usage guide. Agent clients
must use `POST` with a JSON-RPC `message/send` or `tasks/get` request.

It declares A2A protocol version 1.0 and the `audited-evidence-search` skill.

## Send a message

```bash
curl -s http://127.0.0.1:8000/a2a \
  -H 'content-type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": "search-1",
    "method": "message/send",
    "params": {
      "message": {
        "messageId": "message-1",
        "role": "ROLE_USER",
        "parts": [{"text": "Find evidence about KRAS in pancreatic cancer"}]
      },
      "metadata": {
        "sources": ["pubmed", "europepmc"],
        "max_results": 3
      }
    }
  }'
```

The response is a completed A2A `Task`. Its artifact contains Biolab’s normal
audited search result, including retrieval IDs and explicit source errors.

## Retrieve a task

Use the returned task ID with `tasks/get`:

```bash
curl -s http://127.0.0.1:8000/a2a \
  -H 'content-type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": "task-1",
    "method": "tasks/get",
    "params": {"id": "<task_id>"}
  }'
```

The PascalCase aliases `SendMessage` and `GetTask` are also accepted for JSON-
RPC clients following the A2A JSON-RPC binding names.

## Current boundary

This first adapter is synchronous and keeps task state in process memory. It
supports Agent Card discovery, `message/send`, and `tasks/get`; streaming,
push notifications, durable A2A task storage, and multi-turn task continuation
are not implemented yet. The underlying evidence retrieval remains fully
audited in the Biolab database.

A2A interoperability does not change Biolab’s scientific boundary: it preserves
retrieval provenance and explicit errors but does not establish biological truth
or clinical validity.
