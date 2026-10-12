#!/usr/bin/env bash
set -euo pipefail

label="com.dom.mac-load-sampler"
plist="$HOME/Library/LaunchAgents/$label.plist"
script="$(cd "$(dirname "$0")/.." && pwd)/claude/scripts/mac_load_sampler.py"
state="$HOME/.claude/state"

mkdir -p "$state" "$HOME/.claude/logs" "$HOME/Library/LaunchAgents"
install -m 755 "$script" "$state/mac_load_sampler.py"

cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array><string>/usr/bin/python3</string><string>$state/mac_load_sampler.py</string></array>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardErrorPath</key><string>$HOME/.claude/logs/mac_load_sampler.err</string>
</dict></plist>
EOF

launchctl bootout "gui/$(id -u)/$label" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$plist"
echo "installed $label (off: launchctl bootout gui/$(id -u)/$label)"
