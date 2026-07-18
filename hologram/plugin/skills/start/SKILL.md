---
name: Get started with Hologram
description: Onboard to Hologram — explain what it is, scaffold the project if it isn't set up, launch the live dashboard, and hand off to the other Hologram skills and MCP tools. Use when someone asks "what is hologram", "set up hologram", "get started with hologram", "open the hologram dashboard", or is touching a Blender → glTF asset pipeline in this repo for the first time.
allowed-tools: Bash(hologram *), Glob, Read
kind: lifecycle
---

# Get started with Hologram

Hologram gives you eyes on an AI-driven Blender → glTF pipeline. It works two
ways at once, and most people use both:

- **Watch** — a local dashboard in the browser (default
  `http://127.0.0.1:7870`) that streams what the agent is doing and flags
  failures as they happen. Keeps a human in the loop, visually.
- **Drive** — read-only MCP tools and these skills, so the agent can inspect,
  render, validate, and report on assets through natural language.

Both surfaces share one event log, so whatever happens in one shows up in the
other.

## What to do

1. **If they're new, explain the two ways above in a sentence or two.** Don't
   lecture — get them to something working fast.

2. **Check the project is initialized.** Look for `hologram.toml` at the repo
   root (Glob). If it's missing, run `hologram init` — it writes `hologram.toml`
   (paths + dashboard config) and `.mcp.json` (wires the read-only MCP tools
   into Claude Code via `uvx`, no install step). Tell them what landed, and that
   the MCP tools become available once Claude Code picks up `.mcp.json` — a
   reload/restart if it was just created.

3. **Point `export_root` at their GLBs.** Open `hologram.toml` and confirm
   `[paths] export_root` matches where their `.glb` files actually live. This is
   the one setting everything else depends on.

4. **Launch the dashboard.** Run `hologram dashboard` in the background so it
   keeps running, then give them the URL (`http://127.0.0.1:7870` unless they
   set a different port). Mention `hologram watch` as the terminal-only
   alternative if they'd rather not open a browser.

5. **Hand off.** Point them at what's next:
   - `/hologram:inspect` — look at an asset (structure + a rendered preview)
   - `/hologram:check` — validate exports and read the failures
   - `/hologram:status` — catch up on what's wrong or what changed
   - `/hologram:create-skill` — capture one of *their* workflows as a new skill

## Notes

- Don't start a second dashboard if one is already running on the port — check
  first.
- Everything Hologram's MCP server does is read-only. The one tool that touches
  Blender (`render_asset`) renders in a throwaway scene and restores the user's
  working scene — it never modifies their work.
