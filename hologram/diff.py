"""Regression diffing — a compact, comparable fingerprint of an `Asset` plus a
small on-disk snapshot store, so both the dashboard and the agent can answer
"what changed since the last checkpoint?"

Pure and bpy-free: `summarize` and `diff` derive everything from the `Asset`
struct ([gltf.py]), and the snapshot store is plain JSON under
`.hologram/snapshots/` (a runtime artifact, gitignored like the event log).

Baseline discipline: snapshots are refreshed only by an explicit `hologram
check` (see `checks.run_project`), so a diff means "since the last check." The
dashboard `/api/inspect` and the MCP tools *read* a diff against the current
baseline but never write it — reads stay side-effect-free.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import Config
from .gltf import Asset

# Name-set fields compared between two summaries. Counts are diffed separately.
_NAME_FIELDS = ("materials", "meshes", "animations", "top_level", "nodes")


# ── Fingerprint ───────────────────────────────────────────────────────────────

def summarize(asset: Asset) -> dict:
    """A compact, JSON-serializable fingerprint of an asset. Deterministic for a
    given asset (sorted name sets), so re-summarizing an unchanged export yields
    an identical dict — the property the diff relies on."""
    return {
        "stem": asset.stem,
        "counts": {
            "nodes": len(asset.nodes),
            "meshes": len(asset.mesh_names),
            "materials": len(asset.materials),
            "animations": len(asset.animations),
            "skins": len(asset.skins),
        },
        "materials": sorted(asset.materials),
        "meshes": sorted(asset.mesh_names),
        "animations": sorted(asset.animations),
        "top_level": sorted(asset.top_level_node_names()),
        "nodes": sorted(n.name for n in asset.nodes),
    }


# ── Diff ────────────────────────────────────────────────────────────────────────

def diff(prev: dict | None, curr: dict) -> dict:
    """Compare two summaries. Returns ``{}`` when nothing changed (the gate for
    "is there anything to report"), ``{"first_seen": True}`` when there is no
    baseline yet, otherwise some of ``lost`` / ``gained`` / ``changed``:

      * ``lost``    — {field: [names present before, gone now]}
      * ``gained``  — {field: [names new now]}
      * ``changed`` — {count_name: {"from": int, "to": int}}
    """
    if not prev:
        return {"first_seen": True}

    lost: dict[str, list[str]] = {}
    gained: dict[str, list[str]] = {}
    for field in _NAME_FIELDS:
        before = set(prev.get(field, []))
        after = set(curr.get(field, []))
        gone = sorted(before - after)
        new = sorted(after - before)
        if gone:
            lost[field] = gone
        if new:
            gained[field] = new

    changed: dict[str, dict[str, int]] = {}
    prev_counts = prev.get("counts", {}) or {}
    curr_counts = curr.get("counts", {}) or {}
    for name in sorted(set(prev_counts) | set(curr_counts)):
        a = prev_counts.get(name, 0)
        b = curr_counts.get(name, 0)
        if a != b:
            changed[name] = {"from": a, "to": b}

    out: dict[str, Any] = {}
    if lost:
        out["lost"] = lost
    if gained:
        out["gained"] = gained
    if changed:
        out["changed"] = changed
    return out


# ── Snapshot store ───────────────────────────────────────────────────────────────

def snapshots_dir(cfg: Config) -> Path:
    """`.hologram/snapshots/`, derived from the event-log location so it follows
    a project's configured `paths.events_log` directory."""
    return cfg.events_log.parent / "snapshots"


def _slug(rel_path: str) -> str:
    """Flatten a project-relative path into one safe filename component."""
    return rel_path.replace("\\", "/").replace("/", "__")


def snapshot_path(cfg: Config, asset_path: Path | str) -> Path:
    """Path to the snapshot JSON for one asset, keyed by its project-relative path."""
    rel = cfg.rel(Path(asset_path))
    return snapshots_dir(cfg) / f"{_slug(rel)}.json"


def load_snapshot(cfg: Config, asset_path: Path | str) -> dict | None:
    """Last saved summary for an asset, or None if absent/unreadable."""
    p = snapshot_path(cfg, asset_path)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_snapshot(cfg: Config, asset_path: Path | str, summary: dict) -> None:
    """Persist a summary as the new baseline. Failures are swallowed so a
    read-only-filesystem never breaks a check run."""
    p = snapshot_path(cfg, asset_path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    except OSError:
        pass
