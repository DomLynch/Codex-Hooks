# Context Efficiency Research - 2026-06-02

Purpose: source-ground the cross-project context-efficiency policy before
turning pasted recommendations into default Codex or Claude behavior.

## Adoption Rule

Treat a repo as general-rollout ready only when it has clear adoption,
maintenance, license clarity, and low blast radius. Low-star repos can inform
design, but they should not become default infrastructure without a pilot.

## Source Review

| Source | Current signal checked | Decision |
| --- | ---: | --- |
| `rtk-ai/rtk` | 57,858 stars, Apache-2.0, pushed 2026-06-01 | Pilot only. Strong output-compression candidate, but global shell hooks are high blast radius. |
| `Aider-AI/aider` | 45,687 stars, Apache-2.0, pushed 2026-05-22 | Copy the repo-map and `.aiderignore` pattern. Root `.aiderignore` was added separately in the Business workspace. |
| AGENTS.md efficiency paper | arXiv 2601.20404 reports 28.64% median runtime and 16.58% output-token reduction | Keep AGENTS discipline as default. Implemented in Codex and Claude hook policy. |
| `Madhan230205/token-reducer` | 25 stars, MIT, pushed 2026-05-02 | Do not general-rollout. Borrow idea only: compact context packets, BM25/AST/reranking. |
| CogniLayer / `codex-agent-mem` | 28 / 24 stars; one has unclear license | Do not install. Existing Codex memory + Codegraph covers the proven need with lower risk. |
| `opencode-ai/opencode` / `google-gemini/gemini-cli` | 12,798 / 104,861 stars | Use as cheaper scout / second-opinion lanes, not duplicate Codex explorers. |
| `EuniAI/ContextBench` | 35 stars, Apache-2.0; arXiv 2602.05892 has useful findings | Use principle, not infra: optimize context precision, not recall volume. |

## High-Star Alternatives Worth Watching

| Repo | Current signal checked | Why it matters |
| --- | ---: | --- |
| `jgravelle/jcodemunch-mcp` | 1,882 stars | More credible AST/MCP source exploration than low-star token reducers, but still needs pilot. |
| `mem0ai/mem0` | 57,404 stars | Mature memory layer; likely overkill because local memory already exists. |
| `getzep/graphiti` | 26,915 stars | Strong graph memory option; overkill for current command-output/context problem. |
| `modelcontextprotocol/servers` | 86,627 stars | Canonical MCP ecosystem reference. Use for standards, not as a direct patch. |

## Implemented Defaults

- Codex and Claude session baselines require lean command output, capped logs,
  non-duplicative parallel roles, and compact context packets.
- Codex and Claude user-prompt hooks inject a Context Efficiency Contract for
  project work.
- Business root `AGENTS.md` carries workspace-level context rules.
- Business root `.aiderignore` excludes generated, archived, vendored, cached,
  and bulky output paths for repo-map/scout tools.

## Not Implemented

- No RTK global install. Use a measured one-repo pilot first.
- No low-star token-reducer / CogniLayer / codex-agent-mem install.
- No replacement of Codegraph with another indexer.

## Pilot Gate For RTK Or Similar

Run on one non-production repo for 5-10 tasks:

1. Compare raw vs compressed output on `rg`, `git diff`, `pytest`, `npm test`,
   `find`, and `gh pr diff`.
2. Confirm no critical failure lines, paths, or exit codes are hidden.
3. Confirm raw-output escape hatch exists and is easy.
4. Measure actual token reduction in Codex or Claude transcripts, not README
   claims.
5. Only then consider enabling for more projects.
