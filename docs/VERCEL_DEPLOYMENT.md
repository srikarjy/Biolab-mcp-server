# Vercel Deployment

Biolab can run as a Python ASGI application on Vercel through `app.py`. The
entrypoint reuses `biolab.server.create_hosted_app`, so MCP, REST, dashboard,
and A2A routes share the same implementation.

The deployment includes the `libsql` runtime driver. Turso is used only when
the operator supplies the required environment variables; the package does not
create a database or configure credentials automatically.

The Vercel adapter defaults the database to `/tmp/biolab.db` and requires API
authentication. Vercel filesystem storage is ephemeral; configure Turso
variables before using this as a durable service.

Canonical endpoints on the unified deployment are exposed at the domain root;
the `/api` forms remain available as compatibility URLs for Vercel tooling:

```text
/.well-known/agent-card.json
/a2a
/mcp
/health
/openapi.json
/metrics
/
```

This deployment path is for a demo or low-state integration until the free
durable split backend is configured. Turso/libSQL stores projects, retrieval
links, parser records, audit history, and query cache; Neon Postgres with
pgvector stores accepted parser vectors. Configure `TURSO_DATABASE_URL`,
`TURSO_AUTH_TOKEN`, and `PGVECTOR_DATABASE_URL` in the Vercel Production
environment. Keep authentication enabled in hosted deployments; anonymous mode
is only appropriate for an isolated demo.

The repository includes `.env.example`. Copy its variable names into the
deployment environment, but never commit real values. After configuring them,
verify `/v1/storage` reports `connected`, `schema_ready`, and
`durable_claim_allowed: true` for both backends before treating project history
or RAG vectors as durable.
