# Changelog

All notable changes to Hologram are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims
to adhere to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.0] - 2026-07-18

### Added

- **Manifest-aware asset views.** A stdlib manifest reader surfaces per-asset
  metadata (version, generator, params, triangle count, thumbnail) in the
  dashboard when a project ships an `exports/manifest.json`; absent, behavior is
  unchanged.
- **Version history.** When history snapshots exist, an asset gains a
  version flip-through with diff fingerprints between snapshots, reusing the
  existing diff machinery.
- **Export helper template.** A copyable, documented `examples/export_helper.py`
  generalizing an atomic manifest upsert + audit-log append + history rotation +
  thumbnail render, meant to be pasted into a Blender pipeline. Not imported by
  the package (`bpy` stays out of the core).
- **Dashboard activity search/filter** for the live event feed, and a
  `hologram check --watch` loop that re-runs checks as the pipeline changes.
- **PyPI release pipeline.** A tag-triggered (`v*`) workflow builds an sdist and
  wheel with `uv build` and publishes via PyPI Trusted Publishing (OIDC) — no
  API-token secrets in the repository.

### Changed

- **akaOSS visual + copy alignment (lift, merged on `main`).** Warm-neutral
  design tokens in light and dark with an amber accent, honest MCP wording,
  consistent "Hologram" casing, and README polish.

### Developer experience

- **Static type checking with pyright** (basic mode) via a `[tool.pyright]`
  config and a dedicated CI job, alongside the existing pytest matrix and ruff.
- **Expanded dashboard test coverage.** The `/api/{health,state,events,inspect,
  checks,active,blender_mcp}` JSON endpoints and the `/api/events/stream` SSE
  framing are now exercised end-to-end against a real server on an ephemeral
  port, on top of the existing `/api/glb` route tests.

## [0.5.0] - 2026-05

### Added

- Live observability dashboard (stdlib HTTP + SSE), guided skills bundled with
  the Claude Code plugin, and a read-only MCP agent surface (FastMCP, stdio)
  over Blender → glTF asset pipelines.

[0.6.0]: https://github.com/akaieuan/Hologram/releases/tag/v0.6.0
[0.5.0]: https://github.com/akaieuan/Hologram/releases/tag/v0.5.0
