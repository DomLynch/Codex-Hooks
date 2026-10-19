#!/usr/bin/env bash
# Install the daily scratch janitor (DRY RUN by default: it only logs what it would remove).
# To let it delete, Dom says yes (via Strategy), then re-run with APPLY=1.
set -euo pipefail

label="com.dom.scratch-janitor"
plist="$HOME/Library/LaunchAgents/$label.plist"
script="$(cd "$(dirname "$0")/.." && pwd)/claude/scripts/scratch_janitor.py"
state="$HOME/.claude/state"
extra=""
[ "${APPLY:-0}" = "1" ] && extra="<string>--apply</string>"

mkdir -p "$state" "$HOME/.claude/logs" "$HOME/Library/LaunchAgents"
install -m 755 "$script" "$state/scratch_janitor.py"

cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array><string>/usr/bin/python3</string><string>$state/scratch_janitor.py</string>$extra</array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>5</integer><key>Minute</key><integer>30</integer></dict>
  <key>Nice</key><integer>10</integer>
  <key>StandardOutPath</key><string>$HOME/.claude/logs/scratch_janitor.out</string>
  <key>StandardErrorPath</key><string>$HOME/.claude/logs/scratch_janitor.err</string>
</dict></plist>
PLIST

launchctl bootout "gui/$(id -u)/$label" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$plist"
echo "installed $label (apply=${APPLY:-0}; off: launchctl bootout gui/$(id -u)/$label)"
