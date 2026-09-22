#!/usr/bin/env python3
"""Turn a session transcript (.jsonl) into readable markdown: user and assistant text only.

A /clear never deletes ~/.claude/projects/<key>/<session>.jsonl, so the conversation is always recoverable - but
the raw file is mostly tool calls and results, and a day's session runs to tens of megabytes. This keeps the
human thread: local timestamps, user turns, assistant prose, and a one-line note where tool work was dropped.

  python3 lib/export_transcript.py <session.jsonl> [out.md]      # default: <session>.md beside the source
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text").strip()


def _has_tools(content) -> bool:
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") in ("tool_use", "tool_result") for b in content)


def export(src: Path, out: Path) -> tuple[int, int, int]:
    users = replies = dropped = 0
    lines: list[str] = [f"# Transcript — {src.stem}", ""]
    with src.open(errors="ignore") as fh:
        for raw in fh:
            if '"message"' not in raw:
                continue
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            message = entry.get("message") or {}
            role, content = message.get("role"), message.get("content")
            if role not in ("user", "assistant"):
                continue
            if _has_tools(content):
                dropped += 1
                continue
            body = _text(content)
            if not body.strip():
                continue
            stamp = (entry.get("timestamp") or "")[11:16]
            try:   # transcripts are UTC; show local so the times match what the user saw
                stamp = datetime.fromisoformat((entry.get("timestamp") or "").replace("Z", "+00:00")).astimezone().strftime("%H:%M")
            except ValueError:
                pass
            if role == "user":
                users += 1
                lines += [f"## {stamp} — user", "", body, ""]
            else:
                replies += 1
                lines += [f"**{stamp} — assistant**", "", body, ""]
    lines += ["", f"_{users} user turns, {replies} replies; {dropped} tool-only messages dropped._"]
    out.write_text("\n".join(lines))
    return users, replies, dropped


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    src = Path(sys.argv[1]).expanduser()
    if not src.is_file():
        raise SystemExit(f"not a file: {src}")
    out = Path(sys.argv[2]).expanduser() if len(sys.argv) > 2 else src.with_suffix(".md")
    users, replies, dropped = export(src, out)
    print(f"{out}: {users} user turns, {replies} replies, {dropped} tool-only messages dropped, "
          f"{out.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
