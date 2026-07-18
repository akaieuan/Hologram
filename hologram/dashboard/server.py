"""Hologram dashboard — stdlib HTTP + SSE, zero framework dependencies.

Routes:
  GET  /                    dashboard shell
  GET  /static/*            JS/CSS assets
  GET  /api/health          liveness + project info
  GET  /api/state           pipeline snapshot (assets grouped by category)
  GET  /api/events          recent events (newest first)
  GET  /api/events/stream   Server-Sent Events live tail of the event log
  GET  /api/inspect?path=   parse one GLB into the Asset struct
  GET  /api/active          in-flight tool calls (pre/post + MCP start/end pairing)
  GET  /api/blender_mcp     TCP probe of the Blender MCP add-on (default :9876)
  GET  /api/manifest        sidecar exports/manifest.json (empty when absent)
  GET  /api/history?asset=  version snapshots for one asset (+ &v=N introspects
                            a snapshot and diffs it against the current export)
  GET  /api/thumb?asset=    stream one asset's manifest thumbnail (if present)

State caches for 2s. The SSE stream watches the event log's byte size and emits
each newly-appended line. No validation in v0.1 — the dashboard observes.

When a project follows the explogo export convention (an ``exports/manifest.json``
sidecar), ``/api/state`` entries are enriched with that per-asset metadata and the
manifest/history/thumb routes light up. Absent manifest = zero behaviour change.
"""

from __future__ import annotations

import json
import mimetypes
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .. import __version__, blender, events
from .. import manifest as manifest_mod
from ..config import Config, load_config
from ..gltf import load_asset

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"

# Set once by run() before serving; the handler reads it (read-only across threads).
CONFIG: Config | None = None

BLENDER_MCP_HOST = os.environ.get("BLENDER_MCP_HOST", "127.0.0.1")
BLENDER_MCP_PORT = int(os.environ.get("BLENDER_MCP_PORT", "9876"))


# ── State snapshot ──────────────────────────────────────────────────────────

_state_cache: dict[str, Any] = {"ts": 0.0, "data": None}
_state_lock = threading.Lock()


def compute_state(cfg: Config) -> dict:
    """Build a pipeline snapshot: GLB assets grouped by category. No validation.

    When an explogo-style ``manifest.json`` sidecar is present, each discovered
    GLB is enriched with its manifest record (version, generator, params, tris,
    thumbnail) under an additive ``manifest`` key. With no manifest the entries
    are unchanged — the enrichment is purely additive."""
    mani = manifest_mod.load_manifest(cfg)
    cats: dict[str, list[dict]] = {}
    totals = {"assets": 0, "categories": 0}
    by_cat = cfg.list_glbs()
    for cat in cfg.categories:
        entries: list[dict] = []
        for glb in by_cat.get(cat.name, []):
            try:
                mtime = glb.stat().st_mtime
            except OSError:
                mtime = None
            script = cfg.find_script_for(glb, cat)
            entry: dict[str, Any] = {
                "name": glb.stem,
                "glb": cfg.rel(glb),
                "mtime": mtime,
                "script": cfg.rel(script) if script else None,
            }
            if mani is not None:
                rec = mani.by_stem(glb.stem)
                if rec is not None:
                    entry["manifest"] = rec.to_dict()
            entries.append(entry)
        cats[cat.name] = entries
        totals["assets"] += len(entries)
    totals["categories"] = len(cats)
    return {
        "project": cfg.name,
        "root": str(cfg.root),
        "export_root": cfg.rel(cfg.export_root),
        "timestamp": time.time(),
        "totals": totals,
        "categories": cats,
        "has_manifest": mani is not None,
    }


def get_state(cfg: Config, force: bool = False) -> dict:
    now = time.time()
    with _state_lock:
        if force or now - _state_cache["ts"] > 2.0 or _state_cache["data"] is None:
            _state_cache["data"] = compute_state(cfg)
            _state_cache["ts"] = now
        return _state_cache["data"]


# ── Active call tracking ──────────────────────────────────────────────────────

def _event_key(ev: dict) -> str:
    """Stable identity for pairing `pre` and `post` events for one tool call."""
    sid = ev.get("session_id", "")
    if ev.get("mcp_tool"):
        return f"{sid}|{ev['mcp_tool']}|{json.dumps(ev.get('params', {}), sort_keys=True)}"
    if ev.get("tool") == "Bash":
        return f"{sid}|Bash|{ev.get('command', '')[:200]}"
    if ev.get("tool") in ("Write", "Edit", "MultiEdit"):
        return f"{sid}|{ev['tool']}|{ev.get('file_path', '')}"
    if ev.get("type") == "skill_invoke":
        return f"{sid}|skill|{ev.get('skill', '')}|{ev.get('args', '')}"
    return f"{sid}|{ev.get('type', '')}|{ev.get('ts', '')}"


def compute_active(cfg: Config, window_seconds: float = 600.0) -> dict:
    """In-flight calls — `pre` events (and MCP `.start`s) with no matching close."""
    now = time.time()
    cutoff = now - window_seconds
    pending: dict[str, dict] = {}
    for ev in reversed(events.tail(cfg.events_log, limit=2000)):  # oldest -> newest
        if float(ev.get("ts", 0)) < cutoff:
            continue
        phase = ev.get("phase")
        if phase == "pre":
            pending[_event_key(ev)] = ev
        elif phase == "post":
            pending.pop(_event_key(ev), None)
        action = ev.get("action", "")
        if action.endswith(".start"):
            tool = action[: -len(".start")]
            key = f"MCP|{ev.get('session_id','')}|{tool}|{ev.get('detail','')}"
            pending[key] = dict(ev, _pair_tool=tool)
        elif action.endswith(".end"):
            tool = action[: -len(".end")]
            prefix = f"MCP|{ev.get('session_id','')}|{tool}|"
            for k in [k for k in pending if k.startswith(prefix)]:
                pending.pop(k, None)

    active = []
    for ev in pending.values():
        started = float(ev.get("ts", now))
        tool = ev.get("mcp_tool") or ev.get("_pair_tool") or ev.get("tool") or ev.get("type", "?")
        target = (
            ev.get("command")
            or ev.get("file_path")
            or ev.get("detail")
            or (ev.get("params") and " ".join(f"{k}={v}" for k, v in ev["params"].items()))
            or ev.get("args")
            or ""
        )
        active.append(
            {
                "session_id": ev.get("session_id", ""),
                "started_at": started,
                "duration_s": round(now - started, 1),
                "tool": tool,
                "target": str(target)[:200],
            }
        )
    active.sort(key=lambda x: x["duration_s"])
    return {"active": active, "checked_at": now, "window_seconds": window_seconds}


# ── Blender MCP probe ──────────────────────────────────────────────────────────

def probe_blender_mcp(timeout: float = 0.3) -> dict:
    """TCP-probe the Blender MCP add-on socket. A successful connect means the
    add-on is listening inside Blender. Delegates to the shared `blender` client
    so the dashboard and the MCP server speak one socket protocol."""
    return blender.probe(BLENDER_MCP_HOST, BLENDER_MCP_PORT, timeout)


# ── HTTP handler ────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "hologram-dashboard/" + __version__

    def log_message(self, fmt: str, *args: Any) -> None:
        if os.environ.get("HOLOGRAM_VERBOSE"):
            super().log_message(fmt, *args)

    @property
    def cfg(self) -> Config:
        assert CONFIG is not None, "dashboard config not initialised"
        return CONFIG

    def _send_json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        if not path.is_file():
            self.send_error(404, "Not Found")
            return
        ctype, _ = mimetypes.guess_type(str(path))
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            self._send_file(STATIC_DIR / "index.html")
            return
        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            safe = (STATIC_DIR / rel).resolve()
            if STATIC_DIR not in safe.parents and safe != STATIC_DIR:
                self.send_error(403, "Forbidden")
                return
            self._send_file(safe)
            return

        if path == "/api/health":
            self._send_json({"ok": True, "project": self.cfg.name,
                             "root": str(self.cfg.root), "version": __version__})
            return
        if path == "/api/state":
            q = parse_qs(parsed.query)
            force = q.get("force", ["0"])[0] == "1"
            self._send_json(get_state(self.cfg, force=force))
            return
        if path == "/api/events":
            q = parse_qs(parsed.query)
            limit = int(q.get("limit", ["200"])[0])
            self._send_json({"events": events.tail(self.cfg.events_log, limit=limit)})
            return
        if path == "/api/events/stream":
            self._stream_events()
            return
        if path == "/api/inspect":
            q = parse_qs(parsed.query)
            self._inspect(q.get("path", [""])[0])
            return
        if path == "/api/checks":
            q = parse_qs(parsed.query)
            self._checks(q.get("path", [""])[0])
            return
        if path == "/api/glb":
            q = parse_qs(parsed.query)
            self._glb(q.get("path", [""])[0])
            return
        if path == "/api/active":
            self._send_json(compute_active(self.cfg))
            return
        if path == "/api/blender_mcp":
            self._send_json(probe_blender_mcp())
            return
        if path == "/api/manifest":
            self._manifest()
            return
        if path == "/api/history":
            q = parse_qs(parsed.query)
            self._history(q.get("asset", [""])[0], q.get("v", [""])[0],
                          q.get("base", [""])[0])
            return
        if path == "/api/thumb":
            q = parse_qs(parsed.query)
            self._thumb(q.get("asset", [""])[0])
            return

        self.send_error(404, "Not Found")

    # ── Manifest / version-history surface (explogo export convention) ─────────

    def _manifest(self) -> None:
        """The whole sidecar manifest, or an empty, present:false payload when the
        project ships no ``manifest.json``. Read-only."""
        mani = manifest_mod.load_manifest(self.cfg)
        if mani is None:
            self._send_json({"present": False, "count": 0, "assets": {}})
            return
        self._send_json({"present": True, **mani.to_dict()})

    def _current_glb(self, asset_id: str, rec: object | None) -> Path | None:
        """Resolve the live (current) export for an asset, within export_root.
        Prefers the manifest record's declared ``glb`` path, then falls back to a
        stem search under export_root so history works even without a manifest."""
        from ..config import resolve_within
        if isinstance(rec, manifest_mod.AssetRecord) and rec.glb:
            resolved = resolve_within(self.cfg.export_root, rec.glb)
            if resolved and resolved.is_file():
                return resolved
        if not manifest_mod.is_safe_id(asset_id):
            return None
        for cand in sorted(self.cfg.export_root.glob(f"**/{asset_id}.glb")):
            # Skip the .history tree — those are snapshots, not the live export.
            if manifest_mod.HISTORY_DIRNAME not in cand.parts and cand.is_file():
                return cand
        return None

    def _history(self, asset_id: str, v: str, base: str) -> None:
        """Version-history introspection for one asset (read-only).

        Without ``v``: list the available snapshot versions plus the current
        record. With ``v=N``: introspect snapshot ``vN`` and diff its fingerprint
        against the current export (or against ``base=M`` when supplied), reusing
        the same ``diff.py`` machinery as the regression baseline."""
        if not asset_id:
            self._send_json({"error": "missing ?asset="}, status=400)
            return
        if not manifest_mod.is_safe_id(asset_id):
            self._send_json({"error": "invalid asset id", "asset": asset_id}, status=400)
            return

        mani = manifest_mod.load_manifest(self.cfg)
        rec = mani.get(asset_id) if mani else None
        versions = manifest_mod.history_versions(self.cfg, asset_id)

        if not v:
            current = self._current_glb(asset_id, rec)
            self._send_json({
                "asset": asset_id,
                "versions": versions,
                "current_version": rec.version if rec else None,
                "record": rec.to_dict() if rec else None,
                "has_current": current is not None,
            })
            return

        try:
            target_v = int(v)
        except ValueError:
            self._send_json({"error": "v must be an integer", "asset": asset_id}, status=400)
            return

        snap = manifest_mod.history_glb(self.cfg, asset_id, target_v)
        if snap is None:
            self._send_json({"error": f"no snapshot v{target_v} for {asset_id}",
                             "asset": asset_id, "versions": versions}, status=404)
            return

        from ..diff import diff as compute_diff
        from ..diff import summarize
        try:
            selected = load_asset(str(snap))
            payload: dict[str, Any] = {
                "asset": asset_id,
                "version": target_v,
                "inspect": selected.to_dict(),
                "versions": versions,
            }
            # Compare against another snapshot (base=M) or the current export.
            if base:
                try:
                    base_v = int(base)
                except ValueError:
                    self._send_json({"error": "base must be an integer",
                                     "asset": asset_id}, status=400)
                    return
                base_path = manifest_mod.history_glb(self.cfg, asset_id, base_v)
                if base_path is None:
                    self._send_json({"error": f"no snapshot v{base_v} for {asset_id}",
                                     "asset": asset_id, "versions": versions}, status=404)
                    return
                other = load_asset(str(base_path))
                payload["compared_from"] = f"v{base_v}"
                payload["compared_to"] = f"v{target_v}"
                changes = compute_diff(summarize(other), summarize(selected))
            else:
                current = self._current_glb(asset_id, rec)
                payload["compared_from"] = f"v{target_v}"
                payload["compared_to"] = "current"
                if current is None:
                    payload["diff"] = None
                    payload["note"] = "no current export to compare against"
                    self._send_json(payload)
                    return
                other = load_asset(str(current))
                changes = compute_diff(summarize(selected), summarize(other))
            payload["diff"] = changes if changes and not changes.get("first_seen") else {}
            self._send_json(payload)
        except Exception as e:
            self._send_json({"error": f"{type(e).__name__}: {e}",
                             "asset": asset_id, "version": target_v}, status=500)

    def _thumb(self, asset_id: str) -> None:
        """Stream one asset's manifest thumbnail (image bytes), resolved within
        export_root. 404 when there is no manifest, no thumbnail, or the file is
        missing. Read-only."""
        if not asset_id:
            self.send_error(400, "missing ?asset=")
            return
        mani = manifest_mod.load_manifest(self.cfg)
        rec = mani.get(asset_id) if mani else None
        if rec is None or not rec.thumbnail:
            self.send_error(404, "no thumbnail")
            return
        from ..config import resolve_within
        resolved = resolve_within(self.cfg.export_root, rec.thumbnail)
        if resolved is None or not resolved.is_file():
            self.send_error(404, "thumbnail not found")
            return
        try:
            data = resolved.read_bytes()
        except OSError:
            self.send_error(404, "unreadable")
            return
        ctype, _ = mimetypes.guess_type(str(resolved))
        self.send_response(200)
        self.send_header("Content-Type", ctype or "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _inspect(self, p: str) -> None:
        if not p:
            self._send_json({"error": "missing ?path="}, status=400)
            return
        resolved = self.cfg.resolve_asset(p)
        if resolved is None:
            self._send_json({"error": "asset not found within project", "path": p}, status=404)
            return
        try:
            asset = load_asset(str(resolved))
            data = asset.to_dict()
            data["path"] = self.cfg.rel(resolved)
            # Read-only regression diff against the last `hologram check`
            # baseline. We read the snapshot but never write it, so this GET
            # stays side-effect-free (the checkpoint is an explicit check run).
            from ..diff import diff as compute_diff
            from ..diff import load_snapshot, summarize
            prev = load_snapshot(self.cfg, resolved)
            if prev:
                changes = compute_diff(prev, summarize(asset))
                if changes and not changes.get("first_seen"):
                    data["diff"] = changes
            self._send_json(data)
        except Exception as e:
            self._send_json({"error": f"{type(e).__name__}: {e}",
                             "path": self.cfg.rel(resolved)}, status=500)

    def _checks(self, p: str) -> None:
        """Run the built-in + project checks against one asset. Reuses the
        resolve_asset containment boundary. Project checks are loaded lazily
        here (in this local dashboard process), never by the MCP server."""
        if not p:
            self._send_json({"error": "missing ?path="}, status=400)
            return
        resolved = self.cfg.resolve_asset(p)
        if resolved is None:
            self._send_json({"error": "asset not found within project", "path": p}, status=404)
            return
        try:
            from ..checks import all_checks, run_asset
            checks, load_error = all_checks(self.cfg)
            asset = load_asset(str(resolved))
            self._send_json({
                "path": self.cfg.rel(resolved),
                "findings": [f.to_dict() for f in run_asset(asset, checks)],
                "load_error": load_error,
            })
        except Exception as e:
            self._send_json({"error": f"{type(e).__name__}: {e}",
                             "path": self.cfg.rel(resolved)}, status=500)

    def _glb(self, p: str) -> None:
        """Stream raw GLB bytes for the web preview. Shares resolve_asset with
        _inspect, so it cannot escape the project root."""
        if not p:
            self.send_error(400, "missing ?path=")
            return
        resolved = self.cfg.resolve_asset(p)
        if resolved is None:
            self.send_error(404, "asset not found within project")
            return
        try:
            data = resolved.read_bytes()
        except OSError:
            self.send_error(404, "unreadable")
            return
        self.send_response(200)
        self.send_header("Content-Type", "model/gltf-binary")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _stream_events(self) -> None:
        cfg = self.cfg
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        init = events.tail(cfg.events_log, limit=50)
        try:
            self.wfile.write(b"event: init\n")
            self.wfile.write(f"data: {json.dumps({'events': init})}\n\n".encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

        offset = events.size(cfg.events_log)
        while True:
            try:
                time.sleep(1.0)
                new, offset = events.read_since(cfg.events_log, offset)
                if new:
                    for ev in new:
                        self.wfile.write(b"event: append\n")
                        self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                    self.wfile.flush()
                else:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return


# ── Entry point ───────────────────────────────────────────────────────────────

def run(host: str | None = None, port: int | None = None, project: str | None = None) -> int:
    global CONFIG
    cfg = load_config(project)
    CONFIG = cfg
    host = host or cfg.dashboard_host
    port = int(port or cfg.dashboard_port)

    print(f"hologram dashboard -> http://{host}:{port}/")
    print(f"  project:    {cfg.name}")
    print(f"  root:       {cfg.root}")
    print(f"  export:     {cfg.export_root}")
    print(f"  events log: {cfg.events_log} (exists: {cfg.events_log.is_file()})")

    server = ThreadingHTTPServer((host, port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
