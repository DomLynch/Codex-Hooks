# Project state — Codex Hooks

## Codex delegated handover review — 2 October 2026
- Native incoming Codex chat handovers now reach the reviewer as agent input without user authority. The excerpt retains prior user scope/completion evidence, stays within its existing evidence budget, and preserves later user steering. Ordinary tool output cannot impersonate an incoming handover. No model, timeout, config, dependency or Claude-owned changes.
- One audit found a mid-turn steering ordering edge case and fixed it. New regressions reproduce the old omission; final gate passes 220 Codex tests + 49 subtests and Ruff, including 100 seeded ordering/budget cases. Four real Astra Low Stop replays: both observed false-block handovers PASS; unfinished user work and peer-authorized deployment remain ISSUES. Installed release receipts are in the existing Codex handover.

## Codex safe formatting — 1 October 2026
- Explicit edit targets now receive Ruff formatting and isolated, safe E401 import splitting before the existing checks. Read-only commands, fallback dirty-file discovery and Stop verification do not autofix. Final lint explicitly disables project-configured fix/fix-only modes; unsafe transformations, remaining lint errors, missing tools, deadlines and failing tests still block.
- CodeGraph refresh follows formatting. Reviewer model/config and Claude files are unchanged. One audit caught the shared checker's read-only mutation risk; opt-in formatting and regression coverage close it.
- Validation: new tests failed against the old code; the changed Codex gate passes 214 tests + 47 subtests and Ruff. Coverage includes behavior preservation, untouched neighboring files, idempotence, hostile fix settings, real test failures, formatter errors and deadline exhaustion. Release/install receipts belong in the existing Codex handover.

Updated: 2026-09-30. Shared by Codex and Claude; their settings and private memories remain separate. Record verified changes here with a commit, PR, or check receipt.

## 2026-09-30 16:18 (+04) — Claude hooks HANDOFF. READ FIRST, then the sections below, then memory (next-hooks-todo)
- LIVE: `origin/main` 66c4b73 == `~/.claude/hooks`, 48/48 files byte-identical at 16:18; CI green on 66c4b73. Installed-copy note: `tests/test_enforcement.py::test_this_repo_names_its_ci_check` always fails when run from `~/.claude/hooks` (it reads this repo's `.quality-gate.json`); it passes in the repo, so it is not a regression.
- Went live 2026-09-29/30 (all merged, installed, parity checked):
  - #60 handoff check names app-worktree lanes by their title and finds their memory; lanes now self-clear (Lead, Deploy, Auditer, Combat, Hero Look, Weapons, The Pit and others seen doing it).
  - #62 lane titles skip a leading "The": "Frankendom - The Pit" is lane `pit` (docs/state/pit.md); "Duel" is `duel`.
  - #63 repository gate defers (never blocks) when package.json declares packages and none is installed in the folder or a parent node_modules (Duel/The Pit worktrees looped on `Cannot find package '@eslint/js'`); and defers while Lead's `~/.claude/state/box_slot` ("<owner worktree path> <until ISO local time>") names another worktree; 12 h cap; checked at gate start only.
- NOT LIVE: #64 `claude/deploy-hold-while-present` — `deploy_hold` counts while the file exists (cap 12 h, was 30 min; a paused release at 08:44 let a docs-only Stop run the full node suite to load 94). CI green; waiting for Dom's "merge 64". Deploy re-touches the hold every ~20 min until then (Lead confirmed).
- Rulings: merges of peer-requested hook changes need Dom's words in chat (a peer lane cannot approve). Status tables use `scripts/context_table.py --hours 72` with idle computed from the `--hours 24` list, never by eye.
- Open, in order: (1) merge + install #64 on Dom's OK, then tell Lead; (2) offered, not started: the handoff check's state doc must be "updated today", so a lane over its limit just after midnight is always "NOT ready" (accept "within 12 h" instead); (3) Goblin's "deadline exceeded under load should defer"; Stats gate-root question — both unverified.
- Observed, not hooks: Hero Look, Weapons and The Pit sat over their limit because their state docs were days old (the notice tells them to write it); a self-cleared lane stays dark until someone messages it; 9 lanes idle >24 h at 16:15 (Executioner, Character Main, Veteran, Stats, Multi Chars, Pitborn, Nightborn, Goblin, plus Audio earlier) — Lead's call.
- Session: worktree `.claude/worktrees/cool-goldberg-47393f`; no cron jobs.

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

## Codex Stop latency repair — 2026-09-28
- A 1,206 MiB transcript was repeatedly normalized in full before review; one run spent 105s preparing its excerpt. Read recent request turns backwards and reuse one file snapshot, retaining original requests, steering, prior receipts and review-only continuations. Legacy transcripts retain their message boundaries.
- Removed the obsolete 155s verdict-poll loop. The independent reviewer still owns Stop blocking; the repository quality gate remains. No timeout increase, dependency or persistent state added; production diff is +8 net lines across two files.
- Two review passes: second caught and fixed original-request loss across multiple review-only continuations. Four new regressions replace five obsolete poll tests; 150 randomized histories/600 comparisons match prior output. Final gate: 204 tests + 27 subtests and Ruff pass. Initial reproducer failures and one unused-import lint failure are closed by that run.
- Actual large-file excerpt: 0.054s, ~20 MiB peak RSS; real isolated Stop invocation returns a fresh Astra PASS (6.483s model call). No bulk transcript truncation. Performance/correctness checks precede installation; live release receipt belongs in the Codex handover. Historical Sentry issues remain unresolved.

## Codex reviewer scope repair — 2026-09-28
- Nightborn's skill audit was incorrectly expanded to every installed skill. Stop now retains up to 600 characters of preceding user scope even when the current turn uses tools, within the existing excerpt budget. Old tool receipts remain excluded in that case; explicit current scope changes win.
- The prompt treats ambiguous scope as nonblocking and requires clear evidence of a missing requirement. Two production files, +9 net lines; no new state, dependency, timeout or model change. Added one correction-only rule to the local Codex character skill; no character assets or Claude-owned files changed.
- Audit: 206 Codex tests + 27 subtests, Ruff, skill validation and 200 seeded compaction checks pass. Five Astra Low replays pass expected outcomes: actual Nightborn and ambiguous scope PASS, explicit wider scope and genuine omission ISSUES, explicit scope reduction PASS. Earlier prompt attempts and the tiny-budget scope truncation failed and were corrected; no global noise-rate claim. Release/install receipts are in the Codex handover.

## Codex review fallback cap — 2026-09-28
- Follow-up audit found missing transcript paths bypassed the 4,000-character fallback cap. Removed that guard: one fewer production line; transcript-present scope/request preservation is unchanged.
- Regression exercises the actual Stop entry point with absent, empty and nonexistent paths, with/without tool markers. Old code fails four cases; fixed code passes all six and retains the exact final 4,000 characters. Full Codex gate: 207 tests + 33 subtests and Ruff pass. Post-change audit found no further issue in this patch; installed release receipts belong in the Codex handover.

## Codex completion-only quality contract repair — 2026-09-29
- Pitborn's completion-only contract raised `quality contract: 'commands'` before running any checks. Codex now defaults missing base commands to an empty list and validates the combined list; codex-only contracts also avoid eager access to a missing shared key. Empty/malformed contracts still fail. Two net production lines; Claude-owned code/config and character assets unchanged.
- Audit: the old implementation fails all four new standalone-contract cases; the fix executes all three checks and propagates failure correctly. Five invalid-contract cases reject without execution. Full Codex gate: 209 tests + 42 subtests and Ruff pass. Actual Frankendom-Chars gate passes all three commands, including Pitborn L2–L10 preservation receipts and 90 linked previews; this is verification evidence, not artistic or in-game acceptance. Installed release receipts belong in the Codex handover.

## Codex commentary acknowledgment repair — 2026-10-02
- Stop now reads finding decisions from assistant replies after the latest finding-bearing review, including commentary. It ignores user/tool text, preserves chronological decisions, and does not reuse a stale final answer to clear a reopened finding. Missing-transcript behavior remains unchanged. Nine net runtime lines; reviewer model/config and Claude-owned paths unchanged.
- Regression reproduces the duplicate unanswered block, then verifies 11 explicit cases and 50 seeded event sequences through the actual Stop entry point. Final Codex gate: 221 tests + 110 subtests, Ruff and diff check pass. Audit corrected conflicting-reply ordering before the final gate.
- Actual Auditer turn replay at 2026-10-02 06:57:28Z passes with real Astra Low transport and isolated state. Unanswered findings still block without a model call; acknowledged findings still receive model validation. This closes the reproduced repeat-block mechanism, not a measured global noise rate. Installation receipts belong in the existing Codex handover.
