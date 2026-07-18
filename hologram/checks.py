"""hologram.checks — user-authored, read-only validation over the Asset struct.

This is the first authoring surface (v0.3): a tiny SDK so a project can assert
its own export hygiene ("every mesh has a material", "no unnamed nodes") and see
the verdict in the dashboard, the terminal (`hologram check`), and the event log.

The discipline mirrors the rest of hologram:

  * **Read-only.** A check receives an `Asset` and returns a verdict. The API
    hands it no write surface, and checks run only inside the user's own local
    processes (their `hologram check` run, their dashboard).
  * **The MCP server never loads this module.** The agent surface stays
    import-pure exactly as before — project code is loaded only by the CLI and
    the local dashboard, never by the uvx-launched MCP server.
  * **Opt-in by convention.** Drop a `.hologram/checks.py` in the project to add
    your own checks; with no such file only the built-ins run. Scaffold one with
    `hologram check --init`.

Authoring a check::

    from hologram.checks import check, warn, fail

    @check("meshes have a material")
    def materials_present(asset):
        if asset.mesh_names and not asset.materials:
            return warn("geometry exported with no material")

    @check("single root node", severity="error")
    def one_root(asset):
        if len(asset.roots) > 1:
            return fail(f"{len(asset.roots)} roots — expected a single rig root")
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import golden as golden_mod
from . import manifest as manifest_mod
from . import png as png_mod
from .config import Config, resolve_within
from .gltf import Asset, load_asset

__all__ = [
    "check", "ok", "warn", "fail",
    "Result", "Finding", "Check", "Asset",
    "run_asset", "run_project",
    "TRI_BUDGET", "THUMB_DRIFT", "golden_findings", "golden_check_names",
]

# Severity vocabulary, shared with the dashboard's .finding.err/.warn styles.
ERROR = "error"
WARN = "warn"
OK = "ok"

CheckFn = Callable[[Asset], Any]


# ── What a check returns ─────────────────────────────────────────────────────
@dataclass
class Result:
    """An explicit verdict. Prefer the ok()/warn()/fail() helpers over building
    one by hand."""
    ok: bool
    severity: str = WARN
    message: str = ""


def ok() -> Result:
    """Pass. (Returning None or True from a check means the same thing.)"""
    return Result(True)


def warn(message: str) -> Result:
    """Fail at warning severity with a message."""
    return Result(False, WARN, message)


def fail(message: str) -> Result:
    """Fail at error severity with a message."""
    return Result(False, ERROR, message)


# ── Declaring a check ────────────────────────────────────────────────────────
def check(name: str | None = None, *, severity: str = WARN) -> Callable[[CheckFn], CheckFn]:
    """Mark a function as a hologram check.

    The function takes an `Asset` and returns one of:
        None / True                 → pass
        False                       → fail (using this decorator's severity + name)
        a str                       → fail with that message (decorator's severity)
        ok() / warn(..) / fail(..)  → an explicit verdict

    `severity` ("warn" or "error") is the default used when the function returns
    False or a bare string; ok()/warn()/fail() override it per call.
    """
    def deco(func: CheckFn) -> CheckFn:
        func.__hologram_check__ = {
            "name": (name or func.__name__.replace("_", " ")).strip(),
            "severity": severity if severity in (ERROR, WARN) else WARN,
        }
        return func
    return deco


@dataclass
class Check:
    """A registered check: its display name, default severity, and predicate."""
    name: str
    severity: str
    func: CheckFn

    @classmethod
    def from_func(cls, func: CheckFn) -> Check:
        meta = func.__hologram_check__
        return cls(name=meta["name"], severity=meta["severity"], func=func)


@dataclass
class Finding:
    """The outcome of running one check against one asset."""
    asset: str       # asset filename
    check: str       # check display name
    ok: bool
    severity: str    # "ok" | "warn" | "error"
    message: str

    def to_dict(self) -> dict:
        return {
            "asset": self.asset, "check": self.check, "ok": self.ok,
            "severity": self.severity, "message": self.message,
        }


# ── Running checks ───────────────────────────────────────────────────────────
def run_one(chk: Check, asset: Asset) -> Finding:
    """Run a single check, coercing whatever it returns into a Finding. A check
    that raises becomes an error Finding rather than sinking the whole run."""
    try:
        res = chk.func(asset)
    except Exception as e:  # a buggy check must not crash the runner
        return Finding(asset.filename, chk.name, False, ERROR,
                       f"check raised {type(e).__name__}: {e}")

    if res is None or res is True:
        return Finding(asset.filename, chk.name, True, OK, "")
    if res is False:
        return Finding(asset.filename, chk.name, False, chk.severity, chk.name)
    if isinstance(res, str):
        return Finding(asset.filename, chk.name, False, chk.severity, res)
    if isinstance(res, Result):
        if res.ok:
            return Finding(asset.filename, chk.name, True, OK, "")
        sev = res.severity if res.severity in (ERROR, WARN) else chk.severity
        return Finding(asset.filename, chk.name, False, sev, res.message or chk.name)
    # Unknown truthy return — treat as a pass; don't invent a message.
    return Finding(asset.filename, chk.name, True, OK, "")


def run_asset(asset: Asset, checks: list[Check]) -> list[Finding]:
    """Run every check against one asset, in order."""
    return [run_one(c, asset) for c in checks]


# ── Built-in checks ──────────────────────────────────────────────────────────
# Conservative, pipeline-agnostic hygiene. Users extend (or effectively override
# by writing stricter versions) via .hologram/checks.py.
@check("asset has nodes", severity=ERROR)
def _has_nodes(a: Asset):
    if not a.nodes:
        return fail("no nodes — empty or unreadable export")


@check("meshes have a material")
def _meshes_have_materials(a: Asset):
    if a.mesh_names and not a.materials:
        n = len(a.mesh_names)
        return warn(f"{n} mesh{'es' if n != 1 else ''} but no materials")


@check("nodes are named")
def _nodes_named(a: Asset):
    unnamed = [n for n in a.nodes if n.name.startswith("<unnamed_")]
    if unnamed:
        n = len(unnamed)
        return warn(f"{n} unnamed node{'s' if n != 1 else ''}")


BUILTIN_CHECKS: list[Check] = [
    Check.from_func(_has_nodes),
    Check.from_func(_meshes_have_materials),
    Check.from_func(_nodes_named),
]


# ── Golden-truth checks (opt-in via golden.json) ─────────────────────────────
# These aren't `@check`-decorated: unlike a plain check they need per-asset
# context beyond the Asset struct (its manifest tri count and thumbnail, the
# golden budgets), so they produce `Finding`s directly. They run only when the
# project ships a golden.json and otherwise contribute nothing — an absent
# golden file is zero behaviour change. Findings follow the exact same shape and
# reporting path as every other check.
TRI_BUDGET = "tri budget"
THUMB_DRIFT = "thumbnail drift"


def golden_check_names(golden: golden_mod.GoldenTruths) -> list[str]:
    """The golden checks that are *active* for this golden file — a budget check
    when budgets are declared, a drift check when thumbnails are configured."""
    names: list[str] = []
    if golden.tri_budgets is not None:
        names.append(TRI_BUDGET)
    if golden.thumbnails is not None:
        names.append(THUMB_DRIFT)
    return names


def golden_findings(
    cfg: Config,
    asset: Asset,
    path: Path,
    golden: golden_mod.GoldenTruths,
    rec: manifest_mod.AssetRecord | None,
) -> list[Finding]:
    """Run the golden checks over one asset, in the same order as
    :func:`golden_check_names`. A check that has nothing to assert for this asset
    (no budget for its category, no golden thumbnail on file) is skipped silently
    — it contributes no Finding at all."""
    findings: list[Finding] = []
    if golden.tri_budgets is not None:
        f = _tri_budget_finding(cfg, asset, path, golden, rec)
        if f is not None:
            findings.append(f)
    if golden.thumbnails is not None:
        f = _thumbnail_drift_finding(cfg, asset, golden, rec)
        if f is not None:
            findings.append(f)
    return findings


def _golden_category(cfg: Config, path: Path, rec: manifest_mod.AssetRecord | None) -> str | None:
    """The category used for a budget lookup: the manifest record's category
    when present, else the first path segment of the asset under export_root."""
    if rec is not None and rec.category:
        return rec.category
    try:
        parts = path.resolve().relative_to(cfg.export_root.resolve()).parts
    except ValueError:
        return None
    return parts[0] if len(parts) > 1 else None


def _tri_budget_finding(
    cfg: Config,
    asset: Asset,
    path: Path,
    golden: golden_mod.GoldenTruths,
    rec: manifest_mod.AssetRecord | None,
) -> Finding | None:
    """Assert an asset's triangle count against its category budget. Skips when
    no budget applies (no category budget and no default) or when the tri count
    is unknown (there is no manifest record to read it from)."""
    category = _golden_category(cfg, path, rec)
    budget = golden.budget_for(category)
    if budget is None:
        return None  # no budget for this category and no default → skip silently
    tris = rec.tris if rec is not None else None
    if tris is None:
        return None  # no tri count to check against → skip silently
    label = category or "default"
    if tris > budget:
        return Finding(asset.filename, TRI_BUDGET, False, ERROR,
                       f"{tris} tris over budget {budget} ({label})")
    return Finding(asset.filename, TRI_BUDGET, True, OK, "")


def _thumbnail_drift_finding(
    cfg: Config,
    asset: Asset,
    golden: golden_mod.GoldenTruths,
    rec: manifest_mod.AssetRecord | None,
) -> Finding | None:
    """Compare an asset's current thumbnail against its golden reference. Skips
    when no golden thumbnail is on file for the asset. A missing current
    thumbnail or an unreadable/unsupported PNG is a *warning*, not an error; only
    a real drift beyond ``maxDiff`` is an error."""
    thumbs = golden.thumbnails
    assert thumbs is not None  # caller gates on golden.thumbnails
    asset_id = rec.id if rec is not None else asset.stem
    golden_png = _golden_thumb_path(cfg, thumbs.dir, asset_id)
    if golden_png is None or not golden_png.is_file():
        return None  # no golden reference for this asset → skip silently
    current = _current_thumb(cfg, rec)
    if current is None:
        return Finding(asset.filename, THUMB_DRIFT, False, WARN,
                       "golden thumbnail present but asset has no current thumbnail")
    try:
        reference = png_mod.read_png(golden_png)
        live = png_mod.read_png(current)
        diff = png_mod.mean_abs_diff(reference, live)
    except (png_mod.PNGError, OSError) as e:
        return Finding(asset.filename, THUMB_DRIFT, False, WARN,
                       f"could not compare thumbnails: {e}")
    if diff > thumbs.max_diff:
        return Finding(asset.filename, THUMB_DRIFT, False, ERROR,
                       f"thumbnail drift {diff:.3f} > {thumbs.max_diff:g}")
    return Finding(asset.filename, THUMB_DRIFT, True, OK, "")


def _golden_thumb_path(cfg: Config, thumbs_dir: str, asset_id: str) -> Path | None:
    """Locate ``<thumbs_dir>/<asset-id>.png`` under the project root. Returns
    ``None`` for an unsafe asset id (the id names a single PNG file)."""
    if not manifest_mod.is_safe_id(asset_id):
        return None
    return (cfg.root / thumbs_dir / f"{asset_id}.png").resolve()


def _current_thumb(cfg: Config, rec: manifest_mod.AssetRecord | None) -> Path | None:
    """Resolve an asset's live thumbnail exactly as the dashboard's /api/thumb
    route does: the manifest record's ``thumbnail`` path, resolved within
    export_root. ``None`` when there is no record, no thumbnail, or it is missing."""
    if rec is None or not rec.thumbnail:
        return None
    resolved = resolve_within(cfg.export_root, rec.thumbnail)
    if resolved is None or not resolved.is_file():
        return None
    return resolved


# ── Loading user checks ──────────────────────────────────────────────────────
def discover_checks(namespace: dict) -> list[Check]:
    """Collect decorated checks from a module namespace, in definition order."""
    return [
        Check.from_func(v)
        for v in namespace.values()
        if callable(v) and hasattr(v, "__hologram_check__")
    ]


def project_checks_path(cfg: Config) -> Path:
    """Conventional location of a project's checks: `<root>/.hologram/checks.py`."""
    return cfg.root / ".hologram" / "checks.py"


def load_project_checks(cfg: Config) -> tuple[list[Check], str | None]:
    """Import the project's `.hologram/checks.py` and return its checks.

    Returns ``([], None)`` when the file is absent and ``([], "<error>")`` when
    it fails to import. Never raises. Only ever called from the user's own CLI
    or dashboard process — the MCP server does not touch this path.
    """
    path = project_checks_path(cfg)
    if not path.is_file():
        return [], None
    try:
        spec = importlib.util.spec_from_file_location("hologram_project_checks", path)
        if spec is None or spec.loader is None:  # pragma: no cover - import guard
            return [], f"could not load {path}"
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as e:  # surface, don't crash the dashboard/CLI
        return [], f"{type(e).__name__}: {e}"
    return discover_checks(vars(module)), None


def all_checks(cfg: Config) -> tuple[list[Check], str | None]:
    """Built-ins followed by the project's own checks (plus any load error)."""
    user, err = load_project_checks(cfg)
    return [*BUILTIN_CHECKS, *user], err


# ── Aggregate runner (CLI + dashboard) ───────────────────────────────────────
def run_project(cfg: Config, *, emit: bool = False) -> dict:
    """Run every check against every discovered asset.

    Pure aside from the optional ``check_run`` event: the CLI passes
    ``emit=True`` (an intentional, user-initiated run), while the dashboard does
    not, so passive polling never spams the log.

    When ``emit=True`` this is also the *checkpoint* for regression diffing: each
    asset's fingerprint is snapshotted (refreshing the diff baseline) and an
    ``asset_diff`` event is emitted when it changed since the last check. With
    ``emit=False`` the run stays fully pure — no snapshot writes, no events.
    """
    from . import diff as diff_mod
    from . import events

    checks, load_error = all_checks(cfg)
    # Golden truths are opt-in: when a golden.json is present its built-in checks
    # (tri budgets, thumbnail drift) run alongside the user checks; absent, they
    # contribute nothing. The manifest supplies per-asset tri counts/thumbnails.
    golden = golden_mod.load_golden(cfg)
    golden_names = golden_check_names(golden) if golden is not None else []
    mani = manifest_mod.load_manifest(cfg) if golden is not None else None
    results: list[dict] = []
    n_assets = n_error = n_warn = 0

    for category, paths in cfg.list_glbs().items():
        for p in paths:
            n_assets += 1
            try:
                asset = load_asset(str(p))
            except Exception as e:
                results.append({
                    "asset": p.name, "path": cfg.rel(p), "category": category,
                    "ok": False,
                    "findings": [Finding(p.name, "load", False, ERROR,
                                         f"{type(e).__name__}: {e}").to_dict()],
                })
                n_error += 1
                continue
            findings = run_asset(asset, checks)
            if golden is not None:
                rec = mani.by_stem(asset.stem) if mani is not None else None
                findings = [*findings, *golden_findings(cfg, asset, p, golden, rec)]
            problems = [f for f in findings if not f.ok]
            n_error += sum(1 for f in problems if f.severity == ERROR)
            n_warn += sum(1 for f in problems if f.severity == WARN)
            results.append({
                "asset": asset.filename, "path": cfg.rel(p), "category": category,
                "ok": not problems,
                "findings": [f.to_dict() for f in problems],
            })

            # Checkpoint: refresh the diff baseline and report what changed.
            # Gated on emit so passive callers stay read-only (no snapshot writes).
            if emit:
                summary_now = diff_mod.summarize(asset)
                prev = diff_mod.load_snapshot(cfg, p)
                changes = diff_mod.diff(prev, summary_now)
                diff_mod.save_snapshot(cfg, p, summary_now)
                if changes and not changes.get("first_seen"):
                    events.append(
                        cfg.events_log, "asset_diff",
                        path=cfg.rel(p),
                        lost=changes.get("lost", {}),
                        gained=changes.get("gained", {}),
                        changed=changes.get("changed", {}),
                    )

    n_checks = len(checks) + len(golden_names)
    summary = {
        "assets": n_assets,
        "checks": n_checks,
        "errors": n_error,
        "warnings": n_warn,
        "clean": sum(1 for r in results if r["ok"]),
    }
    if emit:
        events.append(
            cfg.events_log, "check_run",
            assets=n_assets, checks=n_checks,
            errors=n_error, warnings=n_warn,
        )
    return {
        "checks": [c.name for c in checks] + golden_names,
        "results": results,
        "summary": summary,
        "load_error": load_error,
    }
