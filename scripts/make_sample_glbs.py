#!/usr/bin/env python3
"""Generate the tiny, valid sample GLBs committed under examples/.

Run once (outputs are committed):  python scripts/make_sample_glbs.py

Each file is a minimal unit cube. We vary node hierarchy, materials, and add a
short translation animation to one asset so the dashboard's GLB drawer has
something interesting to show (nodes / meshes / animations / materials counts).
No Blender required — pure pygltflib.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pygltflib
from pygltflib import (
    GLTF2,
    Accessor,
    Animation,
    AnimationChannel,
    AnimationSampler,
    Attributes,
    Buffer,
    BufferView,
    Material,
    Mesh,
    Node,
    Primitive,
    Scene,
)

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

# Unit cube: 8 corners, 12 triangles.
CUBE_VERTS = [
    (-0.5, -0.5, -0.5), (0.5, -0.5, -0.5), (0.5, 0.5, -0.5), (-0.5, 0.5, -0.5),
    (-0.5, -0.5, 0.5), (0.5, -0.5, 0.5), (0.5, 0.5, 0.5), (-0.5, 0.5, 0.5),
]
CUBE_INDICES = [
    0, 1, 2, 0, 2, 3,  # back
    4, 6, 5, 4, 7, 6,  # front
    0, 4, 5, 0, 5, 1,  # bottom
    3, 2, 6, 3, 6, 7,  # top
    0, 3, 7, 0, 7, 4,  # left
    1, 5, 6, 1, 6, 2,  # right
]


def _pad4(b: bytes) -> bytes:
    return b + b"\x00" * ((4 - len(b) % 4) % 4)


class Blob:
    """Accumulates 4-byte-aligned binary chunks and tracks their bufferViews."""

    def __init__(self) -> None:
        self.data = bytearray()
        self.views: list[BufferView] = []

    def add(self, raw: bytes, target: int | None = None) -> int:
        offset = len(self.data)
        self.data.extend(_pad4(raw))
        self.views.append(
            BufferView(buffer=0, byteOffset=offset, byteLength=len(raw), target=target)
        )
        return len(self.views) - 1


def build_glb(
    path: Path,
    *,
    node_name: str,
    child_name: str | None = None,
    material_name: str | None = None,
    animate: bool = False,
) -> None:
    blob = Blob()
    accessors: list[Accessor] = []

    # POSITION
    pos_bytes = b"".join(struct.pack("<3f", *v) for v in CUBE_VERTS)
    pos_view = blob.add(pos_bytes, target=pygltflib.ARRAY_BUFFER)
    mins = [min(v[i] for v in CUBE_VERTS) for i in range(3)]
    maxs = [max(v[i] for v in CUBE_VERTS) for i in range(3)]
    accessors.append(Accessor(
        bufferView=pos_view, componentType=pygltflib.FLOAT, count=len(CUBE_VERTS),
        type=pygltflib.VEC3, min=mins, max=maxs,
    ))
    pos_accessor = len(accessors) - 1

    # indices
    idx_bytes = struct.pack(f"<{len(CUBE_INDICES)}H", *CUBE_INDICES)
    idx_view = blob.add(idx_bytes, target=pygltflib.ELEMENT_ARRAY_BUFFER)
    accessors.append(Accessor(
        bufferView=idx_view, componentType=pygltflib.UNSIGNED_SHORT,
        count=len(CUBE_INDICES), type=pygltflib.SCALAR,
    ))
    idx_accessor = len(accessors) - 1

    materials: list[Material] = []
    mat_index = None
    if material_name:
        materials.append(Material(name=material_name))
        mat_index = 0

    mesh = Mesh(
        name=f"{node_name}Mesh",
        primitives=[Primitive(
            attributes=Attributes(POSITION=pos_accessor),
            indices=idx_accessor,
            material=mat_index,
        )],
    )

    nodes: list[Node] = []
    if child_name:
        # parent (no mesh) -> child (mesh). Animate the child if requested.
        child = Node(name=child_name, mesh=0, translation=[0.0, 0.0, 0.0])
        nodes.append(child)  # index 0
        parent = Node(name=node_name, children=[0])
        nodes.append(parent)  # index 1
        root_index = 1
        animated_node = 0
    else:
        nodes.append(Node(name=node_name, mesh=0, translation=[0.0, 0.0, 0.0]))
        root_index = 0
        animated_node = 0

    animations: list[Animation] = []
    if animate:
        times = [0.0, 0.5, 1.0]
        time_bytes = struct.pack(f"<{len(times)}f", *times)
        time_view = blob.add(time_bytes)
        accessors.append(Accessor(
            bufferView=time_view, componentType=pygltflib.FLOAT, count=len(times),
            type=pygltflib.SCALAR, min=[times[0]], max=[times[-1]],
        ))
        time_accessor = len(accessors) - 1

        offsets = [(0.0, 0.0, 0.0), (0.0, 0.25, 0.0), (0.0, 0.0, 0.0)]
        out_bytes = b"".join(struct.pack("<3f", *o) for o in offsets)
        out_view = blob.add(out_bytes)
        accessors.append(Accessor(
            bufferView=out_view, componentType=pygltflib.FLOAT, count=len(offsets),
            type=pygltflib.VEC3,
        ))
        out_accessor = len(accessors) - 1

        animations.append(Animation(
            name="Bob",
            samplers=[AnimationSampler(input=time_accessor, output=out_accessor, interpolation="LINEAR")],
            channels=[AnimationChannel(
                sampler=0,
                target={"node": animated_node, "path": "translation"},
            )],
        ))

    gltf = GLTF2(
        scene=0,
        scenes=[Scene(nodes=[root_index])],
        nodes=nodes,
        meshes=[mesh],
        materials=materials,
        accessors=accessors,
        bufferViews=blob.views,
        buffers=[Buffer(byteLength=len(blob.data))],
        animations=animations,
    )
    gltf.set_binary_blob(bytes(blob.data))

    path.parent.mkdir(parents=True, exist_ok=True)
    gltf.save_binary(str(path))
    print(f"wrote {path.relative_to(REPO)}")


def main() -> int:
    minimal = REPO / "examples" / "minimal" / "export" / "gltf"
    build_glb(minimal / "props" / "crate.glb",
              node_name="Crate", material_name="CrateMaterial")
    build_glb(minimal / "weapons" / "sword.glb",
              node_name="Sword", child_name="Blade",
              material_name="SteelBlade", animate=True)
    build_glb(minimal / "lootables" / "coin.glb",
              node_name="Coin", material_name="GoldMaterial")

    flat = REPO / "examples" / "flat-layout" / "assets"
    build_glb(flat / "tree.glb", node_name="Tree", material_name="BarkMaterial")
    build_glb(flat / "rock.glb", node_name="Rock")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
