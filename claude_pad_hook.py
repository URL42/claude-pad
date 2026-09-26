#!/usr/bin/env python3
"""
claude_pad_hook.py - Claude Code hook. Records one small state file per session.

It never touches the duckyPad; the daemon owns the device. This keeps hooks fast
and means a busy pad can never slow Claude Code down.

States: idle, working, waiting (needs a decision), background (turn ended but
background subagents still running), done, error.

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


def apply_event(prev, event):
    """Pure state transition. Returns the new record, or None to delete it."""
    name = event.get("hook_event_name", "")
    rec = dict(prev) if prev else {"state": "idle", "bg_agents": []}
    rec["bg_agents"] = list(rec.get("bg_agents", []))
    rec["cwd"] = event.get("cwd") or rec.get("cwd", "")

    if name == "SessionEnd":
        return None
    if name == "SessionStart":
        rec["state"] = "idle"
        rec["bg_agents"] = []
    elif name in ("UserPromptSubmit", "PostToolUse", "PostToolUseFailure"):
        rec["state"] = "working"      # PostToolUse clears amber after you approve
    elif name == "Notification":
        ntype = event.get("notification_type", "")
        if ntype in WAITING_TYPES:
            rec["state"] = "waiting"
    elif name == "SubagentStart":
        if event.get("background") and event.get("agent_id"):
            if event["agent_id"] not in rec["bg_agents"]:
                rec["bg_agents"].append(event["agent_id"])
    elif name == "SubagentStop":
        aid = event.get("agent_id")
        if aid in rec["bg_agents"]:
            rec["bg_agents"].remove(aid)
        if rec["state"] == "background" and not rec["bg_agents"]:
            rec["state"] = "done"
    elif name == "Stop":
        rec["state"] = "background" if rec["bg_agents"] else "done"
    elif name == "StopFailure":
        rec["state"] = "error"
    else:
        return prev  # unknown event: leave file untouched

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


def write_state(path, event):
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
    if prev is not None and new.get("state") == prev.get("state") \
            and new.get("bg_agents") == prev.get("bg_agents"):
        return  # nothing changed (e.g. PostToolUse mid-turn) - skip the write

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
    except Exception:
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
        write_state(os.path.join(STATE_DIR, f"{sid}.json"), event)


if __name__ == "__main__":
    main()
