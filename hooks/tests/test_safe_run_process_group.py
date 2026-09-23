"""A timed-out command must not leave its children running on the Mac.

WHAT THIS IS NOT: it is not a fix for the two 1,437.9s Stop runs on 2026-09-22. The first theory was that
`subprocess.run(timeout=...)` blocks draining the pipes while a grandchild holds stdout open. That is false on
POSIX and `test_the_stdlib_returns_promptly_even_with_a_live_grandchild` pins it: CPython's `subprocess.run`
kills the direct child and then calls `process.wait()`, not `communicate()`, so it returns at the timeout. The
cause of those two outliers is still unknown.

WHAT IT IS: `subprocess.run` kills only the DIRECT child, so everything that child spawned is orphaned and
keeps running. The Stop path runs `npm run test:changed` (npm -> node), `pytest` (-> xdist workers) and
`go test ./...` (-> test binaries). A timed-out gate therefore leaves a live test suite behind, and on a Mac
shared by a dozen lanes that is exactly the contention the deploy guard exists to prevent — measured
2026-09-21: two 100 s checks took 15 min each under load 30+. Running the child in its own process group and
killing the group on timeout is what makes the timeout mean what it says.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

HOOKS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS))
from lib.git import safe_run  # noqa: E402

GRANDCHILD_DELAY = 3   # long enough to outlive the timeout, short enough to keep the suite quick
TIMEOUT = 1


def spawner(marker: Path) -> list[str]:
    """Parent sleeps past the timeout; a detached grandchild touches the marker after GRANDCHILD_DELAY."""
    return ["bash", "-c", f"(sleep {GRANDCHILD_DELAY}; touch {marker}) & sleep 30"]


class OrphanedGrandchildTests(unittest.TestCase):
    def test_a_timed_out_command_leaves_nothing_running(self) -> None:
        with TemporaryDirectory() as d:
            marker = Path(d) / "grandchild-survived"
            rc, _out, err = safe_run(spawner(marker), timeout=TIMEOUT)
            self.assertEqual(rc, 1)
            self.assertIn("timed out", err)
            time.sleep(GRANDCHILD_DELAY + 1.5)
            self.assertFalse(marker.exists(),
                             "the grandchild outlived the timeout — the process group was not killed")

    def test_the_stdlib_would_have_left_it_running(self) -> None:
        """The behaviour being fixed, pinned so the module docstring cannot rot into folklore."""
        with TemporaryDirectory() as d:
            marker = Path(d) / "grandchild-survived"
            with self.assertRaises(subprocess.TimeoutExpired):
                subprocess.run(spawner(marker), capture_output=True, text=True, timeout=TIMEOUT)
            time.sleep(GRANDCHILD_DELAY + 1.5)
            self.assertTrue(marker.exists(),
                            "expected subprocess.run to orphan the grandchild; if this fails the stdlib changed")

    def test_the_stdlib_returns_promptly_even_with_a_live_grandchild(self) -> None:
        """Disproves the first theory. On POSIX, subprocess.run calls process.wait() after killing the direct
        child — it does NOT drain the pipes — so a live grandchild does not delay it. Whatever caused the two
        1,437.9s Stop runs, it was not this."""
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            subprocess.run(["bash", "-c", "sleep 20 & sleep 20"], capture_output=True, text=True, timeout=1)
        self.assertLess(time.monotonic() - started, 5)


class StillBehavesNormallyTests(unittest.TestCase):
    def test_output_and_exit_code_survive_the_rewrite(self) -> None:
        rc, out, _ = safe_run(["bash", "-c", "echo hello; exit 0"], timeout=10)
        self.assertEqual((rc, out.strip()), (0, "hello"))

    def test_a_failing_command_reports_its_code_and_stderr(self) -> None:
        rc, _out, err = safe_run(["bash", "-c", "echo boom >&2; exit 3"], timeout=10)
        self.assertEqual(rc, 3)
        self.assertIn("boom", err)

    def test_a_missing_binary_is_an_error_not_an_exception(self) -> None:
        rc, _out, err = safe_run(["definitely-not-a-real-binary-xyz"], timeout=5)
        self.assertEqual(rc, 1)
        self.assertTrue(err)

    def test_cwd_and_env_are_still_honoured(self) -> None:
        with TemporaryDirectory() as d:
            rc, out, _ = safe_run(["bash", "-c", "pwd"], cwd=d, timeout=10)
            self.assertEqual(rc, 0)
            self.assertEqual(Path(out.strip()).resolve(), Path(d).resolve())
            rc, out, _ = safe_run(["bash", "-c", "echo $MARKER"], timeout=10,
                                  env={**os.environ, "MARKER": "set-through"})
            self.assertEqual(out.strip(), "set-through")


if __name__ == "__main__":
    unittest.main()
