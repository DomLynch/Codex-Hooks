"""Context-size notice on the Codex side, ported 2026-09-23.

The one thing that had to change in the port is the usage record's shape, and it is the one thing that can
silently halve or double the number, so it is what these tests pin. Codex writes a `token_usage_record` whose
`payload.usage.input_tokens` ALREADY includes `cached_input_tokens`; Claude writes
`message.usage.{cache_read,cache_creation,input}_tokens` as three disjoint figures that must be summed.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

HOOKS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS))
from lib.context_size import URGENT_TOKENS, WARN_TOKENS, context_notice, context_tokens  # noqa: E402


def write(tmp: Path, *records: dict) -> str:
    path = tmp / "rollout.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return str(path)


def codex_record(input_tokens: int, cached: int) -> dict:
    return {"timestamp": "2026-09-23T08:00:00Z", "type": "token_usage_record",
            "payload": {"usage": {"input_tokens": input_tokens, "cached_input_tokens": cached,
                                  "cache_write_input_tokens": 0, "output_tokens": 100,
                                  "total_tokens": input_tokens + 100}}}


class CodexUsageShapeTests(unittest.TestCase):
    def test_cached_tokens_are_not_added_twice(self) -> None:
        """156,370 with 155,136 of it cached is a 156k context, not a 311k one."""
        with TemporaryDirectory() as d:
            path = write(Path(d), codex_record(156_370, 155_136))
            self.assertEqual(context_tokens(path), 156_370)

    def test_the_last_record_wins(self) -> None:
        with TemporaryDirectory() as d:
            path = write(Path(d), codex_record(100_000, 90_000), codex_record(420_000, 410_000))
            self.assertEqual(context_tokens(path), 420_000)

    def test_claude_shaped_records_still_sum_their_three_disjoint_fields(self) -> None:
        with TemporaryDirectory() as d:
            path = write(Path(d), {"message": {"usage": {"input_tokens": 1_000,
                                                         "cache_read_input_tokens": 300_000,
                                                         "cache_creation_input_tokens": 20_000}}})
            self.assertEqual(context_tokens(path), 321_000)

    def test_missing_or_unparseable_transcript_is_zero_never_an_exception(self) -> None:
        self.assertEqual(context_tokens("/nope/does/not/exist.jsonl"), 0)
        with TemporaryDirectory() as d:
            path = Path(d) / "junk.jsonl"
            path.write_text('not json at all\n{"usage": broken\n')
            self.assertEqual(context_tokens(str(path)), 0)


class NoticeTests(unittest.TestCase):
    def test_below_the_threshold_says_nothing(self) -> None:
        with TemporaryDirectory() as d:
            path = write(Path(d), codex_record(WARN_TOKENS - 1, 0))
            line, first = context_notice(path, "/Users/x/Developer/frankendom-deploy")
            self.assertEqual(line, "")
            self.assertFalse(first)

    def test_past_the_threshold_names_the_lane_and_the_size(self) -> None:
        with TemporaryDirectory() as d:
            path = write(Path(d), codex_record(WARN_TOKENS + 50_000, 0))
            line, first = context_notice(path, "/Users/x/Developer/frankendom-deploy")
            self.assertIn("350k", line)
            self.assertIn("docs/state/deploy.md", line)
            self.assertFalse(first)

    def test_coordinating_lanes_get_350k(self) -> None:
        # Dom, 2026-09-24: Lead, Strategy and Deploy at 350k; every other lane stays at WARN_TOKENS.
        with TemporaryDirectory() as d:
            path = write(Path(d), codex_record(320_000, 0))
            self.assertEqual(context_notice(path, "/x/frankendom-deploy"), ("", False))
            self.assertIn("300k one", context_notice(path, "/x/frankendom-weapons")[0])
            path = write(Path(d), codex_record(360_000, 0))
            self.assertIn("350k one", context_notice(path, "/x/frankendom-strategy")[0])

    def test_urgent_threshold_asks_to_be_put_first(self) -> None:
        with TemporaryDirectory() as d:
            path = write(Path(d), codex_record(URGENT_TOKENS + 1, 0))
            _, first = context_notice(path, "/tmp/somewhere")
            self.assertTrue(first)

    def test_it_never_asks_for_a_mid_task_restart(self) -> None:
        """Context is a per-task budget: the notice fires at the task boundary, not in the middle of work."""
        with TemporaryDirectory() as d:
            path = write(Path(d), codex_record(WARN_TOKENS + 1, 0))
            line, _ = context_notice(path, "/tmp/x")
            self.assertIn("When this task closes", line)
            self.assertIn("Do not restart mid-task", line)


if __name__ == "__main__":
    unittest.main()
