"""Append-only event log: roundtrip, tail ordering, and SSE size-delta reads."""

from __future__ import annotations

from hologram import events


def test_append_and_tail_newest_first(tmp_path):
    log = tmp_path / "events.jsonl"
    events.append(log, "session_start", session_id="s1")
    events.append(log, "tool_use", tool="Bash", command="ls")
    events.append(log, "session_stop", session_id="s1")

    got = events.tail(log)
    assert [e["type"] for e in got] == ["session_stop", "tool_use", "session_start"]
    assert all("ts" in e for e in got)
    assert got[1]["command"] == "ls"


def test_tail_limit(tmp_path):
    log = tmp_path / "events.jsonl"
    for i in range(10):
        events.append(log, "tick", n=i)
    got = events.tail(log, limit=3)
    assert [e["n"] for e in got] == [9, 8, 7]


def test_tail_missing_file_is_empty(tmp_path):
    assert events.tail(tmp_path / "nope.jsonl") == []


def test_size_grows(tmp_path):
    log = tmp_path / "events.jsonl"
    assert events.size(log) == 0
    events.append(log, "x")
    assert events.size(log) > 0


def test_read_since_incremental(tmp_path):
    log = tmp_path / "events.jsonl"
    events.append(log, "a")
    offset = events.size(log)

    new, offset2 = events.read_since(log, offset)
    assert new == []  # nothing appended yet
    assert offset2 == offset

    events.append(log, "b", v=2)
    new, offset3 = events.read_since(log, offset2)
    assert [e["type"] for e in new] == ["b"]
    assert new[0]["v"] == 2
    assert offset3 > offset2


def test_read_since_handles_truncation(tmp_path):
    log = tmp_path / "events.jsonl"
    events.append(log, "a")
    events.append(log, "b")
    stale_offset = events.size(log) + 999  # pretend we're past EOF after a rotate
    new, _ = events.read_since(log, stale_offset)
    # File is smaller than the offset -> re-read from the start.
    assert [e["type"] for e in new] == ["a", "b"]


def test_malformed_lines_skipped(tmp_path):
    log = tmp_path / "events.jsonl"
    log.write_text('{"type": "ok"}\nnot json\n{"type": "ok2"}\n', encoding="utf-8")
    got = events.tail(log)
    assert [e["type"] for e in got] == ["ok2", "ok"]
