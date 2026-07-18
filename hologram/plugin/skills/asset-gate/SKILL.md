---
name: Gate assets for review
description: Run the build gate on one or more assets — hologram checks plus golden.json budgets and thumbnail drift — and stamp each pending-review or gate-failed in the manifest. Use after exporting or regenerating an asset, at the end of any agentic build flow, or when someone asks to gate, re-gate, or ready an asset for review.
kind: gate
---

# Gate assets for review

The chokepoint between "an agent built something" and "a human reviews it".
Standards come from `golden.json` and `.hologram/checks.py` — the gate applies
them, it never defines them.

**The one rule: you may stamp `pending-review` or `gate-failed`. You must NEVER
mark an asset approved or rejected — approval is the human's move.**

## What to do

1. **Resolve the target.** An asset id, a `.glb` path, a category, or `all`.
   A bare name → match against manifest id stems; nothing matches → stop and
   say so.

2. **Run the gate checks.** `hologram check --json`, filtered to the target
   assets. With a `golden.json` present this includes the tri budgets and
   golden-thumbnail drift automatically. An `error`-severity finding fails the
   gate for that asset; warnings pass with notes.

3. **Stamp the manifest.** For each gated asset, edit its record in
   `exports/manifest.json` (agent-written — Hologram itself never writes):

   ```json
   "review": {"status": "pending-review" | "gate-failed", "version": <record version>, "at": "<ISO-8601Z>"}
   ```

   - A human decision (`approved` / `rejected`) **on the same version stands** —
     do not overwrite it. A version bump re-opens the asset: stamp normally.
   - Preserve every other key in the record; edit surgically.

4. **Log the gate.** Append one line per run to `<export_root>/activity.jsonl`:

   ```json
   {"ts": "<ISO-8601Z>", "actor": "claude", "skill": "asset-gate", "target": "<target>", "note": "P passed, F failed"}
   ```

5. **Report.** Per asset: id, pass/fail, resulting status, and for failures the
   exact checks that missed with their numbers:

   ```
   Gated 3 assets (target: props)
     ✓ props/market_stall   → pending-review
     ✗ props/crane_arm      → gate-failed (1 error)
         - tri budget: 44,120 tris > props budget 30,000
   ```

## On failures: report and STOP

The fix belongs in the generating script or source file, not the exported GLB —
and the user may want to make that call. Do not auto-fix unless explicitly
asked. Never touch `golden.json`: agents build against the goldens; only the
human moves them.
