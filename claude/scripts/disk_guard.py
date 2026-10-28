#!/usr/bin/env python3
"""Report-only disk guard (Dom + Strategy, 2026-10-26): read how full the Mac, the SanDisk, the VPS root, the VPS volume and the 5TB box are.
Runs every 2 hours since 2026-10-28.

At 85% or more it appends ONE line per target per day to the daily note, naming the biggest folders and the lane that owns each (a du scan, read-only), pops a
Mac notification, and exits 1. It never deletes, moves or mounts anything: what to do about a full disk is
the owner's call (idle over 24 h: verified copy to the box, then remove, with the owner's answer). The VPS is read over ssh (public address first, then the
tailscale one); a target that cannot be read is reported as UNREADABLE, never as fine. Exit 0 = every target readable and under the limit."""

import argparse
import datetime
import os
import re
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


# 2026-10-28 (Dom + Strategy, after the VPS root hit 100% and the Mac 99% twice in a day): a FULL target now names the folders that filled it and the lane that owns
# each, the guard runs every 2 hours (launchd StartInterval), and it pops a Mac notification. Owner rules, first match wins. Deploy before the generic Armagedom rule
# because Deploy's worktree folders also contain "armagedom".
OWNERS = (
    (r"armagedom-preview|/opt/armagedom|arma-deploy|dev-jobs/deploy", "Server & Deploy"),
    (r"dev-jobs/art-|/worktrees/art-|worktrees-gracious", "Armagedom Art"),
    (r"frankendom-v3|dev-jobs/v3", "Frankendom v3 Dev"),
    (r"/Developer/physics|-physics|dev-jobs/physics-|dev-jobs/pd-|/root/pd-", "Physics Dev"),
    (r"opentrident|ue-keys|private-ref-|/work$", "OpenTrident"),
    (r"parts-lab|make-human", "Make Human Dev"),
    (r"armagedom|hub-npcs|/root/arma-", "Armagedom Lead"),
)
# What to measure when a target is FULL: (command words, base paths to skip) per label; "ssh" labels run over the VPS key, the rest locally.
SCANS = {
    "Mac": ("local", [
        (["du", "-xm", "-d1", "/private/tmp/claude-501"], ("/private/tmp/claude-501",)),
        (["du", "-xm", "-d1", os.path.expanduser("~/Developer/armagedom/.claude/worktrees")], (os.path.expanduser("~/Developer/armagedom/.claude/worktrees"),)),
        (["du", "-xm", "-d1", os.path.expanduser("~/Developer")], (os.path.expanduser("~/Developer"),)),
    ]),
    "SanDisk": ("local", [(["du", "-xm", "-d1", "/Volumes/SANDISK256"], ("/Volumes/SANDISK256",))]),
    "VPS root": ("ssh", [("du -xm -d2 /srv/dev-jobs; du -xm -d1 /root /opt /home/lanes", ("/srv/dev-jobs", "/root", "/opt", "/home/lanes"))]),
    "VPS volume": ("ssh", [("du -xm -d1 /mnt/HC_Volume_107195252", ("/mnt/HC_Volume_107195252",))]),
}


def owner_for(path):
    """The lane that owns a folder, by the first matching rule; 'unknown owner' when none does (never guess a lane)."""
    for pattern, lane in OWNERS:
        if re.search(pattern, path):
            return lane
    return "unknown owner"


def parse_du(text, bases=(), min_mb=1000):
    """[(MB, path)] biggest first from `du -xm` output, without the base dirs themselves and without entries under min_mb."""
    rows = []
    for line in text.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2 and parts[0].strip().isdigit():
            mb, path = int(parts[0]), parts[1].strip()
            if path not in bases and mb >= min_mb:
                rows.append((mb, path))
    return sorted(rows, reverse=True)


def offender_text(top, n=3):
    """'9 GB /srv/dev-jobs/v3 (Frankendom v3 Dev); ...' for the n biggest folders."""
    return "; ".join("%d GB %s (%s)" % (round(mb / 1024), path, owner_for(path)) for mb, path in top[:n])


def evaluate(readings, limit=LIMIT, offenders=None):
    """readings: [(label, (size, used, avail, pct) or None)]. Returns [(label, state, text)] where state is OK, FULL or UNREADABLE.
    offenders: {label: [(MB, path)]} appended to a FULL line so the note names the folders and the lanes to ask."""
    offenders = offenders or {}
    rows = []
    for label, r in readings:
        if r is None:
            rows.append((label, "UNREADABLE", label + ": could not be read"))
            continue
        free_gb = r[2] // (1024 * 1024)
        text = "%s: %d%% used, %d GB free" % (label, r[3], free_gb)
        full = r[3] >= limit
        if full and offenders.get(label):
            text += " | biggest: " + offender_text(offenders[label])
        rows.append((label, "FULL" if full else "OK", text))
    return rows


def new_lines(rows, existing, day):
    """The note lines to add: ONE per not-OK target per day, even when the percentage moved (a 2-hourly run must not spam the note).
    An old-format line for the same day, state and target counts as already written."""
    add = []
    for label, state, text in rows:
        if state == "OK":
            continue
        key = "%s disk-guard %s %s:" % (day, state, label)
        if any(e.startswith(key) for e in existing):
            continue
        add.append("%s disk-guard %s %s" % (day, state, text))
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


def scan_offenders(label):
    """The biggest folders on a FULL target, read-only (du only). A scan that fails returns [] and the plain FULL line still gets written."""
    kind, jobs = SCANS.get(label, (None, []))
    found = []
    for cmd, bases in jobs:
        try:
            if kind == "local":
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=280).stdout
            else:
                out = ""
                for host in HOSTS:
                    r = subprocess.run(
                        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-i", KEY, "root@" + host, cmd],
                        capture_output=True, text=True, timeout=290,
                    )
                    if r.stdout:
                        out = r.stdout
                        break
        except (OSError, subprocess.SubprocessError):
            continue
        found += parse_du(out, bases)
    return sorted(set(found), reverse=True)


def notify(lines):
    """One Mac notification for the new lines, so Dom sees a full disk without opening the note. Failure to notify never fails the guard."""
    text = lines[0][:200].replace('"', "'").replace("\\", "")
    if len(lines) > 1:
        text += " (+%d more)" % (len(lines) - 1)
    try:
        subprocess.run(["osascript", "-e", 'display notification "%s" with title "Disk guard"' % text], timeout=10, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pass


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
    day = datetime.date.today().isoformat()
    try:
        existing = open(a.note).read().splitlines()
    except OSError:
        existing = []
    if new_lines(rows, existing, day):  # only measure folders when a line is actually due (du over the VPS is slow)
        offenders = {label: scan_offenders(label) for label, state, _ in rows if state == "FULL"}
        rows = evaluate(readings, a.limit, offenders)
    for _, state, text in rows:
        print("%-10s %s" % (state, text))
    add = new_lines(rows, existing, day)
    if add and not a.dry_run:
        os.makedirs(os.path.dirname(a.note), exist_ok=True)
        with open(a.note, "a") as f:
            f.write("\n".join(add) + "\n")
        notify(add)
    return 1 if any(s != "OK" for _, s, _ in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
