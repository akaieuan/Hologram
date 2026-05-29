"""Load and resolve `hologram.toml`.

The config is what makes Hologram generic: paths and the category taxonomy are
described by the user, not hardcoded. A project with no `[categories]` is valid
— every GLB under `export_root` becomes one flat "all" category.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - exercised only on 3.10
    import tomli as tomllib  # type: ignore[no-redef]

CONFIG_NAME = "hologram.toml"


@dataclass
class Category:
    name: str
    glb_pattern: str  # glob relative to export_root
    script_pattern: str | None = None  # glob relative to source_root


@dataclass
class Config:
    name: str
    root: Path
    export_root: Path
    source_root: Path | None
    events_log: Path
    dashboard_host: str
    dashboard_port: int
    categories: list[Category] = field(default_factory=list)

    # ── Asset discovery ────────────────────────────────────────────────
    def list_glbs(self, category: str | None = None) -> dict[str, list[Path]]:
        """Map category name -> sorted list of GLB paths matching its glob."""
        out: dict[str, list[Path]] = {}
        for cat in self.categories:
            if category and cat.name != category:
                continue
            if not self.export_root.is_dir():
                out[cat.name] = []
                continue
            matches = sorted(set(self.export_root.glob(cat.glb_pattern)))
            out[cat.name] = [p for p in matches if p.is_file()]
        return out

    def find_script_for(self, glb: Path, category: Category) -> Path | None:
        """Best-effort link from a GLB back to a source script by stem match."""
        if not (category.script_pattern and self.source_root and self.source_root.is_dir()):
            return None
        stem = glb.stem
        for cand in sorted(self.source_root.glob(category.script_pattern)):
            if cand.is_file() and stem in cand.stem:
                return cand
        return None

    def rel(self, path: Path) -> str:
        """Path relative to the project root for display (falls back to absolute)."""
        try:
            return str(Path(path).resolve().relative_to(self.root))
        except ValueError:
            return str(path)

    def resolve_asset(self, path: str) -> Path | None:
        """Resolve an asset path (absolute, or relative to root or export_root),
        guaranteeing it stays inside the project. Returns None if it escapes or
        does not exist. Shared security boundary for the inspect endpoints."""
        for base in (self.root, self.export_root):
            resolved = resolve_within(base, path)
            if resolved and resolved.is_file():
                return resolved
        return None


def find_project_root(explicit: str | None = None) -> Path:
    """Resolve the project root: explicit arg, then CLAUDE_PROJECT_DIR (if it has
    a hologram.toml), then walk up from cwd for hologram.toml, else cwd."""
    if explicit:
        return Path(explicit).resolve()
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env and (Path(env) / CONFIG_NAME).is_file():
        return Path(env).resolve()
    cur = Path.cwd().resolve()
    for parent in [cur, *cur.parents]:
        if (parent / CONFIG_NAME).is_file():
            return parent
    if env and Path(env).is_dir():
        return Path(env).resolve()
    return cur


def load_config(root: str | None = None) -> Config:
    root_path = find_project_root(root)
    cfg_path = root_path / CONFIG_NAME

    data: dict = {}
    if cfg_path.is_file():
        with cfg_path.open("rb") as f:
            data = tomllib.load(f)

    project = data.get("project", {}) or {}
    paths = data.get("paths", {}) or {}
    dash = data.get("dashboard", {}) or {}
    cats_raw = data.get("categories", {}) or {}

    export_root = (root_path / paths.get("export_root", "export/gltf")).resolve()
    src = paths.get("source_root")
    source_root = (root_path / src).resolve() if src else None
    events_log = (root_path / paths.get("events_log", ".hologram/events.jsonl")).resolve()

    categories: list[Category] = []
    if cats_raw:
        for cname, cval in cats_raw.items():
            cval = cval or {}
            categories.append(
                Category(
                    name=cname,
                    glb_pattern=cval.get("glb_pattern", f"{cname}/**/*.glb"),
                    script_pattern=cval.get("script_pattern"),
                )
            )
    else:
        # No taxonomy declared: every GLB under export_root is one flat list.
        categories.append(Category(name="all", glb_pattern="**/*.glb"))

    return Config(
        name=project.get("name", root_path.name),
        root=root_path,
        export_root=export_root,
        source_root=source_root,
        events_log=events_log,
        dashboard_host=dash.get("host", "127.0.0.1"),
        dashboard_port=int(dash.get("port", 7870)),
        categories=categories,
    )


def resolve_within(base: Path, candidate: str) -> Path | None:
    """Resolve `candidate` (absolute or relative to `base`) and guarantee it
    stays inside `base`. Returns None on traversal escape. Security boundary
    for the inspect endpoints."""
    base = Path(base).resolve()
    target = Path(candidate)
    target = (target if target.is_absolute() else base / target).resolve()
    if target == base or base in target.parents:
        return target
    return None
