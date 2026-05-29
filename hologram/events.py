"""Append-only JSONL event log — the spine the dashboard tails.

Agents (the MCP server, Claude Code hooks, or a user's own export script)
append one JSON object per line. The dashboard reads recent lines and streams
new ones over SSE by watching the file's byte size.
"""

from __future__ import annotations

import json
import time
from pathlib import Path


def append(log_path: Path, etype: str, **fields) -> None:
    """Append one event. `ts` is added automatically. Failures are swallowed
    so logging never breaks the caller."""
    try:
        log_path = Path(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": time.time(), "type": etype, **fields}
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


def tail(log_path: Path, limit: int = 200) -> list[dict]:
    """Return up to `limit` most recent events, newest first."""
    log_path = Path(log_path)
    if not log_path.is_file():
        return []
    try:
        with log_path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in reversed(lines[-limit:]):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def size(log_path: Path) -> int:
    """Current byte size of the log (0 if absent). Used as the SSE cursor."""
    log_path = Path(log_path)
    try:
        return log_path.stat().st_size
    except OSError:
        return 0


def read_since(log_path: Path, offset: int) -> tuple[list[dict], int]:
    """Read events appended after byte `offset`. Returns (events, new_offset).

    If the file shrank (rotated/truncated), reads from the start.
    """
    log_path = Path(log_path)
    if not log_path.is_file():
        return [], offset
    try:
        current = log_path.stat().st_size
        if current < offset:
            offset = 0
        if current == offset:
            return [], offset
        with log_path.open("r", encoding="utf-8") as f:
            f.seek(offset)
            chunk = f.read()
            new_offset = f.tell()
    except OSError:
        return [], offset
    events: list[dict] = []
    for line in chunk.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events, new_offset
