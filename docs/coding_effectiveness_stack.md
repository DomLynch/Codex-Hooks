# Coding Effectiveness Stack

Lean additions for better Codex/Claude codebase understanding without adding more daily agents.

## 1. Codegraph

Primary structural index. Use before grep/read loops for symbols, callers, callees, and impact.

```bash
python3 scripts/codegraph_fleet_status.py --sync
```

If a repo shows `NO INDEX`, run:

```bash
codegraph init -i "/path/to/repo"
```

Session-start hooks now emit a visible warning when a git repo has no `.codegraph/codegraph.db`.

## 2. Serena Pilot

Use Serena only as a pilot when Codegraph misses a real task. Do not install it globally into every repo.

Current upstream quick start:

```bash
uv tool install -p 3.13 serena-agent
serena init
```

Local state as of 2026-07-05:

```text
serena 1.5.3 installed
global config: ~/.serena/serena_config.yml
backend: LSP
Codex MCP: enabled in ~/.codex/config.toml
Claude MCP: enabled in ~/.claude.json
dashboard opening: disabled for both
```

Verified:

```text
Claude MCP: serena connected
Codex MCP command: starts with --context=codex and auto-detects cwd
Codex Desktop: Serena is installed/listed by Codex CLI but not exposed as in-chat tools in this session; reload Codex Desktop or start a fresh session to access the Serena tool namespace
Serena runtime health-check: Codex Hooks, v3, and v4 pass; this proves project indexing/tool health, not current-session Codex tool exposure
Serena indexed: hooks=43 Python files, v3=422, v4=230
```

Pilot protocol:

1. Pick one hard repo: `Research agent bot v4` or `V5 Memo`.
2. Run three real tasks with Codegraph only.
3. Run the same tasks with Serena enabled.
4. Keep Serena only if it reduces localization/tool calls or catches missed impact.

Decision gate:

```text
keep = better localization on >=2/3 tasks and no extra prompt/runtime noise
drop = overlaps Codegraph or adds orchestration drag
```

## 3. Repomix Handoff Packs

Use when handing a bounded repo snapshot to Claude/GPT or an external reviewer.

```bash
python3 scripts/repomix_handoff.py v4
python3 scripts/repomix_handoff.py hooks --budget 60000
```

Outputs go to `/tmp/researka-handoffs/`. Packs are allowlisted and exclude logs, DBs, caches, runs, and generated bulk.

## 4. ast-grep Recipes

Use for structural bug-pattern scans before/after edits.

```bash
python3 scripts/ast_grep_check.py "/Users/domininclynch/Desktop/Business/Research agent bot v4"
```

Current recipes:

- broad `except Exception: pass`
- `subprocess(..., shell=True)`
- unsafe `yaml.load(...)`
- shell `eval`

## What We Are Not Adding

No Goose/OpenHands/Gemini/Qwen daily-agent layer. The bottleneck is context quality and verification, not agent count.

Reviewed but not integrated now:

- SuperClaude / BMAD / large skill packs: useful ideas, too much prompt/process surface for daily hooks.
- Claude Context: overlaps Codegraph and Serena.
- More terminal coding agents: adds orchestration noise before improving localization.
