"""Dom, 2026-10-16 ("we said the devs wouldn't use bash"), via Strategy: a Bash call that writes a file through a heredoc or an inline
interpreter is refused; files are edited with Read/Edit/Write. Heredocs that feed a command's stdin without writing a file stay allowed."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS_DIR))

from lib import bash_write_guard as g  # noqa: E402

REASON = "edit files with Read/Edit/Write, not Bash (Dom, 2026-10-16)"

DENIED = [
    "cat > notes.txt <<'EOF'\nhello\nEOF",
    "cat >> notes.txt <<EOF\nhello\nEOF",
    "cat <<'EOF' > notes.txt\nhello\nEOF",
    "cd x && cat > a.sh <<'EOF'\necho hi\nEOF",
    "tee out.txt <<'EOF'\nhello\nEOF",
    "tee -a out.txt <<EOF\nhello\nEOF",
    "cat <<'EOF' | tee out.txt\nhello\nEOF",
    "python3 - <<'EOF'\nopen('a.py','w').write('x')\nEOF",
    "python3 <<'EOF'\nfrom pathlib import Path\nPath('a.py').write_text('x')\nEOF",
    "python3 -c \"open('a.txt', 'w').write('x')\"",
    "python3 -c 'from pathlib import Path; Path(\"a\").write_text(\"x\")'",
    "node -e \"require('fs').writeFileSync('a.txt','x')\"",
    "node - <<'EOF'\nrequire('fs').appendFileSync('a.txt','x')\nEOF",
    "perl -e 'open(F, \">a.txt\"); print F \"x\"'",
    "perl -pi -e 's/a/b/' file.txt",
    "ruby -e 'File.write(\"a.txt\", \"x\")'",
    "sed -i '' 's/a/b/' file.txt",
    "sed -i.bak 's/a/b/' file.txt",
    "sed --in-place 's/a/b/' file.txt",
    "printf 'x\\n' >> notes.txt",
    "echo hello > notes.txt",
    "echo \"hello\" >> ~/.claude/state/log",
    "FOO=1 echo hi > a.txt",
    "ls; echo done > flag.txt",
    "bash <<'EOF'\ncat > a.txt <<'INNER'\nx\nINNER\nEOF",
]

ALLOWED = [
    # heredocs that feed a command's stdin, no file written
    "ssh root@h <<'EOF'\ncat /etc/hosts\nEOF",
    "psql -d db <<'SQL'\nselect 1;\nSQL",
    "gh api repos/x/y/issues --input - <<'EOF'\n{\"title\": \"t\"}\nEOF",
    "git commit -m \"$(cat <<'EOF'\nmessage with open('a','w') in prose\nEOF\n)\"",
    "gh pr create --title t --body \"$(cat <<'EOF'\nbody\nEOF\n)\"",
    "cat <<'EOF'\njust printing\nEOF",
    "cat <<'EOF' | gh pr comment 1 --body-file -\nhi\nEOF",
    "python3 - <<'EOF'\nprint(open('a.txt').read())\nEOF",
    "python3 -c \"print(open('a.txt').read())\"",
    "python3 -c 'import json; print(json.dumps({1: 2}))'",
    "node -e \"console.log(require('fs').readFileSync('a.txt','utf8'))\"",
    "perl -e 'print 1'",
    # plain commands and redirects the rule does not cover
    "python3 script.py",
    "python3 -m pytest tests -q",
    "node scripts/build.mjs",
    "sed -n '1,5p' file.txt",
    "sed 's/a/b/' file.txt",
    "echo hello",
    "echo hello > /dev/null",
    "echo err >&2",
    "echo x 2>/dev/null",
    "printf 'x' | wc -c",
    "git diff > /tmp/patch.diff",
    "npm test 2>&1 | tail -5",
    "echo \"a > b\"",
    "git commit -m 'echo x > y.txt in the message'",
    "ssh root@h 'cat > /root/x <<EOF\nhello\nEOF'",
    "ssh root@h \"echo x >> /root/log\"",
    "ls | tee",
]


def test_file_writing_forms_are_denied():
    for cmd in DENIED:
        assert g.bash_write_reason(cmd) == REASON, cmd


def test_stdin_heredocs_and_read_only_commands_are_allowed():
    for cmd in ALLOWED:
        assert g.bash_write_reason(cmd) == "", cmd


def test_deny_reaches_the_hook_output():
    deploy_guard = HOOKS_DIR / "deploy_guard.py"
    payload = {"tool_name": "Bash", "tool_input": {"command": "cat > a.txt <<'EOF'\nx\nEOF"}}
    out = subprocess.run([sys.executable, str(deploy_guard)], input=json.dumps(payload), capture_output=True, text=True).stdout
    if sys.platform == "darwin":                      # the guard is Mac-only, like the rest of deploy_guard's Bash rules
        assert json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"] == REASON
    else:
        assert out == ""
