# Vercel Deployment

Biolab can run as a Python ASGI application on Vercel through `app.py`. The
entrypoint reuses `biolab.server.create_hosted_app`, so MCP, REST, dashboard,
and A2A routes share the same implementation.

The deployment installs the optional Turso extra with `pip install ".[turso]"`.
That makes the runtime capable of using Turso when the operator supplies the
required environment variables; it does not create a database or configure
credentials automatically.

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

This deployment path is for a demo or low-state integration. Durable evidence
storage requires `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, and the optional
Turso dependency in the deployment environment. Keep authentication enabled in
hosted deployments; anonymous mode is only appropriate for an isolated demo.
