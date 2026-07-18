"""The manifest reader + the manifest/history/thumb dashboard routes.

Two layers, mirroring test_dashboard.py:

* ``hologram.manifest`` is unit-tested directly against a committed, explogo-
  shaped fixture under ``tests/fixtures/manifest-project/`` (copied GLBs — the
  tests never touch the real explogo repo).
* The server routes are exercised end-to-end against a real ``ThreadingHTTPServer``
  bound to an ephemeral port, driven by stdlib ``http.client``. A second server
  bound to ``examples/minimal`` (which ships no manifest) proves the
  absent-manifest path is zero behaviour change.
"""

from __future__ import annotations

import http.client
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import hologram.dashboard.server as server
from hologram import manifest as M
from hologram.config import load_config

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests" / "fixtures" / "manifest-project"
MINIMAL = REPO / "examples" / "minimal"


@pytest.fixture
def cfg():
    return load_config(str(FIXTURE))


def _mani(config) -> M.Manifest:
    """Load the fixture manifest, asserting it is present (narrows Optional)."""
    mani = M.load_manifest(config)
    assert mani is not None
    return mani


def _rec(config, asset_id: str) -> M.AssetRecord:
    rec = _mani(config).get(asset_id)
    assert rec is not None
    return rec


# ── manifest.py: loading + records ────────────────────────────────────────────

def test_load_manifest_parses_fixture(cfg):
    mani = M.load_manifest(cfg)
    assert mani is not None
    assert len(mani) == 2
    assert set(mani.records) == {"hero", "coin"}


def test_absent_manifest_is_none():
    # examples/minimal ships no manifest.json — must degrade to None, not raise.
    assert M.load_manifest(load_config(str(MINIMAL))) is None


def test_asset_record_fields_and_camelcase(cfg):
    hero = _rec(cfg, "hero")
    assert hero.version == 3
    assert hero.generator == "pipeline/characters/hero.py"
    assert hero.tris == 3000
    assert hero.category == "characters"
    assert hero.status == "pending-review"
    assert hero.thumbnail == "thumbnails/hero.v3.png"
    # camelCase source keys land on snake_case fields
    assert hero.created_at == "2026-07-04T12:01:26-04:00"
    assert hero.updated_at == "2026-07-07T11:31:18-04:00"
    # nested params preserved verbatim
    assert hero.params["effects"] == {"jump_mult": 1.45}
    assert hero.params["forward"] == [0, 0, 1]


def test_asset_record_null_thumbnail(cfg):
    coin = _rec(cfg, "coin")
    assert coin.thumbnail is None
    assert coin.version == 1


def test_asset_record_stem_and_by_stem(cfg):
    mani = _mani(cfg)
    hero = mani.get("hero")
    assert hero is not None
    assert hero.stem == "hero"
    # the join the server uses to enrich a discovered GLB
    assert mani.by_stem("hero") is hero
    assert mani.by_stem("nope") is None


def test_asset_record_to_dict_shape(cfg):
    d = _rec(cfg, "hero").to_dict()
    assert d["id"] == "hero"
    assert d["version"] == 3
    assert d["tris"] == 3000
    assert d["created_at"].startswith("2026-07-04")
    assert "params" in d and isinstance(d["params"], dict)


def test_unknown_keys_preserved_in_extra():
    entry = {"id": "x", "version": 2, "glb": "c/x.glb", "custom_key": 42, "lodBias": 0.5}
    rec = M.AssetRecord.from_entry("x", entry)
    assert rec.extra == {"custom_key": 42, "lodBias": 0.5}
    assert rec.to_dict()["extra"] == {"custom_key": 42, "lodBias": 0.5}


def test_manifest_to_dict(cfg):
    d = _mani(cfg).to_dict()
    assert d["count"] == 2
    assert set(d["assets"]) == {"hero", "coin"}


def test_malformed_manifest_returns_none(tmp_path):
    root = tmp_path / "proj"
    exp = root / "exports"
    exp.mkdir(parents=True)
    (root / "hologram.toml").write_text(
        '[project]\nname="x"\n[paths]\nexport_root="exports"\n', encoding="utf-8"
    )
    (exp / "manifest.json").write_text("{ not json", encoding="utf-8")
    assert M.load_manifest(load_config(str(root))) is None
    # wrong shape (no "assets" dict) also degrades to None
    (exp / "manifest.json").write_text('{"assets": []}', encoding="utf-8")
    assert M.load_manifest(load_config(str(root))) is None


# ── manifest.py: safe ids + history ───────────────────────────────────────────

@pytest.mark.parametrize("aid,ok", [
    ("hero", True), ("battery-pack", True), ("a.b_c-1", True),
    ("", False), ("..", False), ("../etc", False), ("a/b", False),
    (".hidden", False), ("a b", False),
])
def test_is_safe_id(aid, ok):
    assert M.is_safe_id(aid) is ok


def test_history_versions_sorted(cfg):
    assert M.history_versions(cfg, "hero") == [1, 2]
    assert M.history_versions(cfg, "coin") == [1]


def test_history_versions_absent_or_unsafe(cfg):
    assert M.history_versions(cfg, "does-not-exist") == []
    assert M.history_versions(cfg, "../etc") == []


def test_history_glb_resolves_within_root(cfg):
    p = M.history_glb(cfg, "hero", 2)
    assert p is not None and p.is_file()
    assert p.name == "v2.glb"
    # missing version, unsafe id, and traversal all yield None
    assert M.history_glb(cfg, "hero", 9) is None
    assert M.history_glb(cfg, "..", 1) is None
    assert M.history_glb(cfg, "hero", -1) is None


# ── manifest.py: audit ────────────────────────────────────────────────────────

def test_load_audit_newest_first(cfg):
    aud = M.load_audit(cfg)
    assert len(aud) == 5
    # last line in the fixture is the coin creation
    assert aud[0]["assetId"] == "coin"
    assert aud[0]["action"] == "created"


def test_load_audit_schema_agnostic(cfg):
    # the review line carries actor/decision keys the export lines don't have
    aud = M.load_audit(cfg)
    review = [a for a in aud if a.get("action") == "review"]
    assert review and review[0]["decision"] == "changes-requested"
    assert review[0]["actor"] == "reviewer"


def test_load_audit_filter_and_limit(cfg):
    assert len(M.load_audit(cfg, asset_id="hero")) == 4
    assert len(M.load_audit(cfg, asset_id="coin")) == 1
    assert len(M.load_audit(cfg, limit=2)) == 2


def test_load_audit_absent_is_empty():
    assert M.load_audit(load_config(str(MINIMAL))) == []


# ── Live server ───────────────────────────────────────────────────────────────

def _live_server(project: Path):
    """Serve `project` on an ephemeral port; return (httpd, thread, host, port)."""
    server.CONFIG = load_config(str(project))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    host, port = httpd.server_address[0], httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02},
                              daemon=True)
    thread.start()
    return httpd, thread, host, port


@pytest.fixture
def manifest_server(monkeypatch):
    monkeypatch.setattr(server, "CONFIG", load_config(str(FIXTURE)))
    httpd, thread, host, port = _live_server(FIXTURE)
    try:
        yield host, port
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture
def minimal_server(monkeypatch):
    monkeypatch.setattr(server, "CONFIG", load_config(str(MINIMAL)))
    httpd, thread, host, port = _live_server(MINIMAL)
    try:
        yield host, port
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _get(host, port, path, timeout=5.0):
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, resp.getheader("Content-Type"), resp.read()
    finally:
        conn.close()


def _get_json(host, port, path):
    status, ctype, body = _get(host, port, path)
    return status, ctype, json.loads(body)


# ── /api/manifest ─────────────────────────────────────────────────────────────

def test_api_manifest_present(manifest_server):
    host, port = manifest_server
    status, ctype, data = _get_json(host, port, "/api/manifest")
    assert status == 200
    assert ctype == "application/json; charset=utf-8"
    assert data["present"] is True
    assert data["count"] == 2
    assert set(data["assets"]) == {"hero", "coin"}
    assert data["assets"]["hero"]["generator"] == "pipeline/characters/hero.py"


def test_api_manifest_absent(minimal_server):
    host, port = minimal_server
    status, _, data = _get_json(host, port, "/api/manifest")
    assert status == 200
    assert data == {"present": False, "count": 0, "assets": {}}


# ── /api/state enrichment ─────────────────────────────────────────────────────

def test_api_state_enriched(manifest_server):
    host, port = manifest_server
    # force=1 busts the 2s cross-test state cache (the whole suite runs < 2s).
    status, _, data = _get_json(host, port, "/api/state?force=1")
    assert status == 200
    assert data["has_manifest"] is True
    entries = {e["name"]: e for es in data["categories"].values() for e in es}
    assert set(entries) == {"hero", "coin"}
    hero = entries["hero"]["manifest"]
    assert hero["version"] == 3
    assert hero["generator"] == "pipeline/characters/hero.py"
    assert hero["tris"] == 3000
    assert hero["thumbnail"] == "thumbnails/hero.v3.png"
    assert entries["coin"]["manifest"]["thumbnail"] is None


def test_api_state_no_manifest_unchanged(minimal_server):
    # Absent manifest = zero behaviour change: no `manifest` key on any entry.
    host, port = minimal_server
    status, _, data = _get_json(host, port, "/api/state?force=1")
    assert status == 200
    assert data["has_manifest"] is False
    for entries in data["categories"].values():
        for e in entries:
            assert "manifest" not in e


# ── /api/history ──────────────────────────────────────────────────────────────

def test_api_history_lists_versions(manifest_server):
    host, port = manifest_server
    status, _, data = _get_json(host, port, "/api/history?asset=hero")
    assert status == 200
    assert data["asset"] == "hero"
    assert data["versions"] == [1, 2]
    assert data["current_version"] == 3
    assert data["has_current"] is True
    assert data["record"]["generator"] == "pipeline/characters/hero.py"


def test_api_history_introspect_and_diff_vs_current(manifest_server):
    host, port = manifest_server
    status, _, data = _get_json(host, port, "/api/history?asset=hero&v=2")
    assert status == 200
    assert data["version"] == 2
    assert data["compared_from"] == "v2"
    assert data["compared_to"] == "current"
    # introspection of the snapshot itself
    assert data["inspect"]["stem"] == "v2"
    assert "node_count" in data["inspect"]
    # diff v2 -> current is non-empty (crate → sword)
    assert data["diff"]
    assert "gained" in data["diff"] or "lost" in data["diff"]


def test_api_history_diff_between_two_versions(manifest_server):
    host, port = manifest_server
    status, _, data = _get_json(host, port, "/api/history?asset=hero&v=2&base=1")
    assert status == 200
    assert data["compared_from"] == "v1"
    assert data["compared_to"] == "v2"
    assert data["diff"]  # coin (v1) vs crate (v2) differ


def test_api_history_missing_asset_is_400(manifest_server):
    host, port = manifest_server
    status, _, data = _get_json(host, port, "/api/history")
    assert status == 400
    assert "error" in data


def test_api_history_unsafe_asset_is_400(manifest_server):
    host, port = manifest_server
    status, _, data = _get_json(host, port, "/api/history?asset=../etc")
    assert status == 400


def test_api_history_bad_version_is_400(manifest_server):
    host, port = manifest_server
    status, _, data = _get_json(host, port, "/api/history?asset=hero&v=notint")
    assert status == 400


def test_api_history_missing_snapshot_is_404(manifest_server):
    host, port = manifest_server
    status, _, data = _get_json(host, port, "/api/history?asset=hero&v=99")
    assert status == 404
    assert data["versions"] == [1, 2]


# ── /api/thumb ────────────────────────────────────────────────────────────────

def test_api_thumb_streams_png(manifest_server):
    host, port = manifest_server
    status, ctype, body = _get(host, port, "/api/thumb?asset=hero")
    assert status == 200
    assert ctype == "image/png"
    assert body[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic


def test_api_thumb_no_thumbnail_is_404(manifest_server):
    # coin's manifest thumbnail is null
    host, port = manifest_server
    status, _, _ = _get(host, port, "/api/thumb?asset=coin")
    assert status == 404


def test_api_thumb_missing_asset_is_400(manifest_server):
    host, port = manifest_server
    status, _, _ = _get(host, port, "/api/thumb")
    assert status == 400


def test_api_thumb_unknown_asset_is_404(manifest_server):
    host, port = manifest_server
    status, _, _ = _get(host, port, "/api/thumb?asset=ghost")
    assert status == 404
