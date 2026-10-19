#!/usr/bin/env bash
# Install the volume mount check on the VPS (run AS ROOT ON THE VPS, after the PR merges; Lead 2026-10-19).
# Cron runs it every 5 minutes. It only reads /etc/fstab and /proc/self/mountinfo, writes one marker + one log line per
# incident, and never remounts or touches work/. Remove with: crontab -l | grep -v volume_mount_check | crontab -
set -euo pipefail

script="$(cd "$(dirname "$0")/.." && pwd)/claude/scripts/volume_mount_check.py"
install -m 755 "$script" /usr/local/sbin/volume_mount_check.py

line='*/5 * * * * /usr/bin/python3 /usr/local/sbin/volume_mount_check.py'
( crontab -l 2>/dev/null | grep -v volume_mount_check || true; echo "$line" ) | crontab -
/usr/bin/python3 /usr/local/sbin/volume_mount_check.py && echo "installed; all mounts present now" || echo "installed; ALERT raised, see /opt/frankendom-shadow/VOLUME-ALERT"
