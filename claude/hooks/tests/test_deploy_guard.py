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
        mac = mock.patch.object(deploy_guard, "_is_mac", return_value=True)   # the guard is Mac-only (2026-10-10); the tests below run as the Mac
        mac.start()
        self.addCleanup(mac.stop)
        busy = mock.patch.object(deploy_guard, "_mac_load1", return_value=99.0)   # a busy Mac unless a test says otherwise (2026-10-17 GPU waterfall)
        busy.start()
        self.addCleanup(busy.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_gpu_browser_runs_are_allowed_on_a_quiet_mac(self):
        """2026-10-17 (Dom, GPU waterfall in CLAUDE.md; Lead: Characters was blocked): browser/WebGL rows, stills and clips run on the Mac
        FIRST, the VPS only when the Mac is busy. Builds, suites and Blender stay on the VPS whatever the load."""
        gpu = ("npx playwright test --project=chromium", "node scripts/herolook-game-stills.mjs --frames 28", "node scripts/x-browser-check.mjs --opponent goblin",
               "node scripts/finisher-preview.mjs --only opened", "node scripts/herolook-kill-record.mjs", "node artifacts/arena-browser.mjs",
               "cd ~/Developer/frankendom-characters && npx playwright test")
        cpu = ("npm test", "npm run test:all", "npm run build", "vite build", "node --test tests/*.test.ts", "npm run test:browser",
               "/Applications/Blender.app/Contents/MacOS/Blender -b x.blend", "node scripts/release-checks.mjs", "npx playwright test && npm run build")
        with mock.patch.object(deploy_guard, "_mac_load1", return_value=4.0):
            for cmd in gpu:
                self.assertEqual(deploy_guard.decide(cmd, None), "", cmd)
            for cmd in cpu:
                self.assertIn("VPS", deploy_guard.decide(cmd, None), cmd)
        with mock.patch.object(deploy_guard, "_mac_load1", return_value=45.0):
            for cmd in gpu:
                reason = deploy_guard.decide(cmd, None)
                self.assertIn("VPS", reason, cmd)
                self.assertIn("load 45", reason)
        # 2026-10-17 (Lead): release rows failed their timing limits at load 10-13, so above ~12 a GPU row stutters and its numbers are junk
        with mock.patch.object(deploy_guard, "_mac_load1", return_value=11.9):
            self.assertEqual(deploy_guard.decide("npx playwright test", None), "")
        with mock.patch.object(deploy_guard, "_mac_load1", return_value=13.0):
            reason = deploy_guard.decide("npx playwright test", None)
            self.assertIn("load 13", reason)
            self.assertIn("HF T4", reason)
            self.assertIn("put the load in the receipt", reason)
        with mock.patch.object(deploy_guard, "_mac_load1", return_value=4.0):   # a deploy in flight still wins: one-deployer rule
            self.assertIn("in flight", deploy_guard.decide("npx playwright test", {**lock(pid=1), "age_s": 120}))

    def test_no_lock_allows_light_commands(self):
        self.assertIsNone(deploy_guard.active_lock())
        for cmd in ("git status", "gh pr view 1", "node --test tests/one.test.mjs", "python3 -m pytest tests -q", "grep -rn npm README.md"):
            self.assertEqual(deploy_guard.decide(cmd, None), "", cmd)

    def test_heavy_runs_are_refused_on_the_mac_even_without_a_deploy(self):
        """2026-10-08 (Dom, Optimizer item 2): Mac load sat at 29-57 on 10 cores from lane test and browser runs, not the deploy."""
        for cmd in ("npx playwright test", "npm test", "npm run test:all", "node --test tests/*.test.ts", "npm run build",
                    "vite build", "node scripts/x-browser-check.mjs --opponent goblin", "node scripts/finisher-preview.mjs --only opened",
                    "cd ~/Developer/frankendom-combat && npm run test:browser", "/Applications/Blender.app/Contents/MacOS/Blender -b x.blend"):
            reason = deploy_guard.decide(cmd, None)
            self.assertNotEqual(deploy_guard.decide(cmd, None), "", cmd)
            self.assertIn("hfjob -- <cmd>", reason)   # 2026-10-29 (Dom + Strategy): was capture --prio on the VPS; the VPS is for live sites only
            self.assertIn("MAC_HEAVY_OK=1", reason)

    def test_auditor_probes_on_104(self):
        """Auditor HOLD on #104: flags are not files, and an exempt word must be the command or script, not a path/branch/arg."""
        self.assertEqual(deploy_guard.decide("node --test --test-reporter=tap tests/one.test.mjs", None), "")
        for cmd in ("cd ~/Developer/frankendom-webkit && npm run test:all", "git checkout web/iphone-nav && npm run build",
                    "npm run test:all -- tests/safari-audio.test.ts", "echo MAC_HEAVY_OK=1; npm run test:all"):
            self.assertIn("VPS", deploy_guard.decide(cmd, None), cmd)

    def test_escape_hatch_uses_are_logged(self):
        """Optimizer's condition on #104: every MAC_HEAVY_OK=1 use leaves one auditable line (cwd + command)."""
        log = Path(self.tmp.name) / "deploy_guard.log"
        with mock.patch.object(deploy_guard, "_hatch_log", return_value=log):
            self.assertFalse(deploy_guard.note_hatch_use("npm test", "/w/a"))
            self.assertFalse(deploy_guard.note_hatch_use("echo MAC_HEAVY_OK=1; npm test", "/w/a"))
            self.assertFalse(log.exists())
            self.assertTrue(deploy_guard.note_hatch_use("MAC_HEAVY_OK=1 npm test", "/w/combat"))
        line = log.read_text()
        self.assertIn("cwd=/w/combat", line)
        self.assertIn("MAC_HEAVY_OK=1 npm test", line)
        self.assertEqual(len(line.splitlines()), 1)
        # Auditor LOW on #104: env assignments other than the hatch itself may carry tokens; they are masked in the log
        log.unlink()
        with mock.patch.object(deploy_guard, "_hatch_log", return_value=log):
            deploy_guard.note_hatch_use("GH_TOKEN=abc123 MAC_HEAVY_OK=1 npx playwright test --project=chromium", "/w")
        masked = log.read_text()
        self.assertNotIn("abc123", masked)
        self.assertIn("GH_TOKEN=***", masked)
        self.assertIn("MAC_HEAVY_OK=1", masked)
        self.assertIn("--project=chromium", masked)

    def test_mac_refusal_exemptions(self):
        """Deploy itself, Safari/WebKit and real-iPhone rows, the sparring GPU row, and the explicit escape hatch."""
        for cmd in ("bash scripts/deploy.sh", "npx playwright test --project=webkit", "node scripts/safari-check.mjs",
                    "npm run test:iphone", "xcrun simctl launch booted app", "node scripts/sparring-gpu-row.mjs",
                    "MAC_HEAVY_OK=1 npm test", "echo 'npm test'"):
            self.assertEqual(deploy_guard.decide(cmd, None), "", cmd)

    def test_hfjob_wrapper_is_not_a_local_heavy_run(self):
        """V3 Dev via Micro-Manager, 2026-10-29: the refusal says `hfjob -- <cmd>`, so `hfjob -- npm test` must pass; a local run beside it must not."""
        for cmd in ("hfjob -- npm test", "cd ~/Developer/frankendom-v3 && hfjob -- npm test", "hfjob --timeout 30m -- npm run test:all 2>&1 | tail -20",
                    "hfjob --flavor cpu-upgrade -- npx playwright test", "HF_TOKEN=x hfjob -- npm run build", "hfjob -- bash -c 'npm ci && npm test'"):
            self.assertEqual(deploy_guard.decide(cmd, None), "", cmd)
        for cmd in ("hfjob -- npm test; npm test", "hfjob -- true && npm run build", "hfjob x | npm test", "PATH=./bin hfjob -- npm test", "echo hfjob; npm test",
                    'hfjob -- "$(npm test)"'):
            self.assertIn("VPS", deploy_guard.decide(cmd, None), cmd)

    def test_hf_job_submission_is_not_a_local_heavy_run(self):
        """Lead + COO, 2026-10-08: `hf jobs run ... bash -c 'npm run test:all'` runs on Hugging Face; the Mac only submits it."""
        self.path.write_text(json.dumps(lock()))
        active = deploy_guard.active_lock()
        for cmd in ("hf jobs run --flavor cpu-xl python:3.12 bash -c 'npm run test:all'",
                    'hf jobs run img bash -c "npm run test:all"', "hf jobs run img -- npm run build",
                    "HF_TOKEN=x hf jobs uv run script.py", "cd ~/Developer/frankendom-lead && hf jobs run img npx playwright test"):
            self.assertEqual(deploy_guard.decide(cmd, None), "", cmd)
            self.assertEqual(deploy_guard.decide(cmd, active), "", cmd)
        # Auditor HOLD on #105 (a853769): the usual inline payload must pass, a locally expanded substitution must not, env prefix HF_* only.
        for cmd in ('hf jobs run --flavor cpu-upgrade node:22 bash -c "npm ci; npm run test:all"',
                    "hf jobs run img sh -c 'npm ci && npm run build'", "cd x && hf jobs run img bash -c \"npm ci; npx playwright test\""):
            self.assertEqual(deploy_guard.decide(cmd, None), "", cmd)
            self.assertEqual(deploy_guard.decide(cmd, active), "", cmd)
        for cmd in ('hf jobs run x "$(npm run test:all)"', 'hf jobs run x "a $(npx vite build) b"', 'hf jobs run img bash -c "npm ci; $(npm run test:all)"',
                    'hf jobs run img bash -c "npm ci; `npm test`"', "PATH=./bin hf jobs run x -- npm test", "PATH=./bin:$PATH hf jobs run img -- vite build",
                    'echo "; hf jobs x" && bash -c "npm run test:all"'):
            self.assertIn("VPS", deploy_guard.decide(cmd, None), cmd)
            self.assertIn("in flight", deploy_guard.decide(cmd, active), cmd)
        # Only the submission segment is exempt: a real local run next to it, inside it, or merely naming hf is still refused.
        for cmd in ("npm run test:all", "hf jobs run x; npm run test:all", "hf jobs run x && npm test", "hf jobs run x | npm test",
                    "echo hf jobs run && npm run test:all", 'hf jobs run img bash -c "$(npm test)"', "hf jobs run img `npm test`",
                    "hf jobs ps; npx playwright test", "hf auth login && npm run build", "hf jobs-run x && npm test",
                    "git log hf jobs run -- x; vite build"):
            self.assertIn("VPS", deploy_guard.decide(cmd, None), cmd)

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

    def test_off_darwin_the_guard_is_a_noop(self):
        """2026-10-10: the VPS `lanes` user is Linux; the Mac heavy-job guard refused its npm ci / builds, which is exactly what the VPS is for."""
        heavy = "npm ci && npx playwright test && npm run build"
        with mock.patch.object(deploy_guard, "_is_mac", return_value=True):
            self.assertIn("hfjob -- <cmd>", deploy_guard.decide(heavy, None))   # 2026-10-29: was "go to the VPS"
        with mock.patch.object(deploy_guard, "_is_mac", return_value=False):
            self.assertEqual(deploy_guard.decide(heavy, None), "")
            self.path.write_text(json.dumps(lock(pid=1)))
            self.assertEqual(deploy_guard.decide("npx playwright test", deploy_guard.active_lock()), "", "a deploy lock is a Mac fact too")

    def test_hook_protocol_allows_on_linux(self):
        payload = {"tool_name": "Bash", "tool_input": {"command": "npm run build"}}
        code = "import sys, runpy; sys.platform = 'linux'; sys.argv=['deploy_guard.py']; runpy.run_path(%r, run_name='__main__')" % str(Path(deploy_guard.__file__))
        out = subprocess.run([sys.executable, "-c", code], input=json.dumps(payload), capture_output=True, text=True)
        self.assertEqual(out.stdout, "", "linux: no deny output")

    def test_shell_c_wrappers_are_refused(self):
        """2026-10-14 (Strategy + Dom): the app prompts "This shell -c script runs rm and could not be checked" in every mode, bypass included."""
        for cmd in ("bash -c 'git diff | git patch-id'", "sh -c \"echo hi\"", "zsh -lc 'ls'", "/bin/bash -c 'ls'", "cd x && bash -c 'ls'",
                    "env FOO=1 bash -ec 'ls'", "timeout 5 bash -c 'ls'", "a | sh -c 'cat'"):
            self.assertIn("no shell -c wrapper", deploy_guard.shell_c_reason(cmd), cmd)

    def test_shell_c_check_leaves_data_and_remote_alone(self):
        for cmd in ("git commit -m 'never use bash -c here'", "echo \"sh -c x\"", "bash script.sh", "bash -x scripts/deploy.sh", "grep -n 'bash -c' README.md",
                    "ssh root@host bash -c 'ls'", "hf jobs run img bash -c 'make'", "cat <<EOF\nbash -c 'x'\nEOF", "bash -c true"):
            self.assertEqual(deploy_guard.shell_c_reason(cmd), "", cmd)

    def test_shell_c_deny_reaches_the_hook_output(self):
        payload = {"tool_name": "Bash", "tool_input": {"command": "bash -c 'ls'"}}
        out = subprocess.run([sys.executable, str(Path(deploy_guard.__file__))], input=json.dumps(payload), capture_output=True, text=True).stdout
        self.assertIn("no shell -c wrapper", out)

    def test_rm_recursive_on_an_unguarded_variable_is_refused(self):
        """2026-10-14 (Strategy + Dom): the app's own "Dangerous rm operation on possibly-empty variable path" card parks a bypass lane for hours."""
        for cmd in ("rm -rf $S/$d", "rm -rf \"$S/$d\"", "rm -r ${DIR}/x", "rm -fr \"${DIR}\"", "cd x && rm -Rf $TMP/out", "sudo rm -rf $HOME/.cache/x",
                    "rm --recursive --force $S", "FOO=1 rm -rf $S", "ls; rm -f -r $S/a"):
            reason = deploy_guard.rm_var_reason(cmd)
            self.assertIn("literal path", reason, cmd)
            self.assertNotIn("${VAR:?}", reason, cmd)   # destructive_var_guard denies the :?-guarded form too (Auditor 2026-10-21): one advice, not two walls

    def test_rm_check_leaves_guarded_literal_and_non_recursive_alone(self):
        for cmd in ("rm -rf \"${S:?}/${d:?}\"", "rm -rf ${S:-/tmp/x}/a", "rm -rf /tmp/literal", "rm -f $S/one.txt", "rm $S/one.txt", "rm -rf ./build",
                    "echo 'rm -rf $S'", "git commit -m \"rm -rf $S is bad\"", "echo \"a; rm -rf $S\"", "grep -n 'rm -rf $S' x", "cat <<EOF\nrm -rf $S\nEOF",
                    "ls $S", "rm -rf \"$(mktemp -d)\""):
            self.assertEqual(deploy_guard.rm_var_reason(cmd), "", cmd)

    def test_rm_var_deny_reaches_the_hook_output(self):
        payload = {"tool_name": "Bash", "tool_input": {"command": "rm -rf $S/$d"}}
        out = subprocess.run([sys.executable, str(Path(deploy_guard.__file__))], input=json.dumps(payload), capture_output=True, text=True).stdout
        self.assertIn('"permissionDecision": "deny"', out)
        self.assertIn("literal path", out)
        self.assertNotIn("${VAR:?}", out)

    def test_candidate_contents_check_is_not_a_heavy_run(self):
        """2026-10-14 (Lead): scripts/candidate-contents-check.mjs is seconds of git merge-tree plus GitHub REST, needs GH_TOKEN (cannot go to the VPS),
        and Deploy runs it on every cut; `check` in its name made HEAVY_RE match once it moved into trunk's scripts/ (RPG-game#2728)."""
        for cmd in ("node scripts/candidate-contents-check.mjs", "node scripts/candidate-contents-check.mjs --pr 2733 --base main",
                    "GH_TOKEN=x node scripts/candidate-contents-check.mjs release-1", "cd /x && node /x/scripts/candidate-contents-check.mjs 2>&1 | tail -3"):
            self.assertEqual(deploy_guard.decide(cmd, None), "", cmd)
            self.path.write_text(json.dumps(lock(pid=1)))
            self.assertEqual(deploy_guard.decide(cmd, deploy_guard.active_lock()), "", cmd + " (under a deploy lock)")
            self.path.unlink()

    def test_candidate_contents_exemption_lets_no_heavy_run_through(self):
        for cmd in ("npm test", "npm run build", "vite build", "npx playwright test", "node scripts/release-checks.mjs",
                    "node scripts/other-check.mjs candidate-contents-check.mjs", "node scripts/candidate-contents-check.mjs && npm test",
                    "node scripts/candidate-contents-check.mjs; vite build", "node scripts/candidate-contents-check.mjs | npx playwright test",
                    "node scripts/candidate-contents-check.mjs.sh", "node scripts/xcandidate-contents-check.mjs"):
            self.assertIn("VPS", deploy_guard.decide(cmd, None) + " VPS" if not deploy_guard.decide(cmd, None) else deploy_guard.decide(cmd, None), cmd) if False else self.assertNotEqual(deploy_guard.decide(cmd, None), "", cmd)


if __name__ == "__main__":
    unittest.main()
