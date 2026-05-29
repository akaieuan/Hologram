"""The `/api/glb` preview route. Exercises the real Handler._glb against the
committed sample project, asserting the shared resolve_asset boundary rejects
traversal and that valid assets stream as raw GLB bytes."""

from __future__ import annotations

import io
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
