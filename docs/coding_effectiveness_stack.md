# Coding Effectiveness Stack

Lean additions for better Codex/Claude codebase understanding without adding more daily agents.

## 1. Semble

Primary code search. Find code by what it does before grep/read loops, then read the file at the returned line. If you skip Semble, say why.

## 2. Serena Pilot

Use Serena only as a pilot when Semble misses a real task. Do not install it globally into every repo.

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
2. Run three real tasks with Semble only.
3. Run the same tasks with Serena enabled.
4. Keep Serena only if it reduces localization/tool calls or catches missed impact.

Decision gate:

```text
keep = better localization on >=2/3 tasks and no extra prompt/runtime noise
drop = overlaps Semble or adds orchestration drag
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

## 5. Reviewer Quality Benchmark

Use this to measure whether GPT, Claude, MiMo, or MiniMax actually catches known bugs. It does not call an LLM. Save each reviewer's answer, then score it.

```bash
python3 scripts/reviewer_quality_benchmark.py --list-cases
python3 scripts/reviewer_quality_benchmark.py --answers EVALS/sample_reviewer_answers.json --min-recall 0.8
```

Answer file shape:

```json
{
  "reviewer": "mimo",
  "responses": [
    {"case_id": "bash_eval_injection", "text": "review output here"}
  ]
}
```

Use this before claiming a hook/model change preserves review quality.

## 6. Tree-sitter Analyzer Miswire Audit

Use this only as a diagnostic challenger when call-graph edges look suspicious. It is not a replacement for Semble.

```bash
python3 scripts/tree_sitter_miswire_audit.py hooks --dry-run
python3 scripts/tree_sitter_miswire_audit.py hooks
```

Run it on `v3` or `v4` when a fix depends on call-graph correctness across many same-named functions.

## What We Are Not Adding

No Goose/OpenHands/Gemini/Qwen daily-agent layer. The bottleneck is context quality and verification, not agent count.

Reviewed but not integrated now:

- SuperClaude / BMAD / large skill packs: useful ideas, too much prompt/process surface for daily hooks.
- Claude Context: overlaps Semble and Serena.
- More terminal coding agents: adds orchestration noise before improving localization.
