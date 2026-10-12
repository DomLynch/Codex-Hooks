#!/usr/bin/env python3
"""Append one load row per run to ~/.claude/state/mac-load-log.csv (the Mac-tier gate reads it) and the VPS load to
vps-load-log.csv, same columns. Run by launchd every 300 s (scripts/install_mac_load_sampler.sh), outside any session."""

import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

HEADER = "time_dubai,load1,load5,load15\n"
VPS_CMD = [
    "ssh",
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=8",
    "-i",
    os.path.expanduser("~/.ssh/binance_futures_tool"),
    "root@49.12.7.18",
    "uptime",
]


def parse_uptime(text):
    m = re.search(r"load averages?:\s*([\d.]+)[, ]+([\d.]+)[, ]+([\d.]+)", text)
    return m.groups() if m else None


def append_row(path, loads, now=None):
    now = (now or datetime.now(timezone.utc)).astimezone(timezone(timedelta(hours=4)))
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a") as f:
        if new:
            f.write(HEADER)
        f.write("%s,%s,%s,%s\n" % (now.strftime("%Y-%m-%dT%H:%M"), *loads))


def sample(cmd, timeout):
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_uptime(out)


def main(state=os.path.expanduser("~/.claude/state")):
    os.makedirs(state, exist_ok=True)
    mac = sample(["uptime"], 10)
    if mac:
        append_row(os.path.join(state, "mac-load-log.csv"), mac)
    vps = sample(VPS_CMD, 20)
    if vps:
        append_row(os.path.join(state, "vps-load-log.csv"), vps)
    return 0 if mac else 1


if __name__ == "__main__":
    sys.exit(main())
