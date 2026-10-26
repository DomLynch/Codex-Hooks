#!/usr/bin/env python3
"""Report-only disk guard (Dom + Strategy, 2026-10-26): once a day, read how full the Mac, the SanDisk, the VPS root, the VPS volume and the 5TB box are.

At 85% or more it appends ONE line per target per day to the daily note and exits 1. It never deletes, moves or mounts anything: what to do about a full disk is
the owner's call (idle over 24 h: verified copy to the box, then remove, with the owner's answer). The VPS is read over ssh (public address first, then the
tailscale one); a target that cannot be read is reported as UNREADABLE, never as fine. Exit 0 = every target readable and under the limit."""

import argparse
import datetime
import os
import subprocess
import sys

LIMIT = 85
KEY = os.path.expanduser("~/.ssh/binance_futures_tool")
HOSTS = ("49.12.7.18", "100.96.74.1")
LOCAL = (("Mac", "/System/Volumes/Data"), ("SanDisk", "/Volumes/SANDISK256"))
REMOTE = (
    ("VPS root", "/"),
    ("VPS volume", "/mnt/HC_Volume_107195252"),
    ("5TB box", "/mnt/frankendom-donors"),
)
NOTE = os.path.expanduser("~/.claude/state/disk-guard-daily.md")


def parse_df(text):
    """{mountpoint: (size_kb, used_kb, avail_kb, pct)} from `df -Pk` output. pct is computed from used/(used+avail), the way df -h shows it."""
    out = {}
    for line in text.splitlines()[1:]:
        p = line.split()
        if len(p) >= 6 and p[1].isdigit() and p[2].isdigit() and p[3].isdigit():
            used, avail = int(p[2]), int(p[3])
            pct = 100 * used // (used + avail) if used + avail else 0
            out[p[5]] = (int(p[1]), used, avail, pct)
    return out


def evaluate(readings, limit=LIMIT):
    """readings: [(label, (size, used, avail, pct) or None)]. Returns [(label, state, text)] where state is OK, FULL or UNREADABLE."""
    rows = []
    for label, r in readings:
        if r is None:
            rows.append((label, "UNREADABLE", label + ": could not be read"))
            continue
        free_gb = r[2] // (1024 * 1024)
        text = "%s: %d%% used, %d GB free" % (label, r[3], free_gb)
        rows.append((label, "FULL" if r[3] >= limit else "OK", text))
    return rows


def new_lines(rows, existing, day):
    """The note lines to add: one per not-OK target per day (a second run the same day adds nothing)."""
    add = []
    for label, state, text in rows:
        if state == "OK":
            continue
        line = "%s disk-guard %s %s" % (day, state, text)
        if line not in existing:
            add.append(line)
    return add


def local_df(path):
    try:
        out = subprocess.run(["df", "-Pk", path], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_df(out).get(path) or next(iter(parse_df(out).values()), None)


def remote_df(paths):
    cmd = "df -Pk " + " ".join(paths)
    for host in HOSTS:
        try:
            r = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-i", KEY, "root@" + host, cmd],
                capture_output=True, text=True, timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0 and r.stdout:
            return parse_df(r.stdout)
    return {}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--note", default=NOTE, help="file that gets the daily line (Dom's daily note path)")
    ap.add_argument("--limit", type=int, default=LIMIT)
    ap.add_argument("--dry-run", action="store_true", help="print only, write nothing")
    a = ap.parse_args(argv)
    readings = [(label, local_df(path)) for label, path in LOCAL]
    remote = remote_df([p for _, p in REMOTE])
    readings += [(label, remote.get(path)) for label, path in REMOTE]
    rows = evaluate(readings, a.limit)
    for _, state, text in rows:
        print("%-10s %s" % (state, text))
    day = datetime.date.today().isoformat()
    try:
        existing = open(a.note).read().splitlines()
    except OSError:
        existing = []
    add = new_lines(rows, existing, day)
    if add and not a.dry_run:
        os.makedirs(os.path.dirname(a.note), exist_ok=True)
        with open(a.note, "a") as f:
            f.write("\n".join(add) + "\n")
    return 1 if any(s != "OK" for _, s, _ in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
