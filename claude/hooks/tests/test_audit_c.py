"""Audit C: structured evidence must not be shortened and re-parsed to make correctness decisions."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.claude_adapter import entries_from_lines  # noqa: E402
from lib.transcript import unacknowledged_check_failure as gate  # noqa: E402

CWD = "/Users/dom/Desktop/Business/some/very/long/worktree/path/frankendom-payments-service"


def run(tool_id: str, command: str, output: str, is_error: bool | None = None, cwd: str | None = None) -> list[dict]:
    """A native Claude Bash tool_use record followed by its tool_result record."""
    result = {"type": "tool_result", "tool_use_id": tool_id, "content": output}
    if is_error is not None:
        result["is_error"] = is_error
    tool_input = {"command": command, "description": "Run the payment tests with a long description " * 3}
    if cwd:
        tool_input["cwd"] = cwd
    return [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": tool_id, "name": "Bash", "input": tool_input}]}},
        {"type": "user", "message": {"content": [result]}},
    ]


def turn(*runs: list[dict]) -> list[tuple[str, str]]:
    lines = [{"type": "user", "message": {"content": "fix it"}}]
    for r in runs:
        lines.extend(r)
    return entries_from_lines(lines)


class FullCommandEvidenceTests(unittest.TestCase):
    def test_passing_sibling_test_file_does_not_clear_failing_one(self) -> None:
        create = f"cd {CWD} && python3 -m pytest tests/payments/test_create.py -q"
        delete = f"cd {CWD} && python3 -m pytest tests/payments/test_delete.py -q"
        entries = turn(
            run("t1", create, "FAILED tests/payments/test_create.py::test_ok - AssertionError\n1 failed", True),
            run("t2", delete, "3 passed in 0.1s", False),
        )
        self.assertIn("test_create", gate(entries, "Done, all good."))

    def test_same_check_rerun_still_clears(self) -> None:
        create = f"cd {CWD} && python3 -m pytest tests/payments/test_create.py -q"
        entries = turn(
            run("t1", create, "FAILED tests/payments/test_create.py::test_ok - AssertionError\n1 failed", True),
            run("t2", create, "18 passed in 0.1s", False),
        )
        self.assertEqual(gate(entries, "Done."), "")

    def test_evidence_record_keeps_full_command_and_id(self) -> None:
        create = f"cd {CWD} && python3 -m pytest tests/payments/test_create.py -q"
        body = turn(run("toolu_9", create, "1 passed", False))[-1][1]
        self.assertEqual(body.tool_use_id, "toolu_9")
        self.assertIn("test_create.py", body.command)
        self.assertIs(body.is_error, False)


class NativeFailureStatusTests(unittest.TestCase):
    def test_ruff_f821_with_is_error_is_a_failure(self) -> None:
        out = "Exit code 1\nsrc/app.py:3:5: F821 Undefined name `foo`\nFound 1 error."
        entries = turn(run("r1", "ruff check src", out, True))
        self.assertNotEqual(gate(entries, "Done."), "")

    def test_node_test_timeout_with_is_error_is_a_failure(self) -> None:
        out = "Exit code 1\n# tests 1\n# cancelled 1\ntest timed out after 30000ms"
        entries = turn(run("n1", "node --test test/", out, True))
        explained = gate(entries, "Done.")
        self.assertIn("timed out", explained)

    def test_non_error_run_without_failure_words_is_fine(self) -> None:
        entries = turn(run("r1", "ruff check src", "All checks passed!", False))
        self.assertEqual(gate(entries, "Done."), "")

    def test_intentional_negative_checks_do_not_block(self) -> None:
        for cmd in ("! ruff check bad.py", "pytest tests/test_x.py || true", "cd src && ! ruff check bad.py"):
            entries = turn(run("x1", cmd, "Exit code 1\nsomething", True))
            self.assertEqual(gate(entries, "Done."), "", cmd)

    def test_non_runner_command_failure_is_not_a_check_failure(self) -> None:
        entries = turn(run("g1", "grep -q foo file", "Exit code 1", True))
        self.assertEqual(gate(entries, "Done."), "")


FAIL = "Exit code 1\nFAILED tests/test_api.py::test_one - AssertionError"
PASS = "1 passed in 0.01s"


class CheckIdentityTests(unittest.TestCase):
    """Rule: a check is identified by WHERE it ran as well as what it ran. A pass clears a failure only from the
    same effective directory; an unknown or different directory clears nothing (false block, never false clearance)."""

    def test_a_pass_in_another_directory_does_not_clear_a_failure(self) -> None:
        for runs in (
            (run("a", "cd /tmp/a && pytest tests/test_api.py -q", FAIL, True), run("b", "cd /tmp/b && pytest tests/test_api.py -q", PASS, False)),
            (run("a", "pytest tests/test_api.py -q", FAIL, True, cwd="/tmp/a"), run("b", "pytest tests/test_api.py -q", PASS, False, cwd="/tmp/b")),
            (run("a", "cd /tmp/a && pytest tests/test_api.py", FAIL, True), run("b", "pytest tests/test_api.py", PASS, False, cwd="/tmp/b")),
            (run("a", "cd /tmp/a && pytest tests/test_api.py", FAIL, True), run("m", "cd /tmp/b", "", False), run("b", "pytest tests/test_api.py", PASS, False)),
            (run("a", "ssh h 'cd /srv/a && pytest tests/test_api.py'", FAIL, True), run("b", "cd /tmp/b && pytest tests/test_api.py", PASS, False)),
            # two DIFFERENT unknown places are not one place (relative, variable and subshell targets)
            (run("a", "cd frontend && pytest tests/test_api.py", FAIL, True), run("b", "cd backend && pytest tests/test_api.py", PASS, False)),
            (run("a", "cd $A && pytest tests/test_api.py", FAIL, True), run("b", "cd $B && pytest tests/test_api.py", PASS, False)),
            (run("a", "(cd /tmp/a && pytest tests/test_api.py)", FAIL, True), run("b", "(cd /tmp/b && pytest tests/test_api.py)", PASS, False)),
            (run("a", "(cd /tmp/a && pytest tests/test_api.py)", FAIL, True), run("b", "pytest tests/test_api.py", PASS, False)),
        ):
            self.assertNotEqual(gate(turn(*runs), "Done."), "", runs[0][0]["message"]["content"][0]["input"]["command"])

    def test_the_same_directory_still_clears_however_it_is_spelled(self) -> None:
        for runs in (
            (run("a", "cd /tmp/a && pytest tests/test_api.py -q", FAIL, True), run("b", "cd /tmp/a/ && pytest tests/test_api.py -q", PASS, False)),
            (run("a", "cd /tmp/a && pytest tests/test_api.py", FAIL, True), run("b", "pytest tests/test_api.py", PASS, False, cwd="/tmp/a")),
            (run("a", "pytest tests/test_api.py", FAIL, True), run("b", "pytest tests/test_api.py", PASS, False)),
            # the Bash shell keeps its directory: a rerun after `cd /tmp/a &&` needs no cd of its own
            (run("a", "cd /tmp/a && pytest tests/test_api.py", FAIL, True), run("b", "pytest tests/test_api.py", PASS, False)),
            # a variable or relative-without-base target is unknown, never an accumulating guess: repeating it matches itself
            (run("a", "cd $PROJ && pytest tests/test_api.py", FAIL, True), run("m", "cd $PROJ", "", False), run("b", "cd $PROJ && pytest tests/test_api.py", PASS, False)),
        ):
            self.assertEqual(gate(turn(*runs), "Done."), "", runs[0][0]["message"]["content"][0]["input"]["command"])


class NegativePreconditionTests(unittest.TestCase):
    """Rule: only a check that is itself negated or `|| true` is exempt; an unrelated negative precondition earlier in
    the same command cannot hide that the later check failed."""

    def test_a_negative_precondition_does_not_hide_a_later_failed_check(self) -> None:
        for cmd in ("test ! -f missing.flag && ruff check .", "false || true; ruff check .", "! test -f x && pytest tests/test_x.py",
                    "cd src; test ! -d build && node --test t.mjs"):
            entries = turn(run("x1", cmd, "Exit code 1\nsomething", True))
            self.assertNotEqual(gate(entries, "Done."), "", cmd)

    def test_the_negated_check_itself_is_still_exempt(self) -> None:
        for cmd in ("! ruff check bad.py", "pytest tests/test_x.py || true", "cd src && ! ruff check bad.py", "test ! -f a && ! ruff check bad.py"):
            entries = turn(run("x1", cmd, "Exit code 1\nsomething", True))
            self.assertEqual(gate(entries, "Done."), "", cmd)


if __name__ == "__main__":
    unittest.main()
