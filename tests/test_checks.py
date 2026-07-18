"""The checks SDK + runner: return-coercion, the built-ins on synthetic assets,
project-file discovery, the aggregate run_project, and the /api/checks route.

Pure engine tests build Asset structs by hand (no GLB on disk); the route test
reuses the committed sample project and the same _Recorder handler shim as
test_dashboard.py.
"""

from __future__ import annotations

import dataclasses
import io
import json
from pathlib import Path

import pytest

import hologram.dashboard.server as server
from hologram.checks import (
    BUILTIN_CHECKS,
    Check,
    Finding,
    all_checks,
    check,
    discover_checks,
    fail,
    load_project_checks,
    ok,
    project_checks_path,
    run_asset,
    run_one,
    run_project,
    warn,
)
from hologram.config import load_config
from hologram.gltf import Asset, Node
from hologram.watch import humanize as watch_humanize

REPO = Path(__file__).resolve().parent.parent
MINIMAL = REPO / "examples" / "minimal"


# ── Synthetic asset builder ──────────────────────────────────────────────
def mk_asset(*, nodes=None, materials=None, mesh_names=None, animations=None,
             skins=None, filename="synthetic.glb") -> Asset:
    nodes = nodes if nodes is not None else [Node(index=0, name="Root", parent=None)]
    return Asset(
        path=f"/tmp/{filename}",
        filename=filename,
        stem=filename.rsplit(".", 1)[0],
        nodes=nodes,
        roots=[i for i, n in enumerate(nodes) if n.parent is None],
        animations=animations or [],
        materials=materials or [],
        skins=skins or [],
        mesh_names=mesh_names or [],
    )


# ── Return coercion (run_one) ─────────────────────────────────────────────
def _run(func, asset=None):
    return run_one(Check.from_func(func), asset or mk_asset())


def test_none_and_true_pass():
    @check("none passes")
    def c_none(a):
        return None

    @check("true passes")
    def c_true(a):
        return True

    for f in (_run(c_none), _run(c_true)):
        assert f.ok and f.severity == "ok" and f.message == ""


def test_false_fails_with_default_severity_and_name():
    @check("no bare false", severity="error")
    def c(a):
        return False

    f = _run(c)
    assert not f.ok and f.severity == "error"
    assert f.message == "no bare false"  # falls back to the check name


def test_string_return_fails_with_that_message():
    @check("stringy")  # default severity is warn
    def c(a):
        return "something is off"

    f = _run(c)
    assert not f.ok and f.severity == "warn" and f.message == "something is off"


def test_explicit_result_helpers_override_severity():
    @check("explicit", severity="error")
    def c_warn(a):
        return warn("soft problem")  # overrides the decorator's error default

    @check("explicit2")
    def c_fail(a):
        return fail("hard problem")  # overrides the decorator's warn default

    fw = _run(c_warn)
    ff = _run(c_fail)
    assert not fw.ok and fw.severity == "warn" and fw.message == "soft problem"
    assert not ff.ok and ff.severity == "error" and ff.message == "hard problem"


def test_ok_helper_passes():
    @check("explicit ok")
    def c(a):
        return ok()

    assert _run(c).ok


def test_raising_check_becomes_error_finding_not_crash():
    @check("boom")
    def c(a):
        raise ValueError("kaboom")

    f = _run(c)
    assert not f.ok and f.severity == "error"
    assert "ValueError" in f.message and "kaboom" in f.message


def test_unknown_truthy_return_treated_as_pass():
    @check("weird")
    def c(a):
        return 42  # not None/True/False/str/Result

    f = _run(c)
    assert f.ok and f.message == ""


def test_finding_carries_asset_filename():
    @check("named")
    def c(a):
        return True

    f = run_one(Check.from_func(c), mk_asset(filename="hero.glb"))
    assert f.asset == "hero.glb"


# ── Built-in checks ───────────────────────────────────────────────────────
def _by_name(findings: list[Finding], name: str) -> Finding:
    return next(f for f in findings if f.check == name)


def test_builtins_pass_on_a_healthy_asset():
    asset = mk_asset(
        nodes=[Node(index=0, name="Body", parent=None)],
        materials=["Steel"],
        mesh_names=["BodyMesh"],
    )
    findings = run_asset(asset, BUILTIN_CHECKS)
    assert all(f.ok for f in findings)


def test_builtin_has_nodes_errors_on_empty():
    f = _by_name(run_asset(mk_asset(nodes=[]), BUILTIN_CHECKS), "asset has nodes")
    assert not f.ok and f.severity == "error"


def test_builtin_meshes_need_materials_warns():
    asset = mk_asset(mesh_names=["Body"], materials=[])
    f = _by_name(run_asset(asset, BUILTIN_CHECKS), "meshes have a material")
    assert not f.ok and f.severity == "warn"


def test_builtin_meshes_without_materials_ok_when_no_mesh():
    asset = mk_asset(mesh_names=[], materials=[])
    f = _by_name(run_asset(asset, BUILTIN_CHECKS), "meshes have a material")
    assert f.ok


def test_builtin_unnamed_nodes_warn():
    asset = mk_asset(nodes=[
        Node(index=0, name="Root", parent=None),
        Node(index=1, name="<unnamed_1>", parent=0),
    ])
    f = _by_name(run_asset(asset, BUILTIN_CHECKS), "nodes are named")
    assert not f.ok and f.severity == "warn" and "1 unnamed" in f.message


# ── Declaring / discovering checks ────────────────────────────────────────
def test_check_decorator_derives_name_from_func():
    @check()
    def meshes_have_materials(a):
        return True

    meta = Check.from_func(meshes_have_materials)
    assert meta.name == "meshes have materials"  # underscores -> spaces


def test_invalid_severity_falls_back_to_warn():
    @check("x", severity="catastrophic")
    def c(a):
        return False

    assert Check.from_func(c).severity == "warn"


def test_discover_checks_finds_only_decorated_in_order():
    ns = {}

    @check("first")
    def a(asset):
        return True

    def helper(asset):  # undecorated — must be ignored
        return True

    @check("second")
    def b(asset):
        return True

    ns.update({"a": a, "helper": helper, "b": b, "CONST": 3})
    found = discover_checks(ns)
    assert [c.name for c in found] == ["first", "second"]


# ── Project check files ───────────────────────────────────────────────────
def _project(tmp_path: Path):
    (tmp_path / "hologram.toml").write_text('[project]\nname = "t"\n', encoding="utf-8")
    return load_config(str(tmp_path))


def test_project_checks_path_location(tmp_path):
    cfg = _project(tmp_path)
    assert project_checks_path(cfg) == cfg.root / ".hologram" / "checks.py"


def test_load_project_checks_absent_is_clean(tmp_path):
    checks, err = load_project_checks(_project(tmp_path))
    assert checks == [] and err is None


def test_load_project_checks_present_discovers(tmp_path):
    cfg = _project(tmp_path)
    p = project_checks_path(cfg)
    p.parent.mkdir(parents=True)
    p.write_text(
        "from hologram.checks import check, warn\n"
        "@check('lowercase stem')\n"
        "def low(a):\n"
        "    if a.stem != a.stem.lower():\n"
        "        return warn('uppercase')\n",
        encoding="utf-8",
    )
    checks, err = load_project_checks(cfg)
    assert err is None
    assert [c.name for c in checks] == ["lowercase stem"]


def test_load_project_checks_broken_returns_error_not_raise(tmp_path):
    cfg = _project(tmp_path)
    p = project_checks_path(cfg)
    p.parent.mkdir(parents=True)
    p.write_text("def broken(:\n", encoding="utf-8")  # syntax error
    checks, err = load_project_checks(cfg)
    assert checks == []
    assert err and "SyntaxError" in err


def test_all_checks_is_builtins_then_user(tmp_path):
    cfg = _project(tmp_path)
    p = project_checks_path(cfg)
    p.parent.mkdir(parents=True)
    p.write_text(
        "from hologram.checks import check\n"
        "@check('mine')\n"
        "def mine(a):\n"
        "    return True\n",
        encoding="utf-8",
    )
    checks, err = all_checks(cfg)
    assert err is None
    names = [c.name for c in checks]
    assert names[: len(BUILTIN_CHECKS)] == [c.name for c in BUILTIN_CHECKS]
    assert names[-1] == "mine"


# ── Aggregate runner ──────────────────────────────────────────────────────
def test_run_project_summary_on_sample_is_clean():
    """The committed sample assets pass every check (built-ins + the example
    project's own lowercase-name check): 3 assets, 0 problems."""
    report = run_project(load_config(str(MINIMAL)))  # emit=False — read-only
    s = report["summary"]
    assert s["assets"] == 3
    # builtins + whatever the example project ships (>= builtins, never fewer)
    assert s["checks"] >= len(BUILTIN_CHECKS)
    assert {c.name for c in BUILTIN_CHECKS} <= set(report["checks"])
    assert s["errors"] == 0 and s["warnings"] == 0
    assert s["clean"] == 3
    assert report["load_error"] is None
    assert all(r["ok"] for r in report["results"])


def test_run_project_emit_appends_one_check_run_event(tmp_path):
    """emit=True logs a single check_run summary; the read of GLBs stays on the
    sample, but the event lands in a throwaway log (never the demo log)."""
    from hologram import events

    base = load_config(str(MINIMAL))
    log = tmp_path / "events.jsonl"
    cfg = dataclasses.replace(base, events_log=log)
    run_project(cfg, emit=True)
    rows = events.tail(log, limit=10)
    runs = [e for e in rows if e.get("type") == "check_run"]
    assert len(runs) == 1
    assert runs[0]["assets"] == 3 and runs[0]["checks"] >= len(BUILTIN_CHECKS)


def test_run_project_surfaces_a_load_error(tmp_path):
    cfg = _project(tmp_path)
    p = project_checks_path(cfg)
    p.parent.mkdir(parents=True)
    p.write_text("import nonexistent_module_xyz\n", encoding="utf-8")
    report = run_project(cfg)
    assert report["load_error"] and "ModuleNotFoundError" in report["load_error"]


# ── /api/checks route (handler shim, mirrors test_dashboard.py) ────────────
class _Recorder:
    def __init__(self):
        self.status: int | None = None
        self.errors: list[tuple[int, str | None]] = []
        self.headers: dict[str, str] = {}
        self.wfile = io.BytesIO()

    def send_response(self, code):
        self.status = code

    def send_error(self, code, message=None):
        self.errors.append((code, message))

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass


@pytest.fixture
def handler(monkeypatch):
    monkeypatch.setattr(server, "CONFIG", load_config(str(MINIMAL)))
    h = server.Handler.__new__(server.Handler)  # skip socket-bound __init__
    rec = _Recorder()
    for name in ("send_response", "send_error", "send_header", "end_headers"):
        setattr(h, name, getattr(rec, name))
    h.wfile = rec.wfile
    return h, rec


def _body(rec) -> dict:
    return json.loads(rec.wfile.getvalue().decode("utf-8"))


def test_checks_route_missing_path_is_400(handler):
    h, rec = handler
    h._checks("")
    assert rec.status == 400


def test_checks_route_rejects_path_traversal(handler):
    h, rec = handler
    h._checks("../../etc/passwd")
    assert rec.status == 404
    assert _body(rec)["path"] == "../../etc/passwd"


def test_checks_route_returns_findings_for_real_asset(handler):
    h, rec = handler
    h._checks("export/gltf/weapons/sword.glb")
    assert rec.status == 200
    payload = _body(rec)
    assert payload["load_error"] is None
    assert "findings" in payload and isinstance(payload["findings"], list)
    # sword is healthy — every built-in finding is ok.
    assert all(f["ok"] for f in payload["findings"])
    names = {f["check"] for f in payload["findings"]}
    assert {c.name for c in BUILTIN_CHECKS} <= names


# ── watch.py humanize: check_run twin ─────────────────────────────────────
def test_watch_humanize_check_run_clean():
    action, target = watch_humanize(
        {"type": "check_run", "assets": 3, "checks": 3, "errors": 0, "warnings": 0}
    )
    assert action == "ran checks"
    assert target == "3 assets, all clean"


def test_watch_humanize_check_run_with_problems():
    action, target = watch_humanize(
        {"type": "check_run", "assets": 5, "errors": 2, "warnings": 1}
    )
    assert action == "ran checks"
    assert "2 errors" in target and "1 warnings" in target and "all clean" not in target
