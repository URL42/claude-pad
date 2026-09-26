#!/usr/bin/env python3
"""
claude_pad_hook.py - Claude Code hook. Records one small state file per session.

It never touches the duckyPad; the daemon owns the device. This keeps hooks fast
and means a busy pad can never slow Claude Code down.

States: idle, working, waiting (needs a decision), background (turn ended but
background tasks still running), done, error.

Esc and denied permissions end a turn without a Stop event; the daemon spots those
in the transcript (see claude_pad_daemon.interrupted_at), so the record keeps the
transcript path. It also keeps the Claude Code pid, so the daemon can drop sessions
whose process died without a SessionEnd.

Hooks for one session can fire at the same moment (parallel tools, subagents), so
the read-modify-write happens under one lock shared by all sessions.

Debug: touch ~/.claude-pad/debug and every raw event is appended to
~/.claude-pad/events.jsonl, with the parent process chain. Delete the file to stop.
"""

import contextlib
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time

HOME = os.path.expanduser("~/.claude-pad")
STATE_DIR = os.path.join(HOME, "sessions")
LOCK_FILE = os.path.join(HOME, "hook.lock")
DEBUG_FLAG = os.path.join(HOME, "debug")
EVENTS_LOG = os.path.join(HOME, "events.jsonl")
LOCK_TIMEOUT_S = 1.0

# Notification types that mean "blocked on the human". idle_prompt is NOT one of
# them: it just means the turn finished a while ago.
WAITING_TYPES = {"permission_prompt", "elicitation_dialog", "agent_needs_input",
                 "elicitation_url_dialog"}


TOOL_EVENTS = ("PostToolUse", "PostToolUseFailure")


def apply_event(prev, event):
    """Pure state transition. Returns the new record, or None to delete it."""
    name = event.get("hook_event_name", "")
    if name in TOOL_EVENTS and event.get("agent_id") \
            and not (prev and prev.get("state") == "waiting"):
        return prev  # a subagent's tool call says nothing about the main agent -
        # unless it's the one you just approved, which ends the wait
    rec = dict(prev) if prev else {"state": "idle", "bg_tasks": 0}
    rec.pop("bg_agents", None)  # pre-background_tasks records
    rec["cwd"] = event.get("cwd") or rec.get("cwd", "")
    rec["transcript"] = event.get("transcript_path") or rec.get("transcript", "")

    if name == "SessionEnd":
        return None
    if name == "SessionStart":
        rec["state"] = "idle"
        rec["bg_tasks"] = 0
    elif name == "UserPromptSubmit" or name in TOOL_EVENTS:
        rec["state"] = "working"      # PostToolUse clears amber after you approve
    elif name == "PermissionRequest":
        rec["state"] = "waiting"      # fires the moment a permission dialog appears
        rec.pop("nudge_ts", None)     # a new wait
    elif name == "Notification":
        # Claude Code's own nudge, ~6 s after a prompt goes unanswered. The daemon
        # sends the banner/push on this, not on PermissionRequest: nothing fires when
        # you approve, so a timer would banner quick approvals of slow commands.
        # Also the only signal for elicitation dialogs (no PermissionRequest).
        ntype = event.get("notification_type", "")
        if ntype in WAITING_TYPES:
            rec["state"] = "waiting"
            rec["nudge_ts"] = time.time()
    elif name in ("Stop", "SubagentStop"):
        # Both carry Claude Code's own list of background tasks (agents and shells).
        # A finished background task starts a new turn, whose Stop updates this.
        running = sum(1 for t in event.get("background_tasks") or []
                      if t.get("status") == "running")
        rec["bg_tasks"] = running
        if name == "Stop" or rec["state"] in ("background", "done"):
            rec["state"] = "background" if running else "done"
    elif name == "StopFailure":
        rec["state"] = "error"
    else:
        return prev  # unknown event: leave file untouched

    if rec["state"] != "waiting":
        rec.pop("nudge_ts", None)

    # ts = when this state began. The daemon compares it with interrupt times, so
    # repeats (e.g. the internal SubagentStop that follows an Esc) must not bump it.
    # A new prompt always does: it's a new turn even if the key was stuck blue.
    if not prev or rec["state"] != prev.get("state") or name == "UserPromptSubmit":
        rec["ts"] = time.time()
    return rec


@contextlib.contextmanager
def locked(path, timeout_s=LOCK_TIMEOUT_S):
    """Exclusive flock on path. Yields False if it can't be had in time: better to
    drop one update than to stall Claude Code."""
    with open(path, "a") as f:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    yield False
                    return
                time.sleep(0.005)
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _clip(value, limit=2000):
    """Shorten long strings so tool responses don't bloat the debug log."""
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"...[{len(value)} chars]"
    if isinstance(value, dict):
        return {k: _clip(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip(v, limit) for v in value]
    return value


def _process_chain(pid, depth=3):
    """[(pid, command), ...] walking up from pid - to see which ancestor is Claude Code."""
    chain = []
    for _ in range(depth):
        out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                             capture_output=True, text=True, check=False, timeout=0.5).stdout.strip()
        if not out:
            break
        ppid, comm = out.split(None, 1)
        chain.append((pid, comm))
        pid = int(ppid)
    return chain


def debug_record(event):
    """The debug log line for this event, or None when debugging is off. Built
    before taking the lock so the ps calls never hold up other sessions' hooks."""
    if not os.path.exists(DEBUG_FLAG):
        return None
    rec = {"t": time.time(), "parents": _process_chain(os.getppid()), "event": _clip(event)}
    return json.dumps(rec) + "\n"


def _without_ts(rec):
    return {k: v for k, v in rec.items() if k != "ts"}


def write_state(path, event, pid):
    prev = None
    try:
        with open(path) as f:
            prev = json.load(f)
    except (OSError, ValueError):
        pass

    new = apply_event(prev, event)
    if new is None:
        try:
            os.remove(path)
        except OSError:
            pass
        return
    new["pid"] = pid
    if event.get("hook_event_name") in TOOL_EVENTS and prev is not None \
            and _without_ts(new) == _without_ts(prev):
        return  # PostToolUse mid-turn changes nothing - skip the write. Other events
        # always write: the daemon compares ts against interrupt times.

    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(new, f)
        os.replace(tmp, path)  # atomic: the daemon never reads a half-written file
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def main():
    try:
        event = json.load(sys.stdin)
    except ValueError:  # includes JSONDecodeError and bad UTF-8
        return
    if not isinstance(event, dict):
        return
    sid = event.get("session_id")
    if not sid:
        return
    os.makedirs(STATE_DIR, exist_ok=True)
    line = None
    with contextlib.suppress(Exception):  # debugging must never cost an update
        line = debug_record(event)
    with locked(LOCK_FILE) as got:
        if not got:
            return
        if line:
            with contextlib.suppress(OSError), open(EVENTS_LOG, "a") as f:
                f.write(line)
        # Our parent is the Claude Code process itself (checked with debug capture).
        write_state(os.path.join(STATE_DIR, f"{sid}.json"), event, os.getppid())


if __name__ == "__main__":
    main()
