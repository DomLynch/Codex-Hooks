#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
codex_home="${CODEX_HOME:-$HOME/.codex}"

mkdir -p "$codex_home/hooks"
rsync -a --delete \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  "$repo_root/hooks/" "$codex_home/hooks/"

python3 -m py_compile "$codex_home"/hooks/*.py "$codex_home"/hooks/lib/*.py

cat <<EOF
Installed hooks to:
  $codex_home/hooks

If needed, copy config/hooks.example.toml into:
  $codex_home/config.toml

Secrets, logs, state, and trusted hashes are intentionally not managed by this repo.
EOF
