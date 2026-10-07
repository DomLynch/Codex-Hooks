from __future__ import annotations

import json
import re
from pathlib import Path


MAX_TRANSCRIPT_CHARS = 4000


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if isinstance(item, dict):
            parts.append(str(item.get("text") or ""))
    return "\n".join(part for part in parts if part)


def sanitize(text: str) -> str:
    text = re.sub(r"(?<![A-Za-z0-9])tp-[A-Za-z0-9]+", "tp-<redacted>", text)
    text = re.sub(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]+", "sk-<redacted>", text)
    text = re.sub(r"(?<![A-Za-z0-9])ghp_[A-Za-z0-9_]+", "ghp_<redacted>", text)
    text = re.sub(r'"encrypted_content":\s*"[^"]+"', '"encrypted_content": "<omitted>"', text)
    text = re.sub(r"<hook_prompt\b[^>]*>.*?</hook_prompt>", "<hook_prompt omitted>", text, flags=re.DOTALL)
    return text.replace("<hook_prompt", "<hook-prompt")


def strip_hook_prompts(text: str) -> str:
    return re.sub(r"<hook_prompt\b[^>]*>.*?</hook_prompt>", "", text or "", flags=re.DOTALL | re.IGNORECASE).strip()


def compact(text: str, limit: int) -> str:
    text = sanitize(text)
    if len(text) <= limit:
        return text
    return text[: limit // 2] + "\n...[slice-elided by compactor - full content was present in the actual tool call]...\n" + text[-limit // 2 :]


def _render_entry(role: str, body: str) -> str:
    return f"{role}: {body}" if role == "TOOL CALL" else f"{role}:\n{body}"


def _tool_output_text(output: object) -> str:
    text = _content_text(output) if isinstance(output, list) else str(output)
    parts = []
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            record = None
        if isinstance(record, dict) and "chunk_id" in record and isinstance(record.get("output"), str):
            status = {key: value for key, value in record.items()
                      if key not in {"chunk_id", "wall_time_seconds", "original_token_count", "output"}}
            line = json.dumps(status) + "\n" + record["output"]
        parts.append(line)
    return "\n".join(parts)


class CallText(str):
    """Shortened call text for display; `.full` is the complete, unshortened `name: args` for decisions."""
    full: str = ""
    name: str = ""
    args: str = ""


def _full_command(call: object) -> tuple[str | None, str | None]:
    """(complete command, cwd) of a recorded call: the `command` of a JSON tool input, else the raw arguments."""
    args = getattr(call, "args", "")
    if not args:
        return None, None
    try:
        data = json.loads(args)
    except (ValueError, TypeError):
        return args, None
    if isinstance(data, dict):
        command = data.get("command") or data.get("cmd")
        cwd = data.get("cwd") or data.get("workdir")
        if isinstance(command, list):
            command = " ".join(str(c) for c in command)
        return (str(command) if command else args), (str(cwd) if cwd else None)
    return args, None


class ToolOutputBody(str):
    """TOOL OUTPUT body for display. The attributes are the structured evidence the correctness gates decide on
    (never re-parsed from the shortened text): the COMPLETE command, the tool-use id, native success/failure."""
    command: str | None = None
    cwd: str | None = None
    text: str = ""
    tool_use_id: str = ""
    is_error: bool | None = None
    exit_status: int | None = None


_EXIT_CODE_RE = re.compile(r"^\s*(?:Exit code|exit status|Process exited with code)\s*[:=]?\s*(-?\d+)", re.IGNORECASE)


def tool_entry(payload: dict, calls: dict[str, str]) -> tuple[str, str] | None:
    kind = payload.get("type")
    call_id = str(payload.get("call_id") or "unknown")
    if kind in {"function_call", "custom_tool_call"}:
        name = str(payload.get("name") or "tool")
        args = str(payload.get("arguments") or payload.get("input") or "")
        calls[call_id] = CallText(compact(f"{name}: {args}", 300))
        calls[call_id].full = f"{name}: {args}"
        calls[call_id].name, calls[call_id].args = name, args
        cap = 8000 if name.lower() in {"apply_patch", "edit", "write", "multi_edit", "multiedit", "apply_diff"} else 800
        return "TOOL CALL", f"{name}: [{call_id}] {compact(args, cap)}"
    if kind in {"function_call_output", "custom_tool_call_output"}:
        output = payload.get("output") or ""
        text = _tool_output_text(output)
        command = _bounded_compact(calls.get(call_id, "command outside slice"), 120)
        body = compact(text, 1000)
        failure = _failure_excerpt(text, 350) if len(text) > 1000 else ""
        receipt = _receipt_excerpt(text, body, 300) if len(text) > 1000 else ""
        result = ToolOutputBody(f"[{call_id}] {command}\n{failure}\n{receipt}\n{body}")
        result.command, result.cwd = _full_command(calls.get(call_id))
        result.text = text
        result.tool_use_id = call_id
        is_error = payload.get("is_error")
        result.is_error = is_error if isinstance(is_error, bool) else None
        status = payload.get("exit_status")
        match = _EXIT_CODE_RE.match(text)
        result.exit_status = status if isinstance(status, int) else (int(match.group(1)) if match else None)
        return "TOOL OUTPUT", result
    return None


def _bounded_compact(text: str, limit: int, marker: str = "\n...[slice-elided by compactor]...\n") -> str:
    text = sanitize(text)
    if len(text) <= limit:
        return text
    body_budget = max(0, limit - len(marker))
    head = body_budget // 2
    tail = body_budget - head
    return (text[:head] + marker + (text[-tail:] if tail else ""))[:limit]


def _failure_excerpt(text: str, limit: int) -> str:
    lines = [line for line in text.splitlines() if re.search(
        r'\bFAILED\b|AssertionError|\bERROR\b|\bError:|exit_code["\s:=]+[1-9]|Process exited with code [1-9]',
        line)]
    return _bounded_compact("\n".join(lines), limit) if lines else ""


# Lines that carry a verification result. Head/tail compaction of a long multi-section output
# drops the middle, which is where `curl -w "HTTP %{http_code}"`, `N passed` and `git rev-parse`
# lines land in a `cd && echo === && ...` chain. In replay, ~17% of true receipts were lost this
# way and the reviewer blocked a correct claim. Keep them, the same way failure lines are kept.
_RECEIPT_LINE_RE = re.compile(
    r"HTTP/\d(?:\.\d)?\s+\d{3}\b|\bHTTP\s+\d{3}\b|http_code\W{0,3}\d{3}|\bstatus(?: code)?\W{0,3}[1-5]\d\d\b"
    r"|\b\d+ passed\b|\b\d+ (?:tests? )?(?:passing|ok)\b|All checks passed|\bTests?:\s+\d+|\bPASS(?:ED)?\b"
    r"|Active:\s*\w+|active \(running\)|:\s*(?:active|inactive|failed)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_SHA_LINE_RE = re.compile(r"\b(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")
_SHA_CONTEXT_RE = re.compile(r"\b(?:commit|HEAD|sha|origin|main|master|release|version|tip|deployed|live|VPS|GitHub|Mac|parity|synced)\b", re.IGNORECASE)


def _receipt_excerpt(text: str, kept: str, limit: int) -> str:
    """Receipt lines from the part of `text` that `kept` (the compacted body) no longer shows."""
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped in kept:
            continue
        if _RECEIPT_LINE_RE.search(line) or (_SHA_LINE_RE.search(line) and _SHA_CONTEXT_RE.search(line)):
            lines.append(stripped)
    return "[kept from elided output]\n" + _bounded_compact("\n".join(lines), limit) if lines else ""


TEST_RUNNER_RE = re.compile(
    r"\b(pytest|unittest|npm (run )?(test|quality|check|lint)|node --test|vitest|jest|"
    r"cargo test|go test|make (test|check)|ruff check|mypy|tsc\b|\.quality-gate)",
    re.IGNORECASE,
)
FAILURE_ACK_RE = re.compile(
    r"\b(fail(s|ed|ing|ure)?|error|broken|red|not passing|unresolved|regression|AssertionError|"
    r"still (fails|failing|broken))\b",
    re.IGNORECASE,
)


_COMMAND_JSON_RE = re.compile(r'\{\s*"command"\s*:\s*"((?:[^"\\]|\\.)*)')   # first line may be cut mid-JSON
_TARGET_RE = re.compile(r"^(?!-)[\w.@~-]*[/.][\w./@~:\[\]-]*$")   # a path-like token: tests/, x.py, x.py::t[1-2], .
# Options that do not change which tests are collected. Anything else on the command line (a selection flag, an
# attached value like -ktest_one, --ignore=..., an option we do not know) makes the run opaque: it is the same
# check as itself and covers nothing else. Allow-list, not block-list (recheck 3, 2026-09-27).
# "Takes a value" is not "cannot change coverage" (recheck 4): an ini override (-o), a config file (-c), a plugin
# (-p) or a directory switch (--rootdir, -C) can deselect tests, so they are NOT listed and make the run opaque.
# Value flags are per runner because one letter means different things (-n is xdist workers in pytest; -p is a
# plugin in pytest but the file PATTERN in `unittest discover`).
_VALUE_FLAGS = {
    "*": {"-W", "--tb", "--durations", "--maxfail", "--timeout", "--cov-report", "--color", "--capture", "--reporter",
          "--test-reporter", "--basetemp"},
    "pytest": {"-n", "--cov"},
}
_NEUTRAL_FLAGS = {"-q", "-v", "-x", "-s", "-l", "--quiet", "--verbose", "--exitfirst", "--no-header", "--no-summary",
                  "--disable-warnings", "--strict-markers", "--strict", "--fix", "--noEmit", "--pretty", "--no-cov", "--"}
_NEUTRAL_CLUSTER_RE = re.compile(r"^-[qvxsl]+$|^-r[a-zA-Z]*$")   # -qx, -vv, -rA
# Selection under UNCHANGED configuration: a known subset of what the same paths collect, so a broader run of those
# paths covers it (`pytest tests/x.py` covers `pytest tests/x.py -k t`). Anything else opaque is config-dependent
# (-o/-c/-p/--rootdir, an unknown flag): neither adding nor removing it proves equivalent coverage (recheck 5), so
# only the identical command clears it.
_SUBSET_VALUE_FLAGS = {"-k", "-m", "--deselect", "--ignore", "--ignore-glob", "--grep", "-t", "--testNamePattern",
                       "--testPathPattern", "-run"}
_SUBSET_FLAGS = {"--lf", "--last-failed", "--sw", "--stepwise"}
_SUBCOMMANDS = {"run", "discover", "exec", "check"}
_OPAQUE = "\x00"          # prefix of a target that stands for "this exact command": a known subset of its paths
_OPAQUE_CONFIG = "\x00!"  # same, but config-dependent: covered by nothing except the identical command


def _check_identity(first_line: str) -> tuple[str, frozenset[str]] | None:
    """(runner, targets) of the check a TOOL OUTPUT line ran, or None. The runner is the test/lint tool named on
    the line. Targets are its path-like arguments (empty = the whole suite). A run with any option that is not on
    the coverage-neutral allow-list, or any argument that is not a recognizable path, gets an opaque target: the
    exact command. It is the same check as itself and covers nothing else, because "I could not tell what ran"
    must never mean "everything ran"."""
    return _identity_from_text(first_line.split(":", 1)[-1] if first_line.startswith("[") else first_line)


def _identity_from_text(text: str) -> tuple[str, frozenset[str]] | None:
    m = TEST_RUNNER_RE.search(text)
    if not m:
        return None
    j = _COMMAND_JSON_RE.search(text)
    command = j.group(1).encode().decode("unicode_escape", "ignore") if j else text
    runner = " ".join(m.group(1).lower().split())
    tail = command[command.lower().find(runner.split()[0]) + len(runner):]
    tail = re.split(r"\s(?:&&|\|\||;|\||>|2>)\s|\n", tail)[0]
    tokens = tail.split()
    value_flags = _VALUE_FLAGS["*"] | _VALUE_FLAGS.get(runner, set())
    targets, opaque, skip = set(), "", False
    for tok in tokens:
        if skip:
            skip = False
            continue
        flag = tok.split("=", 1)[0]
        if flag in value_flags or flag in _SUBSET_VALUE_FLAGS:
            skip = "=" not in tok
            opaque = opaque or (_OPAQUE if flag in _SUBSET_VALUE_FLAGS else "")
        elif tok in _SUBSET_FLAGS or (runner == "pytest" and tok[:2] in {"-k", "-m"} and len(tok) > 2):   # --lf, -ktest_one
            opaque = opaque or _OPAQUE
        elif tok in _NEUTRAL_FLAGS or _NEUTRAL_CLUSTER_RE.match(tok) or tok in _SUBCOMMANDS:
            continue
        elif not tok.startswith("-") and _TARGET_RE.match(tok):
            targets.add(tok)
        else:
            opaque = _OPAQUE_CONFIG   # -o/-c/-p/--rootdir, --anything-unknown, a non-path word: coverage unknown
    if opaque:
        targets.add(opaque + " ".join(tokens))   # keeps its paths too, so a broader path rerun can cover a subset
    return runner, frozenset(targets)


def _rerun_covers(failed: frozenset[str], rerun: frozenset[str]) -> bool:
    """A whole-suite rerun (no targets, or `.`) covers anything except a config-dependent failure. An opaque rerun,
    or a config-dependent failure, matches only the identical check. A path rerun covers a failure whose paths all
    sit under it (a file covers its nodes and its own subset runs); it never covers a whole-suite failure or an
    opaque failure without paths."""
    if any(r.startswith(_OPAQUE) for r in rerun) or any(t.startswith(_OPAQUE_CONFIG) for t in failed):
        return failed == rerun   # before `.`: `pytest . -k x` is filtered, not everything
    if not rerun or "." in rerun:
        return True
    paths = {t for t in failed if not t.startswith(_OPAQUE)}   # an opaque failure with no paths is never covered
    return bool(paths) and all(any(t == r or t.startswith((r.rstrip("/") + "/", r + "::")) for r in rerun) for t in paths)


_CD_RE = re.compile(r"(?:^|[;&|(]\s*)(?:cd|pushd)\s+(\"[^\"]+\"|'[^']+'|[^\s;&|)]+)")


def _runner_is_negated(command: str) -> bool:
    """Every part of the command that runs a check is itself an expected failure: `! check`, or `check || true`
    (`|| :`, `|| exit 0`). A negative precondition elsewhere (`test ! -f x && check`, `test ! -f x || check`) does not
    make the check's own failure expected."""
    parts = re.split(r"(&&|\|\||;|\n)", command)
    runners = [i for i in range(0, len(parts), 2) if TEST_RUNNER_RE.search(parts[i])]
    return bool(runners) and all(
        parts[i].strip().startswith("!")
        or (i + 2 < len(parts) and parts[i + 1] == "||" and re.match(r"\s*(?:true|:|exit 0)\b", parts[i + 2]))
        for i in runners)


def _apply_cds(command: str, cwd: str, persist: bool = False) -> str:
    """The directory after the `cd`s in `command` (a `cd` inside quotes, such as an ssh string, does not move this
    shell; with persist=True a `cd` inside a ( subshell ) does not either). A target that cannot be resolved (a
    variable, or a relative path with no known base) makes the directory '?'+its text: the same text matches itself
    and nothing else, so two different unknown places never look like one."""
    import os

    for match in _CD_RE.finditer(command):
        before = command[: match.start()]
        if before.count("'") % 2 or before.count('"') % 2 or (persist and match.group(0).lstrip().startswith("(")):
            continue
        target = os.path.expanduser(match.group(1).strip("\"'"))
        if any(ch in target for ch in "$`("):
            cwd = f"?{target}"   # usually an absolute path in a variable: it replaces the directory, it does not extend it
        elif os.path.isabs(target):
            cwd = target
        elif cwd and not cwd.startswith("?"):
            cwd = os.path.join(cwd, target)
        else:
            cwd = f"?{target}"   # relative to a directory we cannot know: the same text matches itself, nothing else
    return cwd if cwd.startswith("?") else (os.path.normpath(cwd) if cwd else "")


_OPAQUE_CONTEXT_RE = re.compile(
    r"\b(?:ssh|docker|podman|kubectl|nsenter|chroot|su)\b|\b(?:ba|z|da)?sh\s+-c\b|\benv\s+-C\b"   # runs somewhere else
    r"|\bcd\s*(?:&&|;|\||$)")                                                                        # bare cd goes home
_ASSIGNMENT_RE = re.compile(r"\b[A-Za-z_]\w*=\S*\s*[;&]")


def _check_cwd(command: str, tool_cwd: str | None, shell_cwd: str) -> str:
    """Where a check ran: the `cd`s before it, applied to the tool's cwd or else the cwd this turn's earlier commands
    left the shell in. '' = the session's own directory. A check run somewhere we cannot resolve (inside ssh / docker /
    `bash -c '...'`, after a bare `cd` or `env -C`, a quoted cd, or `VAR=x; cd $VAR`) is '?'+everything before the check
    command: the same ssh host and directory is the same place whatever options the check then has, and two different
    unresolved places never clear each other."""
    runner = TEST_RUNNER_RE.search(command)
    head = command[: runner.start()] if runner else command
    quoted_cd = any(m.string[: m.start()].count("'") % 2 or m.string[: m.start()].count('"') % 2 for m in _CD_RE.finditer(head))
    where = _apply_cds(head, tool_cwd or shell_cwd)
    if quoted_cd or _OPAQUE_CONTEXT_RE.search(head) or (where.startswith("?") and "$" in where and _ASSIGNMENT_RE.search(head)):
        return "?" + head.strip()
    return where


def _native_failure(body: str, command: str | None) -> bool:
    """The tool itself reported failure (is_error / non-zero exit). A check written to fail (`! cmd`, `cmd || true`)
    is not a failed check on that signal alone."""
    failed = getattr(body, "is_error", None) is True or (getattr(body, "exit_status", None) or 0) != 0
    return failed and not (command and _runner_is_negated(command))


def _native_failure_text(body: str) -> str:
    """Output text only EXPLAINS a failure already established by the native status."""
    lines = [ln.strip() for ln in (getattr(body, "text", "") or "").splitlines() if ln.strip()]
    status = getattr(body, "exit_status", None)
    head = f"[tool reported failure, exit {status}]" if status is not None else "[tool reported failure (is_error)]"
    return _bounded_compact(head + "\n" + "\n".join(lines[-6:]), 300)


def unacknowledged_check_failure(entries: list[tuple[str, str]], final_message: str) -> str:
    """Deterministic gate for the most common audit finding: a test/check runner failed
    this turn and the final message says nothing about it.

    A failure is identified by its check (runner + targets). Only a later run of the same runner that covers
    those targets AND shows a pass receipt clears it: `pytest tests/test_logging.py` does not clear a failed
    `pytest tests/test_payment.py`, `pytest --help` clears nothing, and `ruff` never clears `pytest`.
    Returns the unresolved failure excerpt(s) to quote back, or "" when nothing to flag.
    """
    failed: dict[tuple[str, frozenset[str], str], str] = {}
    shell_cwd = ""   # the Bash tool's shell keeps its directory between commands
    for role, body in entries:
        if role != "TOOL OUTPUT":
            continue
        # Decide from the COMPLETE command when the evidence record carries it; the first display line is a
        # shortened presentation and only the fallback for plain-text entries.
        full = getattr(body, "command", None)
        where = _check_cwd(full or "", getattr(body, "cwd", None), shell_cwd)
        shell_cwd = _apply_cds(full or "", getattr(body, "cwd", None) or shell_cwd, persist=True)
        identity = _identity_from_text(full) if full else _check_identity(body.split("\n", 1)[0])
        if not identity:
            continue
        excerpt = _failure_excerpt(body, 300)
        if not excerpt and _native_failure(body, full):
            excerpt = _native_failure_text(body)
        if excerpt:
            failed[(*identity, where)] = excerpt
            continue
        if _RECEIPT_LINE_RE.search(body):
            runner, targets = identity
            failed = {k: v for k, v in failed.items() if not (k[0] == runner and k[2] == where and _rerun_covers(k[1], targets))}
    if not failed or FAILURE_ACK_RE.search(final_message or ""):
        return ""
    return _bounded_compact("\n".join(failed.values()), 600)


EDIT_TOOL_RE = re.compile(r"^(Edit|Write|MultiEdit|NotebookEdit):", re.IGNORECASE)
_FILE_PATH_ARG_RE = re.compile(r'"file_path":\s*"([^"]+)"')
_CONFLICT_OPEN_RE = re.compile(r"^<{7} \S", re.MULTILINE)
_CONFLICT_CLOSE_RE = re.compile(r"^>{7} \S", re.MULTILINE)


def edited_paths(entries: list[tuple[str, str]]) -> list[str]:
    """file_path args from Edit/Write-style TOOL CALLs this turn, in order, deduped."""
    seen: list[str] = []
    for role, body in entries:
        if role == "TOOL CALL" and EDIT_TOOL_RE.match(body.split(":", 1)[0] + ":"):
            for path in _FILE_PATH_ARG_RE.findall(body):
                if path not in seen:
                    seen.append(path)
    return seen


def conflict_marker_files(paths: list[str]) -> list[str]:
    """Files among `paths` that still contain git conflict markers."""
    hits = []
    for raw in paths:
        f = Path(raw).expanduser()
        try:
            if f.is_file() and f.stat().st_size < 5_000_000:
                text = f.read_text(errors="replace")
                if _CONFLICT_OPEN_RE.search(text) and _CONFLICT_CLOSE_RE.search(text):
                    hits.append(raw)
        except OSError:
            continue
    return hits


SENSITIVE_PATH_RE = re.compile(
    r"(auth|login|session|token|secret|credential|passw|crypt|payment|billing|stripe|checkout|"
    r"admin|permission|rbac|oauth|jwt|api[_-]?key|\bacl\b)",
    re.IGNORECASE,
)
_WARNING_LINE_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+\S")


def sensitive_paths(entries: list[tuple[str, str]]) -> list[str]:
    """Edited paths this turn that touch auth/secrets/payment/admin surfaces."""
    return [p for p in edited_paths(entries) if SENSITIVE_PATH_RE.search(p)]


def sensitive_section(paths: list[str]) -> str:
    return (
        "SENSITIVE PATHS EDITED THIS TURN (auth / secrets / payment / admin):\n"
        + "\n".join(f"- {p}" for p in paths)
        + "\nApply a security-review standard to these. Any concern about authentication, "
        "authorization, secret handling, injection, or data exposure is BLOCKING — return "
        "VERDICT: ISSUES for it, never PASS with a warning."
    )


def pass_warnings(verdict_text: str) -> list[str]:
    """Bullet/numbered lines after a PASS verdict — the reviewer's 'optional Warnings'."""
    if not (verdict_text or "").startswith("VERDICT: PASS"):
        return []
    rest = verdict_text.split("\n", 1)[1] if "\n" in verdict_text else ""
    return [re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", line).strip()
            for line in rest.splitlines() if _WARNING_LINE_RE.match(line)]


DOC_EXT_RE = re.compile(r"\.(md|mdx|markdown|txt|rst|adoc|asciidoc)$", re.IGNORECASE)
_MUTATING_BASH_RE = re.compile(
    r"(\bsed\s+-i|\btee\b|(?<![<>])>(?!>)|>>|\bmv\b|\bcp\b|\brm\b|\bgit\s+(commit|push|add|checkout|reset|rebase|merge)\b|"
    r"\bnpm\b|\bpnpm\b|\byarn\b|\bpip\b|\bpoetry\b|\bmake\b|\bcargo\b|\bgo\s+(build|run|install)\b|\bpython3?\s+\S+\.py)",
    re.IGNORECASE,
)


def doc_only_turn(entries: list[tuple[str, str]], final_message: str, deliverable_re: re.Pattern) -> bool:
    """True when the turn only edited documentation and made no shipping claim.

    Conservative: any Edit/Write on a non-doc file, any Bash call that looks like it
    mutates the tree or runs code, or any push/deploy/done claim disqualifies. A wrong
    answer here only costs one ordinary audit, so bias toward False."""
    edited = edited_paths(entries)
    if not edited or not all(DOC_EXT_RE.search(p) for p in edited):
        return False
    for role, body in entries:
        if role == "TOOL CALL" and body.startswith("Bash:") and _MUTATING_BASH_RE.search(body):
            return False
    return not deliverable_re.search(final_message or "")


DIFF_CAP = 6000   # 2500 cut a 3.5k new hook in half; the reviewer judged the rest from the agent's words


def turn_diff(cwd: str, paths: list[str], cap: int = DIFF_CAP, since: str = "") -> str:
    """Diff of the files this turn edited, plus the head of any new untracked ones.

    Diffs against HEAD as it was at `since` (the turn's start, via the reflog) so edits
    the agent already committed during the turn still reach the reviewer; lanes with a
    checkpoint hook or a PR flow commit before they stop, which left `git diff HEAD` empty.
    Falls back to HEAD when there is no reflog entry. Scoped to `paths` so another agent's
    changes in a shared worktree are not attributed to this turn. "" when not a repo."""
    if not cwd or not paths:
        return ""
    import subprocess

    base = Path(cwd).expanduser()
    if not (base / ".git").exists() and subprocess.run(["git", "-C", str(base), "rev-parse", "--git-dir"],
                                                       capture_output=True, timeout=5).returncode:
        return ""
    rel = []
    for raw in paths:
        f = Path(raw).expanduser()
        try:
            rel.append(str(f.resolve().relative_to(base.resolve())) if f.is_absolute() else raw)
        except ValueError:
            continue  # outside the repo
    if not rel:
        return ""
    parts = []
    try:
        ref = "HEAD"
        if since:
            probe = subprocess.run(["git", "-C", str(base), "rev-parse", "--verify", "-q", f"HEAD@{{{since}}}"],
                                   capture_output=True, text=True, timeout=5)
            if probe.returncode == 0 and probe.stdout.strip():
                ref = f"HEAD@{{{since}}}"
        d = subprocess.run(["git", "-C", str(base), "diff", ref, "--", *rel], capture_output=True, text=True, timeout=10)
        if d.returncode and "unknown revision" in (d.stderr or ""):   # fresh repo, no HEAD yet
            d = subprocess.run(["git", "-C", str(base), "diff", "--", *rel], capture_output=True, text=True, timeout=10)
        if d.stdout.strip():
            parts.append(d.stdout.strip())
        u = subprocess.run(["git", "-C", str(base), "ls-files", "--others", "--exclude-standard", "--", *rel],
                           capture_output=True, text=True, timeout=5).stdout.split()
        for name in u[:5]:
            try:
                body = (base / name).read_text(errors="replace")
                parts.append(f"--- NEW FILE {name} ---\n{body[:800]}")
            except OSError:
                continue
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return _bounded_compact("\n\n".join(parts), cap, "\n...[diff elided to fit]...\n") if parts else ""



def turn_commits(cwd: str, since: str, limit: int = 5) -> list[str]:
    """SHAs committed in THIS worktree since `since` (ISO, the turn's start), oldest first.

    Read from the worktree's own HEAD reflog, so it sees commits however they were made (Edit tool, a Bash
    script, `git commit` after a python heredoc) and never another worktree's. 2026-09-27: the hooks lane and
    the Auditer edit through Bash, so edited_paths() was empty and the reviewer judged 7 code turns from prose."""
    if not cwd or not since:
        return []
    import subprocess
    from datetime import datetime

    try:
        start = datetime.fromisoformat(since.replace("Z", "+00:00")).timestamp()
        out = subprocess.run(["git", "-C", cwd, "reflog", "-200", "--date=unix", "--format=%H %gd %gs", "HEAD"],
                             capture_output=True, text=True, timeout=5).stdout
    except (ValueError, OSError, subprocess.SubprocessError):
        return []
    shas: list[str] = []
    for line in out.splitlines():
        m = re.match(r"([0-9a-f]{40}) HEAD@\{(\d+)\} (\S+)", line)
        if not m or int(m.group(2)) < start:
            continue
        if m.group(3).startswith("commit") and m.group(1) not in shas:
            shas.append(m.group(1))
    return list(reversed(shas))[-limit:]


def turn_commits_diff(cwd: str, since: str, cap: int = DIFF_CAP) -> str:
    """The code this turn committed: per commit its file list, then the patch of non-test files (tests are
    named in the list; the code is what the reviewer must read). "" when the turn committed nothing."""
    import subprocess

    parts = []
    try:
        for sha in turn_commits(cwd, since):
            stat = subprocess.run(["git", "-C", cwd, "show", "--stat", "--format==== commit %h %s ===", sha],
                                  capture_output=True, text=True, timeout=10).stdout.strip()
            code = subprocess.run(["git", "-C", cwd, "show", "--format=", "--patch", sha, "--", ".",
                                   ":(exclude,glob)**/tests/**", ":(exclude,glob)**/test_*"],
                                  capture_output=True, text=True, timeout=10).stdout.strip()
            parts.append("\n".join(x for x in (stat, code) if x))
    except (OSError, subprocess.SubprocessError):
        return ""
    return _bounded_compact("\n\n".join(parts), cap, "\n...[diff elided to fit]...\n") if parts else ""

# Narrow by design: an explicit HTTP status claim about a named host is the only live/deploy
# claim shape a regex can separate from narrative. Broad "is live / shipped / deployed" matching
# fired on 89% of real turns in replay (commit links, "live API", "shipped 30%", quoted SHAs).
_HTTP_CLAIM_RE = re.compile(
    r"(?:\b(?:HTTP/?(?:1\.1|2)?\s*|returns?\s+(?:a\s+)?|responds?\s+(?:with\s+)?|status\s+(?:code\s+)?|got\s+(?:a\s+)?)|→\s*|=\s*)(2\d\d)\b(?:\s*OK)?",
    re.IGNORECASE,
)
_HTTP_CLAIM_NEG_RE = re.compile(
    r"\b(?:not|isn't|no|never|failed|instead of|expected|expecting|should|want|until|before)\b(?:[^.\n]|\.(?=\w)){0,40}\b2\d\d\b"
    r"|\b2\d\d\b(?:[^.\n]|\.(?=\w)){0,25}\b(?:not|isn't|expected|but got|instead|yet)\b",
    re.IGNORECASE,
)
# Lowercase only: hostnames in prose are lowercase; "OSF.IO/Q4DJS" inside a DOI is not a claim about
# osf.io. No `.app`: `agent.app` / `src.app` are Python modules far more often than hosts.
_HOST_RE = re.compile(r"\b((?:[a-z0-9-]+\.)+(?:com|org|net|io|dev|ai|co\.uk|ae))\b")
_HOST_IGNORE = {"github.com", "example.com", "localhost.com"}
# A 2xx written under a heading like "Acceptance criteria" / "Deploy + verify:", or as the expected
# result of a backticked command (`curl X` → 200), is a plan or a checklist, not a report.
_PLAN_CONTEXT_RE = re.compile(r"acceptance criteria|verify:|to verify|checklist|next steps?\b|\bto-?do\b|\brun:", re.IGNORECASE)
_EXPECTED_RESULT_RE = re.compile(r"`[^`\n]*\b(?:curl|wget|http|fetch)\b[^`\n]*`\s*(?:→|->|=>|:|=)\s*$")
_HTTP_OK_RE = re.compile(r"\bHTTP/\S+\s+2\d\d\b|(?<![\d.])2\d\d(?![\d.])", re.MULTILINE)
_HTTP_FAIL_RE = re.compile(r"curl: \(\d+\)|Connection refused|Could not resolve|Failed to connect|timed out", re.IGNORECASE)
_CALL_ID_RE = re.compile(r"\[(toolu_[A-Za-z0-9]+|call_[A-Za-z0-9]+|[a-z]\w{0,12})\]")
_ELIDED_MARKER = "slice-elided by compactor"


def unreceipted_live_claim(entries: list[tuple[str, str]], final_message: str) -> str:
    """Final message asserts an HTTP 2xx from a named host, and no TOOL OUTPUT this turn shows a
    successful response naming that host. Returns the hosts lacking a receipt, or "".

    Fires only on positive evidence of absence. A paired call that names the host but whose output
    was elided by the compactor is indeterminate and stays silent (left to the model)."""
    msg = final_message or ""
    if _HTTP_CLAIM_NEG_RE.search(msg):
        return ""
    # Only hosts named within ~100 chars of a 2xx claim are being claimed about. A long
    # message can mention many hosts; demanding receipts for all of them fired on real
    # audit turns that had curled exactly the host they reported.
    hosts: set[str] = set()
    for m in _HTTP_CLAIM_RE.finditer(msg):
        line_start = msg.rfind("\n", 0, m.start()) + 1
        if _PLAN_CONTEXT_RE.search(msg[max(0, m.start() - 200): m.start()]) or _EXPECTED_RESULT_RE.search(msg[line_start: m.start()]):
            continue
        window = msg[max(0, m.start() - 100): m.end() + 100]
        hosts |= {h.group(1) for h in _HOST_RE.finditer(window)}
    hosts -= _HOST_IGNORE
    if not hosts:
        return ""
    # Pair calls with outputs by call_id. The host is matched against the CALL too (800-char cap,
    # URL near the front) because the output embeds only a 300-char copy of the command and a
    # `cd x && echo "..." && curl URL` prefix pushes the URL into the elided middle. Success is
    # still judged from the OUTPUT, so a failed call never counts as a receipt.
    calls = {m.group(1): body for role, body in entries if role == "TOOL CALL"
             for m in [_CALL_ID_RE.search(body)] if m}
    candidates: list[tuple[str, bool, bool, bool]] = []  # (text, ok, failed, elided)
    for role, body in entries:
        if role != "TOOL OUTPUT":
            continue
        m = _CALL_ID_RE.search(body)
        text = ((calls.get(m.group(1), "") if m else "") + "\n" + body).lower()
        out = body.partition("\n")[2]
        candidates.append((text, bool(_HTTP_OK_RE.search(out)), bool(_HTTP_FAIL_RE.search(out)), _ELIDED_MARKER in out))
    missing = []
    for host in sorted(hosts):
        mine = [c for c in candidates if host in c[0]]
        if any(ok and not failed for _, ok, failed, _e in mine):
            continue
        if any(elided or (ok and failed) for _, ok, failed, elided in mine):
            continue  # can't tell from what survived compaction; leave it to the reviewer
        missing.append(host)
    return ", ".join(missing)


def deterministic_findings(entries: list[tuple[str, str]], final_message: str, cwd: str) -> list[str]:
    """All mechanical pre-model gates. Each item is a complete, actionable finding."""
    findings: list[str] = []
    missed = unacknowledged_check_failure(entries, final_message)
    if missed:
        findings.append(
            "A test/check run in this turn failed and your final message does not mention it. "
            "Either fix it and show the rerun, or state the failure and why it is expected:\n" + missed
        )
    conflicts = conflict_marker_files(edited_paths(entries))
    if conflicts:
        findings.append(
            "Files you edited this turn still contain git conflict markers (<<<<<<< / >>>>>>>). "
            "Resolve them and show the clean file: " + ", ".join(conflicts)
        )
    live = unreceipted_live_claim(entries, final_message)
    if live:
        findings.append(
            "Your final message reports an HTTP 2xx from a host, but no tool output this turn shows a successful "
            "response from it. Run the request and include the output, or drop the status claim. Unreceipted: " + live
        )
    return findings


def compact_task_entries(entries: list[tuple[str, str]], max_chars: int) -> str:
    text = sanitize("\n\n".join(_render_entry(role, body) for role, body in entries))
    if len(text) <= max_chars:
        return text
    failures = [(body, _failure_excerpt(body, 220)) for role, body in entries if role == "TOOL OUTPUT"]
    failures = [(body, excerpt) for body, excerpt in failures if excerpt]
    history = ""
    if failures and max_chars >= 1600:
        history = "\n\nCHECK HISTORY (excerpts, not verdicts; match later reruns and expected outcomes):\n"
        history += "\n".join(_bounded_compact(body.splitlines()[0], 100) + "\n" + excerpt
                             for body, excerpt in failures[-4:])
        history = _bounded_compact(history, min(900, max_chars // 4))
    max_chars -= len(history)
    first_role, first_body = entries[0]
    head_budget = max_chars // 4
    first = sanitize(_render_entry(first_role, first_body))
    if len(first) > head_budget:
        first = _bounded_compact(first, head_budget, "\n...[head truncated]...\n")
    marker = "\n\n...[older task details omitted to stay focused]...\n\n"
    final = next((_render_entry(role, body) for role, body in reversed(entries) if role == "ASSISTANT"), "")
    recent = "\n\n".join(_render_entry(role, body) for role, body in entries[-3:] if role not in {"TOOL OUTPUT", "ASSISTANT"})
    tail = _bounded_compact("\n\n".join(part for part in (final, recent) if part), max_chars // 2)
    outputs = [body for role, body in entries if role == "TOOL OUTPUT"]
    # Retain the initial verification as well as the latest results, in order.
    selected = outputs if len(outputs) <= 4 else outputs[:1] + outputs[-3:]
    header = (f"\n\n...[verification receipts preserved: {len(selected)}/{len(outputs)}; "
              f"omitted tool outputs={len(outputs) - len(selected)}; excerpts only]...\n\n") if outputs else ""
    receipt_budget = max(0, max_chars - len(first) - len(tail) - len(marker) - len(header))
    excerpts = [""] * len(selected)
    for remaining, idx in zip(range(len(selected), 0, -1), sorted(range(len(selected)), key=lambda i: len(selected[i]))):
        budget = max(0, receipt_budget // remaining - 1)
        body = selected[idx]
        # Keep explicit status lines visible even when head/tail elision cuts the result.
        status = "\n".join(line for line in body.splitlines()
                           if re.search(r"exit_code|Process exited|\bFAILED\b|\bERROR\b", line))
        status = _bounded_compact(status, budget // 2)
        prefix = "TOOL OUTPUT:\n" + ("[status excerpts]\n" + status + "\n" if status else "")
        excerpts[idx] = prefix + _bounded_compact(body, max(0, budget - len(prefix)))
        receipt_budget -= len(excerpts[idx]) + 1
    receipts = "\n".join(excerpts)
    receipt_block = header + receipts
    return (first + receipt_block + marker + tail)[:max_chars] + history


def compact_latest_task(entries: list[tuple[str, str]], max_chars: int, include_previous: bool = False) -> str:
    start = next((i for i in range(len(entries) - 1, -1, -1) if entries[i][0] == "USER"), 0)
    current = entries[start:]
    context = ""
    if include_previous and max_chars >= 600 and start and not any(role.startswith("TOOL") for role, _ in current):
        previous_start = next((i for i in range(start - 1, -1, -1) if entries[i][0] == "USER"), 0)
        previous = entries[previous_start:start]
        if any(role == "TOOL OUTPUT" for role, _ in previous):
            context = "\n\nPRIOR TURN CONTEXT (historical data, not a new request or fresh verification):\n"
            context += compact_task_entries(previous, max_chars // 3 - len(context))
    return compact_task_entries(current, max_chars - len(context)) + context


def latest_task_slice(transcript_path: str, fallback: str = "", max_chars: int = MAX_TRANSCRIPT_CHARS,
                      include_previous: bool = False) -> str:
    if not transcript_path:
        return sanitize(fallback)[-max_chars:]
    path = Path(transcript_path)
    if not path.exists():
        return sanitize(fallback)[-max_chars:]
    try:
        lines = path.read_text(errors="replace").splitlines()
    except Exception:
        return sanitize(fallback)[-max_chars:]

    entries: list[tuple[str, str]] = []
    calls: dict[str, str] = {}
    for line in lines:
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") != "response_item":
            continue
        payload = obj.get("payload") or {}
        item_type = payload.get("type")
        if item_type == "message":
            role = str(payload.get("role") or "message").upper()
            text = _content_text(payload.get("content"))
            if role == "USER" and text.lstrip().startswith("<hook_prompt"):
                remaining = strip_hook_prompts(text)
                if remaining:
                    entries.append(("USER", remaining))
                    continue
                role = "PRIOR REVIEW (not a new request)"
                text = re.sub(r"</?hook_prompt\b[^>]*>", "", text)
            if text:
                entries.append((role, text))
        else:
            entry = tool_entry(payload, calls)
            if entry:
                entries.append(entry)

    if not entries:
        return sanitize(fallback)[-max_chars:]
    return compact_latest_task(entries, max_chars, include_previous)
