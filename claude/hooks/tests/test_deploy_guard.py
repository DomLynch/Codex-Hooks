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
                    "/Applications/Blender.app/Contents/MacOS/Blender -b x.blend", "node scripts/finisher-preview.mjs --only opened",
                    # 2026-09-26: these ran straight through deploy 0325a0b.
                    "cd ~/Developer/frankendom-code-quality && node scripts/quality-stop-targeted.mjs", "npm test",
                    "npm run test", "npm test -- --test-name-pattern=x", "npm run test:browser",
                    "node scripts/pommel-battery.mjs"):
            reason = deploy_guard.decide(cmd, active)
            self.assertIn("Deploy abc1234 is in flight", reason, cmd)
            self.assertIn("one-deployer rule", reason)

    def test_stills_records_and_bakes_are_heavy_whatever_the_cwd_or_prefix(self):
        # 2026-09-26: these ran through deploy a981f7a5 from a .claude/worktrees cwd. The guard reads the command only, never
        # the cwd, so a cp && prefix, an env prefix, a pipe suffix or a cd into a worktree must not hide the script name.
        self.path.write_text(json.dumps(lock()))
        active = deploy_guard.active_lock()
        for cmd in ("cp a b && node scripts/herolook-game-stills.mjs --label game-raw --pilot /herolook/legionary-d.glb --frames 28 2>&1 | tail -2",
                    "node scripts/herolook-game-stills.mjs --frames 34 | tail -1 ; ~/.venvs/face/bin/python -c 'from PIL import Image'",
                    "cd /Users/domininclynch/Desktop/Business/frankendom/.claude/worktrees/vigorous-northcutt-a264de && node scripts/herolook-stills.mjs",
                    "node scripts/herolook-stills.mjs", "node scripts/herolook-kill-record.mjs",
                    "CREATURE_OUT=/tmp/out node scripts/build-creatures.mjs hoplite", "node scripts/build-creatures.mjs hoplite",
                    "node scripts/build-warrior.mjs", "blender -b -P scripts/character/herolook_scutum.py",
                    "/Applications/Blender.app/Contents/MacOS/Blender -b -P scripts/character/creatures.py -- --family hoplite",
                    # Armour's: a Playwright contact sheet, and any future herolook-*.mjs render by prefix.
                    "node scripts/armour-contact-sheet.mjs --family hoplite | tail -1", "node scripts/herolook-anything-new.mjs"):
            self.assertIn("one-deployer rule", deploy_guard.decide(cmd, active), cmd)
        # Light or remote (HF / API calls, PIL): the herolook python helpers stay allowed.
        for script in ("herolook_normal.py", "herolook_attach.py", "herolook-strip.py", "herolook_rawstill.py", "trellis2.py", "hunyuan21.py", "kontext.py", "t2i.py"):
            for cmd in (f"~/.venvs/face/bin/python scripts/character/{script} --in x.glb", f"python3 scripts/character/{script}"):
                self.assertEqual(deploy_guard.decide(cmd, active), "", cmd)

    def test_pixelmator_pro_is_heavy_plain_osascript_is_not(self):
        # 2026-09-26 (Hero Look's headless texture step): Pixelmator Pro via osascript or open -a competes with a deploy.
        self.path.write_text(json.dumps(lock()))
        active = deploy_guard.active_lock()
        for cmd in ("osascript -e 'tell application \"Pixelmator Pro\" to open POSIX file \"/tmp/a.png\"'",
                    "osascript -e 'tell application \"Pixelmator Pro\"' -e 'activate' 2>&1 | tail -1",
                    "osascript scripts/pixelmator-upscale.scpt /tmp/a.png", "osascript ~/Developer/scripts/Pixelmator-export.applescript",
                    "osascript <<'EOF'\ntell application \"Pixelmator Pro\"\n  activate\nend tell\nEOF",
                    "open -a \"Pixelmator Pro\" /tmp/a.png", "open -a Pixelmator\\ Pro /tmp/a.png", "open -b com.pixelmatorteam.pixelmator.x",
                    "cd ~/Developer/frankendom-herolook && node scripts/pixelmator-upscale.mjs --in a.png", "node scripts/textures-upscale.mjs",
                    "node scripts/pixelmator-maps.mjs --in a.png", "node scripts/herolook-sheet.mjs"):   # Hero Look's names, 21:3x
            self.assertIn("one-deployer rule", deploy_guard.decide(cmd, active), cmd)
        for cmd in ("osascript -e 'display notification \"done\" with title \"deploy\"'", "osascript -e 'tell application \"Finder\" to activate'",
                    "osascript scripts/notify.scpt", "open -a Finder", "git commit -m 'osascript Pixelmator Pro texture step'",
                    "git log --grep 'Pixelmator' --oneline", "grep -rn pixelmator scripts/",
                    "osascript -e 'display notification \"x\"' && git log --grep Pixelmator",
                    # 2026-09-27 review: a newline or a single | ends the osascript command, and so does its heredoc
                    "osascript -e 'display notification \"x\"'\ngit commit -m \"pixelmator texture\"",
                    "osascript -e 'display notification \"done\"' | tee pixelmator.log",
                    "osascript <<'EOF'\ndisplay notification \"x\"\nEOF\ngit commit -m pixelmator"):
            self.assertEqual(deploy_guard.decide(cmd, active), "", cmd)

    def test_light_commands_pass_under_a_lock(self):
        self.path.write_text(json.dumps(lock()))
        active = deploy_guard.active_lock()
        for cmd in ("git status", "gh pr view 326", "grep -rn foo src", "node --test tests/record.test.ts",
                    "curl -s https://frankendom.com/release.json", "npm run typecheck", "ls -la",
                    "npm run typecheck:tests", "grep -n test package.json"):
            self.assertEqual(deploy_guard.decide(cmd, active), "", cmd)

    def test_text_that_is_only_data_is_not_a_command(self):
        # 2026-09-26: a commit message mentioning "node --test through deploy" was refused mid-deploy.
        self.path.write_text(json.dumps(lock()))
        active = deploy_guard.active_lock()
        for cmd in ('git commit -m "fix: gate kept spawning node --test through deploy 0325a0b"',
                    "gh pr create --title 'guard blocks npm test' --body 'npm run test:browser and npx playwright'",
                    "git commit -F - <<'EOF'\nguard: npm test and node scripts/quality-stop-targeted.mjs\nEOF",
                    "python3 - <<'EOF'\n# node --test a b\nprint('npm test')\nEOF\necho done",
                    'echo "run npm test after FREE"',
                    "cat > $SP/tables.sh <<'EOF'\n(cd wt && npm run build)\nEOF\nchmod +x $SP/tables.sh",
                    './deploy-notes.sh -c "npm test"',
                    'gh pr create --body "Checks: \\`npm test\\` 661/0/2, \\$(not run) either"'):
            self.assertEqual(deploy_guard.decide(cmd, active), "", cmd)

    def test_quoted_text_a_shell_runs_is_still_a_command(self):
        self.path.write_text(json.dumps(lock()))
        active = deploy_guard.active_lock()
        for cmd in ('bash -c "npm test"', "zsh -lc 'node scripts/quality-stop-targeted.mjs'", "eval 'npm run test:all'",
                    'git commit -m "wip" && npm test', "bash <<'EOF'\nnpx playwright test\nEOF",
                    "/bin/zsh -c 'npm test'", "ssh vps 'x' ; sh <<EOF\nnpm run build\nEOF",
                    'echo "result: $(npm test 2>&1 | tail -1)"', 'X="`npx playwright test`"',
                    'git commit -m "note: scripts/deploy.sh owns the lock" && npm run test:all',
                    'cd "/Users/x/Developer/frankendom-world" && npm run test:browser'):
            self.assertIn("is in flight", deploy_guard.decide(cmd, active), cmd)

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
