"""The one-deployer rule on the Codex side, ported 2026-09-23.

The two things worth guarding against here are the two deliberate differences from the Claude copy: the lock is
SHARED with the Claude install (one deployer per Mac, not per agent), and the command is matched over the whole
serialized tool_input because Codex's `exec` wraps it in a JS string rather than passing it bare.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

HOOKS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS))
import deploy_guard  # noqa: E402


def lock(age_s: int = 120, pid: int | None = None) -> dict:
    started = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    return {"pid": pid or os.getpid(), "started": started.isoformat().replace("+00:00", "Z"),
            "revision": "abcdef1234", "age_s": age_s}


class LockSharingTests(unittest.TestCase):
    def test_lock_path_is_the_claude_one_not_a_codex_one(self) -> None:
        """One deployer per Mac. A separate ~/.codex lock would let Codex run a browser suite straight
        through a Claude lane's deploy, which is the contention this exists to prevent."""
        self.assertNotIn("/.codex/", str(deploy_guard.LOCK_PATH))
        self.assertTrue(str(deploy_guard.LOCK_PATH).endswith("deploy_in_flight.json"))

    def test_a_stale_or_dead_lock_never_wedges_anyone(self) -> None:
        with mock.patch.object(deploy_guard.Path, "read_text",
                               return_value=json.dumps({"pid": os.getpid(), "started": "2020-01-01T00:00:00Z"})):
            self.assertIsNone(deploy_guard.active_lock())
        dead = json.dumps({"pid": 2 ** 22, "started": datetime.now(timezone.utc).isoformat()})
        with mock.patch.object(deploy_guard.Path, "read_text", return_value=dead):
            self.assertIsNone(deploy_guard.active_lock())

    def test_no_lock_means_nothing_is_ever_refused(self) -> None:
        self.assertEqual(deploy_guard.decide("npx playwright test", None), "")


class HeavyCommandTests(unittest.TestCase):
    def test_heavy_commands_are_deferred_while_a_deploy_runs(self) -> None:
        for cmd in ("npx playwright test", "npm run build", "npm run quality:ci",
                    "node scripts/browser-check.mjs", "blender -b scene.blend", "vite build"):
            self.assertNotEqual(deploy_guard.decide(cmd, lock(), own_pid=None), "", cmd)

    def test_light_commands_pass_straight_through(self) -> None:
        for cmd in ("git status", "gh pr list", "grep -rn foo src/", "node --test tests/one.test.ts",
                    "cat README.md"):
            self.assertEqual(deploy_guard.decide(cmd, lock(), own_pid=None), "", cmd)

    def test_the_deploy_script_itself_is_never_blocked(self) -> None:
        self.assertEqual(deploy_guard.decide("bash scripts/deploy.sh", lock(), own_pid=None), "")

    def test_the_reason_names_the_revision_and_the_minutes_spent(self) -> None:
        reason = deploy_guard.decide("npm run build", lock(age_s=7 * 60), own_pid=None)
        self.assertIn("abcdef1", reason)
        self.assertIn("7 min ago", reason)


class CodexPayloadShapeTests(unittest.TestCase):
    def test_a_command_wrapped_in_codex_js_exec_is_still_matched(self) -> None:
        """Codex passes `tools.exec_command({cmd: "..."})`, not a bare string."""
        wrapped = 'const r = await tools.exec_command({cmd: "npm run build", workdir: "/x"});'
        self.assertNotEqual(deploy_guard.decide(deploy_guard.command_text(wrapped), lock(), own_pid=None), "")

    def test_command_text_handles_string_dict_and_argv_shapes(self) -> None:
        self.assertIn("npm run build", deploy_guard.command_text("npm run build"))
        self.assertIn("npm run build", deploy_guard.command_text({"command": "npm run build"}))
        self.assertIn("npm run build", deploy_guard.command_text({"cmd": ["bash", "-lc", "npm run build"]}))

    def test_main_ignores_tools_that_are_not_a_shell(self) -> None:
        out = _run_main({"tool_name": "Read", "tool_input": {"file_path": "npm run build"}})
        self.assertEqual(out, "")

    def test_main_denies_an_exec_call_during_a_deploy(self) -> None:
        with mock.patch.object(deploy_guard, "active_lock", return_value=lock(pid=2 ** 22)):
            out = _run_main({"tool_name": "exec", "tool_input": {"cmd": "npx playwright test"}})
        self.assertIn("permissionDecision", out)
        self.assertIn("deny", out)


def _run_main(payload: dict) -> str:
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), redirect_stdout(buf):
        deploy_guard.main()
    return buf.getvalue()


if __name__ == "__main__":
    unittest.main()
