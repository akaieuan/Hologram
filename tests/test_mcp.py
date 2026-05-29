"""MCP tool logic against the committed examples/minimal project.

We patch the server's `_cfg()` so the tools resolve to the example project
without depending on the process cwd or CLAUDE_PROJECT_DIR.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

import hologram.mcp.server as mcp_server
from hologram import events
from hologram.config import load_config

REPO = Path(__file__).resolve().parent.parent
MINIMAL = REPO / "examples" / "minimal"


@pytest.fixture
def minimal_cfg(monkeypatch, tmp_path):
    # events_log -> tmp so tool calls that _emit() never write into the repo.
    cfg = dataclasses.replace(load_config(str(MINIMAL)), events_log=tmp_path / "events.jsonl")
    monkeypatch.setattr(mcp_server, "_cfg", lambda: cfg)
    return cfg


def test_list_assets_all(minimal_cfg):
    out = mcp_server._list_assets()
    assert out["total"] == 3
    assert set(out["categories"]) == {"lootables", "weapons", "props"}
    assert out["categories"]["props"][0]["name"] == "crate"


def test_list_assets_filter(minimal_cfg):
    out = mcp_server._list_assets("weapons")
    assert out["total"] == 1
    assert list(out["categories"]) == ["weapons"]


def test_inspect_asset_ok(minimal_cfg):
    out = mcp_server._inspect_asset("weapons/sword.glb")
    assert "error" not in out
    assert out["node_count"] == 2
    assert out["animations"] == ["Bob"]
    assert out["path"].endswith("sword.glb")


def test_inspect_asset_missing(minimal_cfg):
    assert "error" in mcp_server._inspect_asset("weapons/ghost.glb")


def test_inspect_asset_rejects_traversal(minimal_cfg):
    assert "error" in mcp_server._inspect_asset("../../../../etc/passwd")


def test_tail_events_roundtrip(minimal_cfg):
    events.append(minimal_cfg.events_log, "mcp_server", action="list_assets")
    out = mcp_server._tail_events(limit=10)
    assert [e["type"] for e in out["events"]] == ["mcp_server"]


def test_tool_wrapper_emits_and_returns(minimal_cfg):
    # The @mcp.tool() wrapper also fires _emit(); ensure the full path works.
    out = mcp_server.list_assets()
    assert out["total"] == 3
    logged = events.tail(minimal_cfg.events_log)
    assert any(e.get("action") == "list_assets" for e in logged)


# ── pipeline_status (Move 3) ─────────────────────────────────────────────────
def test_pipeline_status_buckets_the_log(minimal_cfg):
    log = minimal_cfg.events_log
    events.append(log, "tool_use", phase="post", tool="Bash", command="false",
                  failed=True, error="exit 1")
    events.append(log, "check_run", assets=3, checks=4, errors=1, warnings=2)
    events.append(log, "asset_diff", path="export/gltf/weapons/sword.glb",
                  gained={"materials": ["steel"]}, lost={}, changed={})
    out = mcp_server._pipeline_status()
    assert out["failure_count"] == 1
    assert out["failures"][0]["error"] == "exit 1"
    lc = out["last_check"]
    assert (lc["assets"], lc["checks"], lc["errors"], lc["warnings"]) == (3, 4, 1, 2)
    assert out["recent_diffs"][0]["path"].endswith("sword.glb")


def test_pipeline_status_empty_log_is_safe(minimal_cfg):
    out = mcp_server._pipeline_status()
    assert out["failures"] == []
    assert out["last_check"] is None
    assert out["recent_diffs"] == []


def test_pipeline_status_uses_latest_check_only(minimal_cfg):
    log = minimal_cfg.events_log
    events.append(log, "check_run", assets=1, checks=1, errors=0, warnings=0)
    events.append(log, "check_run", assets=9, checks=9, errors=5, warnings=0)
    assert mcp_server._pipeline_status()["last_check"]["assets"] == 9  # newest wins


def test_pipeline_status_does_not_load_user_checks(monkeypatch, tmp_path):
    """Purity: a broken .hologram/checks.py would crash any code that imported
    it. pipeline_status reads the log only, so it returns cleanly regardless."""
    (tmp_path / "hologram.toml").write_text('[project]\nname = "t"\n', encoding="utf-8")
    (tmp_path / ".hologram").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".hologram" / "checks.py").write_text("import nonexistent_xyz\n", encoding="utf-8")
    cfg = load_config(str(tmp_path))
    monkeypatch.setattr(mcp_server, "_cfg", lambda: cfg)
    out = mcp_server._pipeline_status()  # must not raise
    assert out["failures"] == [] and out["last_check"] is None


# ── render_asset (Move 1) ────────────────────────────────────────────────────
def test_render_asset_missing_never_touches_blender(minimal_cfg, monkeypatch):
    """The containment guard runs first: an unresolved path returns an error
    without ever opening a socket to Blender."""
    called: list[int] = []
    monkeypatch.setattr(mcp_server.blender, "render_glb",
                        lambda *a, **k: called.append(1) or {"ok": False})
    out = mcp_server._render_asset("weapons/ghost.glb")
    assert "error" in out and out.get("ok") is not True
    assert called == []


def test_render_asset_degrades_when_blender_unreachable(minimal_cfg, monkeypatch):
    # Force an unreachable Blender so the tool exercises its degradation path
    # without depending on whether a real Blender is listening on :9876.
    monkeypatch.setattr(mcp_server.blender, "render_glb",
                        lambda *a, **k: {"ok": False, "error": "ConnectionRefusedError",
                                         "image_path": None})
    out = mcp_server._render_asset("weapons/sword.glb")
    assert out.get("ok") is not True
    assert out["path"].endswith("sword.glb")
    assert str(mcp_server.blender.PORT) in out["hint"]  # actionable: tells you the port


def test_render_asset_tool_returns_image_and_emits(minimal_cfg, monkeypatch, tmp_path):
    """Success path through the @mcp.tool() wrapper: a non-empty PNG yields a
    FastMCP Image, the temp file is consumed, and an event is logged."""
    from mcp.server.fastmcp import Image

    png = tmp_path / "fake.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    monkeypatch.setattr(mcp_server.blender, "render_glb",
                        lambda *a, **k: {"ok": True, "image_path": str(png)})
    result = mcp_server.render_asset("weapons/sword.glb")
    assert isinstance(result, Image)
    assert not png.exists()  # the temp render is read then unlinked
    assert any(e.get("action") == "render_asset" for e in events.tail(minimal_cfg.events_log))


def test_render_asset_tool_returns_error_dict_on_failure(minimal_cfg, monkeypatch):
    """Failure path through the wrapper: returns the structured dict (not an
    Image) and logs an error event."""
    monkeypatch.setattr(mcp_server.blender, "render_glb",
                        lambda *a, **k: {"ok": False, "error": "boom", "image_path": None})
    result = mcp_server.render_asset("weapons/sword.glb")
    assert isinstance(result, dict) and "error" in result
    assert any(e.get("action") == "render_asset.error" for e in events.tail(minimal_cfg.events_log))
