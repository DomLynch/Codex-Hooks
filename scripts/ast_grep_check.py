#!/usr/bin/env python3
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    target = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    binary = shutil.which("sg") or shutil.which("ast-grep")
    if not binary:
        cmd = ["npx", "--yes", "-p", "@ast-grep/cli", "sg"]
    else:
        cmd = [binary]
    return subprocess.run([*cmd, "scan", "--config", str(ROOT / "sgconfig.yml"), str(target)]).returncode


if __name__ == "__main__":
    raise SystemExit(main())
