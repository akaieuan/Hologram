---
name: Audit the asset catalogue
description: Read-only conformance sweep of the whole export catalogue — files on disk vs the manifest, hologram checks, stale thumbnails, and version drift — reported as a table and logged to the activity trail. Use when someone asks to audit, lint, or health-check the assets, thinks the manifest is out of date, or wants a catalogue-wide status before a review pass.
kind: audit
---

# Audit the asset catalogue

A catalogue-wide, read-only conformance pass. Audit REPORTS; it never fixes and
never changes review states (that's `/hologram:asset-gate`).

## What to do

1. **Run the checks.** `hologram check --json` (add `--project <dir>` off-root).
   This covers the project's `.hologram/checks.py` rules plus the golden-truth
   budgets and thumbnail drift when a `golden.json` is present.

2. **Cross-check the manifest against disk.** Read `exports/manifest.json` (or
   the project's export root — `hologram status` names it). For each record:
   the `.glb` exists on disk; the thumbnail exists; the thumbnail isn't stale
   (rendered for an older version than the record's `version`); category is one
   the project uses. Then the reverse: GLBs on disk that the manifest doesn't
   know about (unregistered exports — usually a missed manifest upsert; point
   at `examples/export_helper.py`'s convention).

3. **Report a compact table.** One row per asset: `asset · status · issues`,
   with `error` (broken file or contract) separated from `warn` (missing
   optional metadata, stale thumbnail). End with totals: N assets, E errors,
   W warnings, and the top recurring issue types.

4. **Log the run.** Append one line to `<export_root>/activity.jsonl`
   (append-only, agent-written — Hologram itself never writes):

   ```json
   {"ts": "<ISO-8601Z>", "actor": "claude", "skill": "asset-audit", "target": "manifest", "note": "N assets, E errors, W warnings"}
   ```

## Rules

- **Read-only.** Do not modify manifests, assets, thumbnails, or review states.
  Offer fixes as follow-ups and let the user pick.
- If there's no manifest at all, say so and point at the export-manifest
  convention in the Hologram README — the audit still runs the GLB checks.
