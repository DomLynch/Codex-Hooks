#!/usr/bin/env python3
"""Replay a deterministic gate against real Claude transcripts, turn by turn.

Usage: replay_gate.py <gate_name> [--days N] [--show K]

Splits every transcript into turns with the SAME rule the Stop hook uses
(is_turn_start), builds each turn's (role, body) entries with the SAME builder
(entries_from_lines), and calls the gate with that turn's own tool outputs and
final assistant text. This is what the hook would have seen. Use it before
shipping any new gate: a high fire rate here is a false-positive rate in prod.
"""
from __future__ import annotations

import glob
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
from lib import transcript as T  # noqa: E402
from lib.claude_adapter import entries_from_lines, is_turn_start, read_claude_transcript_lines  # noqa: E402


def turns(path: str):
    lines = read_claude_transcript_lines(path)
    starts = [i for i, e in enumerate(lines) if is_turn_start(e)]
    for a, b in zip(starts, starts[1:] + [len(lines)]):
        seg = lines[a:b]
        finals = [e for e in seg if e.get("type") == "assistant"
                  and any(isinstance(x, dict) and x.get("type") == "text" for x in ((e.get("message") or {}).get("content") or []))]
        if not finals:
            continue
        final = "\n".join(x.get("text", "") for x in (finals[-1]["message"]["content"]) if isinstance(x, dict) and x.get("type") == "text").strip()
        if not final or final.startswith("VERDICT:"):
            continue
        yield entries_from_lines(seg), final, (lines[a].get("cwd") or "")


def main() -> None:
    name = sys.argv[1]
    days = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 4
    show = int(sys.argv[sys.argv.index("--show") + 1]) if "--show" in sys.argv else 10
    gate = getattr(T, name)
    files = [f for f in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")) if time.time() - os.path.getmtime(f) < days * 86400]
    n = fired = 0
    hits: list[tuple[str, str, str]] = []
    for f in files:
        for entries, final, cwd in turns(f):
            n += 1
            try:
                r = gate(entries, final, cwd) if gate.__code__.co_argcount >= 3 else gate(entries, final)
            except TypeError:
                r = gate(entries, final)
            if r:
                fired += 1
                hits.append((os.path.basename(f)[:8], str(r)[:60], re.sub(r"\s+", " ", final)[:170]))
    print(f"gate={name}  transcripts={len(files)}  turns={n}  fires={fired}  ({100 * fired / max(n, 1):.1f}%)")
    for h in hits[:show]:
        print(f"  [{h[0]}] {h[1]!r}\n      {h[2]}")


if __name__ == "__main__":
    main()
