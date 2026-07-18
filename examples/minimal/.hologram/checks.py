"""Example project checks for the minimal demo.

These run in `hologram check` and the local dashboard (never in the MCP server).
Each function takes an `asset` and returns None/True to pass, or warn()/fail()
to flag a problem. Scaffold your own with `hologram check --init`.
"""

from hologram.checks import check, warn


@check("texture-friendly name")
def lowercase_stem(asset):
    # Lowercase asset names keep texture/material lookups predictable.
    if asset.stem != asset.stem.lower():
        return warn(f"'{asset.stem}' has uppercase — prefer lowercase names")


# Uncomment to require a single rig root per asset (errors, not just warns):
#
# @check("single root node", severity="error")
# def one_root(asset):
#     if len(asset.roots) > 1:
#         return fail(f"{len(asset.roots)} roots — expected a single rig root")
