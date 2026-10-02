---
name: evidence-workspace
description: Build an auditable biomedical evidence workspace with Biolab. Use for cross-source evidence searches, source verification, reviewed collections, target validation, literature monitoring, or retrieval provenance.
---

# Biolab Evidence Workspace

Use Biolab as the source-of-record layer. Distinguish retrieved source material,
agent interpretation, and human review at all times.

## Search

For a broad scientific question, call `search_evidence` with only relevant
sources. Begin with a small result limit, inspect explicit source errors, and
preserve each returned `retrieval_id` in downstream notes.

For a source-specific question, prefer its dedicated tool. Never claim that a
search result validates a mechanism, causal relationship, clinical intervention,
or experimental conclusion.

## Verify

Call `get_retrieval` before quoting or relying on a previously retrieved item.
Compare the interpretation with the normalized snapshot and raw response. A
retrieval ID is evidence provenance, not a quality score.

## Organize

Before creating or changing a project, collection, or watch, summarize the
intended mutation and obtain user approval when the host requests it.

1. Create or select a project.
2. Create a focused evidence collection.
3. Add retrievals with an explicit relevance state and concise human-review note.
4. Keep conflicting and excluded evidence rather than silently deleting it.

## Monitor

Create a watch only for a query and source the user wants monitored. Running a
watch contacts the external source, writes new retrieval records, and may create
added, removed, or content-changed events. Report changes and source errors.

## Boundaries

- Do not provide clinical advice.
- Do not send patient or sensitive data to a public deployment.
- Do not equate Open Targets scores with proof of causal biology.
- Do not equate UniProt review status with validation of a therapeutic claim.
- Require scientist review before experimental or clinical decisions.
