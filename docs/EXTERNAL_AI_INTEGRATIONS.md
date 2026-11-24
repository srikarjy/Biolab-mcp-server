# External AI integrations

Biolab exposes one governed MCP surface for research assistants. ChatGPT,
Claude, and other MCP clients call Biolab tools; they do not receive database,
embedding, or generation-provider credentials.

## Hosted endpoint

```text
MCP:  https://biolab-a2a.vercel.app/mcp
REST: https://biolab-a2a.vercel.app/v1
A2A:  https://biolab-a2a.vercel.app/.well-known/agent-card.json
```

The hosted deployment must have `BIOLAB_REQUIRE_AUTH=true` before it is used
with private research projects. Generate a Biolab API key through the operator
workflow and send it as:

```http
Authorization: Bearer blk_...
```

Never send `NVIDIA_API_KEY`, `PGVECTOR_DATABASE_URL`, `TURSO_AUTH_TOKEN`, or
other provider/database secrets to an AI client.

## Claude Desktop or another MCP client

Configure the remote MCP server using the client’s HTTP transport settings:

```json
{
  "mcpServers": {
    "biolab": {
      "url": "https://biolab-a2a.vercel.app/mcp",
      "headers": {
        "Authorization": "Bearer blk_REPLACE_WITH_BIOLAB_KEY"
      }
    }
  }
}
```

For local development, run `python -m biolab.server` and use
`http://127.0.0.1:8000/mcp` instead.

## ChatGPT or OpenAI clients

Use the client’s remote MCP/connector configuration and provide the same MCP
URL and Biolab bearer key. The supported tool contract is the MCP contract, not
an OpenAI-specific database integration.

The tools are intentionally separated by permission:

- Read-only: project listing, RAG search, grounded answers, citation tracing,
  graph inspection, and audit verification.
- Proposal/write: paper parsing, block review, graph proposals, notes, and
  audit export.
- Human-controlled: accepting graph proposals or other irreversible changes.

## Supported governed tools

```text
list_projects
list_project_papers
search_project_rag
answer_project_rag
get_project_context
list_project_query_history
get_project_audit
verify_project_audit
export_project_audit
get_graph_stats
query_project_graph
expand_graph_entity
trace_graph_entity
```

Every project-scoped tool checks project ownership. Every write is attributed
to the authenticated agent identity and relevant activity is added to the
project audit ledger.

## Safe researcher workflow

```text
AI client
  -> identify project
  -> retrieve accepted evidence
  -> answer with citations
  -> trace citations when needed
  -> propose graph facts
  -> human reviews proposals
  -> verify and export audit history
```

An AI client can use Biolab’s evidence, but Biolab does not permanently teach
the model the papers. The project database preserves the papers, queries,
citations, decisions, and audit history.

## Connection acceptance test

After connecting a client, verify this sequence in one project:

1. `list_projects`
2. `get_project_context`
3. `search_project_rag`
4. `answer_project_rag`
5. `verify_project_audit`
6. Confirm the external agent identity appears in the audit events.

If the client reports an authentication error, do not substitute an NVIDIA or
OpenAI provider key. The MCP client requires a Biolab API key.
