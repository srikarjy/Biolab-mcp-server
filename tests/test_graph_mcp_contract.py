"""MCP contract tests for the graph tool surface.

These tests intentionally inspect the published tool contract without starting
the network server. The live streamable-HTTP path remains a separate phase.
"""

import asyncio


def test_graph_mcp_tools_are_read_or_proposal_only():
    from biolab.server import mcp

    async def listed():
        return {tool.name: tool for tool in await mcp.list_tools() if "graph" in tool.name}

    tools = asyncio.run(listed())
    assert {
        "list_graph_proposals", "get_graph_stats", "expand_graph_entity",
        "trace_graph_entity", "propose_graph_entity", "propose_graph_relationship",
    } <= tools.keys()
    assert tools["list_graph_proposals"].annotations.readOnlyHint is True
    assert tools["expand_graph_entity"].annotations.readOnlyHint is True
    assert tools["propose_graph_entity"].annotations.readOnlyHint is False
    assert tools["propose_graph_relationship"].annotations.readOnlyHint is False
    assert not any("accept_graph" in name or "review_graph" in name for name in tools)
