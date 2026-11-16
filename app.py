"""Vercel ASGI entrypoint for the Biolab MCP/REST/A2A application."""

import os

os.environ.setdefault("BIOLAB_DB_PATH", "/tmp/biolab.db")
os.environ.setdefault("BIOLAB_ARTIFACT_DIR", "/tmp/biolab_artifacts")
os.environ.setdefault("BIOLAB_A2A_BASE_PATH", "")
os.environ.setdefault("BIOLAB_REQUIRE_AUTH", "true")
os.environ.setdefault("BIOLAB_ALLOW_ANONYMOUS", "false")
# FastMCP enables localhost-only DNS rebinding protection when its host is
# 127.0.0.1. Vercel terminates TLS and forwards the public Host header, so the
# serverless adapter must opt out of that localhost-only default. The REST and
# A2A routes retain their own API-key middleware and Vercel is still the TLS
# boundary.
os.environ.setdefault("BIOLAB_HOST", "0.0.0.0")

from biolab.server import create_hosted_app  # noqa: E402

app = create_hosted_app()
