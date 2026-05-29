"""`hologram watch` — stream the event log to the terminal, no browser.

The no-browser glance flow. Mirrors the dashboard's humanized one-liners using
the same log primitives the SSE loop uses (`events.size` + `events.read_since`),
so the terminal and the web view stay in lockstep. Stdlib only, no curses —
plain streaming print with ANSI color that is suppressed when stdout is not a
TTY (piped/redirected).
"""

from __future__ import annotations

import sys
import time
from typing import TextIO

from . import events
from .config import Config

# ── ANSI ────────────────────────────────────────────────────────────────
RESET = "\033[0m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"

# Match the dashboard's category palette intent (skill/mcp/tool/session).
CAT_ANSI = {"skill": MAGENTA, "mcp": CYAN, "tool": BLUE, "session": YELLOW}


# ── Pure helpers (mirror app.js; unit-tested) ─────────────────────────────
def short_sid(sid: str | None) -> str:
    if not sid:
        return "—"
    return sid if sid.startswith("mcp-") else sid[:8]


def classify(ev: dict) -> str:
    t = ev.get("type")
    if t == "skill_invoke":
        return "skill"
    if t == "mcp_server" or ev.get("mcp_tool"):
        return "mcp"
    if t in ("session_start", "session_stop"):
        return "session"
    return "tool"


def should_show(ev: dict) -> bool:
    """Hide `pre`-phase tool events (the in-flight half) except skill invokes —
    same filter the feed uses so the stream isn't doubled."""
    if ev.get("phase") == "pre":
        return ev.get("type") == "skill_invoke"
    return True


def _truncate(s: str, n: int) -> str:
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def humanize(ev: dict) -> tuple[str, str]:
    """Return (action, target) for an event — plain-text twin of app.js humanize."""
    t = ev.get("type")
    if t == "session_start":
        return "started a session", ev.get("cwd") or ""
    if t == "session_stop":
        return "ended the session", ""
    if t == "mcp_server":
        action = ev.get("action") or ""
        if action == "mcp.start":
            return "MCP server came online", ev.get("detail") or ""
        if action == "mcp.stop":
            return "MCP server stopped", ""
        if action.endswith(".start"):
            return f"started {action[:-6]}", ev.get("detail") or ""
        if action.endswith(".end"):
            return f"finished {action[:-4]}", ev.get("detail") or ""
        return action, ev.get("detail") or ""
    if t == "skill_invoke":
        args = f" {ev['args']}" if ev.get("args") else ""
        return f"ran /{ev.get('skill', '?')}{args}", ""

    failed = ev.get("failed") is True
    if ev.get("mcp_tool"):
        short = ev["mcp_tool"]
        if short.startswith("mcp__"):
            short = short[5:]
        params = ev.get("params")
        target = (
            " ".join(f"{k}={v}" for k, v in params.items())
            if isinstance(params, dict)
            else ""
        )
        return (f"failed calling {short}" if failed else f"called {short}"), target

    tool = ev.get("tool")
    if tool == "Bash":
        return ("shell command failed" if failed else "ran shell command"), ev.get("command") or ""
    if tool in ("Edit", "Write", "MultiEdit"):
        if tool == "Write":
            verb = "failed to write" if failed else "wrote"
        else:
            verb = "failed to edit" if failed else "edited"
        return f"{verb} {ev.get('file_path', 'file')}", ""
    return (t or "event"), ""


def _dur_suffix(ev: dict) -> str:
    """` · 4.2s` for completed events that carry a duration; else empty."""
    if ev.get("phase") != "post":
        return ""
    ms = ev.get("duration_ms")
    if not isinstance(ms, (int, float)):
        return ""
    return f" · {ms / 1000:.1f}s" if ms >= 1000 else f" · {round(ms)}ms"


def format_event(ev: dict, color: bool = True) -> str:
    """Render one event as a single terminal line. Pure — no I/O, deterministic
    given `ev` (the time field aside). Failed events get a red `✗` marker and the
    error inline; everything else gets a category-tinted `·`."""

    def paint(text: str, code: str) -> str:
        return f"{code}{text}{RESET}" if color and code else text

    ts = ev.get("ts")
    tstr = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "--:--:--"
    failed = ev.get("failed") is True
    action, target = humanize(ev)
    sid = short_sid(ev.get("session_id")).ljust(8)
    cat = classify(ev)

    marker = paint("✗", RED) if failed else paint("·", CAT_ANSI.get(cat, ""))
    action_str = paint(action, RED) if failed else action

    line = f"{paint(tstr, DIM)}  {marker} {sid}  {action_str}"
    if target:
        line += paint(f"  {_truncate(target, 70)}", DIM)
    if failed:
        err = ev.get("error")
        if err:
            line += paint(f"  {_truncate(err, 100)}", RED)
        elif ev.get("is_interrupt"):
            line += paint("  interrupted by user", RED)
    suffix = _dur_suffix(ev)
    if suffix:
        line += paint(suffix, DIM)
    return line


# ── Streaming loop ────────────────────────────────────────────────────────
def run(
    cfg: Config,
    limit: int = 20,
    color: bool | None = None,
    interval: float = 1.0,
    stream: TextIO | None = None,
) -> int:
    """Backfill the last `limit` events, then tail the log forever (ctrl-c to stop)."""
    out = stream if stream is not None else sys.stdout
    if color is None:
        color = bool(getattr(out, "isatty", lambda: False)())
    log = cfg.events_log

    def paint(text: str, code: str) -> str:
        return f"{code}{text}{RESET}" if color and code else text

    print(f"hologram watch · {cfg.name}", file=out)
    print(paint(f"tailing {cfg.rel(log)}  (ctrl-c to stop)", DIM), file=out)

    backfill = [e for e in reversed(events.tail(log, limit)) if should_show(e)]
    if backfill:
        for ev in backfill:
            print(format_event(ev, color), file=out)
    else:
        print(paint("(no events yet)", DIM), file=out)
    out.flush()

    offset = events.size(log)
    try:
        while True:
            new, offset = events.read_since(log, offset)
            wrote = False
            for ev in new:
                if should_show(ev):
                    print(format_event(ev, color), file=out)
                    wrote = True
            if wrote:
                out.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        print(file=out)
        return 0
