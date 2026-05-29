"""GLB introspection — wraps pygltflib in a flat `Asset` struct.

This is Hologram's stable public surface: anything an agent or the dashboard
asks about a GLB goes through `Asset`. It is intentionally bpy-free (pure
pygltflib) so it runs anywhere, with no Blender install.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Node:
    index: int
    name: str
    parent: int | None  # index into Asset.nodes, or None for roots
    children: list[int] = field(default_factory=list)
    mesh: int | None = None
    skin: int | None = None
    extras: dict[str, Any] = field(default_factory=dict)  # custom properties
    # Local translation relative to the parent; identity when omitted. We do
    # not track a full 4x4 matrix — callers that need world transforms walk
    # the parent chain themselves.
    translation: tuple[float, float, float] = (0.0, 0.0, 0.0)


@dataclass
class Asset:
    path: str
    filename: str  # basename including extension
    stem: str  # basename without extension
    nodes: list[Node]
    roots: list[int]  # indices of nodes with no parent
    animations: list[str]  # animation names
    materials: list[str]  # material names
    skins: list[list[str]]  # list of bone-name lists, one per skin
    mesh_names: list[str]  # mesh names by index

    def node_by_name(self, name: str) -> Node | None:
        for n in self.nodes:
            if n.name == name:
                return n
        return None

    def children_of(self, node_index: int) -> list[Node]:
        return [self.nodes[c] for c in self.nodes[node_index].children]

    def top_level_node_names(self) -> list[str]:
        """Names of direct children of the single root, or all roots otherwise."""
        if len(self.roots) == 1:
            return [self.nodes[c].name for c in self.nodes[self.roots[0]].children]
        return [self.nodes[r].name for r in self.roots]

    def to_dict(self) -> dict:
        """JSON-serializable view, used by the MCP `inspect_asset` tool and the dashboard."""
        return {
            "path": self.path,
            "filename": self.filename,
            "stem": self.stem,
            "node_count": len(self.nodes),
            "animation_count": len(self.animations),
            "material_count": len(self.materials),
            "mesh_count": len(self.mesh_names),
            "skin_count": len(self.skins),
            "roots": self.roots,
            "top_level_nodes": self.top_level_node_names(),
            "animations": self.animations,
            "materials": self.materials,
            "mesh_names": self.mesh_names,
            "skins": self.skins,
            "nodes": [
                {
                    "index": n.index,
                    "name": n.name,
                    "parent": n.parent,
                    "children": n.children,
                    "mesh": n.mesh,
                    "skin": n.skin,
                    "translation": list(n.translation),
                    "extras": n.extras,
                }
                for n in self.nodes
            ],
        }


def load_asset(path: str) -> Asset:
    """Parse a GLB/glTF and return an `Asset`.

    Raises FileNotFoundError if the path doesn't exist, ImportError if pygltflib
    isn't installed, and may raise on malformed files (pygltflib's own errors).
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"GLB not found: {path}")

    try:
        from pygltflib import GLTF2
    except ImportError as e:  # pragma: no cover - dependency guard
        raise ImportError("pygltflib is required. Install with: pip install hologram") from e

    gltf = GLTF2().load(path)
    filename = os.path.basename(path)
    stem, _ = os.path.splitext(filename)

    nodes: list[Node] = []
    parents: dict[int, int] = {}
    for i, n in enumerate(gltf.nodes or []):
        children = list(n.children) if n.children else []
        for c in children:
            parents[c] = i
        extras = dict(n.extras) if isinstance(n.extras, dict) else {}
        t = n.translation if (n.translation and len(n.translation) == 3) else (0.0, 0.0, 0.0)
        nodes.append(
            Node(
                index=i,
                name=n.name or f"<unnamed_{i}>",
                parent=None,  # filled in below
                children=children,
                mesh=n.mesh,
                skin=n.skin,
                extras=extras,
                translation=(float(t[0]), float(t[1]), float(t[2])),
            )
        )
    for i, node in enumerate(nodes):
        node.parent = parents.get(i)

    roots = [i for i, n in enumerate(nodes) if n.parent is None]
    animations = [a.name or f"<anim_{i}>" for i, a in enumerate(gltf.animations or [])]
    materials = [m.name or f"<mat_{i}>" for i, m in enumerate(gltf.materials or [])]
    mesh_names = [m.name or f"<mesh_{i}>" for i, m in enumerate(gltf.meshes or [])]

    skins: list[list[str]] = []
    for s in gltf.skins or []:
        bone_names = []
        for joint_index in s.joints or []:
            if 0 <= joint_index < len(nodes):
                bone_names.append(nodes[joint_index].name)
        skins.append(bone_names)

    return Asset(
        path=path,
        filename=filename,
        stem=stem,
        nodes=nodes,
        roots=roots,
        animations=animations,
        materials=materials,
        skins=skins,
        mesh_names=mesh_names,
    )
