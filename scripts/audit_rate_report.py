#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median


EVENT_RE = re.compile(r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] (?P<msg>.*)$")
DEFAULT_LOGS = (
    ("codex-stop", Path.home() / ".codex" / "logs" / "mimo_audit.log"),
    ("codex-checkpoint", Path.home() / ".codex" / "logs" / "post_tool.log"),
    ("claude-stop", Path.home() / ".claude" / "logs" / "mimo_audit.log"),
    ("claude-checkpoint", Path.home() / ".claude" / "logs" / "post_tool.log"),
)
DEFAULT_USAGE_LOGS = tuple(
    (agent, tuple(Path.home() / f".{agent}" / "logs" / name for name in
                  ("mimo_audit.log", "mimo_api.log", "minimax_api.log", "codex_audit.log")))
    for agent in ("codex", "claude")
)
EVENTS = ("PASS", "ISSUES", "SKIP_CHAT", "DAMPER", "ERROR", "TOO_SHORT")
USAGE_RE = re.compile(
    r"(?:MIMO|MINIMAX)_USAGE lane=(?P<lane>\S+) model=(?P<model>\S+) "
    r"input_tokens=(?P<input>\d+) output_tokens=(?P<output>\d+) "
    r"cache_read_tokens=(?P<cache_read>\d+) cache_write_tokens=(?P<cache_write>\d+) "
    r"elapsed_ms=(?P<elapsed>\d+)"
)
CODEX_USAGE_RE = re.compile(
    r"CODEX_AUDIT lane=(?P<lane>\S+) model=\S+ effort=\S+ elapsed_ms=(?P<elapsed>\d+)"
)


def classify(message: str) -> str | None:
    if "mimo checkpoint verdict=PASS" in message:
        return "PASS"
    if "mimo checkpoint verdict=ISSUES" in message:
        return "ISSUES"
    if "verdict" in message and "VERDICT: PASS" in message:
        return "PASS"
    if "verdict" in message and "VERDICT: ISSUES" in message:
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


def count_usage(lines: list[str], now: datetime, hours: int) -> tuple[Counter[str], list[int]]:
    cutoff = now - timedelta(hours=hours)
    counts: Counter[str] = Counter()
    latencies: list[int] = []
    for line in lines:
        event = EVENT_RE.match(line)
        usage = USAGE_RE.search(line) or CODEX_USAGE_RE.search(line)
        if not event or not usage:
            continue
        if not cutoff <= datetime.strptime(event.group("ts"), "%Y-%m-%d %H:%M:%S") <= now:
            continue
        counts["calls"] += 1
        counts[f"lane:{usage.group('lane')}"] += 1
        if usage.re is USAGE_RE:
            counts["token_calls"] += 1
            for key in ("input", "output", "cache_read", "cache_write"):
                counts[key] += int(usage.group(key))
        latencies.append(int(usage.group("elapsed")))
    return counts, latencies


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
            other_skips = counts["ERROR"] + counts["TOO_SHORT"]
            total_events = verdicts + counts["SKIP_CHAT"] + other_skips
            blocks = max(0, counts["ISSUES"] - counts["DAMPER"])
            summary = (
                f"total_events={total_events} verdicts={verdicts} "
                f"issue_rate={percent(counts['ISSUES'], verdicts)} "
                f"block_rate={percent(blocks, verdicts)} dampers={counts['DAMPER']} "
                f"chat_skips={counts['SKIP_CHAT']} other_skips={other_skips}"
            )
            values = " ".join(str(counts[event]) for event in EVENTS)
            print(f"{name} {label} {summary} {values}")


def print_usage_report(logs: tuple[tuple[str, tuple[Path, ...]], ...], now: datetime) -> None:
    for name, paths in logs:
        lines = [line for path in paths for line in read_lines(path)]
        for hours, label in ((24, "24h"), (24 * 7, "7d")):
            counts, latencies = count_usage(lines, now, hours)
            p50 = int(median(latencies)) if latencies else 0
            p95 = sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)] if latencies else 0
            tokens = {key: counts[key] if counts["token_calls"] == counts["calls"] and counts["calls"] else "unknown"
                      for key in ("input", "output", "cache_read", "cache_write")}
            print(
                f"{name} usage {label} completed_calls={counts['calls']} "
                f"stop_calls={counts['lane:stop']} checkpoint_calls={counts['lane:checkpoint']} "
                f"other_calls={counts['calls'] - counts['lane:stop'] - counts['lane:checkpoint']} "
                f"token_measured_calls={counts['token_calls']} input_tokens={tokens['input']} "
                f"output_tokens={tokens['output']} cache_read_tokens={tokens['cache_read']} "
                f"cache_write_tokens={tokens['cache_write']} latency_p50_ms={p50} latency_p95_ms={p95}"
            )


def parse_log_arg(value: str) -> tuple[str, Path]:
    if "=" in value:
        name, path = value.split("=", 1)
        return name, Path(path).expanduser()
    path = Path(value).expanduser()
    return path.stem, path


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize audit hook verdict rates.")
    parser.add_argument(
        "--log",
        action="append",
        default=[],
        help="Optional name=path log input. Defaults to Codex and Claude logs.",
    )
    args = parser.parse_args()
    logs = [parse_log_arg(item) for item in args.log] if args.log else list(DEFAULT_LOGS)
    print_report(logs, datetime.now())
    if not args.log:
        print_usage_report(DEFAULT_USAGE_LOGS, datetime.now())


if __name__ == "__main__":
    main()
