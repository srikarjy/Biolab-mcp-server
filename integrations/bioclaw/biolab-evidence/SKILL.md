---
name: biolab-evidence
description: Search and preserve auditable biomedical evidence through Biolab. Use for literature, clinical-trial, or cross-source evidence searches where the user needs retrieval IDs, raw-source traceability, or audit-chain verification.
---

# Biolab Evidence

Use Biolab as the provenance layer for scientific evidence retrieved in BioClaw.
Every result is persisted before it is returned and includes a `retrieval_id`
that can be used to recover the normalized snapshot and original source payload.

## Configuration

The script reads these environment variables:

- `BIOLAB_URL` — dashboard/API origin, default `http://host.docker.internal:8000`
- `BIOLAB_API_KEY` — optional bearer token when an authenticated gateway is used

Start a local Biolab API on the host:

```bash
biolab web
```

On Linux Docker hosts, `host.docker.internal` may require an explicit host-gateway
mapping. Alternatively set `BIOLAB_URL` to an address reachable from the agent
container. Do not use the public demo for patient or otherwise sensitive data.

## Search Evidence

```bash
python /home/node/.claude/skills/biolab-evidence/scripts/biolab_client.py search \
  "KRAS pancreatic cancer" \
  --sources pubmed,europepmc,clinicaltrials,uniprot,opentargets \
  --max-results 5 \
  --agent-id bioclaw:research
```

Always report:

- Which sources succeeded
- Which sources failed
- The retrieval ID for every item
- That the retrieved material is source evidence, not a validated conclusion

Do not hide the response's `errors` object when one source fails.

## Retrieve the Audit Record

```bash
python /home/node/.claude/skills/biolab-evidence/scripts/biolab_client.py get RETRIEVAL_ID
```

Use this when the scientist asks to inspect the raw evidence, reproduce a prior
search result, or verify that a summary matches the source payload.

## Verify the Audit Chain

```bash
python /home/node/.claude/skills/biolab-evidence/scripts/biolab_client.py verify
```

A nonzero exit status means the chain is invalid or the API request failed. Never
describe a broken chain as verified.

## Scientific Boundaries

- Do not convert search results into clinical advice.
- Keep agent interpretation separate from Biolab's immutable source record.
- Preserve retrieval IDs in downstream reports and analysis manifests.
- Require human review before experimental or clinical decisions.
