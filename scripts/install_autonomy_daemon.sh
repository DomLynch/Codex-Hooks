#!/usr/bin/env bash
set -euo pipefail

codex_home="${CODEX_HOME:-$HOME/.codex}"
plist="$HOME/Library/LaunchAgents/com.domlynch.codex-autonomy-daemon.plist"
python_bin="$(command -v python3)"

mkdir -p "$codex_home/state" "$codex_home/logs" "$HOME/Library/LaunchAgents"

cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.domlynch.codex-autonomy-daemon</string>
  <key>ProgramArguments</key>
  <array>
    <string>$python_bin</string>
    <string>$codex_home/hooks/autonomy_daemon.py</string>
  </array>
  <key>StartInterval</key>
  <integer>720</integer>
  <key>RunAtLoad</key>
  <true/>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>StandardOutPath</key>
  <string>$codex_home/logs/autonomy_daemon.launchd.log</string>
  <key>StandardErrorPath</key>
  <string>$codex_home/logs/autonomy_daemon.launchd.err</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)" "$plist" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$plist"
launchctl kickstart -k "gui/$(id -u)/com.domlynch.codex-autonomy-daemon" >/dev/null 2>&1 || true

echo "Installed LaunchAgent: $plist"
echo "Stop switch: touch $codex_home/state/AUTONOMY_STOP"
echo "Logs: $codex_home/logs/autonomy_daemon.log"
