---
name: Inspect a Hologram asset
description: Look at a glTF/GLB asset through Hologram — resolve which file they mean, read its structure (nodes, meshes, materials, animations, skins), and render a PNG preview via the live Blender so you can actually see it. Use when someone says "what's in hero.glb", "show me that asset", "inspect this model", "render the sword", "how many materials does X have", or wants to understand an export without opening Blender.
allowed-tools: mcp__hologram__list_assets, mcp__hologram__inspect_asset, mcp__hologram__render_asset
kind: workflow
---

# Inspect a Hologram asset

Bridge the two ways of understanding an export: its **structure** (what's in the
file) and its **picture** (what it looks like).

## What to do

1. **Resolve the asset.** If they named it loosely ("the hero", "that sword"),
   call `list_assets` to find the matching `.glb` and its path. If they gave a
   path, use it directly.

2. **Read the structure** with `inspect_asset(path)`. It returns the flat Asset:
   node hierarchy (parent / children / mesh / skin / transform), root nodes,
   top-level nodes, materials, mesh names, animations, skins, and counts. Don't
   dump the raw JSON — summarize it like a person would: *"12 nodes under one
   root, 3 materials (body, eyes, metal), one idle animation, rigged with a
   28-bone skin."* Call out anything that looks off (no materials, dozens of
   roots, empty meshes).

3. **Show it, if useful.** Call `render_asset(path)` for a PNG preview from the
   live Blender — the agent's version of the visual the dashboard gives a human.
   - On success you get an image back — describe what you actually see.
   - If it returns `{"error", "hint"}`, Blender isn't reachable. Say so plainly
     and pass on the hint (start Blender with the MCP add-on on `:9876`). Don't
     treat it as a hard failure — the structure read still stands on its own.

4. **Answer the actual question.** If they asked "how many materials", lead with
   the number. Inspect and render are the means, not the deliverable.

## Notes

- `render_asset` is non-destructive: it renders in a throwaway scene and
  restores the user's working scene. Safe against a live Blender session.
- Paths resolve relative to the project root or `export_root`, and must stay
  inside the project.
