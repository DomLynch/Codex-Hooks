# Replacement for the heavy-job sections of the shared CLAUDE.md (Dom via Strategy, 2026-10-31)

Replace these three sections in `~/.claude/CLAUDE.md`, and delete them wherever else they are copied (lane memory files):

- `## Hugging Face CPU for every lane, every repo` (the `hfjob -- <cmd>` rule)
- `## GPU waterfall: Mac first, HF GPU for essentials only`
- `## Heavy jobs run on the VPS`

with this one section:

```markdown
## Where work runs (Dom, standing, 2026-10-31, VPS restructure)

The Windows laptop is where work happens: coding, builds, full suites, Playwright, stills, clips, Blender and any other heavy job, one heavy job at a time, in a checkout on the
laptop. GitHub is the source of truth: commit and push your branch; anything not in git (assets, data, notes) goes to the laptop's handover working folder.

The VPS is production only. On it: no worktrees, no scratch, no heavy jobs, no `pkill` by name, never touch armagedom paths, and never delete without Dom's per-folder word.
`hfjob` and `lanejob` are retired. The hooks refuse a heavy command sent over ssh and a clone into /root (`hf_enforce_guard`, `root_clone_guard`).
```

What the hooks do now: `deploy_guard` no longer refuses heavy local runs unless a deploy is in flight (the one-deployer lock); `hf_enforce_guard` keeps refusing heavy commands
over ssh, on every OS; `agent_brief_guard` appends the "where work runs" line to sub-agent briefs that mention heavy work.
