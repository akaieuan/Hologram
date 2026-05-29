# ◇ hologram

**Live observability + an agent (MCP) surface for Blender → glTF pipelines.**

![status](https://img.shields.io/badge/status-v0.1.0-blue)
![python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)
![license](https://img.shields.io/badge/license-MIT-green)
![MCP](https://img.shields.io/badge/MCP-read--only-6E56CF?logo=anthropic&logoColor=white)
![Blender](https://img.shields.io/badge/Blender-glTF-F5792A?logo=blender&logoColor=white)
![build](https://img.shields.io/badge/build-none-success)

Hologram watches a glTF asset pipeline and streams what's happening to a local
dashboard in real time — including the tool calls your AI coding agent is making
right now. It also exposes the pipeline to agents through a small, read-only
[MCP](https://modelcontextprotocol.io) server, so Claude (or any MCP client) can
enumerate and introspect your exported `.glb` assets without touching Blender.

It is deliberately small. v0.1 **observes**; it does not drive Blender, validate,
or export. No framework, no build step, no database — a stdlib HTTP server, a
JSONL event log, and pure-Python glTF parsing.

> Why it's different: plenty of tools inspect a `.glb`. Hologram is the only one
> that puts a **live feed of your agent's pipeline activity** next to the assets
> it's producing, and hands that same pipeline to the agent as MCP tools.

<!-- ![hologram dashboard](docs/dashboard.png) -->

---

## Why I built this

I build games mostly solo, and the bulk of my asset work runs through Blender
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
  slash-command invocations stream in as they happen.
- **MCP tool surface** — three read-only tools (`list_assets`, `inspect_asset`,
  `tail_events`) give an agent structured access to your exported GLBs.
- **Pure-Python glTF introspection** — nodes, hierarchy, animations, materials,
  skins, and meshes, parsed with `pygltflib`. No Blender required.
- **Generic by config** — a single `hologram.toml` describes your paths and an
  *optional* category taxonomy. Flat projects need no categories at all.
- **Blender MCP awareness** — the dashboard probes for a Blender MCP add-on
  (TCP `:9876`) and shows whether it's live.
- **Claude Code activity hook** — a generic event-logging hook (ships as a
  plugin) feeds the dashboard with your agent's real activity.

## Requirements

- Python **3.10+**
- That's it. Blender is optional (only needed if you also run the Blender MCP
  add-on; Hologram itself never imports `bpy`).

## Install

```bash
git clone https://github.com/akaieuan/Hologram.git
cd Hologram
pip install -e .
hologram --version          # -> hologram 0.1.0
```

Every command below is also available as `python -m hologram …` — handy if the
`hologram` script isn't on your `PATH`.

## Quickstart (≈1 minute)

A categorized example project ships in the repo. Point the dashboard at it:

```bash
cd examples/minimal
hologram dashboard          # serves http://127.0.0.1:7870
```

Open **http://127.0.0.1:7870**. You'll see three sample assets grouped into
`lootables`, `weapons`, and `props`. Click any asset to introspect its glTF
structure. Switch to **Debug** for copy-pastable JSON state.

To watch the live feed do something, append an event to the log:

```bash
echo '{"ts": '"$(date +%s)"', "type": "tool_use", "tool": "Bash", "command": "blender --background bake.blend"}' \
  >> .hologram/events.jsonl
```

It appears in the **Live** tab within a second.

## Use it in your own project

```bash
cd /path/to/your/pipeline
hologram init               # writes hologram.toml + .mcp.json
hologram dashboard
```

Edit `hologram.toml` to point `export_root` at wherever your `.glb` files land.

## The MCP server

`hologram init` drops a `.mcp.json` that registers the server with Claude Code:

```json
{
  "mcpServers": {
    "hologram": {
      "command": "hologram",
      "args": ["mcp"]
    }
  }
}
```

Once registered, an agent gets three tools:

| Tool | What it does |
|---|---|
| `list_assets(category?)` | Enumerate exported GLBs, grouped by category. |
| `inspect_asset(path)` | Parse one GLB into the flat `Asset` struct (nodes, animations, materials, skins…). |
| `tail_events(limit?)` | Read recent pipeline activity from the shared event log. |

The server imports no project code — it only reads files and your config.

## Live agent activity (the plugin)

The dashboard's feed renders activity that something has to *log*. Hologram ships
a generic Claude Code hook (under `hologram/plugin/`) that records session
lifecycle, shell commands, edits to `.glb` / `.gltf` / `.py` / `.toml` files, and
`mcp__hologram*` / `mcp__blender*` tool calls — then the dashboard streams them.

Point Claude Code at the bundled plugin (it wires both the hook and the MCP
server), or copy `hologram/plugin/hooks/log_event.py` into your own hook setup.
The hook is stdlib-only and writes straight to your configured `events_log`.

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
```

- **`hologram/events.py`** — append-only JSONL log; the dashboard tails it by
  watching byte size and emitting each new line over SSE.
- **`hologram/gltf.py`** — the stable `Asset` public API (bpy-free).
- **`hologram/config.py`** — loads `hologram.toml`; categories are optional.
- **`hologram/dashboard/`** — stdlib `ThreadingHTTPServer` + SSE + vanilla JS.
- **`hologram/mcp/`** — `FastMCP` server with the three read-only tools.

## Project layout

```
hologram/
  gltf.py  events.py  config.py  cli.py
  dashboard/   server.py + static/{index.html,app.js,style.css}
  mcp/         server.py (list_assets, inspect_asset, tail_events)
  plugin/      Claude Code hook + plugin manifest
examples/
  minimal/      categorized: export/gltf/{lootables,weapons,props}/*.glb
  flat-layout/  no categories: assets/*.glb
tests/          pytest smoke suite
```

## Roadmap

v0.1 is intentionally scoped to **observe**. Planned for later versions:
validation rules, asset/scene templates, and export orchestration — built on the
stable `Asset` API this release establishes.

## License

[MIT](LICENSE)
