import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import duckypad_hid as hidmod
from claude_pad_hook import apply_event
from claude_pad_daemon import (DEFAULTS, Pusher, assign_slots, build_frame,
                               build_gvs, project_states)


def ev(name, **kw):
    kw.setdefault("cwd", "/proj/a")
    return dict(hook_event_name=name, session_id="s1", **kw)


def run(*events):
    rec = None
    for e in events:
        rec = apply_event(rec, e)
    return rec


class HookTransitions(unittest.TestCase):
    def test_normal_turn(self):
        self.assertEqual(run(ev("SessionStart"))["state"], "idle")
        self.assertEqual(run(ev("SessionStart"), ev("UserPromptSubmit"))["state"], "working")
        self.assertEqual(run(ev("UserPromptSubmit"), ev("Stop"))["state"], "done")

    def test_permission_prompt_goes_amber_then_clears_on_approval(self):
        r = run(ev("UserPromptSubmit"),
                ev("Notification", notification_type="permission_prompt"))
        self.assertEqual(r["state"], "waiting")
        r = apply_event(r, ev("PostToolUse", tool_name="Bash"))
        self.assertEqual(r["state"], "working")

    def test_idle_prompt_is_not_waiting(self):
        r = run(ev("UserPromptSubmit"), ev("Stop"),
                ev("Notification", notification_type="idle_prompt"))
        self.assertEqual(r["state"], "done")

    def test_other_waiting_types(self):
        for t in ("elicitation_dialog", "agent_needs_input"):
            r = run(ev("UserPromptSubmit"), ev("Notification", notification_type=t))
            self.assertEqual(r["state"], "waiting", t)

    def test_background_subagent_keeps_stop_from_going_green(self):
        r = run(ev("UserPromptSubmit"),
                ev("SubagentStart", agent_id="x", background=True),
                ev("Stop"))
        self.assertEqual(r["state"], "background")
        r = apply_event(r, ev("SubagentStop", agent_id="x", background=True))
        self.assertEqual(r["state"], "done")

    def test_foreground_subagent_ignored(self):
        r = run(ev("UserPromptSubmit"),
                ev("SubagentStart", agent_id="y", background=False),
                ev("Stop"))
        self.assertEqual(r["state"], "done")

    def test_session_end_deletes(self):
        self.assertIsNone(run(ev("SessionStart"), ev("SessionEnd")))

    def test_stop_failure_is_error(self):
        self.assertEqual(run(ev("UserPromptSubmit"), ev("StopFailure"))["state"], "error")

    def test_unknown_event_untouched(self):
        r = run(ev("UserPromptSubmit"))
        self.assertIs(apply_event(r, ev("CwdChanged")), r)


class DaemonLogic(unittest.TestCase):
    cfg = DEFAULTS

    def test_worst_state_per_project(self):
        sessions = [{"cwd": "/a", "state": "working", "ts": 0},
                    {"cwd": "/a", "state": "waiting", "ts": 0},
                    {"cwd": "/b", "state": "done", "ts": 100}]
        p = project_states(sessions, 100, self.cfg)
        self.assertEqual(p, {"/a": "waiting", "/b": "done"})

    def test_stale_done_fades_to_idle(self):
        p = project_states([{"cwd": "/a", "state": "done", "ts": 0}], 99999, self.cfg)
        self.assertEqual(p["/a"], "idle")

    def test_slots_are_stable(self):
        m = assign_slots({"/a": "working"}, {}, [0, 1])
        m = assign_slots({"/a": "working", "/b": "done"}, m, [0, 1])
        self.assertEqual(m, {"/a": 0, "/b": 1})
        # /a finishes and closes; /b keeps its key
        m = assign_slots({"/b": "done"}, m, [0, 1])
        self.assertEqual(m["/b"], 1)

    def test_full_board_evicts_dead_project_only(self):
        m = {"/a": 0, "/b": 1}
        m = assign_slots({"/b": "working", "/c": "waiting"}, m, [0, 1])
        self.assertEqual(m, {"/b": 1, "/c": 0})
        # nothing dead to evict -> newcomer gets no slot, nobody is bumped
        m2 = assign_slots({"/b": "working", "/c": "waiting", "/d": "working"}, m, [0, 1])
        self.assertEqual(m2, m)

    def test_frame_pulses_waiting(self):
        amber = tuple(self.cfg["colors"]["waiting"])
        f0 = build_frame({"/a": "waiting"}, {"/a": 0}, [0, 1], 0, self.cfg)
        f1 = build_frame({"/a": "waiting"}, {"/a": 0}, [0, 1], 1, self.cfg)
        self.assertEqual(f0[0], amber)
        self.assertNotEqual(f1[0], amber)
        self.assertEqual(f0[1], (0, 0, 0))

    def test_gvs(self):
        g = build_gvs({"/a": "waiting", "/b": "working", "/c": "waiting"}, self.cfg)
        self.assertEqual(g, {20: 6, 21: 2})
        self.assertEqual(build_gvs({}, self.cfg), {20: 0, 21: 0})


class FakePad:
    def __init__(self, busy_after=None):
        self.sent, self.busy_after = [], busy_after

    def set_led(self, i, r, g, b):
        if self.busy_after is not None and len(self.sent) >= self.busy_after:
            raise hidmod.DuckyPadBusy()
        self.sent.append(("led", i, (r, g, b)))

    def write_gv(self, v):
        self.sent.append(("gv", v))


class PusherBehaviour(unittest.TestCase):
    def test_only_changes_are_sent(self):
        pad = FakePad()
        p = Pusher(pad, lambda m: None)
        p.push({0: (1, 2, 3), 1: (0, 0, 0)}, {20: 4})
        p.push({0: (1, 2, 3), 1: (9, 9, 9)}, {20: 4})
        self.assertEqual(pad.sent, [("led", 0, (1, 2, 3)), ("led", 1, (0, 0, 0)),
                                    ("gv", {20: 4}), ("led", 1, (9, 9, 9))])

    def test_busy_retries_next_tick(self):
        pad = FakePad(busy_after=1)
        p = Pusher(pad, lambda m: None)
        self.assertFalse(p.push({0: (1, 1, 1), 1: (2, 2, 2)}, {}))
        pad.busy_after = None
        self.assertTrue(p.push({0: (1, 1, 1), 1: (2, 2, 2)}, {}))
        self.assertEqual([s[1] for s in pad.sent], [0, 1])  # LED 0 not resent


class PusherErrorLogging(unittest.TestCase):
    def test_error_logged_once_then_recovery(self):
        class Unplugged(FakePad):
            def set_led(self, i, r, g, b):
                raise hidmod.DuckyPadNotFound("not found")
        logs = []
        p = Pusher(Unplugged(), logs.append)
        for _ in range(3):
            self.assertFalse(p.push({0: (1, 1, 1)}, {}))
        self.assertEqual(logs, ["pad: not found"])
        p.pad = FakePad()
        self.assertTrue(p.push({0: (1, 1, 1)}, {}))
        self.assertTrue(p.push({0: (1, 1, 1)}, {}))
        self.assertEqual(logs, ["pad: not found", "pad: ok"])


HOOK = os.path.join(os.path.dirname(__file__), "..", "claude_pad_hook.py")


class HookProcess(unittest.TestCase):
    """Runs the real hook script with HOME pointed at a temp dir."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = dict(os.environ, HOME=self.tmp.name)
        self.pad_home = os.path.join(self.tmp.name, ".claude-pad")

    def tearDown(self):
        self.tmp.cleanup()

    def spawn(self, event):
        p = subprocess.Popen([sys.executable, HOOK], stdin=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=self.env)
        p.stdin.write(json.dumps(event).encode())
        p.stdin.close()
        return p

    def run_hook(self, event):
        p = self.spawn(event)
        _, err = p.communicate(timeout=10)
        self.assertEqual(p.returncode, 0, err)

    def state(self):
        with open(os.path.join(self.pad_home, "sessions", "s1.json")) as f:
            return json.load(f)

    def test_concurrent_hooks_lose_no_updates(self):
        self.run_hook(ev("UserPromptSubmit"))
        procs = [self.spawn(ev("SubagentStart", agent_id=f"a{i}", background=True))
                 for i in range(20)]
        for p in procs:
            _, err = p.communicate(timeout=10)
            self.assertEqual(p.returncode, 0, err)
        self.assertEqual(sorted(self.state()["bg_agents"]),
                         sorted(f"a{i}" for i in range(20)))

    def test_lock_timeout_skips_update_without_failing(self):
        import fcntl
        import time
        self.run_hook(ev("UserPromptSubmit"))
        with open(os.path.join(self.pad_home, "hook.lock"), "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            start = time.monotonic()
            self.run_hook(ev("Stop"))
            elapsed = time.monotonic() - start
        self.assertLess(elapsed, 3)
        self.assertEqual(self.state()["state"], "working")  # update dropped, not queued

    def test_debug_failure_still_updates_state(self):
        os.makedirs(self.pad_home)
        open(os.path.join(self.pad_home, "debug"), "w").close()
        os.mkdir(os.path.join(self.pad_home, "events.jsonl"))  # unwritable as a file
        self.run_hook(ev("UserPromptSubmit"))
        self.assertEqual(self.state()["state"], "working")

    def test_debug_capture_off_by_default(self):
        self.run_hook(ev("UserPromptSubmit"))
        self.assertFalse(os.path.exists(os.path.join(self.pad_home, "events.jsonl")))

    def test_debug_capture_when_flagged(self):
        os.makedirs(self.pad_home)
        open(os.path.join(self.pad_home, "debug"), "w").close()
        self.run_hook(ev("UserPromptSubmit", prompt="x" * 5000))
        with open(os.path.join(self.pad_home, "events.jsonl")) as f:
            lines = [json.loads(line) for line in f]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["event"]["hook_event_name"], "UserPromptSubmit")
        self.assertLess(len(lines[0]["event"]["prompt"]), 2100)
        self.assertTrue(lines[0]["parents"])
        self.assertEqual(self.state()["state"], "working")


class Packets(unittest.TestCase):
    def test_set_led(self):
        pkt = hidmod.build_set_led(3, 255, 128, 0)
        self.assertEqual(len(pkt), 64)
        self.assertEqual(pkt[:7], bytes([0x05, 0, 0x04, 3, 255, 128, 0]))
        self.assertEqual(pkt[7:], bytes(57))

    def test_write_gv_little_endian_top_bit(self):
        pkt = hidmod.build_write_gv({21: 2, 20: 0x01020304})
        self.assertEqual(pkt[2], 0x19)
        self.assertEqual(pkt[3], 20 | 0x80)
        self.assertEqual(struct.unpack("<I", pkt[4:8])[0], 0x01020304)
        self.assertEqual(pkt[8], 21 | 0x80)
        self.assertEqual(struct.unpack("<I", pkt[9:13])[0], 2)

    def test_bad_led_index(self):
        with self.assertRaises(ValueError):
            hidmod.build_set_led(20, 0, 0, 0)


if __name__ == "__main__":
    unittest.main()
