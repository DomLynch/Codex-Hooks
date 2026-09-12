# Optional Sentry Operational Telemetry

Hook entry points remain stdlib-only. Reporting uses a separate Python worker and
is disabled without a DSN. It never changes a hook verdict. Do not report normal
review ISSUES or expected test failures.

## Install the Optional Runtime

Run `sh scripts/install-telemetry.sh` on macOS or Linux with Python 3.9+ and
the Python venv/pip packages available. Set `PYTHON` to a Python executable if needed.
The installer pins sentry-sdk 2.69.1 under
`~/.local/share/agent-hooks/venv`. It does not install hook sources or configure Sentry.

## Enable

Set `HOOK_SENTRY_DSN`, or create `~/.config/agent-hooks/sentry.json` with mode 600:

```json
{
  "dsn": "https://PUBLIC_KEY@YOUR_SENTRY_HOST/PROJECT_ID",
  "environment": "production",
  "release": "agent-hooks"
}
```

Use the actual HTTPS DSN from the approved project; the example is not a live DSN.
Environment/release are explicit non-sensitive labels, at most 64 letters,
digits, dots, underscores or hyphens, starting with a letter or digit. Never put
usernames, paths, prompts or secrets in those labels.
`HOOK_SENTRY_DSN` overrides the file; an explicitly empty value disables reporting.
Missing, unreadable or invalid configuration disables reporting silently.

## Parent Wiring Contract

Call `report_failure(component, category)` only for operational failures.
Components: `session_start`, `user_prompt_submit`, `post_tool`,
`stop_orchestrator`, `mimo_audit`, `autonomy_daemon`, `smoke_test`.
Categories: `audit_unavailable`, `hook_exception`, `smoke_test`.
Unknown labels are ignored. Do not pass exception messages or log lines.

There is no process without a valid configured DSN. The parent uses a 1.8-second
worker timeout, suppresses worker stdout/stderr, and prints only a fixed diagnostic
if the worker fails. There are no telemetry retries.
The DSN travels through stdin, never command arguments. The isolated worker runs
outside the repository with only a minimal PATH environment.

A dedicated SDK Client disables integrations, tracing, sessions, logs, metrics,
client reports and PII/local-variable capture. No global SDK initialization or
exception capture occurs. `before_send` rebuilds the event with only an event ID,
fixed message/level, component/category/agent tags and configured environment/release.
Its fingerprint is exactly `[agent-hooks, agent, component, category]`, keeping
distinct operational failures in separate groups.
Hostname, request, user, breadcrumbs, extra, contexts, stacks and locals are removed.
Transport event IDs/timestamps and ordinary network connection metadata still exist.
Reporting is best-effort; worker success is not proof that Sentry ingested an event.

## Explicit Smoke and Ingestion Verification

After configuring the approved DSN, run
`python3 hooks/lib/telemetry.py --smoke` (or the Claude mirror).
This deliberately emits a controlled `smoke_test` event. Stdout contains only the
32-character event ID; invalid/missing config, SDK failures and timeouts exit
nonzero with fixed, sanitized stderr. Normal `report_failure` has no success return.

An event ID proves local SDK capture, NOT server ingestion: the SDK transport can
fail delivery without raising. Use the Sentry MCP event lookup in the configured
project with that event ID and verify the component/category/agent tags before
reporting ingestion confirmed. Never use a DSN or raw log text as lookup output.

## Offline Verification

```sh
env HOOK_SENTRY_DSN= python3 -B -m unittest discover -s hooks/tests -p test_telemetry.py -v
env HOOK_SENTRY_DSN= python3 -B -m unittest discover -s claude/hooks/tests -p test_telemetry.py -v
```

Keep this empty DSN override for all offline/full-suite runs, even when the home
configuration contains a live DSN. The repository quality gate and CI use the same
override. Telemetry unit tests explicitly isolate and override their test settings.
The actual-SDK tests require the private runtime;
they use an in-memory Transport with an invalid example DSN and never send network
events. Other tests cover disabled reporting, invalid configuration, label rejection,
worker errors, real process timeout and unchanged hook control flow.

Official SDK source: https://github.com/getsentry/sentry-python/tree/2.69.1
