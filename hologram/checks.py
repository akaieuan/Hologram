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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import Config
from .gltf import Asset, load_asset

__all__ = [
    "check", "ok", "warn", "fail",
    "Result", "Finding", "Check", "Asset",
    "run_asset", "run_project",
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
    def from_func(cls, func: CheckFn) -> "Check":
        meta = getattr(func, "__hologram_check__")
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
    """
    from . import events

    checks, load_error = all_checks(cfg)
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
            problems = [f for f in findings if not f.ok]
            n_error += sum(1 for f in problems if f.severity == ERROR)
            n_warn += sum(1 for f in problems if f.severity == WARN)
            results.append({
                "asset": asset.filename, "path": cfg.rel(p), "category": category,
                "ok": not problems,
                "findings": [f.to_dict() for f in problems],
            })

    summary = {
        "assets": n_assets,
        "checks": len(checks),
        "errors": n_error,
        "warnings": n_warn,
        "clean": sum(1 for r in results if r["ok"]),
    }
    if emit:
        events.append(
            cfg.events_log, "check_run",
            assets=n_assets, checks=len(checks),
            errors=n_error, warnings=n_warn,
        )
    return {
        "checks": [c.name for c in checks],
        "results": results,
        "summary": summary,
        "load_error": load_error,
    }
