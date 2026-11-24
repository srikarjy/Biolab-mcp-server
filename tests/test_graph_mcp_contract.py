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


def test_external_ai_audit_tools_have_explicit_permissions():
    from biolab.server import mcp

    async def listed():
        return {tool.name: tool for tool in await mcp.list_tools() if "audit" in tool.name}

    tools = asyncio.run(listed())
    assert {"get_project_audit", "verify_project_audit", "export_project_audit"} <= tools.keys()
    assert tools["get_project_audit"].annotations.readOnlyHint is True
    assert tools["verify_project_audit"].annotations.readOnlyHint is True
    assert tools["export_project_audit"].annotations.readOnlyHint is False


def test_external_ai_notebook_export_is_write_audited():
    from biolab.server import mcp

    async def listed():
        return {tool.name: tool for tool in await mcp.list_tools() if "notebook" in tool.name}

    tools = asyncio.run(listed())
    assert "export_project_notebook" in tools
    assert tools["export_project_notebook"].annotations.readOnlyHint is False


def test_external_ai_cited_notes_export_is_write_audited():
    from biolab.server import mcp

    async def listed():
        return {tool.name: tool for tool in await mcp.list_tools() if "cited_notes" in tool.name}

    tools = asyncio.run(listed())
    assert "export_project_cited_notes" in tools
    assert tools["export_project_cited_notes"].annotations.readOnlyHint is False
