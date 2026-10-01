from __future__ import annotations

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


def pending_quality(session_id: str, cwd: Path | None = None, clear: bool = False) -> list[str]:
    if cwd is None and not clear:
        return load_json_locked(PENDING_PATH, {}).get(session_id, [])
    def update(data):
        if clear:
            data.pop(session_id, None)
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


def run_project_gate(cwd: Path, timeout: float = 420) -> dict:
    """Run the repository-owned contract; missing or invalid checks fail closed."""
    path = cwd / ".quality-gate.json"
    results = []
    try:
        contract = json.loads(path.read_text())
        limit = contract.get("timeout_seconds", 420)
        if type(limit) is not int or not 1 <= limit <= 420:
            raise ValueError("timeout_seconds must be an integer from 1 to 420")
        timeout = min(timeout, limit)
        commands = contract.get("codex_commands", contract.get("commands", []))
        if not isinstance(commands, list):
            raise ValueError("commands must be a list of argv lists")
        completion = contract.get("completion_commands", [])
        if not isinstance(completion, list):
            raise ValueError("completion_commands must be a list of argv lists")
        commands = commands + completion
        if not commands:
            raise ValueError("quality contract must contain at least one command")
        started = time.monotonic()
        for command in commands:
            if not isinstance(command, list) or not command or not all(isinstance(arg, str) and arg for arg in command):
                raise ValueError("each command must be a nonempty argv list")
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("repository quality deadline exceeded")
            # Kill the whole check process group on timeout, including npm/make children.
            with subprocess.Popen(command, cwd=cwd, text=True, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, start_new_session=True) as proc:
                try:
                    out, err = proc.communicate(timeout=remaining)
                except subprocess.TimeoutExpired:
                    import signal
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.communicate()
                    raise TimeoutError("repository quality deadline exceeded") from None
                rc = proc.returncode
            results.append(_result(path, " ".join(command), rc == 0, out + err))
            if rc != 0:
                break
    except Exception as exc:
        results.append(_result(path, "quality contract", False, str(exc)))
    return {"ok": bool(results) and all(item["ok"] for item in results), "results": results}


def _result(file: Path, tool: str, ok: bool, output: str = "") -> dict:
    return {"file": str(file), "tool": tool, "ok": ok, "output": output[-4000:]}


def run_quality_checks(files: list[Path], cwd: Path, timeout: int = 60, *, autofix: bool = False) -> dict:
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

    if autofix and py_files and _tool_exists(project_tool("ruff", cwd)):
        # Only split combined imports automatically; never inherit broader fix rules.
        for args in (["check", "--isolated", "--select", "E401", "--fix", "--no-unsafe-fixes"],
                     ["format"]):
            rc, out, err = run([project_tool("ruff", cwd), *args, *map(str, py_files)])
            results.append(_result(py_files[0], "ruff " + args[0], rc == 0, out + err))
    for file in py_files:
        rc, out, err = run(["python3", "-m", "py_compile", str(file)])
        results.append(_result(file, "py_compile", rc == 0, out + err))
    if py_files and _tool_exists(project_tool("ruff", cwd)):
        rc, out, err = run([project_tool("ruff", cwd), "check", "--no-fix", "--no-fix-only", *map(str, py_files)])
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
