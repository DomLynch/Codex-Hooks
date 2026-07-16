from __future__ import annotations

import importlib.util
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "audit_rate_report.py"
SPEC = importlib.util.spec_from_file_location("audit_rate_report", SCRIPT)
audit_rate_report = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(audit_rate_report)


class AuditRateReportTests(unittest.TestCase):
    def test_counts_recent_audit_events(self) -> None:
        lines = [
            "[2026-06-11 10:00:00] MiniMax verdict (iter 1): VERDICT: PASS",
            "[2026-06-11 10:01:00] MiniMax verdict (iter 1): VERDICT: ISSUES",
            "[2026-06-11 10:02:00] ALLOW: chat turn - audit skipped",
            "[2026-06-11 10:03:00] BLOCK_CHAIN_DAMPER: session=s blocks=3",
            "[2026-06-11 10:04:00] RETRY_NETWORK_EXHAUSTED: attempts=2 error=timeout",
            "[2026-06-11 10:05:00] ALLOW: transcript too short (14 chars)",
            "[2026-06-11 10:06:00] mimo checkpoint verdict=PASS transcript=3000",
            "[2026-06-10 09:00:00] MiniMax verdict (iter 1): VERDICT: ISSUES",
        ]

        counts = audit_rate_report.count_events(lines, datetime(2026, 6, 11, 11, 0, 0), 24)

        self.assertEqual(counts["PASS"], 2)
        self.assertEqual(counts["ISSUES"], 1)
        self.assertEqual(counts["SKIP_CHAT"], 1)
        self.assertEqual(counts["DAMPER"], 1)
        self.assertEqual(counts["ERROR"], 1)
        self.assertEqual(counts["TOO_SHORT"], 1)
        self.assertEqual(audit_rate_report.percent(1, 2), "50%")
        self.assertEqual(audit_rate_report.percent(0, 0), "-")

    def test_print_report_outputs_codex_claude_windows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex = Path(tmp) / "codex.log"
            claude = Path(tmp) / "claude.log"
            codex.write_text("[2026-06-11 10:00:00] MiniMax verdict (iter 1): VERDICT: PASS\n")
            claude.write_text("[2026-06-11 10:00:00] ALLOW: chat turn - audit skipped\n")
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                audit_rate_report.print_report(
                    [("codex", codex), ("claude", claude)],
                    datetime(2026, 6, 11, 11, 0, 0),
                )

        self.assertIn("agent window summary PASS ISSUES SKIP_CHAT DAMPER ERROR TOO_SHORT", stdout.getvalue())
        self.assertIn(
            "codex 24h total_events=1 verdicts=1 issue_rate=0% block_rate=0% dampers=0 chat_skips=0 other_skips=0 1 0 0 0 0 0",
            stdout.getvalue(),
        )
        self.assertIn(
            "claude 7d total_events=1 verdicts=0 issue_rate=- block_rate=- dampers=0 chat_skips=1 other_skips=0 0 0 1 0 0 0",
            stdout.getvalue(),
        )

    def test_usage_report_sums_tokens_and_latency(self) -> None:
        lines = [
            "[2026-06-11 10:00:00] MINIMAX_USAGE lane=stop model=MiniMax-M3 input_tokens=100 output_tokens=10 cache_read_tokens=20 cache_write_tokens=0 elapsed_ms=500",
            "[2026-06-11 10:01:00] MINIMAX_USAGE lane=checkpoint model=MiniMax-M3 input_tokens=200 output_tokens=20 cache_read_tokens=30 cache_write_tokens=5 elapsed_ms=900",
        ]

        counts, latencies = audit_rate_report.count_usage(lines, datetime(2026, 6, 11, 11, 0, 0), 24)

        self.assertEqual(counts["calls"], 2)
        self.assertEqual(counts["input"], 300)
        self.assertEqual(counts["output"], 30)
        self.assertEqual(counts["cache_read"], 50)
        self.assertEqual(counts["cache_write"], 5)
        self.assertEqual(latencies, [500, 900])

    def test_damper_does_not_inflate_total_turns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "codex.log"
            log.write_text(
                "[2026-06-11 10:00:00] MiniMax verdict (iter 3): VERDICT: ISSUES\n"
                "[2026-06-11 10:00:01] BLOCK_CHAIN_DAMPER: session=s blocks=3\n"
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                audit_rate_report.print_report([("codex-stop", log)], datetime(2026, 6, 11, 11, 0, 0))

        self.assertIn(
            "total_events=1 verdicts=1 issue_rate=100% block_rate=0% dampers=1",
            stdout.getvalue(),
        )


if __name__ == "__main__":
    unittest.main()
