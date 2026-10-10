"""Dom, 2026-10-10 (via Strategy): the Stop-hook quality gate never runs while a release is in flight on the Mac."""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

HOOKS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS_DIR))

import stop_orchestrator as so  # noqa: E402
from lib import quality  # noqa: E402

RUNNING = ["bash scripts/deploy.sh --revision abc", "/bin/bash scripts/deploy.sh", "sh -x ./scripts/deploy.sh now",
           "/Users/d/Developer/frankendom-deploy/scripts/deploy.sh --full", "scripts/deploy.sh"]
# The Deploy lane's monitor loop carries the text inside bash -c / eval: it must never count as a release.
MENTIONS = ["/bin/zsh -c eval 'd=$(pgrep -f \"^bash scripts/deploy.sh\" >/dev/null && echo running)'",
            "vim scripts/deploy.sh", "tail -f deploy-abc.log scripts/deploy.sh", "rg deploy scripts/deploy.sh.bak",
            "python3 -m pytest tests/test_release_defer.py"]


def _ps(lines):
    out = "\n".join(lines) + "\n"
    return patch.object(quality.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=out))


def test_a_running_deploy_is_detected_and_a_mention_is_not():
    for line in RUNNING:
        with _ps(["/sbin/launchd", line]):
            found = quality.release_in_flight()
            assert found and "release in flight" in found["reason"], line
    for line in MENTIONS:
        with _ps(["/sbin/launchd", line]):
            assert quality.release_in_flight() is None, line


def test_the_gate_defers_while_a_release_runs_and_does_not_run_the_suite():
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        (cwd / ".quality-gate.json").write_text('{"commands": [["false"]]}')
        ran = []
        busy = {"reason": "release in flight (scripts/deploy.sh is running on this Mac)"}
        with patch.object(so, "session_lane", return_value="deploy"), patch.object(so, "ci_covered_commit", return_value=("", "")), \
                patch.object(so, "unchanged_from_base", return_value=None), patch.object(so, "active_deploy_lock", return_value=None), \
                patch.object(so, "gate_busy", return_value=None), patch.object(so, "deploy_hold", return_value=None), \
                patch.object(so, "release_in_flight", return_value=busy), \
                patch.object(so, "run_project_gate", side_effect=lambda *a, **k: ran.append(1)), \
                patch.object(so, "load_json_locked", return_value={}), patch.object(so, "update_json_locked"), \
                patch.object(so, "log") as log:
            assert so.run_aaa_gate(cwd, "", session_id="s1") == so.DEFERRED
        assert not ran
        assert any("deferred" in str(c) and "release in flight" in str(c) for c in log.call_args_list)
