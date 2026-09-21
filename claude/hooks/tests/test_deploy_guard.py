import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deploy_guard  # noqa: E402


def lock(age_s=120, pid=None):
    started = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"revision": "abc1234def", "started": started, "pid": pid if pid is not None else os.getpid(), "cwd": "/x"}


class DeployGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "lock.json"
        patcher = mock.patch.object(deploy_guard, "LOCK_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_no_lock_allows_everything(self):
        self.assertIsNone(deploy_guard.active_lock())
        self.assertEqual(deploy_guard.decide("npx playwright test", None), "")

    def test_fresh_lock_blocks_heavy_commands_and_names_the_deploy(self):
        self.path.write_text(json.dumps(lock()))
        active = deploy_guard.active_lock()
        self.assertIsNotNone(active)
        for cmd in ("npx playwright test", "node scripts/quiet-one-browser-check.mjs --opponent goblin",
                    "cd ~/Developer/frankendom-audio && node artifacts/audio/fatal-crowd/browser.mjs",
                    "npm run test:all", "npm run quality:stop", "npm run build", "node --test tests/*.test.ts",
                    "/Applications/Blender.app/Contents/MacOS/Blender -b x.blend", "node scripts/finisher-preview.mjs --only opened"):
            reason = deploy_guard.decide(cmd, active)
            self.assertIn("Deploy abc1234 is in flight", reason, cmd)
            self.assertIn("one-deployer rule", reason)

    def test_light_commands_pass_under_a_lock(self):
        self.path.write_text(json.dumps(lock()))
        active = deploy_guard.active_lock()
        for cmd in ("git status", "gh pr view 326", "grep -rn foo src", "node --test tests/record.test.ts",
                    "curl -s https://frankendom.com/release.json", "npm run typecheck", "ls -la"):
            self.assertEqual(deploy_guard.decide(cmd, active), "", cmd)

    def test_deploy_script_itself_and_the_deployers_tree_are_never_blocked(self):
        self.path.write_text(json.dumps(lock()))
        active = deploy_guard.active_lock()
        self.assertEqual(deploy_guard.decide("bash scripts/deploy.sh", active), "")
        # own_pid is this process, and the lock pid is this process: the deployer's own tool calls pass.
        self.assertEqual(deploy_guard.decide("npm run test:all", active, own_pid=os.getpid()), "")

    def test_stale_or_dead_locks_are_ignored(self):
        self.path.write_text(json.dumps(lock(age_s=deploy_guard.STALE_S + 60)))
        self.assertIsNone(deploy_guard.active_lock(), "older than STALE_S")
        self.path.write_text(json.dumps(lock(pid=2 ** 22 - 1)))
        self.assertIsNone(deploy_guard.active_lock(), "pid not alive")
        self.path.write_text("not json")
        self.assertIsNone(deploy_guard.active_lock(), "unreadable")

    def test_hook_protocol_denies_with_reason(self):
        self.path.write_text(json.dumps(lock(pid=1)))  # pid 1 is alive and never our ancestor's child
        payload = {"tool_name": "Bash", "tool_input": {"command": "npx playwright test"}}
        env = {**os.environ, "DEPLOY_LOCK": str(self.path)}
        out = subprocess.run([sys.executable, str(Path(deploy_guard.__file__))], input=json.dumps(payload), capture_output=True, text=True, env=env)
        decision = json.loads(out.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("Deploy abc1234 is in flight", decision["permissionDecisionReason"])
        out = subprocess.run([sys.executable, str(Path(deploy_guard.__file__))], input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"}}), capture_output=True, text=True, env=env)
        self.assertEqual(out.stdout, "", "light command: no output, allowed")


if __name__ == "__main__":
    unittest.main()
