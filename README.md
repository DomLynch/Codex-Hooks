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
  periodic mid-task MiMo checkpoint audits.
- `Stop / MiMo audit`: final MiMo-v2.5-Pro review before Codex ends a turn.
- `Stop / orchestrator`: waits for MiMo pass, runs milestone gates, optional
  Auto-PR, and self-spec chaining.

## Install / Sync To Local Codex

From this repo:

```bash
./scripts/install.sh
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

Do not commit secrets. MiMo credentials should live in one of:

```text
MIMO_API_KEY
~/.codex/secrets/mimo_api_key
shell rc export MIMO_API_KEY=...
```

Runtime feature flags live in:

```text
~/.codex/secrets/hook_env
```

Example:

```text
CODEX_HOOK_AUTO_PR=1
CODEX_HOOK_CLAUDE_AUDIT=0
```

## Verify

```bash
python3 -m py_compile hooks/*.py hooks/lib/*.py hooks/tests/*.py
python3 -m unittest discover -s hooks/tests -p 'test_*.py'
```

## Safety Boundary

This repo is the source-of-truth for the global hook code. Project repos should
not vendor or own these hooks. The hooks apply to all Codex projects through the
user-level Codex config.
