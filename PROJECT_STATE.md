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
- Keep Codex and Claude model settings separate. Claude no longer blocks solely because a final answer names an unopened file. A peer-started merge/deploy with no in-session owner approval blocks again (restored at Dom's request on 2026-09-23 after being logged-only); the lane answers with `[Fn] rejected: <owner's words>` when standing approval exists.
- Failed checks, conflict markers, and unsupported live-response claims still block. Local verification: 195 Codex tests, 201 Claude tests, and Ruff passed.

## Claude reviewer timeout mitigation — 2026-09-23
- Claude Stop reviews allow 100 seconds per attempt, retry once after 2 seconds, and have a 240-second outer Stop cap. Three first attempts timed out around 19:49–19:51; none exhausted retries. Cause is unproven.
- The Claude CLI reviewer now starts in safe mode without session persistence. Its review prompt supplies the needed context; this avoids loading user hooks, plugins, MCP and saved session state. Codex reviewer settings are unchanged.
- A direct isolated CLI call returned PASS in 9.2 seconds, and the changed hook transport returned PASS in 5.8 seconds. These smoke checks prove compatibility, not a reduced tail-timeout rate. Compare future live Stop logs before calling the timeout issue resolved.

## Next validation
- Compare repo and both installed hook trees, then run the two test suites and Ruff separately before any hook change. Verify a real reviewer call after any model or effort change.

## Account switch and VPS sync — 2026-09-25
- The Claude reviewer runs the terminal `claude` CLI on its keychain login; `hook_env` holds no token. The old account hit its weekly limit, so every Stop review failed. `claude auth login` to the new account fixed it: 100 reviews from 18:52 to 19:47 +04 with no errors (`~/.claude/logs/claude_audit.log`, model `claude-opus-5-5`, effort `low`).
- Mac live installs match the repo; both suites pass from the main checkout (Codex 210, Claude 232). Run from a worktree or the live folder, three tests fail because they expect repo-relative paths.
- VPS `/root/.codex/hooks` synced to 15ab573 (backup `/root/hooks-backup-20260925/`); 209 of 210 tests pass, and the one failure is the repo-path test above.
- `install.sh` now seeds `claude-opus-5-5`, and the example Stop timeout for `stop_orchestrator.py` is 960 s, matching the budget in `stop_orchestrator.py`.

## Evening 2026-09-25 — open items for the next hooks session
- Context limits (PR #51, 2026-09-27, live in `~/.claude/hooks`): every lane 500k, Lead 750k, Strategy 700k, Deploy 600k, "put first" at 800k. Lanes run in desktop worktrees whose folders name no lane, so the notice reads the session title (`customTitle`). Table: `python3 scripts/context_table.py`. Codex untouched on purpose (Dom: Claude only).
- OPEN, VPS: running the Claude hook suite as root on the VPS (19:54 and 19:56 +04) killed processes server-wide: nginx, postgresql@16-main, the OpenCode GitHub runner, polkit, multipathd, unattended-upgrades and systemd's own units. nginx and the knowledge health check were restarted; postgresql@16-main, actions.runner.DomLynch-OpenCode.brain-opencode, polkit and multipathd were still failed at the last check and need `systemctl start` (the auto-mode classifier blocked the restart; ask Dom). Cause not found: no broad kill in the hook code (only `lib/git.py:28`, `lib/quality.py:242` kill process groups). Do NOT run the Claude suite as root on the VPS until it is.
- VPS Claude install is current (hook_env Opus 5.5 low, CLI 2.1.282, backup `/root/hooks-backup-20260925/`), but its CLI is still on the old account. Dom: no VPS reviewer needed; Claude Code runs on the Mac.
- Reviewer since the account switch: 93.5% pass, 5% model flags, 0 timeouts, 5 s median; lanes fixed 6 of 11 findings. Re-run `scripts/reviewer_scorecard.py` with a day of data.
- Frankendom deploys (logs `~/Developer/deploy-<sha>.log`, success = `Published <sha>`): ~10 min each on a quiet Mac; batching took the cost from ~4.6 to ~2.1 min per PR. Slowness past ~15 min is load from other lanes' browser runs, not a hang; the 50-min ceiling catches real hangs.

## Reviewer: plan-item and progress claims — 2026-09-26
- Strategy's 06:07 list called SCOPE item 8 "done and live" with 2 of 10 moves built, and 06:23 said three moves were "being built" on the strength of its own order to Lead; the reviewer passed both. It sees one ~4k-char turn and had no view of the scope doc.
- `AUDIT_PROMPT` (Claude only) now flags: a numbered plan item called done/live unless the turn reads its definition and checks every part; and "being built"/firm dates backed only by the lane's own outgoing order.
- Replay on the real turns: "being built" flagged 4/4, "special move done" 3 of 5 runs after adding a checklist pointer (0/4 without). Eight other lanes' latest turns: no new blocks against the old prompt. Findings survive the void/anchor filters.

## Deploy guard and gate deferral — 2026-09-26
- PR #16: the Stop gate re-reads `deploy_in_flight.json` every 5 s and between commands; a deploy that starts mid-gate kills the check's process group and defers (no cached pass). The guard also blocks `quality-stop-targeted.mjs`, `npm test`, `npm run test:browser`.
- The guard now reads only the text a shell line runs (`executable_text`): commit messages, PR bodies and heredocs written to files are ignored; `bash -c`, `eval`, `bash <<EOF`, `$(...)` and backticks are still checked, and the `scripts/deploy.sh` exception only applies when it is actually run. Replay of 5,493 lane commands from 24 h: 87 fewer false blocks, 0 new blocks.
- Codex `hooks/deploy_guard.py` is not changed (Claude only).

## Codex safety repair — 2026-09-27
- Codex hooks validate and review only: automatic checkpoint commits, push/PR creation and Self-Spec task chaining were removed. Requested Git/deploy work remains an explicit agent action.
- Stop and checkpoint reviews preserve the complete current user request separately from compacted evidence, including empty-response retries. Keyword-based advisory suppression was removed; quote anchoring remains.
- Post-tool CodeGraph updates require a verified Git root. The Codex quality/CI command uses pytest so plain test functions are collected. Claude source, configuration and installed hooks are unchanged.
- Validation: 200 Codex tests plus 23 subtests passed twice, including the final run with Astra Low overrides; Ruff passed. Four regression tests cover unrelated staged work, read-only/task scope, request preservation/retry, actual blocking and non-repository indexing.
- Second review found and fixed the empty-response retry truncation. A fresh Astra Low call saw the complete long request and returned ISSUES for zero of 18 requested tasks created.
- Follow-up: the live Codex Stop gate still selected the shared Claude suite, whose model assertion inherited Codex's Astra override. Added an optional `codex_commands` contract override, retaining shared completion checks and leaving the original shared commands and Claude files unchanged.

## Codex reviewer calibration — 2026-09-28
- Review excerpts now group the original request and steering by Codex turn ID. Fresh turns reset scope; non-review callers keep their existing boundary. Stop/checkpoint prompts require concrete defects and matching resource/revision evidence, with equivalent wording accepted.
- Two review passes: request/retry regressions, 100 seeded boundary sequences, unchanged non-review output and redaction; Codex gate passes 205 tests + 27 subtests and Ruff under Astra Low. Initial regression failures were expected and now pass.
- Eight paired Astra Low replays: baseline 7/8, changed 8/8. The lost-request miss now blocks; five nonblocking cases pass and all three real defects block. This small sample does not establish a lower live noise rate or token savings. Replay evidence: `/tmp/codex-noise-replay-results.json`.
- The optional second reviewer was disabled in Codex-owned VPS settings; its configured branch makes no external reviewer call. Claude-owned files/settings were not changed. Deploy-guard command matching remains a separate open fix; historical Sentry issues remain unresolved.
