"""A Hugging Face token never reaches a command line, commit, PR body or message (Dom via Strategy, 2026-10-21).

Dom put an ACCOUNT-WIDE token on the VPS at /root/.hf/token and ruled that Hugging Face jobs run from the VPS. Only a job script may read it
(`HF_TOKEN=$(cat /root/.hf/token)` inside the script); it is never echoed, never on a command line, log, PR body, receipt or message.

THE RULE (Auditor recheck, 2026-10-21, after v1 matched the wording `cat /root/.hf/token` and missed globs, `to''ken`, `F=/root/.hf; cat $F/token`,
`cd /root/.hf && cat token`, `python3 -c open(...)`, `tar`/`scp` of the folder and `echo $HF_TOKEN`): fail CLOSED on any Bash command whose
RESOLVED arguments touch /root/.hf (or ~/.hf) or print the token variable. A token split across strings cannot be caught by a regex, so the
guard closes the read instead. `hf_token_reason` returns the deny reason for a command that
  - contains a token-shaped string (`hf_` + 34 letters or digits) anywhere,
  - has an argument that resolves (shell quotes removed, `~`, `$HOME`, `cd`, an inline `F=...` or `for` assignment, and a shell glob, where a
    `*` does not match a dot-folder) to the token folder or a file inside it,
  - has an argument that resolves to a PARENT of the folder (`/root`, `/`) while the command copies or archives (tar, zip, scp, rsync, cp, mv ...),
  - prints the token variable ($HF_TOKEN, $HUGGING_FACE_HUB_TOKEN ...) on a command line,
  - runs `python -c` / `node -e` style code that names `.hf`, or does any of the above inside `ssh host '<command>'` / `bash -c '<command>'`.
Exceptions: ls, stat and test (they read no content), and the text of a message flag (-m, --body, --title ...), which is data.
REAL GLOBS (Dom via Strategy, 2026-10-23): `cd ~; for d in * .[a-zA-Z]*; do du "$d"; done` was denied on the Mac because `.[a-zA-Z]*` COULD match
`.hf`, though ~/.hf does not exist there. For a LOCAL command a glob is now expanded against the real filesystem as the shell would (cwd after a
leading `cd`) and only a real match that touches the token folder denies. Still judged in the abstract (fail closed): ssh / bash -c / `$(..)`
bodies, scp and rsync, a glob whose literal prefix is already the token folder (`/root/.hf/tok*`), a command with a subshell `( )`, and any
cwd that cannot be worked out (`cd "$X"`, `cd -`, pushd, popd): there a relative glob that could name `.hf` is denied.
HOLES (Auditor, 2026-10-23, older than the real-glob change; a false allow outranks a false block): (1) `shopt -s dotglob`, `setopt globdots`,
GLOBIGNORE make a plain `?hf` or `*f` match `.hf`: when the command sets one, globs are judged in the abstract with the dot rule off, never by looking
at the disk; (2) a zsh qualifier `?hf(D)` or a bash extglob group `@(.hf)` does the same, and the splitter would cut the word at its parens, so the
text is normalised first (`@(.hf)` -> `*`, `?hf(D)` -> `?hf`); (3) `[^c]` negates in bash and zsh, so it is read as `[!c]`; (4) a symlink to the token
folder is the token folder: every local path, the matches of a local glob and the cwd are also judged after realpath.
Scope on purpose: Bash command lines only (a job script's own body, Write/Edit content and non-Bash tools are out of scope). The reason never
repeats the token.
"""
from __future__ import annotations

import fnmatch
import glob
import os
import re
import shlex
from pathlib import Path

from .bash_write_guard import _split as split_segments
from .cache_guard import ASSIGN_RE, KEYWORDS, WRAPPERS, _resolve

TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])hf_[A-Za-z0-9]{34}")
HF_ROOTS = sorted({"/root/.hf", str(Path.home() / ".hf")})
TOKEN_VARS = ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACE_HUB_TOKEN")
VAR_WORD = re.compile(r"\$\{?(?:" + "|".join(TOKEN_VARS) + r")\b|^(?:" + "|".join(TOKEN_VARS) + r")$")
CODE_HF = re.compile(r"(?<![\w])\.hf(?![\w])")
SAFE_HEADS = {"ls", "stat", "test", "[", "[["}
SHELL_HEADS = {"ssh", "bash", "sh", "zsh"}
CODE_HEADS = {"node", "perl", "ruby", "php", "deno", "bun", "awk", "gawk", "lua"}
COPIERS = {"tar", "zip", "7z", "7za", "scp", "rsync", "cp", "mv", "ditto", "cpio", "pax", "gzip", "bzip2", "xz", "zstd"}
TREE_SEARCHERS = {"rg", "ag", "ack"}   # recursive by default
GREPS = {"grep", "egrep", "fgrep", "zgrep"}
PLACEHOLDER = "${_SUBST_}"
MESSAGE_FLAGS ={"-m", "--message", "--body", "-b", "--title", "-t", "--notes", "--subject"}
MESSAGE_PREFIXES = tuple(f"{flag}=" for flag in MESSAGE_FLAGS if flag.startswith("--"))
MESSAGE = ("A Hugging Face token never goes on a command line, in a commit, PR body or message, and nothing outside a job script touches "
           "/root/.hf (Dom 2026-10-21). Only a job script reads it: HF_TOKEN=$(cat /root/.hf/token) inside the script, never echoed or "
           "copied. Run the job from the VPS and read the result file instead.")


# A word that carries a zsh glob qualifier `?hf(D)` or a bash extglob group `@(.hf)` / `+(.hf)`: it can name a dot-folder whatever its letters are.
QUALIFIED = re.compile(r"[@+*?!]\(|[?*\]][^()]*\([A-Za-z.^/@*+-]+\)$")
QUALIFIER_TEXT = re.compile(r"[@+*?!]\(|[^\s\"'()]*[?*\]][^\s\"'()]*\([A-Za-z.^/@*+-]+\)")   # a glob word with a `(D)` suffix; plain `bug(s)` is text
EXTGLOB_GROUP = re.compile(r"[@+*?!]\([^()]*\)")
QUALIFIER_SUFFIX = re.compile(r"([^\s\"'()]*[?*\]][^\s\"'()]*)\([A-Za-z.^/@*+-]+\)")
# Options that make a glob match dot-folders (Auditor holes, 2026-10-23): bash `shopt -s dotglob` / GLOBIGNORE, zsh `setopt globdots` / GLOB_DOTS.
DOT_OPTIONS = re.compile(r"glob_?dots|dotglob|globignore", re.IGNORECASE)


def _component_match(pattern: str, target: str, dots: bool = False) -> bool:
    if QUALIFIED.search(pattern):
        pattern, dots = "*", True
    pattern = pattern.replace("[^", "[!")   # bash and zsh negate a class with ^; fnmatch only knows !
    if target.startswith(".") and not pattern.startswith(".") and not dots:
        return False   # a shell * or ? never matches a leading dot (unless dotglob / globdots / a qualifier says so)
    return fnmatch.fnmatchcase(target, pattern)


def _could_be(path: str) -> bool:
    """A path with a `*` from an unresolved substitution can hold slashes (`$(printf /roo)t` is /root): match it against the whole
    path of the token folder, its file, and every parent, not component by component."""
    pool = {str(p) for root in HF_ROOTS for p in Path(root).parents} | set(HF_ROOTS) | {f"{root}/token" for root in HF_ROOTS}
    return any(fnmatch.fnmatchcase(target, path) for target in pool)


def _touches(path: str, copier: bool, anywhere: bool = True, unknown: bool = False, dots: bool = False) -> bool:
    if unknown and copier and _could_be(path):
        return True
    parts = [""] if path == "/" else path.rstrip("/").split("/")
    # a ".hf" folder under ANY prefix: a symlink to /root read as /tmp/rr/.hf/token (Auditor, 2026-10-21). Not for a word with spaces:
    # that is a command string or a sentence, not a path (ssh and bash -c strings are judged by the recursion).
    if anywhere and any(part == ".hf" or (
            (any(c in part for c in "*?[") or QUALIFIED.search(part)) and (part.startswith(".h") or dots or QUALIFIED.search(part))
            and _component_match(part, ".hf", dots)) for part in parts):
        return True
    for root in HF_ROOTS:
        target = root.split("/")
        paired = list(zip(parts, target))
        if not all(_component_match(p, t, dots) for p, t in paired):
            continue
        if len(parts) >= len(target) or copier:   # the folder itself or inside it; a parent only when the command copies or archives
            return True
    return False


GLOB_CHARS = "*?["
GLOB_CAP = 5000


def _expand_locally(path: str) -> list[str] | None:
    """What the shell would pass for a glob over a LOCAL folder, or None when it must be judged in the abstract: not a glob, or its literal
    prefix (the part before the first glob component) does not exist here, or the prefix is itself inside the token folder (`/root/.hf/tok*`)."""
    if not any(ch in path for ch in GLOB_CHARS) or QUALIFIED.search(path):
        return None
    path = path.replace("[^", "[!")
    parts = path.split("/")
    first = next(i for i, part in enumerate(parts) if any(ch in part for ch in GLOB_CHARS))
    prefix = "/".join(parts[:first]) or "/"
    if not os.path.isdir(prefix) or _touches(prefix, False):
        return None
    matches = glob.glob(path)
    return matches if len(matches) <= GLOB_CAP else None   # too many to judge one by one: the abstract check decides (Auditor, 2026-10-23: a cut list failed open)


def _could_name_token_folder(word: str, dots: bool = False) -> bool:
    """A relative word with a glob component that could be `.hf`: denied when the cwd is not known."""
    return not word.startswith(("/", "~", "$")) and any(
        (any(ch in part for ch in GLOB_CHARS) or QUALIFIED.search(part)) and _component_match(part, ".hf", dots) for part in word.split("/"))


def _reads_trees(head: str, rest: list[str], command: str) -> bool:
    """True when the command reads or copies everything under the folders it is given (so a parent of /root/.hf counts):
    archivers and copiers, rg/ag/ack, grep -r, and find that runs something on what it finds (-exec ... or piped to xargs)."""
    if head in COPIERS or head in TREE_SEARCHERS:
        return True
    if head in GREPS:
        return any((w.startswith("-") and not w.startswith("--") and ("r" in w[1:] or "R" in w[1:])) or w in {"--recursive", "--dereference-recursive"}
                   for w in rest)
    if head == "find":
        return any(w in {"-exec", "-execdir", "-ok", "-okdir"} for w in rest) or "xargs" in command
    return False


def _mask_substitutions(command: str) -> tuple[str, list[str]]:
    """Replace each live `$(...)` / backtick substitution by the placeholder `${_SUBST_}` (an unknown variable: it has no parens for the
    segment splitter to cut at, and resolves to `*`) and return the bodies, so the word around it stays whole
    (`$(readlink -f X)/origins/y.json` keeps its literal suffix instead of being cut at the paren) and each body is judged as a command of its
    own. Single-quoted text is literal and untouched. Unbalanced text is returned as it came."""
    out, bodies, i, n = [], [], 0, len(command)
    while i < n:
        ch = command[i]
        if ch == "'":
            j = command.find("'", i + 1)
            j = n - 1 if j == -1 else j
            out.append(command[i:j + 1])
            i = j + 1
        elif command.startswith("$(", i):
            depth, j = 1, i + 2
            while j < n and depth:
                depth += (command[j] == "(") - (command[j] == ")")
                j += 1
            if depth:
                return command, []
            bodies.append(command[i + 2:j - 1])
            out.append(PLACEHOLDER)
            i = j
        elif ch == "`":
            j = command.find("`", i + 1)
            if j == -1:
                return command, []
            bodies.append(command[i + 1:j])
            out.append(PLACEHOLDER)
            i = j + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out), bodies


def hf_token_reason(command: str, cwd: str = "", home: str | None = None, _depth: int = 0, _env: dict | None = None, _dots: bool = False) -> str:
    command = command or ""
    if TOKEN_RE.search(command):
        return MESSAGE
    dots = _dots or bool(DOT_OPTIONS.search(command))   # globs may match dot-folders from here on: judged in the abstract, never by looking at the disk
    command, bodies = _mask_substitutions(command)
    # the segment splitter cuts a word at its parens, so `?hf(D)` would reach the matcher as `?hf`: a qualifier anywhere in the text closes the globs too
    if QUALIFIER_TEXT.search(command):
        dots = True
        command = QUALIFIER_SUFFIX.sub(r"\1", EXTGLOB_GROUP.sub("*", command))   # `@(.hf)` becomes `*`, `?hf(D)` becomes `?hf`: the pieces the splitter would lose
    if _depth < 4 and any(hf_token_reason(body, cwd, home, _depth + 1, dict(_env or {}), dots) for body in bodies):
        return MESSAGE
    here = home or str(Path.home())
    env = {} if _env is None else _env
    grouped = "(" in command   # a subshell `( cd x; ... )` or a function body: the cwd the guard tracks may not be the one the shell uses
    cwd_ok = bool(cwd)
    for segment in split_segments(command):
        try:
            words = shlex.split(segment.rstrip("\\ \t"))   # a find -exec ... \; leaves a trailing backslash after the split on ;
        except ValueError:   # cannot be read: close it only when the segment names the folder or the variable
            if ".hf" in segment or any(var in segment for var in TOKEN_VARS):
                return MESSAGE
            continue
        assigns = []
        while words and (words[0] in KEYWORDS or words[0] in WRAPPERS or re.fullmatch(r"\w+=\S*", words[0])):
            if words[0] not in KEYWORDS and words[0] not in WRAPPERS:
                assigns.append(words[0])
            words = words[1:]
        if not words:
            for assign in assigns:   # a bare `name=value` sets it for the rest of the command
                name, value = ASSIGN_RE.fullmatch(assign).groups()
                resolved = _resolve(value, here, cwd, env)
                if resolved and all(not unknown for _, unknown in resolved):
                    env[name] = [path for path, _ in resolved]
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
            continue
        if head == "cd":
            placed = _resolve(rest[0], here, cwd, env) if rest and rest[0] != "-" else []
            if not rest:
                cwd, cwd_ok = here, True   # a bare `cd` goes home
            elif placed and not placed[0][1]:
                cwd, cwd_ok = placed[0][0], True
            else:
                cwd_ok = False             # `cd -`, `cd "$X"`, `cd $(..)`: the folder cannot be worked out here
            continue
        if head in {"pushd", "popd"}:
            cwd_ok = False
            continue
        if head in SAFE_HEADS:
            continue
        if head in SHELL_HEADS and _depth < 3:
            for arg in rest:
                if " " in arg and hf_token_reason(arg, "", "/root", _depth + 1, dict(env), dots):
                    return MESSAGE
        copier = _reads_trees(head, rest, command)
        code = head.startswith("python") or head in CODE_HEADS
        skip = False
        for word in rest:
            if skip:
                skip = False
                continue
            if word in MESSAGE_FLAGS:
                skip = True
                continue
            if word.startswith(MESSAGE_PREFIXES) or (word.startswith("-m") and len(word) > 2 and not word.startswith("--")):
                continue
            if VAR_WORD.search(word) or (code and CODE_HF.search(word)):
                return MESSAGE
            candidates = [word, re.sub(r"^-{1,2}[\w-]+=", "", word)]   # --opt=/path
            if head in {"scp", "rsync"}:
                candidates.append(re.sub(r"^[^/\s:]+:", "", word))      # host:/path names a path on the remote
            # a real expansion only for a plain local command: not ssh/bash -c/$(..) bodies (_depth), not scp/rsync (a remote half), not a subshell
            local = _depth == 0 and not grouped and cwd_ok and not dots and head not in {"scp", "rsync"} and head not in SHELL_HEADS
            if (grouped or not cwd_ok or dots) and any(_could_name_token_folder(c, dots) for c in candidates):
                return MESSAGE
            for candidate in candidates:
                spaced = bool(re.search(r"\s", candidate))
                for path, unknown in _resolve(candidate, "/root" if candidate != word else here, cwd, env):
                    # a relative word like `*` stays unjoined in _resolve (a `*` can stand for an unknown variable): place it in the tracked cwd to expand it
                    at = path if path.startswith("/") else os.path.join(cwd, path)
                    real = _expand_locally(at) if local and not unknown and candidate == word else None
                    if real is not None:   # what the shell passes, and where each of those paths really leads (a symlink to the token folder)
                        if any(_touches(match, copier, not spaced) or _touches(os.path.realpath(match), copier, not spaced) for match in real):
                            return MESSAGE
                    elif _touches(path, copier, not spaced, unknown, dots):
                        return MESSAGE
                    elif local and not unknown and candidate == word and not any(ch in path for ch in GLOB_CHARS):
                        if _touches(os.path.realpath(path), copier, not spaced):   # a literal path through a symlink, or the cwd itself a symlink
                            return MESSAGE
    return ""
