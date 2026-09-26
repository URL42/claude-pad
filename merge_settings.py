#!/usr/bin/env python3
"""Add (or with --remove, take out) the claude-pad hooks in a Claude Code settings.json.
Never touches other hooks. Writes a timestamped backup before changing anything."""

import json
import os
import shutil
import sys
import time

CMD = 'python3 "$HOME/.claude-pad/claude_pad_hook.py"'
WAITING = "permission_prompt|elicitation_dialog|elicitation_url_dialog|agent_needs_input"
EVENTS = {  # event -> matcher (None = no matcher)
    "SessionStart": None, "UserPromptSubmit": None, "PostToolUse": None,
    "PostToolUseFailure": None, "Notification": WAITING,
    "SubagentStop": None, "Stop": None, "StopFailure": None, "SessionEnd": None,
}
RETIRED = ["SubagentStart"]  # ours in older installs; always taken out


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
    return settings


def main():
    path = os.path.expanduser(sys.argv[1])
    remove = "--remove" in sys.argv
    settings = {}
    if os.path.exists(path):
        with open(path) as f:
            settings = json.load(f)
        shutil.copy(path, f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(merge(settings, remove), f, indent=2)
    print(("Removed" if remove else "Added") + f" claude-pad hooks in {path}")


if __name__ == "__main__":
    main()
