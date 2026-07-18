"""Read an explogo-style ``exports/manifest.json`` — optional per-asset metadata.

Some Blender → glTF pipelines write a sidecar manifest next to their exports: a
single ``manifest.json`` recording, per asset, the generator that produced it,
the parameters it was built from, a version number, a triangle count, and an
optional thumbnail. Alongside it sit an append-only ``audit.jsonl`` (one line per
export/decision) and a ``.history/<asset>/vN.glb`` store of prior versions.

Hologram reads that convention when it is present and ignores it entirely when it
is not — an **absent manifest is zero behaviour change**. This module is the
read-only reader for it: stdlib ``json`` only, no writes, no new dependencies. The
schema mirrors the reference exporter documented in the README's "Export manifest
convention" section (see also ``examples/export_helper.py``).

Layout, relative to the configured ``export_root``::

    export_root/
      manifest.json                 {"assets": {id: {version, params, tris, …}}}
      audit.jsonl                    one JSON object per line (schema-agnostic)
      <category>/<id>.glb            the live export
      .history/<id>/vN.glb           prior versions (keep-N rotation)
      thumbnails/<id>.vN.png         optional per-version thumbnail

Everything degrades: a missing file, an unreadable file, or a malformed line
yields ``None`` / ``[]`` rather than raising, so the dashboard and CLI never break
on a half-written pipeline.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config, resolve_within

MANIFEST_NAME = "manifest.json"
AUDIT_NAME = "audit.jsonl"
HISTORY_DIRNAME = ".history"

# Asset ids are single path components (they name a history subdirectory). Reject
# anything that could escape the exports tree; `resolve_within` is the backstop.
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Snapshot filenames are `v<N>.glb`.
_VERSION_RE = re.compile(r"^v(\d+)\.glb$")


# ── Locations ─────────────────────────────────────────────────────────────────

def manifest_path(cfg: Config) -> Path:
    """Where the sidecar manifest lives: ``<export_root>/manifest.json``."""
    return cfg.export_root / MANIFEST_NAME


def audit_path(cfg: Config) -> Path:
    """Where the append-only audit log lives: ``<export_root>/audit.jsonl``."""
    return cfg.export_root / AUDIT_NAME


def history_dir(cfg: Config, asset_id: str) -> Path | None:
    """Directory of version snapshots for one asset, or ``None`` if the id is
    unsafe or resolves outside ``export_root``."""
    if not is_safe_id(asset_id):
        return None
    return resolve_within(cfg.export_root, f"{HISTORY_DIRNAME}/{asset_id}")


def is_safe_id(asset_id: str) -> bool:
    """True when ``asset_id`` is a single, traversal-free path component."""
    return bool(asset_id) and asset_id != ".." and _SAFE_ID.match(asset_id) is not None


# ── Records ───────────────────────────────────────────────────────────────────

@dataclass
class AssetRecord:
    """One entry from ``manifest.json[assets][id]``.

    Known keys are lifted onto typed fields; anything the pipeline added beyond
    the documented schema is preserved verbatim in ``extra`` (and ``raw`` keeps
    the whole original object) so nothing is silently dropped.
    """
    id: str
    name: str
    category: str
    glb: str
    version: int
    generator: str
    params: dict[str, Any]
    tris: int | None
    thumbnail: str | None
    status: str
    created_at: str
    updated_at: str
    extra: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    # camelCase source key -> snake_case field
    _KNOWN = {
        "id", "name", "category", "glb", "version", "generator",
        "params", "tris", "thumbnail", "status", "createdAt", "updatedAt",
    }

    @classmethod
    def from_entry(cls, asset_id: str, entry: dict[str, Any]) -> AssetRecord:
        params = entry.get("params")
        return cls(
            id=str(entry.get("id", asset_id)),
            name=str(entry.get("name", asset_id)),
            category=str(entry.get("category", "")),
            glb=str(entry.get("glb", "")),
            version=_as_int(entry.get("version"), default=0),
            generator=str(entry.get("generator", "")),
            params=params if isinstance(params, dict) else {},
            tris=_as_int(entry.get("tris"), default=None),
            thumbnail=entry.get("thumbnail") if entry.get("thumbnail") else None,
            status=str(entry.get("status", "")),
            created_at=str(entry.get("createdAt", "")),
            updated_at=str(entry.get("updatedAt", "")),
            extra={k: v for k, v in entry.items() if k not in cls._KNOWN},
            raw=dict(entry),
        )

    @property
    def stem(self) -> str:
        """Filename stem of the export (the natural join key to a discovered GLB)."""
        name = self.glb.rsplit("/", 1)[-1] if self.glb else ""
        return name[:-4] if name.endswith(".glb") else (name or self.id)

    def to_dict(self) -> dict[str, Any]:
        """Compact, JSON-serializable view for the API and the dashboard."""
        out: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "glb": self.glb,
            "version": self.version,
            "generator": self.generator,
            "params": self.params,
            "tris": self.tris,
            "thumbnail": self.thumbnail,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.extra:
            out["extra"] = self.extra
        return out


class Manifest:
    """Parsed ``manifest.json`` — a read-only index of :class:`AssetRecord`s."""

    def __init__(self, path: Path, records: dict[str, AssetRecord]) -> None:
        self.path = path
        self.records = records
        self._by_stem: dict[str, AssetRecord] = {}
        for rec in records.values():
            self._by_stem.setdefault(rec.stem, rec)

    def __len__(self) -> int:
        return len(self.records)

    def __contains__(self, asset_id: object) -> bool:
        return asset_id in self.records

    def get(self, asset_id: str) -> AssetRecord | None:
        """Record for an exact asset id, or ``None``."""
        return self.records.get(asset_id)

    def by_stem(self, stem: str) -> AssetRecord | None:
        """Record whose export filename matches ``stem`` — the join used to
        enrich discovered GLBs, since a GLB's stem is the asset id in this
        convention."""
        return self.records.get(stem) or self._by_stem.get(stem)

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": len(self.records),
            "assets": {aid: rec.to_dict() for aid, rec in self.records.items()},
        }


def load_manifest(cfg: Config) -> Manifest | None:
    """Load the sidecar manifest, or ``None`` when it is absent, unreadable, or
    not the expected ``{"assets": {...}}`` shape. Never raises."""
    path = manifest_path(cfg)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    assets = data.get("assets")
    if not isinstance(assets, dict):
        return None
    records: dict[str, AssetRecord] = {}
    for asset_id, entry in assets.items():
        if isinstance(entry, dict):
            records[str(asset_id)] = AssetRecord.from_entry(str(asset_id), entry)
    return Manifest(path, records)


# ── Version history ───────────────────────────────────────────────────────────

def history_versions(cfg: Config, asset_id: str) -> list[int]:
    """Ascending list of snapshot version numbers under ``.history/<id>/``.

    Empty when the id is unsafe, the directory is missing, or it holds no
    ``vN.glb`` files."""
    hist = history_dir(cfg, asset_id)
    if hist is None or not hist.is_dir():
        return []
    versions: list[int] = []
    for child in hist.iterdir():
        m = _VERSION_RE.match(child.name)
        if m and child.is_file():
            versions.append(int(m.group(1)))
    return sorted(versions)


def history_glb(cfg: Config, asset_id: str, version: int) -> Path | None:
    """Path to one version snapshot, guaranteed inside ``export_root``. Returns
    ``None`` for an unsafe id, a non-positive version, or a missing file."""
    if not is_safe_id(asset_id) or version < 0:
        return None
    resolved = resolve_within(
        cfg.export_root, f"{HISTORY_DIRNAME}/{asset_id}/v{int(version)}.glb"
    )
    if resolved is None or not resolved.is_file():
        return None
    return resolved


# ── Audit log ─────────────────────────────────────────────────────────────────

def load_audit(
    cfg: Config, *, limit: int | None = None, asset_id: str | None = None
) -> list[dict[str, Any]]:
    """Parse ``audit.jsonl`` newest-first. Schema-agnostic — each line is decoded
    as a plain dict, so mixed record shapes (exports vs. review decisions) all
    pass through. Optionally filter to one ``assetId`` and cap at ``limit``.
    Malformed lines are skipped; a missing file yields ``[]``."""
    path = audit_path(cfg)
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if asset_id is not None and obj.get("assetId") != asset_id:
            continue
        out.append(obj)
        if limit is not None and len(out) >= limit:
            break
    return out


def _as_int(value: Any, *, default: int | None) -> Any:
    """Coerce a manifest number to ``int`` (versions/tri counts are integers),
    falling back to ``default`` on anything non-numeric."""
    if isinstance(value, bool):  # bool is an int subclass; not a count
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return default
