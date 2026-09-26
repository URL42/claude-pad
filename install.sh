#!/bin/bash
# install.sh - set up claude-pad on macOS.
#   ./install.sh            install / update
#   ./install.sh --uninstall
set -euo pipefail

DEST="$HOME/.claude-pad"
PLIST="$HOME/Library/LaunchAgents/com.claude-pad.daemon.plist"
SETTINGS="$HOME/.claude/settings.json"
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3)"

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  "$PY" "$HERE/merge_settings.py" "$SETTINGS" --remove
  echo "Removed daemon and hooks. State left in $DEST (delete it by hand if you like)."
  exit 0
fi

echo "Using python: $PY"
"$PY" -c "import hid" 2>/dev/null || "$PY" -m pip install --user hidapi

mkdir -p "$DEST/sessions" "$HOME/Library/LaunchAgents"
cp "$HERE"/duckypad_hid.py "$HERE"/claude_pad_hook.py "$HERE"/claude_pad_daemon.py "$DEST/"

# Hooks: merged into ~/.claude/settings.json, with a timestamped backup first.
"$PY" "$HERE/merge_settings.py" "$SETTINGS"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.claude-pad.daemon</string>
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

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo
echo "Done. Daemon log:   tail -f $DEST/daemon.log"
echo "Open a NEW Claude Code session - running sessions don't pick up new hooks."
