---
name: Blender 5.2 simulation cheat sheet
description: Quick reference for Blender 5.2's XPBD physics — the Geometry-Nodes cloth/hair system, effectors and colliders, the bake-to-GLB pathway for every sim type, and the 5.2 API changes that break headless export scripts. Use when someone asks how to sim cloth or hair, bake physics into a GLB, drape or sag a mesh, or why keyframes or GN inputs behave differently since 5.2.
kind: reference
---

# Blender 5.2 physics & simulation — cheat sheet

When invoked, print the reference below. Quick lookup — do **not** read files or
run tools. (Digest of the 5.2 LTS release notes and community testing, July 2026;
the long-form version lives in `docs/research/blender-52-simulation.md` of the
Hologram repo.)

---

## The new system (5.2, experimental)

**XPBD Solver node** — Geometry-Nodes-native, constraint-based (the solver family
modern game/film cloth uses). ~2–4× faster than legacy cloth in reference tests.
Two ready-made assets sit on top of it:

| Asset | Use | Notes |
|---|---|---|
| **Cloth Dynamics** | modifier + node group on any mesh | pinning, stretch/bend, tearing. NO self-collision or pressure yet. |
| **Hair Dynamics** | `Add > Curves > Empty Hair` auto-wires it | needs an attach surface + Capture Rest Geometry modifier; each curve sims as a Cosserat rod |

**Effectors** — collisions and forces are *explicit*, nothing is auto-detected:
- **Collider** — any closed mesh, via the Collider modifier, collected into the
  collection you assign as the cloth's effectors
- **Custom Force** — per-point force vectors. Only gravity is built in; wind /
  turbulence / drag = build a Custom Force on an Empty (Empties host GN modifiers now)
- **Custom Effector** — a closure injected at a solver stage (e.g. Post Solve)

**Working values:** stretch/bend stiffness above ~0.25 goes rubbery — stay under
it. Meshes need density for collisions to register — subdivide at least once.
Pin the attachment edge (pole edge, rim, hem).

**It is experimental.** The node design may change; don't build load-bearing
tooling against node-group internals — wrap your recipe in one script or skill
so there's a single place to fix.

## Bake-to-GLB pathways (per sim type)

glTF has no runtime physics — **sim at authoring time, export baked geometry**.

| Sim | Export-safe route |
|---|---|
| XPBD Cloth/Hair, Simulation Zone | Bake node (Mode = Still for a frozen pose) → evaluated mesh exports like any modifier result; or scrub to the good frame and apply the modifier |
| Classic cloth / soft body | scrub to frame → `bpy.ops.object.modifier_apply` freezes the drape |
| Rigid body | `bpy.ops.rigidbody.bake_to_keyframes(frame_start, frame_end, step)` → real object keyframes, glTF exports them natively — the only rigid-body path that survives export |
| Fluid | cache bake → mesh sequence; no keyframe path |

Classic systems (legacy cloth, rigid body, soft body, Mantaflow, particle hair)
are unchanged in 5.2 and coexist with XPBD.

## ⚠ 5.2 gotchas for headless scripts

1. **"Only Insert Needed" keyframe preference now defaults ON** — `keyframe_insert()`
   silently skips keys that match the current value, so a hold pose keyed on two
   frames melts when a later key changes. Fix in any baking script's preamble:
   `bpy.context.preferences.edit.use_keyframe_insert_needed = False`, or pass
   explicit options per call.
2. **GN modifier inputs are real RNA now** — `modifier["Input_2"]` is dead. Use
   `modifier.properties.inputs.<identifier>.value` (and `.type = "ATTRIBUTE"` +
   `.attribute_name` for attribute inputs); outputs likewise. Any XPBD scripting
   must use the new API from day one.
3. **The glTF exporter sorts nodes by name and inlines materials when possible** —
   output can differ byte-for-byte from 5.1 with identical visuals. Don't hash
   GLBs for parity across Blender versions; compare *visually* (golden thumbnails).
4. **Unchanged / safe:** bmesh, armature/bone data, action/F-curve APIs, and the
   glTF export operator's parameters (`export_yup`, `export_extras`, skins).

## Also new and useful

- **Auto IK** works through disconnected parents, scroll-wheel chain depth while
  dragging — easier grip/contact posing (pairs with `/hologram:pose-authoring`).
- **Pose Library auto-converts rotation modes** (quat ↔ Euler ↔ axis-angle) on
  apply — used to silently corrupt orientation.
- **Mesh Bevel node** (GN) — procedural edge-rounding before export;
  **Cluster by Distance / by Connected** — procedural damaged/broken variants.
- Playback **Stop-at-End / Bounce** modes — auditing loop seams on cycle clips.
