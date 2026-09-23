# Project state — Codex Hooks

Updated: 2026-09-23. Shared by Codex and Claude; their settings and private memories remain separate. Record verified changes here with a commit, PR, or check receipt.

## Scope and owners
- This repo versions Codex hooks in `hooks/` and Claude hooks in `claude/hooks/`. Live installs are `~/.codex/hooks/` and `~/.claude/hooks/`.
- No exclusive implementation owner is recorded here. Check the working tree and installed copies before editing either stack.

## Verified local state
- The local `main` checkout was clean at `626aa31` on 2026-09-23.
- Codex reviewer defaults in `hooks/lib/api.py` are `gpt-6-sol` and `low`; its `codex exec` call passes effort explicitly and ignores normal user config. The installed file showed the same defaults. Claude reviewer settings are separate.
- Recent local audit log entries recorded `model=gpt-6-sol effort=low`. This does not reverify the earlier Mac/VPS smoke or CI reported in the handover.

## Open questions
- Measure reviewer tokens per call, extra coding turns after blocks, and useful versus wording-only blocks before changing reviewer settings again.
- Claude-to-Codex inbox delivery and the cause of the 12:03–12:04 reviewer timeouts remain unverified.

## Next validation
- Compare repo and both installed hook trees, then run the two test suites and Ruff separately before any hook change. Verify a real reviewer call after any model or effort change.
