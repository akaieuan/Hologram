"""Hologram MCP server — a small, read-only agent surface over a glTF pipeline.

The tools observe the pipeline without mutating it (read-only by design); the
one exception, `render_asset`, drives a *live* Blender non-destructively (a
throwaway scene, the user's scene restored) to produce a preview image:

  * list_assets(category?)  — enumerate exported GLBs, grouped by category
  * inspect_asset(path)     — parse one GLB into the `Asset` struct as JSON
  * render_asset(path)      — render one GLB to a PNG via the live Blender
  * tail_events(limit?)     — recent activity from the shared event log
  * pipeline_status(limit?) — "what's wrong right now": failures + last check
                              summary + recent asset diffs, from the log alone

Discipline preserved from the reference design: this process imports no user
project code. It only reads files and the config (never `.hologram/checks.py`);
driving a separate running Blender over a socket is not importing user code.
Launched by Claude Code via `.mcp.json` (stdio transport).
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

from .. import blender, events
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

def _list_assets(category: str | None = None) -> dict:
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


def _pipeline_status(limit: int = 200, max_items: int = 10) -> dict:
    """Aggregate pipeline health from the event log alone.

    PURITY: this reads the log and nothing else. It must reflect the *last
    emitted* ``check_run`` — it must NOT call ``run_project`` / ``all_checks``,
    because those load ``.hologram/checks.py`` (user code) and this server is
    forbidden to import user project code. Do not "helpfully" run checks here.
    """
    cfg = _cfg()
    evs = events.tail(cfg.events_log, limit=limit)  # newest first
    failures: list[dict] = []
    recent_diffs: list[dict] = []
    last_check: dict | None = None
    for ev in evs:
        t = ev.get("type")
        if t == "tool_use" and ev.get("failed"):
            if len(failures) < max_items:
                failures.append({
                    "tool": ev.get("tool") or ev.get("mcp_tool"),
                    "target": ev.get("command") or ev.get("file_path") or "",
                    "error": ev.get("error"),
                    "is_interrupt": bool(ev.get("is_interrupt")),
                    "ts": ev.get("ts"),
                })
        elif t == "check_run" and last_check is None:
            last_check = {
                "assets": ev.get("assets", 0),
                "checks": ev.get("checks", 0),
                "errors": ev.get("errors", 0),
                "warnings": ev.get("warnings", 0),
                "ts": ev.get("ts"),
            }
        elif t == "asset_diff":
            if len(recent_diffs) < max_items:
                recent_diffs.append({
                    "path": ev.get("path"),
                    "lost": ev.get("lost", {}),
                    "gained": ev.get("gained", {}),
                    "changed": ev.get("changed", {}),
                    "ts": ev.get("ts"),
                })
    return {
        "project": cfg.name,
        "failures": failures,
        "failure_count": len(failures),
        "last_check": last_check,
        "recent_diffs": recent_diffs,
        "generated_at": time.time(),
    }


def _render_asset(path: str) -> dict:
    """Render one GLB to PNG bytes via the live Blender instance (Move 1).

    Read-only toward the project and non-destructive toward Blender:
    ``blender.render_glb`` drives a throwaway scene in the running Blender and
    restores the user's scene afterward. We own the temp PNG it writes — read
    the bytes, then unlink. Never raises; on any failure (Blender unreachable /
    import error / timeout) returns a structured error dict with a hint.
    """
    cfg = _cfg()
    resolved = cfg.resolve_asset(path)
    if resolved is None:
        return {"error": f"asset not found within project: {path}", "path": path}
    result = blender.render_glb(str(resolved))
    if not result.get("ok"):
        return {
            "error": result.get("error") or "render failed",
            "path": cfg.rel(resolved),
            "hint": f"Start Blender with the MCP add-on listening on :{blender.PORT}, then retry.",
        }
    img_path = Path(result["image_path"])
    try:
        data = img_path.read_bytes()
    except OSError as e:
        return {"error": f"could not read render output: {e}", "path": cfg.rel(resolved)}
    finally:
        try:
            img_path.unlink()
        except OSError:
            pass
    return {"ok": True, "data": data, "path": cfg.rel(resolved)}


# ── MCP server setup ────────────────────────────────────────────────────────────

try:
    from mcp.server.fastmcp import FastMCP, Image
except ImportError as e:  # pragma: no cover - dependency guard
    raise SystemExit("The 'mcp' package is required. Install with: pip install hologram") from e


mcp = FastMCP("hologram")


@mcp.tool()
def list_assets(category: str | None = None) -> dict:
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


@mcp.tool()
def pipeline_status(limit: int = 200) -> dict:
    """Summarize what's wrong with the pipeline right now — one read of the log.

    Aggregates, newest-first:
      * failures     — recent failed tool calls, each with its error text
      * last_check   — the most recent `hologram check` summary
                       (assets / checks / errors / warnings)
      * recent_diffs — assets whose fingerprint changed since their last
                       checkpoint (lost / gained / changed counts)

    Args:
        limit: How many recent events to scan, newest first (default 200).

    Returns:
        {"project", "failures", "failure_count", "last_check", "recent_diffs",
         "generated_at"}.

    Note: this reflects the *last emitted* check_run from the log; it does not
    run checks (the MCP server never imports user project code).
    """
    out = _pipeline_status(limit)
    _emit("pipeline_status", f"{out['failure_count']} recent failures",
          failures=out["failure_count"])
    return out


@mcp.tool()
def render_asset(path: str):
    """Render a GLB to a PNG image using the live Blender instance — let the
    agent *see* an export, not just count its nodes.

    Drives the running Blender (the same socket the dashboard probes) to render
    the asset in a throwaway scene; the user's working scene is never modified.

    Args:
        path: Path to the .glb (absolute, or relative to the project root or
              export_root). Must resolve inside the project.

    Returns:
        On success, a PNG Image of the rendered asset. On failure (Blender
        unreachable / import error / timeout), a structured dict
        {"error", "path", "hint"} — never raises.
    """
    out = _render_asset(path)
    if out.get("ok"):
        _emit("render_asset", out["path"])
        return Image(data=out["data"], format="png")
    _emit("render_asset.error", out.get("error", "render failed"))
    return out


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
