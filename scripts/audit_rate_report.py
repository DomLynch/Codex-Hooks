#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path


EVENT_RE = re.compile(r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] (?P<msg>.*)$")
DEFAULT_LOGS = (
    ("codex", Path.home() / ".codex" / "logs" / "mimo_audit.log"),
    ("claude", Path.home() / ".claude" / "logs" / "mimo_audit.log"),
)
EVENTS = ("PASS", "ISSUES", "SKIP_CHAT", "DAMPER", "ERROR", "TOO_SHORT")


def classify(message: str) -> str | None:
    if "MiniMax verdict" in message and "VERDICT: PASS" in message:
        return "PASS"
    if "MiniMax verdict" in message and "VERDICT: ISSUES" in message:
        return "ISSUES"
    if "ALLOW: chat turn - audit skipped" in message:
        return "SKIP_CHAT"
    if "BLOCK_CHAIN_DAMPER" in message:
        return "DAMPER"
    if "audit unavailable" in message or "RETRY_NETWORK_EXHAUSTED" in message:
        return "ERROR"
    if "transcript too short" in message:
        return "TOO_SHORT"
    return None


def count_events(lines: list[str], now: datetime, hours: int) -> Counter[str]:
    cutoff = now - timedelta(hours=hours)
    counts: Counter[str] = Counter()
    for line in lines:
        match = EVENT_RE.match(line)
        if not match:
            continue
        ts = datetime.strptime(match.group("ts"), "%Y-%m-%d %H:%M:%S")
        if ts < cutoff:
            continue
        event = classify(match.group("msg"))
        if event:
            counts[event] += 1
    return counts


def percent(part: int, total: int) -> str:
    if total == 0:
        return "-"
    return f"{(part / total) * 100:.0f}%"


def read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(errors="replace").splitlines()


def print_report(logs: list[tuple[str, Path]], now: datetime) -> None:
    print(f"now={now:%Y-%m-%d %H:%M:%S}")
    print("agent window summary " + " ".join(EVENTS))
    for name, path in logs:
        lines = read_lines(path)
        for hours, label in ((24, "24h"), (24 * 7, "7d")):
            counts = count_events(lines, now, hours)
            verdicts = counts["PASS"] + counts["ISSUES"]
            other_skips = counts["DAMPER"] + counts["ERROR"] + counts["TOO_SHORT"]
            total_events = verdicts + counts["SKIP_CHAT"] + other_skips
            summary = (
                f"total_events={total_events} verdicts={verdicts} "
                f"issue_rate={percent(counts['ISSUES'], verdicts)} "
                f"chat_skips={counts['SKIP_CHAT']} other_skips={other_skips}"
            )
            values = " ".join(str(counts[event]) for event in EVENTS)
            print(f"{name} {label} {summary} {values}")


def parse_log_arg(value: str) -> tuple[str, Path]:
    if "=" in value:
        name, path = value.split("=", 1)
        return name, Path(path).expanduser()
    path = Path(value).expanduser()
    return path.stem, path


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize MiniMax audit hook verdict rates.")
    parser.add_argument(
        "--log",
        action="append",
        default=[],
        help="Optional name=path log input. Defaults to Codex and Claude logs.",
    )
    args = parser.parse_args()
    logs = [parse_log_arg(item) for item in args.log] if args.log else list(DEFAULT_LOGS)
    print_report(logs, datetime.now())


if __name__ == "__main__":
    main()
