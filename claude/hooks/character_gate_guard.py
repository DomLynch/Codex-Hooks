#!/usr/bin/env python3
"""PreToolUse: the character-from-pictures hard gates (Dom, 2026-10-30, "set this in stone", via Strategy).

Demon 2 circled seven TRELLIS seeds on contact-sheet crops. Two gates, every lane:
- Gate 2 (BLOCK): an image-to-3D run (trellis, hunyuan3d, run_trellis) whose input images are under 1024 px on the long
  side, or whose inputs are a single file at least 1.4x wider than tall (a contact sheet), is refused before it starts.
- Gate 3 (WARN, never block): writing a custom *decimat*.py / *planar*.py / *project*uv*.py outside ~/.claude/skills.
The size check reads PNG/JPEG headers itself (no Pillow, no sips) and fails OPEN on anything it cannot read: a gate that
crashes or guesses wrong would block honest work. Only a command that RUNS something (python, uv, bash, ./script ...)
is a run; ls, cat, grep, git, pip, open and sips mentioning "trellis" are not.
"""
from __future__ import annotations

import fnmatch
import json
import re
import shlex
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.log import log  # noqa: E402

COMPONENT = "character_gate_guard"
MIN_LONG_SIDE = 1024
SHEET_RATIO = 1.4
GENERATOR_RE = re.compile(r"trellis|hunyuan[-_ ]?3d", re.I)
ENV_ASSIGN_RE = re.compile(r"^\w+=\S*$")
RUNNERS = {"uv", "bash", "sh", "zsh", "nohup", "time", "env", "hfjob", "xargs"}
PYTHON_RE = re.compile(r"^(?:\S*/)?python[\d.]*$")
IMAGE_EXT = (".png", ".jpg", ".jpeg")
INPUT_FLAG_RE = re.compile(r"^--?(?:i|in|input|input[-_]?dirs?|images?|image[-_]?dirs?|img[-_]?dirs?|src|source|views?)$", re.I)
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SCRIPT_GLOBS = ("*decimat*.py", "*planar*.py", "*project*uv*.py")
SKILLS_DIR = Path.home() / ".claude" / "skills"
GATE_3 = "Hard gate 3: use the generator's own simplify and bake, no custom decimate scripts."


def image_size(path: Path) -> tuple[int, int] | None:
    """(width, height) from a PNG or JPEG header; None when the file is missing, not an image or cut short."""
    try:
        with open(path, "rb") as f:
            head = f.read(26)
            if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
                return struct.unpack(">II", head[16:24])
            if head[:2] != b"\xff\xd8":
                return None
            f.seek(2)
            while True:
                b = f.read(1)
                if not b:
                    return None
                if b != b"\xff":
                    continue
                m = f.read(1)
                while m == b"\xff":
                    m = f.read(1)
                if not m or m in (b"\xd8", b"\x01") or b"\xd0" <= m <= b"\xd7":
                    continue
                if m == b"\xd9" or m == b"\xda":
                    return None
                length = struct.unpack(">H", f.read(2))[0]
                if m in (b"\xc0", b"\xc1", b"\xc2", b"\xc3", b"\xc5", b"\xc6", b"\xc7", b"\xc9", b"\xca", b"\xcb", b"\xcd", b"\xce", b"\xcf"):
                    _, h, w = struct.unpack(">BHH", f.read(5))
                    return w, h
                f.seek(length - 2, 1)
    except (OSError, struct.error):
        return None


def segments(command: str) -> list[list[str]]:
    out = []
    for raw in re.split(r"&&|\|\||;|\n|\|", command):
        try:
            words = shlex.split(raw)
        except ValueError:
            words = raw.split()
        while words and ENV_ASSIGN_RE.match(words[0]):
            words = words[1:]
        if words:
            out.append(words)
    return out


def is_run(words: list[str]) -> bool:
    first = words[0]
    if PYTHON_RE.match(first) or first in RUNNERS:
        return True
    return first.startswith(("./", "/", "~/")) and (first.endswith((".sh", ".py")) or "run_" in first)


def candidates(words: list[str], cwd: str) -> list[Path]:
    """Image files and input folders named on this run's command line, resolved against the hook cwd."""
    base = Path(cwd) if cwd else Path.cwd()
    found: list[Path] = []
    take_next = False
    for w in words[1:]:
        value = ""
        if take_next:
            value, take_next = w, False
        elif INPUT_FLAG_RE.match(w):
            take_next = True
            continue
        elif "=" in w and INPUT_FLAG_RE.match(w.split("=", 1)[0]):
            value = w.split("=", 1)[1]
        elif w.lower().endswith(IMAGE_EXT) or not w.startswith("-"):
            value = w
        if not value:
            continue
        p = Path(value).expanduser()
        p = p if p.is_absolute() else base / p
        if value.lower().endswith(IMAGE_EXT) or p.is_dir():
            found.append(p)
    return found


def images_of(paths: list[Path]) -> list[Path]:
    images: list[Path] = []
    for p in paths:
        try:
            if p.is_dir():
                if GENERATOR_RE.search(str(p)):   # the checkout itself, not an input folder
                    continue
                images += sorted(c for c in p.iterdir() if c.suffix.lower() in IMAGE_EXT and c.is_file())
            elif p.suffix.lower() in IMAGE_EXT:
                images.append(p)
        except OSError:
            continue
    return list(dict.fromkeys(images))


def input_gate(command: str, cwd: str = "") -> str:
    """The refusal for an image-to-3D run with a bad input, '' otherwise."""
    if not GENERATOR_RE.search(command):
        return ""
    for words in segments(command):
        if not is_run(words):   # the generator may be named in an earlier `cd ~/trellis &&` segment
            continue
        sized = [(p, s) for p in images_of(candidates(words, cwd)) if (s := image_size(p))]
        for p, (w, h) in sized:
            if max(w, h) < MIN_LONG_SIDE:
                return _refusal(p, w, h)
        if len(sized) == 1 and sized[0][1][0] >= SHEET_RATIO * sized[0][1][1]:
            return _refusal(sized[0][0], *sized[0][1])
    return ""


def _refusal(path: Path, w: int, h: int) -> str:
    return (f"Input gate: {path} is {w}x{h}; one full-size image per view, 1024 px minimum. "
            "Ask Dom for proper images. See character-from-pictures skill.")


def custom_script_warning(tool_name: str, tool_input: dict, cwd: str) -> str:
    """Gate 3 reminder when this call WRITES a custom decimate/planar/uv-projection script outside the skills folder."""
    def matches(name: str) -> bool:
        return any(fnmatch.fnmatch(Path(name).name.lower(), g) for g in SCRIPT_GLOBS)

    if tool_name in EDIT_TOOLS:
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        if not path or not matches(path):
            return ""
        try:
            Path(path).expanduser().resolve().relative_to(SKILLS_DIR.resolve())
            return ""
        except (ValueError, OSError):
            return GATE_3
    if tool_name != "Bash":
        return ""
    for words in segments(str(tool_input.get("command") or "")):
        for i, w in enumerate(words):
            target = w.lstrip(">").strip("'\"")
            if not target or not matches(target) or SKILLS_DIR.as_posix() in target or "/.claude/skills/" in target:
                continue
            after_redirect = w.startswith(">") or (i > 0 and words[i - 1].startswith(">")) or (i > 0 and words[i - 1] in (">", ">>"))
            if after_redirect or words[0] in ("tee", "cp", "mv", "install", "touch"):
                return GATE_3
    return ""


def main() -> None:
    try:   # a gate that crashes must not block honest work: any odd payload means say nothing
        payload = json.loads(sys.stdin.read() or "{}")
        tool_input = payload.get("tool_input")
        tool_input = tool_input if isinstance(tool_input, dict) else {}
        tool, cwd = str(payload.get("tool_name") or ""), str(payload.get("cwd") or "")
        refusal = input_gate(str(tool_input.get("command") or ""), cwd) if tool == "Bash" else ""
        warning = "" if refusal else custom_script_warning(tool, tool_input, cwd)
    except Exception:
        return
    if refusal:
        log(COMPONENT, f"blocked cwd={cwd} {refusal[:200]}")
        out = {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": refusal}
    elif warning:
        log(COMPONENT, f"warned cwd={cwd}")
        out = {"hookEventName": "PreToolUse", "additionalContext": warning}
    else:
        return
    sys.stdout.write(json.dumps({"hookSpecificOutput": out}))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
