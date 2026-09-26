# claude-pad

Claude Code agent status on a duckyPad Pro. The whole top row shows the most
urgent state across all your sessions (or one key per project, see Config):

| Colour | Meaning |
|---|---|
| blue | working |
| **amber, pulsing** | **waiting on you** (permission, question, input) — also a macOS banner |
| purple | turn ended, background tasks (agents, shells) still running |
| green | done (fades to dim after 30 min) |
| red | API error |
| dim white | session open, idle (also after Esc or a denied permission) |

## How it works

```
Claude Code hooks ─► claude_pad_hook.py ─► ~/.claude-pad/sessions/<id>.json
                                                    │
                                    claude_pad_daemon.py (launchd)
                                                    │  HID, open/close per write
                                                    ▼
                                            duckyPad Pro LEDs + _GV20/_GV21
```

The hook only writes a file (~25 ms). The daemon is the only thing that talks to
the pad, retries when the pad says BUSY, and repaints every 10 s so a profile
switch doesn't wipe the status.

## Install

1. Close the duckyPad configurator (it holds the device).
2. `./install.sh`
3. Start a **new** Claude Code session.

Uninstall: `./install.sh --uninstall`

## Check it

```bash
python3 duckypad_hid.py                 # top row flashes red, green, off
tail -f ~/.claude-pad/daemon.log         # state changes, pad errors
python3 -m unittest discover -s tests    # logic tests, no hardware needed
```

Then ask Claude Code to run something that needs permission. Its key should go amber and a banner should appear.

## Config

Create `~/.claude-pad/config.json` to override anything in `DEFAULTS` at the top of
`claude_pad_daemon.py`, e.g.

```json
{ "slots": [0, 1, 2, 3], "ntfy_url": "https://ntfy.sh/your-topic", "mac_notify": true }
```

`"layout": "per_project"` gives each project its own key instead of lighting the whole row.

Then `launchctl kickstart -k gui/$(id -u)/com.claude-pad.daemon`.

## Likely snags

- **Works from Terminal, "not found" under launchd:** macOS may want the Python
  binary itself in Privacy & Security → Input Monitoring. The path is the first
  line `install.sh` prints.
- **Keys flicker back to profile colours:** that's the profile repainting; the
  daemon wins it back within 10 s. Lower `resync_s` if it bugs you.
- **More than 4 projects (per_project layout):** extra ones don't get a key (nobody
  gets bumped), but they still count in `_GV20/_GV21`.

`status_key.txt` is an optional duckyPad key script that prints the summary on the OLED.
