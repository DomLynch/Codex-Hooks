"""The Stop-hook judge (`claude -p`) never stacks up on a busy Mac (Strategy + Lead, Dom's authority, 2026-10-14 06:50 Dubai:
Mac load 15-22 on 10 cores all night while every lane's Stop spawned its own judge).

Rule: at most ONE judge process machine-wide (a flock on a state file, released by the kernel if the holder dies, so it can
never go stale) and no judge at all while the 1-minute load is above CLAUDE_JUDGE_MAX_LOAD (default 10). A skipped judge
logs one line and RELEASES the turn unreviewed: raising a normal audit error would block the lane and make it retry,
which is more judges on a saturated Mac.
"""
from __future__ import annotations

import fcntl
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

HOOKS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS_DIR))

import mimo_audit  # noqa: E402
from lib import api, audit_state  # noqa: E402


class JudgeGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lock = Path(self.tmp.name) / "judge.lock"
        for patcher in (mock.patch.object(api, "JUDGE_LOCK_PATH", self.lock),
                        mock.patch.object(api, "claude_binary", return_value="/fake/claude"),
                        mock.patch.object(api, "log")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.logged = api.log

    def _call(self, load: float, run=None):
        run = run or mock.Mock(return_value=(0, "VERDICT: PASS\n", ""))
        with mock.patch.object(api, "judge_load1", return_value=load), mock.patch.object(api, "safe_run", run):
            return api.call_claude_cli("audit this", lane="stop"), run

    def _logged_lines(self) -> list[str]:
        return [str(c.args[-1]) for c in self.logged.call_args_list]

    def test_a_busy_mac_skips_the_judge_with_one_logged_line(self) -> None:
        run = mock.Mock(side_effect=AssertionError("claude must not start while the Mac is over the load line"))
        with self.assertRaises(api.JudgeBusy):
            self._call(10.1, run)
        run.assert_not_called()
        skipped = [m for m in self._logged_lines() if "JUDGE_SKIPPED" in m]
        self.assertEqual(len(skipped), 1)
        self.assertIn("load1=10.1", skipped[0])
        self.assertIn("lane=stop", skipped[0])

    def test_the_load_line_is_inclusive_of_ten_and_configurable(self) -> None:
        out, run = self._call(10.0)
        self.assertEqual(out, "VERDICT: PASS")
        run.assert_called_once()
        with mock.patch.object(api, "JUDGE_MAX_LOAD", 4.0):
            with self.assertRaises(api.JudgeBusy):
                self._call(4.5)

    def test_a_second_judge_is_skipped_while_one_runs(self) -> None:
        holder = open(self.lock, "a+")
        self.addCleanup(holder.close)
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run = mock.Mock(side_effect=AssertionError("a second judge must not start"))
        with self.assertRaises(api.JudgeBusy):
            self._call(0.5, run)
        run.assert_not_called()
        self.assertTrue(any("JUDGE_SKIPPED" in m and "reason=lock" in m for m in self._logged_lines()))

    def test_the_lock_is_held_while_the_judge_runs_and_free_after(self) -> None:
        seen: dict[str, bool] = {}

        def fake_run(command, **kwargs):
            probe = open(self.lock, "a+")
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                seen["held"] = False
            except BlockingIOError:
                seen["held"] = True
            finally:
                probe.close()
            return 0, "VERDICT: PASS\n", ""

        out, _ = self._call(1.0, fake_run)
        self.assertEqual(out, "VERDICT: PASS")
        self.assertTrue(seen["held"], "the judge runs under the lock")
        after = open(self.lock, "a+")
        self.addCleanup(after.close)
        fcntl.flock(after, fcntl.LOCK_EX | fcntl.LOCK_NB)   # raises if the lock leaked

    def test_the_lock_is_freed_when_the_judge_fails(self) -> None:
        with self.assertRaises(RuntimeError):
            self._call(1.0, mock.Mock(return_value=(1, "", "boom")))
        after = open(self.lock, "a+")
        self.addCleanup(after.close)
        fcntl.flock(after, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_a_skipped_judge_is_not_an_audit_error(self) -> None:
        # the Stop hook releases the turn unreviewed: no block, no retry, no error counted
        self.assertTrue(issubclass(api.JudgeBusy, RuntimeError))


class StopHookReleasesWhenJudgeSkippedTests(unittest.TestCase):
    def _run(self, tmp: str, side_effect) -> tuple[dict, mock.Mock, list[str]]:
        entries = [("TOOL CALL", 'Edit: [t] {"file_path": "src/app.py", "old_string": "a", "new_string": "b"}')]
        with (
            mock.patch.object(audit_state, "STATE_FILE", Path(tmp) / "s.json"),
            mock.patch.object(audit_state, "VERDICT_FILE", Path(tmp) / "v.json"),
            mock.patch.object(mimo_audit.ledger, "LEDGER_PATH", Path(tmp) / "l.json"),
            mock.patch.object(mimo_audit, "log") as log,
            mock.patch.object(mimo_audit, "record_lesson"),
            mock.patch.object(mimo_audit, "ask_mimo_with_retry", side_effect=side_effect) as model,
            mock.patch.object(mimo_audit, "latest_task_slice_claude", return_value="x" * 600 + " TOOL CALL edit implemented"),
            mock.patch.object(mimo_audit, "latest_task_entries_claude", return_value=entries),
        ):
            payload = {"session_id": "S", "turn_id": "t", "transcript_path": "/nope", "cwd": tmp, "last_assistant_message": "Done."}
            with mock.patch.object(mimo_audit.sys, "stdin", io.StringIO(json.dumps(payload))):
                buf = io.StringIO()
                with redirect_stdout(buf), self.assertRaises(SystemExit) as exit_info:
                    mimo_audit.main()
        self.assertEqual(exit_info.exception.code or 0, 0)
        out = json.loads(buf.getvalue()) if buf.getvalue().strip() else {}
        return out, model, [c.args[0] for c in log.call_args_list]

    def test_a_skipped_judge_releases_the_turn_instead_of_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(mimo_audit, "block_audit_error", side_effect=AssertionError("a skip is not an audit error")):
            out, model, logs = self._run(tmp, api.JudgeBusy("machine busy: load1=15.6 over 10"))
        self.assertNotEqual(out.get("decision"), "block", "a busy Mac must not block the lane")
        self.assertIn("released UNREVIEWED", json.dumps(out), "the lane is told the turn was released without a review")
        self.assertNotIn("Review limit reached", json.dumps(out), "a skip is not a review limit")
        self.assertIn("load1=15.6", json.dumps(out), "and why")
        self.assertTrue(any("AUDIT_SKIPPED_BUSY" in m for m in logs))
        model.assert_called_once()


if __name__ == "__main__":
    unittest.main()
