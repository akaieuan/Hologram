"""`hologram watch` pure helpers — humanize/format/should_show and the backfill
formatting. The streaming loop is I/O; these cover the deterministic core."""

from __future__ import annotations

from hologram import watch


def test_humanize_bash_success_vs_failure():
    ok = {"type": "tool_use", "phase": "post", "tool": "Bash", "command": "ls"}
    bad = {**ok, "failed": True}
    assert watch.humanize(ok) == ("ran shell command", "ls")
    assert watch.humanize(bad) == ("shell command failed", "ls")


def test_humanize_edit_and_write_verbs():
    assert watch.humanize({"tool": "Edit", "file_path": "a.py"})[0] == "edited a.py"
    assert watch.humanize({"tool": "Edit", "file_path": "a.py", "failed": True})[0] == "failed to edit a.py"
    assert watch.humanize({"tool": "Write", "file_path": "a.glb"})[0] == "wrote a.glb"
    assert watch.humanize({"tool": "Write", "file_path": "a.glb", "failed": True})[0] == "failed to write a.glb"


def test_humanize_mcp_strips_prefix_and_joins_params():
    action, target = watch.humanize({
        "mcp_tool": "mcp__hologram__inspect_asset",
        "params": {"path": "weapons/sword.glb"},
    })
    assert action == "called hologram__inspect_asset"
    assert target == "path=weapons/sword.glb"


def test_humanize_asset_diff_summarizes_changes():
    action, target = watch.humanize({
        "type": "asset_diff",
        "path": "export/gltf/weapons/sword.glb",
        "gained": {"materials": ["steel", "wood"]},
        "lost": {"animations": ["Bob"]},
        "changed": {},
    })
    assert action == "asset changed"
    assert target.startswith("export/gltf/weapons/sword.glb · ")
    assert "+2 materials" in target
    assert "-1 animations" in target


def test_humanize_asset_diff_count_only_fallback():
    # When there are no name-level changes, fall back to the count delta.
    _, target = watch.humanize({
        "type": "asset_diff", "path": "a.glb",
        "gained": {}, "lost": {}, "changed": {"skins": {"from": 0, "to": 1}},
    })
    assert "skins 0->1" in target


def test_should_show_hides_pre_tool_but_keeps_skill_and_post():
    assert watch.should_show({"phase": "pre", "type": "tool_use"}) is False
    assert watch.should_show({"phase": "pre", "type": "skill_invoke"}) is True
    assert watch.should_show({"phase": "post", "type": "tool_use"}) is True
    assert watch.should_show({"type": "session_start"}) is True


def test_short_sid_keeps_mcp_prefix_else_truncates():
    assert watch.short_sid("mcp-blender") == "mcp-blender"
    assert watch.short_sid("0123456789abcdef") == "01234567"
    assert watch.short_sid(None) == "—"


def test_format_event_plain_failure_has_marker_error_no_ansi():
    line = watch.format_event({
        "ts": 0, "type": "tool_use", "phase": "post", "session_id": "sess1234",
        "failed": True, "error": "boom", "tool": "Bash", "command": "make",
        "duration_ms": 4231,
    }, color=False)
    assert "\033[" not in line  # no ANSI when color disabled
    assert "✗" in line
    assert "shell command failed" in line
    assert "make" in line
    assert "boom" in line
    assert "· 4.2s" in line


def test_format_event_success_marker_and_duration():
    line = watch.format_event({
        "ts": 0, "type": "tool_use", "phase": "post", "session_id": "sess1234",
        "tool": "Bash", "command": "blender --version", "duration_ms": 820,
    }, color=False)
    assert "·" in line and "✗" not in line
    assert "ran shell command" in line
    assert "· 820ms" in line


def test_format_event_color_emits_ansi():
    ev = {"ts": 0, "type": "tool_use", "phase": "post", "session_id": "s",
          "failed": True, "error": "x", "tool": "Bash", "command": "c"}
    assert "\033[" in watch.format_event(ev, color=True)


def test_format_event_interrupt_without_error():
    line = watch.format_event({
        "ts": 0, "type": "tool_use", "phase": "post", "session_id": "s",
        "failed": True, "is_interrupt": True, "tool": "Bash", "command": "sleep 99",
    }, color=False)
    assert "interrupted by user" in line
