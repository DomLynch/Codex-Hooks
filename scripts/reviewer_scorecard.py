#!/usr/bin/env python3
"""Reviewer scorecard by configuration window.

Answers "is the current reviewer model/effort still the right one?" by scoring
each config window on the numbers the decision is actually made on: how often
the reviewer blocked, how often the damper had to fire, latency, and whether
the lanes accepted the findings.

Columns:
  reviews     Stop-hook verdicts rendered in the window
  mblock/rate verdicts the MODEL returned as ISSUES (each one costs a lane turn). This is the
              number the model/effort A/B is judged on.
  gate        turns blocked by a DETERMINISTIC_GATE finding, where the model was never called.
              Excluded from mblock: they say nothing about the reviewer model.
  damper/100  BLOCK_CHAIN_DAMPER firings per 100 reviews
  timeouts    review calls that timed out
  void        findings dropped by the void/anchor filters
  median/p90  wall-clock latency of the review call
  accept      lane acceptance, from `[Fn] fixed:` / `[Fn] rejected:` ledger
              lines in the lanes' own transcripts

Windows are local Mac time (the hook logs are local). Lane transcripts are
UTC, so window bounds are shifted by --utc-offset before matching them.

    python3 scripts/reviewer_scorecard.py
    python3 scripts/reviewer_scorecard.py --window "opus low:2026-09-22 15:55:2026-09-23 00:00"
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

ELAPSED_RE = re.compile(r"elapsed_ms=(\d+)")
TIMESTAMP_RE = re.compile(r'"timestamp":"([0-9T:-]{16})')
FIXED_RE = re.compile(r"\[F\d+\] fixed:")
REJECTED_RE = re.compile(r"\[F\d+\] rejected:")

# The A/B windows recorded so far. Each is (label, start, end) in local time.
DEFAULT_WINDOWS = [
    ("sonnet HIGH", "2026-09-21 09:00", "2026-09-21 23:45"),
    ("sonnet MEDIUM, no filter", "2026-09-21 23:45", "2026-09-22 09:41"),
    ("sonnet MEDIUM + filters", "2026-09-22 09:41", "2026-09-22 11:28"),
    ("OPUS 5 LOW + filters", "2026-09-22 11:28", "2026-09-22 15:33"),
    ("HAIKU 4.5 XHIGH + filters", "2026-09-22 15:33", "2026-09-22 15:49"),
    ("HAIKU 4.5 HIGH + filters", "2026-09-22 15:49", "2026-09-22 15:55"),
    ("OPUS 5 LOW (restored)", "2026-09-22 15:55", "2026-09-29 00:00"),
]


def parse_window(spec: str) -> tuple[str, str, str]:
    """Parse 'label:YYYY-MM-DD HH:MM:YYYY-MM-DD HH:MM' into its three parts."""
    label, _, rest = spec.partition(":")
    start, _, end = rest.rpartition(":")
    # rpartition split inside the end timestamp's HH:MM, so stitch it back.
    start, _, end_hour = start.rpartition(":")
    if not label or not start or not end_hour:
        raise argparse.ArgumentTypeError(f"bad --window {spec!r}; want 'label:START:END'")
    return label.strip(), start.strip(), f"{end_hour.strip()}:{end}"


def read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(errors="ignore").splitlines()


def in_window(lines: list[str], lo: str, hi: str) -> list[str]:
    """Hook logs start each line with '[YYYY-MM-DD HH:MM:SS]'."""
    return [line for line in lines if lo <= line[1:17] < hi]


def shift(stamp: str, hours: int) -> str:
    """Local 'YYYY-MM-DD HH:MM' -> UTC 'YYYY-MM-DDTHH:MM' for transcript matching."""
    return (datetime.strptime(stamp, "%Y-%m-%d %H:%M") - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M")


def acceptance(lo_utc: str, hi_utc: str, projects: Path, max_age_days: int) -> tuple[int, int]:
    """Count lane ledger verdicts on reviewer findings inside the window."""
    fixed = rejected = 0
    cutoff = time.time() - max_age_days * 86400
    for path in glob.glob(str(projects / "*" / "*.jsonl")):
        if "claude-audit-" in path or os.path.getmtime(path) < cutoff:
            continue
        with open(path, errors="ignore") as handle:
            for line in handle:
                if '"type":"assistant"' not in line:
                    continue
                if "] fixed:" not in line and "] rejected:" not in line:
                    continue
                match = TIMESTAMP_RE.search(line)
                if match and lo_utc <= match.group(1) < hi_utc:
                    fixed += len(FIXED_RE.findall(line))
                    rejected += len(REJECTED_RE.findall(line))
    return fixed, rejected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--logs", type=Path, default=Path.home() / ".claude" / "logs")
    parser.add_argument("--projects", type=Path, default=Path.home() / ".claude" / "projects")
    parser.add_argument("--utc-offset", type=int, default=4, help="hours the Mac clock is ahead of UTC (default 4)")
    parser.add_argument("--max-age-days", type=int, default=3, help="ignore transcripts older than this (default 3)")
    parser.add_argument("--window", action="append", type=parse_window, metavar="LABEL:START:END",
                        help="replace the built-in windows; repeatable")
    args = parser.parse_args()

    windows = args.window or DEFAULT_WINDOWS
    audit = read_lines(args.logs / "mimo_audit.log")
    calls = read_lines(args.logs / "claude_audit.log")
    if not audit:
        print(f"no reviewer log at {args.logs / 'mimo_audit.log'}", flush=True)
        return 1

    print(f"{'window':26s} reviews mblock rate  gate damper/100 timeouts void  median  p90   accept")
    for label, lo, hi in windows:
        a = in_window(audit, lo, hi)
        c = in_window(calls, lo, hi)
        reviews = sum("verdict (iter" in line for line in a)
        blocks = sum("VERDICT: ISSUES" in line for line in a)
        # A DETERMINISTIC_GATE finding blocks the turn WITHOUT calling the model, but still writes a
        # "Claude <model> verdict ...: VERDICT: ISSUES" line. Counting those as model blocks makes the
        # reviewer look worse the more the deterministic rules fire, which is how the 2026-09-22 evening
        # window read as "Opus 5 low regressed to 20% blocked" when the model had blocked nothing at all.
        det = 0
        pending_gate = False
        for line in a:
            if "DETERMINISTIC_GATE" in line:
                pending_gate = True
            elif "VERDICT: ISSUES" in line:
                det += pending_gate
                pending_gate = False
            elif "VERDICT: PASS" in line:
                pending_gate = False
        model_blocks = blocks - det
        damper = sum("BLOCK_CHAIN_DAMPER" in line for line in a)
        timeouts = sum("timed out" in line for line in a)
        void = sum("VOID_FINDING" in line for line in a)

        elapsed = sorted(int(m.group(1)) for line in c for m in [ELAPSED_RE.search(line)] if m and int(m.group(1)) > 0)
        median = elapsed[len(elapsed) // 2] / 1000 if elapsed else 0
        p90 = elapsed[int(len(elapsed) * 0.9)] / 1000 if elapsed else 0

        fixed, rejected = acceptance(shift(lo, args.utc_offset), shift(hi, args.utc_offset),
                                     args.projects, args.max_age_days)
        answered = fixed + rejected
        accept = f"{fixed / answered * 100:3.0f}% ({answered})" if answered else "  n/a"

        print(f"{label:26s} {reviews:7d} {model_blocks:6d} {model_blocks / reviews * 100 if reviews else 0:4.0f}% "
              f"{det:5d} {damper / reviews * 100 if reviews else 0:9.1f} {timeouts:8d} {void:5d} "
              f"{median:6.1f}s {p90:5.1f}s  {accept}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
