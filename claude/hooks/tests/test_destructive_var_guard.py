"""A destructive command on a variable path is a HARD DENY with a one-line reason, never a popup for Dom (Dom via Strategy, 2026-10-21).

Claude Code raises its own "Allow Claude to run?" prompt for `rm -f $T/*` when no hook decides. An OpenTrident sub-agent sent
`ssh root@49.12.7.18 'T=_scratch/t; rm -f $T/*'` and the prompt reached Dom. The hook must say deny first, so the lane rewrites with a literal path."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import cache_guard as cg  # noqa: E402
from lib import destructive_var_guard as g  # noqa: E402

CWD = "/Users/x/Developer/frankendom-deploy"

DENY = [
    "T=_scratch/t; rm -f $T/*",
    "ssh root@49.12.7.18 'T=_scratch/t; rm -f $T/*'",   # the case that raised the popup
    "ssh root@49.12.7.18 \"cd /srv/x && rm -rf $T\"",
    "rm -rf \"$DIR\"",
    "rm -rf $DIR/node_modules",
    "rm -rf /tmp/x/${NAME}",
    "rm -rf $(mktemp -d)",
    "rm -f `ls /tmp/x`",
    "rm -rf \"$(dirname $F)/old\"",
    "mv $SRC /tmp/archive/",
    "mv /tmp/a \"$DST\"",
    "rsync -a --delete src/ $DEST/",
    "rsync -a --delete src/ root@host:$DEST/",
    "git clean -fd $DIR",
    "find $DIR -name '*.tmp' -delete",
    "find \"$ROOT\" -mtime +7 -exec rm -rf {} +",
    "bash -c 'rm -rf $D'",
    "sh -c \"rm -rf $D\"",
    "eval 'rm $X'",
    "for f in a b; do rm $f; done",
    "for f in /tmp/a /tmp/b; do rm -f \"$f\"; done",
    "timeout 5 rm -rf $D",
    "nice -n 5 rm -f $F",
    "ls && cd /tmp/x && rm -rf $OLD",
    "unlink $LINK",
    "rmdir \"$D\"",
    "trash $F",
]
ALLOW = [
    "rm -rf /tmp/scratch/a",
    "rm -rf node_modules",
    "rm -f /tmp/a/*.log",
    "rm -f ~/x.log",
    "rm -f $HOME/x.log",
    "rm -rf \"${HOME}/tmp/a\"",
    "ssh root@49.12.7.18 'rm -rf /srv/dev-jobs/x/_scratch/t'",
    "ssh root@49.12.7.18 'ls $T'",
    "echo $T",
    "ls $T/*",
    "cp $A $B",
    "mkdir -p $D",
    "cat $F | head",
    "git commit -m 'close rm -rf $X in the guard'",
    "git clean -n",
    "git status",
    "echo 'rm -rf $X'",
    "grep -rn 'rm -rf $x' .",
    "python3 -c \"print('rm $x')\"",
    "find /tmp/scratch -name '*.log' -delete",
    "find /tmp/scratch -name \"$P\" -print",
    "cat > /tmp/a.sh <<'EOF'\nrm -rf $X\nEOF",
    "rsync -a src/ $DEST/",
    "rsync -a --delete -e \"ssh -i $K -o BatchMode=yes\" /tmp/dist/ root@49.12.7.18:/var/www/frankendom/c/",   # replay: the key variable is not the destination
    "rsync -a --delete $SRC/ /tmp/out/",
    "mv /tmp/a /tmp/b",
    "ssh root@49.12.7.18 'cd /srv/x && ls'",
    "xargs echo $X",
    "bash -c 'echo $D'",
]


def test_denies_a_destructive_command_on_a_variable_path():
    for command in DENY:
        assert g.variable_delete_reason(command), command


def test_allows_literal_paths_and_non_destructive_uses_of_variables():
    for command in ALLOW:
        assert g.variable_delete_reason(command) == "", command


def test_reason_is_one_line_and_tells_the_model_what_to_do():
    reason = g.variable_delete_reason("rm -rf $D")
    assert "\n" not in reason and "literal" in reason


def test_deny_reaches_the_hook_output_on_the_mac_and_skips_literal_paths():
    import io
    import json
    from unittest import mock

    import deploy_guard

    def run(command: str) -> str:
        payload = {"tool_name": "Bash", "cwd": "/tmp", "tool_input": {"command": command}}
        out = io.StringIO()
        with mock.patch.object(deploy_guard, "_is_mac", return_value=True), \
                mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), mock.patch("sys.stdout", out):
            deploy_guard.main()
        return out.getvalue()

    denied = json.loads(run("ssh root@49.12.7.18 'T=_scratch/t; rm -f $T/*'"))["hookSpecificOutput"]
    assert denied["permissionDecision"] == "deny" and denied["permissionDecisionReason"] == g.REASON
    assert run("ssh root@49.12.7.18 'rm -f /srv/dev-jobs/x/_scratch/t/a.log'") == ""


def test_ssh_string_resolves_relative_paths_from_the_remote_home():
    # without a cwd for the remote side, `.cache/ms-playwright` was skipped (a gap) and `_scratch/t` fell to a glob (a false block)
    assert cg.cache_delete_reason("ssh root@h 'rm -rf .cache/ms-playwright'", CWD)
    assert cg.cache_delete_reason("ssh root@h 'rm -rf .cache'", CWD)
    assert cg.cache_delete_reason("ssh root@h 'T=_scratch/t; rm -f $T/*'", CWD) == ""
    assert cg.cache_delete_reason("ssh root@h 'rm -rf _scratch/t'", CWD) == ""
