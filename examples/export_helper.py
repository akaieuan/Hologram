"""Copyable Blender → glTF export helper — writes the manifest Hologram reads.

This is a **template, not part of the Hologram package.** Hologram's core stays
read-only and `bpy`-free; the write side lives here, in a script you paste into
your own Blender pipeline and adapt. Nothing in the installed package imports it
(it imports `bpy`, which only exists inside Blender), and Hologram never runs it —
it only *reads* the files this produces.

What it does, in one `export_asset(...)` call, is the explogo export convention
generalized:

  1. **Snapshot** the previous export into ``.history/<id>/vN.glb`` and rotate to
     the newest ``HISTORY_KEEP`` versions.
  2. **Export** the selected objects to ``<category>/<id>.glb``.
  3. **Render** a small transparent thumbnail (optional, per-category).
  4. **Upsert** ``manifest.json`` atomically (write-temp + ``os.replace``).
  5. **Append** one line to ``audit.jsonl``.

The resulting ``exports/`` tree is exactly what ``hologram.manifest`` parses and
what the dashboard surfaces (version, generator, params, tri count, thumbnail,
version history). See the README's "Export manifest convention" section for the
schema this writes.

Usage inside Blender (or `blender --background --python your_build.py`)::

    import export_helper as ex
    ex.PROJECT_ROOT = "/path/to/your/project"   # or leave and edit the constant
    ex.export_asset(
        objects=[bpy.data.objects["Hero"]],
        asset_id="hero",
        category="characters",
        generator="pipeline/characters/hero.py",
        params={"height": 0.9, "rig": "biped"},
        note="v3 — larger hands",
    )

Point your ``hologram.toml`` at the same tree::

    [paths]
    export_root = "exports"          # this file's EXPORTS dir, relative to root
"""

import datetime
import json
import math
import os
import shutil

import bpy  # provided by Blender; this template is never imported by the package
from mathutils import Vector

# ── Configuration — edit these for your project ───────────────────────────────
PROJECT_ROOT = "/path/to/your/project"
EXPORTS = os.path.join(PROJECT_ROOT, "exports")

# How many prior versions to keep under .history/<id>/ (older ones are pruned).
HISTORY_KEEP = 5

# Categories that get a rendered thumbnail (usually the web-previewable ones).
# Everything in CATEGORIES is a valid export target; only WEB_CATEGORIES render.
WEB_CATEGORIES = ("characters", "movement", "attachments", "items", "props")
CATEGORIES = WEB_CATEGORIES + ("kits",)


# ── Time + manifest I/O ───────────────────────────────────────────────────────
def _now() -> str:
    """Local ISO-8601 timestamp with offset, second precision (matches the schema)."""
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _load_manifest() -> dict:
    path = os.path.join(EXPORTS, "manifest.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {"assets": {}}


def _save_manifest(manifest: dict) -> None:
    """Atomic upsert: write a temp file then os.replace, so a reader (the
    dashboard) never observes a half-written manifest."""
    path = os.path.join(EXPORTS, "manifest.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _append_audit(event: dict) -> None:
    """One JSON object per line, append-only — the export ledger."""
    with open(os.path.join(EXPORTS, "audit.jsonl"), "a") as f:
        f.write(json.dumps(event) + "\n")


# ── Geometry + GLB export ─────────────────────────────────────────────────────
def _tri_count(objects) -> int:
    deps = bpy.context.evaluated_depsgraph_get()
    total = 0
    for ob in objects:
        if ob.type != "MESH":
            continue
        ev = ob.evaluated_get(deps)
        mesh = ev.to_mesh()
        mesh.calc_loop_triangles()
        total += len(mesh.loop_triangles)
        ev.to_mesh_clear()
    return total


def _export_glb(objects, path, anim_mode="SCENE") -> None:
    for ob in bpy.context.view_layer.objects:
        ob.select_set(False)
    for ob in objects:
        ob.select_set(True)
    bpy.context.view_layer.objects.active = objects[0]
    kwargs = dict(
        filepath=path,
        export_format="GLB",
        use_selection=True,
        export_yup=True,
        export_apply=True,
    )
    try:
        bpy.ops.export_scene.gltf(**kwargs, export_animation_mode=anim_mode)
    except TypeError:
        # Older Blender without export_animation_mode.
        bpy.ops.export_scene.gltf(**kwargs)


def _render_thumbnail(objects, path) -> None:
    """512px transparent 3/4-angle thumbnail with a temporary camera + two suns.
    Everything is torn down afterwards, so your scene is left untouched."""
    scene = bpy.context.scene

    pts = [ob.matrix_world @ Vector(c) for ob in objects for c in ob.bound_box]
    if not pts:
        return
    lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    center = (lo + hi) / 2
    radius = max((hi - lo).length / 2, 0.1)

    temp = []
    cam_data = bpy.data.cameras.new("_thumb_cam")
    cam = bpy.data.objects.new("_thumb_cam", cam_data)
    scene.collection.objects.link(cam)
    temp.append(cam)
    az, el = math.radians(35), math.radians(18)
    dist = radius / math.tan(cam_data.angle / 2) * 1.35
    offset = Vector((
        math.cos(el) * math.sin(az),
        -math.cos(el) * math.cos(az),
        math.sin(el),
    )) * dist
    cam.location = center + offset
    direction = (center - cam.location).normalized()
    cam.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()

    def _sun(name, energy, rot):
        light_data = bpy.data.lights.new(name, "SUN")
        light_data.energy = energy
        light = bpy.data.objects.new(name, light_data)
        light.rotation_euler = rot
        scene.collection.objects.link(light)
        temp.append(light)

    _sun("_thumb_key", 3.0, (math.radians(50), 0, math.radians(25)))
    _sun("_thumb_fill", 1.2, (math.radians(60), 0, math.radians(205)))

    # Snapshot the collection with list() before mutating hide_render — mutating
    # the live bpy collection while iterating it silently skips objects.
    keep = set(objects) | set(temp)
    hidden = []
    for ob in list(scene.collection.all_objects):
        if ob not in keep and not ob.hide_render:
            ob.hide_render = True
            hidden.append(ob)

    r = scene.render
    saved = (r.engine, r.filepath, r.resolution_x, r.resolution_y,
             r.resolution_percentage, r.film_transparent, scene.camera)
    try:
        try:
            r.engine = "BLENDER_EEVEE_NEXT"
        except TypeError:
            r.engine = "BLENDER_EEVEE"
        r.filepath = path
        r.resolution_x = r.resolution_y = 512
        r.resolution_percentage = 100
        r.film_transparent = True
        scene.camera = cam
        bpy.ops.render.render(write_still=True)
    finally:
        (r.engine, r.filepath, r.resolution_x, r.resolution_y,
         r.resolution_percentage, r.film_transparent, scene.camera) = saved
        for ob in hidden:
            ob.hide_render = False
        for ob in temp:
            data = ob.data
            bpy.data.objects.remove(ob, do_unlink=True)
            if isinstance(data, bpy.types.Camera):
                bpy.data.cameras.remove(data)
            elif isinstance(data, bpy.types.Light):
                bpy.data.lights.remove(data)


def _prune(directory, prefix, suffix, keep) -> None:
    """Keep only the newest `keep` files matching prefix + vN + suffix."""
    if not os.path.isdir(directory):
        return
    versioned = []
    for f in os.listdir(directory):
        if f.startswith(prefix) and f.endswith(suffix):
            try:
                versioned.append((int(f[len(prefix):-len(suffix)]), f))
            except ValueError:
                continue
    for _, f in sorted(versioned)[:-keep] if keep else []:
        os.remove(os.path.join(directory, f))


# ── The one public entry point ────────────────────────────────────────────────
def export_asset(objects, asset_id, category, generator, params=None, note="",
                 anim_mode="SCENE") -> dict:
    """Export `objects` as one asset and record it. Returns the manifest entry.

    Snapshots the prior version, exports the GLB, renders a thumbnail (for
    WEB_CATEGORIES), upserts manifest.json, and appends to audit.jsonl. The
    version auto-increments from the manifest; ``createdAt`` is preserved across
    re-exports while ``updatedAt`` moves.
    """
    if category not in CATEGORIES:
        raise ValueError(f"unknown category {category!r}, expected one of {CATEGORIES}")
    if not objects:
        raise ValueError("export_asset called with no objects")
    params = params or {}

    os.makedirs(os.path.join(EXPORTS, category), exist_ok=True)
    os.makedirs(os.path.join(EXPORTS, "thumbnails"), exist_ok=True)

    manifest = _load_manifest()
    prev = manifest["assets"].get(asset_id)
    version = prev["version"] + 1 if prev else 1

    glb_rel = f"{category}/{asset_id}.glb"
    glb_path = os.path.join(EXPORTS, glb_rel)

    # Snapshot the previous version before overwriting, then rotate keep-N.
    if prev and os.path.exists(glb_path):
        hist_dir = os.path.join(EXPORTS, ".history", asset_id)
        os.makedirs(hist_dir, exist_ok=True)
        shutil.copy2(glb_path, os.path.join(hist_dir, f"v{prev['version']}.glb"))
        _prune(hist_dir, "v", ".glb", HISTORY_KEEP)

    _export_glb(objects, glb_path, anim_mode=anim_mode)

    thumb_rel = None
    if category in WEB_CATEGORIES:
        thumb_rel = f"thumbnails/{asset_id}.v{version}.png"
        _render_thumbnail(objects, os.path.join(EXPORTS, thumb_rel))
        _prune(os.path.join(EXPORTS, "thumbnails"), f"{asset_id}.v", ".png", HISTORY_KEEP)

    now = _now()
    tris = _tri_count(objects)
    entry = {
        "id": asset_id,
        "name": asset_id.replace("-", " ").title(),
        "category": category,
        "glb": glb_rel,
        "thumbnail": thumb_rel,
        "version": version,
        "params": params,
        "tris": tris,
        "generator": generator,
        "createdAt": prev["createdAt"] if prev else now,
        "updatedAt": now,
        "status": "pending-review",
    }
    manifest["assets"][asset_id] = entry
    _save_manifest(manifest)
    _append_audit({
        "ts": now,
        "action": "updated" if prev else "created",
        "assetId": asset_id,
        "category": category,
        "generator": generator,
        "params": params,
        "version": version,
        "note": note,
        "thumbnail": thumb_rel,
        "tris": tris,
    })
    return entry
