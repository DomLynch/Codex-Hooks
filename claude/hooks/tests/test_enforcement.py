import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import quality  # noqa: E402
import stop_orchestrator  # noqa: E402
import post_tool  # noqa: E402


class EnforcementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, target in (('GATE_CACHE_PATH', self.root / 'gate-cache.json'), ('GATE_LOCK_PATH', self.root / 'gate.lock')):
            p = patch.object(quality, name, target)
            p.start()
            self.addCleanup(p.stop)
        # The gate defers while a real Frankendom deploy holds the lock; these tests must not depend on that.
        lock = patch.object(stop_orchestrator, 'active_deploy_lock', return_value=None)
        lock.start()
        self.addCleanup(lock.stop)
        # Nor on this Mac's load, a Deploy hold, or a lane's gate running at the same time.
        for name in ('gate_busy', 'deploy_hold'):
            quiet = patch.object(stop_orchestrator, name, return_value=None)
            quiet.start()
            self.addCleanup(quiet.stop)
        # run_project_gate re-reads load before each completion command, so a busy Mac must not defer these tests.
        calm = patch.object(quality.os, 'getloadavg', return_value=(5.0, 5.0, 5.0))
        calm.start()
        self.addCleanup(calm.stop)

    def contract(self, commands):
        (self.root / '.quality-gate.json').write_text(json.dumps({'commands': commands}))

    def test_checkpoint_preserves_newer_checkout_and_uncommitted_edits(self):
        from lib import git as git_tools

        def git(*args):
            return subprocess.check_output(['git', *args], cwd=self.root, text=True).strip()

        git('init', '-q', '-b', 'main')
        git('config', 'user.email', 'hooks-test@example.com')
        git('config', 'user.name', 'Hooks Test')
        (self.root/'version.txt').write_text('old release\n')
        git('add', '.')
        git('commit', '-qm', 'old release')
        branch = post_tool.current_session_branch('stale-test')
        git('branch', branch)
        (self.root/'version.txt').write_text('current release\n')
        git('commit', '-qam', 'current release')
        (self.root/'pending.txt').write_text('uncommitted task work\n')
        before = (git('rev-parse', 'HEAD'), git('status', '--porcelain'), git('rev-parse', branch))
        with (
            patch.object(post_tool, 'CHECKPOINTS_ENABLED', True),
            patch.object(post_tool, 'checkpoint_state', return_value={'edits_since_last_commit': 5}),
            patch.object(post_tool, 'update_checkpoint_state') as state,
            patch.object(post_tool, 'commit', return_value=(False, 'not committed')) as commit,
            patch.object(post_tool, 'push') as push,
            patch.object(post_tool, 'log'),
        ):
            post_tool.maybe_checkpoint('stale-test', self.root)
        self.assertEqual(git('branch', '--show-current'), 'main')
        self.assertEqual((git('rev-parse', 'HEAD'), git('status', '--porcelain'), git('rev-parse', branch)), before)
        self.assertEqual((self.root/'version.txt').read_text(), 'current release\n')
        self.assertEqual((self.root/'pending.txt').read_text(), 'uncommitted task work\n')
        commit.assert_not_called()
        push.assert_not_called()
        state.assert_called_once_with('stale-test', branch_failed=True)
        self.assertTrue(git_tools.ensure_branch('fresh-checkpoint', self.root))
        self.assertEqual(git('rev-parse', 'HEAD'), before[0])
        self.assertTrue(git_tools.ensure_branch('main', self.root))
        self.assertTrue(git_tools.ensure_branch('main', self.root))

    def test_checkpoint_disabled_by_auto_pr_switch_never_touches_the_checkout(self):
        # Lane worktrees sit on their own branches; with CODEX_HOOK_AUTO_PR=0 the
        # checkpoint must not create claude/<session>/main, commit or push there.
        subprocess.check_output(['git', 'init', '-q', '-b', 'lane/feature'], cwd=self.root)
        (self.root/'work.txt').write_text('in progress\n')
        with (
            patch.object(post_tool, 'CHECKPOINTS_ENABLED', False),
            patch.object(post_tool, 'checkpoint_state', return_value={'edits_since_last_commit': 50}),
            patch.object(post_tool, 'ensure_branch') as ensure,
            patch.object(post_tool, 'commit') as commit,
            patch.object(post_tool, 'push') as push,
            patch.object(post_tool, 'log'),
        ):
            post_tool.maybe_checkpoint('lane-test', self.root)
        ensure.assert_not_called()
        commit.assert_not_called()
        push.assert_not_called()
        self.assertEqual(subprocess.check_output(['git', 'branch', '--show-current'], cwd=self.root, text=True).strip(), 'lane/feature')

    def test_auto_pr_and_checkpoints_default_off_without_config(self):
        # Audit E: the installer writes CODEX_HOOK_AUTO_PR=0 but a missing or unreadable
        # hook_env fell back to "1", so a broken config turned commit+push on.
        hooks = Path(__file__).resolve().parents[1]
        env = {k: v for k, v in os.environ.items() if k != 'CODEX_HOOK_AUTO_PR'}
        env['HOME'] = str(self.root)
        out = subprocess.check_output(
            [sys.executable, '-c', 'import post_tool; print(post_tool.CHECKPOINTS_ENABLED)'],
            cwd=hooks, env=env, text=True)
        self.assertEqual(out.split(), ['False'])

    def test_commit_stages_only_the_files_the_session_edited(self):
        from lib import git as git_tools

        def git(*args):
            return subprocess.check_output(['git', *args], cwd=self.root, text=True).strip()

        git('init', '-q', '-b', 'main')
        git('config', 'user.email', 'hooks-test@example.com')
        git('config', 'user.name', 'Hooks Test')
        for name in ('owned.txt', 'other.txt', 'gone.txt'):
            (self.root/name).write_text('v1\n')
        git('add', '.')
        git('commit', '-qm', 'base')
        (self.root/'owned.txt').write_text('task edit\n')
        (self.root/'gone.txt').unlink()
        (self.root/'other.txt').write_text('another lane\'s work\n')
        (self.root/'stray.txt').write_text('untracked, not ours\n')
        git('add', 'other.txt')  # someone else's staged change must not ride along either
        ok, sha = git_tools.commit('task', self.root, [self.root/'owned.txt', self.root/'gone.txt', Path('/elsewhere/x.py')])
        self.assertTrue(ok, sha)
        self.assertEqual(git('show', '--name-only', '--format=', 'HEAD').split(), ['gone.txt', 'owned.txt'])
        self.assertEqual(git('status', '--porcelain').splitlines(), ['M  other.txt', '?? stray.txt'])
        self.assertEqual(git_tools.commit('nothing owned', self.root, []), (False, ''))

    def test_checkpoint_commits_only_recorded_session_files(self):
        with tempfile.TemporaryDirectory() as tmp, patch('lib.state.CHECKPOINT_PATH', Path(tmp)/'cp.json'):
            post_tool.update_checkpoint_state('s1', edits=1, files=[self.root/'a.py'])
            post_tool.update_checkpoint_state('s1', edits=1, files=[self.root/'b.py', self.root/'a.py'])
            self.assertEqual(post_tool.checkpoint_state('s1')['files'], [str(self.root/'a.py'), str(self.root/'b.py')])
            with (
                patch.object(post_tool, 'CHECKPOINTS_ENABLED', True),
                patch.object(post_tool, 'is_git_repo', return_value=True),
                patch.object(post_tool, 'ensure_branch', return_value=True),
                patch.object(post_tool, 'commit', return_value=(True, 'abc')) as commit,
                patch.object(post_tool, 'push', return_value=True),
                patch.object(post_tool, 'log'),
                patch.object(post_tool, 'CHECKPOINT_EDIT_THRESHOLD', 2),
            ):
                post_tool.maybe_checkpoint('s1', self.root)
            self.assertEqual(commit.call_args.args[2], [str(self.root/'a.py'), str(self.root/'b.py')])
            self.assertNotIn('files', post_tool.checkpoint_state('s1'), 'a checkpoint commit starts a fresh file list')

    def test_contract_success_and_real_failure(self):
        for code in (0, 1):
            self.contract([[sys.executable, '-c', f'raise SystemExit({code})']])
            self.assertEqual(quality.run_project_gate(self.root)['ok'], code == 0)

    def test_completion_checks_cannot_hide_behind_green_unit_checks(self):
        for code in (1, 0):
            (self.root / '.quality-gate.json').write_text(json.dumps({
                'commands': [[sys.executable, '-c', 'print("unit checks pass")']],
                'completion_commands': [[sys.executable, '-c', f'print("workflow receipt"); raise SystemExit({code})']],
            }))
            result = quality.run_project_gate(self.root)
            self.assertEqual(result['ok'], code == 0)
            self.assertEqual(len(result['results']), 2)
            self.assertIn('workflow receipt', result['results'][-1]['output'])

    def test_invalid_completion_checks_fail_closed(self):
        for commands in ('echo pass', [[]], [['missing-workflow-tool']]):
            (self.root / '.quality-gate.json').write_text(json.dumps({
                'commands': [[sys.executable, '-c', 'pass']], 'completion_commands': commands,
            }))
            self.assertFalse(quality.run_project_gate(self.root)['ok'])

    def test_missing_invalid_empty_and_missing_executable_block(self):
        self.assertFalse(quality.run_project_gate(self.root)['ok'])
        for commands in ([], 'echo pass', [[]], [['definitely-missing-quality-tool']]):
            self.contract(commands)
            self.assertFalse(quality.run_project_gate(self.root)['ok'])

    def test_deploy_starting_mid_check_kills_the_check_tree_and_defers(self):
        # 2026-09-26: a gate begun at 06:54 on a free Mac kept spawning node --test through deploy 0325a0b.
        pidfile = self.root / 'grandchild.pid'
        spawn = (f'import subprocess, sys, time; p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]); '
                 f'open({str(pidfile)!r}, "w").write(str(p.pid)); time.sleep(60)')
        self.contract([[sys.executable, '-c', spawn]])
        calls = []
        def lock_after_start():
            calls.append(1)
            return {'revision': 'abc1234'} if len(calls) > 1 and pidfile.exists() else None
        started = time.monotonic()
        with patch.object(quality, 'DEFER_POLL_S', 0.1):
            result = quality.run_project_gate(self.root, defer_to=lock_after_start)
        self.assertLess(time.monotonic() - started, 10)
        self.assertTrue(result['ok'])
        self.assertEqual(result['deferred'], {'revision': 'abc1234'})
        grandchild = int(pidfile.read_text())
        for _ in range(50):   # SIGKILL of the group is async; give the kernel a moment to reap
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            os.kill(grandchild, 9)
            self.fail('the check process group outlived the deferral')

    def test_deploy_starting_between_checks_stops_before_the_next_and_caches_nothing(self):
        marker = self.root / 'second-ran'
        self.contract([[sys.executable, '-c', 'pass'], [sys.executable, '-c', f'open({str(marker)!r}, "w")']])
        answers = iter([None, {'revision': 'abc1234'}])
        result = quality.run_project_gate(self.root, defer_to=lambda: next(answers))
        self.assertEqual(result['deferred'], {'revision': 'abc1234'})
        self.assertFalse(marker.exists())
        self.assertTrue(quality.run_project_gate(self.root)['ok'])   # no cached pass: the full contract runs
        self.assertTrue(marker.exists())

    def test_deadline_blocks_and_says_it_is_a_time_limit(self):
        self.contract([[sys.executable, '-c', 'import time; time.sleep(5)']])
        result = quality.run_project_gate(self.root, timeout=0.05)
        self.assertFalse(result['ok'])
        self.assertIn('not a failing check', result['results'][-1]['output'])
        self.assertIn('time.sleep(5)', result['results'][-1]['output'])

    def test_pass_on_identical_tree_is_remembered_until_the_tree_changes(self):
        repo = self.root / 'repo'
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', '-b', 'main'], cwd=repo, check=True)
        (repo / 'a.txt').write_text('one\n')
        subprocess.run(['git', 'add', '.'], cwd=repo, check=True)
        subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-qm', 'init'], cwd=repo, check=True)
        marker = self.root / 'runs'
        cmd = [sys.executable, '-c', f'open({str(marker)!r}, "a").write("x")']
        (repo / '.quality-gate.json').write_text(json.dumps({'commands': [cmd]}))
        self.assertTrue(quality.run_project_gate(repo)['ok'])
        second = quality.run_project_gate(repo)
        self.assertTrue(second['ok'])
        self.assertIn('cached pass', second['results'][0]['output'])
        self.assertEqual(marker.read_text(), 'x', 'identical tree: gate not re-run')
        (repo / 'a.txt').write_text('two\n')  # unstaged edit -> new key
        self.assertTrue(quality.run_project_gate(repo)['ok'])
        self.assertEqual(marker.read_text(), 'xx')
        (repo / 'new.txt').write_text('untracked\n')  # untracked file -> new key
        self.assertTrue(quality.run_project_gate(repo)['ok'])
        self.assertEqual(marker.read_text(), 'xxx')
        (repo / '.quality-gate.json').write_text(json.dumps({'commands': [cmd + ['--changed']]}))  # contract -> new key
        self.assertTrue(quality.run_project_gate(repo)['ok'])
        self.assertEqual(marker.read_text(), 'xxxx')
        with patch.object(quality, 'GATE_CACHE_TTL_S', 0):  # expired entry is not trusted
            self.assertTrue(quality.run_project_gate(repo)['ok'])
        self.assertEqual(marker.read_text(), 'xxxxx')

    def _repo_with_origin(self, workflow=True):
        """Local repo with a bare origin whose default branch is main; returns the repo path."""
        origin = self.root / 'origin.git'
        repo = self.root / 'repo'
        subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', origin], check=True)
        subprocess.run(['git', 'clone', '-q', origin, repo], check=True, capture_output=True)

        def g(*a):
            subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', *a], cwd=repo, check=True, capture_output=True)

        if workflow:
            (repo / '.github' / 'workflows').mkdir(parents=True)
            (repo / '.github' / 'workflows' / 'ci.yml').write_text('on: push\n')
        (repo / '.quality-gate.json').write_text(json.dumps({'commands': [[sys.executable, '-c', 'pass']]}))
        (repo / 'a.txt').write_text('1\n')
        g('add', '.')
        g('commit', '-qm', 'init')
        g('push', '-q', '-u', 'origin', 'main')
        g('remote', 'set-head', 'origin', 'main')
        return repo, g

    def test_gate_is_skipped_only_when_every_named_ci_check_passed_on_this_exact_commit(self):
        # Audit A (2026-09-27) + recheck: "some green Actions runs" is not "the checks that replace this gate".
        # The contract names them (ci_checks); each must be present and green on the exact HEAD SHA.
        repo, g = self._repo_with_origin()
        head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()
        with patch.object(quality, '_check_runs', return_value=[('python-hooks', 'completed', 'success')]) as runs:
            self.assertEqual(quality.ci_covered_commit(repo), ('', ''), 'no ci_checks in the contract -> gate runs')
            runs.assert_not_called()
        (repo / '.quality-gate.json').write_text(json.dumps({'commands': [['true']], 'ci_checks': ['python-hooks', 'lint']}))
        g('commit', '-qam', 'name the checks')
        g('push', '-q', 'origin', 'main')
        head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()
        ok, lint = ('python-hooks', 'completed', 'success'), ('lint', 'completed', 'success')
        cases = [
            (None, ('', ''), 'gh failed -> gate runs'),
            ([], ('', ''), 'no runs on this commit -> gate runs'),
            ([ok, ('docs', 'completed', 'success')], ('', ''), 'a named check is missing; an unrelated green one does not stand in'),
            ([ok, lint, ('docs', 'completed', 'failure')], (head[:7], 'passed'), 'every named check green; other checks are ignored'),
            ([ok, ('lint', 'in_progress', '')], (head[:7], 'pending'), 'a named check still running -> deferred'),
            ([ok, ('lint', 'completed', 'failure')], ('', ''), 'a named check failed -> gate runs'),
            ([ok, ('lint', 'completed', 'cancelled')], ('', ''), 'cancelled is not a pass'),
            ([ok, ('lint', 'completed', 'skipped')], (head[:7], 'passed'), 'skipped counts as not failed'),
        ]
        for runs, expected, why in cases:
            with patch.object(quality, '_check_runs', return_value=runs) as check_runs:
                self.assertEqual(quality.ci_covered_commit(repo), expected, why)
                check_runs.assert_called_once_with(repo, head)
        for bad in ('python-hooks', [], [1], {'a': 1}):
            (repo / '.quality-gate.json').write_text(json.dumps({'commands': [['true']], 'ci_checks': bad}))
            with patch.object(quality, '_check_runs', return_value=[ok, lint]):
                self.assertEqual(quality.ci_covered_commit(repo), ('', ''), f'invalid ci_checks {bad!r} -> gate runs')
        (repo / '.quality-gate.json').write_text(json.dumps({'commands': [['true']], 'ci_checks': ['python-hooks', 'lint']}))
        with patch.object(quality, '_check_runs', return_value=[ok, lint]):
            (repo / 'a.txt').write_text('2\n')
            self.assertEqual(quality.ci_covered_commit(repo), ('', ''), 'modified file -> gate runs')
            g('checkout', '-q', 'a.txt')
            (repo / 'new.txt').write_text('untracked\n')
            self.assertEqual(quality.ci_covered_commit(repo), ('', ''), 'untracked file -> gate runs')

    def test_this_repo_names_its_ci_check(self):
        contract = json.loads((Path(__file__).resolve().parents[3] / '.quality-gate.json').read_text())
        workflow = (Path(__file__).resolve().parents[3] / '.github' / 'workflows' / 'hooks-ci.yml').read_text()
        for name in contract['ci_checks']:
            self.assertIn(f'\n  {name}:', workflow, f'ci_checks names a job that is not in hooks-ci.yml: {name}')

    def test_gate_skip_needs_a_ci_workflow_and_orchestrator_honours_it(self):
        repo, g = self._repo_with_origin(workflow=False)
        (repo / '.quality-gate.json').write_text(json.dumps({'commands': [['true']], 'ci_checks': ['python-hooks']}))
        g('commit', '-qam', 'name the check')
        g('push', '-q', 'origin', 'main')
        green = [('python-hooks', 'completed', 'success')]
        with patch.object(quality, '_check_runs', return_value=green):
            self.assertEqual(quality.ci_covered_commit(repo), ('', ''), 'no CI in the repo -> nothing else verifies it -> gate runs')
        (repo / '.github' / 'workflows').mkdir(parents=True)
        (repo / '.github' / 'workflows' / 'ci.yml').write_text('on: push\n')
        g('add', '.')
        g('commit', '-qm', 'ci')
        g('push', '-q', 'origin', 'main')
        with (patch.object(quality, '_check_runs', return_value=green),
              patch.object(stop_orchestrator, 'run_project_gate') as run_gate, patch.object(stop_orchestrator, 'log')):
            self.assertIs(stop_orchestrator.run_aaa_gate(repo, 'transcript'), True)
            run_gate.assert_not_called()
        with (patch.object(quality, '_check_runs', return_value=[('python-hooks', 'in_progress', '')]),
              patch.object(stop_orchestrator, 'DEFERRAL_NOTICE_PATH', self.root / 'notified.json'),
              patch.object(stop_orchestrator, 'run_project_gate') as run_gate, patch.object(stop_orchestrator, 'log'),
              patch('sys.stdout', new_callable=io.StringIO)):
            self.assertEqual(stop_orchestrator.run_aaa_gate(repo, 'transcript', session_id='s1'), stop_orchestrator.DEFERRED)
            run_gate.assert_not_called()
            self.assertIn('CI is still running', '\n'.join(stop_orchestrator._CONTEXT_NOTES))
        (repo / 'a.txt').write_text('dirty\n')
        with patch.object(stop_orchestrator, 'run_project_gate', return_value={'ok': True, 'results': []}) as run_gate, patch.object(stop_orchestrator, 'log'):
            self.assertTrue(stop_orchestrator.run_aaa_gate(repo, 'transcript'))
            run_gate.assert_called_once()

    def test_deploy_deferral_notice_is_sent_once_per_session_and_revision(self):
        # Stop additionalContext continues the turn: repeating it on every Stop looped an idle lane (2026-09-23).
        stop_orchestrator._CONTEXT_NOTES.clear()
        self.contract([['true']])
        lock = {'revision': 'c0b321c7f8', 'age_s': 60}
        with patch.object(stop_orchestrator, 'active_deploy_lock', return_value=lock), \
                patch.object(stop_orchestrator, 'DEFERRAL_NOTICE_PATH', self.root / 'notified.json'), \
                patch.object(stop_orchestrator, 'ci_covered_commit', return_value=('', '')), \
                patch.object(stop_orchestrator, 'run_project_gate') as run_gate, \
                patch.object(stop_orchestrator, 'log'), \
                patch('sys.stdout', new_callable=io.StringIO):
            self.assertTrue(stop_orchestrator.run_aaa_gate(self.root, 't', session_id='s1'))
            self.assertIn('deferred: deploy c0b321c', '\n'.join(stop_orchestrator._CONTEXT_NOTES))
            stop_orchestrator._CONTEXT_NOTES.clear()
            self.assertTrue(stop_orchestrator.run_aaa_gate(self.root, 't', session_id='s1'))
            self.assertEqual('\n'.join(stop_orchestrator._CONTEXT_NOTES), '', 'second Stop under the same deploy must stay silent')
            self.assertTrue(stop_orchestrator.run_aaa_gate(self.root, 't', session_id='s2'))
            self.assertIn('deferred', '\n'.join(stop_orchestrator._CONTEXT_NOTES), 'another session still hears it once')
            stop_orchestrator._CONTEXT_NOTES.clear()
            lock['revision'] = 'dfed9951aa'
            self.assertTrue(stop_orchestrator.run_aaa_gate(self.root, 't', session_id='s1'))
            self.assertIn('deferred: deploy dfed995', '\n'.join(stop_orchestrator._CONTEXT_NOTES), 'a new deploy is announced again')
            run_gate.assert_not_called()

    def test_gate_waits_for_load_hold_or_a_run_already_in_this_folder(self):
        # Lead, 2026-09-26: gates stacked to load 386, two in one worktree, while Deploy waited to launch.
        self.contract([[sys.executable, '-c', 'import time; time.sleep(30)']])
        hold = self.root / 'deploy_hold'
        with patch.object(quality, 'DEPLOY_HOLD_PATH', hold), patch.object(quality.os, 'getloadavg', return_value=(5, 5, 5)):
            self.assertIsNone(quality.gate_busy(self.root))
            with patch.object(quality.os, 'getloadavg', return_value=(386, 200, 100)):
                self.assertIn('load 386', quality.gate_busy(self.root)['reason'])
            hold.touch()
            self.assertIn('Deploy is holding', quality.gate_busy(self.root)['reason'])
            # Lead, 2026-09-30 08:44: a release paused 46 min after its last touch; the hold still stands.
            os.utime(hold, (time.time() - 46 * 60, time.time() - 46 * 60))
            self.assertIn('Deploy is holding', quality.gate_busy(self.root)['reason'], 'a paused release still holds')
            os.utime(hold, (time.time() - 13 * 3600, time.time() - 13 * 3600))
            self.assertIsNone(quality.gate_busy(self.root), 'a forgotten hold expires after 12 h')
            running = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], cwd=self.root)
            self.addCleanup(running.kill)
            time.sleep(0.3)
            self.assertIn(f'pid {running.pid}', quality.gate_busy(self.root)['reason'])
            elsewhere = self.root / 'other'
            elsewhere.mkdir()
            (elsewhere / '.quality-gate.json').write_text((self.root / '.quality-gate.json').read_text())
            self.assertIsNone(quality.gate_busy(elsewhere), 'the same check in another folder does not count')

    def test_gate_waits_when_no_declared_package_is_installed(self):
        # Lead, 2026-09-29: the Duel app worktree has no node_modules (the Desktop checkout's is empty), so eslint
        # failed on "Cannot find package '@eslint/js'" and blocked every Stop. Nothing is installed automatically.
        self.contract([['true']])
        (self.root / 'package.json').write_text(json.dumps({'devDependencies': {'@eslint/js': '9', 'eslint': '9'}}))
        with patch.object(quality, 'DEPLOY_HOLD_PATH', self.root / 'no-hold'), \
                patch.object(quality.os, 'getloadavg', return_value=(5, 5, 5)):
            self.assertIn('no node_modules', quality.gate_busy(self.root)['reason'])
            (self.root / 'node_modules').mkdir()
            self.assertIn('no node_modules', quality.gate_busy(self.root)['reason'], 'an empty node_modules is none')
            pkg = self.root / 'node_modules' / 'eslint'
            pkg.mkdir()
            (pkg / 'package.json').write_text('{}')
            self.assertIsNone(quality.gate_busy(self.root), 'installed packages: the gate runs (and a missing one fails)')
            child = self.root / 'wt'
            child.mkdir()
            (child / '.quality-gate.json').write_text((self.root / '.quality-gate.json').read_text())
            (child / 'package.json').write_text((self.root / 'package.json').read_text())
            self.assertIsNone(quality.gate_busy(child), 'packages in a parent folder resolve for node too')
            (self.root / 'package.json').write_text(json.dumps({'name': 'no-deps'}))
            shutil.rmtree(self.root / 'node_modules')
            self.assertIsNone(quality.gate_busy(self.root), 'a package.json with no dependencies needs nothing installed')

    def test_gate_waits_while_lead_has_handed_the_box_to_another_worktree(self):
        # Lead, 2026-09-29: Combat's gate ran at 20:01 while Weapons held the slot Lead had handed it. Lead writes
        # ~/.claude/state/box_slot = "<owner worktree path> <until, ISO local time>" and removes it on "box back".
        from datetime import datetime, timedelta
        self.contract([['true']])
        slot = self.root / 'box_slot'
        soon = (datetime.now() + timedelta(minutes=30)).isoformat(timespec='minutes')
        owner = self.root / 'frankendom weapons'
        with patch.object(quality, 'BOX_SLOT_PATH', slot), patch.object(quality, 'DEPLOY_HOLD_PATH', self.root / 'no-hold'), \
                patch.object(quality.os, 'getloadavg', return_value=(5, 5, 5)):
            self.assertIsNone(quality.gate_busy(self.root), 'no slot file: the gate runs')
            slot.write_text(f'{owner} {soon}\n')
            reason = quality.gate_busy(self.root)['reason']
            self.assertIn(f'box slot held by {owner} until {soon}', reason)
            owner.mkdir()
            self.assertIsNone(quality.gate_busy(owner), 'the slot owner runs its own gate')
            (owner / 'sub').mkdir()
            self.assertIsNone(quality.gate_busy(owner / 'sub'), 'and from a folder inside it')
            past = (datetime.now() - timedelta(minutes=1)).isoformat(timespec='minutes')
            slot.write_text(f'{owner} {past}\n')
            self.assertIsNone(quality.gate_busy(self.root), 'an expired slot does not hold')
            slot.write_text('garbage\n')
            self.assertIsNone(quality.gate_busy(self.root), 'an unreadable slot does not hold')
            slot.write_text(f'{owner} {(datetime.now() + timedelta(days=3)).isoformat(timespec="minutes")}\n')
            os.utime(slot, (time.time() - 13 * 3600, time.time() - 13 * 3600))
            self.assertIsNone(quality.gate_busy(self.root), 'a forgotten slot expires 12 h after it was written')

    def test_busy_gate_defers_with_a_note_instead_of_failing(self):
        self.contract([['true']])
        with patch.object(stop_orchestrator, 'gate_busy', return_value={'reason': 'the Mac is overloaded (load 386, limit 60)'}), \
                patch.object(stop_orchestrator, 'DEFERRAL_NOTICE_PATH', self.root / 'notified.json'), \
                patch.object(stop_orchestrator, 'ci_covered_commit', return_value=('', '')), \
                patch.object(stop_orchestrator, 'run_project_gate') as run_gate, \
                patch.object(stop_orchestrator, 'log'), \
                patch('sys.stdout', new_callable=io.StringIO):
            self.assertTrue(stop_orchestrator.run_aaa_gate(self.root, 't', session_id='s1'))
            self.assertIn('deferred: the Mac is overloaded (load 386', '\n'.join(stop_orchestrator._CONTEXT_NOTES))
            run_gate.assert_not_called()

    def test_browser_completion_command_defers_when_load_rises_after_the_gate_started(self):
        # Strategy for Auditor, 2026-10-06: the gate sampled load once at its start, then ran Chromium rows
        # (sparring-browser-check.mjs) while load climbed to 60-180; real-time waits failed (two false reds, src/
        # unchanged). Load is re-read before each completion command (where repeatable browser checks live).
        subprocess.run(['git', 'init', '-q', '-b', 'main'], cwd=self.root, check=True)
        subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-qm', 'init', '--allow-empty'], cwd=self.root, check=True)
        first, second = self.root / 'first', self.root / 'second'
        def touch(m):
            return [sys.executable, '-c', f'open({str(m)!r}, "w").write("x")']

        (self.root / '.quality-gate.json').write_text(json.dumps({'commands': [touch(first)], 'completion_commands': [touch(second)]}))
        def busy():
            return (500.0, 100.0, 50.0) if first.exists() else (5.0, 5.0, 5.0)

        with patch.object(quality.os, 'getloadavg', side_effect=busy), patch.object(quality, 'GATE_LOCK_PATH', self.root / 'lock'), \
                patch.object(quality, 'GATE_CACHE_PATH', self.root / 'cache'), patch.object(quality, 'DEPLOY_HOLD_PATH', self.root / 'no-hold'):
            result = quality.run_project_gate(self.root)
        self.assertIn('overloaded (load 500', result['deferred']['reason'])
        self.assertTrue(first.exists(), 'the first command ran: load was fine when the gate started')
        self.assertFalse(second.exists(), 'the browser row did not run under load')
        second.unlink(missing_ok=True)
        first.unlink()
        with patch.object(quality.os, 'getloadavg', return_value=(5.0, 5.0, 5.0)), patch.object(quality, 'GATE_LOCK_PATH', self.root / 'lock'), \
                patch.object(quality, 'GATE_CACHE_PATH', self.root / 'cache2'), patch.object(quality, 'DEPLOY_HOLD_PATH', self.root / 'no-hold'):
            result = quality.run_project_gate(self.root)
        self.assertTrue(result['ok'] and not result.get('deferred') and second.exists(), 'low load: every command runs')

    def test_one_repository_gate_per_mac(self):
        # Lead, 2026-09-27: 8-12 `quality-stop-targeted.mjs` runs at once, 9 min each against a 420 s budget. The
        # run holds a Mac-wide lock; other Stops defer. A dead or stale holder does not block anyone.
        marker = self.root / 'ran'
        self.contract([[sys.executable, '-c', f'open({str(marker)!r}, "w").write("x")']])
        subprocess.run(['git', 'init', '-q', '-b', 'main'], cwd=self.root, check=True)
        subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-qm', 'init', '--allow-empty'], cwd=self.root, check=True)
        holder = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        self.addCleanup(holder.kill)
        lock = self.root / 'gate.lock'
        lock.write_text(json.dumps({'pid': holder.pid, 'cwd': '/Users/x/frankendom-deploy', 'at': time.time()}))
        with patch.object(quality.os, 'getloadavg', return_value=(5, 5, 5)), patch.object(quality, 'DEPLOY_HOLD_PATH', self.root / 'none'):
            self.assertIn('one gate per Mac', quality.gate_busy(self.root)['reason'])
            self.assertIn('frankendom-deploy', quality.gate_busy(self.root)['reason'])
            result = quality.run_project_gate(self.root)
            self.assertIn('one gate per Mac', result['deferred']['reason'])
            self.assertFalse(marker.exists(), 'a deferred gate runs nothing')
            lock.write_text(json.dumps({'pid': holder.pid, 'cwd': 'x', 'at': time.time() - 600}))
            self.assertIn('one gate per Mac', quality.gate_busy(self.root)['reason'], 'a live holder past the Stop budget still holds')
            lock.write_text(json.dumps({'pid': holder.pid, 'cwd': 'x', 'at': time.time() - 3600}))
            self.assertIsNone(quality.gate_busy(self.root), 'a forgotten lock expires')
            holder.kill()
            holder.wait()
            lock.write_text(json.dumps({'pid': holder.pid, 'cwd': 'x', 'at': time.time()}))
            self.assertIsNone(quality.gate_busy(self.root), 'a dead holder does not block')
            self.assertTrue(quality.run_project_gate(self.root)['ok'])
            self.assertTrue(marker.exists())
            self.assertFalse(lock.exists(), 'the lock is released after the run')

    def test_a_dead_holders_orphaned_check_is_killed_before_the_next_gate_starts(self):
        # Lead, 2026-09-27: a Stop killed by Claude Code left its node tree running (8-minute quality-stop-targeted
        # in frankendom-stats). The holder records its check's process group; the next acquire kills it.
        orphan = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
        self.addCleanup(orphan.kill)
        dead = subprocess.Popen([sys.executable, '-c', 'pass'])
        dead.wait()
        (self.root / 'gate.lock').write_text(json.dumps({'pid': dead.pid, 'cwd': 'x', 'at': time.time(), 'pgid': orphan.pid}))
        self.assertTrue(quality.acquire_gate_lock(self.root))
        self.addCleanup(quality.release_gate_lock)
        deadline = time.time() + 5
        while orphan.poll() is None and time.time() < deadline:
            time.sleep(0.05)
        self.assertIsNotNone(orphan.poll(), 'the orphaned check group is killed')
        self.assertEqual(json.loads((self.root / 'gate.lock').read_text())['pid'], os.getpid())

    def test_gate_is_skipped_when_the_checkout_has_no_change_on_top_of_its_base(self):
        # Lead, 2026-09-27: Deploy's checkout on unchanged trunk ran the 9-minute gate at every Stop for nothing.
        # Clean tree AND `git diff --quiet base...HEAD` -> skip. Base = contract "base", else origin/HEAD.
        repo, g = self._repo_with_origin(workflow=False)
        self.assertEqual(quality.unchanged_from_base(repo), 'origin/main')
        with patch.object(stop_orchestrator, 'ci_covered_commit', return_value=('', '')), \
                patch.object(stop_orchestrator, 'run_project_gate') as run_gate, patch.object(stop_orchestrator, 'log'):
            self.assertTrue(stop_orchestrator.run_aaa_gate(repo, 't', session_id='s1'))
            run_gate.assert_not_called()
        (repo / 'a.txt').write_text('2\n')
        self.assertEqual(quality.unchanged_from_base(repo), '', 'a dirty tree is a task-owned change')
        g('commit', '-qam', 'edit')
        self.assertEqual(quality.unchanged_from_base(repo), '', 'a commit on top of the base is a task-owned change')
        g('push', '-q')
        self.assertEqual(quality.unchanged_from_base(repo), 'origin/main', 'pushed and merged into the base: nothing local')
        g('checkout', '-qb', 'feature')
        g('commit', '-qm', 'no tree change', '--allow-empty')
        self.assertEqual(quality.unchanged_from_base(repo), 'origin/main', 'an empty commit changes nothing the gate could test')
        (repo / 'b.txt').write_text('1\n')
        g('add', '.')
        g('commit', '-qm', 'more')
        self.assertEqual(quality.unchanged_from_base(repo), '', 'an unpushed branch commit still runs the gate')
        g('checkout', '-q', 'main')
        contract = json.loads((repo / '.quality-gate.json').read_text())
        for base, expected, why in (('origin/no-such-branch', '', 'an unresolvable base never skips'),
                                    ('origin/main', 'origin/main', 'the contract may name the base')):
            (repo / '.quality-gate.json').write_text(json.dumps({**contract, 'base': base}))
            g('commit', '-qam', f'base {base}')
            g('push', '-q')
            self.assertEqual(quality.unchanged_from_base(repo), expected, why)

    def test_failures_are_never_cached(self):
        repo = self.root / 'repo'
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', '-b', 'main'], cwd=repo, check=True)
        subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-qm', 'init', '--allow-empty'], cwd=repo, check=True)
        (repo / '.quality-gate.json').write_text(json.dumps({'commands': [[sys.executable, '-c', 'raise SystemExit(1)']]}))
        self.assertFalse(quality.run_project_gate(repo)['ok'])
        self.assertFalse(quality.run_project_gate(repo)['ok'])
        cache = self.root / 'gate-cache.json'
        self.assertEqual(json.loads(cache.read_text() or '{}') if cache.exists() else {}, {})

    def test_a_deferred_gate_stays_pending(self):
        # Audit C: a deferred gate returned like a pass, the whole pending queue was cleared.
        one, two = self.root.resolve()/'one', self.root.resolve()/'two'
        outcomes = {str(one): True, str(two): stop_orchestrator.DEFERRED}
        with (
            patch.object(quality, 'PENDING_PATH', self.root/'pending.json'),
            patch.object(stop_orchestrator, 'run_aaa_gate', side_effect=lambda cwd, *a, **k: outcomes.get(str(cwd), True)),
            patch.object(stop_orchestrator, 'is_git_repo', return_value=False),
            patch.object(stop_orchestrator, 'resolve_git_cwd', return_value=self.root),
            patch.object(stop_orchestrator, 'remember_repo'),
            patch.object(stop_orchestrator, 'log'),
            patch('sys.stdin', io.StringIO(json.dumps({'session_id': 's1', 'cwd': str(self.root)}))),
        ):
            for repo in (one, two):
                repo.mkdir()
                (repo/'.quality-gate.json').write_text('{}')
                quality.pending_quality('s1', repo)
            stop_orchestrator.main()
            self.assertEqual(quality.pending_quality('s1'), [str(two.resolve())], 'the deferred repo is still owed a gate')
            outcomes[str(two)] = True
            with patch('sys.stdin', io.StringIO(json.dumps({'session_id': 's1', 'cwd': str(self.root)}))):
                stop_orchestrator.main()
            self.assertEqual(quality.pending_quality('s1'), [])

    def test_project_timeout_is_bounded_and_overridable(self):
        self.contract([[sys.executable, '-c', 'pass']])
        with patch.object(quality.subprocess, 'Popen') as popen:
            proc = popen.return_value.__enter__.return_value
            proc.communicate.return_value = ('', '')
            proc.returncode = 0
            self.assertTrue(quality.run_project_gate(self.root, timeout=500)['ok'])
            self.assertGreater(proc.communicate.call_args.kwargs['timeout'], 410)
            self.assertLessEqual(proc.communicate.call_args.kwargs['timeout'], 420)

            self.root.joinpath('.quality-gate.json').write_text(json.dumps({
                'commands': [[sys.executable, '-c', 'pass']], 'timeout_seconds': 30,
            }))
            self.assertTrue(quality.run_project_gate(self.root, timeout=500)['ok'])
            self.assertLessEqual(proc.communicate.call_args.kwargs['timeout'], 30)

    def test_invalid_project_timeout_fails_closed(self):
        for value in (0, 421, True, 1.5, '420'):
            self.root.joinpath('.quality-gate.json').write_text(json.dumps({
                'commands': [[sys.executable, '-c', 'pass']], 'timeout_seconds': value,
            }))
            with patch.object(quality.subprocess, 'Popen') as popen:
                self.assertFalse(quality.run_project_gate(self.root)['ok'])
                popen.assert_not_called()

    def test_missing_tools_are_not_success(self):
        file = self.root/'valid.py'
        file.write_text('x = 1\n')
        with patch.object(quality, '_tool_exists', return_value=False):
            result = quality.run_quality_checks([file], self.root)
        self.assertFalse(result['ok'])
        self.assertIn('ruff', [r['tool'] for r in result['results'] if not r['ok']])

    def test_project_local_binary_preferred(self):
        binary = self.root/'node_modules/.bin/eslint'
        binary.parent.mkdir(parents=True)
        binary.write_text('test')
        binary.chmod(0o700)
        self.assertEqual(quality.project_tool('eslint', self.root), str(binary))

    def test_mypy_requires_actual_configuration(self):
        project = self.root/'pyproject.toml'
        project.write_text('[project]\nname="test"\n')
        self.assertFalse(quality.has_mypy_config(self.root))
        project.write_text('[tool.mypy]\nstrict=true\n')
        self.assertTrue(quality.has_mypy_config(self.root))

    def test_debugging_policy_reaches_both_injection_and_review(self):
        root = Path(__file__).resolve().parents[1]
        for name in ('session_start.py', 'lib/policies.py', 'lib/audit_prompt.py'):
            text = (root/name).read_text()
            for required in ('.quality-gate.json', 'Hypothesis', 'ast-grep', 'py-spy', 'Tree-sitter', 'Repomix'):
                self.assertIn(required, text, (name, required))

    def test_native_stop_entrypoint_blocks_bad_contract_and_allows_good(self):
        import time
        home = self.root/'home'
        home.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        for agent in ('.codex', '.claude'):
            state = home/agent/'state'
            state.mkdir(parents=True)
            (state/'mimo_last_verdict.json').write_text(json.dumps({
                'gate-test': {'turn_id': 't1', 'verdict': 'PASS', 'updated_at': time.time()}
            }))
        env = dict(os.environ, HOME=str(home), CODEX_HOOK_AUTO_PR='0',
                   CLAUDE_GATE_MAX_LOAD='100000')  # this Mac's load must not defer the gate under test
        payload = json.dumps({'session_id':'gate-test', 'turn_id':'t1', 'cwd':str(self.root),
                              'last_assistant_message':'Checked the project.'})
        for code in (1, 0):
            (self.root / '.quality-gate.json').write_text(json.dumps({
                'commands': [[sys.executable, '-c', 'pass']],
                'completion_commands': [[sys.executable, '-c', f'raise SystemExit({code})']],
            }))
            result = subprocess.run([sys.executable, stop_orchestrator.__file__], input=payload,
                                    capture_output=True, text=True, env=env, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            if code:
                self.assertEqual(json.loads(result.stdout)['decision'], 'block')
                self.assertIn('Repository Quality Gate', result.stdout)
            else:
                self.assertEqual(result.stdout, '')

    def test_gate_runs_without_milestone_or_dirty_tree(self):
        self.contract([[sys.executable, '-c', 'print("checked")']])
        with patch.object(stop_orchestrator, 'log'), patch.object(stop_orchestrator, 'milestone_detected', return_value=False):
            self.assertTrue(stop_orchestrator.run_aaa_gate(self.root, 'plain confirmation'))
        self.contract([[sys.executable, '-c', 'raise SystemExit(1)']])
        with patch.object(stop_orchestrator, 'block') as block, patch.object(stop_orchestrator, 'log'):
            self.assertFalse(stop_orchestrator.run_aaa_gate(self.root, 'done'))
            block.assert_called_once()

    def test_pending_survives_auto_commit_and_covers_multiple_repos(self):
        with patch.object(quality, 'PENDING_PATH', self.root/'state.json'):
            quality.pending_quality('session', self.root/'one')
            quality.pending_quality('session', self.root/'two')
            self.assertEqual(len(quality.pending_quality('session')), 2)
            quality.pending_quality('session', clear=True)
            self.assertEqual(quality.pending_quality('session'), [])

    def test_read_only_dirt_does_not_register_or_checkpoint(self):
        import io
        payload = {'tool_name': 'exec_command', 'cwd': str(self.root),
                   'tool_input': {'cmd': 'git status --short', 'workdir': str(self.root/'project')}}
        with patch.object(post_tool.sys, 'stdin', io.StringIO(json.dumps(payload))), \
             patch.object(post_tool, 'resolve_git_cwd', side_effect=lambda cwd, files: cwd), \
             patch.object(post_tool, 'remember_repo'), patch.object(post_tool, 'log'), \
             patch.object(post_tool, 'is_git_repo', return_value=True), \
             patch.object(post_tool, 'changed_files', return_value=[self.root/'old.py']), \
             patch.object(post_tool, 'run_quality_checks', return_value={'ok': True}) as check, \
             patch.object(post_tool, 'pending_quality') as pending, \
             patch.object(post_tool, 'maybe_checkpoint') as checkpoint:
            post_tool.main()
            pending.assert_not_called()
            checkpoint.assert_not_called()
            self.assertEqual(check.call_args.args[1], (self.root/'project').resolve())

    def test_multi_repo_edits_register_every_repo(self):
        import io
        files = [self.root/'one'/'app.py', self.root/'two'/'app.py']
        payload = {'tool_name': 'Edit', 'cwd': str(self.root), 'tool_input': {}}
        with patch.object(post_tool.sys, 'stdin', io.StringIO(json.dumps(payload))), \
             patch.object(post_tool, 'changed_files_in_payload', return_value=files), \
             patch.object(post_tool, 'git_root', side_effect=lambda path: path), \
             patch.object(post_tool, 'resolve_git_cwd', return_value=self.root), \
             patch.object(post_tool, 'remember_repo'), patch.object(post_tool, 'log'), \
             patch.object(post_tool, 'run_quality_checks', return_value={'ok': True}), \
             patch.object(post_tool, 'pending_quality') as pending, \
             patch.object(post_tool, 'update_checkpoint_state'), patch.object(post_tool, 'maybe_checkpoint'):
            post_tool.main()
            self.assertEqual({call.args[1] for call in pending.call_args_list}, {file.parent for file in files})


if __name__ == '__main__':
    unittest.main()
