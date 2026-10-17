"""Puts claude/hooks on sys.path so the guard tests import `lib` the way the installed hooks do."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
