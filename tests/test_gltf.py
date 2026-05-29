"""Parse the committed sample GLBs into the Asset struct."""

from __future__ import annotations

from pathlib import Path

import pytest

from hologram.gltf import Asset, load_asset

MINIMAL = Path(__file__).resolve().parent.parent / "examples" / "minimal" / "export" / "gltf"
CRATE = MINIMAL / "props" / "crate.glb"
SWORD = MINIMAL / "weapons" / "sword.glb"


def test_load_crate_basic():
    asset = load_asset(str(CRATE))
    assert isinstance(asset, Asset)
    assert asset.filename == "crate.glb"
    assert asset.stem == "crate"
    assert len(asset.nodes) == 1
    assert asset.materials == ["CrateMaterial"]
    assert asset.animations == []
    assert asset.roots == [0]


def test_load_sword_hierarchy_and_animation():
    asset = load_asset(str(SWORD))
    assert len(asset.nodes) == 2
    assert asset.animations == ["Bob"]
    assert asset.materials == ["SteelBlade"]
    # Single root "Sword" wrapping child "Blade"; top-level = the root's children.
    blade = asset.node_by_name("Blade")
    sword = asset.node_by_name("Sword")
    assert blade is not None and sword is not None
    assert blade.parent == sword.index
    assert blade.index in sword.children
    assert asset.top_level_node_names() == ["Blade"]
    assert [n.name for n in asset.children_of(sword.index)] == ["Blade"]


def test_to_dict_shape():
    d = load_asset(str(SWORD)).to_dict()
    for key in (
        "path", "filename", "stem", "node_count", "animation_count",
        "material_count", "mesh_count", "skin_count", "roots",
        "top_level_nodes", "animations", "materials", "mesh_names", "skins", "nodes",
    ):
        assert key in d
    assert d["node_count"] == 2
    assert d["animation_count"] == 1
    assert d["material_count"] == 1
    assert isinstance(d["nodes"], list) and len(d["nodes"]) == 2
    assert d["nodes"][0]["name"] in ("Blade", "Sword")


def test_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        load_asset(str(MINIMAL / "props" / "does-not-exist.glb"))
