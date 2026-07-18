"""Blender command client — the stdlib socket protocol for the BlenderMCP add-on.

The add-on (ahujasid/blender-mcp style) listens on a TCP socket (default
127.0.0.1:9876) and accepts one JSON command per connection:

    {"type": "<command>", "params": {...}}

replying with ``{"status": "success", "result": ...}`` or
``{"status": "error", "message": ...}``. There is no length-prefix framing —
the client accumulates bytes until ``json.loads`` parses a complete object.

This module is the *only* place that speaks that protocol. Both the dashboard
(a liveness probe) and the MCP server (rendering) use it, so the MCP server
never imports dashboard code. Pure stdlib — no ``bpy``, no project code — so a
socket call here keeps the MCP server's import purity intact. Driving a separate
running process is not importing its code.

Nothing here raises on a connection problem: every entry point returns a
structured dict so callers can degrade gracefully when Blender is not running.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import time

HOST = os.environ.get("BLENDER_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("BLENDER_MCP_PORT", "9876"))


# ── Liveness ────────────────────────────────────────────────────────────────────

def probe(host: str = HOST, port: int = PORT, timeout: float = 0.3) -> dict:
    """Connect-only liveness check. A successful connect means the add-on is
    listening. Returns ``{on, host, port, checked_at, error}``."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    err: str | None = None
    on = False
    try:
        s.connect((host, port))
        on = True
    except Exception as e:
        err = type(e).__name__
    finally:
        try:
            s.close()
        except Exception:
            pass
    return {"on": on, "host": host, "port": port, "checked_at": time.time(), "error": err}


# ── Command transport ────────────────────────────────────────────────────────────

def send_command(
    cmd_type: str,
    params: dict | None = None,
    *,
    host: str = HOST,
    port: int = PORT,
    timeout: float = 30.0,
) -> dict:
    """Send one command and return the add-on's parsed JSON reply.

    On any transport failure (Blender not running, timeout, half-open socket,
    unparseable reply) returns ``{"status": "error", "message": ...}`` rather
    than raising — the caller decides how to degrade.
    """
    payload = json.dumps({"type": cmd_type, "params": params or {}}).encode("utf-8")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    chunks: list[bytes] = []
    try:
        s.connect((host, port))
        s.sendall(payload)
        while True:
            try:
                data = s.recv(8192)
            except TimeoutError:
                break
            if not data:
                break
            chunks.append(data)
            # The add-on sends one JSON object; stop as soon as it parses.
            try:
                return json.loads(b"".join(chunks).decode("utf-8"))
            except json.JSONDecodeError:
                continue
    except (TimeoutError, ConnectionRefusedError, OSError) as e:
        return {"status": "error", "message": f"{type(e).__name__}: {e}"}
    finally:
        try:
            s.close()
        except Exception:
            pass
    # Connection closed before a complete JSON object arrived.
    try:
        return json.loads(b"".join(chunks).decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"status": "error", "message": "incomplete or invalid reply from Blender"}


# ── Rendering ────────────────────────────────────────────────────────────────────

def render_code(glb_path: str, out_png: str, size: int = 512) -> str:
    """Assemble the ``bpy`` snippet that renders ``glb_path`` to ``out_png``.

    Non-destructive by construction: it works inside a throwaway scene, restores
    the user's original active scene in a ``finally``, and removes the scene +
    objects it created. The user's working scene is never modified. Paths are
    JSON-encoded so they embed safely as Python string literals.
    """
    glb = json.dumps(glb_path)
    out = json.dumps(out_png)
    return f"""
import bpy
from mathutils import Vector

# Every datablock kind a glTF import (+ our own camera/sun) can create. We
# snapshot each collection before touching anything and remove only the *new*
# members in the finally, so the user's file is left exactly as we found it —
# no orphaned scene / camera / sun / mesh / material / action accumulating
# across repeated renders. (Removing only `_created` objects, as an earlier
# version did, leaked the camera + sun + import orphans.)
_KINDS = ("objects", "collections", "meshes", "curves", "armatures",
          "cameras", "lights", "materials", "images", "actions",
          "node_groups", "scenes")
_orig_scene = bpy.context.window.scene
_before = {{k: set(getattr(bpy.data, k)) for k in _KINDS}}
_tmp = bpy.data.scenes.new("hologram_preview")
try:
    bpy.context.window.scene = _tmp
    bpy.ops.import_scene.gltf(filepath={glb})
    _imported = [o for o in bpy.data.objects if o not in _before["objects"]]

    coords = []
    for ob in _imported:
        for corner in ob.bound_box:
            coords.append(ob.matrix_world @ Vector(corner))
    if coords:
        lo = Vector((min(c.x for c in coords), min(c.y for c in coords), min(c.z for c in coords)))
        hi = Vector((max(c.x for c in coords), max(c.y for c in coords), max(c.z for c in coords)))
        center = (lo + hi) / 2.0
        radius = max((hi - lo).length / 2.0, 0.001)
    else:
        center, radius = Vector((0, 0, 0)), 1.0

    cam_data = bpy.data.cameras.new("hologram_cam")
    cam = bpy.data.objects.new("hologram_cam", cam_data)
    _tmp.collection.objects.link(cam)
    _direction = Vector((1.0, -1.2, 0.7)).normalized()
    cam.location = center + _direction * radius * 3.2
    cam.rotation_euler = (center - cam.location).to_track_quat('-Z', 'Y').to_euler()
    _tmp.camera = cam

    sun_data = bpy.data.lights.new("hologram_sun", type='SUN')
    sun_data.energy = 3.0
    sun = bpy.data.objects.new("hologram_sun", sun_data)
    sun.location = center + Vector((2.0, -2.0, 4.0)) * max(radius, 1.0)
    _tmp.collection.objects.link(sun)

    for _engine in ('BLENDER_EEVEE_NEXT', 'BLENDER_EEVEE', 'CYCLES'):
        try:
            _tmp.render.engine = _engine
            break
        except Exception:
            continue
    _tmp.render.resolution_x = {int(size)}
    _tmp.render.resolution_y = {int(size)}
    _tmp.render.film_transparent = True
    _tmp.render.image_settings.file_format = 'PNG'
    _tmp.render.filepath = {out}
    bpy.ops.render.render(write_still=True)
finally:
    bpy.context.window.scene = _orig_scene
    # Sweep every datablock we created, most-dependent kind first (objects
    # before their data, scenes last), so nothing we made survives. _KINDS is
    # ordered for clean removal; force-remove tolerates lingering users.
    for _k in _KINDS:
        _coll = getattr(bpy.data, _k)
        for _db in [d for d in _coll if d not in _before[_k]]:
            try:
                _coll.remove(_db)
            except Exception:
                pass
"""


def render_glb(
    abs_path: str,
    *,
    host: str = HOST,
    port: int = PORT,
    out_png: str | None = None,
    size: int = 512,
    timeout: float = 60.0,
) -> dict:
    """Render a GLB via the live Blender instance.

    Picks its own temp output path (same machine / OS user as Blender, so it can
    read back what Blender writes), sends the render snippet, and confirms a
    non-empty PNG landed. Returns ``{"ok": True, "image_path": str}`` on success
    or ``{"ok": False, "error": str, "image_path": str|None}`` otherwise — never
    raises.
    """
    if out_png is None:
        fd, out_png = tempfile.mkstemp(suffix=".png", prefix="hologram_render_")
        os.close(fd)
    reply = send_command("execute_code", {"code": render_code(abs_path, out_png, size)},
                         host=host, port=port, timeout=timeout)
    if reply.get("status") != "success":
        return {"ok": False, "error": reply.get("message") or "render failed",
                "image_path": out_png}
    if not (os.path.isfile(out_png) and os.path.getsize(out_png) > 0):
        return {"ok": False, "error": "Blender reported success but wrote no image",
                "image_path": out_png}
    return {"ok": True, "image_path": out_png}
