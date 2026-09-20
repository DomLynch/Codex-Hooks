from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


MODULE = Path(__file__).resolve().parents[1] / "lib/telemetry.py"
SPEC = importlib.util.spec_from_file_location("telemetry_under_test", MODULE)
telemetry = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(telemetry)
DSN = "https://public@example.invalid/1"


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = Path(self.directory.name) / "sentry.json"
        patch = mock.patch.object(telemetry, "CONFIG_PATH", self.config)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.dict(os.environ, {}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def test_no_dsn_is_silent_and_spawns_nothing(self):
        for config in (None, {}, {"environment": "test"}):
            if config is not None:
                self.config.write_text(json.dumps(config))
            stderr = io.StringIO()
            with mock.patch.object(telemetry.subprocess, "run") as run, redirect_stderr(stderr):
                telemetry.report_failure("mimo_audit", "audit_unavailable")
            run.assert_not_called()
            self.assertEqual(stderr.getvalue(), "")

    def test_invalid_config_disables_reporting(self):
        for config in ("broken SECRET", "[]", "null", '{"dsn": 123}',
                       '{"dsn": "http://public@example.invalid/1"}',
                       '{"dsn": "https://public@example.invalid/1", "release": "/private/SECRET"}'):
            self.config.write_text(config)
            with mock.patch.object(telemetry.subprocess, "run") as run:
                telemetry.report_failure("mimo_audit", "audit_unavailable")
            run.assert_not_called()
        with mock.patch.object(Path, "open", side_effect=PermissionError("SECRET")), \
                mock.patch.object(telemetry.subprocess, "run") as run:
            telemetry.report_failure("mimo_audit", "audit_unavailable")
        run.assert_not_called()

    def test_config_and_environment_override_send_only_static_labels(self):
        self.config.write_text(json.dumps({
            "dsn": DSN, "environment": "test", "release": "hooks-1",
            "extra": "SECRET", "username": "SECRET",
        }))
        override = "https://override@example.invalid/2"
        with mock.patch.dict(os.environ, {"HOOK_SENTRY_DSN": override, "SECRET": "private",
                                         "SENTRY_RELEASE": "PRIVATE", "PYTHONPATH": "/private"}), \
                mock.patch.object(telemetry.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            telemetry.report_failure("mimo_audit", "audit_unavailable")
        args, kwargs = run.call_args
        self.assertEqual(args[0][0], str(telemetry.WORKER_PYTHON))
        self.assertEqual(args[0][1:3], ["-I", "-B"])
        self.assertNotIn(override, str(args))
        self.assertEqual(json.loads(kwargs["input"]), {
            "dsn": override, "environment": "test", "release": "hooks-1",
            "component": "mimo_audit", "category": "audit_unavailable",
        })
        self.assertEqual(kwargs["env"], {"PATH": os.defpath})
        self.assertEqual(kwargs["cwd"], "/")
        self.assertLessEqual(kwargs["timeout"], 8)
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        with mock.patch.dict(os.environ, {"HOOK_SENTRY_DSN": ""}), \
                mock.patch.object(telemetry.subprocess, "run") as run:
            telemetry.report_failure("mimo_audit", "audit_unavailable")
        run.assert_not_called()

    def test_normal_findings_and_unapproved_labels_never_spawn(self):
        with mock.patch.dict(os.environ, {"HOOK_SENTRY_DSN": DSN}), \
                mock.patch.object(telemetry.subprocess, "run") as run:
            for component, category in (("mimo_audit", "ISSUES"), ("mimo_audit", "test_failed"),
                                        ("SECRET", "hook_exception"), ("mimo_audit", "SECRET")):
                telemetry.report_failure(component, category)
        run.assert_not_called()

    def test_runtime_failures_and_timeouts_never_raise_or_leak(self):
        outcomes = (FileNotFoundError("SECRET"), RuntimeError("SECRET"),
                    subprocess.TimeoutExpired("SECRET", 1.8, stderr="SECRET"),
                    subprocess.CompletedProcess([], 1, stderr="SECRET"))
        for outcome in outcomes:
            stderr = io.StringIO()
            options = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
            with mock.patch.dict(os.environ, {"HOOK_SENTRY_DSN": DSN}), \
                    mock.patch.object(telemetry.subprocess, "run", **options), redirect_stderr(stderr):
                self.assertIsNone(telemetry.report_failure("mimo_audit", "audit_unavailable"))
            self.assertEqual(stderr.getvalue(), "[agent-hooks telemetry] report unavailable\n")
        with mock.patch.dict(os.environ, {"HOOK_SENTRY_DSN": DSN}), \
                mock.patch.object(telemetry.subprocess, "run", side_effect=OSError("SECRET")), \
                mock.patch.object(telemetry, "print", side_effect=OSError("closed stderr"), create=True):
            telemetry.report_failure("mimo_audit", "audit_unavailable")

    def test_real_worker_timeout_returns_within_budget(self):
        worker = Path(self.directory.name) / "sleep.py"
        worker.write_text("import time\ntime.sleep(30)\n")
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {"HOOK_SENTRY_DSN": DSN}), \
                mock.patch.object(telemetry, "__file__", str(worker)), \
                mock.patch.object(telemetry, "WORKER_PYTHON", Path(sys.executable)), redirect_stderr(stderr):
            start = time.monotonic()
            telemetry.report_failure("mimo_audit", "audit_unavailable")
            elapsed = time.monotonic() - start
        self.assertLess(elapsed, telemetry.WORKER_TIMEOUT + 1)
        self.assertIn("report unavailable", stderr.getvalue())

    def test_worker_invalid_input_fails_closed_without_sdk(self):
        for data in ("not-json SECRET", "[]", '{"component":"SECRET"}'):
            with mock.patch.object(sys, "argv", ["telemetry.py", "--worker"]), \
                    mock.patch.object(sys, "stdin", io.StringIO(data)), \
                    mock.patch.object(telemetry, "_client") as client:
                self.assertEqual(telemetry._worker(), 1)
            client.assert_not_called()

    def test_actual_sdk_transport_sanitizes_and_captures_valid_events(self):
        self.assertTrue(telemetry.WORKER_PYTHON.is_file(), "Run scripts/install-telemetry.sh for SDK tests")
        code = r'''
import importlib.util
import json
import sys
from sentry_sdk.transport import Transport

spec = importlib.util.spec_from_file_location("telemetry", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class MemoryTransport(Transport):
    def __init__(self):
        super().__init__()
        self.envelopes = []
    def capture_envelope(self, envelope):
        self.envelopes.append(envelope)

transport = MemoryTransport()
settings = {"dsn": "https://public@example.invalid/1", "environment": "test", "release": "hooks-1"}
client = module._client(settings, transport)
assert not client.integrations
for key in ("default_integrations", "auto_enabling_integrations", "send_default_pii",
            "include_local_variables", "send_client_reports", "auto_session_tracking",
            "enable_logs", "enable_metrics", "attach_stacktrace"):
    assert client.options[key] is False, key
assert client.options["traces_sample_rate"] == 0
dirty = {
    "message": "SECRET", "server_name": "SECRET", "user": {"email": "SECRET"},
    "request": {"data": "SECRET"}, "breadcrumbs": {"values": [{"message": "SECRET"}]},
    "contexts": {"trace": {"trace_id": "a" * 32}, "device": {"name": "SECRET"}},
    "extra": {"secret": "SECRET"}, "dist": "SECRET", "transaction": "SECRET",
    "fingerprint": ["SECRET"],
    "exception": {"values": [{"type": "ValueError", "value": "SECRET",
        "stacktrace": {"frames": [{"filename": "/SECRET", "vars": {"secret": "SECRET"}}]}}]},
    "tags": {"component": "mimo_audit", "category": "audit_unavailable",
             "agent": "SECRET", "unknown": "SECRET", "synthetic_test": "true"},
}
assert client.capture_event(dirty)
client.close(timeout=0)
event_id = module._send({**settings, "component": "smoke_test", "category": "smoke_test"}, transport)
assert len(transport.envelopes) == 2
for envelope in transport.envelopes:
    event = envelope.get_event()
    assert set(event) == {"event_id", "level", "message", "tags", "environment", "release", "fingerprint"}, event
    assert set(event["tags"]) == {"component", "category", "agent", "synthetic_test"}
    assert event["tags"]["agent"] == module.AGENT
    assert event["message"] == module.MESSAGE and event["level"] == "error"
    assert event["environment"] == "test" and event["release"] == "hooks-1"
    assert event["fingerprint"] == ["agent-hooks", module.AGENT, event["tags"]["component"], event["tags"]["category"]]
    assert len(envelope.items) == 1
    assert "SECRET" not in envelope.serialize().decode()
assert transport.envelopes[1].get_event()["tags"]["category"] == "smoke_test"
assert transport.envelopes[0].get_event()["tags"]["synthetic_test"] == "false"
assert transport.envelopes[1].get_event()["tags"]["synthetic_test"] == "true"
for category in module.CATEGORIES:
    assert module._before_send({"tags": {"component": "mimo_audit", "category": category,
        "synthetic_test": "true"}}, {})["tags"]["synthetic_test"] == str(category == "smoke_test").lower()
assert event_id == transport.envelopes[1].get_event()["event_id"]
assert transport.envelopes[0].get_event()["fingerprint"] != transport.envelopes[1].get_event()["fingerprint"]
assert module._before_send({"tags": {"component": "mimo_audit", "category": "SECRET"}}, {}) is None
print("SDK: two sanitized event envelopes captured; no network transport")
'''
        result = subprocess.run(
            [str(telemetry.WORKER_PYTHON), "-I", "-B", "-c", code, str(MODULE)],
            capture_output=True, text=True, timeout=15, cwd="/", env={"PATH": os.defpath},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("two sanitized event envelopes captured", result.stdout)

    def test_smoke_reports_only_event_id_or_fixed_unavailable_status(self):
        for output, returncode, expected in (("a" * 32 + "\n", 0, 0),
                                             ("SECRET\n", 0, 1), ("SECRET", 1, 1)):
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, {"HOOK_SENTRY_DSN": DSN}), \
                    mock.patch.object(sys, "argv", ["telemetry.py", "--smoke"]), \
                    mock.patch.object(telemetry.subprocess, "run",
                                      return_value=subprocess.CompletedProcess([], returncode, stdout=output)) as run, \
                    redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(telemetry._worker(), expected)
            self.assertEqual(stdout.getvalue(), "a" * 32 + "\n" if expected == 0 else "")
            self.assertNotIn("SECRET", stderr.getvalue())
            self.assertEqual(run.call_args.kwargs["stdout"], subprocess.PIPE)
            payload = json.loads(run.call_args.kwargs["input"])
            self.assertEqual((payload["component"], payload["category"]), ("smoke_test", "smoke_test"))
        for config in (None, "malformed SECRET"):
            if config:
                self.config.write_text(config)
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.object(sys, "argv", ["telemetry.py", "--smoke"]), \
                    mock.patch.object(telemetry.subprocess, "run") as run, \
                    redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(telemetry._worker(), 1)
            run.assert_not_called()
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("not configured", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
