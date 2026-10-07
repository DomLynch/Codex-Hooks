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


if __name__ == '__main__':
    unittest.main()
