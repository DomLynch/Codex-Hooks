"""cache_guard: delete forms hidden behind a wrapper (Hooks Dev follow-up card to #171, 2026-10-21).
`eval`, `timeout`/`nice`/`exec`/`env -i`, `find -exec sh -c`, `xargs sh -c`, `ruby -e`, `echo '...' | sh` and `osascript do shell script`
all reached `rm` without the guard seeing the deleter or its target."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import cache_guard as g  # noqa: E402

CWD = "/Users/x/Developer/frankendom-deploy"
HOME = str(Path.home())

DENY = [
    "eval 'rm -rf ~/Library/Caches/ms-playwright'",
    "eval rm -rf ~/Library/Caches/ms-playwright",
    "eval \"find ~/.cache -delete\"",
    "timeout 5 rm -rf ~/Library/Caches/ms-playwright",
    "timeout -s KILL 5 rm -rf ~/Library/Caches",
    "nice rm -rf ~/Library/Caches/ms-playwright",
    "nice -n 10 rm -rf ~/.cache",
    "ionice -c 3 rm -rf /root/.cache",
    "exec rm -rf ~/Library/Caches/ms-playwright",
    "env -i rm -rf ~/Library/Caches/ms-playwright",
    "env FOO=1 rm -rf ~/Library/Caches/ms-playwright",
    "env -u HOME rm -rf ~/.cache",
    "sudo -u root rm -rf /root/.cache",
    "caffeinate -i rm -rf ~/Library/Caches",
    "stdbuf -oL rm -rf ~/.cache",
    "find ~/Library/Caches -maxdepth 1 -exec sh -c 'rm -rf \"$0\"' {} \\;",
    "find ~/.cache -exec bash -c 'rm -rf {}' \\;",
    "find ~/Library/Caches -mindepth 1 -execdir bash -c 'rm -rf \"$1\"' _ {} +",
    "find /tmp/scratch -exec sh -c 'rm -rf ~/Library/Caches/ms-playwright' \\;",
    "ls ~/Library/Caches | xargs sh -c 'rm -rf \"$@\"' _",
    "ls ~/.cache | xargs -n1 bash -c 'rm -rf \"$0\"'",
    "xargs sh -c 'rm -rf \"$1\"' _ < list.txt",
    "ls /tmp/scratch | xargs sh -c 'rm -rf ~/Library/Caches/ms-playwright'",
    "ruby -e 'require \"fileutils\"; FileUtils.rm_rf(File.expand_path(\"~/Library/Caches/ms-playwright\"))'",
    f"ruby -e 'require \"fileutils\"; FileUtils.rm_rf(\"{HOME}/Library/Caches\")'",
    "ruby -e 'Dir[File.join(ENV[\"HOME\"], \".cache\", \"*\")].each { |f| File.delete(f) }'",
    "ruby -e 'system(\"rm -rf ~/Library/Caches/ms-playwright\")'",
    "echo 'rm -rf ~/Library/Caches/ms-playwright' | sh",
    "printf 'rm -rf ~/.cache\\n' | bash",
    "echo \"find ~/Library/Caches -delete\" | zsh",
    "osascript -e 'do shell script \"rm -rf ~/Library/Caches/ms-playwright\"'",
    "osascript -e 'do shell script \"find ~/.cache -delete\"'",
    # Auditor 2026-10-21: a subprocess call is judged by what it runs, so these stay denied
    f"python3 -c \"import subprocess; subprocess.run(['rm','-rf','{HOME}/Library/Caches/ms-playwright'])\"",
    "python3 -I - <<'EOF'\nimport subprocess, os\nsubprocess.run(['rm', '-rf', os.path.expanduser('~/.cache')])\nEOF",
    "node -e \"require('child_process').execSync('rm -rf ' + process.env.HOME + '/.cache')\"",
]
ALLOW = [
    "eval echo hello",
    "eval 'rm -rf /tmp/scratch'",
    "eval \"$(ssh-agent -s)\"",
    "timeout 5 rm -rf /tmp/scratch",
    "timeout 30 python3 -m pytest tests -q",
    "nice -n 10 rm -rf /tmp/scratch",
    "nice -n 10 python3 build.py",
    "exec rm -rf /tmp/scratch",
    "env -i rm -rf /tmp/scratch",
    "env FOO=1 python3 -m pytest",
    "sudo -u root rm -rf /tmp/scratch",
    "find /tmp/scratch -exec sh -c 'rm -rf \"$0\"' {} \\;",
    "find ~/Library/Caches -exec sh -c 'echo \"$0\"' {} \\;",
    "find ~/Library/Caches/semble -exec bash -c 'rm -rf {}' \\;",
    "ls /tmp/scratch | xargs sh -c 'rm -rf \"$@\"' _",
    "ls ~/Library/Caches | xargs sh -c 'echo \"$@\"' _",
    "ruby -e 'puts 1'",
    "ruby -e 'require \"fileutils\"; FileUtils.rm_rf(\"/tmp/scratch\")'",
    "ruby script.rb",
    "echo 'rm -rf /tmp/scratch' | sh",
    "echo hello | sh",
    "curl -fsSL https://example.com/install.sh | sh",
    "osascript -e 'display dialog \"hi\"'",
    "osascript -e 'do shell script \"rm -rf /tmp/scratch\"'",
    "bash -c 'rm -rf ~/Library/Caches/semble'",
    "bash -c 'ls ~/Library/Caches'",
    # Auditor 2026-10-21: read-only subprocess calls (git show, git log, npx tsc) are not deleters
    "python3 -I - <<'EOF'\nimport subprocess, json\nrev = 'abc123'\nr = subprocess.run([\"git\", \"show\", f\"{rev}:tests/fixtures/rng-fingerprint.json\"], capture_output=True, text=True)\nprint(json.loads(r.stdout))\nEOF",
    "python3 -c \"import subprocess; subprocess.run(['git','log','-1','--oneline'])\"",
    "python3 -c \"import subprocess; subprocess.run(['npx','tsc','--noEmit'], check=True)\"",
    "node -e \"require('child_process').execSync('git status --short')\"",
    "python3 -c \"import os; os.system('ls /tmp')\"",
]


def test_denies_deletes_hidden_behind_a_wrapper():
    for command in DENY:
        assert g.cache_delete_reason(command, CWD), command


def test_allows_wrapped_commands_that_cannot_reach_the_cache():
    for command in ALLOW:
        assert g.cache_delete_reason(command, CWD) == "", command
