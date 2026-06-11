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
            "[2026-06-10 09:00:00] MiniMax verdict (iter 1): VERDICT: ISSUES",
        ]

        counts = audit_rate_report.count_events(lines, datetime(2026, 6, 11, 11, 0, 0), 24)

        self.assertEqual(counts["PASS"], 1)
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

        self.assertIn(
            "agent window total_events verdicts issue_rate chat_skips other_skips PASS ISSUES SKIP_CHAT DAMPER ERROR TOO_SHORT",
            stdout.getvalue(),
        )
        self.assertIn("codex 24h 1 1 0% 0 0 1 0 0 0 0 0", stdout.getvalue())
        self.assertIn("claude 7d 1 0 - 1 0 0 0 1 0 0 0", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
