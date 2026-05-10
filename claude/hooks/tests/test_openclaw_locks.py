"""Tests for lib.openclaw_locks — session-lock hygiene helpers.

Covers the safe surface (lock cleanup) extensively. The opt-in process
reaper has limited unit coverage because it depends on real ps/pgrep state;
we verify only that it stays disabled by default."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HOOKS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS_DIR))

from lib.openclaw_locks import (  # noqa: E402
    MALFORMED_LOCK_MIN_AGE_SECONDS,
    REAP_ENV_FLAG,
    cleanup_stale_session_locks,
    get_gateway_pids,
    is_pid_alive,
    session_boot_cleanup,
)


class IsPidAliveTests(unittest.TestCase):
    def test_zero_or_negative_returns_false(self):
        self.assertFalse(is_pid_alive(0))
        self.assertFalse(is_pid_alive(-1))

    def test_self_pid_returns_true(self):
        self.assertTrue(is_pid_alive(os.getpid()))

    def test_definitely_dead_pid_returns_false(self):
        # 99999 is conventionally far above any normal pid range; if by chance
        # it exists, we still expect a defined boolean.
        result = is_pid_alive(99999)
        self.assertIsInstance(result, bool)

    def test_permission_error_treated_as_alive(self):
        # On macOS, signaling pid 1 (launchd) raises PermissionError for
        # non-root callers. We deliberately treat that as "alive" so we never
        # unlink another user's lock.
        if os.geteuid() == 0:
            self.skipTest("running as root, PermissionError path not exercised")
        self.assertTrue(is_pid_alive(1))


class CleanupStaleSessionLocksTests(unittest.TestCase):
    def _mk(self, root: Path, agent: str, name: str, body):
        sd = root / agent / "sessions"
        sd.mkdir(parents=True, exist_ok=True)
        p = sd / name
        if isinstance(body, dict):
            p.write_text(json.dumps(body))
        else:
            p.write_text(body)
        return p

    def test_removes_dead_pid_locks_keeps_live(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            stale = self._mk(root, "main", "stale.jsonl.lock", {"pid": 99999})
            live = self._mk(root, "main", "live.jsonl.lock", {"pid": os.getpid()})
            n = cleanup_stale_session_locks(agents_root=root)
            self.assertEqual(n, 1)
            self.assertFalse(stale.exists())
            self.assertTrue(live.exists())

    def test_scans_multiple_agents(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for agent in ("main", "aux", "trident"):
                self._mk(root, agent, "stale.jsonl.lock", {"pid": 99999})
            n = cleanup_stale_session_locks(agents_root=root)
            self.assertEqual(n, 3)

    def test_old_malformed_lock_removed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bad = self._mk(root, "main", "bad.jsonl.lock", "{not json")
            # Force mtime back beyond the malformed-age guard.
            old = time.time() - (MALFORMED_LOCK_MIN_AGE_SECONDS + 5)
            os.utime(bad, (old, old))
            n = cleanup_stale_session_locks(agents_root=root)
            self.assertEqual(n, 1)
            self.assertFalse(bad.exists())

    def test_fresh_malformed_lock_preserved(self):
        # Critical: a malformed lock that was JUST created might be a mid-write
        # race, not a dead writer. We must not unlink it.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bad = self._mk(root, "main", "bad.jsonl.lock", "{not json")
            # mtime stays "now" — well within malformed_min_age window.
            n = cleanup_stale_session_locks(agents_root=root)
            self.assertEqual(n, 0)
            self.assertTrue(bad.exists())

    def test_pid_zero_is_corrupt_treated_as_stale(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            corrupt = self._mk(root, "main", "corrupt.jsonl.lock", {"pid": 0})
            n = cleanup_stale_session_locks(agents_root=root)
            self.assertEqual(n, 1)
            self.assertFalse(corrupt.exists())

    def test_clean_tree_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "main" / "sessions").mkdir(parents=True)
            self.assertEqual(cleanup_stale_session_locks(agents_root=root), 0)

    def test_missing_root_returns_zero(self):
        with tempfile.TemporaryDirectory() as td:
            # never create the agents dir
            self.assertEqual(
                cleanup_stale_session_locks(agents_root=Path(td) / "missing"), 0
            )

    def test_log_fn_invoked_for_each_removal(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._mk(root, "main", "a.jsonl.lock", {"pid": 99999})
            self._mk(root, "main", "b.jsonl.lock", {"pid": 99999})
            calls = []
            n = cleanup_stale_session_locks(
                agents_root=root, log_fn=lambda m: calls.append(m)
            )
            self.assertEqual(n, 2)
            self.assertEqual(len(calls), 2)
            self.assertTrue(all("removed stale lock" in c for c in calls))


class SessionBootCleanupTests(unittest.TestCase):
    def test_reaper_off_by_default(self):
        """Most important safety property: reaper does not fire unless both
        enable_reaper=True AND env var set."""
        with mock.patch("lib.openclaw_locks.reap_orphan_agents") as mreap, \
             mock.patch("lib.openclaw_locks.cleanup_stale_session_locks", return_value=0):
            os.environ.pop(REAP_ENV_FLAG, None)
            session_boot_cleanup()  # default enable_reaper=False
            mreap.assert_not_called()

    def test_reaper_skipped_when_only_arg_set(self):
        with mock.patch("lib.openclaw_locks.reap_orphan_agents") as mreap, \
             mock.patch("lib.openclaw_locks.cleanup_stale_session_locks", return_value=0):
            os.environ.pop(REAP_ENV_FLAG, None)
            session_boot_cleanup(enable_reaper=True)
            mreap.assert_not_called()  # arg alone is insufficient

    def test_reaper_skipped_when_only_env_set(self):
        with mock.patch("lib.openclaw_locks.reap_orphan_agents") as mreap, \
             mock.patch("lib.openclaw_locks.cleanup_stale_session_locks", return_value=0):
            os.environ[REAP_ENV_FLAG] = "1"
            try:
                session_boot_cleanup()  # arg defaults to False
                mreap.assert_not_called()
            finally:
                del os.environ[REAP_ENV_FLAG]

    def test_reaper_runs_when_both_set(self):
        with mock.patch(
            "lib.openclaw_locks.reap_orphan_agents", return_value=2
        ) as mreap, \
             mock.patch("lib.openclaw_locks.cleanup_stale_session_locks", return_value=3), \
             mock.patch("lib.openclaw_locks.get_gateway_pids", return_value=[100]):
            os.environ[REAP_ENV_FLAG] = "1"
            try:
                summary = session_boot_cleanup(enable_reaper=True)
                mreap.assert_called_once()
                self.assertEqual(summary, {"reaped_agents": 2, "removed_locks": 3})
            finally:
                del os.environ[REAP_ENV_FLAG]

    def test_returns_summary_dict(self):
        result = session_boot_cleanup()
        self.assertIsInstance(result, dict)
        self.assertIn("reaped_agents", result)
        self.assertIn("removed_locks", result)

    def test_swallows_internal_exceptions(self):
        with mock.patch(
            "lib.openclaw_locks.cleanup_stale_session_locks",
            side_effect=RuntimeError("boom"),
        ):
            # Must not raise.
            result = session_boot_cleanup()
            self.assertEqual(result, {"reaped_agents": 0, "removed_locks": 0})


class GetGatewayPidsTests(unittest.TestCase):
    def test_returns_list_of_ints(self):
        # We can't assert a specific value (depends on whether the LaunchAgent
        # is currently loaded), but the contract is "list of ints, never raises".
        pids = get_gateway_pids()
        self.assertIsInstance(pids, list)
        for p in pids:
            self.assertIsInstance(p, int)

    def test_returns_empty_on_subprocess_failure(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertEqual(get_gateway_pids(), [])


if __name__ == "__main__":
    unittest.main()
