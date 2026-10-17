# Claude Code hooks (guards only)

Slimmed on 2026-10-17 (Dom): the Stop reviewer, prompt-time contracts, session handoff queue, post-edit checks and autonomy
daemon were deleted as bloat. What stays are three `PreToolUse` guards that stop mistakes that cannot be undone.
The old code is in git history (last full version: commit `309583a`).

| Event | Script | What it does |
|---|---|---|
| `PreToolUse` Bash | `deploy_guard.py` | One-deployer rule (heavy commands wait while `deploy.sh` runs), no file writes through Bash (use Edit/Write), no deleting or moving the Playwright cache, one worktree per lane |
| `PreToolUse` Bash/Edit/Write | `live_hooks_guard.py` | Warns when changing the installed `~/.claude/hooks` directly instead of through a PR to this repo |
| `PreToolUse` AskUserQuestion + `mcp__ccd_turn__wrap_up` | `ask_guard.py` | Denies blocking questions to Dom (also a wrap_up that carries `ask`/`actions`); rulings go by message to Lead Dev / Strategy |

`hooks/lib/context_size.py` is kept only because `~/.claude/state/lane_table.py` and `scripts/context_table.py` import it.
`scripts/mac_load_sampler.py` is the Mac load sampler (launchd `com.dom.mac-load-sampler`).

## Install

```bash
./claude/scripts/install.sh
```

Idempotent. Copies the hook files to `~/.claude/hooks/`, backs up `~/.claude/settings.json`, and merges the guard blocks from
`config/settings.example.json` without touching your other hooks. It does not delete files from `~/.claude/hooks/`: retire old
ones by hand.

## Verify

```bash
python3 -m pytest claude/hooks/tests -q -p no:cacheprovider && ruff check claude/hooks
```

## Disable

Remove the block from `~/.claude/settings.json`, or restore `~/.claude/settings.json.backup-*`.
