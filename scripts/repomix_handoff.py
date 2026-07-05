#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


BUSINESS = Path("/Users/domininclynch/Desktop/Business")
OUT_DIR = Path("/tmp/researka-handoffs")
ALLOWLIST = {
    "hooks": BUSINESS / "Codex Hooks",
    "v3": BUSINESS / "Research Agent Bot",
    "v4": BUSINESS / "Research agent bot v4",
    "v5": BUSINESS / "V5 Memo",
    "v6": BUSINESS / "V6 Alpha Memo",
    "db": BUSINESS / "Researka Database",
    "web": BUSINESS / "researka-platform-main",
}
IGNORE = ",".join([
    ".git/**",
    ".codegraph/**",
    "node_modules/**",
    ".venv/**",
    "__pycache__/**",
    ".pytest_cache/**",
    ".ruff_cache/**",
    "logs/**",
    "runs/**",
    "dist/**",
    "build/**",
    "*.jsonl",
    "*.sqlite",
    "*.db",
])


def main() -> int:
    parser = argparse.ArgumentParser(description="Create an allowlisted Repomix handoff pack.")
    parser.add_argument("repo", choices=sorted(ALLOWLIST))
    parser.add_argument("--budget", type=int, default=120_000)
    parser.add_argument("--style", choices=["markdown", "xml", "json", "plain"], default="markdown")
    parser.add_argument("--full", action="store_true", help="Do not use Repomix structural compression.")
    args = parser.parse_args()

    src = ALLOWLIST[args.repo]
    if not src.exists():
        raise SystemExit(f"missing repo: {src}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{args.repo}-handoff.{args.style if args.style != 'markdown' else 'md'}"
    cmd = [
        "npx",
        "--yes",
        "repomix",
        str(src),
        "--quiet",
        "--style",
        args.style,
        "--output",
        str(out),
        "--ignore",
        IGNORE,
        "--token-budget",
        str(args.budget),
        "--truncate-base64",
    ]
    if not args.full:
        cmd.append("--compress")
    subprocess.run(cmd, check=True)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
