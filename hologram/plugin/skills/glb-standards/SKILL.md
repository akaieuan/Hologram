---
name: GLB authoring standards
description: Author and enforce a project's GLB conventions — naming, hierarchy, socket empties and their placement, custom properties, and tri budgets — and turn them into hologram checks so they're validated on every export. Use when someone asks what makes a well-formed GLB, how to add attachment sockets, why an item won't sit right in a character's hands, or wants their asset conventions enforced automatically.
kind: reference
---

# GLB authoring standards

A GLB is a contract between the DCC that exported it and every consumer
downstream — engines, viewers, other assets. The contract only holds if it's
written down and checked on every export. This skill is the catalog of
conventions worth adopting, and how to enforce them with `hologram check`.

## The conventions

**One root.** A single root node (usually an Empty carrying the asset's custom
properties). Multiple roots make bone attachment, instancing, and transforms
ambiguous.

**Prefix naming.** Pick prefixes that encode what a node is and stick to them —
e.g. `SK_` skinned/skeletal, `SM_` static mesh, `col_` collision-only geometry,
`slot_*` gameplay attachment points. Consumers can then select by name instead
of guessing by shape.

**Socket empties.** Attachment points are `Socket_*` Empties parented into the
asset (grip points, mounts, muzzle, mag well). The rules that make them work:

- **Store socket positions in local space** relative to the asset root — never
  world. Convert with `asset.matrix_world.inverted() @ socket.matrix_world`
  before persisting; don't rely on the asset sitting at the origin.
- **Reach-check grips.** A support-hand socket the arm can't reach guarantees a
  bad pose: sum the arm chain's bone lengths (shoulder → forearm → hand) and
  keep socket-to-shoulder distance under ~95% of it. For two-handed items keep
  the primary-to-support grip span inside a sane band (≈ 0.20–0.50 m at human
  scale) — under it the hands collide, over it the arms twist.
- **Never move the attachment root** (the socket the engine mounts the whole
  asset by). Moving it shifts the entire asset relative to the hand; if the
  asset clips, fix the hand-side socket instead.
- **Sockets moved → poses stale.** Any pose authored against old socket
  positions must be re-baked (`/hologram:pose-authoring`).

**Custom properties.** Consumers shouldn't infer type from names alone — stamp
the root with explicit extras (asset class, version, category) and export with
`export_extras=True`.

**Tri budgets per category.** Budgets belong in `golden.json` (human-edited
only), not in tribal memory — `hologram check` enforces them automatically when
present (see `codex/golden.json` conventions in the Hologram README).

**Thumbnails track versions.** A thumbnail rendered from version N is stale the
moment version N+1 exports; stale thumbnails hide drift from reviewers.

## Enforcing with hologram checks

Conventions become real in `.hologram/checks.py` (`hologram check --init`
scaffolds it). Examples worth adapting:

```python
from hologram.checks import check, warn, fail

@check("single root node", severity="error")
def one_root(asset):
    if len(asset.roots) > 1:
        return fail(f"{len(asset.roots)} roots — expected one")

@check("collision pairs", severity="warn")
def col_pairs(asset):
    doors = [n for n in asset.mesh_names if n.startswith("mesh_door_")]
    for d in doors:
        if d.replace("mesh_", "col_") not in asset.mesh_names:
            return warn(f"{d} has no matching col_ mesh")

@check("grip span", severity="warn")
def grip_span(asset):
    names = asset.top_level_node_names()
    if "Socket_Grip" in names and "Socket_Foregrip" not in names:
        return warn("two-handed item with no Socket_Foregrip?")
```

Run `hologram check` after every export batch; wire it into CI if exports are
committed. Checks are read-only — they report, they never fix.

## Reviewing a report

Group by severity, lead with errors, and for each finding name the asset, the
check, and the concrete fix. Offer fixes as follow-ups — the fix belongs in the
generating script or the source .blend, never in the exported GLB.
