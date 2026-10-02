#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
codex_home="${CODEX_HOME:-$HOME/.codex}"

# A tracked release archive has no .git; a checkout must not ship stray files.
if [[ -e "$repo_root/.git" ]]; then
  untracked_runtime=$(git -C "$repo_root" ls-files --others --exclude-standard -- hooks scripts)
  if [[ -n "$untracked_runtime" ]]; then
    echo "Refusing installation: untracked files under hooks/ or scripts/. Preserve them outside the checkout first." >&2
    exit 1
  fi
fi

mkdir -p "$codex_home/hooks" "$codex_home/scripts"
rsync -a --delete \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  "$repo_root/hooks/" "$codex_home/hooks/"
rsync -a \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  "$repo_root/scripts/" "$codex_home/scripts/"

python3 -m py_compile "$codex_home"/hooks/*.py "$codex_home"/hooks/lib/*.py

cat <<EOF
Installed hooks to:
  $codex_home/hooks

If needed, copy config/hooks.example.toml into:
  $codex_home/config.toml

Secrets, logs, state, and trusted hashes are intentionally not managed by this repo.
EOF
