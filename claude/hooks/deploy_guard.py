#!/usr/bin/env python3
"""PreToolUse (Bash): the one-deployer rule, made mechanical.

While Frankendom's deploy.sh runs it holds ~/.claude/state/deploy_in_flight.json. A Mac shared by a dozen lanes
cannot run their browser checks, test suites and bakes at the same time without the deploy taking three times as
long (measured 2026-09-21: two 100 s checks took 15 min each under load 30+). So while a fresh lock exists, this
hook refuses the heavy commands from every other session with a one-line reason and the time already spent, and
lets everything else through. The deployer's own session (the lock's pid is its child) is never blocked, and neither
is deploy.sh itself. A lock whose pid is dead or older than STALE_S is ignored: a killed deploy cannot wedge anyone.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from lib.bash_write_guard import bash_write_reason

LOCK_PATH = Path(os.environ.get("DEPLOY_LOCK") or Path.home() / ".claude" / "state" / "deploy_in_flight.json")
STALE_S = 45 * 60
# What competes with a deploy for the Mac: real browsers, whole test suites, builds and bakes.
# 2026-09-26: herolook-game-stills / herolook-stills (Playwright + Vite), herolook-kill-record (the sim) and build-creatures
# (spawns blender) ran through the a981f7a5 lock: the name list needs stills, record and build-.
# armour-contact-sheet (Playwright) and any herolook-*.mjs render are heavy too; the python herolook_*.py helpers are light and
# never match here (python, .py). Unit tests of one file,
# git, gh, curl, greps and edits are not on this list on purpose.
HEAVY_RE = re.compile(
    r"(?:^|[\s;&|(])(?:"
    r"npx\s+playwright\b|node\s+scripts/[\w-]*(?:check|preview|gate|quality|battery|stills|record|build-|contact-sheet|herolook-|pixelmator-|textures-)[\w-]*\.mjs|node\s+artifacts/\S+browser\S*\.mjs"
    r"|npm\s+run\s+(?:test:all|test:slow|test:browser|quality(?::\w+)?|build|release-checks?)\b|npm\s+(?:run\s+)?test\b(?!:)"
    r"|node\s+scripts/release-checks\.mjs"
    r"|(?:node|tsx)\s+--test\b(?:\s+-\S+)*\s+(?:\S*[*?\[$]\S*|\S*/|\"\")(?=\s|$)|(?:node|tsx)\s+--test\b(?:\s+-\S+)*\s+(?:\S*/)?[^\s./*-][^\s./*]*(?=(?:\s+\d*>&?\S*)*\s*$)|tsx\s+--test\b(?:\s+-\S+)*\s+[^-\s]\S*\s+[^-\s]\S*"   # dir, glob, substitution, tsx (Auditor on #129)
    r"|node\s+--test\s+tests/\*|node\s+--test\s*$|node\s+--test(?:\s+-[^\s]*)*\s+[^-\s][^\s]*\s+[^-\s][^\s]*"
    r"|(?:\S*/)?[Bb]lender\b|vite\s+build\b|meshopt\S*|gltfpack\b"
    r")"
)
# An allowed segment RUNS the deploy script: optional VAR=x / env / sh|bash|zsh prefix, then the path as the command word.
# Merely naming it (test -f scripts/deploy.sh, cat, echo) exempts nothing, and each ; && || | ( ) segment is judged alone.
ALLOW_RE = re.compile(r"^\s*(?:(?:env\s+|\w+=\S*\s+|(?:ba|z)?sh\s+(?:-\w+\s+)*))*(?:\S*/)?scripts/deploy\.sh\b")
SEGMENT_SPLIT_RE = re.compile(r"[;&|()`\n]+")
# 2026-10-14 (Lead): scripts/candidate-contents-check.mjs is seconds of git merge-tree plus GitHub REST and needs GH_TOKEN, so it cannot go to the VPS;
# Deploy runs it on every cut. `check` in its name made HEAVY_RE match once it moved into trunk's scripts/ (RPG-game#2728). Exempt that ONE script
# as a segment's command (leading env only; any other script, npm test, vite build or playwright is judged on its own segment).
LIGHT_SCRIPT_RE = re.compile(r"^\s*(?:\w+=\S*\s+)*node\s+(?:\S*/)?scripts/candidate-contents-check\.mjs(?=\s|$)")
# 2026-10-08 (Lead + COO): submitting a Hugging Face job runs nothing on the Mac, even when its payload names `npm run test:all`.
# Exempt only a segment whose command word is `hf jobs`; a real local run beside it (; && | $( ) `) is its own segment and still refused.
# Only HF_* env prefixes: PATH=./bin hf ... could run a local script named hf (Auditor LOW on #105).
HF_JOBS_RE = re.compile(r"^\s*(?:HF_\w+=\S*\s+)*hf\s+jobs\s")
# Pixelmator Pro (Hero Look's headless texture step, 2026-09-26: ML super resolution + export via osascript) is heavy. The app is
# named inside the osascript argument's quotes, which executable_text() strips as data, so this looks at the RAW command: a real
# osascript / open -a invocation (in the executable text) whose own segment (no ; && || between) names Pixelmator, inline -e,
# a .scpt/.applescript path, a heredoc or `open -a`. Plain osascript (notifications) and a commit message naming Pixelmator pass.
APP_HOST_RE = re.compile(r"(?:^|[\s;&|(])(?:osascript\b|open\s+-[ab]\b)")
# Same line and segment (a newline or single | ends the command: 2026-09-27 review found `osascript -e notify` then
# `git commit -m pixelmator` on the next line was blocked), or inside the osascript heredoc's own body.
PIXELMATOR_RE = re.compile(
    r"(?:osascript\b|open\s+-[ab]\b)[^\n;|&]*?pixelmator"
    r"|osascript\b[^\n;|&]*<<-?\s*['\"]?(\w+)\b['\"]?[^\n]*\n(?:(?!\n\s*\1\s*(?:\n|$)).)*?pixelmator",
    re.IGNORECASE | re.DOTALL)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _is_descendant_of(pid: int, ancestor: int) -> bool:
    """Is this hook (and so the tool call) running under the deployer's process tree?"""
    import subprocess
    seen = 0
    while pid > 1 and seen < 64:
        if pid == ancestor:
            return True
        try:
            out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, timeout=2).stdout.strip()
            pid = int(out) if out else 0
        except Exception:
            return False
        seen += 1
    return False


def active_lock(now: datetime | None = None) -> dict | None:
    try:
        data = json.loads(LOCK_PATH.read_text())
        pid = int(data.get("pid", 0))
        started = datetime.fromisoformat(str(data.get("started", "")).replace("Z", "+00:00"))
    except Exception:
        return None
    now = now or datetime.now(timezone.utc)
    age = (now - started).total_seconds()
    if age < 0 or age > STALE_S or not _pid_alive(pid):
        return None
    data["age_s"] = int(age)
    return data


HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n[ \t]*\2[ \t]*(?=\n|$)", re.DOTALL)
QUOTED_RE = re.compile(r"\"(?:[^\"\\]|\\.)*\"|'[^']*'")
SUBSTITUTION_RE = re.compile(r"(?<!\\)\$\(([^)]*)\)|(?<!\\)`((?:[^`\\]|\\.)*)`")   # \` and \$( are literal text
SHELL = r"(?:^|[\s;&|(/])(?:ba|z)?sh"   # a shell command, not a file named x.sh
RUNS_ITS_ARGUMENT_RE = re.compile(rf"(?:{SHELL}\s+-\w*c|(?:^|[\s;&|(])eval)\s*$")   # bash -c "...", zsh -lc '...', eval '...'
SHELL_STDIN_RE = re.compile(rf"{SHELL}\s*$")                                         # bash <<EOF: the body is run


def executable_text(command: str) -> str:
    """The parts of a shell line that run: commit messages, PR bodies and script heredocs are data, not commands."""
    def heredoc(m: re.Match) -> str:
        return m.group(0) if SHELL_STDIN_RE.search(command[:m.start()].rstrip()) else ""

    def quoted(m: re.Match) -> str:
        inner = m.group(0)[1:-1]
        before = SEGMENT_SPLIT_RE.split(QUOTED_RE.sub('""', stripped[:m.start()]))[-1]
        # `hf jobs run img bash -c "..."`: that payload runs on Hugging Face, so it is data like any other quoted argument (#105).
        if RUNS_ITS_ARGUMENT_RE.search(stripped[:m.start()]) and not HF_JOBS_RE.search(before):
            return f" {inner} "
        # "$(npm test)" and "`npm test`" run even inside double quotes; single quotes run nothing.
        if m.group(0)[0] == "'":
            return '""'
        return '""\n' + "\n".join(a or b for a, b in SUBSTITUTION_RE.findall(inner))   # own segments: a segment exemption never covers $( )

    stripped = HEREDOC_RE.sub(heredoc, command)
    return QUOTED_RE.sub(quoted, stripped)


# 2026-10-08 (Dom, Optimizer item 2): heavy runs are refused on the Mac at any time, not only under the deploy lock (Mac load sat at
# 29-57 on 10 cores from lane test/browser runs). Exempt: the deploy itself (ALLOW_RE / lock holder), Safari/WebKit and real-iPhone rows,
# the sparring GPU row, and MAC_HEAVY_OK=1 for when the VPS is down.
_ROW = r"(?:webkit|safari|iphone|simctl|sparring)"
# Judged per heavy segment: the exempt word must be the command or script (npm script name, scripts/ file, --project), never a path,
# branch or test-file argument; MAC_HEAVY_OK=1 only as that segment's own leading env (Auditor on #104).
MAC_EXEMPT_RE = re.compile(
    rf"^\s*(?:\w+=\S*\s+)*MAC_HEAVY_OK=1\b|^\s*(?:\w+=\S*\s+)*(?:xcrun\s+simctl\b|npm\s+run\s+[\w:-]*{_ROW}"
    rf"|node\s+(?:\S*/)?scripts/[\w-]*{_ROW}|npx\s+playwright\s+test\b[^\n]*--project[= ]{_ROW})", re.IGNORECASE)
# 2026-10-09 (Lead, Dom's rule: WebKit and real-clock rows never run on HF/the VPS): the nine Mac-only release rows
# (.quality-gate.json release_commands 2, 4, 22, 44, 45, 48, 49, 52, 54) are allowed by EXACT command, never by pattern.
# Only leading env assignments and trailing redirects are tolerated; any other argument or a different script is refused.
MAC_ONLY_ROWS = (
    "node scripts/roster-browser-check.mjs", "node scripts/audio-preview.mjs --label audio-gate --check",
    "node scripts/arena-audio-check.mjs", "node scripts/sparring-browser-check.mjs",
    "node scripts/double-tap-browser-check.mjs", "node scripts/browser-replay-check.mjs",
    "node scripts/browser-replay-check.mjs --engine webkit", "node scripts/next-fight-black-check.mjs",
    "node scripts/pit-landscape-webkit.mjs --engine webkit",   # release row 54 (Web, RPG-game#2280; Lead asked 2026-10-12)
)
MAC_ROW_RE = re.compile(r"^\s*(?:\w+=\S*\s+)*(?:" + "|".join(re.escape(r) for r in MAC_ONLY_ROWS) + r")(?:\s+\d*>&?\S*)*\s*$")
# 2026-10-09 (COO, Web): scripts/capture-mac.sh only ssh's to the VPS and launches a Hugging Face job, so the test command it carries
# does not run on this Mac. Narrow exemption: that script as the command, --hf-ok among its options (before the lane), and nothing that
# could point the "VPS" back at the Mac: no --host, no leading env (CAPTURE_HOST / CAPTURE_SSH), no other option.
# Auditor on #113: the script must be THIS checkout's scripts/capture-mac.sh (relative path, never /tmp/x/scripts/... or ../evil/...), byte-identical to
# trunk's blob (a lane branch could edit it to run locally), and no CAPTURE_* variable may be exported in the environment (the script honours them).
CAPTURE_SCRIPT = "scripts/capture-mac.sh"


def capture_script_is_trunk(cwd: str) -> bool:
    if not cwd:
        return False
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=10, check=True).stdout.strip()
    try:
        return bool(git("hash-object", CAPTURE_SCRIPT)) and git("hash-object", CAPTURE_SCRIPT) == git("rev-parse", f"refs/remotes/origin/HEAD:{CAPTURE_SCRIPT}")
    except (OSError, subprocess.SubprocessError):
        return False   # fail closed


SHELL_META_RE = re.compile(r"[;&|\n(){}`$<]")
# Auditor on #121: a redirect target is a write. Only 2>&1, >/dev/null and >artifacts/... may appear (no `..`); a bare `>` followed by
# a word, or a `>` glued to anything else, refuses the exemption (it would truncate e.g. a tracked scripts/lib file).
REDIRECT_OK_RE = re.compile(r"\d?>&\d|\d?>>?/dev/null|\d?>>?artifacts/[\w./-]*")


def redirects_ok(command: str) -> bool:
    return all(">" not in tok or (REDIRECT_OK_RE.fullmatch(tok) and ".." not in tok) for tok in command.split())


def timeout_ok(value: str) -> bool:   # gpu-run's own range: 1m..3h
    m = re.fullmatch(r"(\d{1,3})([mh])", value)
    return bool(m) and (1 <= int(m[1]) <= 180 if m[2] == "m" else 1 <= int(m[1]) <= 3)


def hf_capture_wrapper(segment: str, cwd: str = "", command: str = "") -> bool:
    # Auditor on #120: the blob check reads the cwd BEFORE the command runs, so an earlier segment (cd /tmp/x && ..., cp evil
    # scripts/capture-mac.sh; ..., git checkout evil -- ...) could change what runs. The exemption is for a command that IS
    # the capture call and nothing else: one segment, no separators, grouping or substitution (redirects like 2>&1 are fine).
    whole = re.sub(r"\s+\d*>&?\S*", "", command.strip())
    if not command or SHELL_META_RE.search(whole) or not redirects_ok(command) or whole.split() != re.sub(r"\s+\d*>&?\S*", "", segment).split():
        return False
    words = whole.split()
    if not words or words[0] not in (CAPTURE_SCRIPT, "./" + CAPTURE_SCRIPT):
        return False
    if any(name.startswith("CAPTURE_") for name in os.environ) or not capture_script_is_trunk(cwd):
        return False
    i, hf_ok = 1, False
    while i < len(words) and words[i].startswith("--"):
        if words[i] == "--hf-ok":
            hf_ok, i = True, i + 1
        elif words[i] in ("--dir", "--prio") and i + 1 < len(words):
            i += 2
        else:
            return False
    return hf_ok and len(words) - i >= 2   # a lane and a command


# Lead, 2026-10-10: `node scripts/gpu-run.mjs <sha> [--blender] [--timeout 20m] -- <cmd>` only launches a Hugging Face T4 job (the command after
# `--` is single-quoted into the job script and runs there). Narrow exemption, same rules as capture-mac: the call is the ENTIRE command (no
# separator, grouping or substitution anywhere, `--` command included), relative script path, a 40-hex sha, only the script's own options, and
# nothing that could point it at this Mac: the whole scripts/ folder must equal trunk's, and the environment must not redirect it
# (GPU_RUN_HF replaces the `hf` binary; HF_BIN, HF_LEDGER_DIR, HF_WALL_ROWS_ENV_FILE, NODE_OPTIONS and NODE_PATH change what runs).
# HF_TOKEN is the login itself and is fine.
GPU_RUN_SCRIPT = "scripts/gpu-run.mjs"
GPU_RUN_ENV = ("HF_BIN", "HF_LEDGER_DIR", "HF_WALL_ROWS_ENV_FILE", "NODE_OPTIONS", "NODE_PATH")


def scripts_dir_is_trunk(cwd: str) -> bool:
    if not cwd:
        return False
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=15)
    try:
        if git("rev-parse", "--verify", f"refs/remotes/origin/HEAD:{GPU_RUN_SCRIPT}").returncode != 0 or not (Path(cwd) / GPU_RUN_SCRIPT).is_file():
            return False
        clean = git("diff", "--quiet", "refs/remotes/origin/HEAD", "--", "scripts").returncode == 0
        extra = git("ls-files", "--others", "--exclude-standard", "--", "scripts")
        return clean and extra.returncode == 0 and not extra.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False   # fail closed


def gpu_run_wrapper(segment: str, cwd: str = "", command: str = "") -> bool:
    whole = re.sub(r"\s+\d*>&?\S*", "", command.strip())
    if not command or SHELL_META_RE.search(whole) or not redirects_ok(command) or whole.split() != re.sub(r"\s+\d*>&?\S*", "", segment).split():
        return False
    words = whole.split()
    if words[:2] not in (["node", GPU_RUN_SCRIPT], ["node", "./" + GPU_RUN_SCRIPT]) or len(words) < 6:
        return False
    if not re.fullmatch(r"[0-9a-f]{40}", words[2]):
        return False
    i = 3
    while i < len(words) and words[i] != "--":
        if words[i] == "--blender":
            i += 1
        elif words[i] == "--timeout" and i + 1 < len(words) and timeout_ok(words[i + 1]):
            i += 2
        else:
            return False
    if i >= len(words) - 1:   # no `--`, or nothing after it
        return False
    if any(n.startswith("GPU_RUN_") or n in GPU_RUN_ENV for n in os.environ):
        return False
    return scripts_dir_is_trunk(cwd)


# Mac middle tier, piece 2 (Strategy + Dom, 2026-10-12): `bash scripts/lanejob/lanejob-mac.sh CMD...` runs a CPU-only job on this Mac only when
# the VPS would queue or spill, under its own gates (load, release lock, one slot, nice, 30 min cap). Same shape as the other wrappers: the call
# is the ENTIRE command (no separator, grouping or substitution), relative script path, the script and the whole scripts/lanejob folder equal
# trunk's, and the environment must not redirect it (any LANEJOB_* but the two off-switches, plus NODE_OPTIONS/NODE_PATH/BASH_ENV/ENV).
# The inner command may never be a browser, full suite, build or Blender run: those stay on the VPS whatever the wrapper does.
LANEJOB_MAC_SCRIPT = "scripts/lanejob/lanejob-mac.sh"
LANEJOB_MAC_ENV = ("NODE_OPTIONS", "NODE_PATH", "BASH_ENV", "ENV")
LANEJOB_MAC_OFF = ("LANEJOB_NO_MAC", "LANEJOB_NO_SPILL")
LANEJOB_NODE_TEST_RE = re.compile(r"node --test(?: -[^\s*]+)*(?: [^-\s*][^\s*]*)+")
LANEJOB_INNER_DENY_RE = re.compile(r"playwright|test:all|test:slow|test:browser|quality|release-check|build|blender|webkit|safari|iphone|simctl|meshopt|gltfpack", re.IGNORECASE)


def lanejob_dir_is_trunk(cwd: str) -> bool:
    if not cwd:
        return False
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=15)
    try:
        if git("rev-parse", "--verify", f"refs/remotes/origin/HEAD:{LANEJOB_MAC_SCRIPT}").returncode != 0 or not (Path(cwd) / LANEJOB_MAC_SCRIPT).is_file():
            return False
        clean = git("diff", "--quiet", "refs/remotes/origin/HEAD", "--", "scripts/lanejob").returncode == 0
        extra = git("ls-files", "--others", "--exclude-standard", "--", "scripts/lanejob")
        return clean and extra.returncode == 0 and not extra.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False   # fail closed


def lanejob_mac_wrapper(segment: str, cwd: str = "", command: str = "") -> bool:
    whole = re.sub(r"\s+\d*>&?\S*", "", command.strip())
    if not command or SHELL_META_RE.search(whole) or not redirects_ok(command) or whole.split() != re.sub(r"\s+\d*>&?\S*", "", segment).split():
        return False
    words = whole.split()
    if len(words) < 3 or words[0] != "bash" or words[1] not in (LANEJOB_MAC_SCRIPT, "./" + LANEJOB_MAC_SCRIPT):
        return False
    inner = " ".join(words[2:])
    # Auditor on #129: wrapped is never looser than bare. The inner command is judged by this guard's own HEAVY_RE; the one heavy shape the Mac
    # tier may carry is targeted `node --test <files>` (no glob), and the name blocklist stays as a second fence.
    if LANEJOB_INNER_DENY_RE.search(inner) or (HEAVY_RE.search(" " + inner) and not LANEJOB_NODE_TEST_RE.fullmatch(inner)):
        return False
    if any((n.startswith("LANEJOB_") and n not in LANEJOB_MAC_OFF) or n in LANEJOB_MAC_ENV for n in os.environ):
        return False
    return lanejob_dir_is_trunk(cwd)


VPS_REASON = (
    "Heavy runs (test suites, browsers, builds, Blender) go to the VPS, not the Mac (Dom, 2026-10-08): "
    "ssh -i ~/.ssh/binance_futures_tool root@49.12.7.18 'capture --prio 3 <lane> <cmd>' (vps-heavy-jobs skill). "
    "Safari/WebKit, real-iPhone and sparring rows are exempt; if the VPS is down, prefix the command with MAC_HEAVY_OK=1."
)


HATCH_RE = re.compile(r"^\s*(?:\w+=\S*\s+)*MAC_HEAVY_OK=1\b")
ENV_ASSIGN_RE = re.compile(r"(?<!\S)(\w+)=\S+")


def _hatch_log() -> Path:   # lazy: Path.home() at import time could crash the hook, and a crash lets the heavy command through
    return Path.home() / ".claude" / "logs" / "deploy_guard.log"


SPLICE_WORD_RE = re.compile(r"(?:[\w.:/@=-]|'[\w.:/@=-]*'|\"[\w.:/@=-]*\")+")
TOKEN_RE = re.compile(r"(?:[^\s'\"]|'[^']*'|\"[^\"]*\")+")


def _dequote_heads(command: str) -> str:
    """play''wright, 'playwright', "play"wright, pla\\ywright and 'npx' all run playwright. Undo quoting only on the first three simple words of each
    segment (the command, its runner and its first argument); quoted DATA (a commit message) has spaces or punctuation and is left alone."""
    command = re.sub(r"\\(?=\w)", "", command)
    parts = re.split(r"(&&|\|\||[;&|\n])", command)
    for i in range(0, len(parts), 2):
        seen = [0]
        def fix(m: re.Match) -> str:
            seen[0] += 1
            tok = m.group(0)
            return re.sub(r"['\"]", "", tok) if seen[0] <= 3 and SPLICE_WORD_RE.fullmatch(tok) else tok
        parts[i] = TOKEN_RE.sub(fix, parts[i])
    return "".join(parts)


def _runs(command: str) -> str:
    command = _dequote_heads(command or "")
    runs = executable_text(command) if command else ""
    return "\n".join(seg for seg in SEGMENT_SPLIT_RE.split(runs) if seg.strip() and not ALLOW_RE.search(seg) and not HF_JOBS_RE.search(seg) and not LIGHT_SCRIPT_RE.search(seg)) if runs else ""


def note_hatch_use(command: str, cwd: str) -> bool:
    """One audit line per MAC_HEAVY_OK=1 use (Optimizer's condition on #104: without it the prefix becomes the habit)."""
    if not any(HEAVY_RE.search(seg) and HATCH_RE.search(seg) for seg in _runs(command).split("\n")):
        return False
    shown = ENV_ASSIGN_RE.sub(lambda m: m.group(0) if m.group(1) == "MAC_HEAVY_OK" else f"{m.group(1)}=***", command[:300])
    log = _hatch_log()
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a") as f:
        f.write(f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} MAC_HEAVY_OK cwd={cwd} {shown!r}\n")
    return True


def _is_mac() -> bool:
    return sys.platform == "darwin"


def decide(command: str, lock: dict | None, own_pid: int | None = None, cwd: str = "") -> str:
    """Return the deny reason, or '' to allow.

    2026-10-10 (Dom, Lead: the VPS `lanes` user): this guard keeps heavy runs and builds off the shared MAC. On Linux (the VPS, where heavy
    runs belong) it is a no-op; it refused npm ci, playwright install and vite build there, even wrapped in `capture`."""
    if not _is_mac():
        return ""
    runs = _runs(command)
    pixelmator = bool(lock) and bool(runs) and APP_HOST_RE.search(runs) is not None and PIXELMATOR_RE.search(command) is not None
    if not runs or not (HEAVY_RE.search(runs) or pixelmator):
        return ""
    if not lock:
        heavy = [seg for seg in runs.split("\n") if HEAVY_RE.search(seg)]
        return "" if all(MAC_EXEMPT_RE.search(seg) or MAC_ROW_RE.match(seg) or hf_capture_wrapper(seg, cwd, command) or gpu_run_wrapper(seg, cwd, command) or lanejob_mac_wrapper(seg, cwd, command) for seg in heavy) else VPS_REASON
    if own_pid is not None and _is_descendant_of(own_pid, int(lock.get("pid", 0))):
        return ""
    mins = lock["age_s"] // 60
    return (
        f"Deploy {str(lock.get('revision', ''))[:7]} is in flight (started {lock.get('started', '?')[11:16]}Z, {mins} min ago) — "
        "one-deployer rule: browser checks, test suites, builds and bakes wait until the deploy dev posts FREE. "
        "Deploys take 5–10 min on a free Mac; retry then. Light commands (git, gh, grep, single-file tests) are not blocked."
    )


# CI work, hooks side (Dom's all-hands rule via Lead and the COO, 2026-10-12; skill ci-runners-first): a release candidate PR must carry ci:release-checks AT CUT, or
# release-checks.yml plans no rows and every deploy log reads "0 CI" (3-12 October: no candidate carried it). The cut is `gh pr create ... --head release-<X>` (by hand
# today, scripts/cut-release.mjs soon): refuse it unless the same command names the label. Not a Mac-only rule, and no heavy-job exemption was needed:
# Deploy's cut, pre-launch.sh, launch.mjs unit and deploy.sh commands were never refused (tests/test_cut_label.py probes them).
CUT_REASON = (
    "A release candidate PR must carry the ci:release-checks label at cut (Dom, 2026-10-12): without it release-checks runs no rows and the deploy log reads 0 CI. "
    "Add --label ci:release-checks to gh pr create, or cut with node scripts/cut-release.mjs."
)


def _cut_segments(text: str) -> list[list[str]]:
    """Split on ; & | newline and parens OUTSIDE quotes, then shlex each segment into argv tokens (quoted bodies stay one token)."""
    segs, cur, quote, i = [], [], "", 0
    while i < len(text):
        c = text[i]
        if quote:
            cur.append(c)
            if c == "\\" and quote == '"' and i + 1 < len(text):
                i += 1
                cur.append(text[i])
            elif c == quote:
                quote = ""
        elif c == "\\" and i + 1 < len(text):
            cur.append(c + text[i + 1])
            i += 1
        elif c in "\"'":
            quote = c
            cur.append(c)
        elif c in ";&|\n()":
            segs.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    segs.append("".join(cur))
    out = []
    for seg in segs:
        try:
            out.append(shlex.split(seg))
        except ValueError:
            out.append(seg.split())
    return out


def _opt_values(args: list[str], long: str, short: str) -> list[str]:
    vals = []
    for i, a in enumerate(args):
        if a == long or a == short:
            if i + 1 < len(args):
                vals.append(args[i + 1])
        elif a.startswith(long + "="):
            vals.append(a[len(long) + 1:])
        elif a.startswith(short) and not a.startswith("--") and len(a) > 2:
            vals.append(a[2:])
    return vals


CUT_SHELLS = {"bash", "sh", "zsh", "dash"}
_CUT_SUBST_RE = re.compile(r"`([^`]*)`|\$\(([^()]*)\)")


def _cut_gh_reason(args: list[str]) -> str:
    heads = [h.split(":", 1)[-1] for h in _opt_values(args, "--head", "-H")]
    labels = [x for v in _opt_values(args, "--label", "-l") for x in v.split(",")]
    return CUT_REASON if any(h.startswith("release-") for h in heads) and "ci:release-checks" not in labels else ""


def cut_label_reason(command: str) -> str:
    """Refuse `gh pr create --head release-*` without ci:release-checks. Auditor on #136 (2026-10-12): the wrapper list kept growing (sudo, env, timeout,
    xargs, caffeinate, eval, braces, if/then, `!`), so the rule is: in every segment find ANY token whose basename is gh, skip gh's global -R/--repo,
    and require `pr create`; look inside `bash -c` / eval scripts and backtick / $(...) bodies the same way."""
    text = HEREDOC_RE.sub("", command or "")
    for m in _CUT_SUBST_RE.finditer(text):
        inner = cut_label_reason(m.group(1) or m.group(2) or "")
        if inner:
            return inner
    for argv in _cut_segments(text):
        for k, tok in enumerate(argv):
            word = os.path.basename(tok)
            if word in CUT_SHELLS:
                for j in range(k + 1, len(argv) - 1):
                    if re.fullmatch(r"-[a-zA-Z]*c", argv[j]):
                        script = argv[j + 1 + (argv[j + 1] == "--")] if j + 1 + (argv[j + 1] == "--") < len(argv) else ""
                        inner = cut_label_reason(script)
                        if inner:
                            return inner
                        break
            elif word == "eval":
                inner = cut_label_reason(" ".join(argv[k + 1:]))
                if inner:
                    return inner
            elif word == "gh":
                m = k + 1
                while m < len(argv) and argv[m].startswith("-"):
                    m += 2 if argv[m] in ("-R", "--repo") else 1
                if argv[m:m + 2] == ["pr", "create"]:
                    reason = _cut_gh_reason(argv[m + 2:])
                    if reason:
                        return reason
    return ""


# 2026-10-14 (Strategy + Dom): the app's "This shell -c script runs rm and could not be checked" prompt fires in every permission mode,
# bypass included, so a lane's harmless `bash -c '...'` still stops Dom at the keyboard. Refuse the wrapper itself (a quoted script after
# bash/sh/zsh -c); ssh and hf jobs run the script elsewhere and the app does not prompt for them.
SHELL_C_RE = re.compile(rf"{SHELL}\s+(?:-\w+\s+)*-\w*c\w*\s+\"\"")
SHELL_C_REASON = "write the command directly, no shell -c wrapper; the app cannot check inside quotes and will prompt Dom"
REMOTE_SEGMENT_RE = re.compile(r"^\s*(?:\w+=\S*\s+)*ssh\b")


def shell_c_reason(command: str) -> str:
    command = command or ""
    flat = QUOTED_RE.sub('""', HEREDOC_RE.sub(lambda m: "" if not SHELL_STDIN_RE.search(command[:m.start()].rstrip()) else m.group(0), command))   # executable_text keeps a -c payload; this wants the bare wrapper
    for seg in SEGMENT_SPLIT_RE.split(flat):
        if SHELL_C_RE.search(seg) and not HF_JOBS_RE.search(seg) and not REMOTE_SEGMENT_RE.search(seg):
            return SHELL_C_REASON
    return ""


# 2026-10-14 (Strategy + Dom): the app's own "Dangerous rm operation on possibly-empty variable path" card runs in every permission mode,
# bypass included, and parks a lane until Dom is back. Deny the shape first (a recursive rm whose path holds a bare $VAR or ${VAR}) so the
# model rewrites it as "${VAR:?}/..." or a literal path in one turn. ${VAR:?} and ${VAR:-x} do not match BARE_VAR_RE; single quotes expand nothing.
BARE_VAR_RE = re.compile(r"\$(?:\{[A-Za-z_]\w*\}|[A-Za-z_]\w*)")
RM_RE = re.compile(r"^\s*(?:(?:\w+=\S*|sudo|env|command|time|nohup|\\)\s+)*rm\s+(.*)$", re.DOTALL)
RM_VAR_REASON = 'rm -r on an unguarded $VAR can delete the wrong tree if it is empty: rewrite as "${VAR:?}/..." or a literal path, then retry'


def rm_var_reason(command: str) -> str:
    command = command or ""
    text = HEREDOC_RE.sub(lambda m: m.group(0) if SHELL_STDIN_RE.search(command[:m.start()].rstrip()) else "", command)
    text = QUOTED_RE.sub(lambda m: "''" if m.group(0)[0] == "'" else re.sub(r"[;&|()`\n]", " ", m.group(0)), text)   # separators inside quotes are data
    for seg in SEGMENT_SPLIT_RE.split(text):
        m = RM_RE.match(seg)
        if not m:
            continue
        args = m.group(1).split()
        flags = [a for a in args if a.startswith("-") and a != "--"]
        paths = [a for a in args if not a.startswith("-")]
        recursive = any(f in ("--recursive",) or (not f.startswith("--") and set(f[1:]) & set("rR")) for f in flags)
        if recursive and any(BARE_VAR_RE.search(a) for a in paths):
            return RM_VAR_REASON
    return ""


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return
    if payload.get("tool_name") != "Bash":
        return
    command = str((payload.get("tool_input") or {}).get("command") or "")
    lock = active_lock()
    reason = (_is_mac() and (shell_c_reason(command) or rm_var_reason(command) or bash_write_reason(command))) or cut_label_reason(command) or decide(command, lock, own_pid=os.getpid(), cwd=str(payload.get("cwd") or ""))
    if not reason:
        if not lock:
            try:
                note_hatch_use(command, str(payload.get("cwd") or ""))
            except (OSError, RuntimeError):   # an audit line must never cost a lane its command
                pass
        return
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason},
    }))


if __name__ == "__main__":
    main()
