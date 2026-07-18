---
name: Create a Hologram skill
description: Scaffold a new project skill that automates one of *your* Hologram workflows — pre-wired to Hologram's MCP tools and CLI so it actually works. Use when someone says "make a hologram skill", "create a skill for X", "turn this into a skill", "automate this hologram workflow", or wants a reusable command for a repeated asset task.
allowed-tools: Write, Read, Glob, Bash(hologram *)
kind: workflow
---

# Create a Hologram skill

Turn a repeated pipeline task into a one-word skill. This writes a project-level
skill (under `.claude/skills/`) that Claude Code auto-discovers — so next time
it's `/your-skill` instead of re-explaining the steps.

## What to do

1. **Figure out the workflow.** What repeated task are they capturing? e.g.
   *"before I commit, render every prop and flag any with no materials"*, or
   *"summarize what changed since this morning"*. Ask once if it's genuinely
   unclear; otherwise infer it from what they just asked for.

2. **Pick a short, lowercase, hyphenated name** — it becomes the command
   (`pre-commit-render` → `/pre-commit-render`).

3. **Write `.claude/skills/<name>/SKILL.md`** using this shape:

   ```markdown
   ---
   name: <Readable display name>
   description: <One or two sentences — what it does and the phrases that should
     trigger it. Put the trigger words early; this is what makes it fire.>
   allowed-tools: <only what it needs, e.g. mcp__hologram__inspect_asset, Bash(hologram *)>
   ---

   # <Title>

   <Numbered steps for the agent to follow, in plain language. Reference the
   Hologram tools below by name. Say how to present the result.>
   ```

   Keep it focused — one workflow per skill. Only list the tools it actually
   uses in `allowed-tools`.

4. **Tell them how to use it.** Project skills load on the next turn; it shows up
   as `/<name>` and fires automatically when the description matches.

## Hologram surface to wire in

Read-only MCP tools (server name `hologram`):

| Tool | Does |
|------|------|
| `list_assets(category?)` | enumerate exported GLBs, grouped by category |
| `inspect_asset(path)` | parse one GLB → nodes, meshes, materials, animations, skins, counts |
| `render_asset(path)` | render one GLB to a PNG via the live Blender (needs Blender on `:9876`) |
| `tail_events(limit?)` | recent activity from the event log, newest first |
| `pipeline_status(limit?)` | failures + last check summary + recent diffs, from the log alone |

CLI (for `Bash`):

| Command | Does |
|---------|------|
| `hologram check [--json]` | run read-only checks over the assets |
| `hologram dashboard` | run the live dashboard (background it) |
| `hologram watch` | stream the event log to the terminal |

## Notes

- These are *read-only* surfaces. Don't author skills that claim to modify or
  delete assets — Hologram observes, it doesn't mutate. `render_asset` is the
  only one that touches Blender, and it's non-destructive (throwaway scene,
  user's scene restored).
- For a deeper, general-purpose skill builder, the `skill-creator` skill exists
  too — this one is the Hologram-flavored shortcut.
