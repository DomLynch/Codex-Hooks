#!/usr/bin/env python3
"""Read-only check, run from cron on the VPS: is the Hetzner volume mounted, and is every bind mount of it still there?

The 10 lane folders moved on 2026-10-19 (shadow/work and /srv/dev-jobs) are bind mounts of the volume with `nofail` in
/etc/fstab. If the volume does not come up at boot, those mounts fail silently and lanes see EMPTY folders. This check
reads /etc/fstab (the single source of truth for what must be mounted) and /proc/self/mountinfo, then:
  - something missing: writes the marker file ONCE (not inside any work/ folder) and appends one line to the log;
  - still missing on the next run: nothing new (alert once);
  - all back: removes the marker and logs the recovery.
It never remounts and never writes into the folders it watches. Exit 0 = all mounted, 1 = something missing."""

import argparse
import os
import sys
import time

VOLUME = "/mnt/HC_Volume_107195252"
MARKER = "/opt/frankendom-shadow/VOLUME-ALERT"
LOG = "/root/volume-mount-check.log"


def expected_binds(fstab, volume=VOLUME):
    """Targets of the fstab bind lines whose source lives on the volume."""
    targets = []
    with open(fstab) as f:
        for line in f:
            parts = line.split()
            if (
                len(parts) >= 4
                and parts[0].startswith(volume + "/")
                and "bind" in parts[3].split(",")
            ):
                targets.append(parts[1])
    return targets


def mounts(mountinfo):
    """{mountpoint: (maj:min, root)} from /proc/self/mountinfo (the last mount at a point wins)."""
    out = {}
    with open(mountinfo) as f:
        for line in f:
            p = line.split()
            if len(p) > 4:
                out[p[4]] = (p[2], p[3])
    return out


def problems(fstab, mountinfo, volume=VOLUME):
    m = mounts(mountinfo)
    if volume not in m:
        return ["volume not mounted: " + volume]
    dev = m[volume][0]
    bad = []
    for t in expected_binds(fstab, volume):
        if t not in m:
            bad.append("bind mount missing: " + t)
        elif m[t][0] != dev:
            bad.append("not on the volume: " + t)
    return bad


def stamp():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def run(fstab, mountinfo, marker, log, volume=VOLUME):
    bad = problems(fstab, mountinfo, volume)
    if bad:
        if not os.path.exists(marker):
            tmp = marker + ".tmp"
            with open(tmp, "w") as f:
                f.write("%s volume mounts broken:\n%s\n" % (stamp(), "\n".join(bad)))
            os.replace(tmp, marker)
            with open(log, "a") as f:
                f.write(
                    "%s ALERT %d problem(s): %s\n" % (stamp(), len(bad), "; ".join(bad))
                )
        return 1
    if os.path.exists(marker):
        os.remove(marker)
        with open(log, "a") as f:
            f.write("%s OK again, marker removed\n" % stamp())
    return 0


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fstab", default="/etc/fstab")
    ap.add_argument("--mountinfo", default="/proc/self/mountinfo")
    ap.add_argument("--marker", default=MARKER)
    ap.add_argument("--log", default=LOG)
    ap.add_argument("--volume", default=VOLUME)
    a = ap.parse_args(argv)
    return run(a.fstab, a.mountinfo, a.marker, a.log, a.volume)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
