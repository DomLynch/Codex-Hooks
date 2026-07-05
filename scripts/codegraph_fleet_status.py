#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


BUSINESS = Path("/Users/domininclynch/Desktop/Business")
DEFAULT_REPOS = [
    "Codex Hooks",
    "Research Agent Bot",
    "Research agent bot v4",
    "V5 Memo",
    "V6 Alpha Memo",
    "Researka Database",
    "researka-platform-main",
]
ALIASES = {
    "hooks": "Codex Hooks",
    "v3": "Research Agent Bot",
    "v4": "Research agent bot v4",
    "v5": "V5 Memo",
    "v6": "V6 Alpha Memo",
    "db": "Researka Database",
    "web": "researka-platform-main",
}


def status(path: Path, sync: bool) -> str:
    if not path.exists():
        return "missing path"
    if not (path / ".git").exists():
        return "not a git repo"
    if not (path / ".codegraph" / "codegraph.db").exists():
        return "NO INDEX: run `codegraph init -i`"
    if sync:
        subprocess.run(["codegraph", "sync", str(path)], text=True, capture_output=True, timeout=30)
    proc = subprocess.run(["codegraph", "status", str(path)], text=True, capture_output=True, timeout=30)
    if proc.returncode != 0:
        return "status failed: " + (proc.stderr or proc.stdout).strip().splitlines()[0]
    lines = [line.strip().replace("\x1b[0m", "") for line in proc.stdout.splitlines()]
    keep = [
        line
        for line in lines
        if line.startswith(("**Files indexed:", "**Total nodes:", "**Total edges:", "Files:", "Nodes:", "Edges:"))
    ]
    return "; ".join(keep) if keep else "indexed"


def main() -> int:
    parser = argparse.ArgumentParser(description="Show Codegraph coverage for key local repos.")
    parser.add_argument("--sync", action="store_true", help="Run codegraph sync before status.")
    parser.add_argument("repos", nargs="*", help="Repo folder names or absolute paths.")
    args = parser.parse_args()
    repos = args.repos or DEFAULT_REPOS
    for repo in repos:
        repo = ALIASES.get(repo, repo)
        path = Path(repo).expanduser()
        if not path.is_absolute():
            path = BUSINESS / repo
        print(f"{path.name}: {status(path, args.sync)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
