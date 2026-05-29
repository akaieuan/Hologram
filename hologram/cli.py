"""Command-line entry point: `hologram {dashboard,mcp,init}`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__

DEFAULT_CONFIG = """\
[project]
name = "my-project"

[paths]
# Root that GLB assets are discovered under (relative to this file's directory).
export_root = "export/gltf"
# Optional: where source scripts live, used only to link a script to its GLB.
source_root = "blender/scripts"
# Where the live event log is written and tailed from.
events_log = ".hologram/events.jsonl"

[dashboard]
host = "127.0.0.1"
port = 7870

# Categories are OPTIONAL. Omit this whole section to treat every GLB under
# export_root as one flat list. Otherwise each category maps a glob of GLBs
# (relative to export_root) and, optionally, source scripts (relative to
# source_root) so the dashboard can link a script to its exported asset.
#
# [categories.props]
# glb_pattern = "props/**/*.glb"
# script_pattern = "props/*.py"
"""

DEFAULT_MCP_JSON = """\
{
  "mcpServers": {
    "hologram": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/akaieuan/Hologram", "hologram", "mcp"]
    }
  }
}
"""


def _init(directory: str, force: bool = False) -> int:
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    wrote = []
    skipped = []
    for name, content in (("hologram.toml", DEFAULT_CONFIG), (".mcp.json", DEFAULT_MCP_JSON)):
        dest = root / name
        if dest.exists() and not force:
            skipped.append(name)
            continue
        dest.write_text(content, encoding="utf-8")
        wrote.append(name)

    if wrote:
        print(f"hologram init: wrote {', '.join(wrote)} in {root}")
    if skipped:
        print(f"hologram init: skipped existing {', '.join(skipped)} (use --force to overwrite)")
    print("Next: `hologram dashboard` to view the pipeline. .mcp.json wires the read-only MCP tools into Claude Code (launched via uvx — no install needed).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hologram",
        description="Live observability + an agent (MCP) surface for Blender -> glTF pipelines.",
    )
    parser.add_argument("--version", action="version", version=f"hologram {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_dash = sub.add_parser("dashboard", help="Run the live dashboard server.")
    p_dash.add_argument("--host", default=None, help="Override host (default from config or 127.0.0.1).")
    p_dash.add_argument("--port", type=int, default=None, help="Override port (default from config or 7870).")
    p_dash.add_argument("--project", default=None, help="Project root (default: cwd / nearest hologram.toml).")

    sub.add_parser("mcp", help="Run the MCP server over stdio (launched by Claude Code).")

    p_watch = sub.add_parser("watch", help="Stream the event log to the terminal (no browser).")
    p_watch.add_argument("--project", default=None, help="Project root (default: cwd / nearest hologram.toml).")
    p_watch.add_argument("--limit", type=int, default=20, help="Recent events to backfill before streaming (default: 20).")
    p_watch.add_argument("--interval", type=float, default=1.0, help="Poll interval in seconds (default: 1.0).")
    p_watch.add_argument("--no-color", action="store_true", help="Disable ANSI color (auto-off when piped).")

    p_init = sub.add_parser("init", help="Scaffold hologram.toml + .mcp.json in a project.")
    p_init.add_argument("directory", nargs="?", default=".", help="Target directory (default: cwd).")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing files.")

    args = parser.parse_args(argv)

    if args.command == "dashboard":
        from .dashboard.server import run
        return run(host=args.host, port=args.port, project=args.project)
    if args.command == "mcp":
        from .mcp.server import main as mcp_main
        return mcp_main()
    if args.command == "watch":
        from .config import load_config
        from .watch import run as watch_run
        cfg = load_config(args.project)
        return watch_run(
            cfg,
            limit=args.limit,
            interval=args.interval,
            color=False if args.no_color else None,
        )
    if args.command == "init":
        return _init(args.directory, force=args.force)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
