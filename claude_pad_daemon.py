#!/usr/bin/env python3
"""
claude_pad_daemon.py - owns the duckyPad. Reads the state files the hook writes,
works out one colour per project, and pushes only what changed.

  blue    working            purple  turn ended, background subagents running
  amber   WAITING ON YOU     green   done (fades to dim after STALE_DONE_S)
  red     API error          dim     idle / session open, nothing pending

Run by hand first:   python3 claude_pad_daemon.py --verbose
"""

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
    "poll_s": 0.5,
    "resync_s": 10,                 # re-push everything (profile switches repaint LEDs)
    "stale_done_s": 1800,           # green fades to dim after 30 min
    "dead_session_s": 43200,        # ignore state files untouched for 12 h
    "gv_worst_state": 20,           # _GV20 = most urgent state code (see STATE_CODE)
    "gv_waiting_count": 21,         # _GV21 = number of projects waiting on you
    "mac_notify": True,             # macOS banner when a project starts waiting
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
        if state == "done" and now - rec.get("ts", now) > cfg["stale_done_s"]:
            state = "idle"
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


def build_frame(projects, slot_map, slots, tick, cfg):
    colors = cfg["colors"]
    frame = {s: tuple(colors["empty"]) for s in slots}
    for cwd, slot in slot_map.items():
        state = projects.get(cwd)
        if state is None:
            continue
        rgb = colors[state]
        if state == "waiting" and tick % 2:          # pulse: full / 25%
            rgb = [c // 4 for c in rgb]
        frame[slot] = tuple(rgb)
    return frame


def build_gvs(projects, cfg):
    w = worst(set(projects.values())) or "none"
    waiting = sum(1 for s in projects.values() if s == "waiting")
    return {cfg["gv_worst_state"]: STATE_CODE[w], cfg["gv_waiting_count"]: waiting}


# ---------- side effects ----------

def read_sessions(now, cfg):
    out = []
    for path in glob.glob(os.path.join(STATE_DIR, "*.json")):
        try:
            if now - os.path.getmtime(path) > cfg["dead_session_s"]:
                continue
            with open(path) as f:
                out.append(json.load(f))
        except (OSError, ValueError):
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
    last_states, last_resync, tick = {}, time.time(), 0
    while True:
        now = time.time()
        projects = project_states(read_sessions(now, cfg), now, cfg)

        new_map = assign_slots(projects, slot_map, cfg["slots"])
        if new_map != slot_map:
            slot_map = new_map
            with open(SLOTS_FILE, "w") as f:
                json.dump(slot_map, f, indent=1)

        for cwd, state in projects.items():
            if state != last_states.get(cwd):
                log(f"{os.path.basename(cwd)}: {last_states.get(cwd)} -> {state}")
                if state == "waiting":
                    notify(cwd, cfg, log)
        last_states = projects

        if now - last_resync > cfg["resync_s"]:
            pusher.forget()
            last_resync = now
        pusher.push(build_frame(projects, slot_map, cfg["slots"], tick, cfg),
                    build_gvs(projects, cfg))
        tick += 1
        time.sleep(cfg["poll_s"])


if __name__ == "__main__":
    main()
