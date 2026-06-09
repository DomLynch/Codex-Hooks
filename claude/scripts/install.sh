#!/usr/bin/env bash
#
# Install Claude Code hooks: MiniMax-M3 audit, autonomy contract, quality floor,
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

# 3. Verify MiniMax key is accessible (shared from Codex install)
MINIMAX_KEY_CODEX="$HOME/.codex/secrets/minimax_api_key"
MINIMAX_KEY_CLAUDE="$SECRETS_DIR/minimax_api_key"
if [ ! -f "$MINIMAX_KEY_CLAUDE" ] && [ ! -f "$MINIMAX_KEY_CODEX" ]; then
  echo "WARNING: No MiniMax API key found at $MINIMAX_KEY_CLAUDE or $MINIMAX_KEY_CODEX"
  echo "         Audit hook will fail-open (no audit). Add key to either path."
elif [ -f "$MINIMAX_KEY_CODEX" ] && [ ! -f "$MINIMAX_KEY_CLAUDE" ]; then
  echo "==> MiniMax key shared from Codex install: $MINIMAX_KEY_CODEX"
fi

# 4. Initialize hook env file with safe defaults if absent
HOOK_ENV="$SECRETS_DIR/hook_env"
if [ ! -f "$HOOK_ENV" ]; then
  cat > "$HOOK_ENV" <<'EOF'
# Claude hook runtime flags
# CODEX_HOOK_AUTO_PR: 1 = enable auto-commit/push/PR after Stop audit pass
# CODEX_HOOK_CLAUDE_AUDIT: 1 = enable second-pass Anthropic audit (requires anthropic_api_key)
CODEX_HOOK_AUTO_PR=0
CODEX_HOOK_CLAUDE_AUDIT=0
EOF
  chmod 600 "$HOOK_ENV"
  echo "==> Created default hook_env (Auto-PR=0, Claude=0). Edit $HOOK_ENV to enable."
fi

# 5. Merge hooks into settings.json (preserving existing config)
echo "==> Merging settings into $SETTINGS"
if [ -f "$SETTINGS" ]; then
  cp -f "$SETTINGS" "$BACKUP"
  echo "    Backup: $BACKUP"
fi

python3 - "$SRC_DIR/config/settings.example.json" "$SETTINGS" <<'PYEOF'
import json, sys
from pathlib import Path

example_path, target_path = sys.argv[1], sys.argv[2]
example = json.loads(Path(example_path).read_text())
new_hooks = example.get("hooks", {})

target = Path(target_path)
existing = json.loads(target.read_text()) if target.exists() else {}
existing.setdefault("hooks", {})

# Hook commands we are managing — used to identify our entries for replacement.
OUR_COMMAND_PREFIX = "python3 /Users/domininclynch/.claude/hooks/"

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
