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


CHECKS_TEMPLATE = '''\
"""Project checks for hologram — read-only assertions over each exported asset.

Run them with `hologram check` (also shown per-asset in the dashboard). Each
function receives an `asset` (a hologram.gltf.Asset: .nodes, .roots, .materials,
.animations, .skins, .mesh_names, .stem, helpers like .top_level_node_names()).

Return None or True to pass; return warn("...") or fail("...") to flag a problem.
These run only in your own `hologram check` and local dashboard — never in the
MCP server. They cannot modify anything.
"""

from hologram.checks import check, warn, fail


@check("texture-friendly name")
def lowercase_stem(asset):
    if asset.stem != asset.stem.lower():
        return warn(f"'{asset.stem}' has uppercase — prefer lowercase asset names")


# @check("single root node", severity="error")
# def one_root(asset):
#     if len(asset.roots) > 1:
#         return fail(f"{len(asset.roots)} roots — expected a single rig root")
'''


def format_check_report(report: dict, color: bool = True) -> str:
    """Render a `run_project` report as a terminal block. Pure — deterministic
    for a given report, so it is unit-tested with color disabled."""
    from .watch import RED, GREEN, YELLOW, DIM, RESET

    def paint(text: str, code: str) -> str:
        return f"{code}{text}{RESET}" if color and code else text

    def plural(n: int, word: str) -> str:
        return f"{n} {word}{'' if n == 1 else 's'}"

    s = report["summary"]
    lines = [paint(f"{plural(s['checks'], 'check')} · {plural(s['assets'], 'asset')}", DIM), ""]

    for r in report["results"]:
        problems = r["findings"]
        has_err = any(f["severity"] == "error" for f in problems)
        mark = paint("✗", RED) if has_err else (paint("⚠", YELLOW) if problems else paint("✓", GREEN))
        label = f"{r['category']}/{r['asset']}" if r.get("category") else r["asset"]
        lines.append(f"  {mark} {label}")
        for f in problems:
            err = f["severity"] == "error"
            lines.append(paint(f"      {'✗' if err else '⚠'} {f['check']} · {f['message']}",
                               RED if err else YELLOW))

    summary = f"{s['clean']} clean · {plural(s['warnings'], 'warning')} · {plural(s['errors'], 'error')}"
    tone = RED if s["errors"] else (YELLOW if s["warnings"] else GREEN)
    lines += ["", "  " + paint(summary, tone)]
    if report.get("load_error"):
        lines.append(paint(f"  ! checks file failed to load: {report['load_error']}", RED))
    return "\n".join(lines)


def _check(project: str | None, as_json: bool, do_init: bool) -> int:
    import sys

    from . import checks as checks_mod
    from .config import load_config

    cfg = load_config(project)
    if do_init:
        dest = checks_mod.project_checks_path(cfg)
        if dest.exists():
            print(f"hologram check: {cfg.rel(dest)} already exists — edit it directly.")
            return 0
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(CHECKS_TEMPLATE, encoding="utf-8")
        print(f"hologram check: wrote {cfg.rel(dest)}")
        print("Edit it, then run `hologram check`.")
        return 0

    report = checks_mod.run_project(cfg, emit=True)
    if as_json:
        import json
        print(json.dumps(report, indent=2))
    else:
        print(f"hologram check · {cfg.name}")
        print(format_check_report(report, color=bool(getattr(sys.stdout, "isatty", lambda: False)())))
    return 1 if report["summary"]["errors"] else 0


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

    p_check = sub.add_parser("check", help="Run read-only checks over the exported assets.")
    p_check.add_argument("--project", default=None, help="Project root (default: cwd / nearest hologram.toml).")
    p_check.add_argument("--json", action="store_true", dest="as_json", help="Emit a machine-readable JSON report.")
    p_check.add_argument("--init", action="store_true", help="Scaffold .hologram/checks.py and exit.")

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
    if args.command == "check":
        return _check(args.project, args.as_json, args.init)
    if args.command == "init":
        return _init(args.directory, force=args.force)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
