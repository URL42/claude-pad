#!/usr/bin/env python3
"""
claude_pad_daemon.py - owns the duckyPad. Reads the state files the hook writes,
works out one colour per project, and pushes only what changed.

  blue    working            purple  turn ended, background tasks running
  amber   WAITING ON YOU     green   done (fades to dim after STALE_DONE_S)
  red     API error          dim     idle / session open, nothing pending

Run by hand first:   python3 claude_pad_daemon.py --verbose
"""

import datetime
import glob
import json
import os
import subprocess
import sys
import time
import urllib.request

HOME = os.path.expanduser("~/.claude-pad")
STATE_DIR = os.path.join(HOME, "sessions")
SLOTS_FILE = os.path.join(HOME, "slots.json")
CONFIG_FILE = os.path.join(HOME, "config.json")

DEFAULTS = {
    "slots": [0, 1, 2, 3],          # LED indices used as agent keys (top row)
    "layout": "row",                # "row": all slots show the most urgent state;
                                    # "per_project": one slot per project
    "poll_s": 0.5,
    "resync_s": 10,                 # re-push everything (profile switches repaint LEDs)
    "stale_done_s": 1800,           # green fades to dim after 30 min
    "dead_session_s": 43200,        # no live Claude pid on record: ignore after 12 h
    "gv_worst_state": 20,           # _GV20 = most urgent state code (see STATE_CODE)
    "gv_waiting_count": 21,         # _GV21 = number of projects waiting on you
    "mac_notify": True,             # macOS banner when a prompt is left unanswered
    "ntfy_url": "",                 # e.g. https://ntfy.sh/your-topic or an n8n webhook
    "colors": {
        "empty": [0, 0, 0],
        "idle": [12, 12, 12],
        "working": [0, 60, 255],
        "background": [110, 0, 255],
        "waiting": [255, 120, 0],
        "done": [0, 200, 40],
        "error": [255, 0, 0],
    },
}

PRIORITY = ["waiting", "error", "working", "background", "done", "idle"]
STATE_CODE = {"none": 0, "idle": 1, "done": 2, "background": 3, "working": 4,
              "error": 5, "waiting": 6}


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(CONFIG_FILE) as f:
            user = json.load(f)
        cfg["colors"].update(user.pop("colors", {}))
        cfg.update(user)
    except (OSError, ValueError):
        pass
    return cfg


def worst(states):
    for s in PRIORITY:
        if s in states:
            return s
    return None


# ---------- pure logic (unit-tested) ----------

def project_states(sessions, now, cfg):
    """sessions: list of hook records -> {cwd: state}, worst state per project."""
    by_cwd = {}
    for rec in sessions:
        state = rec.get("state", "idle")
        age = now - rec.get("ts", now)
        if state == "done" and age > cfg["stale_done_s"]:
            state = "idle"
        if state == "background" and age > cfg["dead_session_s"]:
            state = "idle"  # a task that vanished without ending the turn
        by_cwd.setdefault(rec.get("cwd", "?"), []).append(state)
    return {cwd: worst(states) for cwd, states in by_cwd.items()}


def assign_slots(projects, slot_map, slots):
    """Keep existing assignments stable; give new projects a free slot.
    Projects with no live session release their slot only when one is needed."""
    slot_map = {k: v for k, v in slot_map.items() if v in slots}
    for cwd in sorted(projects):
        if cwd in slot_map:
            continue
        used = set(slot_map.values())
        free = [s for s in slots if s not in used]
        if not free:
            for old in [c for c in slot_map if c not in projects]:
                free.append(slot_map.pop(old))
                break
        if free:
            slot_map[cwd] = free[0]
    return slot_map


def _state_rgb(state, tick, colors):
    rgb = colors[state]
    if state == "waiting" and tick % 2:              # pulse: full / 25%
        rgb = [c // 4 for c in rgb]
    return tuple(rgb)


def build_frame(projects, slot_map, slots, tick, cfg):
    colors = cfg["colors"]
    frame = {s: tuple(colors["empty"]) for s in slots}
    if cfg["layout"] == "row":
        state = worst(set(projects.values()))
        if state is not None:
            frame = {s: _state_rgb(state, tick, colors) for s in slots}
        return frame
    for cwd, slot in slot_map.items():
        state = projects.get(cwd)
        if state is not None:
            frame[slot] = _state_rgb(state, tick, colors)
    return frame


def new_nudges(sessions, seen):
    """cwds to banner: sessions still waiting whose Claude Code "unanswered" nudge
    (nudge_ts, set by the hook) we haven't announced yet. seen is updated in place
    and pruned to nudges that are still live."""
    live = {(r.get("cwd", "?"), r["nudge_ts"]) for r in sessions
            if r.get("state") == "waiting" and r.get("nudge_ts")}
    due = sorted({cwd for cwd, _ in live - seen})
    seen.intersection_update(live)
    seen.update(live)
    return due


def build_gvs(projects, cfg):
    w = worst(set(projects.values())) or "none"
    waiting = sum(1 for s in projects.values() if s == "waiting")
    return {cfg["gv_worst_state"]: STATE_CODE[w], cfg["gv_waiting_count"]: waiting}


# ---------- side effects ----------

INTERRUPT_MARK = "[Request interrupted by user"   # also "... for tool use]" (deny)
TAIL_BYTES = 64 * 1024
_interrupt_cache = {}   # transcript path -> ((mtime, size), interrupted-at or None)


def _entry_text(entry):
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(c.get("text") or "" for c in content
                   if isinstance(c, dict) and c.get("type") == "text")


def interrupted_at(lines):
    """Given transcript lines, return the epoch time of an Esc/deny interrupt if it is
    the last user or assistant entry, else None. Esc and deny end the turn without a
    Stop hook, and this marker is the only trace. Transcript format is Claude Code's
    own, not a documented API: if it changes, this returns None and keys stay blue."""
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # the partial first line of a tail read
        if not isinstance(entry, dict):
            continue
        if entry.get("type") not in ("user", "assistant"):
            continue  # system / attachment entries come after the marker
        if entry.get("type") == "user" and _entry_text(entry).startswith(INTERRUPT_MARK):
            try:
                ts = entry["timestamp"].replace("Z", "+00:00")
                return datetime.datetime.fromisoformat(ts).timestamp()
            except (KeyError, ValueError):
                return None
        return None
    return None


def transcript_interrupted_at(path):
    """interrupted_at() for a transcript file, re-read only when the file changes."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (st.st_mtime, st.st_size)
    cached = _interrupt_cache.get(path)
    if cached and cached[0] == key:
        return cached[1]
    with open(path, "rb") as f:
        f.seek(max(0, st.st_size - TAIL_BYTES))
        lines = f.read().decode("utf-8", "replace").splitlines()
    result = interrupted_at(lines)
    _interrupt_cache[path] = (key, result)
    return result


_pid_is_claude = {}


def session_process(pid):
    """"dead", "claude" (alive and named claude) or "unknown" (alive, other name - a
    reused pid, or Claude Code running under another binary name)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        _pid_is_claude.pop(pid, None)
        return "dead"
    except PermissionError:
        pass  # exists, owned by someone else
    if pid not in _pid_is_claude:
        comm = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)], capture_output=True,
                              text=True, check=False, timeout=2).stdout.strip()
        _pid_is_claude[pid] = os.path.basename(comm).lower() == "claude"
    return "claude" if _pid_is_claude[pid] else "unknown"


def read_sessions(now, cfg, state_dir=STATE_DIR):
    out = []
    for path in glob.glob(os.path.join(state_dir, "*.json")):
        try:
            with open(path) as f:
                rec = json.load(f)
            proc = session_process(rec["pid"]) if rec.get("pid") else "unknown"
            if proc == "dead":
                # Died without a SessionEnd (killed, crashed). Re-read first: a resumed
                # session may have just rewritten this file with a new pid.
                with open(path) as f:
                    if json.load(f).get("pid") == rec["pid"]:
                        os.remove(path)
                continue
            if proc != "claude" and now - os.path.getmtime(path) > cfg["dead_session_s"]:
                continue
            if rec.get("state") in ("working", "waiting") and rec.get("transcript"):
                t = transcript_interrupted_at(rec["transcript"])
                if t is not None and t > rec.get("ts", 0):
                    rec["state"] = "background" if rec.get("bg_tasks") else "idle"
            out.append(rec)
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
    return out


def notify(cwd, cfg, log):
    name = os.path.basename(cwd.rstrip("/")) or cwd
    msg = f"{name} is waiting on you"
    if cfg["mac_notify"] and sys.platform == "darwin":
        subprocess.run(["osascript", "-e",
                        f'display notification "{msg}" with title "Claude Code"'],
                       check=False, timeout=5)
    if cfg["ntfy_url"]:
        try:
            req = urllib.request.Request(cfg["ntfy_url"], data=msg.encode(),
                                         headers={"Title": "Claude Code"})
            urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            log(f"ntfy failed: {e}")


class Pusher:
    """Sends only changed LEDs/GVs. On BUSY, stops and retries next tick.
    Pad errors are logged once when they start or change, not every tick."""

    def __init__(self, pad, log):
        self.pad, self.log = pad, log
        self.leds, self.gvs = {}, {}
        self.last_err = None

    def forget(self):
        self.leds, self.gvs = {}, {}

    def push(self, frame, gvs):
        from duckypad_hid import DuckyPadBusy, DuckyPadNotFound, DuckyPadError
        try:
            for led, rgb in sorted(frame.items()):
                if self.leds.get(led) != rgb:
                    self.pad.set_led(led, *rgb)
                    self.leds[led] = rgb
            changed = {k: v for k, v in gvs.items() if self.gvs.get(k) != v}
            if changed:
                self.pad.write_gv(changed)
                self.gvs.update(changed)
        except DuckyPadBusy:
            return False
        except (DuckyPadNotFound, DuckyPadError, OSError) as e:
            msg = f"pad: {e}"
            if msg != self.last_err:
                self.log(msg)
                self.last_err = msg
            self.forget()
            return False
        if self.last_err:
            self.log("pad: ok")
            self.last_err = None
        return True


def main():
    import duckypad_hid as pad
    verbose = "--verbose" in sys.argv
    log = (lambda m: print(time.strftime("%H:%M:%S"), m, flush=True)) if verbose \
        else (lambda m: None)
    cfg = load_config()
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        with open(SLOTS_FILE) as f:
            slot_map = json.load(f)
    except (OSError, ValueError):
        slot_map = {}

    pusher = Pusher(pad, log)
    last_states, nudged, last_resync, tick = {}, set(), time.time(), 0
    while True:
        now = time.time()
        sessions = read_sessions(now, cfg)
        projects = project_states(sessions, now, cfg)

        new_map = assign_slots(projects, slot_map, cfg["slots"])
        if new_map != slot_map:
            slot_map = new_map
            with open(SLOTS_FILE, "w") as f:
                json.dump(slot_map, f, indent=1)

        for cwd, state in projects.items():
            if state != last_states.get(cwd):
                log(f"{os.path.basename(cwd)}: {last_states.get(cwd)} -> {state}")
        last_states = projects
        for cwd in new_nudges(sessions, nudged):
            notify(cwd, cfg, log)

        if now - last_resync > cfg["resync_s"]:
            pusher.forget()
            last_resync = now
        pusher.push(build_frame(projects, slot_map, cfg["slots"], tick, cfg),
                    build_gvs(projects, cfg))
        tick += 1
        time.sleep(cfg["poll_s"])


if __name__ == "__main__":
    main()
