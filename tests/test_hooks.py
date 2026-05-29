"""`build_record` in the Claude Code logging hook — focused on the v0.2 failure
mapping (PostToolUseFailure) and that success/filter behavior is unchanged.

The hook is a standalone stdlib script that deliberately imports no project code,
so it is loaded by file path rather than as a package module.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK_PATH = REPO / "hologram" / "plugin" / "hooks" / "log_event.py"


def _load_hook():
    spec = importlib.util.spec_from_file_location("hologram_hook_log_event", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


hook = _load_hook()
build_record = hook.build_record


def test_bash_failure_maps_failed_with_error_and_duration():
    rec = build_record({
        "hook_event_name": "PostToolUseFailure",
        "session_id": "s1",
        "tool_name": "Bash",
        "tool_input": {"command": "false"},
        "error": "Command exited with non-zero status code 1",
        "duration_ms": 4231,
    })
    assert rec == {
        "type": "tool_use", "phase": "post", "session_id": "s1",
        "duration_ms": 4231, "failed": True,
        "error": "Command exited with non-zero status code 1",
        "tool": "Bash", "command": "false",
    }


def test_interrupt_flag_carried_only_when_true():
    rec = build_record({
        "hook_event_name": "PostToolUseFailure",
        "session_id": "s1", "tool_name": "Bash",
        "tool_input": {"command": "sleep 100"},
        "is_interrupt": True,
    })
    assert rec["failed"] is True
    assert rec["is_interrupt"] is True


def test_bash_success_has_no_failure_keys():
    rec = build_record({
        "hook_event_name": "PostToolUse",
        "session_id": "s1", "tool_name": "Bash",
        "tool_input": {"command": "blender --version"},
        "duration_ms": 1200,
    })
    assert rec["phase"] == "post"
    assert rec["duration_ms"] == 1200
    assert "failed" not in rec and "error" not in rec and "is_interrupt" not in rec


def test_failed_edit_respects_watched_suffix_filter():
    watched = build_record({
        "hook_event_name": "PostToolUseFailure",
        "session_id": "s1", "tool_name": "Edit",
        "tool_input": {"file_path": "scripts/export.py"},
        "error": "No such file or directory: 'scripts/export.py'",
    })
    assert watched["tool"] == "Edit"
    assert watched["file_path"] == "scripts/export.py"
    assert watched["failed"] is True

    # A failure on an unwatched file type is still filtered out, like success.
    ignored = build_record({
        "hook_event_name": "PostToolUseFailure",
        "session_id": "s1", "tool_name": "Edit",
        "tool_input": {"file_path": "notes/todo.txt"},
        "error": "boom",
    })
    assert ignored is None


def test_mcp_failure_maps_to_mcp_tool():
    rec = build_record({
        "hook_event_name": "PostToolUseFailure",
        "session_id": "s1", "tool_name": "mcp__hologram__inspect_asset",
        "tool_input": {"path": "weapons/sword.glb"},
        "error": "bad path",
    })
    assert rec["mcp_tool"] == "mcp__hologram__inspect_asset"
    assert rec["failed"] is True
    assert rec["params"] == {"path": "weapons/sword.glb"}


def test_long_error_is_truncated():
    long = "x" * 500
    rec = build_record({
        "hook_event_name": "PostToolUseFailure",
        "session_id": "s1", "tool_name": "Bash",
        "tool_input": {"command": "boom"},
        "error": long,
    })
    assert len(rec["error"]) == hook.MAX_ERROR + 1  # truncated body + ellipsis
    assert rec["error"].endswith("…")


def test_pre_tool_use_has_no_duration_or_failure():
    rec = build_record({
        "hook_event_name": "PreToolUse",
        "session_id": "s1", "tool_name": "Bash",
        "tool_input": {"command": "blender --background"},
        "duration_ms": 999,
    })
    assert rec["phase"] == "pre"
    assert "duration_ms" not in rec
    assert "failed" not in rec
