---
name: Hologram pipeline status
description: Answer "what's wrong right now" from Hologram's event log alone — recent failures, the last check summary, and assets that changed since their last checkpoint. Fast, read-only, works without Blender. Use when someone says "what's the status", "what's broken", "catch me up", "what changed", "what has the agent been doing", or "is anything failing".
allowed-tools: mcp__hologram__pipeline_status, mcp__hologram__tail_events
---

# Hologram pipeline status

A catch-up read. `pipeline_status` scans the recent event log and buckets it
into the three things you usually want to know — no Blender, no re-running
checks, just what already happened.

## What to do

1. **Call `pipeline_status`.** You get back:
   - `failures` — recent failed tool calls, each with its error text
   - `last_check` — the most recent `hologram check` summary (assets / checks /
     errors / warnings)
   - `recent_diffs` — assets whose fingerprint changed since their last
     checkpoint (lost / gained / changed counts)

2. **Lead with the headline.** *"Two failures in the last hour, last check was
   clean, one asset gained a material."* Then the detail. If everything's quiet,
   say so — a calm pipeline is a valid answer.

3. **Need the raw timeline?** `tail_events(limit)` returns recent events
   newest-first if they want the play-by-play instead of the summary.

## Notes

- This reflects the *last emitted* check — it does not run checks itself (the
  MCP server never loads user code). If the summary looks stale, run
  `/hologram:check` to refresh it.
- Diffs are recorded against the baseline from the last `hologram check`, so
  "changed" means "since you last checked".
