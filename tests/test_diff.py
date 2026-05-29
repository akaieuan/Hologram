"""Regression diffing: the fingerprint, the diff algebra, the snapshot store,
the asset_diff emit in run_project (checkpoint), and the read-only diff on
/api/inspect.

Engine tests build Asset structs by hand (no GLB on disk); the integration
tests reuse the committed examples/minimal project and the same _Recorder
handler shim as test_dashboard.py / test_checks.py — with events_log redirected
to tmp so nothing is written into the repo.
"""

from __future__ import annotations

import dataclasses
import io
import json
from pathlib import Path

import pytest

import hologram.dashboard.server as server
from hologram import diff as diff_mod
from hologram import events
from hologram.checks import run_project
from hologram.config import load_config
from hologram.gltf import Asset, Node

REPO = Path(__file__).resolve().parent.parent
MINIMAL = REPO / "examples" / "minimal"

# A summary of an "empty" asset — used to seed a stale baseline so a real export
# reads as fully changed.
EMPTY_SUMMARY = {
    "stem": "stale",
    "counts": {"nodes": 0, "meshes": 0, "materials": 0, "animations": 0, "skins": 0},
    "materials": [], "meshes": [], "animations": [], "top_level": [], "nodes": [],
}


def mk_asset(*, nodes=None, materials=None, mesh_names=None, animations=None,
             skins=None, filename="synthetic.glb") -> Asset:
    nodes = nodes if nodes is not None else [Node(index=0, name="Root", parent=None)]
    return Asset(
        path=f"/tmp/{filename}",
        filename=filename,
        stem=filename.rsplit(".", 1)[0],
        nodes=nodes,
        roots=[i for i, n in enumerate(nodes) if n.parent is None],
        animations=animations or [],
        materials=materials or [],
        skins=skins or [],
        mesh_names=mesh_names or [],
    )


# ── summarize ──────────────────────────────────────────────────────────────────
def test_summarize_is_deterministic():
    a = mk_asset(materials=["b", "a"], mesh_names=["m2", "m1"], animations=["x"])
    assert diff_mod.summarize(a) == diff_mod.summarize(a)


def test_summarize_sorts_names_and_counts():
    a = mk_asset(materials=["zinc", "amber"], mesh_names=["body"], animations=["Run", "Idle"])
    s = diff_mod.summarize(a)
    assert s["materials"] == ["amber", "zinc"]
    assert s["animations"] == ["Idle", "Run"]
    assert s["counts"] == {"nodes": 1, "meshes": 1, "materials": 2, "animations": 2, "skins": 0}


# ── diff algebra ─────────────────────────────────────────────────────────────────
def test_diff_first_seen_when_no_baseline():
    assert diff_mod.diff(None, diff_mod.summarize(mk_asset())) == {"first_seen": True}


def test_diff_identical_is_empty():
    s = diff_mod.summarize(mk_asset(materials=["a"], animations=["Idle"]))
    assert diff_mod.diff(s, s) == {}


def test_diff_reports_gained_and_lost_names():
    prev = diff_mod.summarize(mk_asset(materials=["a", "b"]))
    curr = diff_mod.summarize(mk_asset(materials=["a", "c"]))
    d = diff_mod.diff(prev, curr)
    assert d["gained"]["materials"] == ["c"]
    assert d["lost"]["materials"] == ["b"]


def test_diff_reports_count_changes():
    prev = diff_mod.summarize(mk_asset(animations=["Idle"]))
    curr = diff_mod.summarize(mk_asset(animations=["Idle", "Run"]))
    d = diff_mod.diff(prev, curr)
    assert d["changed"]["animations"] == {"from": 1, "to": 2}
    assert d["gained"]["animations"] == ["Run"]


# ── snapshot store ───────────────────────────────────────────────────────────────
def _project(tmp_path):
    (tmp_path / "hologram.toml").write_text('[project]\nname = "t"\n', encoding="utf-8")
    return load_config(str(tmp_path))


def test_snapshot_roundtrip(tmp_path):
    cfg = _project(tmp_path)
    s = diff_mod.summarize(mk_asset(materials=["a"]))
    assert diff_mod.load_snapshot(cfg, "x/hero.glb") is None
    diff_mod.save_snapshot(cfg, "x/hero.glb", s)
    assert diff_mod.load_snapshot(cfg, "x/hero.glb") == s


def test_snapshot_path_under_hologram_snapshots(tmp_path):
    cfg = _project(tmp_path)
    p = diff_mod.snapshot_path(cfg, "weapons/sword.glb")
    assert p.parent == cfg.events_log.parent / "snapshots"
    assert p.name == "weapons__sword.glb.json"


def test_load_snapshot_corrupt_returns_none(tmp_path):
    cfg = _project(tmp_path)
    p = diff_mod.snapshot_path(cfg, "x.glb")
    p.parent.mkdir(parents=True)
    p.write_text("{not json", encoding="utf-8")
    assert diff_mod.load_snapshot(cfg, "x.glb") is None


# ── run_project checkpoint (emit) ──────────────────────────────────────────────
def test_run_project_emit_appends_asset_diff_when_changed(tmp_path):
    base = load_config(str(MINIMAL))
    cfg = dataclasses.replace(base, events_log=tmp_path / "events.jsonl")
    # Seed a stale baseline for sword so the real export reads as changed.
    diff_mod.save_snapshot(cfg, base.export_root / "weapons" / "sword.glb", EMPTY_SUMMARY)
    run_project(cfg, emit=True)
    diffs = [e for e in events.tail(cfg.events_log, limit=50) if e.get("type") == "asset_diff"]
    assert any(e["path"].endswith("sword.glb") for e in diffs)


def test_run_project_emit_no_diff_for_first_seen(tmp_path):
    # No baseline at all → every asset is first-seen → no asset_diff events.
    base = load_config(str(MINIMAL))
    cfg = dataclasses.replace(base, events_log=tmp_path / "events.jsonl")
    run_project(cfg, emit=True)
    diffs = [e for e in events.tail(cfg.events_log, limit=50) if e.get("type") == "asset_diff"]
    assert diffs == []


def test_run_project_no_emit_writes_no_snapshots(tmp_path):
    # Passive (emit=False) callers stay fully read-only: no baseline writes.
    base = load_config(str(MINIMAL))
    cfg = dataclasses.replace(base, events_log=tmp_path / "events.jsonl")
    run_project(cfg, emit=False)
    assert not (tmp_path / "snapshots").exists()


# ── /api/inspect read-only diff (handler shim) ─────────────────────────────────
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
def handler(monkeypatch, tmp_path):
    cfg = dataclasses.replace(load_config(str(MINIMAL)), events_log=tmp_path / "events.jsonl")
    monkeypatch.setattr(server, "CONFIG", cfg)
    h = server.Handler.__new__(server.Handler)
    rec = _Recorder()
    for name in ("send_response", "send_error", "send_header", "end_headers"):
        setattr(h, name, getattr(rec, name))
    h.wfile = rec.wfile
    return h, rec, cfg


def _body(rec) -> dict:
    return json.loads(rec.wfile.getvalue().decode("utf-8"))


def test_inspect_attaches_diff_against_baseline(handler):
    h, rec, cfg = handler
    diff_mod.save_snapshot(cfg, cfg.export_root / "weapons" / "sword.glb", EMPTY_SUMMARY)
    h._inspect("export/gltf/weapons/sword.glb")
    assert rec.status == 200
    assert "diff" in _body(rec)


def test_inspect_no_baseline_no_diff(handler):
    h, rec, cfg = handler
    h._inspect("export/gltf/weapons/sword.glb")
    assert rec.status == 200
    assert "diff" not in _body(rec)


def test_inspect_does_not_write_snapshot(handler):
    h, rec, cfg = handler
    h._inspect("export/gltf/weapons/sword.glb")
    assert rec.status == 200
    # GET stays read-only — inspecting never establishes a baseline.
    assert diff_mod.load_snapshot(cfg, cfg.export_root / "weapons" / "sword.glb") is None
