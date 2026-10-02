# Biolab scientist workflow demo

This two-minute walkthrough uses real UniProt and Open Targets records while
keeping scientific interpretation explicitly separate from provenance.

## Run it

Start Biolab in one terminal:

```bash
biolab web
```

Run the recorded workflow in another:

```bash
python scripts/scientist_workflow_demo.py --url http://127.0.0.1:8000
```

The script performs this sequence:

1. Creates a KRAS evidence project.
2. Searches UniProt and Open Targets and preserves each raw response.
3. Adds one retrieval to a reviewed collection.
4. Creates a claim and links the evidence as `context` / `agent_proposed`.
5. Creates and runs a UniProt evidence watch.
6. Generates the project report and verifies the complete audit hash chain.

## Verified release run

The 0.4 release walkthrough returned one UniProt and one Open Targets result,
created one collection, claim, and watch, recorded the watch's initial `added`
event, and verified a three-record audit chain with no broken retrieval.

IDs are intentionally generated per run. The output includes the project,
retrieval, collection, claim, and watch IDs so every step can be inspected.

This demonstrates retrieval provenance and workflow traceability. It does not
establish biological truth, target validity, or clinical utility.
