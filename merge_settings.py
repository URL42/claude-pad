#!/usr/bin/env python3
"""Add (or with --remove, take out) the claude-pad hooks in a Claude Code settings.json.
Never touches other hooks. Writes a timestamped backup before changing anything."""

import contextlib
import glob
import json
import os
import shutil
import sys
import tempfile
import time

CMD = 'python3 "$HOME/.claude-pad/claude_pad_hook.py"'
WAITING = "permission_prompt|elicitation_dialog|elicitation_url_dialog|agent_needs_input"
EVENTS = {  # event -> matcher (None = no matcher)
    "SessionStart": None, "UserPromptSubmit": None, "PostToolUse": None,
    "PostToolUseFailure": None, "Notification": WAITING, "PermissionRequest": None,
    "SubagentStop": None, "Stop": None, "StopFailure": None, "SessionEnd": None,
}
RETIRED = ["SubagentStart"]  # ours in older installs; always taken out
KEEP_BACKUPS = 5


def ours(group):
    return any(h.get("command") == CMD for h in group.get("hooks", []))


def merge(settings, remove=False):
    hooks = settings.setdefault("hooks", {})
    for event in [*EVENTS, *RETIRED]:
        matcher = EVENTS.get(event)
        groups = [g for g in hooks.get(event, []) if not ours(g)]
        if not remove and event in EVENTS:
            group = {"hooks": [{"type": "command", "command": CMD, "timeout": 5}]}
            if matcher:
                group["matcher"] = matcher
            groups.append(group)
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    if not hooks:
        settings.pop("hooks")  # don't leave an empty "hooks": {} behind on uninstall
    return settings


def write_atomic(path, settings):
    """Write via a temp file + rename, so a crash can't leave settings.json half-written."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(settings, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def prune_backups(path):
    # Only our timestamped backups (they sort chronologically), never a hand-made .bak-foo
    for old in sorted(glob.glob(glob.escape(path) + ".bak-[0-9]*"))[:-KEEP_BACKUPS]:
        os.remove(old)


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: merge_settings.py <settings.json> [--remove]")
    path = os.path.realpath(os.path.expanduser(sys.argv[1]))  # keep a dotfile symlink intact
    remove = "--remove" in sys.argv
    settings = {}
    if os.path.exists(path):
        with open(path) as f:
            settings = json.load(f)
        shutil.copy(path, f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        prune_backups(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_atomic(path, merge(settings, remove))
    print(("Removed" if remove else "Added") + f" claude-pad hooks in {path}")


if __name__ == "__main__":
    main()
