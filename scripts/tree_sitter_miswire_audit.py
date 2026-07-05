#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path


BUSINESS = Path("/Users/domininclynch/Desktop/Business")
ALLOWLIST = {
    "hooks": BUSINESS / "Codex Hooks",
    "v3": BUSINESS / "Research Agent Bot",
    "v4": BUSINESS / "Research agent bot v4",
    "v5": BUSINESS / "V5 Memo",
    "v6": BUSINESS / "V6 Alpha Memo",
    "db": BUSINESS / "Researka Database",
    "web": BUSINESS / "researka-platform-main",
}


def resolve_target(value: str) -> Path:
    return ALLOWLIST.get(value, Path(value)).expanduser().resolve()


def build_cmd(target: Path) -> list[str]:
    uvx = shutil.which("uvx")
    if not uvx:
        raise SystemExit("missing uvx; install uv first")
    return [uvx, "--from", "tree-sitter-analyzer", "miswire-audit", str(target)]


def run_audit(cmd: list[str], target: Path) -> int:
    cache = target / ".ast-cache"
    cache_preexisted = cache.exists()
    try:
        return subprocess.run(cmd).returncode
    finally:
        if not cache_preexisted and cache.exists():
            shutil.rmtree(cache)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Tree-sitter Analyzer miswire audit as a Codegraph diagnostic.")
    parser.add_argument("target", nargs="?", default=".", help="Repo alias or path.")
    parser.add_argument("--dry-run", action="store_true", help="Print command without executing.")
    args = parser.parse_args()

    target = resolve_target(args.target)
    if not target.exists():
        raise SystemExit(f"missing target: {target}")
    cmd = build_cmd(target)
    if args.dry_run:
        print(" ".join(cmd))
        return 0
    return run_audit(cmd, target)


if __name__ == "__main__":
    raise SystemExit(main())
