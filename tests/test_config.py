"""Config load + asset discovery for both the categorized and flat layouts,
plus the path-traversal security boundary."""

from __future__ import annotations

from pathlib import Path

from hologram.config import load_config, resolve_within

REPO = Path(__file__).resolve().parent.parent
MINIMAL = REPO / "examples" / "minimal"
FLAT = REPO / "examples" / "flat-layout"


def test_minimal_categorized_layout():
    cfg = load_config(str(MINIMAL))
    assert cfg.name == "minimal-example"
    names = {c.name for c in cfg.categories}
    assert names == {"lootables", "weapons", "props"}

    by_cat = cfg.list_glbs()
    assert [p.stem for p in by_cat["props"]] == ["crate"]
    assert [p.stem for p in by_cat["weapons"]] == ["sword"]
    assert [p.stem for p in by_cat["lootables"]] == ["coin"]

    # Filtering to one category returns only that category.
    only = cfg.list_glbs("weapons")
    assert set(only) == {"weapons"}


def test_flat_layout_synthesizes_all_category():
    cfg = load_config(str(FLAT))
    assert cfg.name == "flat-example"
    assert [c.name for c in cfg.categories] == ["all"]
    by_cat = cfg.list_glbs()
    assert sorted(p.stem for p in by_cat["all"]) == ["rock", "tree"]


def test_dashboard_defaults_and_overrides():
    cfg_min = load_config(str(MINIMAL))
    assert cfg_min.dashboard_port == 7870
    cfg_flat = load_config(str(FLAT))
    assert cfg_flat.dashboard_port == 7871
    assert cfg_flat.dashboard_host == "127.0.0.1"


def test_resolve_asset_within_project():
    cfg = load_config(str(MINIMAL))
    # Relative to export_root.
    resolved = cfg.resolve_asset("props/crate.glb")
    assert resolved is not None and resolved.name == "crate.glb"
    # Nonexistent inside project -> None.
    assert cfg.resolve_asset("props/ghost.glb") is None


def test_resolve_asset_rejects_traversal():
    cfg = load_config(str(MINIMAL))
    assert cfg.resolve_asset("../../../../etc/passwd") is None


def test_resolve_within_boundary(tmp_path):
    base = tmp_path / "proj"
    base.mkdir()
    (base / "ok.glb").write_text("x")
    assert resolve_within(base, "ok.glb") == (base / "ok.glb").resolve()
    assert resolve_within(base, "../escape") is None
    assert resolve_within(base, "/etc/passwd") is None
