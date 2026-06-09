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
  periodic mid-task MiniMax-M3 checkpoint audits.
- `Stop / MiniMax-M3 audit`: final MiniMax-M3 review before Codex ends a turn.
- `Stop / orchestrator`: waits for MiniMax pass, runs milestone gates, optional
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

## Secrets

Do not commit secrets. MiniMax credentials should live in one of:

```text
MINIMAX_API_KEY
~/.codex/secrets/minimax_api_key
shell rc export MINIMAX_API_KEY=...
```

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

## Safety Boundary

This repo is the source-of-truth for the global hook code. Project repos should
not vendor or own these hooks. The hooks apply to all Codex projects through the
user-level Codex config.
