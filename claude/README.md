# Claude Code Hooks (port of Codex hooks)

Parallel install of the Codex hook system for Claude Code. Same MiMo audit,
autonomy contract, quality floor, auto-checkpoint, and stop orchestrator —
adapted for Claude Code's payload schema and config format.

## What gets installed

| Event | Script | What it does |
|---|---|---|
| `UserPromptSubmit` | `user_prompt_submit.py` | Injects autonomy contract + V4/MCP routing on every prompt |
| `SessionStart` | `session_start.py` | Loads handoff state, mines backlog, captures session spec |
| `PostToolUse` | `post_tool.py` | Quality floor + auto-checkpoint commits + mid-task MiMo |
| `Stop` | `mimo_audit.py` | MiMo-v2.5-Pro reviews the turn, blocks on issues |
| `Stop` | `stop_orchestrator.py` | Optional 2nd audit (Claude), AAA gate, Auto-PR, self-spec chaining |
| launchd | `autonomy_daemon.py` | Shared Codex/Claude queue planner via `~/.codex/state/` |

## Differences vs the Codex install

1. **Payload adapter** (`lib/claude_adapter.py`) normalizes Claude's stdin format
   to the Codex-shaped payload that the audit logic expects:
   - Synthesizes `turn_id` from session_id + transcript hash
   - Extracts `last_assistant_message` from transcript JSONL
   - Parses Claude's transcript schema (`type=user|assistant|tool_use|tool_result`)
2. **State + logs** live at `~/.claude/state/` and `~/.claude/logs/` — fully
   isolated from the Codex install, so neither can corrupt the other.
3. **MiMo API key** is shared from `~/.codex/secrets/mimo_api_key` (single source
   of truth). Override at `~/.claude/secrets/mimo_api_key` if needed.
4. **Branch naming** uses `claude/<sid>/task-<N>` instead of `codex/<sid>/...`
   so PRs from the two systems don't collide.
5. **Settings file** is JSON at `~/.claude/settings.json` (not TOML). The install
   script merges with any existing hooks rather than replacing.

## Install

```bash
./scripts/install.sh
```

What it does (idempotent, safe to re-run):

1. Copies hook scripts to `~/.claude/hooks/`
2. Creates `~/.claude/state/`, `~/.claude/logs/`, `~/.claude/secrets/`
3. Initializes `~/.claude/secrets/hook_env` with safe defaults
   (Auto-PR off, Claude audit off)
4. Backs up your current `~/.claude/settings.json` to `*.backup-YYYYMMDD-HHMMSS`
5. Merges the new hook blocks into settings.json (preserves your other hooks)
6. Verifies Python compile + JSON validity

## Runtime flags

Edit `~/.claude/secrets/hook_env`:

```bash
CODEX_HOOK_AUTO_PR=0       # 1 = auto commit + push + PR on every passed task
CODEX_HOOK_CLAUDE_AUDIT=0  # 1 = run 2nd-pass audit via Anthropic API
CODEX_HOOK_AUTONOMY=1
CODEX_HOOK_AUTONOMY_LEVEL=standard
```

The autonomy daemon is installed from the repo root with:

```bash
./scripts/install_autonomy_daemon.sh
```

Stop switch:

```bash
touch ~/.codex/state/AUTONOMY_STOP
```

For the Anthropic 2nd audit: drop the key at `~/.claude/secrets/anthropic_api_key`
(chmod 600), set `CODEX_HOOK_CLAUDE_AUDIT=1`.

## Verify

```bash
# Compile-check
python3 -m py_compile claude/hooks/*.py claude/hooks/lib/*.py
python3 -m unittest discover -s claude/hooks/tests -q

# Watch live activity
tail -f ~/.claude/logs/*.log
```

You should see `user_prompt_submit.log` entries on every prompt, `post_tool.log`
on every Edit/Write, `mimo_audit.log` whenever Claude finishes a turn.

## Disable

- Per-hook: comment out the block in `~/.claude/settings.json`
- All: re-run installer (it cleans old install) then delete the appended blocks
- Nuclear: restore `~/.claude/settings.json.backup-*` from before install

## Safety

- Logs to `~/.claude/logs/` only — never modifies user code
- All hooks fail-open: missing MiMo key, network errors, malformed payloads
  all result in "allow stop" rather than blocking Claude
- Auto-PR is OFF by default. Side effects (commit/push/PR) only happen when
  you explicitly flip the flag
