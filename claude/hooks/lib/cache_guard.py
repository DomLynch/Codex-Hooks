"""Never delete or move the Playwright browser cache (Lead + Dom, 2026-10-16).

A cleanup command removed ~/Library/Caches/ms-playwright; chromium went missing and the HL2 publish stopped. `cache_delete_reason`
denies rm/rmdir/unlink/trash/mv (and `rsync --delete`, judged by its destination) whose target is ms-playwright (Mac or VPS), anything
inside it, or a folder that contains it (~/Library/Caches, ~/.cache, /root/.cache, ~/Library, ~, /root, /), including a glob that would
match any of those. Other caches (~/.cache/uv, ~/Library/Caches/semble ...) stay deletable. Quoted text is data; `ssh host '<command>'`
is checked with /root as home.

2026-10-21 (Strategy + Dom): FAIL CLOSED on a path it cannot resolve. A loop deleted ms-playwright through `rm -rf "${s:?}"` because the old
guard skipped every target containing `$`. Now an inline literal assignment (`s=~/x`) or `for s in a b c` in the same command is resolved,
and a variable or command substitution that stays unknown becomes `*`: denied when `*` could be the cache or a folder holding it
(`"${s:?}"`, `$(mktemp -d)`), allowed when a literal part rules it out (`"$DIR/node_modules"`). Still fail-open only on an odd quote.

2026-10-21 (Auditor + Strategy follow-up): the delete forms that never name the path to `rm` are judged too. `find <paths> -delete` and
`find <paths> -exec rm ...` are denied when a start path is the cache, inside it, or a folder holding it; `... | xargs rm` is denied unless
the pipe's producer is a find/ls/echo/printf whose paths are resolved and clear of the cache (anything else, e.g. `cat list | xargs rm`, is
unknowable and fails closed); an inline Python command (`python -c`, `python - <<EOF`) that calls rmtree/os.remove/unlink/... is denied when
a string literal reaches the cache, the code mentions playwright, or it builds paths dynamically (expanduser, Path.home, listdir, glob ...).
A script run as a FILE is not inspected: its own paths are the lane's to check. Python source is read from the shlex words (a `-c "..."`
argument without the shell's quotes) and the heredoc body; a string that looks like a shell command (os.system, subprocess) is judged as one.
xargs counts as pipe-fed only straight after a single `|`; after `;`/`&&` or with a `<` redirect its stdin is unknown and it fails closed.
node -e and perl -e are judged the same way (Lead's ruling via the Auditor); a delete whose path is built from pieces (+, f-string, .format, %,
join, ${}) is denied because it cannot be shown clear of the cache. Not covered: interpreters other than python/node/perl/ruby.

2026-10-21 (Hooks Dev, follow-up card to #171): wrappers no longer hide the deleter. `timeout 5`, `nice -n 10`, `ionice`, `exec`, `stdbuf`,
`caffeinate`, `setsid`, `env -i`/`env -u X` and `sudo -u x` are skipped with their own options; `eval <words>` and `osascript -e 'do shell script
"..."'` are judged as the shell command they carry; `echo|printf '<command>' | sh` is judged as that command; `find -exec sh -c '<script>'` and
`xargs sh -c '<script>'` judge the script (positional $0/$1/$@ stand for what find or the producer supplies, so the start path or the producer
decides, as for `-exec rm`); ruby -e joins python/node/perl. Still not covered: `cat file | sh`, scripts run as files, a command word built
by substitution (`$(echo rm) -rf ...`).
"""
from __future__ import annotations

import fnmatch
import os
import re
import shlex
from pathlib import Path

from .bash_write_guard import _split as split_segments

PLAYWRIGHT = [Path.home() / "Library" / "Caches" / "ms-playwright", Path.home() / ".cache" / "ms-playwright",
              Path.home() / "Developer" / ".ms-playwright",   # Deploy's second browser cache, never-touch since 2026-10-21 (Lead)
              Path("/root/.cache/ms-playwright")]
ANCESTORS = {str(parent) for pw in PLAYWRIGHT for parent in pw.parents}
PROTECTED = {str(pw) for pw in PLAYWRIGHT}
DELETERS = {"rm", "rmdir", "unlink", "trash"}
WRAPPERS = {"sudo", "command", "env", "nohup", "time", "timeout", "nice", "ionice", "exec", "stdbuf", "caffeinate", "setsid", "builtin"}
WRAPPER_VALUED = {"sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U"}, "env": {"-u", "-C", "-S", "-P"}, "timeout": {"-s", "-k"},
                  "nice": {"-n"}, "ionice": {"-c", "-n", "-p"}, "stdbuf": {"-i", "-o", "-e"}, "caffeinate": {"-t", "-w"}}   # options that take a value
SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
DELETE_TEXT_RE = re.compile(r"\b(?:rm|rmdir|unlink|trash|mv)\b|\bfind\b[^|;&]*-delete|\brsync\b[^|;&]*--delete")
KEYWORDS = {"do", "then", "if", "else", "elif", "while", "until", "!", "{"}   # shell words that can start a segment before the command
REMOTE_RE = re.compile(r"\b(?:rm|rmdir|unlink|trash|mv|rsync|find|xargs|python[\d.]*)\b")
VAR_RE = re.compile(r"\$\{(\w+)[^}]*\}|\$(\w+)")
SUBST_RE = re.compile(r"\$\([^)]*\)?|`[^`]*`?|\$")
ASSIGN_RE = re.compile(r"(\w+)=(.*)", re.DOTALL)
MESSAGE = ("Never delete or move ms-playwright, or a folder that contains it (Lead + Dom 2026-10-16: it vanished and stopped "
           "the HL2 publish). Name a sibling you checked no lane depends on instead, e.g. ~/Library/Caches/semble.")
UNRESOLVED = ("cache_guard: unresolvable path, use the literal path. A variable or command substitution here could name ms-playwright or a "
              "folder that holds it (Strategy + Dom 2026-10-21: a loop deleted it through $s). Write the literal path, or assign it in the "
              "same command (s=~/Library/Caches/semble).")


INDIRECT = ("cache_guard: this delete names its targets indirectly (xargs, find -delete/-exec, python) and could reach ms-playwright or a "
            "folder that holds it. Pass the literal paths to rm, or give find/ls a literal start path outside ~/Library/Caches, ~/.cache and "
            "their parents (Auditor + Strategy 2026-10-21).")
FIND_EXEC = {"-exec", "-execdir", "-ok", "-okdir"}
FIND_PRODUCERS = {"find", "ls", "echo", "printf"}
XARGS_VALUED = {"-I", "-n", "-P", "-L", "-s", "-d", "-E", "-a", "-J"}
PY_HEAD_RE = re.compile(r"python[\d.]*|node(?:js)?|perl[\d.]*|ruby[\d.]*")   # python, node and perl one-liners are judged alike (Lead via the Auditor, 2026-10-21)
PY_DELETE_RE = re.compile(r"\brmtree\b|shutil\.move|\bos\.(?:remove|unlink|rmdir|removedirs|rename|replace)\b|\.(?:unlink|rmdir|rename)\("
                          r"|send2trash|\b(?:rmSync|rmdirSync|unlinkSync|renameSync)\b|fs\.(?:rm|rmdir|unlink|rename)\b|\.rm\(|\brimraf\b|\bdel\b\("
                          r"|\bremove_tree\b|\bunlink\b|\brmdir\b"
                          r"|\bFileUtils\b|\brm_rf?\b|File\.(?:delete|rename)\b|Dir\.(?:rmdir|delete)\b")   # ruby (Hooks Dev 2026-10-21)
PY_LAUNCH_RE = re.compile(r"os\.(?:system|popen|exec\w*)|subprocess|child_process|execSync|spawnSync|\bsystem\b")   # runs another program: a deleter only when what it runs deletes (Auditor 2026-10-21)
SHELLISH_RE = re.compile(r"\b(?:rm|rmdir|unlink|trash|mv|find|xargs|rsync)\b")
PY_DYNAMIC_RE = re.compile(r"expanduser|Path\.home|environ|getenv|listdir|iterdir|rglob|glob|walk|scandir|argv|input\(|__import__|eval\(|exec\("
                           r"|homedir|process\.env|\bENV\b|path\.join|path\.resolve|joinpath|readdirSync|readdir|opendir|File::Find|File::Spec|expand_path|Dir\.home|Dir\[|Dir\.glob"
                           r"""|['"`]\s*[+.%]\s*[\w$(\['"`]|[\w)\]]\s*[+.]\s*['"`]|\bf['"]|\.format\(|\.join\(|\$\{|\$\w""")   # a path built from pieces
PY_LITERAL_RE = re.compile(r"""(['"])(.+?)\1""")


def _resolve(arg: str, home: str, cwd: str, env: dict[str, list[str]]) -> list[tuple[str, bool]]:
    """Every absolute path an argument can name, as (path, unknown). `unknown` means a variable or substitution we cannot resolve was
    replaced by '*' (so the path is a pattern). Empty when a relative path has no cwd to be placed in."""
    path = arg
    if path == "~" or path.startswith("~/"):
        path = home + path[1:]
    options = [(path, False)]
    for _ in range(8):
        grown, changed = [], False
        for text, unknown in options:
            m = VAR_RE.search(text)
            if not m:
                grown.append((text, unknown))
                continue
            changed = True
            name = m.group(1) or m.group(2)
            values = [home] if name == "HOME" else env.get(name)
            if values is None:
                grown.append((text[:m.start()] + "*" + text[m.end():], True))
            else:
                grown.extend((text[:m.start()] + value + text[m.end():], unknown) for value in values)
        options = grown
        if not changed:
            break
    out = []
    for text, unknown in options:
        replaced = SUBST_RE.sub("*", text)
        unknown = unknown or replaced != text
        if not replaced.startswith("/") and not replaced.startswith("*"):
            if not cwd:
                continue
            replaced = os.path.join(cwd, replaced)
        out.append((os.path.normpath(replaced), unknown))
    return out


def _hits(path: str) -> bool:
    if any(ch in path for ch in "*?["):
        return any(fnmatch.fnmatchcase(p, path) for p in PROTECTED | ANCESTORS)
    return path in PROTECTED or path in ANCESTORS or any(path.startswith(p + "/") for p in PROTECTED)


def _targets(words: list[str]) -> list[str]:
    out, only_files = [], False
    for word in words:
        if word == "--" and not only_files:
            only_files = True
        elif only_files or not word.startswith("-"):
            out.append(word)
    return out


def _known(resolved: list[tuple[str, bool]]) -> list[str]:
    return [path for path, unknown in resolved if not unknown]


def _find_starts(rest: list[str]) -> list[str]:
    i = 0
    while i < len(rest) and rest[i] in {"-H", "-L", "-P"}:
        i += 1
    out = []
    while i < len(rest) and not rest[i].startswith(("-", "(", "!")):
        out.append(rest[i])
        i += 1
    return out or ["."]


def _paths_hit(words: list[str], cwd: str, here: str, env: dict[str, list[str]]) -> str:
    for word in words:
        for path, unknown in _resolve(word, here, cwd, env):
            if _hits(path):
                return UNRESOLVED if unknown else MESSAGE
    return ""


def _skip_wrapper_args(wrapper: str, words: list[str]) -> list[str]:
    """Drop a wrapper's own options (and `timeout`'s duration) so the next word is the command it runs."""
    valued = WRAPPER_VALUED.get(wrapper, set())
    i = 0
    while i < len(words) and words[i].startswith("-") and words[i] != "--":
        i += 2 if words[i] in valued else 1
    if wrapper == "timeout" and i < len(words) and re.fullmatch(r"[\d.]+[smhd]?", words[i]):
        i += 1
    return words[i:]


def _shell_script(words: list[str]) -> str:
    """The `-c` script of a shell invocation's words (`-c`, `-lc`, ...), or ''."""
    for i, word in enumerate(words[:-1]):
        if re.fullmatch(r"-\w*c", word):
            return re.sub(r"\$\{?[0-9@*#]+\}?|\$@|\$\*", "/ARG", words[i + 1])   # find/xargs positional parameters are decided by the producer, not the script
    return ""


def _find_reason(rest: list[str], cwd: str, here: str, env: dict[str, list[str]]) -> str:
    deletes = "-delete" in rest or any(
        w in FIND_EXEC and i + 1 < len(rest) and os.path.basename(rest[i + 1]) in DELETERS | {"mv"} for i, w in enumerate(rest))
    for i, w in enumerate(rest):   # `-exec sh -c '<script>'`: judge the script (Hooks Dev follow-up to #171)
        if w in FIND_EXEC and i + 1 < len(rest) and os.path.basename(rest[i + 1]) in SHELLS:
            script = _shell_script(rest[i + 2:])
            if script and DELETE_TEXT_RE.search(script):
                reason = _scan(script, cwd, here, dict(env))
                if reason:
                    return reason
                deletes = True
    return _paths_hit(_find_starts(rest), cwd, here, env) if deletes else ""


def _xargs_reason(rest: list[str], prev: list[str], cwd: str, here: str, env: dict[str, list[str]]) -> str:
    i = 0
    while i < len(rest) and rest[i].startswith("-"):
        i += 2 if rest[i] in XARGS_VALUED else 1
    if i >= len(rest):
        return ""
    if os.path.basename(rest[i]) in SHELLS:   # `xargs sh -c '<script>'`: the script deletes what stdin feeds it
        script = _shell_script(rest[i + 1:])
        if not script or not DELETE_TEXT_RE.search(script):
            return ""
        reason = _scan(script, cwd, here, dict(env))
    elif os.path.basename(rest[i]) in DELETERS | {"mv"}:
        reason = _paths_hit(_targets(rest[i + 1:]), cwd, here, env)   # operands given on the xargs line itself
    else:
        return ""
    if reason:
        return reason
    producer, args = (os.path.basename(prev[0]), prev[1:]) if prev else ("", [])
    if producer not in FIND_PRODUCERS:
        return INDIRECT   # stdin of unknown origin: fail closed
    return _paths_hit(_find_starts(args) if producer == "find" else _targets(args) or ["."], cwd, here, env)


def _piped(command: str, segment: str) -> bool:
    """True when every occurrence of the segment follows a single `|` (its stdin is the previous command's output)."""
    start, found = 0, False
    while (idx := command.find(segment, start)) >= 0:
        before = command[:idx].rstrip()
        if not before.endswith("|") or before.endswith("||"):
            return False
        found, start = True, idx + 1
    return found


def _python_reason(command: str, words: list[str], cwd: str, here: str) -> str:
    """`words` are the shlex words of the python segments, so a `-c "..."` source is seen without the shell's quotes around it (Auditor 2026-10-21);
    `command` adds a heredoc body. A string that looks like a shell command (os.system, subprocess) is judged as one."""
    sources = [command, *words]
    launches_deleter = any(PY_LAUNCH_RE.search(src) for src in sources) and any(DELETE_TEXT_RE.search(src) for src in sources)
    if not launches_deleter and not any(PY_DELETE_RE.search(src) for src in sources):
        return ""
    if "playwright" in command.lower() or any(PY_DYNAMIC_RE.search(src) for src in sources):
        return INDIRECT
    for src in sources:
        for _, literal in PY_LITERAL_RE.findall(src):
            for path, _unknown in _resolve(literal, here, cwd, {}):
                if _hits(path):
                    return MESSAGE
            if " " in literal and SHELLISH_RE.search(literal):
                reason = _scan(literal, cwd, here, {})
                if reason:
                    return reason
    return ""


def _scan(command: str, cwd: str, here: str, env: dict[str, list[str]]) -> str:
    prev: list[str] = []
    python: list[str] | None = None
    for segment in split_segments(command or ""):
        try:
            words = shlex.split(segment.rstrip().removesuffix("\\"))   # `find ... -exec rm {} \;` splits at `;` and leaves a lone backslash
        except ValueError:
            continue
        assigns = []
        while words and (words[0] in KEYWORDS or words[0] in WRAPPERS or re.fullmatch(r"\w+=\S*", words[0])):
            word, words = words[0], words[1:]
            if word in WRAPPERS:
                words = _skip_wrapper_args(word, words)   # timeout 5 / nice -n 10 / env -i / exec ...
            elif word not in KEYWORDS:
                assigns.append(word)
        if not words:
            for assign in assigns:   # a bare `name=value` sets it for the rest of the command
                name, value = ASSIGN_RE.fullmatch(assign).groups()
                resolved = _resolve(value, here, cwd, env)
                if resolved and len(_known(resolved)) == len(resolved):
                    env[name] = _known(resolved)
                else:
                    env.pop(name, None)
            continue
        head, rest = os.path.basename(words[0]), words[1:]
        if head == "for" and len(rest) >= 3 and rest[1] == "in":
            values = [r for value in rest[2:] for r in _resolve(value, here, cwd, env)]
            if values and all(not unknown for _, unknown in values):
                env[rest[0]] = [path for path, _ in values]
            else:
                env.pop(rest[0], None)
        elif head == "cd" and rest:
            placed = _resolve(rest[0], here, cwd, env)
            if placed and not placed[0][1]:
                cwd = placed[0][0]
        elif head == "eval":   # eval runs its words as a command line
            reason = _scan(" ".join(rest), cwd, here, dict(env))
            if reason:
                return reason
        elif head == "osascript":   # `do shell script "<command>"`
            for arg in rest:
                if "do shell script" in arg:
                    reason = _scan(arg.split("do shell script", 1)[1].replace("\\", " ").replace('"', " "), cwd, here, dict(env))
                    if reason:
                        return reason
        elif head == "ssh" or head in SHELLS:   # `ssh host '<command>'` and `bash -c '<command>'`: judge the inner command (Auditor, 2026-10-21)
            for arg in rest:
                if REMOTE_RE.search(arg):
                    reason = _scan(arg, "/root" if head == "ssh" else cwd, "/root" if head == "ssh" else here, dict(env))   # a remote command starts in the remote home
                    if reason:
                        return reason
            if head in SHELLS and not _shell_script(rest) and prev[:1] in (["echo"], ["printf"]) and _piped(command or "", segment):
                reason = _scan(" ".join(prev[1:]).replace("\\n", "\n"), cwd, here, dict(env))   # `echo '<command>' | sh`
                if reason:
                    return reason
        elif head == "find":
            reason = _find_reason(rest, cwd, here, env)
            if reason:
                return reason
        elif head == "xargs":
            fed = prev if _piped(command or "", segment) and not any(w.startswith("<") for w in words) else []   # a `;`/`&&` before it or a `<` redirect makes stdin unknown
            reason = _xargs_reason(rest, fed, cwd, here, env)
            if reason:
                return reason
        elif PY_HEAD_RE.fullmatch(head):
            python = (python or []) + words
        elif head in DELETERS or head == "mv" or (head == "rsync" and any(w.startswith("--delete") for w in rest)):
            targets = _targets(rest)
            if head == "mv":
                targets = targets[:-1]   # the last word is where it goes, not what leaves
            elif head == "rsync":
                targets = targets[-1:]   # --delete empties the destination
            for target in targets:
                home = here
                if head == "rsync" and re.match(r"^[^/\s:]+:", target):
                    target, home = re.sub(r"^[^/\s:]+:", "", target), "/root"   # host:path is judged on the remote
                for path, unknown in _resolve(target, home, cwd, env):
                    if _hits(path):
                        return UNRESOLVED if unknown else MESSAGE
        prev = [head] + rest
    return _python_reason(command or "", python, cwd, here) if python is not None else ""


def cache_delete_reason(command: str, cwd: str = "", home: str | None = None) -> str:
    return _scan(command, cwd or "", home or str(Path.home()), {})
