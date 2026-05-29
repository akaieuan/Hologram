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

State caches for 2s. The SSE stream watches the event log's byte size and emits
each newly-appended line. No validation in v0.1 — the dashboard observes.
"""

from __future__ import annotations

import json
import mimetypes
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from .. import __version__, events
from ..config import Config, load_config
from ..gltf import load_asset

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"

# Set once by run() before serving; the handler reads it (read-only across threads).
CONFIG: Optional[Config] = None

BLENDER_MCP_HOST = os.environ.get("BLENDER_MCP_HOST", "127.0.0.1")
BLENDER_MCP_PORT = int(os.environ.get("BLENDER_MCP_PORT", "9876"))


# ── State snapshot ──────────────────────────────────────────────────────────

_state_cache: dict[str, Any] = {"ts": 0.0, "data": None}
_state_lock = threading.Lock()


def compute_state(cfg: Config) -> dict:
    """Build a pipeline snapshot: GLB assets grouped by category. No validation."""
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
            entries.append(
                {
                    "name": glb.stem,
                    "glb": cfg.rel(glb),
                    "mtime": mtime,
                    "script": cfg.rel(script) if script else None,
                }
            )
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
    add-on is listening inside Blender."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    err: Optional[str] = None
    on = False
    try:
        s.connect((BLENDER_MCP_HOST, BLENDER_MCP_PORT))
        on = True
    except Exception as e:
        err = type(e).__name__
    finally:
        try:
            s.close()
        except Exception:
            pass
    return {"on": on, "host": BLENDER_MCP_HOST, "port": BLENDER_MCP_PORT,
            "checked_at": time.time(), "error": err}


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

        self.send_error(404, "Not Found")

    def _inspect(self, p: str) -> None:
        if not p:
            self._send_json({"error": "missing ?path="}, status=400)
            return
        resolved = self.cfg.resolve_asset(p)
        if resolved is None:
            self._send_json({"error": "asset not found within project", "path": p}, status=404)
            return
        try:
            data = load_asset(str(resolved)).to_dict()
            data["path"] = self.cfg.rel(resolved)
            self._send_json(data)
        except Exception as e:
            self._send_json({"error": f"{type(e).__name__}: {e}", "path": self.cfg.rel(resolved)}, status=500)

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
            self.wfile.write(f"data: {json.dumps({'events': init})}\n\n".encode("utf-8"))
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
                        self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                else:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return


# ── Entry point ───────────────────────────────────────────────────────────────

def run(host: Optional[str] = None, port: Optional[int] = None, project: Optional[str] = None) -> int:
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
