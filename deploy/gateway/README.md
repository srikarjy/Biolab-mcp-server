# Zero-cost gateway deployment

This Compose stack runs one Biolab process behind Caddy. The same process serves:

- MCP at `/mcp`
- REST at `/v1/*`
- the scientist dashboard at `/`
- readiness at `/health`

Its dedicated image uses standard-library SQLite and does not compile or install
the optional Turso driver.

It uses a local named volume and requires Biolab API keys. Start it with:

```bash
docker compose -f deploy/gateway/compose.yml up --build
```

Create a key from another shell:

```bash
docker compose -f deploy/gateway/compose.yml exec biolab \
  biolab keys create scientist --db /data/biolab.db
```

The local gateway listens on `http://127.0.0.1:8080` through Docker's port
publishing. Before public deployment, replace the Caddy address with a real
domain so Caddy can provision HTTPS, keep `BIOLAB_REQUIRE_AUTH=true`, and store
database/API secrets outside the Compose file. This template is not a HIPAA or
clinical deployment profile.
