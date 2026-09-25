#!/usr/bin/env python3
"""Context size of every live Claude session, against the limit the context hook applies to it.

Reads each session's latest transcript under ~/.claude/projects and names it by its desktop title, so the
table matches the sidebar. The limits come from the installed hook, so the table and the hook never disagree.

    python3 scripts/context_table.py            # sessions active in the last 12 hours
    python3 scripts/context_table.py --hours 48
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
import time

sys.path.insert(0, os.path.expanduser("~/.claude/hooks"))
from lib.context_size import context_tokens, session_lane, session_title, warn_tokens  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=float, default=12)
    args = parser.parse_args()
    cutoff = time.time() - args.hours * 3600
    latest: dict[str, str] = {}
    for path in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")):
        if os.path.getmtime(path) < cutoff:
            continue
        folder = os.path.dirname(path)
        if folder not in latest or os.path.getmtime(path) > os.path.getmtime(latest[folder]):
            latest[folder] = path
    # A lane that moved folders leaves its old session behind; the newest session with a title is the live one.
    by_title: dict[str, tuple[float, str, str]] = {}
    for folder, path in latest.items():
        title = session_title(path) or os.path.basename(folder)[-40:]
        if title not in by_title or os.path.getmtime(path) > by_title[title][0]:
            by_title[title] = (os.path.getmtime(path), folder, path)
    rows = []
    for title, (_, folder, path) in by_title.items():
        tokens = context_tokens(path)
        if not tokens:
            continue
        limit = warn_tokens(session_lane(path, "/" + os.path.basename(folder).replace("-", "/")))
        action = "CLEAR" if tokens >= limit else ("clear soon" if tokens >= limit - 50_000 else "ok")
        rows.append((tokens, title, limit, action))
    print("| Session | Context | Limit | Action |\n|---|---|---|---|")
    for tokens, title, limit, action in sorted(rows, reverse=True):
        print(f"| {title} | {tokens // 1000}k | {limit // 1000}k | {action} |")


if __name__ == "__main__":
    main()
