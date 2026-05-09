from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from unittest import mock


HOOKS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS_DIR))

import mimo_audit  # noqa: E402
import post_tool  # noqa: E402
import session_start  # noqa: E402
import stop_orchestrator  # noqa: E402
import user_prompt_submit  # noqa: E402


class HookEntrypointFailureModeTests(unittest.TestCase):
    def test_malformed_payloads_fail_open_without_crashing_codex(self) -> None:
        hooks = (
            (user_prompt_submit, "log"),
            (session_start, "log"),
            (post_tool, "log"),
            (mimo_audit, "log"),
            (stop_orchestrator, "log"),
        )
        for module, log_name in hooks:
            with self.subTest(hook=module.__name__):
                with (
                    mock.patch("sys.stdin", io.StringIO("{bad json")),
                    mock.patch.object(module, log_name),
                    mock.patch("sys.stdout", io.StringIO()) as stdout,
                ):
                    try:
                        module.main()
                    except SystemExit as exc:
                        self.assertEqual(int(exc.code or 0), 0)
                    self.assertNotIn("Traceback", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
