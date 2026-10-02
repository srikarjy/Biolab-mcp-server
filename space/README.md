---
title: Biolab MCP Server
emoji: 🧬
colorFrom: blue
colorTo: green
sdk: docker
app_port: 8000
---

# Biolab MCP Server

Scientific evidence workspace with auditable retrieval, projects, collections,
watches, claims, and reports. Unified search supports PubMed, Europe PMC,
ClinicalTrials.gov, UniProt, and Open Targets; bioRxiv/medRxiv remains available
through its source-specific tool.

This Space runs MCP at `/mcp`, REST at `/v1`, and the dashboard at `/`. The base
image uses local SQLite. Mount durable storage for self-hosting; a remote
[Turso](https://turso.tech) deployment additionally installs the `turso` extra
and supplies `TURSO_DATABASE_URL` / `TURSO_AUTH_TOKEN` secrets.

Source: https://github.com/srikarjy/biolab-mcp-server
