"""Audit C: structured evidence must not be shortened and re-parsed to make correctness decisions."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.claude_adapter import entries_from_lines  # noqa: E402
from lib.transcript import unacknowledged_check_failure as gate  # noqa: E402

CWD = "/Users/dom/Desktop/Business/some/very/long/worktree/path/frankendom-payments-service"


def run(tool_id: str, command: str, output: str, is_error: bool | None = None) -> list[dict]:
    """A native Claude Bash tool_use record followed by its tool_result record."""
    result = {"type": "tool_result", "tool_use_id": tool_id, "content": output}
    if is_error is not None:
        result["is_error"] = is_error
    return [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": tool_id, "name": "Bash",
             "input": {"command": command, "description": "Run the payment tests with a long description " * 3}}]}},
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
        for cmd in ("! ruff check bad.py", "pytest tests/test_x.py || true", "test ! -f out.txt && ruff check ."):
            entries = turn(run("x1", cmd, "Exit code 1\nsomething", True))
            self.assertEqual(gate(entries, "Done."), "", cmd)

    def test_non_runner_command_failure_is_not_a_check_failure(self) -> None:
        entries = turn(run("g1", "grep -q foo file", "Exit code 1", True))
        self.assertEqual(gate(entries, "Done."), "")


if __name__ == "__main__":
    unittest.main()
