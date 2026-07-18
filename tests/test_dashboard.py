"""The dashboard HTTP surface.

Two layers of coverage:

* The `/api/glb` preview route is exercised in-process against a synthetic
  Handler (the `handler` fixture) so path-traversal rejection can be asserted
  without a socket.
* Every JSON endpoint plus the SSE stream is exercised end-to-end against a
  real `ThreadingHTTPServer` bound to an ephemeral port (the `live_server`
  fixture), served from `examples/minimal`, driven by stdlib `http.client`.
  Deterministic: no fixed ports, no sleeps beyond the SSE socket timeout."""

from __future__ import annotations

import http.client
import io
import json
import socket
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import hologram.dashboard.server as server
from hologram.config import load_config

REPO = Path(__file__).resolve().parent.parent
MINIMAL = REPO / "examples" / "minimal"


class _Recorder:
    def __init__(self):
        self.status: int | None = None
        self.errors: list[tuple[int, str | None]] = []
        self.headers: dict[str, str] = {}
        self.wfile = io.BytesIO()

    def send_response(self, code):
        self.status = code

    def send_error(self, code, message=None):
        self.errors.append((code, message))

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass


@pytest.fixture
def handler(monkeypatch):
    monkeypatch.setattr(server, "CONFIG", load_config(str(MINIMAL)))
    h = server.Handler.__new__(server.Handler)  # skip socket-bound __init__
    rec = _Recorder()
    for name in ("send_response", "send_error", "send_header", "end_headers"):
        setattr(h, name, getattr(rec, name))
    h.wfile = rec.wfile
    return h, rec


def test_glb_rejects_path_traversal(handler):
    h, rec = handler
    h._glb("../../etc/passwd")
    assert rec.errors and rec.errors[-1][0] == 404
    assert rec.status is None  # never reached the 200 streaming path


def test_glb_rejects_absolute_escape(handler):
    h, rec = handler
    h._glb("/etc/passwd")
    assert rec.errors and rec.errors[-1][0] == 404


def test_glb_missing_path_is_400(handler):
    h, rec = handler
    h._glb("")
    assert rec.errors and rec.errors[-1][0] == 400


def test_glb_streams_real_asset(handler):
    h, rec = handler
    h._glb("export/gltf/weapons/sword.glb")
    assert rec.status == 200
    assert not rec.errors
    assert rec.headers.get("Content-Type") == "model/gltf-binary"
    body = rec.wfile.getvalue()
    assert body[:4] == b"glTF"  # GLB magic
    assert rec.headers.get("Content-Length") == str(len(body))


# ── Live server: real socket, ephemeral port, examples/minimal ──────────────


def _free_port() -> int:
    """Bind :0, read the assigned port, release it. Used as a definitely-closed
    target so the Blender-MCP probe resolves to a refused connection."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def live_server(monkeypatch):
    """Serve examples/minimal on an ephemeral port in a background thread."""
    monkeypatch.setattr(server, "CONFIG", load_config(str(MINIMAL)))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    host, port = httpd.server_address[0], httpd.server_address[1]
    # Short poll interval so shutdown() returns promptly in teardown.
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02},
                              daemon=True)
    thread.start()
    try:
        yield host, port
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _get(host: str, port: int, path: str, timeout: float = 5.0):
    """GET one URL, return (status, content_type, raw_body)."""
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, resp.getheader("Content-Type"), resp.read()
    finally:
        conn.close()


def _get_json(host: str, port: int, path: str):
    status, ctype, body = _get(host, port, path)
    return status, ctype, json.loads(body)


def test_health(live_server):
    host, port = live_server
    status, ctype, data = _get_json(host, port, "/api/health")
    assert status == 200
    assert ctype == "application/json; charset=utf-8"
    assert data["ok"] is True
    assert data["project"] == "minimal-example"
    assert data["version"]  # non-empty version string


def test_state(live_server):
    host, port = live_server
    status, _, data = _get_json(host, port, "/api/state")
    assert status == 200
    assert data["project"] == "minimal-example"
    # examples/minimal ships three GLBs across three categories.
    assert data["totals"] == {"assets": 3, "categories": 3}
    assert set(data["categories"]) == {"lootables", "weapons", "props"}
    names = {e["name"] for entries in data["categories"].values() for e in entries}
    assert names == {"coin", "sword", "crate"}


def test_state_force_refresh(live_server):
    host, port = live_server
    status, _, data = _get_json(host, port, "/api/state?force=1")
    assert status == 200
    assert data["totals"]["assets"] == 3


def test_events(live_server):
    host, port = live_server
    status, _, data = _get_json(host, port, "/api/events")
    assert status == 200
    evs = data["events"]
    assert isinstance(evs, list)
    assert len(evs) == 5  # the committed fixture log
    # tail() returns newest-first: the last appended line is a `pre` Bash call.
    assert evs[0]["phase"] == "pre"
    assert evs[0]["tool"] == "Bash"


def test_events_limit(live_server):
    host, port = live_server
    status, _, data = _get_json(host, port, "/api/events?limit=2")
    assert status == 200
    assert len(data["events"]) == 2


def test_inspect_valid_asset(live_server):
    host, port = live_server
    status, _, data = _get_json(host, port, "/api/inspect?path=export/gltf/weapons/sword.glb")
    assert status == 200
    assert data["stem"] == "sword"
    assert data["path"] == "export/gltf/weapons/sword.glb"
    assert isinstance(data["nodes"], list)
    assert "node_count" in data


def test_inspect_missing_path_is_400(live_server):
    host, port = live_server
    status, _, data = _get_json(host, port, "/api/inspect")
    assert status == 400
    assert "error" in data


def test_inspect_traversal_is_404(live_server):
    host, port = live_server
    status, _, data = _get_json(host, port, "/api/inspect?path=../../etc/passwd")
    assert status == 404
    assert "error" in data


def test_checks_valid_asset(live_server):
    host, port = live_server
    status, _, data = _get_json(host, port, "/api/checks?path=export/gltf/weapons/sword.glb")
    assert status == 200
    assert data["path"] == "export/gltf/weapons/sword.glb"
    assert isinstance(data["findings"], list)
    assert "load_error" in data


def test_checks_missing_path_is_400(live_server):
    host, port = live_server
    status, _, data = _get_json(host, port, "/api/checks")
    assert status == 400
    assert "error" in data


def test_active(live_server):
    host, port = live_server
    status, _, data = _get_json(host, port, "/api/active")
    assert status == 200
    assert isinstance(data["active"], list)
    assert data["window_seconds"] == 600.0
    assert "checked_at" in data


def test_blender_mcp_probe(live_server, monkeypatch):
    host, port = live_server
    # Point the probe at a just-released port so the connect is refused: the
    # endpoint must report `on: False` rather than raise.
    monkeypatch.setattr(server, "BLENDER_MCP_PORT", _free_port())
    status, _, data = _get_json(host, port, "/api/blender_mcp")
    assert status == 200
    assert data["on"] is False
    assert {"host", "port", "checked_at", "error"} <= set(data)


def test_unknown_route_is_404(live_server):
    host, port = live_server
    status, _, _ = _get(host, port, "/api/does-not-exist")
    assert status == 404


def test_events_stream_sse_init_frame(live_server):
    host, port = live_server
    # SSE is an unbounded stream; read only the opening `event: init` frame,
    # then close. The socket timeout guards against a hang if framing breaks.
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request("GET", "/api/events/stream")
        resp = conn.getresponse()
        assert resp.status == 200
        assert resp.getheader("Content-Type") == "text/event-stream"
        assert resp.getheader("Cache-Control") == "no-store"
        assert resp.fp is not None
        event_line = resp.fp.readline()
        data_line = resp.fp.readline()
        blank_line = resp.fp.readline()
        assert event_line == b"event: init\n"
        assert data_line.startswith(b"data: ")
        assert blank_line == b"\n"  # frames are terminated by a blank line
        payload = json.loads(data_line[len(b"data: "):].decode())
        assert isinstance(payload["events"], list)
        assert len(payload["events"]) == 5  # init frame replays the recent tail
    finally:
        conn.close()
