#!/usr/bin/env python3
"""Legacy no-op shim.

The old Stop hook is no longer registered. Running Codex sessions can keep
stale hook commands cached until restart, so this file remains as a harmless
compatibility target. It intentionally performs no work.
"""

from __future__ import annotations


def main() -> None:
    return None


if __name__ == "__main__":
    main()
