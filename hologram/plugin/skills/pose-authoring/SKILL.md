---
name: Author a pose via IK and bake
description: Author a character pose in Blender by snapping IK targets to an item's socket empties, baking the solve to FK keyframes, and persisting the resolved Euler angles into the project's pose registry. Use when someone wants a character to hold, grip, carry, or aim an item, needs a new pose or stance clip, or asks why runtime IK is the wrong place to solve a pose.
kind: workflow
---

# Author a pose via IK + bake

The contract this workflow enforces: **authoring solves IK, runtime never does.**
A pose is solved once in Blender, baked to plain FK rotations, and stored as
data the runtime replays. Runtime IK is a per-frame solver fighting your
animation blend; baked FK is a lookup.

Works over a live Blender session when a Blender MCP is connected (probe it
first — e.g. a `get_scene_info`-style tool); otherwise write the same steps as
a headless `bpy` script the user runs with `blender --background --python`.

## Preconditions

- A rigged character GLB and an item GLB whose grip points are marked with
  `Socket_*` empties (e.g. `Socket_Grip` for the primary hand, `Socket_Foregrip`
  for the support hand). No sockets → author them first (`/hologram:glb-standards`).
- A pose registry in the project — wherever this project persists authored poses
  (a Python dict module, JSON file, or similar that its export scripts read).
  If none exists, propose one: a `POSES = {name: {bone: {"rotation_euler": (x, y, z)}}}`
  module the export script iterates.
- The pose name must be new; overwriting an existing pose needs explicit user
  approval.

## Workflow

1. **Snapshot the scene.** Record existing object/action/collection names so
   cleanup can remove exactly what this run adds. The live scene is a sandbox —
   the only persistent outputs are the registry entry and the re-export.
2. **Import into a fresh collection** (fail if a previous run's collection is
   still there). Import the character, then the item; parent the item to the
   hand bone (`parent_type = "BONE"`, `matrix_parent_inverse` from the inverted
   world @ pose-bone matrix) so it rides the rig exactly as it will at runtime.
3. **Place IK targets at the sockets.** One empty per involved hand, positioned
   with the socket's `matrix_world`. Two-handed items: primary hand →
   `Socket_Grip`, support hand → `Socket_Foregrip`. One-handed: primary only.
4. **Constrain the hand bones.** An IK constraint per involved hand, targeting
   its empty, `chain_count = 3` (hand → forearm → upper arm). Optional pre-pose
   before solving (e.g. dip the neck for an aim stance).
5. **Screenshot and get approval.** Show the solve; accept nudges to the IK
   targets ("move the left target up 2cm") and re-screenshot until the user
   approves or cancels. Cancel → cleanup, write nothing.
6. **Bake.** First force full keying — Blender 5.2's "Only Insert Needed"
   preference silently skips redundant keys and melts hold poses:
   `bpy.context.preferences.edit.use_keyframe_insert_needed = False`. Then
   `bpy.ops.nla.bake(frame_start=0, frame_end=2, only_selected=True,
   visual_keying=True, clear_constraints=True, bake_types={"POSE"})`.
7. **Read the resolved rotations.** For every bone the bake keyed, take
   `pose_bone.matrix_basis.to_euler("XYZ")` in degrees, rounded to 2 decimals
   (sub-centidegree differences are imperceptible and the diff stays readable).
   Pick ONE Euler order and use it everywhere — mixing orders corrupts poses.
8. **Persist to the registry** in the project's existing format, with a comment
   header (what, why, authoring date). Then **clean up**: delete the run's
   collection, its objects, and the temporary baked action.
9. **Re-export** the character through the project's export path so the pose
   ships in the GLB, then tell the user how to verify (equip the item; the pose
   should match the approved screenshot).

## Rules that keep poses stable

- **Never author legs or hips** in an upper-body pose. Bones missing from the
  registry entry fall through to the locomotion clip playing under the mask;
  keying them makes the pose fight walking.
- **Pose name == clip name.** The registry key becomes the action name, the clip
  name in the GLB, and the runtime lookup key. Rename in all places or none.
- **Sockets moved → poses stale.** A pose is baked against socket positions;
  retuning a socket invalidates every pose authored against it. Re-run this
  workflow for each affected pose.
- **Why store Eulers, not IK targets:** replaying FK needs no solver, no
  convergence, no surprises — that is the entire point of authoring-time IK.
