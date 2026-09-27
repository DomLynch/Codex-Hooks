"""live_hooks_guard: warn (never block) a lane changing ~/.claude/hooks directly; quiet on reads and for the hooks lane.
Cases are the shapes of the Auditer's real 2026-09-26 edits (live-only until #26/#27) and real read-only commands."""
from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import live_hooks_guard as g  # noqa: E402

WRITES = [
    "cd ~/.claude/hooks && python3 - <<'EOF'\nfrom pathlib import Path\nh = Path(\"deploy_guard.py\"); s = h.read_text()\nh.write_text(s.replace(a, b))\nEOF",
    "cp /tmp/deploy_guard.py ~/.claude/hooks/deploy_guard.py",
    "git show origin/main:claude/hooks/x.py > ~/.claude/hooks/x.py",
    "sed -i '' 's/a/b/' ~/.claude/hooks/lib/api.py",
]
READS = [
    "cd ~/.claude/hooks && python3 -m pytest -q tests/test_deploy_guard.py 2>&1 | tail -2",
    "cd ~/.claude/hooks && python3 - <<'EOF'\nimport unittest\nr = unittest.TextTestRunner(stream=open(\"/dev/null\",\"w\"))\nEOF",
    "grep -n HEAVY_RE ~/.claude/hooks/deploy_guard.py > /tmp/out.txt",
    "cp ~/.claude/hooks/deploy_guard.py claude/hooks/deploy_guard.py",
    "sed -n 74,95p ~/.claude/hooks/deploy_guard.py 2>/dev/null",
    "python3 -c \"import sys; sys.path.insert(0, '/Users/x/.claude/hooks'); import deploy_guard\"",
]


def test_flags_real_write_shapes_and_passes_reads():
    for cmd in WRITES:
        assert g.live_write("Bash", {"command": cmd}), cmd
    for cmd in READS:
        assert g.live_write("Bash", {"command": cmd}) == "", cmd
    assert g.live_write("Edit", {"file_path": "/Users/x/.claude/hooks/post_tool.py"})
    assert g.live_write("Write", {"file_path": "/Users/x/Developer/frankendom-web/src/app.ts"}) == ""


def _run(payload: dict) -> str:
    buf = io.StringIO()
    with mock.patch.object(g.sys, "stdin", io.StringIO(json.dumps(payload))), mock.patch.object(g, "log"), redirect_stdout(buf):
        g.main()
    return buf.getvalue()


def test_main_warns_without_blocking_and_skips_the_hooks_lane():
    lane = {"tool_name": "Bash", "cwd": "/Users/x/Desktop/Business/frankendom/.claude/worktrees/priceless-bohr",
            "tool_input": {"command": WRITES[0]}}
    out = json.loads(_run(lane))["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse" and "permissionDecision" not in out
    assert "DomLynch/Codex-Hooks" in out["additionalContext"] and "undone by the next install" in out["additionalContext"]
    hooks_lane = dict(lane, cwd="/Users/x/Desktop/Business/Codex Hooks/.claude/worktrees/cool-goldberg")
    assert _run(hooks_lane) == ""
    assert _run(dict(lane, tool_input={"command": READS[0]})) == ""
