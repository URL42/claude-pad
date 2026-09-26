#!/bin/bash
# install.sh - set up claude-pad on macOS.
#   ./install.sh            install / update
#   ./install.sh --uninstall
set -euo pipefail

DEST="$HOME/.claude-pad"
PLIST="$HOME/Library/LaunchAgents/com.claude-pad.daemon.plist"
SETTINGS="$HOME/.claude/settings.json"
HERE="$(cd "$(dirname "$0")" && pwd)"
SYS_PY="$(command -v python3)"   # stdlib-only helpers (merge_settings)
VENV="$DEST/.venv"               # the daemon's own Python, with hidapi
PY="$VENV/bin/python"
DOMAIN="gui/$(id -u)"
LABEL="com.claude-pad.daemon"

# bootout returns before launchd has fully let go, and an immediate bootstrap then
# fails with "Input/output error". Wait (up to ~5 s) for the job to disappear.
stop_daemon() {
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || return 0
    sleep 0.5
  done
}

if [[ "${1:-}" == "--uninstall" ]]; then
  stop_daemon
  rm -f "$PLIST"
  "$SYS_PY" "$HERE/merge_settings.py" "$SETTINGS" --remove
  echo "Removed daemon and hooks. State left in $DEST (delete it by hand if you like)."
  exit 0
fi

# The daemon gets its own venv so a Homebrew Python upgrade can't take hidapi away.
command -v uv >/dev/null || { echo "claude-pad needs uv: https://docs.astral.sh/uv/" >&2; exit 1; }
mkdir -p "$DEST/sessions" "$HOME/Library/LaunchAgents"
uv venv --quiet --allow-existing "$VENV"
uv pip install --quiet --python "$PY" hidapi
echo "Using python: $(readlink -f "$PY")"
cp "$HERE"/duckypad_hid.py "$HERE"/claude_pad_hook.py "$HERE"/claude_pad_daemon.py "$DEST/"

# Hooks: merged into ~/.claude/settings.json, with a timestamped backup first.
"$SYS_PY" "$HERE/merge_settings.py" "$SETTINGS"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string><string>$DEST/claude_pad_daemon.py</string><string>--verbose</string>
  </array>
  <key>WorkingDirectory</key><string>$DEST</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DEST/daemon.log</string>
  <key>StandardErrorPath</key><string>$DEST/daemon.log</string>
</dict></plist>
EOF

stop_daemon
if ! launchctl bootstrap "$DOMAIN" "$PLIST"; then
  echo "launchd wouldn't start the daemon (often: the old one hadn't fully stopped)." >&2
  echo "Hooks and plist are installed. Retry: launchctl bootstrap $DOMAIN $PLIST" >&2
  exit 1
fi
echo
echo "Done. Daemon log:   tail -f $DEST/daemon.log"
echo "Open a NEW Claude Code session - running sessions don't pick up new hooks."
