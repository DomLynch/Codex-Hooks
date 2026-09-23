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

## Claude reviewer timeout mitigation — 2026-09-23
- Claude Stop reviews allow 100 seconds per attempt, retry once after 2 seconds, and have a 240-second outer Stop cap. Three first attempts timed out around 19:49–19:51; none exhausted retries. Cause is unproven.
- The Claude CLI reviewer now starts in safe mode without session persistence. Its review prompt supplies the needed context; this avoids loading user hooks, plugins, MCP and saved session state. Codex reviewer settings are unchanged.
- A direct isolated CLI call returned PASS in 9.2 seconds, and the changed hook transport returned PASS in 5.8 seconds. These smoke checks prove compatibility, not a reduced tail-timeout rate. Compare future live Stop logs before calling the timeout issue resolved.

## Next validation
- Compare repo and both installed hook trees, then run the two test suites and Ruff separately before any hook change. Verify a real reviewer call after any model or effort change.
