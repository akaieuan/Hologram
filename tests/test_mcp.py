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
