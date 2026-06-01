---
name: Check the Hologram pipeline
description: Run Hologram's read-only checks over the exported assets and explain the results — which assets are clean, which warn or error, and what to do about each. Also helps author custom checks. Use when someone says "check the assets", "validate my exports", "run hologram check", "are my GLBs okay", "what's failing", or wants to add a validation rule.
allowed-tools: Bash(hologram *), Read, Write
---

# Check the Hologram pipeline

`hologram check` runs the project's checks — assertions over every exported GLB
— and reports pass / warn / fail per asset. It emits a `check_run` event, so the
result also surfaces on the dashboard and in `/hologram:status`.

## What to do

1. **Run it.** `hologram check` for a human-readable report, or
   `hologram check --json` if you want to parse the findings. Add
   `--project <dir>` if you're not in the project root.

2. **Read the results back plainly.** Group by outcome: clean assets, warnings,
   errors. For each problem give the asset, the check name, and the message —
   then say what would fix it. A non-zero exit means at least one
   `error`-severity check failed.

3. **If there are no checks yet,** offer to scaffold them: `hologram check
   --init` writes `.hologram/checks.py` with a worked example.

## Authoring a check

Checks live in `.hologram/checks.py`. Each is a function decorated with
`@check(...)` that receives an `asset` and returns nothing / `True` to pass, or
`warn("...")` / `fail("...")` to flag a problem:

```python
from hologram.checks import check, warn, fail

@check("single root node", severity="error")
def one_root(asset):
    if len(asset.roots) > 1:
        return fail(f"{len(asset.roots)} roots — expected a single rig root")
```

The `asset` exposes `.nodes`, `.roots`, `.materials`, `.animations`, `.skins`,
`.mesh_names`, `.stem`, and helpers like `.top_level_node_names()`. Checks are
read-only — they can't modify anything, and they run only here and in the local
dashboard, never in the MCP server.

## Notes

- `check` *runs* validation (it loads the user's `checks.py`).
  `/hologram:status` only *reads* the last result from the log — reach for that
  when you just want to catch up without re-running.
