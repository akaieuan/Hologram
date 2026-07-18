"""Golden-truths plumbing: the golden.json loader, the stdlib PNG reader, the
tri-budget / thumbnail-drift checks, and the /api/golden + /api/skills routes.

Everything is built in ``tmp_path`` from real GLBs copied out of the committed
``manifest-project`` fixture and PNGs synthesised in-process (a tiny forward-
filtering encoder mirrors the reader), so no new binary fixtures are committed.
The live-server helper replicates test_dashboard.py's pattern locally rather than
importing it — that file is left untouched.
"""

from __future__ import annotations

import http.client
import json
import shutil
import struct
import threading
import zlib
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import hologram.checks as C
import hologram.dashboard.server as server
import hologram.golden as G
import hologram.png as P
from hologram import manifest as M
from hologram.config import load_config
from hologram.gltf import load_asset

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests" / "fixtures" / "manifest-project"
HERO_GLB = FIXTURE / "exports" / "characters" / "hero.glb"
COIN_GLB = FIXTURE / "exports" / "items" / "coin.glb"
MINIMAL = REPO / "examples" / "minimal"

TOML = (
    '[project]\nname = "golden-fixture"\n'
    '[paths]\nexport_root = "exports"\nevents_log = ".hologram/events.jsonl"\n'
    '[categories.characters]\nglb_pattern = "characters/**/*.glb"\n'
    '[categories.items]\nglb_pattern = "items/**/*.glb"\n'
)


# ── In-process PNG encoder (forward filters mirror png.py's reverse ones) ──────

def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def _png(width: int, height: int, bit_depth: int, color_type: int,
         interlace: int, idat: bytes) -> bytes:
    """Assemble PNG bytes with an arbitrary IHDR — used to craft both valid and
    deliberately-unsupported images."""
    ihdr = struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, interlace)
    return P.PNG_SIGNATURE + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


def make_png(rows: list[list[tuple[int, ...]]], filters: list[int] | None = None) -> bytes:
    """Encode an 8-bit RGB (3-tuple) or RGBA (4-tuple) image. ``filters`` picks a
    per-scanline filter type (0-4) so a decode round-trip can exercise them all."""
    h = len(rows)
    w = len(rows[0])
    ch = len(rows[0][0])
    color_type = 2 if ch == 3 else 6
    if filters is None:
        filters = [0] * h
    raw = bytearray()
    prev = [0] * (w * ch)
    for y, row in enumerate(rows):
        cur: list[int] = []
        for px in row:
            cur.extend(px)
        ft = filters[y]
        raw.append(ft)
        line = bytearray(w * ch)
        for i in range(w * ch):
            x = cur[i]
            a = cur[i - ch] if i >= ch else 0
            b = prev[i]
            c = prev[i - ch] if i >= ch else 0
            if ft == 0:
                v = x
            elif ft == 1:
                v = x - a
            elif ft == 2:
                v = x - b
            elif ft == 3:
                v = x - ((a + b) >> 1)
            else:
                v = x - _paeth(a, b, c)
            line[i] = v & 0xFF
        raw.extend(line)
        prev = cur
    return _png(w, h, 8, color_type, 0, zlib.compress(bytes(raw)))


def _solid(w: int, h: int, color: tuple[int, ...]) -> list[list[tuple[int, ...]]]:
    return [[color] * w for _ in range(h)]


# ── png.py: decode + mean_abs_diff ────────────────────────────────────────────

def test_decode_png_round_trip_all_filters():
    # Five scanlines so filter types 0..4 each appear; content varies in both
    # axes so Sub/Up/Average/Paeth actually predict something.
    rows = [[((x * 17) % 256, (x + y * 3) % 256, (y * 29) % 256) for x in range(6)]
            for y in range(5)]
    img = P.decode_png(make_png(rows, filters=[0, 1, 2, 3, 4]))
    assert (img.width, img.height, img.channels) == (6, 5, 3)
    expected = bytes(v for row in rows for px in row for v in px)
    assert img.pixels == expected


def test_decode_png_rgba_channels():
    img = P.decode_png(make_png([[(10, 20, 30, 128), (40, 50, 60, 255)]]))
    assert img.channels == 4
    assert img.width == 2 and img.height == 1


def test_mean_abs_diff_identical_is_zero():
    a = P.decode_png(make_png(_solid(3, 3, (12, 34, 56))))
    b = P.decode_png(make_png(_solid(3, 3, (12, 34, 56))))
    assert P.mean_abs_diff(a, b) == 0.0


def test_mean_abs_diff_black_vs_white_is_one():
    a = P.decode_png(make_png(_solid(2, 2, (0, 0, 0))))
    b = P.decode_png(make_png(_solid(2, 2, (255, 255, 255))))
    assert P.mean_abs_diff(a, b) == 1.0


def test_mean_abs_diff_ignores_alpha():
    a = P.decode_png(make_png([[(10, 20, 30, 0)]]))
    b = P.decode_png(make_png([[(10, 20, 30, 255)]]))
    assert a.channels == 4 and b.channels == 4
    assert P.mean_abs_diff(a, b) == 0.0


def test_mean_abs_diff_dimension_mismatch_raises():
    a = P.decode_png(make_png(_solid(1, 1, (0, 0, 0))))
    b = P.decode_png(make_png(_solid(2, 1, (0, 0, 0))))
    with pytest.raises(P.DimensionMismatch):
        P.mean_abs_diff(a, b)


@pytest.mark.parametrize("data", [
    _png(1, 1, 8, 3, 0, zlib.compress(b"\x00\x00")),          # palette
    _png(1, 1, 8, 2, 1, zlib.compress(b"\x00" * 4)),          # interlaced
    _png(1, 1, 16, 2, 0, zlib.compress(b"\x00" * 7)),         # 16-bit
    _png(1, 1, 8, 0, 0, zlib.compress(b"\x00" * 2)),          # grayscale
    b"definitely not a png",                                   # bad signature
])
def test_decode_rejects_unsupported(data):
    with pytest.raises(P.UnsupportedPNG):
        P.decode_png(data)


# ── golden.py: loader search order + parsing ───────────────────────────────────

def _base_project(tmp_path: Path, name: str = "proj") -> Path:
    root = tmp_path / name
    (root / "exports").mkdir(parents=True)
    (root / "hologram.toml").write_text(TOML, encoding="utf-8")
    return root


def test_load_golden_absent_is_none():
    assert G.load_golden(load_config(str(MINIMAL))) is None


def test_load_golden_search_order(tmp_path):
    root = _base_project(tmp_path)
    exp = root / "exports"
    (root / "golden.json").write_text(json.dumps({"triBudgets": {"default": 1}}))
    (root / "codex").mkdir()
    (root / "codex" / "golden.json").write_text(json.dumps({"triBudgets": {"default": 2}}))
    (exp / "golden.json").write_text(json.dumps({"triBudgets": {"default": 3}}))
    cfg = load_config(str(root))

    g = G.load_golden(cfg)
    assert g is not None and g.budget_for("x") == 1 and g.path == root / "golden.json"

    (root / "golden.json").unlink()
    codex_hit = G.load_golden(cfg)
    assert codex_hit is not None and codex_hit.budget_for("x") == 2  # codex/ is next

    (root / "codex" / "golden.json").unlink()
    export_hit = G.load_golden(cfg)
    assert export_hit is not None and export_hit.budget_for("x") == 3  # export_root is last

    (exp / "golden.json").unlink()
    assert G.load_golden(cfg) is None


def test_load_golden_malformed_is_none(tmp_path):
    root = _base_project(tmp_path)
    cfg = load_config(str(root))
    (root / "golden.json").write_text("{ not json", encoding="utf-8")
    assert G.load_golden(cfg) is None
    (root / "golden.json").write_text("[1, 2, 3]", encoding="utf-8")  # not an object
    assert G.load_golden(cfg) is None


def test_budget_for_fallback_and_unknown_keys(tmp_path):
    root = _base_project(tmp_path)
    (root / "golden.json").write_text(json.dumps({
        "_note": "docs, ignored by typed accessors",
        "triBudgets": {"_comment": "skipped", "characters": 8000, "default": 3000},
        "thumbnails": {"dir": "codex/golden/thumbs", "maxDiff": 0.08},
        "simEnvelope": {"contactMsMax": 120},
    }), encoding="utf-8")
    g = G.load_golden(load_config(str(root)))
    assert g is not None
    assert g.tri_budgets == {"characters": 8000, "default": 3000}  # underscore key dropped
    assert g.budget_for("characters") == 8000
    assert g.budget_for("items") == 3000  # no items budget → default
    assert g.budget_for(None) == 3000
    assert g.thumbnails is not None
    assert g.thumbnails.dir == "codex/golden/thumbs"
    assert g.thumbnails.max_diff == 0.08
    # forward-compat: unknown + underscore keys survive verbatim in raw
    assert g.raw["simEnvelope"] == {"contactMsMax": 120}
    assert g.raw["_note"].startswith("docs")


def test_budget_for_none_when_no_budgets(tmp_path):
    root = _base_project(tmp_path, "a")
    (root / "golden.json").write_text(json.dumps({"thumbnails": {"dir": "t", "maxDiff": 0.1}}))
    g = G.load_golden(load_config(str(root)))
    assert g is not None and g.tri_budgets is None
    assert g.budget_for("anything") is None

    root2 = _base_project(tmp_path, "b")
    (root2 / "golden.json").write_text(json.dumps({"triBudgets": {"characters": 100}}))
    g2 = G.load_golden(load_config(str(root2)))
    assert g2 is not None
    assert g2.budget_for("characters") == 100
    assert g2.budget_for("items") is None  # no match, no default


# ── golden checks: tri budget ──────────────────────────────────────────────────

def _asset_project(tmp_path, name, *, golden, manifest, thumbs=None):
    """A tmp project with the two fixture GLBs, a manifest, an optional golden
    file, and optional PNGs written at arbitrary paths under the root."""
    root = tmp_path / name
    exp = root / "exports"
    (exp / "characters").mkdir(parents=True)
    (exp / "items").mkdir(parents=True)
    shutil.copy(HERO_GLB, exp / "characters" / "hero.glb")
    shutil.copy(COIN_GLB, exp / "items" / "coin.glb")
    (root / "hologram.toml").write_text(TOML, encoding="utf-8")
    (exp / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if golden is not None:
        (root / "golden.json").write_text(json.dumps(golden), encoding="utf-8")
    for rel, data in (thumbs or {}).items():
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    return root


def _findings_for(root, glb_rel, asset_id):
    cfg = load_config(str(root))
    g = G.load_golden(cfg)
    assert g is not None
    mani = M.load_manifest(cfg)
    assert mani is not None
    path = root / "exports" / glb_rel
    asset = load_asset(str(path))
    return [f for f in C.golden_findings(cfg, asset, path, g, mani.by_stem(asset_id))]


def _only(findings, check_name):
    return [f for f in findings if f.check == check_name]


def test_tri_budget_pass(tmp_path):
    root = _asset_project(tmp_path, "tri-pass",
        golden={"triBudgets": {"characters": 8000, "default": 1000}},
        manifest={"assets": {"hero": {"id": "hero", "category": "characters",
                                      "glb": "characters/hero.glb", "tris": 3000, "version": 1}}})
    tri = _only(_findings_for(root, "characters/hero.glb", "hero"), C.TRI_BUDGET)
    assert len(tri) == 1 and tri[0].ok and tri[0].severity == "ok"


def test_tri_budget_over_is_error(tmp_path):
    root = _asset_project(tmp_path, "tri-over",
        golden={"triBudgets": {"characters": 8000, "default": 1000}},
        manifest={"assets": {"coin": {"id": "coin", "category": "items",
                                      "glb": "items/coin.glb", "tris": 1200, "version": 1}}})
    tri = _only(_findings_for(root, "items/coin.glb", "coin"), C.TRI_BUDGET)
    assert len(tri) == 1 and not tri[0].ok and tri[0].severity == "error"
    # message names actual vs budget (1200 tris over the default budget of 1000)
    assert "1200" in tri[0].message and "1000" in tri[0].message


def test_tri_budget_no_budget_skips_silently(tmp_path):
    root = _asset_project(tmp_path, "tri-skip",
        golden={"triBudgets": {"characters": 8000}},  # no default
        manifest={"assets": {"coin": {"id": "coin", "category": "items",
                                      "glb": "items/coin.glb", "tris": 1200, "version": 1}}})
    assert _only(_findings_for(root, "items/coin.glb", "coin"), C.TRI_BUDGET) == []


def test_tri_budget_category_from_path_when_record_has_none(tmp_path):
    # No category on the record → fall back to the first path segment (characters).
    root = _asset_project(tmp_path, "tri-path",
        golden={"triBudgets": {"characters": 8000, "default": 1000}},
        manifest={"assets": {"hero": {"id": "hero", "category": "",
                                      "glb": "characters/hero.glb", "tris": 9000, "version": 1}}})
    tri = _only(_findings_for(root, "characters/hero.glb", "hero"), C.TRI_BUDGET)[0]
    assert not tri.ok and "characters" in tri.message  # 9000 > 8000 via path category


# ── golden checks: thumbnail drift ─────────────────────────────────────────────

_DRIFT_GOLDEN = {"triBudgets": {"default": 10_000_000},
                 "thumbnails": {"dir": "thumbs", "maxDiff": 0.05}}


def _drift_manifest(thumbnail):
    return {"assets": {"hero": {"id": "hero", "category": "characters",
                               "glb": "characters/hero.glb", "tris": 10,
                               "thumbnail": thumbnail, "version": 1}}}


def test_thumbnail_drift_pass(tmp_path):
    png = make_png(_solid(4, 4, (120, 130, 140)))
    root = _asset_project(tmp_path, "drift-pass", golden=_DRIFT_GOLDEN,
        manifest=_drift_manifest("thumbnails/hero.png"),
        thumbs={"thumbs/hero.png": png, "exports/thumbnails/hero.png": png})
    drift = _only(_findings_for(root, "characters/hero.glb", "hero"), C.THUMB_DRIFT)
    assert len(drift) == 1 and drift[0].ok and drift[0].severity == "ok"


def test_thumbnail_drift_over_is_error(tmp_path):
    root = _asset_project(tmp_path, "drift-fail", golden=_DRIFT_GOLDEN,
        manifest=_drift_manifest("thumbnails/hero.png"),
        thumbs={"thumbs/hero.png": make_png(_solid(4, 4, (0, 0, 0))),
                "exports/thumbnails/hero.png": make_png(_solid(4, 4, (255, 255, 255)))})
    drift = _only(_findings_for(root, "characters/hero.glb", "hero"), C.THUMB_DRIFT)[0]
    assert not drift.ok and drift.severity == "error"
    assert "drift" in drift.message and "1.0" in drift.message  # diff value reported


def test_thumbnail_drift_missing_current_is_warn(tmp_path):
    # Golden reference exists, but the manifest lists no current thumbnail.
    root = _asset_project(tmp_path, "drift-nocur", golden=_DRIFT_GOLDEN,
        manifest=_drift_manifest(None),
        thumbs={"thumbs/hero.png": make_png(_solid(4, 4, (10, 10, 10)))})
    drift = _only(_findings_for(root, "characters/hero.glb", "hero"), C.THUMB_DRIFT)[0]
    assert not drift.ok and drift.severity == "warn"


def test_thumbnail_drift_unsupported_png_is_warn(tmp_path):
    # Golden reference is a palette PNG the reader can't handle → warn, not crash.
    palette = _png(4, 4, 8, 3, 0, zlib.compress(b"\x00" * 8))
    root = _asset_project(tmp_path, "drift-bad", golden=_DRIFT_GOLDEN,
        manifest=_drift_manifest("thumbnails/hero.png"),
        thumbs={"thumbs/hero.png": palette,
                "exports/thumbnails/hero.png": make_png(_solid(4, 4, (10, 10, 10)))})
    drift = _only(_findings_for(root, "characters/hero.glb", "hero"), C.THUMB_DRIFT)[0]
    assert not drift.ok and drift.severity == "warn"


def test_thumbnail_drift_dimension_mismatch_is_warn(tmp_path):
    root = _asset_project(tmp_path, "drift-dim", golden=_DRIFT_GOLDEN,
        manifest=_drift_manifest("thumbnails/hero.png"),
        thumbs={"thumbs/hero.png": make_png(_solid(4, 4, (10, 10, 10))),
                "exports/thumbnails/hero.png": make_png(_solid(2, 2, (10, 10, 10)))})
    drift = _only(_findings_for(root, "characters/hero.glb", "hero"), C.THUMB_DRIFT)[0]
    assert not drift.ok and drift.severity == "warn"


def test_thumbnail_drift_no_golden_reference_skips(tmp_path):
    # No <asset-id>.png under the golden thumbs dir → the check does not apply.
    root = _asset_project(tmp_path, "drift-none", golden=_DRIFT_GOLDEN,
        manifest=_drift_manifest("thumbnails/hero.png"),
        thumbs={"exports/thumbnails/hero.png": make_png(_solid(4, 4, (10, 10, 10)))})
    assert _only(_findings_for(root, "characters/hero.glb", "hero"), C.THUMB_DRIFT) == []


# ── run_project integration (golden checks flow through the runner) ─────────────

def test_run_project_includes_golden_checks(tmp_path):
    root = _asset_project(tmp_path, "run",
        golden={"triBudgets": {"characters": 8000, "default": 1000}},
        manifest={"assets": {
            "hero": {"id": "hero", "category": "characters",
                     "glb": "characters/hero.glb", "tris": 3000, "version": 1},
            "coin": {"id": "coin", "category": "items",
                     "glb": "items/coin.glb", "tris": 1200, "version": 1}}})
    report = C.run_project(load_config(str(root)), emit=False)
    assert C.TRI_BUDGET in report["checks"]
    assert report["summary"]["checks"] == len(report["checks"])
    coin = next(r for r in report["results"] if r["asset"] == "coin.glb")
    assert not coin["ok"]
    assert any(f["check"] == C.TRI_BUDGET and f["severity"] == "error" for f in coin["findings"])
    assert report["summary"]["errors"] >= 1


def test_run_project_without_golden_is_unchanged(tmp_path):
    root = _asset_project(tmp_path, "nogolden", golden=None,
        manifest={"assets": {"hero": {"id": "hero", "category": "characters",
                                      "glb": "characters/hero.glb", "tris": 3000, "version": 1}}})
    report = C.run_project(load_config(str(root)), emit=False)
    # Absent golden.json = zero behaviour change: no golden checks in the list.
    assert C.TRI_BUDGET not in report["checks"]
    assert C.THUMB_DRIFT not in report["checks"]


# ── Server: /api/golden + /api/skills ──────────────────────────────────────────

def _serve(project: Path):
    server.CONFIG = load_config(str(project))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    host, port = httpd.server_address[0], httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02},
                              daemon=True)
    thread.start()
    return httpd, thread, host, port


def _get_json(host, port, path, timeout=5.0):
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, resp.getheader("Content-Type"), json.loads(resp.read())
    finally:
        conn.close()


@pytest.fixture
def golden_server(tmp_path, monkeypatch):
    root = _asset_project(tmp_path, "server", golden={
        "_note": "docs",
        "triBudgets": {"characters": 8000, "default": 1000},
        "thumbnails": {"dir": "thumbs", "maxDiff": 0.08},
        "customEnvelope": {"foo": 1}},
        manifest={"assets": {
            "hero": {"id": "hero", "category": "characters",
                     "glb": "characters/hero.glb", "tris": 3000, "version": 1},
            "coin": {"id": "coin", "category": "items",
                     "glb": "items/coin.glb", "tris": 1200, "version": 1}}})
    monkeypatch.setattr(server, "CONFIG", load_config(str(root)))
    httpd, thread, host, port = _serve(root)
    try:
        yield host, port
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture
def plain_server(monkeypatch):
    monkeypatch.setattr(server, "CONFIG", load_config(str(MINIMAL)))
    httpd, thread, host, port = _serve(MINIMAL)
    try:
        yield host, port
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_api_golden_present(golden_server):
    host, port = golden_server
    status, ctype, data = _get_json(host, port, "/api/golden")
    assert status == 200
    assert ctype == "application/json; charset=utf-8"
    assert data["present"] is True
    # the parsed golden round-trips, including unknown + underscore keys
    assert data["golden"]["triBudgets"] == {"characters": 8000, "default": 1000}
    assert data["golden"]["customEnvelope"] == {"foo": 1}
    assert data["golden"]["_note"] == "docs"
    # per-asset budget verdicts for every manifest asset
    assert data["budgets"]["hero"] == {"tris": 3000, "budget": 8000, "over": False}
    assert data["budgets"]["coin"] == {"tris": 1200, "budget": 1000, "over": True}


def test_api_golden_absent(plain_server):
    host, port = plain_server
    status, _, data = _get_json(host, port, "/api/golden")
    assert status == 200
    assert data == {"present": False, "golden": None, "budgets": {}}


def test_api_skills_registry(plain_server):
    host, port = plain_server
    status, ctype, data = _get_json(host, port, "/api/skills")
    assert status == 200
    assert isinstance(data, list) and len(data) >= 5
    ids = {s["id"] for s in data}
    # the five skills committed before H3; the registry is not hardcoded, so
    # extra skills added by the orchestrator are fine — we only require these.
    assert {"check", "inspect", "start", "status", "create-skill"} <= ids
    for s in data:
        assert set(s) == {"id", "trigger", "name", "description", "kind"}
        assert s["trigger"] == f"/hologram:{s['id']}"
        assert isinstance(s["kind"], str) and s["kind"]
        assert isinstance(s["name"], str) and isinstance(s["description"], str)


# ── Skill frontmatter parsing (deterministic, off a temp skills dir) ───────────

def test_parse_frontmatter():
    fm = server._parse_frontmatter(
        "---\nname: Foo\ndescription: does a: thing\nallowed-tools: A, B\n---\n# body")
    assert fm["name"] == "Foo"
    assert fm["description"] == "does a: thing"  # split on the first colon only
    assert "kind" not in fm
    assert server._parse_frontmatter("kind: gate\n") == {}  # no leading --- → empty
    # surrounding quotes are stripped from values
    assert server._parse_frontmatter('---\nkind: "gate"\n---') == {"kind": "gate"}


def test_load_skills_defaults_kind_and_skips_dirs(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    (root / "foo").mkdir(parents=True)
    (root / "foo" / "SKILL.md").write_text("---\nname: Foo\ndescription: does foo\n---\n# Foo")
    (root / "bar").mkdir()
    (root / "bar" / "SKILL.md").write_text("---\nname: Bar\nkind: reference\n---\n")
    (root / "nope").mkdir()  # no SKILL.md → skipped
    monkeypatch.setattr(server, "SKILLS_DIR", root)
    skills = {s["id"]: s for s in server.load_skills()}
    assert set(skills) == {"foo", "bar"}
    assert skills["foo"]["kind"] == "workflow"  # default when frontmatter omits it
    assert skills["foo"]["trigger"] == "/hologram:foo"
    assert skills["bar"]["kind"] == "reference"
