"""Read a project's ``golden.json`` — the golden-truths convention.

Some pipelines keep a small, human-blessed file of *standards* an asset is
checked against: a triangle budget per category, a maximum allowed thumbnail
drift, and (forward-compat) whatever other envelopes a project chooses to
codify. Agents build **against** these standards; only a human moves the
goldens. Hologram reads that file when it is present and does nothing at all
when it is absent — an **absent ``golden.json`` is zero behaviour change**.

This module is the read-only reader for it: stdlib ``json`` only, no writes, no
new dependencies. It never writes ``golden.json``.

Search order (first hit wins), relative to the resolved :class:`~hologram.config.Config`::

    <project>/golden.json
    <project>/codex/golden.json
    <export_root>/golden.json

Parsed shape (all keys optional; unknown keys are preserved verbatim so a newer
golden file round-trips through an older Hologram)::

    {
      "triBudgets":  { "<category>": <int>, "default": <int> },
      "thumbnails":  { "dir": "<path>", "maxDiff": <float> },
      ...anything else...
    }

Underscore-prefixed keys (``_note``, ``_provisional``, …) are documentation:
ignored by the typed accessors but kept in ``raw`` so nothing is dropped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config

GOLDEN_NAME = "golden.json"


# ── Records ─────────────────────────────────────────────────────────────────

@dataclass
class Thumbnails:
    """The ``thumbnails`` block: where golden reference thumbnails live and how
    much per-pixel drift is tolerated before a thumbnail is considered changed."""
    dir: str
    max_diff: float


@dataclass
class GoldenTruths:
    """A parsed ``golden.json``.

    Known blocks are lifted onto typed fields; the whole original object is kept
    in ``raw`` (including unknown and underscore-prefixed keys) so the file
    round-trips and newer schemas are never silently dropped.
    """
    path: Path
    tri_budgets: dict[str, int] | None
    thumbnails: Thumbnails | None
    raw: dict[str, Any] = field(default_factory=dict)

    def budget_for(self, category: str | None) -> int | None:
        """Triangle budget for ``category``, falling back to the ``default``
        budget. ``None`` when there are no budgets at all, or when the category
        has no budget and no ``default`` is declared."""
        if not self.tri_budgets:
            return None
        if category is not None and category in self.tri_budgets:
            return self.tri_budgets[category]
        return self.tri_budgets.get("default")


# ── Locations ───────────────────────────────────────────────────────────────

def search_paths(cfg: Config) -> list[Path]:
    """The candidate locations for ``golden.json``, in priority order."""
    return [
        cfg.root / GOLDEN_NAME,
        cfg.root / "codex" / GOLDEN_NAME,
        cfg.export_root / GOLDEN_NAME,
    ]


def golden_path(cfg: Config) -> Path | None:
    """The first existing ``golden.json`` in the search order, or ``None``."""
    for path in search_paths(cfg):
        if path.is_file():
            return path
    return None


# ── Loading ─────────────────────────────────────────────────────────────────

def load_golden(cfg: Config) -> GoldenTruths | None:
    """Load the golden truths, or ``None`` when absent, unreadable, or malformed.
    Never raises — a broken golden file degrades to "no goldens", never a crash."""
    path = golden_path(cfg)
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return GoldenTruths(
        path=path,
        tri_budgets=_parse_tri_budgets(data.get("triBudgets")),
        thumbnails=_parse_thumbnails(data.get("thumbnails")),
        raw=data,
    )


def _parse_tri_budgets(value: Any) -> dict[str, int] | None:
    """Coerce the ``triBudgets`` block into ``{category: int}``. Returns ``None``
    when the block is absent or not an object; underscore-prefixed keys are
    documentation and are skipped; non-integer values are dropped."""
    if not isinstance(value, dict):
        return None
    budgets: dict[str, int] = {}
    for key, raw in value.items():
        if str(key).startswith("_"):
            continue
        n = _as_int(raw)
        if n is not None:
            budgets[str(key)] = n
    return budgets


def _parse_thumbnails(value: Any) -> Thumbnails | None:
    """Coerce the ``thumbnails`` block into a :class:`Thumbnails`. Returns
    ``None`` unless a non-empty ``dir`` string is present."""
    if not isinstance(value, dict):
        return None
    directory = value.get("dir")
    if not isinstance(directory, str) or not directory:
        return None
    return Thumbnails(dir=directory, max_diff=_as_float(value.get("maxDiff")))


def _as_int(value: Any) -> int | None:
    """Coerce a JSON number to ``int`` (budgets are integer tri counts), or
    ``None`` for anything non-numeric. ``bool`` is not a count."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _as_float(value: Any, default: float = 0.0) -> float:
    """Coerce a JSON number to ``float``, falling back to ``default``. ``bool``
    is not a threshold."""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    return default
