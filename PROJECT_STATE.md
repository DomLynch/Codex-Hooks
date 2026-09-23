# Project state — Codex Hooks

Updated: 2026-09-23. Shared by Codex and Claude; their settings and private memories remain separate. Record verified changes here with a commit, PR, or check receipt.

## Scope and owners
- This repo versions Codex hooks in `hooks/` and Claude hooks in `claude/hooks/`. Live installs are `~/.codex/hooks/` and `~/.claude/hooks/`.
- No exclusive implementation owner is recorded here. Check the working tree and installed copies before editing either stack.

## Verified local state
- The local `main` checkout was clean at `2f1305c` before the 2026-09-23 block calibration.
- Codex reviewer defaults in `hooks/lib/api.py` are `gpt-6-sol` and `low`; its `codex exec` call passes effort explicitly and ignores normal user config. The installed file showed the same defaults. Claude reviewer settings are separate.
- Recent local audit log entries recorded `model=gpt-6-sol effort=low`. This does not reverify the earlier Mac/VPS smoke or CI reported in the handover.

## Open questions
- Reviewer tokens per call are still unlogged. Repeat the block-value sample before changing reviewer models or effort.
- Claude-to-Codex inbox delivery and the cause of the 12:03–12:04 reviewer timeouts remain unverified.

## Reviewer block calibration — 2026-09-23
- In 31 recent Claude blocks, 8 of 11 model findings led to substantive corrections. Twelve file-reference blocks mostly caused rereads; seven of eight peer-action blocks were rejected with cited standing approval.
- Keep Codex and Claude model settings separate. Claude no longer blocks solely because a final answer names an unopened file. A peer-started merge/deploy with no in-session owner approval is logged for audit, while the normal model review still runs; the Stop hook only sees that action after it happened.
- Failed checks, conflict markers, and unsupported live-response claims still block. Local verification: 195 Codex tests, 201 Claude tests, and Ruff passed.

## Next validation
- Compare repo and both installed hook trees, then run the two test suites and Ruff separately before any hook change. Verify a real reviewer call after any model or effort change.
