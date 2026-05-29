"""Hologram MCP server — a small, read-only agent surface over a glTF pipeline.

Three tools cover observation of the pipeline without driving it (v0.1 is
read-only by design):

  * list_assets(category?)  — enumerate exported GLBs, grouped by category
  * inspect_asset(path)     — parse one GLB into the `Asset` struct as JSON
  * tail_events(limit?)     — recent activity from the shared event log

Discipline preserved from the reference design: this process imports no user
project code. It only reads files and the config. Launched by Claude Code via
`.mcp.json` (stdio transport).
"""

from __future__ import annotations

import uuid
from typing import Optional

from .. import events
from ..config import Config, load_config
from ..gltf import load_asset

# Stable per-process id so the dashboard can distinguish this server instance
# from Claude-session ids. The `mcp-` prefix makes it obvious in the UI.
_SERVER_SID = f"mcp-{uuid.uuid4().hex[:8]}"


def _cfg() -> Config:
    """Resolve the active project config. Cheap enough to re-read per call,
    which keeps the tools correct if the user edits hologram.toml live."""
    return load_config()


def _emit(action: str, detail: str = "", **extra) -> None:
    """Append a live event so the dashboard reflects MCP activity."""
    try:
        events.append(
            _cfg().events_log,
            "mcp_server",
            action=action,
            detail=detail,
            session_id=_SERVER_SID,
            **extra,
        )
    except Exception:
        pass


# ── Tool logic (plain functions; easy to unit-test) ────────────────────────────

def _list_assets(category: Optional[str] = None) -> dict:
    cfg = _cfg()
    by_cat = cfg.list_glbs(category)
    result: dict[str, list[dict]] = {}
    total = 0
    for cat in cfg.categories:
        if category and cat.name != category:
            continue
        entries: list[dict] = []
        for glb in by_cat.get(cat.name, []):
            script = cfg.find_script_for(glb, cat)
            try:
                mtime = glb.stat().st_mtime
            except OSError:
                mtime = None
            entries.append(
                {
                    "name": glb.stem,
                    "glb": cfg.rel(glb),
                    "mtime": mtime,
                    "script": cfg.rel(script) if script else None,
                }
            )
        total += len(entries)
        result[cat.name] = entries
    return {"project": cfg.name, "root": str(cfg.root), "total": total, "categories": result}


def _inspect_asset(path: str) -> dict:
    cfg = _cfg()
    resolved = cfg.resolve_asset(path)
    if resolved is None:
        return {"error": f"asset not found within project: {path}", "path": path}
    try:
        asset = load_asset(str(resolved))
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "path": cfg.rel(resolved)}
    data = asset.to_dict()
    data["path"] = cfg.rel(resolved)
    return data


def _tail_events(limit: int = 50) -> dict:
    cfg = _cfg()
    return {"events": events.tail(cfg.events_log, limit=limit)}


# ── MCP server setup ────────────────────────────────────────────────────────────

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as e:  # pragma: no cover - dependency guard
    raise SystemExit("The 'mcp' package is required. Install with: pip install hologram") from e


mcp = FastMCP("hologram")


@mcp.tool()
def list_assets(category: Optional[str] = None) -> dict:
    """List exported GLB assets, grouped by the categories defined in hologram.toml.

    Args:
        category: Optional category name to filter to. If omitted, every
                  configured category is returned. Projects with no [categories]
                  expose a single "all" category.

    Returns:
        {"project": str, "root": str, "total": int,
         "categories": {name: [{"name", "glb", "mtime", "script"}, ...]}}
    """
    out = _list_assets(category)
    _emit("list_assets", f"{out['total']} assets", count=out["total"])
    return out


@mcp.tool()
def inspect_asset(path: str) -> dict:
    """Parse a single GLB into Hologram's flat Asset structure.

    Args:
        path: Path to the .glb (absolute, or relative to the project root or
              export_root). Must resolve inside the project.

    Returns:
        The Asset as JSON: nodes (hierarchy with parent/children/mesh/skin/
        translation/extras), roots, top_level_nodes, animations, materials,
        mesh_names, skins, and counts. On failure: {"error": str, "path": str}.
    """
    out = _inspect_asset(path)
    if "error" in out:
        _emit("inspect_asset.error", out["error"])
    else:
        _emit("inspect_asset", f"{out.get('filename', path)} · {out.get('node_count', 0)} nodes")
    return out


@mcp.tool()
def tail_events(limit: int = 50) -> dict:
    """Return recent pipeline activity from the shared event log.

    Args:
        limit: Maximum number of events to return, newest first (default 50).

    Returns:
        {"events": [event dicts]}
    """
    return _tail_events(limit)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    """Run the MCP server over stdio."""
    cfg = _cfg()
    _emit("mcp.start", f"server {_SERVER_SID} online at {cfg.root}")
    try:
        mcp.run()
    finally:
        _emit("mcp.stop", f"server {_SERVER_SID} stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
