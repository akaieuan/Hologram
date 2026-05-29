"""hologram.blender — the stdlib socket client for the BlenderMCP add-on.

These exercise the wire protocol against a throwaway localhost server (no real
Blender), the render-code assembly (path injection + non-destructive markers),
and the graceful-degradation contract: every entry point returns a structured
dict instead of raising when Blender is unreachable.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from contextlib import contextmanager

from hologram import blender


# ── fakes ─────────────────────────────────────────────────────────────────────

def _free_port() -> int:
    """A port number that is bound then released, so a later connect refuses."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@contextmanager
def listening():
    """A socket that accepts connections but never replies (a probe target)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    try:
        yield srv.getsockname()[1]
    finally:
        srv.close()


@contextmanager
def fake_blender(reply, *, chunked=False, capture=None, on_command=None):
    """A throwaway server speaking the add-on's framing: read one JSON command,
    then send ``reply`` (optionally split across two packets to exercise the
    client's accumulate-until-parse loop). Appends the parsed command to
    ``capture`` if given; ``on_command(cmd)`` may write side effects (e.g. the
    output PNG) before the reply is sent."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        try:
            chunks: list[bytes] = []
            cmd = None
            while True:
                data = conn.recv(8192)
                if not data:
                    break
                chunks.append(data)
                try:
                    cmd = json.loads(b"".join(chunks).decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                break
            if cmd is not None:
                if capture is not None:
                    capture.append(cmd)
                if on_command is not None:
                    on_command(cmd)
            payload = json.dumps(reply).encode("utf-8")
            if chunked and len(payload) > 4:
                mid = len(payload) // 2
                conn.sendall(payload[:mid])
                time.sleep(0.05)
                conn.sendall(payload[mid:])
            else:
                conn.sendall(payload)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        yield port
    finally:
        srv.close()
        t.join(timeout=1.0)


# ── probe ─────────────────────────────────────────────────────────────────────

def test_probe_on_when_listening():
    with listening() as port:
        out = blender.probe("127.0.0.1", port, timeout=0.5)
    assert out["on"] is True
    assert out["error"] is None
    assert out["port"] == port


def test_probe_off_when_refused():
    out = blender.probe("127.0.0.1", _free_port(), timeout=0.5)
    assert out["on"] is False
    assert out["error"]  # a non-empty exception name (e.g. ConnectionRefusedError)


# ── send_command ────────────────────────────────────────────────────────────────

def test_send_command_roundtrips_and_frames():
    seen: list[dict] = []
    reply = {"status": "success", "result": {"ok": 1}}
    with fake_blender(reply, capture=seen) as port:
        out = blender.send_command("ping", {"x": 1}, port=port, timeout=2.0)
    assert out == reply
    assert seen == [{"type": "ping", "params": {"x": 1}}]


def test_send_command_accumulates_chunked_reply():
    reply = {"status": "success", "result": "a" * 50}
    with fake_blender(reply, chunked=True) as port:
        out = blender.send_command("execute_code", {"code": "x"}, port=port, timeout=2.0)
    assert out == reply


def test_send_command_refused_is_structured_not_raised():
    out = blender.send_command("ping", port=_free_port(), timeout=0.5)
    assert out["status"] == "error"
    assert "message" in out


# ── render_code (path injection / non-destructive markers) ─────────────────────

def test_render_code_encodes_paths_safely():
    code = blender.render_code('/tmp/a"b\\c.glb', "/out/p.png", size=256)
    # Paths embed as JSON string literals — quotes/backslashes escaped, not raw.
    assert json.dumps('/tmp/a"b\\c.glb') in code
    assert json.dumps("/out/p.png") in code
    assert "256" in code


def test_render_code_is_non_destructive():
    code = blender.render_code("/a.glb", "/b.png")
    assert "scenes.new" in code                    # a throwaway scene…
    assert "_orig_scene" in code                   # original captured…
    assert "window.scene = _orig_scene" in code    # …and restored afterward
    assert "write_still=True" in code
    # …and every datablock we create is swept, not just the imported objects —
    # the camera and sun (and import orphans) must be cleaned up too.
    assert "_before" in code                       # snapshot all collections
    assert "_coll.remove(_db)" in code             # remove only the new members
    assert '"cameras"' in code and '"lights"' in code  # cam/sun kinds swept


# ── render_glb (degradation + success / empty-file contract) ───────────────────

def test_render_glb_refused_is_structured_not_raised():
    out = blender.render_glb("/some/asset.glb", port=_free_port(), timeout=0.5)
    assert out["ok"] is False
    assert out["error"]


def test_render_glb_success_when_png_written(tmp_path):
    png = tmp_path / "r.png"

    def write_png(_cmd):
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

    with fake_blender({"status": "success"}, on_command=write_png) as port:
        out = blender.render_glb("/a.glb", port=port, out_png=str(png), timeout=2.0)
    assert out["ok"] is True
    assert out["image_path"] == str(png)


def test_render_glb_success_but_empty_png_is_error(tmp_path):
    png = tmp_path / "empty.png"  # never written by the fake server
    with fake_blender({"status": "success"}) as port:
        out = blender.render_glb("/a.glb", port=port, out_png=str(png), timeout=2.0)
    assert out["ok"] is False
    assert "no image" in out["error"]
