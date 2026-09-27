from __future__ import annotations

import hashlib
import json
import configparser
import os
import re
import shutil
import subprocess
import time
import tomllib
from pathlib import Path
from typing import Any

from .git import safe_run
from .json_state import load_json_locked, update_json_locked

PENDING_PATH = Path.home() / ".codex/state/pending_quality.json"


def pending_quality(session_id: str, cwd: Path | None = None, clear: bool = False, done: list[str] | None = None) -> list[str]:
    """Repos this session edited that still owe a quality gate. `done` removes only the repos whose gate
    passed (a deferred one stays owed); `clear` removes all."""
    if cwd is None and not clear and done is None:
        return load_json_locked(PENDING_PATH, {}).get(session_id, [])
    def update(data):
        if clear:
            data.pop(session_id, None)
        elif done is not None:
            data[session_id] = sorted(set(data.get(session_id, [])) - set(done))
            if not data[session_id]:
                data.pop(session_id)
        else:
            data[session_id] = sorted(set(data.get(session_id, [])) | {str(cwd.resolve())})
        return data
    return update_json_locked(PENDING_PATH, update).get(session_id, [])


SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    "build",
    ".next",
    "archive",
    "archived",
    "legacy",
    "agent_archived",
    "agent_legacy",
}


def _paths_from_patch(text: str, cwd: Path) -> list[Path]:
    paths: list[Path] = []
    for line in text.splitlines():
        for prefix in ("*** Update File: ", "*** Add File: ", "*** Delete File: "):
            if line.startswith(prefix):
                paths.append((cwd / line[len(prefix):].strip()).resolve())
    return paths


def changed_files_in_payload(payload: dict[str, Any]) -> list[Path]:
    tool_input = payload.get("tool_input") or {}
    cwd = Path(payload.get("cwd") or os.getcwd())
    if isinstance(tool_input, dict) and tool_input.get("workdir"):
        cwd = Path(str(tool_input["workdir"]))
    files: list[Path] = []
    if isinstance(tool_input, dict):
        for key in ("path", "file_path", "filename"):
            if tool_input.get(key):
                files.append((cwd / str(tool_input[key])).resolve())
        command = str(tool_input.get("command") or tool_input.get("cmd") or "")
        files.extend(_paths_from_patch(command, cwd))
    elif isinstance(tool_input, str):
        files.extend(_paths_from_patch(tool_input, cwd))
    seen = set()
    out = []
    for path in files:
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out


def _has_file_upwards(cwd: Path, names: set[str]) -> bool:
    cur = cwd.resolve()
    for path in (cur, *cur.parents):
        if any((path / name).exists() for name in names):
            return True
    return False


def _tool_exists(name: str) -> bool:
    return shutil.which(name) is not None


def has_mypy_config(cwd: Path) -> bool:
    for root in (cwd.resolve(), *cwd.resolve().parents):
        if (root / "mypy.ini").exists() or (root / ".mypy.ini").exists():
            return True
        project = root / "pyproject.toml"
        if project.exists() and "mypy" in tomllib.loads(project.read_text()).get("tool", {}):
            return True
        config = configparser.ConfigParser()
        config.read(root / "setup.cfg")
        if config.has_section("mypy"):
            return True
    return False


def project_tool(name: str, cwd: Path) -> str:
    for folder in (".venv/bin", ".venv-quality/bin", "node_modules/.bin"):
        path = cwd / folder / name
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return name


# A pass is a receipt for the exact inputs it checked. Re-running the same commands on an identical
# tree at every Stop wastes the budget and, for a slow CI, makes the gate unwinnable.
GATE_CACHE_PATH = Path.home() / ".claude" / "state" / "quality_gate_cache.json"
GATE_CACHE_TTL_S = 6 * 3600
GATE_CACHE_MAX = 200
DEFER_POLL_S = 5   # how often a running gate re-reads the deploy lock
# Lead, 2026-09-26 18:11: gates stacked to load 386 while Deploy waited to launch. A gate does not start when
# the Mac is overloaded, when Deploy holds the Mac before its lock exists, or when the same check already runs
# in this folder (a lane's own run, or one a Stop deadline left behind).
GATE_MAX_LOAD = float(os.environ.get("CLAUDE_GATE_MAX_LOAD") or 60)
DEPLOY_HOLD_PATH = Path.home() / ".claude" / "state" / "deploy_hold"   # Deploy touches it while it waits to launch
DEPLOY_HOLD_TTL_S = 30 * 60


def deploy_hold() -> dict | None:
    try:
        age = time.time() - DEPLOY_HOLD_PATH.stat().st_mtime
    except OSError:
        return None
    return {"reason": f"Deploy is holding the Mac ({DEPLOY_HOLD_PATH.name}, {age:.0f}s old)"} if age < DEPLOY_HOLD_TTL_S else None


def _same_check_running(cwd: Path, command: list[str]) -> str:
    """pid of a live process running `command` with cwd as its folder, or ""."""
    argv = [a for a in command if not (command[0] == "env" and ("=" in a or a == "env"))]
    def esc(text: str) -> str:  # pgrep takes a regex
        return re.sub(r"([][.*+?(){}|^$\\])", r"\\\1", text)

    # macOS may show argv[0] as a full path (python3 -> .../Python), so match the arguments, anchored at the end.
    pattern = (" " + esc(" ".join(argv[1:])) if argv[1:] else "(^|/)" + esc(argv[0])) + "$"
    rc, out, _ = safe_run(["pgrep", "-f", pattern], cwd=cwd, timeout=5)
    for pid in out.split() if rc == 0 else []:
        if pid == str(os.getpid()):
            continue
        _, where, _ = safe_run(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"], cwd=cwd, timeout=5)
        if f"n{cwd.resolve()}" in where.splitlines():
            return pid
    return ""


# Lead, 2026-09-27 evening: 8-12 `quality-stop-targeted.mjs` runs at once (every lane's Stop fires the same gate),
# 9 min each at load 40-77 against a 420 s budget, while Dom's Blender batch had the Mac. One gate per Mac: the
# run holds this lock; every other Stop defers with its existing notice. A dead holder or a forgotten lock expires.
GATE_LOCK_PATH = Path.home() / ".claude" / "state" / "quality_gate.lock"
# Lead: gate processes outlived the 420 s Stop budget (an 8-minute node tree in frankendom-stats), so the lock breaks
# only when the holder is dead or the lock is far older than any budget. The holder records its check's process
# group; a later acquire kills that group when the holder died without releasing (Claude Code killed the hook).
GATE_LOCK_TTL_S = 1800


def _read_lock() -> dict | None:
    try:
        return json.loads(GATE_LOCK_PATH.read_text())
    except Exception:
        return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def gate_lock_holder() -> dict | None:
    """{pid, cwd, at[, pgid]} of the live gate run holding the Mac-wide lock; None when free, expired, or dead."""
    data = _read_lock()
    try:
        alive = data is not None and time.time() - float(data["at"]) <= GATE_LOCK_TTL_S and _alive(int(data["pid"]))
    except (KeyError, TypeError, ValueError):
        return None
    return data if alive else None


def _note_gate_child(pgid: int) -> None:
    """Record the running check's process group in our lock; never fails the gate (best effort)."""
    try:
        data = _read_lock()
        if data and int(data.get("pid", 0)) == os.getpid():
            GATE_LOCK_PATH.write_text(json.dumps({**data, "pgid": int(pgid)}))
    except Exception:
        pass


def acquire_gate_lock(cwd: Path) -> bool:
    """Take the Mac-wide gate lock for this process; False when another live run holds it. A dead holder's check
    process group (an orphan left by a killed hook) is killed first, so no second gate runs on top of it."""
    for _ in range(2):
        holder = gate_lock_holder()
        if holder and int(holder.get("pid", 0)) != os.getpid():
            return False
        GATE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        if holder is None:
            stale = _read_lock() or {}
            pgid = int(stale.get("pgid") or 0)
            if pgid and pgid != os.getpid() and _alive(pgid):
                import signal
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except OSError:
                    pass
            GATE_LOCK_PATH.unlink(missing_ok=True)   # stale or dead
        try:
            fd = os.open(GATE_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue   # a race: re-read the holder once
        with os.fdopen(fd, "w") as fh:
            json.dump({"pid": os.getpid(), "cwd": str(cwd), "at": time.time()}, fh)
        return True
    return False


def release_gate_lock() -> None:
    holder = gate_lock_holder()
    if holder and int(holder.get("pid", 0)) == os.getpid():
        GATE_LOCK_PATH.unlink(missing_ok=True)


def unchanged_from_base(cwd: Path) -> str:
    """Name of the base branch when the checkout carries no task-owned change: clean tree AND no diff between the
    merge-base with the base and HEAD (`git diff --quiet base...HEAD`). Base = `base` in .quality-gate.json, else
    origin/HEAD. "" (so the gate runs) for a dirty tree, any commit on top of the base, or no resolvable base.
    Lead, 2026-09-27: Deploy's checkout on unchanged trunk ran the 9-minute gate at every Stop for nothing."""
    rc, status, _ = safe_run(["git", "status", "--porcelain"], cwd=cwd)
    if rc or status.strip():
        return ""
    try:
        base = json.loads((cwd / ".quality-gate.json").read_text()).get("base")
    except Exception:
        base = None
    if not isinstance(base, str) or not base:
        rc, base, _ = safe_run(["git", "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"], cwd=cwd)
        base = base.strip() if rc == 0 else ""
    if not base:
        return ""
    rc, _, _ = safe_run(["git", "diff", "--quiet", f"{base}...HEAD"], cwd=cwd)
    return base if rc == 0 else ""


def gate_busy(cwd: Path) -> dict | None:
    """Why the repository gate should wait instead of starting now, or None."""
    load = os.getloadavg()[0]
    if load > GATE_MAX_LOAD:
        return {"reason": f"the Mac is overloaded (load {load:.0f}, limit {GATE_MAX_LOAD:.0f})"}
    hold = deploy_hold()
    if hold:
        return hold
    holder = gate_lock_holder()
    if holder and int(holder.get("pid", 0)) != os.getpid():
        return {"reason": f"a repository gate is already running in {holder.get('cwd')} (pid {holder.get('pid')}, one gate per Mac)"}
    try:
        contract = json.loads((cwd / ".quality-gate.json").read_text())
        commands = list(contract.get("commands") or []) + list(contract.get("completion_commands") or [])
    except Exception:
        return None  # an unreadable contract fails closed in run_project_gate
    for command in commands:
        if isinstance(command, list) and command and all(isinstance(a, str) and a for a in command):
            pid = _same_check_running(cwd, command)
            if pid:
                return {"reason": f"`{' '.join(command)}` is already running in this folder (pid {pid})"}
    return None


def _tree_key(cwd: Path, commands: list) -> str | None:
    """Identity of what a gate run checks: HEAD, staged/unstaged diffs, untracked files, commands."""
    rc, head, _ = safe_run(["git", "rev-parse", "HEAD"], cwd=cwd)
    if rc != 0:
        return None
    parts = [str(cwd), head.strip(), json.dumps(commands)]
    for args in (["git", "status", "--porcelain", "--untracked-files=all"], ["git", "diff"], ["git", "diff", "--cached"]):
        rc, out, _ = safe_run(args, cwd=cwd)
        if rc != 0:
            return None
        parts.append(out)
    for line in parts[3].splitlines():  # untracked content is invisible to diff: pin size + mtime
        if line.startswith("?? "):
            try:
                st = (cwd / line[3:]).stat()
                parts.append(f"{line[3:]} {st.st_size} {st.st_mtime_ns}")
            except OSError:
                parts.append(line)
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def _cached_pass(key: str | None) -> str:
    if not key:
        return ""
    entry = load_json_locked(GATE_CACHE_PATH, {}).get(key)
    if isinstance(entry, dict) and time.time() - float(entry.get("at", 0)) < GATE_CACHE_TTL_S:
        return str(entry.get("at_iso", ""))
    return ""


def _remember_pass(key: str | None, cwd: Path) -> None:
    if not key:
        return

    def updater(data: dict) -> dict:
        data[key] = {"at": time.time(), "at_iso": time.strftime("%Y-%m-%dT%H:%M:%S"), "repo": str(cwd)}
        if len(data) > GATE_CACHE_MAX:
            data = dict(sorted(data.items(), key=lambda kv: kv[1].get("at", 0))[-GATE_CACHE_MAX:])
        return data

    update_json_locked(GATE_CACHE_PATH, updater)


def _deadline(command: list[str], elapsed: float, budget: float) -> TimeoutError:
    return TimeoutError(
        f"repository quality deadline exceeded: `{' '.join(command)}` was still running after {elapsed:.0f}s of the "
        f"{budget:.0f}s Stop budget. This is the hook's time limit, not a failing check. Keep `commands` and "
        "`completion_commands` fast (unit tests, lint); run long CI in the release workflow instead, or set "
        "`timeout_seconds` (max 420). A pass on an unchanged tree is remembered for 6h."
    )


_CI_OK = {"success", "skipped", "neutral"}


def _check_runs(cwd: Path, sha: str, timeout: int = 8) -> list[tuple[str, str, str]] | None:
    """(name, status, conclusion) of every check run on commit `sha`, whichever app made it; None when gh fails."""
    rc, out, _ = safe_run(["gh", "api", "--paginate", f"repos/{{owner}}/{{repo}}/commits/{sha}/check-runs?per_page=100",
                           "--jq", '.check_runs[] | [.name, .status, (.conclusion // "")]'], cwd=cwd, timeout=timeout)
    try:
        return [(str(n), str(st), str(c)) for n, st, c in map(json.loads, out.splitlines())] if rc == 0 else None
    except (ValueError, TypeError):
        return None


def _named_ci_checks(cwd: Path) -> list[str]:
    """The `ci_checks` list from .quality-gate.json: the CI checks that stand in for the local gate. [] unless it
    is a nonempty list of nonempty strings."""
    try:
        names = json.loads((cwd / ".quality-gate.json").read_text()).get("ci_checks")
    except Exception:
        return []
    return names if isinstance(names, list) and names and all(isinstance(n, str) and n for n in names) else []


def ci_covered_commit(cwd: Path) -> tuple[str, str]:
    """(short SHA, "passed" | "pending") when the working tree is clean (no modified or untracked files) and every
    CI check the contract names in `ci_checks` ran on this exact commit: "passed" when each completed as
    success/skipped/neutral, "pending" while any is still queued or running and none failed. ("", "") in every
    other case (no ci_checks named, no workflow, a named check missing/failed/cancelled, any git/gh error), so
    the gate runs. Unnamed checks (docs, other apps) do not count either way."""
    expected = _named_ci_checks(cwd)
    if not expected or not (cwd / ".github" / "workflows").is_dir():
        return "", ""
    rc, status, _ = safe_run(["git", "status", "--porcelain"], cwd=cwd)
    if rc or status.strip():
        return "", ""
    rc, head, _ = safe_run(["git", "rev-parse", "HEAD"], cwd=cwd)
    head = head.strip()
    if rc or not head:
        return "", ""
    runs = _check_runs(cwd, head)
    if runs is None:
        return "", ""
    pending = False
    for name in expected:
        mine = [(st, c) for n, st, c in runs if n == name]
        if not mine or any(st == "completed" and c not in _CI_OK for st, c in mine):
            return "", ""
        pending = pending or any(st != "completed" for st, _ in mine)
    return head[:7], "pending" if pending else "passed"


def run_project_gate(cwd: Path, timeout: float = 420, defer_to: Any = None) -> dict:
    """Run the repository-owned contract; missing or invalid checks fail closed.

    defer_to() returning a lock (a deploy started mid-gate) kills the running check's process group and returns
    {"ok": True, "deferred": lock}: one deployer per Mac, the same as a gate that finds the lock at its start."""
    path = cwd / ".quality-gate.json"
    results = []
    try:
        contract = json.loads(path.read_text())
        limit = contract.get("timeout_seconds", 420)
        if type(limit) is not int or not 1 <= limit <= 420:
            raise ValueError("timeout_seconds must be an integer from 1 to 420")
        timeout = min(timeout, limit)
        commands = contract["commands"]
        if not isinstance(commands, list) or not commands:
            raise ValueError("commands must be a nonempty list of argv lists")
        completion = contract.get("completion_commands", [])
        if not isinstance(completion, list):
            raise ValueError("completion_commands must be a list of argv lists")
        commands = commands + completion
        for command in commands:
            if not isinstance(command, list) or not command or not all(isinstance(arg, str) and arg for arg in command):
                raise ValueError("each command must be a nonempty argv list")
        key = _tree_key(cwd, commands)
        cached = _cached_pass(key)
        if cached:
            return {"ok": True, "results": [_result(path, "quality contract", True, f"cached pass at {cached} on an identical tree")]}
        if not acquire_gate_lock(cwd):
            return {"ok": True, "deferred": gate_busy(cwd) or {"reason": "another repository gate took the Mac-wide lock first"},
                    "results": results}
        started = time.monotonic()
        for command in commands:
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise _deadline(command, time.monotonic() - started, timeout)
            lock = defer_to() if defer_to else None
            if lock:
                return {"ok": True, "deferred": lock, "results": results}
            # Kill the whole check process group on timeout or a deploy, including npm/node --test children.
            with subprocess.Popen(command, cwd=cwd, text=True, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, start_new_session=True) as proc:
                _note_gate_child(proc.pid)   # its own session: pgid == pid
                while True:
                    remaining = timeout - (time.monotonic() - started)
                    try:
                        wait = min(remaining, DEFER_POLL_S) if defer_to else remaining
                        out, err = proc.communicate(timeout=max(0.0, wait))
                        break
                    except subprocess.TimeoutExpired:
                        lock = defer_to() if defer_to and remaining > 0 else None
                        if remaining > 0 and not lock:
                            continue
                        import signal
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.communicate()
                        if lock:
                            return {"ok": True, "deferred": lock, "results": results}
                        raise _deadline(command, time.monotonic() - started, timeout) from None
                rc = proc.returncode
            results.append(_result(path, " ".join(command), rc == 0, out + err))
            if rc != 0:
                break
        if results and all(item["ok"] for item in results):
            _remember_pass(key, cwd)
    except Exception as exc:
        results.append(_result(path, "quality contract", False, str(exc)))
    finally:
        release_gate_lock()
    return {"ok": bool(results) and all(item["ok"] for item in results), "results": results}


def _result(file: Path, tool: str, ok: bool, output: str = "") -> dict:
    return {"file": str(file), "tool": tool, "ok": ok, "output": output[-4000:]}


def run_quality_checks(files: list[Path], cwd: Path, timeout: int = 60) -> dict:
    results: list[dict] = []
    deadline = time.monotonic() + timeout

    def run(command, **kwargs):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 1, "", "Quality floor deadline exceeded"
        return safe_run(command, cwd=cwd, timeout=remaining)

    files = [p for p in files if p.exists() and p.is_file()]
    py_files = [p for p in files if p.suffix == ".py"]
    ts_files = [p for p in files if p.suffix in {".ts", ".tsx"}]
    go_files = [p for p in files if p.suffix == ".go"]
    rs_files = [p for p in files if p.suffix == ".rs"]

    required = (["ruff"] if py_files else []) + (["tsc", "eslint"] if ts_files else [])
    if py_files and has_mypy_config(cwd):
        required.append("mypy")
    if any(p.name.startswith("test_") for p in py_files):
        required.append("pytest")
    for name in required:
        if not _tool_exists(project_tool(name, cwd)):
            results.append(_result(files[0], name, False, f"Required tool unavailable: {name}; install project development dependencies."))

    for file in py_files:
        rc, out, err = run(["python3", "-m", "py_compile", str(file)])
        results.append(_result(file, "py_compile", rc == 0, out + err))
    if py_files and _tool_exists(project_tool("ruff", cwd)):
        rc, out, err = run([project_tool("ruff", cwd), "check", *map(str, py_files)])
        results.append(_result(py_files[0], "ruff", rc == 0, out + err))
    if py_files and _tool_exists(project_tool("mypy", cwd)) and has_mypy_config(cwd):
        rc, out, err = run([project_tool("mypy", cwd), "--no-error-summary", *map(str, py_files)])
        results.append(_result(py_files[0], "mypy", rc == 0, out + err))
    for file in py_files:
        if file.name.startswith("test_") and _tool_exists(project_tool("pytest", cwd)):
            rc, out, err = run([project_tool("pytest", cwd), "-q", str(file)])
            results.append(_result(file, "pytest", rc == 0, out + err))

    if ts_files and _tool_exists(project_tool("tsc", cwd)) and _has_file_upwards(cwd, {"tsconfig.json"}):
        rc, out, err = run([project_tool("tsc", cwd), "--noEmit"])
        results.append(_result(ts_files[0], "tsc", rc == 0, out + err))
    if ts_files and _tool_exists(project_tool("eslint", cwd)):
        rc, out, err = run([project_tool("eslint", cwd), *map(str, ts_files)])
        results.append(_result(ts_files[0], "eslint", rc == 0, out + err))

    for file in go_files:
        rc, out, err = run(["go", "test", f"./{file.parent.relative_to(cwd)}"])
        results.append(_result(file, "go test", rc == 0, out + err))
    if rs_files and _has_file_upwards(cwd, {"Cargo.toml"}):
        rc, out, err = run(["cargo", "check"])
        results.append(_result(rs_files[0], "cargo check", rc == 0, out + err))

    ok = all(item["ok"] for item in results)
    return {"ok": ok, "results": results}


def mine_backlog(cwd: Path, max_items: int = 10) -> list[dict]:
    items: list[dict] = []
    source_files: list[Path] = []
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        if len(source_files) > 500:
            break
        for name in files:
            path = Path(root) / name
            if path.suffix in {".py", ".ts", ".tsx", ".js", ".jsx", ".md"}:
                source_files.append(path)

    test_text = ""
    for path in source_files:
        if "test" in path.name.lower() or "spec" in path.name.lower():
            try:
                test_text += "\n" + path.read_text(errors="replace")[:20_000]
            except Exception:
                pass

    for path in source_files:
        try:
            text = path.read_text(errors="replace")
        except Exception:
            continue
        rel = str(path.relative_to(cwd)) if path.is_relative_to(cwd) else str(path)
        for i, line in enumerate(text.splitlines(), start=1):
            if re.search(r"\b(TODO|FIXME|XXX|HACK)\b", line):
                items.append({"description": f"Resolve {line.strip()[:120]}", "source_file": f"{rel}:{i}", "priority": 50})
        is_test_file = "test" in path.name.lower() or "spec" in path.name.lower()
        if path.suffix == ".py" and not is_test_file:
            for match in re.finditer(r"^def\s+([A-Za-z_][A-Za-z0-9_]*)\(", text, flags=re.M):
                name = match.group(1)
                if not name.startswith("_") and name not in test_text:
                    items.append({"description": f"Add coverage for Python function `{name}`", "source_file": rel, "priority": 70})
        if path.suffix in {".ts", ".tsx", ".js", ".jsx"}:
            for match in re.finditer(r"\bfunction\s+([A-Za-z_][A-Za-z0-9_]*)\(", text):
                name = match.group(1)
                if name not in test_text:
                    items.append({"description": f"Add coverage for function `{name}`", "source_file": rel, "priority": 70})
        if path.suffix == ".md" and re.search(r"(?im)^##\s+.*\n\s*(TODO|TBD|)$", text):
            items.append({"description": "Fill incomplete markdown section", "source_file": rel, "priority": 30})

    items.sort(key=lambda x: x["priority"], reverse=True)
    return items[:max_items]
