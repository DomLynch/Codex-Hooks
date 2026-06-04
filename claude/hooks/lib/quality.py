from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any

from .git import safe_run


BACKLOG_MARKER_RE = re.compile(r"\b(TODO|FIXME|XXX|HACK)(?:\([^)]+\))?:")

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
        if path not in seen and path.exists() and path.is_file():
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


def _result(file: Path, tool: str, ok: bool, output: str = "") -> dict:
    return {"file": str(file), "tool": tool, "ok": ok, "output": output[-4000:]}


def run_quality_checks(files: list[Path], cwd: Path, timeout: int = 60) -> dict:
    results: list[dict] = []
    files = [p for p in files if p.exists() and p.is_file()]
    py_files = [p for p in files if p.suffix == ".py"]
    ts_files = [p for p in files if p.suffix in {".ts", ".tsx"}]
    go_files = [p for p in files if p.suffix == ".go"]
    rs_files = [p for p in files if p.suffix == ".rs"]

    for file in py_files:
        rc, out, err = safe_run(["python3", "-m", "py_compile", str(file)], cwd=cwd, timeout=timeout)
        results.append(_result(file, "py_compile", rc == 0, out + err))
    if py_files and _tool_exists("ruff"):
        rc, out, err = safe_run(["ruff", "check", *map(str, py_files)], cwd=cwd, timeout=timeout)
        results.append(_result(py_files[0], "ruff", rc == 0, out + err))
    if py_files and _tool_exists("mypy") and _has_file_upwards(cwd, {"mypy.ini", "pyproject.toml", "setup.cfg"}):
        rc, out, err = safe_run(["mypy", "--no-error-summary", *map(str, py_files)], cwd=cwd, timeout=timeout)
        results.append(_result(py_files[0], "mypy", rc == 0, out + err))
    for file in py_files:
        if file.name.startswith("test_") and _tool_exists("pytest"):
            rc, out, err = safe_run(["pytest", "-q", str(file)], cwd=cwd, timeout=timeout)
            results.append(_result(file, "pytest", rc == 0, out + err))

    if ts_files and _tool_exists("tsc") and _has_file_upwards(cwd, {"tsconfig.json"}):
        rc, out, err = safe_run(["tsc", "--noEmit"], cwd=cwd, timeout=timeout)
        results.append(_result(ts_files[0], "tsc", rc == 0, out + err))
    if ts_files and _tool_exists("eslint"):
        rc, out, err = safe_run(["eslint", *map(str, ts_files)], cwd=cwd, timeout=timeout)
        results.append(_result(ts_files[0], "eslint", rc == 0, out + err))

    for file in go_files:
        rc, out, err = safe_run(["go", "test", f"./{file.parent.relative_to(cwd)}"], cwd=cwd, timeout=timeout)
        results.append(_result(file, "go test", rc == 0, out + err))
    if rs_files and _has_file_upwards(cwd, {"Cargo.toml"}):
        rc, out, err = safe_run(["cargo", "check"], cwd=cwd, timeout=timeout)
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
            if BACKLOG_MARKER_RE.search(line):
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
