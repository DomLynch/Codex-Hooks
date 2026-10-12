"""Audit A (quality runner): F01 gate lock race, F02 cache identity, F11 detached pipe holder, F17 JS checks."""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path
from unittest.mock import patch

HOOKS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS))
from lib import quality  # noqa: E402

WORKER = """
import sys, time
from pathlib import Path
sys.path.insert(0, {hooks!r})
from lib import quality
quality.GATE_LOCK_PATH = Path(sys.argv[1])
start, release = float(sys.argv[2]), Path(sys.argv[3])
while time.time() < start:
    pass
ok = quality.acquire_gate_lock(Path(sys.argv[4]))
print("OK" if ok else "NO", flush=True)
if ok:
    t = time.time() + 20
    while not release.exists() and time.time() < t:
        time.sleep(0.02)
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, target in (('GATE_CACHE_PATH', self.root / 'gate-cache.json'), ('GATE_LOCK_PATH', self.root / 'gate.lock')):
            p = patch.object(quality, name, target)
            p.start()
            self.addCleanup(p.stop)
        calm = patch.object(quality.os, 'getloadavg', return_value=(5.0, 5.0, 5.0))
        calm.start()
        self.addCleanup(calm.stop)

    def repo(self, commands, **extra):
        repo = self.root / 'repo'
        repo.mkdir(exist_ok=True)
        def g(*a):
            subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', *a], cwd=repo, check=True, capture_output=True)

        g('init', '-q', '-b', 'main')
        (repo / '.quality-gate.json').write_text(json.dumps({'commands': commands, **extra}))
        g('add', '.')
        g('commit', '-qm', 'init')
        return repo


STALE_WORKER = """
import os, sys, time
from pathlib import Path
sys.path.insert(0, {hooks!r})
from lib import quality
lock = Path(sys.argv[1]); root = lock.parent; role = sys.argv[2]
quality.GATE_LOCK_PATH = lock
def until(name, secs=5):
    end = time.time() + secs
    while not (root / name).exists() and time.time() < end:
        time.sleep(0.01)
if role == 'W':   # a waiter paused right after it read the old holder's metadata
    orig, seen = quality._read_lock, []
    def slow():
        data = orig()
        if not seen:
            seen.append(1)
            (root / 'w_read').write_text('x')
            until('go')
        return data
    quality._read_lock = slow
ok = quality.acquire_gate_lock(root)
print(role, 'OK' if ok else 'NO', flush=True)
if ok and role == 'A':
    quality._write_lock(quality._GATE_FD, dict(pid=os.getpid(), cwd='x', at=time.time() - 3600))
    (root / 'a_ready').write_text('x')
    until('a_release')
    quality.release_gate_lock()
    (root / 'a_done').write_text('x')
elif ok:
    time.sleep(2)
"""


class GateLockRaceTests(Base):
    def test_f01_competing_processes_never_both_hold_the_gate_lock(self):
        script = self.root / 'worker.py'
        script.write_text(WORKER.format(hooks=str(HOOKS)))
        dead = subprocess.Popen([sys.executable, '-c', 'pass'])
        dead.wait()
        for round_no in range(5):
            lock, release = self.root / f'lock{round_no}', self.root / f'release{round_no}'
            lock.write_text(json.dumps({'pid': dead.pid, 'cwd': 'x', 'at': time.time()}))
            start = time.time() + 1.5
            procs = [subprocess.Popen([sys.executable, str(script), str(lock), str(start), str(release), str(self.root)],
                                      stdout=subprocess.PIPE, text=True) for _ in range(6)]
            try:
                answers = [p.stdout.readline().strip() for p in procs]
            finally:
                release.write_text('x')
                for p in procs:
                    p.wait(timeout=30)
            self.assertEqual(answers.count('OK'), 1, f'round {round_no}: exactly one process may hold the lock, got {answers}')

    def test_f01_delayed_stale_waiter_cannot_unlink_a_fresh_owner(self):
        """A holds an old-looking lock, waiter W reads it and pauses, A releases, B takes the lock, W resumes:
        W must not delete B's lock file and become a second owner."""
        script = self.root / 'stale.py'
        script.write_text(STALE_WORKER.format(hooks=str(HOOKS)))
        lock = self.root / 'stale.lock'

        def spawn(role):
            return subprocess.Popen([sys.executable, str(script), str(lock), role], stdout=subprocess.PIPE, text=True)

        def until(name):
            end = time.time() + 5
            while not (self.root / name).exists() and time.time() < end:
                time.sleep(0.01)

        a = spawn('A')
        until('a_ready')
        w = spawn('W')
        until('w_read')
        (self.root / 'a_release').write_text('x')
        until('a_done')
        b = spawn('B')
        b_answer = b.stdout.readline().strip()
        (self.root / 'go').write_text('x')
        w_answer = w.stdout.readline().strip()
        for p in (a, b, w):
            p.wait(timeout=20)
        self.assertEqual(b_answer, 'B OK')
        self.assertEqual(w_answer, 'W NO', 'a waiter that read an old holder must not unlink the fresh owner')

    def test_f01_inode_mismatch_after_flock_retries_and_own_group_is_never_killed(self):
        real_stat, calls = os.stat, []

        def stat_once_replaced(path, *a, **k):
            result = real_stat(path, *a, **k)
            if Path(path) == quality.GATE_LOCK_PATH and not calls:
                calls.append(1)
                return os.stat_result((result.st_mode, result.st_ino + 1, *tuple(result)[2:]))
            return result

        with patch.object(quality.os, 'stat', side_effect=stat_once_replaced):
            self.assertTrue(quality.acquire_gate_lock(self.root))
        self.assertEqual(calls, [1], 'the first fd looked replaced, so a second open must have been tried')
        quality.release_gate_lock()
        dead = subprocess.Popen([sys.executable, '-c', 'pass'])
        dead.wait()
        quality.GATE_LOCK_PATH.write_text(json.dumps({'pid': dead.pid, 'cwd': 'x', 'at': time.time(), 'pgid': os.getpgrp()}))
        with patch.object(quality.os, 'killpg') as killpg:
            self.assertTrue(quality.acquire_gate_lock(self.root))
        killpg.assert_not_called()
        quality.release_gate_lock()

    def test_f01_eight_processes_four_hundred_rounds_never_double_own(self):
        script = self.root / 'rounds.py'
        script.write_text(
            'import fcntl, os, sys\nfrom pathlib import Path\nsys.path.insert(0, %r)\nfrom lib import quality\n'
            'quality.GATE_LOCK_PATH = Path(sys.argv[1])\nmarker = Path(sys.argv[2])\nwins = bad = 0\n'
            'for _ in range(400):\n    if quality.acquire_gate_lock(Path(sys.argv[1]).parent):\n        wins += 1\n'
            '        try:\n            fd = os.open(marker, os.O_CREAT | os.O_EXCL)\n            os.close(fd)\n            os.unlink(marker)\n'
            '        except FileExistsError:\n            bad += 1\n        quality.release_gate_lock()\n'
            'print(wins, bad)\n' % str(HOOKS))
        procs = [subprocess.Popen([sys.executable, str(script), str(self.root / 'many.lock'), str(self.root / 'owner')],
                                  stdout=subprocess.PIPE, text=True) for _ in range(8)]
        results = [p.communicate(timeout=120)[0].split() for p in procs]
        self.assertEqual([r[1] for r in results], ['0'] * 8, f'double owners: {results}')
        self.assertGreater(sum(int(r[0]) for r in results), 0)

    def test_f01_holder_diagnostics_and_release_still_work(self):
        self.assertTrue(quality.acquire_gate_lock(self.root))
        self.assertEqual(quality.gate_lock_holder()['pid'], os.getpid())
        quality._note_gate_child(4242)
        self.assertEqual(quality.gate_lock_holder()['pgid'], 4242)
        quality.release_gate_lock()
        self.assertIsNone(quality.gate_lock_holder())
        self.assertTrue(quality.acquire_gate_lock(self.root), 'free again after release')
        quality.release_gate_lock()


class CacheIdentityTests(Base):
    def test_f02_untracked_file_with_a_space_in_its_name_invalidates_the_cache(self):
        check = "import sys; compile(open('new test.py').read(), 'new test.py', 'exec')"
        repo = self.repo([[sys.executable, '-c', check]])
        target = repo / 'new test.py'
        target.write_text('x = 1\n')
        self.assertTrue(quality.run_project_gate(repo)['ok'])
        target.write_text('def broken(:\n')
        self.assertFalse(quality.run_project_gate(repo)['ok'], 'changed contents must not hit a cached pass')

    def test_f02_untracked_symlink_to_a_file_outside_the_tree_invalidates_the_cache(self):
        outside = self.root / 'outside.txt'
        outside.write_text('ok\n')
        check = "import sys; sys.exit(0 if open('link.txt').read() == 'ok\\n' else 1)"
        repo = self.repo([[sys.executable, '-c', check]])
        (repo / 'link.txt').symlink_to(outside)
        self.assertTrue(quality.run_project_gate(repo)['ok'])
        outside.write_text('bad\n')
        self.assertFalse(quality.run_project_gate(repo)['ok'], 'the link target changed outside the tree')
        (repo / 'dirlink').symlink_to(self.root)
        self.assertIsNone(quality._tree_key(repo, [['x']]), 'a directory or dangling link is uncacheable')

    def test_f02_special_oversized_and_tracked_outside_symlink_inputs(self):
        repo = self.repo([[sys.executable, '-c', 'pass']])
        link = repo / 'zero'
        link.symlink_to('/dev/zero')
        started = time.monotonic()
        self.assertIsNone(quality._tree_key(repo, [['x']]), 'a device link is uncacheable')
        self.assertLess(time.monotonic() - started, 5, 'hashing /dev/zero must not hang')
        link.unlink()
        fifo = repo / 'pipe'
        os.mkfifo(fifo)
        with self.assertRaises(OSError):
            quality._file_digest(fifo)   # opens without blocking and refuses a non-regular file
        fifo.unlink()
        big = repo / 'big.bin'
        big.write_bytes(b'x' * 100)
        with patch.object(quality, 'DIGEST_CAP', 50):
            self.assertIsNone(quality._tree_key(repo, [['x']]), 'over the size cap is uncacheable')
        big.unlink()
        outside = self.root / 'tracked-target.txt'
        outside.write_text('ok\n')
        (repo / 'tlink').symlink_to(outside)
        subprocess.run(['git', 'add', 'tlink'], cwd=repo, check=True)
        subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-qm', 'link'], cwd=repo, check=True)
        before = quality._tree_key(repo, [['x']])
        self.assertIsNotNone(before)
        outside.write_text('changed\n')
        self.assertNotEqual(quality._tree_key(repo, [['x']]), before, 'a tracked outside symlink target is an input')

    def test_f02_unreadable_untracked_input_is_a_cache_miss(self):
        repo = self.repo([[sys.executable, '-c', 'pass']])
        (repo / 'secret.txt').write_text('x')
        (repo / 'secret.txt').chmod(0)
        self.addCleanup(lambda: (repo / 'secret.txt').chmod(0o600))
        if os.access(repo / 'secret.txt', os.R_OK):
            self.skipTest('running as a user that can read mode 000 files')
        self.assertIsNone(quality._tree_key(repo, [['x']]))

    def test_f02_no_success_is_cached_when_inputs_change_during_the_run(self):
        repo = self.repo([[sys.executable, '-c', "open('late.txt', 'w').write('made by the check')"]])
        self.assertTrue(quality.run_project_gate(repo)['ok'])
        cache = self.root / 'gate-cache.json'
        self.assertEqual(json.loads(cache.read_text() or '{}') if cache.exists() else {}, {})


class DetachedPipeHolderTests(Base):
    @staticmethod
    def _reap(pidfile):
        try:
            os.kill(int(pidfile.read_text()), signal.SIGKILL)
        except (OSError, ValueError):
            pass

    def _script(self, body):
        path = self.root / 'check.py'
        path.write_text(body)
        return path

    def test_f11_a_detached_descendant_holding_the_pipe_cannot_outlast_the_budget(self):
        pidfile = self.root / 'child.pid'
        script = self._script(
            "import subprocess, sys, time\n"
            f"c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(40)'], start_new_session=True)\n"
            f"open({str(pidfile)!r}, 'w').write(str(c.pid))\n"
            "time.sleep(60)\n")
        repo = self.repo([[sys.executable, str(script)]], timeout_seconds=2)
        self.addCleanup(self._reap, pidfile)
        started = time.monotonic()
        result = quality.run_project_gate(repo)
        self.assertLess(time.monotonic() - started, 12, 'the gate returns by its deadline')
        self.assertTrue(result['ok'], 'a time limit is unproven, not a block')
        self.assertTrue(result['timed_out'])
        self.assertIn('time limit', result['deferred']['reason'])

    def test_f11_a_finished_check_is_not_held_up_by_a_detached_child_on_its_pipe(self):
        pidfile = self.root / 'child2.pid'
        script = self._script(
            "import subprocess, sys\n"
            f"c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(40)'], start_new_session=True)\n"
            f"open({str(pidfile)!r}, 'w').write(str(c.pid))\n"
            "print('check done')\n")
        repo = self.repo([[sys.executable, str(script)]], timeout_seconds=20)
        self.addCleanup(self._reap, pidfile)
        started = time.monotonic()
        result = quality.run_project_gate(repo)
        self.assertLess(time.monotonic() - started, 12)
        self.assertTrue(result['ok'], result)
        self.assertIn('check done', result['results'][0]['output'])


@unittest.skipUnless(shutil.which('node'), 'node not installed')
class TotalBudgetTests(Base):
    """Dom, 2026-10-12: the whole Stop gate, regular `commands` too, ends at GATE_TOTAL_BUDGET_S whatever timeout_seconds says."""

    def test_a_slow_regular_command_is_cut_at_the_total_budget_not_at_timeout_seconds(self):
        repo = self.repo([[sys.executable, '-c', 'import time; time.sleep(30)']], timeout_seconds=420)
        with patch.object(quality, 'GATE_TOTAL_BUDGET_S', 1.5):
            started = time.monotonic()
            result = quality.run_project_gate(repo)
        self.assertLess(time.monotonic() - started, 10)
        self.assertTrue(result['ok'], 'a time limit is unproven, not a block')
        self.assertTrue(result['timed_out'])
        self.assertIn('time limit', result['deferred']['reason'])

    def test_the_total_budget_is_shared_by_all_commands(self):
        py = sys.executable
        repo = self.repo([[py, '-c', 'import time; time.sleep(1.0)'], [py, '-c', 'import time; time.sleep(30)']])
        with patch.object(quality, 'GATE_TOTAL_BUDGET_S', 2.0):
            started = time.monotonic()
            result = quality.run_project_gate(repo)
        self.assertLess(time.monotonic() - started, 10, 'the second command only gets what the first left')
        self.assertTrue(result['timed_out'])

    def test_a_fast_gate_inside_the_budget_still_passes_and_caches(self):
        repo = self.repo([[sys.executable, '-c', 'pass']])
        with patch.object(quality, 'GATE_TOTAL_BUDGET_S', 5.0):
            result = quality.run_project_gate(repo)
        self.assertTrue(result['ok'])
        self.assertNotIn('timed_out', result)


class CompletionBudgetTests(Base):
    """Lead, 2026-10-12: all completion commands share one window; a browser command is never run by the Stop gate."""

    def test_completion_commands_share_one_window_and_the_rest_is_deferred(self):
        py = sys.executable
        repo = self.repo([[py, '-c', 'pass']], completion_commands=[[py, '-c', 'import time; time.sleep(1.2)'],
                                                                    [py, '-c', 'import time; time.sleep(30)']])
        with patch.object(quality, 'COMPLETION_BUDGET_S', 2.0):
            started = time.monotonic()
            result = quality.run_project_gate(repo)
        self.assertLess(time.monotonic() - started, 10, 'the second command is cut at the shared 2 s window, not its own')
        self.assertTrue(result['ok'], 'unproven, not a block')
        self.assertTrue(result['timed_out'])

    def test_a_slow_regular_command_is_not_cut_by_the_completion_window(self):
        py = sys.executable
        repo = self.repo([[py, '-c', 'import time; time.sleep(1.5)']], completion_commands=[[py, '-c', 'pass']])
        with patch.object(quality, 'COMPLETION_BUDGET_S', 0.5):
            result = quality.run_project_gate(repo)
        self.assertTrue(result['ok'])
        self.assertNotIn('timed_out', result)

    def test_browser_completion_commands_are_skipped_not_run_and_not_cached(self):
        py = sys.executable
        marker = self.root / 'ran.txt'
        for cmd in ([py, '-c', f"open({str(marker)!r}, 'w')", 'scripts/sparring-browser-check.mjs'],
                    [py, '-c', f"open({str(marker)!r}, 'w')", '--playwright']):
            repo = self.repo([[py, '-c', 'pass']], completion_commands=[cmd])
            result = quality.run_project_gate(repo)
            self.assertTrue(result['ok'])
            self.assertFalse(marker.exists(), 'the browser command never ran')
            self.assertIn('skipped', result['results'][-1]['output'])
            cache = self.root / 'gate-cache.json'
            self.assertEqual(json.loads(cache.read_text() or '{}') if cache.exists() else {}, {}, 'a skipped check is not a proven pass')
            shutil.rmtree(repo)

    def test_a_browser_word_in_a_regular_command_still_runs(self):
        py = sys.executable
        marker = self.root / 'ran2.txt'
        repo = self.repo([[py, '-c', f"open({str(marker)!r}, 'w')", 'browser-check.mjs']])
        self.assertTrue(quality.run_project_gate(repo)['ok'])
        self.assertTrue(marker.exists(), 'only completion commands are filtered')


class JsQuickCheckTests(Base):
    def test_f17_invalid_js_fails_and_valid_js_passes_with_a_check_executed(self):
        for name in ('bad.js', 'bad.mjs', 'bad.cjs'):
            f = self.root / name
            f.write_text('function (:\n')
            result = quality.run_quality_checks([f], self.root)
            self.assertFalse(result['ok'], name)
        good = self.root / 'good.js'
        good.write_text('const x = 1;\n')
        result = quality.run_quality_checks([good], self.root)
        self.assertTrue(result['ok'])
        self.assertEqual([r['tool'] for r in result['results']], ['node --check'])
        self.assertFalse(result.get('no_checks'))


class NotRunStatusTests(Base):
    """F17 recheck: passed / failed / not_run, with the file and cause named. No test assumes node is installed."""

    def _file(self, name, text='x\n'):
        f = self.root / name
        f.write_text(text)
        return f

    def test_jsx_without_eslint_is_not_run_with_cause(self):
        r = quality.run_quality_checks([self._file('page.jsx', 'const a = <div>;\n')], self.root)
        self.assertEqual(r['status'], 'not_run')
        self.assertTrue(r['ok'])
        self.assertEqual(r['not_run'], [{'file': 'page.jsx', 'cause': '.jsx has no checker (no eslint)'}])
        self.assertIn('page.jsx', r['note'])

    def test_js_without_node_is_not_run_even_beside_a_passing_file(self):
        good, py = self._file('good.js'), self._file('a.py', 'x = 1\n')
        with mock.patch.object(quality.shutil, 'which', return_value=None):
            r = quality.run_quality_checks([good, py], self.root)
        self.assertEqual(r['not_run'], [{'file': 'good.js', 'cause': 'node not on PATH'}])
        self.assertIn('good.js', r['note'])

    def test_ts_without_tsconfig_and_rs_without_cargo_are_not_run(self):
        r = quality.run_quality_checks([self._file('a.ts'), self._file('a.rs')], self.root)
        causes = {i['file']: i['cause'] for i in r['not_run']}
        self.assertEqual(causes['a.rs'], 'no Cargo.toml')
        self.assertIn('a.ts', causes)

    def test_passed_and_failed_controls_do_not_need_ruff(self):
        with mock.patch.object(quality, '_tool_exists', return_value=False):
            self.assertEqual(quality.run_quality_checks([self._file('notes.md')], self.root)['status'], 'passed')
            r = quality.run_quality_checks([self._file('bad.py', 'def (:\n')], self.root)
        self.assertEqual((r['status'], r['ok']), ('failed', False))
        empty = quality.run_quality_checks([], self.root)
        self.assertEqual(empty['status'], 'passed')
        self.assertTrue(empty['no_checks'])

    def test_broken_json_fails_and_valid_json_passes(self):
        self.assertEqual(quality.run_quality_checks([self._file('a.json', '{"a": 1}')], self.root)['status'], 'passed')
        self.assertEqual(quality.run_quality_checks([self._file('b.json', '{"a": ')], self.root)['status'], 'failed')

    def test_commented_tsconfig_and_jsonc_are_not_run_not_failed(self):
        ts = self._file('tsconfig.json', '{\n  // comment\n  "a": 1,\n}\n')
        (self.root / '.vscode').mkdir()
        vs = self.root / '.vscode' / 'settings.json'
        vs.write_text('{"a": 1,}')
        r = quality.run_quality_checks([ts, vs, self._file('x.jsonc', '{/*c*/}')], self.root)
        self.assertEqual(r['status'], 'not_run')
        self.assertTrue(r['ok'])
        self.assertEqual({i['cause'] for i in r['not_run']}, {'JSONC: no checker'})
        self.assertEqual(len(r['not_run']), 3)

    def test_real_run_broken_sh_and_unknown_extension_are_not_run(self):
        sh = self._file('run.sh', 'if then fi (\n')
        odd = self._file('thing.vue', '<template>\n')
        r = quality.run_quality_checks([sh, odd, self._file('README.md')], self.root)
        self.assertEqual(r['status'], 'not_run')
        self.assertEqual({i['file'] for i in r['not_run']}, {'run.sh', 'thing.vue'})
        self.assertIn('no checker for .sh files', r['note'])

    def test_failure_and_not_checked_file_in_one_turn_block_names_both(self):
        import post_tool
        with mock.patch.object(quality, '_tool_exists', return_value=False):
            r = quality.run_quality_checks([self._file('bad.py', 'def (:\n'), self._file('run.sh', 'x\n')], self.root)
        self.assertEqual(r['status'], 'failed')
        text = post_tool.format_quality_failures(r)
        self.assertIn('bad.py', text)
        self.assertIn('run.sh', text)


class AuditTransportCleanupTests(unittest.TestCase):
    def test_f11_cli_transports_kill_a_descendant_when_they_time_out(self):
        from lib import api
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / 'fake-cli.sh'
            fake.write_text('#!/bin/sh\nsleep 30 &\necho $! > "$PIDFILE"\nsleep 30\n')
            fake.chmod(0o755)
            for name, transport in (('claude', 'claude'), ('codex', 'codex')):
                pidfile = Path(tmp) / f'{name}.pid'
                env = {'CLAUDE_AUDIT_BIN': str(fake), 'CODEX_AUDIT_BIN': str(fake), 'PIDFILE': str(pidfile)}
                with patch.dict(os.environ, env), patch.object(api, 'AUDIT_TRANSPORT', transport):
                    with self.assertRaises(TimeoutError):
                        api.call_mimo('prompt', timeout=3)   # 2026-10-08: 0.5s lost the race to shell start-up on loaded CI/Mac (3 red runs)
                self.assertTrue(pidfile.exists(), f'{name}: the fixture never started its descendant')
                pid = int(pidfile.read_text())
                time.sleep(0.2)
                try:
                    os.kill(pid, 0)
                except OSError:
                    continue
                os.kill(pid, signal.SIGKILL)
                self.fail(f'{name}: the descendant {pid} outlived the timed-out transport')

    def _cli_fixture(self, tmp, body):
        fake = Path(tmp) / 'fake-cli.sh'
        fake.write_text('#!/bin/sh\n' + body)
        fake.chmod(0o755)
        return fake

    def _alive(self, pidfile):
        try:
            os.kill(int(pidfile.read_text()), 0)
        except OSError:
            return False
        return True

    def test_f11_grandchild_outliving_the_direct_child_is_killed_promptly(self):
        from lib.git import safe_run
        with tempfile.TemporaryDirectory() as tmp:
            for name, body in (('quiet', 'sleep 30 >/dev/null 2>&1 &\necho $! > "$PIDFILE"\n'), ('holds_pipe', 'sleep 30 &\necho $! > "$PIDFILE"\n')):
                pidfile = Path(tmp) / name
                started = time.monotonic()
                safe_run([str(self._cli_fixture(tmp, body))], env={**os.environ, 'PIDFILE': str(pidfile)}, timeout=20)
                self.assertLess(time.monotonic() - started, 3, f'{name}: must not wait for the grandchild')
                time.sleep(0.2)
                alive = self._alive(pidfile)
                if alive:
                    os.kill(int(pidfile.read_text()), signal.SIGKILL)
                self.assertFalse(alive, f'{name}: grandchild survived')

    def test_f11_sigterm_and_sigint_to_the_hook_leave_no_descendant_on_either_transport(self):
        for transport in ('claude', 'codex'):
            for sig in (signal.SIGTERM, signal.SIGINT):
                with tempfile.TemporaryDirectory() as tmp:
                    pidfile, ready = Path(tmp) / 'pid', Path(tmp) / 'ready'
                    fake = self._cli_fixture(tmp, 'sleep 30 &\necho $! > "$PIDFILE"\nsleep 30\n')
                    code = ('import sys\nfrom pathlib import Path\nsys.path.insert(0, %r)\nfrom lib import api\n'
                            'api.AUDIT_TRANSPORT = %r\nPath(sys.argv[1]).write_text("x")\n'
                            'api.call_mimo("p", timeout=60)\n' % (str(HOOKS), transport))
                    env = {**os.environ, 'CLAUDE_AUDIT_BIN': str(fake), 'CODEX_AUDIT_BIN': str(fake), 'PIDFILE': str(pidfile)}
                    hook = subprocess.Popen([sys.executable, '-c', code, str(ready)], env=env)
                    end = time.time() + 10
                    while not (pidfile.exists() and pidfile.read_text().strip()) and time.time() < end:
                        time.sleep(0.05)
                    hook.send_signal(sig)
                    hook.wait(timeout=10)
                    time.sleep(0.3)
                    alive = self._alive(pidfile)
                    if alive:
                        os.kill(int(pidfile.read_text()), signal.SIGKILL)
                    self.assertFalse(alive, f'{transport}/{sig.name}: descendant survived')

    def test_f11_safe_run_reports_timeout_explicitly(self):
        from lib.git import safe_run
        with self.assertRaises(TimeoutError):
            safe_run(['sleep', '5'], timeout=0.3, raise_on_timeout=True)



if __name__ == '__main__':
    unittest.main()
