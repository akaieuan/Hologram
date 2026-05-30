# ◇ hologram

**Live observability + an agent (MCP) surface for Blender → glTF pipelines.**

![status](https://img.shields.io/badge/status-v0.4.0-blue)
![python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)
![license](https://img.shields.io/badge/license-MIT-green)
![MCP](https://img.shields.io/badge/MCP-read--only-6E56CF?logo=anthropic&logoColor=white)
![Blender](https://img.shields.io/badge/Blender-glTF-F5792A?logo=blender&logoColor=white)
![build](https://img.shields.io/badge/build-none-success)

Hologram watches a glTF asset pipeline and streams what's happening to a local
dashboard in real time — including the tool calls your AI coding agent is making
right now. It also exposes the pipeline to agents through a small
[MCP](https://modelcontextprotocol.io) server, so Claude (or any MCP client) can
enumerate, introspect, **render**, and health-check your exported `.glb` assets.

It stays deliberately **read-only / non-destructive**: it observes, introspects,
validates, and previews your pipeline — but it never modifies your assets, and
the MCP server imports none of your project code. No framework, no build step,
no database — a stdlib HTTP server, a JSONL event log, and pure-Python glTF
parsing.

> Why it's different: plenty of tools inspect a `.glb`. Hologram is the only one
> that puts a **live feed of your agent's pipeline activity** next to the assets
> it's producing, and hands that same pipeline to the agent as MCP tools — now
> including a render so the agent can *see* an export, not just count its nodes.

---

## Why I built this

I build games with my friend, and the bulk of my asset work runs through Blender
into glTF — characters, props, weapons, the lot. At some point my AI coding
agent became a real part of that pipeline: it writes the Blender scripts, runs
the exports, and rearranges the `.glb` files I ship. That was a huge speed-up,
right up until I realised I had no real idea what it was *doing*. Assets
changed, exports appeared, and I'd be scrolling back through a terminal trying
to reconstruct which step touched which file.

Hologram is what I built to close that gap. It tails a single event log and
shows the agent's live activity — edits, shell commands, exports — right next
to the assets those actions produce, in one local dashboard. Then it hands the
agent that same pipeline back as a few read-only MCP tools, so we end up looking
at the same picture instead of talking past each other.

It started life as pipeline-specific glue buried inside one game's repo. This is
the clean-room version: none of that project's code and none of its assumptions
— just the pattern, pulled out and made generic, in case it's useful to anyone
else wiring an agent into a Blender → glTF workflow.

## Features

- **Live activity feed** — a Server-Sent-Events dashboard that tails an
  append-only event log. Sessions, shell commands, file edits, MCP calls, and
  slash-command invocations stream in as they happen — failures and in-flight
  work included.
- **Asset visualizer** — browse exported GLBs grouped by category, click any one
  to introspect its glTF structure, and see an in-browser preview of the model.
- **MCP tool surface** — five read-only tools (`list_assets`, `inspect_asset`,
  `render_asset`, `tail_events`, `pipeline_status`) give an agent structured
  access to your pipeline.
- **Agent vision (`render_asset`)** — render a GLB to an image through your live
  Blender, so the agent can *see* an export. Non-destructive (a throwaway scene,
  your scene restored) and degrades to a clear error when Blender isn't running.
- **Read-only checks** — `hologram check` runs assertions you author in
  `.hologram/checks.py` over each asset (naming, root count, whatever you like);
  findings show in the terminal and the dashboard. Checks can't modify anything
  and never run inside the MCP server.
- **Regression diffing** — each `hologram check` fingerprints every asset, so the
  dashboard and `pipeline_status` can answer "what changed since the last check"
  (materials/meshes/animations gained, lost, or changed).
- **`pipeline_status`** — one MCP read of "what's wrong right now": recent
  failures, the last check summary, and recent asset diffs, straight from the log.
- **Pure-Python glTF introspection** — nodes, hierarchy, animations, materials,
  skins, and meshes, parsed with `pygltflib`. No Blender required.
- **`hologram watch`** — a terminal companion that streams the same event log,
  no browser needed.
- **Generic by config** — a single `hologram.toml` describes your paths and an
  *optional* category taxonomy. Flat projects need no categories at all.
- **Blender MCP awareness** — the dashboard probes for a Blender MCP add-on
  (TCP `:9876`) and shows whether it's live; `render_asset` drives that add-on.
- **Claude Code activity hook** — a generic event-logging hook (ships as a
  plugin) feeds the dashboard with your agent's real activity.

## Requirements

- [**uv**](https://docs.astral.sh/uv/) — it fetches Hologram (and a matching
  Python 3.10+) on demand, so there's nothing to install or build. `brew install
  uv`, or `curl -LsSf https://astral.sh/uv/install.sh | sh`.
- **Claude Code** — optional, but it's what the plugin (live activity feed + MCP
  tools) plugs into.
- **Blender** — optional. Needed for `render_asset` (and the live-status probe),
  via the Blender MCP add-on listening on `:9876`. Hologram itself never imports
  `bpy` — it drives Blender over a socket, so its import purity stays intact.

## Run it

No install, no build step — `uv` fetches and runs Hologram on demand, then
caches it. There are two entry points:

**1 · The dashboard** — point it at any project that has exported GLBs:

```bash
uvx --from git+https://github.com/akaieuan/Hologram hologram dashboard
```

It serves **http://127.0.0.1:7870**. (Once Hologram is on PyPI this shortens to
`uvx hologram dashboard`.)

**2 · The Claude Code plugin** — the live activity feed + MCP tools. Inside
Claude Code:

```text
/plugin marketplace add akaieuan/Hologram
/plugin install hologram
```

That wires up two things: the **activity hook** (your agent's edits, shell
commands, and MCP calls stream into the dashboard) and the five read-only
**MCP tools**. The MCP server runs through `uvx` too — installing the plugin
needs no `pip` step.

### Commands

| Command | What it does |
|---|---|
| `hologram dashboard` | Run the live SSE dashboard (`--host`, `--port`, `--project`). |
| `hologram watch` | Stream the event log to the terminal — no browser (`--limit`, `--interval`, `--no-color`). |
| `hologram check` | Run read-only checks over the exported assets (`--json`, `--init` to scaffold). |
| `hologram mcp` | Run the MCP server over stdio (Claude Code launches this for you). |
| `hologram init` | Scaffold `hologram.toml` + `.mcp.json` in a project (`--force`). |

## How it's delivered

Hologram is three pieces, and each reaches you without a manual install:

| Piece | What it is | How it runs |
|---|---|---|
| **Activity hook** | a stdlib-only Claude Code hook logging sessions, shell commands, edits, and MCP calls | bundled in the plugin; runs on your system `python3`, zero dependencies |
| **MCP server** | five read-only tools over your GLBs | launched by `uvx` straight from this repo — Claude Code starts it per session |
| **Dashboard** | the live SSE web UI | a server you start with `uvx hologram dashboard` |

`uv` downloads the code (and a matching Python) the first time and caches it, so
there's no release to download and no environment to maintain. The marketplace
install wires the hook + MCP in one step.

The dashboard is the one piece you launch yourself — by design. A Claude Code
plugin contributes hooks, commands, and MCP servers, not long-running web
servers, so the dashboard stays a `uvx hologram dashboard` you run in a terminal
when you want eyes on the pipeline. Same UI, same command — only the delivery is
install-free.

> **Prefer a classic install?** From a clone, `pip install -e .` (or `uv pip
> install -e .`) still gives you a plain `hologram` command. And publishing to
> PyPI (`uv build && uv publish`) turns every `uvx --from git+… hologram` here
> into a tidy `uvx hologram`.

## Try the bundled examples

Want to poke at it with real assets first? Clone the repo and run the dashboard
against the example project — still no install:

```bash
git clone https://github.com/akaieuan/Hologram.git
cd Hologram
uvx --from . hologram dashboard --project examples/minimal
```

Open **http://127.0.0.1:7870** — three sample assets grouped into `lootables`,
`weapons`, and `props`. Click any asset to introspect its glTF structure and
preview the model; switch to **Debug** for copy-pastable JSON state.

To watch the live feed move, append an event to the log:

```bash
echo '{"ts": '"$(date +%s)"', "type": "tool_use", "tool": "Bash", "command": "blender --background bake.blend"}' \
  >> examples/minimal/.hologram/events.jsonl
```

It shows up in the **Live** tab within a second.

## Use it in your own project

From your pipeline's root:

```bash
uvx --from git+https://github.com/akaieuan/Hologram hologram init
```

That writes a `hologram.toml` (edit `export_root` to point at wherever your
`.glb` files land) plus a `.mcp.json`. Then launch the dashboard the same way:

```bash
uvx --from git+https://github.com/akaieuan/Hologram hologram dashboard
```

To author your own validation rules, scaffold a checks file and edit it:

```bash
uvx --from git+https://github.com/akaieuan/Hologram hologram check --init
```

## The MCP server

Installing the plugin wires the MCP server up for you — there's nothing else to
do. If you'd rather register it by hand (no plugin), `hologram init` drops a
`.mcp.json` that points Claude Code at the server through `uvx`:

```json
{
  "mcpServers": {
    "hologram": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/akaieuan/Hologram", "hologram", "mcp"]
    }
  }
}
```

`uvx` fetches and caches the server on first use, so this needs no `pip` step.
(Once Hologram is on PyPI the args shorten to just `["hologram", "mcp"]`.) Either
way, an agent gets five tools:

| Tool | What it does |
|---|---|
| `list_assets(category?)` | Enumerate exported GLBs, grouped by category. |
| `inspect_asset(path)` | Parse one GLB into the flat `Asset` struct (nodes, animations, materials, skins…). |
| `render_asset(path)` | Render a GLB to a PNG via the live Blender — see an export, not just its node counts. Non-destructive; returns a clear `{error, hint}` if Blender isn't reachable. |
| `tail_events(limit?)` | Read recent pipeline activity from the shared event log. |
| `pipeline_status(limit?)` | "What's wrong right now" — recent failures + the last check summary + recent asset diffs, from one read of the log. |

The server imports no project code — it only reads files and your config (never
`.hologram/checks.py`). `render_asset` drives a *separate, running* Blender over
a socket, which isn't importing your code, so that purity holds.

## Live agent activity (the plugin)

The dashboard's feed renders activity that something has to *log*. Hologram ships
a generic Claude Code hook (under `hologram/plugin/`) that records session
lifecycle, shell commands, edits to `.glb` / `.gltf` / `.py` / `.toml` files, and
`mcp__hologram*` / `mcp__blender*` tool calls — then the dashboard streams them.

Installing the plugin wires both the hook and the MCP server in one step:

```text
/plugin marketplace add akaieuan/Hologram
/plugin install hologram
```

Or, if you keep your own hook setup, copy `hologram/plugin/hooks/log_event.py`
into it. The hook is stdlib-only and writes straight to your configured
`events_log`.

## Configuration

`hologram.toml` lives at your project root:

```toml
[project]
name = "my-project"

[paths]
export_root = "export/gltf"            # root for GLB discovery (required)
source_root = "blender/scripts"        # optional; links a script to its GLB
events_log  = ".hologram/events.jsonl" # optional; default shown

[dashboard]
host = "127.0.0.1"                      # optional
port = 7870                             # optional

# [categories] is OPTIONAL. Omit it entirely and every GLB under export_root
# becomes one flat "all" list. Otherwise each category is a glob:
[categories.props]
glb_pattern    = "props/**/*.glb"       # relative to export_root
script_pattern = "props/*.py"           # optional, relative to source_root
```

See `examples/minimal/` (categorized) and `examples/flat-layout/` (no categories)
for both shapes running on the same dashboard.

## How it works

```
your pipeline ──┐
Claude Code  ───┼──> .hologram/events.jsonl ──> dashboard (SSE live tail)
MCP server   ──┘                                    │
                                                    └─> /api/inspect ─> pygltflib
live Blender ──(socket :9876)── render_asset ─> PNG
```

- **`hologram/events.py`** — append-only JSONL log; the dashboard tails it by
  watching byte size and emitting each new line over SSE.
- **`hologram/gltf.py`** — the stable `Asset` public API (bpy-free).
- **`hologram/config.py`** — loads `hologram.toml`; categories are optional.
- **`hologram/checks.py`** — the read-only checks engine + per-asset diff
  baseline (refreshed only on `hologram check`, so reads stay pure).
- **`hologram/diff.py`** — asset fingerprint + diff ("what changed since last check").
- **`hologram/blender.py`** — stdlib socket client for the Blender MCP add-on
  (liveness probe + the non-destructive render).
- **`hologram/dashboard/`** — stdlib `ThreadingHTTPServer` + SSE + vanilla JS.
- **`hologram/mcp/`** — `FastMCP` server with the five read-only tools.

## Project layout

```
.claude-plugin/marketplace.json   # makes the repo installable via /plugin
hologram/
  gltf.py  events.py  config.py  cli.py
  checks.py  diff.py  blender.py  watch.py
  dashboard/   server.py + static/{index.html,app.js,style.css}
  mcp/         server.py (list_assets, inspect_asset, render_asset,
               tail_events, pipeline_status)
  plugin/      Claude Code hook + plugin manifest + .mcp.json (uvx)
examples/
  minimal/      categorized: export/gltf/{lootables,weapons,props}/*.glb
  flat-layout/  no categories: assets/*.glb
tests/          pytest suite
```

## Roadmap

Hologram stays scoped to **observe, introspect, validate, and preview** — all
read-only / non-destructive. Shipped so far: the live dashboard + MCP surface
(v0.1), failures + GLB previews + `hologram watch` (v0.2), the read-only checks
engine + asset visualizer (v0.3), and agent vision + regression diffing +
`pipeline_status` (v0.4). Still on the table, built on the stable `Asset` API:
an offline/headless render path (no running Blender), render thumbnails on disk,
asset/scene templates, and export orchestration.

## License

[MIT](LICENSE)
