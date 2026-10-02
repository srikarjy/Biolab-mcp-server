# Dockerfile for the Hugging Face Space — runs the Python MCP server
# (biolab/) over streamable-http. Distinct from the repo-root Dockerfile,
# which packages the Go implementation for stdio use; do not merge these.
#
# Build context for this Dockerfile is the repo root (see deploy instructions
# in space/README.md / PROJECT_STATUS.md), so it can COPY pyproject.toml,
# biolab/, and LICENSE directly.
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml LICENSE README.md ./
COPY biolab ./biolab

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential cmake \
    && pip install --no-cache-dir ".[turso]" \
    && apt-get purge -y --auto-remove build-essential cmake \
    && rm -rf /var/lib/apt/lists/*

# This hosted image includes the optional Turso driver for durable Space storage.
ENV BIOLAB_HOST=0.0.0.0
ENV BIOLAB_PORT=8000
ENV BIOLAB_DB_PATH=/tmp/biolab.db

EXPOSE 8000

ENTRYPOINT ["python", "-m", "biolab.server"]
