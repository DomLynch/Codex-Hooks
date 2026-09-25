#!/usr/bin/env bash
#
# Install Claude Code hooks: GPT-5.6-Terra audit, autonomy contract, quality floor,
# auto-checkpoint, and stop orchestrator.
#
# Idempotent: safe to re-run. Backs up settings.json before merging.
# Preserves any existing hooks you already have in ~/.claude/settings.json.

set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLAUDE_HOME="$HOME/.claude"
HOOKS_DIR="$CLAUDE_HOME/hooks"
STATE_DIR="$CLAUDE_HOME/state"
LOGS_DIR="$CLAUDE_HOME/logs"
SECRETS_DIR="$CLAUDE_HOME/secrets"
SETTINGS="$CLAUDE_HOME/settings.json"
BACKUP="$SETTINGS.backup-$(date +%Y%m%d-%H%M%S)"

echo "==> Installing Claude hooks from: $SRC_DIR"
echo "==> Target: $CLAUDE_HOME"

# 1. Create directories
mkdir -p "$HOOKS_DIR/lib" "$HOOKS_DIR/tests" "$STATE_DIR" "$LOGS_DIR" "$SECRETS_DIR"
chmod 700 "$SECRETS_DIR"

# 2. Copy hook scripts
echo "==> Copying hook scripts"
cp -f "$SRC_DIR/hooks/"*.py "$HOOKS_DIR/"
cp -f "$SRC_DIR/hooks/lib/"*.py "$HOOKS_DIR/lib/"
cp -f "$SRC_DIR/hooks/tests/"*.py "$HOOKS_DIR/tests/"
chmod +x "$HOOKS_DIR/"*.py 2>/dev/null || true

# 3. Initialize hook env file with safe defaults if absent
HOOK_ENV="$SECRETS_DIR/hook_env"
if [ ! -f "$HOOK_ENV" ]; then
  cat > "$HOOK_ENV" <<'EOF'
# Claude hook runtime flags
# CODEX_HOOK_AUTO_PR: 1 = enable auto-commit/push/PR after Stop audit pass
# CODEX_HOOK_CLAUDE_AUDIT: 1 = enable second-pass Anthropic audit (requires anthropic_api_key)
# CLAUDE_AUDIT_MODEL / CLAUDE_AUDIT_EFFORT: the per-Stop reviewer (Opus 5.5 low; Opus 5 low won the 2026-09 scorecard)
# CLAUDE_STOP_REVIEW: 0 = turn the per-Stop reviewer off
CODEX_HOOK_AUTO_PR=0
CODEX_HOOK_CLAUDE_AUDIT=0
CLAUDE_AUDIT_MODEL=claude-opus-5-5
CLAUDE_AUDIT_EFFORT=low
EOF
  chmod 600 "$HOOK_ENV"
  echo "==> Created default hook_env (Auto-PR=0, Claude=0). Edit $HOOK_ENV to enable."
fi

# 4. Merge hooks into settings.json (preserving existing config)
echo "==> Merging settings into $SETTINGS"
if [ -f "$SETTINGS" ]; then
  cp -f "$SETTINGS" "$BACKUP"
  echo "    Backup: $BACKUP"
fi

python3 - "$SRC_DIR/config/settings.example.json" "$SETTINGS" "$HOOKS_DIR" <<'PYEOF'
import json, sys
from pathlib import Path

example_path, target_path, hooks_dir = sys.argv[1], sys.argv[2], sys.argv[3]
# The example is written against the author's home; point every command at THIS machine's hooks dir.
AUTHOR_PREFIX = "python3 /Users/domininclynch/.claude/hooks/"
OUR_COMMAND_PREFIX = f"python3 {hooks_dir}/"
example = json.loads(Path(example_path).read_text().replace(AUTHOR_PREFIX, OUR_COMMAND_PREFIX))
new_hooks = example.get("hooks", {})

target = Path(target_path)
existing = json.loads(target.read_text()) if target.exists() else {}
existing.setdefault("hooks", {})


for event, blocks in new_hooks.items():
    existing_blocks = existing["hooks"].setdefault(event, [])

    # Strip out any prior install of OUR hooks for this event (clean re-install)
    cleaned = []
    for block in existing_blocks:
        block_hooks = block.get("hooks", [])
        kept_hooks = [
            h for h in block_hooks
            if not (h.get("type") == "command" and OUR_COMMAND_PREFIX in (h.get("command") or ""))
        ]
        if kept_hooks:
            new_block = dict(block)
            new_block["hooks"] = kept_hooks
            cleaned.append(new_block)
        elif "matcher" in block and not block_hooks:
            # Preserve empty matcher blocks if they had matchers (shouldn't happen normally)
            pass
    existing["hooks"][event] = cleaned

    # Append our new blocks
    for block in blocks:
        existing["hooks"][event].append(block)

target.write_text(json.dumps(existing, indent=2))
print(f"    settings.json: merged {sum(len(blocks) for blocks in new_hooks.values())} hook blocks across {len(new_hooks)} events")
PYEOF

# 6. Verify
echo "==> Verifying"
python3 -m py_compile "$HOOKS_DIR/"*.py "$HOOKS_DIR/lib/"*.py && echo "    Python compile: OK"
python3 -c "import json; json.loads(open('$SETTINGS').read())" && echo "    settings.json: valid"

echo ""
echo "==> Installed."
echo ""
echo "Next steps:"
echo "  1. Restart Claude Code (so the hooks load)"
echo "  2. Watch logs:    tail -f $LOGS_DIR/*.log"
echo "  3. Enable Auto-PR / Claude audit by editing $HOOK_ENV"
echo "  4. Disable any hook: comment its block in $SETTINGS"
echo ""
echo "  To uninstall: re-run this script and delete $HOOKS_DIR + restore $BACKUP"
