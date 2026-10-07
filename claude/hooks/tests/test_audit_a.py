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
        self.assertFalse(result['ok'])
        self.assertIn('deadline', result['results'][-1]['output'])

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

    def test_f17_no_applicable_checks_is_reported_as_such(self):
        f = self.root / 'page.jsx'
        f.write_text('const a = <div/>;\n')
        result = quality.run_quality_checks([f], self.root)
        self.assertTrue(result['ok'])
        self.assertTrue(result['no_checks'])
        self.assertIn('no checks', result['note'].lower())
        self.assertTrue(quality.run_quality_checks([], self.root)['no_checks'])


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
                        api.call_mimo('prompt', timeout=0.5)
                self.assertTrue(pidfile.exists(), f'{name}: the fixture never started its descendant')
                pid = int(pidfile.read_text())
                time.sleep(0.2)
                try:
                    os.kill(pid, 0)
                except OSError:
                    continue
                os.kill(pid, signal.SIGKILL)
                self.fail(f'{name}: the descendant {pid} outlived the timed-out transport')


if __name__ == '__main__':
    unittest.main()
