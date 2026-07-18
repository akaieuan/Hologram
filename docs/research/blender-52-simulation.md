# Blender 5.2 simulation & headless-export research digest

*Digest, July 2026. Sources: official Blender 5.2 LTS release notes
(developer.blender.org — physics, python_api, pipeline_io, animation_rigging,
geometry_nodes), the 5.2 manual, feature-overview videos, and community XPBD
testing writeups. This is a digest with adoption notes, not a copy — verify
against the release notes before depending on a detail.*

The condensed version ships as the plugin skill `/hologram:blender-sim`; this
document keeps the longer reasoning and the adopt / don't-adopt calls.

---

## 1. The headline: a node-based physics system

5.2 ships an experimental **Geometry-Nodes-native physics stack** — the first
new solver family in years, built on an **XPBD Solver node** (extended
position-based dynamics, the same family as Houdini's and most game/film cloth).
Reported ~2–4× faster than legacy cloth depending on the benchmark.

| Piece | What it is |
|---|---|
| XPBD Solver node | Core constraint-based solver powering cloth, hair, and particles |
| Cloth Dynamics | Ready-made modifier + node group for any mesh: pinning, stretch/bend stiffness, tearing, damping |
| Hair Dynamics | On Curves geometry; `Add > Curves > Empty Hair` wires the attach surface + a Capture Rest Geometry modifier; each curve sims as a Cosserat rod (real bend/twist) |
| Effectors | Explicit influence: **Collider** (closed mesh + Collider modifier, gathered in a collection assigned to the sim), **Custom Force** (per-point vectors), **Custom Effector** (closure at a solver stage). Nothing is auto-detected |
| Forces | Only gravity is built in. Wind/turbulence/drag = author a Custom Force on an Empty (Empties can host GN modifiers now) |

**Adopted:** authoring-time cloth shaping — draped tarps, sagging nets, flags
mid-wave, rope slack — baked into exported meshes. Silhouette wins at zero
runtime cost.

**Not adopted:** anything runtime-reactive, and any tooling that reaches into
the node-group internals. The system is explicitly experimental; 5.2 has **no
self-collision, no pressure, no stitching, no built-in wind**, stiffness values
above ~0.25 read rubbery, and sparse meshes miss collisions (subdivide ≥1×).
Wrap the working recipe in one place (a skill or one script) so there is a
single spot to fix when 5.3 moves the internals.

**No dedicated bpy API.** Scripts drive the system through the Geometry Nodes
modifier RNA — see §3, the new inputs API.

Classic systems (legacy cloth, rigid body, soft body, Mantaflow, particle hair)
are untouched and coexist with XPBD.

## 2. Bake-to-GLB pathways (the part that matters for pipelines)

glTF has no runtime physics. Simulation is authoring-time only; consumers get
baked geometry or baked keyframes.

| Sim | Export-safe route |
|---|---|
| XPBD Cloth/Hair, Simulation Zone | Bake node (Mode = Still, Target = Packed/Disk) → evaluated mesh exports like any modifier result. For a frozen drape: scrub to the good frame, apply the modifier |
| Classic cloth / soft body | Scrub to frame → `bpy.ops.object.modifier_apply` |
| Rigid body | `bpy.ops.rigidbody.bake_to_keyframes(frame_start, frame_end, step)` → real object keyframes → glTF exports natively. The only rigid-body path that survives export |
| Fluid | Cache bake → mesh sequence; no keyframe path |

## 3. Risks to headless export scripts (ranked)

1. **"Only Insert Needed" keyframe preference defaults ON.** `keyframe_insert()`
   now skips keys matching the current value. Failure mode: a hold pose keyed
   identically on frames 1 and 10 with a change at 20 — the frame-10 key is
   skipped and the hold melts into an interpolation. Mitigation: force
   `bpy.context.preferences.edit.use_keyframe_insert_needed = False` in the
   baking preamble, or pass explicit per-call options. Verify by re-exporting
   one animated asset and diffing clip keycounts.
2. **glTF exporter sorts nodes by name and inlines materials when possible.**
   Output can differ byte-for-byte from 5.1 with identical visuals. Any
   md5/shasum parity or golden-*byte* diffing breaks; golden *thumbnails*
   (visual comparison) remain valid — which is why Hologram's golden convention
   compares renders, not bytes.
3. **Geometry Nodes modifier inputs moved to real RNA.**
   `modifier["Input_2"]`-style custom-prop access is dead; use
   `modifier.properties.inputs.<identifier>.value`, `.type = "ATTRIBUTE"`,
   `.attribute_name`; outputs likewise. All new GN/XPBD scripting must use the
   new API from day one.
4. Low: Compare/Random Value node socket identifiers changed; sculpt
   automasking props moved to `MeshAutomaskingSettings`;
   `UILayout.template_palette` argument removed.
5. **Non-risks:** no changes to bmesh, armature/bone data, action/F-curve APIs,
   or the glTF export operator's parameters (`export_yup`, `export_extras`,
   skins). `gpu.init()` now exists for GPU work under `--background`.

## 4. Animation & rigging notes

- **Auto IK** fixed for disconnected parents; scroll-wheel chain-length control
  while dragging; chains extend further (into the spine). Directly improves
  IK-based pose authoring (`/hologram:pose-authoring`).
- **Pose Library auto-converts rotation modes** (quat ↔ Euler ↔ axis-angle) on
  apply — previously silently corrupted orientation. Assumes Euler XYZ when
  ambiguous — one more reason to standardize on a single Euler order in pose
  registries.
- Bone add redo panel: deform toggle, explicit length, world-axis alignment.
  `Armature > Duplicate and Rename` batch search/replace across a bone chain.
- Graph Editor local view (`/`), mass-delete F-curve modifiers; Dope Sheet
  Select-Keyframes-by-Type; playback loop modes (Stop at End / Bounce) — handy
  for auditing loop seams on cycle clips.

## 5. Geometry Nodes notes (the scriptable surface)

- **Bundles**: arbitrary data — including fields and closures — riding with
  geometry across modifier/object boundaries (the physics system is built on
  these). **Lists**: a real list type + Filter/Sort/Length nodes.
- **Mesh Bevel node** — bevel-modifier parity inside GN: procedural
  edge-rounding applied before export. **Cluster by Distance / by Connected** —
  procedural damaged/broken variant generation.
- Node-tool inputs are now settable from Python (were UI-only).
- Attribute tooling: Rename/Get Names/Transfer/Capture-with-selection; string
  nodes; NURBS order/weight nodes.

## 6. What Hologram does with this

1. The keyframe-preference guard and the RNA-inputs change are called out in
   `/hologram:blender-sim` and `/hologram:pose-authoring` — the two places a
   baking script gets authored.
2. Golden-thumbnail comparison (visual) over byte hashing is the drift check in
   `hologram check`'s golden gate — §3.2 is the reason.
3. The XPBD recipe (collider collection → Cloth Dynamics on a subdivided mesh →
   effectors assigned → stiffness ≤ 0.25 → pin the attach edge → bake/apply →
   export) lives in `/hologram:blender-sim` as the single fix-it-here spot.
