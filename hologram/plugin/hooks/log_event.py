#!/usr/bin/env python3
"""Generic Claude Code event-logging hook for hologram.

Reads one hook payload as JSON on stdin and appends a single JSON line to the
project's hologram event log, which the dashboard tails live over SSE.

Self-contained: stdlib only, imports no project code and does not import the
`hologram` package (it may run under a different interpreter than the one the
package is installed in). It resolves the event-log path itself.

What it records (nothing else, to keep the feed pipeline-relevant):
  * SessionStart / SessionEnd  -> session lifecycle
  * UserPromptSubmit           -> slash-command invocations only ("/foo …")
  * Bash                       -> the command (truncated)
  * Write / Edit / MultiEdit   -> edits to .glb / .gltf / .py / .toml files
  * mcp__hologram* / mcp__Blender* / mcp__blender* tool calls
  * PostToolUseFailure         -> the same tools, flagged `failed` with the error

PostToolUse fires only on success; failures arrive as a separate
PostToolUseFailure event carrying a top-level `error` string, `is_interrupt`,
and `duration_ms`. Failures reuse the same tool filters as successes so the feed
stays pipeline-relevant.

Everything else is ignored. The hook always exits 0 and never raises, so a
logging failure can never block a tool call or break the session.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

DEFAULT_EVENTS_LOG = ".hologram/events.jsonl"
CONFIG_NAME = "hologram.toml"

# Files whose edits are worth surfacing in a Blender -> glTF pipeline feed.
WATCHED_SUFFIXES = (".glb", ".gltf", ".py", ".toml")

# MCP servers whose calls are relevant to the pipeline.
MCP_PREFIXES = ("mcp__hologram", "mcp__Blender", "mcp__blender")

MAX_COMMAND = 1000
MAX_PARAM_VALUE = 160
MAX_PARAMS = 8
MAX_ERROR = 300


def find_root(cwd: str | None) -> Path:
    """Locate the project root the same way hologram.config does:
    CLAUDE_PROJECT_DIR (if it holds a hologram.toml) -> walk up from cwd for a
    hologram.toml -> fall back to cwd."""
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env and (Path(env) / CONFIG_NAME).is_file():
        return Path(env)
    start = Path(cwd) if cwd else Path.cwd()
    try:
        start = start.resolve()
    except OSError:
        pass
    for d in (start, *start.parents):
        if (d / CONFIG_NAME).is_file():
            return d
    return Path(env) if env else start


def events_log_path(root: Path) -> Path:
    """Read `events_log` from hologram.toml without requiring tomllib (the hook
    may run on Python 3.9). Full TOML parse if available, else a line scan."""
    cfg = root / CONFIG_NAME
    rel = DEFAULT_EVENTS_LOG
    if cfg.is_file():
        text = ""
        try:
            text = cfg.read_text(encoding="utf-8")
        except OSError:
            text = ""
        parsed = None
        try:
            import tomllib  # type: ignore
            parsed = tomllib.loads(text)
        except Exception:
            try:
                import tomli  # type: ignore
                parsed = tomli.loads(text)
            except Exception:
                parsed = None
        if isinstance(parsed, dict):
            rel = (parsed.get("paths") or {}).get("events_log") or rel
        else:
            m = re.search(r'^\s*events_log\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
            if m:
                rel = m.group(1)
    p = Path(rel)
    return p if p.is_absolute() else root / p


def write_event(log: Path, record: dict) -> None:
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


def trim_params(tool_input: dict) -> dict:
    out: dict = {}
    for i, (k, v) in enumerate(tool_input.items()):
        if i >= MAX_PARAMS:
            break
        if isinstance(v, (dict, list)):
            v = json.dumps(v)
        s = str(v)
        out[k] = s if len(s) <= MAX_PARAM_VALUE else s[: MAX_PARAM_VALUE - 1] + "…"
    return out


def build_record(payload: dict) -> dict | None:
    """Map a hook payload to an event record, or None to skip logging."""
    event = payload.get("hook_event_name", "")
    sid = payload.get("session_id", "")

    if event == "SessionStart":
        return {"type": "session_start", "session_id": sid,
                "cwd": payload.get("cwd", ""), "source": payload.get("source", "")}
    if event in ("SessionEnd", "Stop"):
        return {"type": "session_stop", "session_id": sid}

    if event == "UserPromptSubmit":
        prompt = (payload.get("prompt") or "").strip()
        if not prompt.startswith("/"):
            return None
        rest = prompt[1:]
        skill, _, args = rest.partition(" ")
        if not skill:
            return None
        return {"type": "skill_invoke", "phase": "pre", "session_id": sid,
                "skill": skill, "args": args.strip()}

    if event in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
        failed = event == "PostToolUseFailure"
        phase = "pre" if event == "PreToolUse" else "post"
        tool = payload.get("tool_name", "")
        ti = payload.get("tool_input") or {}
        if not isinstance(ti, dict):
            ti = {}
        base = {"type": "tool_use", "phase": phase, "session_id": sid}

        # duration_ms rides on any post event (success or failure) when present.
        if phase == "post":
            dur = payload.get("duration_ms")
            if isinstance(dur, (int, float)):
                base["duration_ms"] = int(dur)
        if failed:
            base["failed"] = True
            err = payload.get("error")
            if err is not None:
                err = str(err)
                base["error"] = err if len(err) <= MAX_ERROR else err[:MAX_ERROR] + "…"
            if payload.get("is_interrupt"):
                base["is_interrupt"] = True

        # Same tool filters for success and failure, so the feed stays relevant.
        if tool.startswith(MCP_PREFIXES):
            return {**base, "mcp_tool": tool, "params": trim_params(ti)}
        if tool == "Bash":
            cmd = str(ti.get("command", ""))
            return {**base, "tool": "Bash",
                    "command": cmd if len(cmd) <= MAX_COMMAND else cmd[:MAX_COMMAND] + "…"}
        if tool in ("Write", "Edit", "MultiEdit"):
            fp = str(ti.get("file_path", ""))
            if fp.lower().endswith(WATCHED_SUFFIXES):
                return {**base, "tool": tool, "file_path": fp}
            return None
        return None

    return None


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0

    try:
        record = build_record(payload)
        if record is None:
            return 0
        record = {"ts": time.time(), **record}
        root = find_root(payload.get("cwd"))
        write_event(events_log_path(root), record)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
