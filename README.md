# Codex Hooks

Global Codex hook system for Dom Lynch's local Codex app.

This repo versions the hook scripts, shared hook library, tests, and sanitized
config templates. It intentionally does not include secrets, runtime state,
logs, trusted hashes, or full local Codex app config.

## What Is Installed

- `UserPromptSubmit`: injects current-prompt discipline, autonomous execution,
  and V4/MCP routing guidance.
- `SessionStart`: injects session handoff and project baseline context.
- `PostToolUse`: runs quality checks after tool use, checkpoint commits, and
  periodic mid-task GPT-5.6-Terra checkpoint audits.
- `Stop / GPT-5.6-Terra audit`: fresh read-only Codex review before a turn ends.
- `Stop / orchestrator`: waits for the audit pass, runs milestone gates, optional
  Auto-PR, and self-spec chaining.
- `Autonomy daemon`: launchd job that keeps a shared task queue warm for
  Codex/Claude long-horizon runs using local repo mining only.

## Install / Sync To Local Codex

From this repo:

```bash
./scripts/install.sh
./scripts/install_autonomy_daemon.sh
```

Then ensure `~/.codex/config.toml` includes the hook blocks from:

```text
config/hooks.example.toml
```

The active local install path is:

```text
~/.codex/hooks/
```

The audit uses the bundled Codex CLI and your existing ChatGPT login. Override
the defaults with `CODEX_AUDIT_MODEL`, `CODEX_AUDIT_EFFORT`, or
`CODEX_AUDIT_BIN` when needed.

Runtime feature flags live in:

```text
~/.codex/secrets/hook_env
```

Example:

```text
CODEX_HOOK_AUTO_PR=1
CODEX_HOOK_CLAUDE_AUDIT=0
CODEX_HOOK_AUTONOMY=1
CODEX_HOOK_AUTONOMY_LEVEL=standard
```

Stop switch:

```bash
touch ~/.codex/state/AUTONOMY_STOP
```

## Verify

```bash
python3 -m py_compile hooks/*.py hooks/lib/*.py hooks/tests/*.py
python3 -m unittest discover -s hooks/tests -p 'test_*.py'
tail -f ~/.codex/logs/autonomy_daemon.log
```

## Coding Effectiveness Utilities

Optional tools for better Codex/Claude codebase understanding without adding
more daily agents:

```bash
python3 scripts/codegraph_fleet_status.py --sync
python3 scripts/repomix_handoff.py hooks --budget 60000
python3 scripts/ast_grep_check.py .
```

See `docs/coding_effectiveness_stack.md`.

## Safety Boundary

This repo is the source-of-truth for the global hook code. Project repos should
not vendor or own these hooks. The hooks apply to all Codex projects through the
user-level Codex config.
# Repository quality contracts

Both Stop adapters run a repository's `.quality-gate.json` commands as argv
lists. An empty/invalid contract, missing executable, nonzero exit or timeout
blocks completion. Explicit edited repositories remain pending across
checkpoints; all pending contracts share a 120-second budget. Stop registration
allows 360 seconds for that budget, the reviewer wait and orchestration.
PostToolUse has a shared 120-second check budget and a 180-second registration.

Each new edited repository needs its own contract; do not copy Python commands
into a TypeScript project. Project-local binaries take precedence. Baselines
freeze existing complexity/duplication, not new regressions. Dead-code and
mock-density reports need human assessment before deleting anything.

Semble discovery and CodeGraph impact analysis remain mandatory in the existing
instructions/reviewer, with the documented unavailable/index-free fallback.
This is not deterministic proof every tool was used: wrapped tool output is not
an authenticated invocation record. Profiles, mutation tests and state-machine
tests apply to the relevant failure, not every edit. Hooks do not prevent direct
shell edits, disabled hooks or repository owners weakening configuration;
required CI branch checks are the merge boundary.

## Finding closure

SessionStart and UserPromptSubmit inject the same finding-closure policy for
Codex and Claude; Stop reviews it. Current-task failures require a matching
passing rerun or an evidence-based expected/unrelated disposition. Reporting,
delegation, unrelated green tests or closing a Sentry issue do not resolve them.
Status/audit-only questions, explicit pauses and concrete external blockers
remain exceptions; bounded reviewer exhaustion is unresolved, never PASS.

Put repeatable, non-destructive workflow/browser checks in the optional
`completion_commands` argv-list array in `.quality-gate.json`. Stop runs
`commands` followed by `completion_commands`, within the existing deadline;
either failing blocks. The shared installer cannot invent application-specific
tests: each project's developer owns those commands and regressions. Manual
descriptions are not executed. Do not put publication, paid calls or production
mutations in automatic checks.

The bounded reviewer slice preserves middle-of-turn failure excerpts as
CHECK HISTORY, including errors inside long tool output. These are evidence,
not a deterministic verdict: expected negative tests and successful reruns must
be assessed in context. No new daemon or cross-project failure backlog is added.
