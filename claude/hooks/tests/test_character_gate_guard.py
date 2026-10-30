"""character_gate_guard: Dom's 2026-10-30 "set this in stone" hard gates for the character-from-pictures pipeline.
Block an image-to-3D run whose inputs are under 1024 px or are a lone contact sheet; warn (never block) on custom
decimate / planar / uv-projection scripts. Images here are header-only fakes: the guard reads sizes, not pixels."""
from __future__ import annotations

import io
import json
import struct
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import character_gate_guard as g  # noqa: E402


def png(path: Path, w: int, h: int) -> str:
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0) + b"\0" * 4)
    return str(path)


def jpg(path: Path, w: int, h: int) -> str:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\0" + b"\x01\x01\0\0\x01\0\x01\0\0"
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
    path.write_bytes(b"\xff\xd8" + app0 + sof + b"\xff\xd9")
    return str(path)


def test_blocks_small_image_in_argv_with_the_exact_message(tmp_path):
    front = png(tmp_path / "front.png", 700, 900)
    why = g.input_gate(f"python3 run_trellis.py {front} --seed 1", str(tmp_path))
    assert why == (f"Input gate: {front} is 700x900; one full-size image per view, 1024 px minimum. "
                   "Ask Dom for proper images. See character-from-pictures skill.")


def test_blocks_jpeg_and_every_generator_name(tmp_path):
    small = jpg(tmp_path / "side.jpg", 700, 1000)
    for cmd in (f"python3 run_trellis.py {small}", f"python /opt/hunyuan3d/gen.py --image {small}",
                f"cd ~/trellis && python3 example.py {small}", f"uv run trellis_run.py {small}"):
        assert g.input_gate(cmd, str(tmp_path)).startswith("Input gate: "), cmd


def test_passes_full_size_views(tmp_path):
    a, b = png(tmp_path / "front.png", 1024, 1536), jpg(tmp_path / "side.jpg", 2048, 3000)
    assert g.input_gate(f"python3 run_trellis.py {a} {b}", str(tmp_path)) == ""


def test_input_dir_is_measured_through_flags_and_bare_dirs(tmp_path):
    d = tmp_path / "views"
    d.mkdir()
    png(d / "front.png", 2000, 3000)
    png(d / "back.png", 640, 960)
    for cmd in (f"python3 run_trellis.py --input-dir {d}", f"python3 run_trellis.py --images={d}"):
        assert "back.png is 640x960" in g.input_gate(cmd, str(tmp_path)), cmd


def test_a_bare_folder_is_not_an_input_folder(tmp_path):
    """Unity Chars (2026-10-31): a prep script or find run on a Unity Assets folder was blocked for the procedural
    128x256 / 512x512 textures inside it. Only explicit image files and input-folder flags are the generator's inputs."""
    assets = tmp_path / "Assets" / "Necropolis"
    assets.mkdir(parents=True)
    png(assets / "flame.png", 128, 256)
    png(assets / "fire_sheet.png", 512, 512)
    for cmd in (f"python3 prep_trellis_inputs.py {assets}", f"cd {assets} && python3 trellis_prep.py . && find {assets} -name '*.tmp' -delete",
                f"python3 run_trellis.py --out {assets}", f"bash trellis_all.sh {assets}/"):
        assert g.input_gate(cmd, str(tmp_path)) == "", cmd


def test_contact_sheet_is_a_single_file_much_wider_than_tall(tmp_path):
    d = tmp_path / "sheet"
    d.mkdir()
    png(d / "all-views.png", 3072, 1536)
    why = g.input_gate(f"python3 run_trellis.py --input-dir {d}", str(tmp_path))
    assert "all-views.png is 3072x1536" in why
    # two views side by side in the folder are separate files, not a sheet
    png(d / "second.png", 3072, 1536)
    assert g.input_gate(f"python3 run_trellis.py --input-dir {d}", str(tmp_path)) == ""


def test_not_a_run_is_quiet(tmp_path):
    small = png(tmp_path / "front.png", 300, 300)
    for cmd in (f"ls trellis {small}", f"sips -g pixelWidth {small}", "git clone https://github.com/microsoft/TRELLIS.2 trellis",
                f"grep -rn trellis {small}", "pip install -r trellis/requirements.txt", f"open {small}",
                f"python3 resize.py {small}", "cat hunyuan3d/README.md"):
        assert g.input_gate(cmd, str(tmp_path)) == "", cmd


def test_unreadable_or_missing_inputs_fail_open(tmp_path):
    (tmp_path / "bad.png").write_bytes(b"not an image")
    assert g.input_gate("python3 run_trellis.py bad.png", str(tmp_path)) == ""
    assert g.input_gate("python3 run_trellis.py nowhere.png", str(tmp_path)) == ""


def test_relative_paths_resolve_against_the_hook_cwd(tmp_path):
    png(tmp_path / "front.png", 512, 512)
    assert "front.png is 512x512" in g.input_gate("python3 run_trellis.py front.png", str(tmp_path))


def test_hard_gate_3_warns_on_custom_scripts_outside_skills(tmp_path):
    for name in ("decimate_demon.py", "my_decimator.py", "planar_unwrap.py", "project_uv_bake.py", "Demon_Project_UV.PY"):
        assert g.custom_script_warning("Write", {"file_path": str(tmp_path / name)}, "x").startswith("Hard gate 3"), name
    assert g.custom_script_warning("Bash", {"command": "cat > decimate_it.py <<'EOF'\nEOF"}, "x").startswith("Hard gate 3")
    assert g.custom_script_warning("Bash", {"command": "cp /tmp/a.py planar_fix.py"}, "x").startswith("Hard gate 3")


def test_hard_gate_3_is_quiet_for_skills_and_other_files(tmp_path):
    skills = Path.home() / ".claude" / "skills" / "character-from-pictures" / "decimate_notes.py"
    assert g.custom_script_warning("Write", {"file_path": str(skills)}, "x") == ""
    assert g.custom_script_warning("Write", {"file_path": str(tmp_path / "decimate.md")}, "x") == ""
    assert g.custom_script_warning("Write", {"file_path": str(tmp_path / "bake.py")}, "x") == ""
    assert g.custom_script_warning("Bash", {"command": "python3 decimate_demon.py"}, "x") == ""   # running one is not writing one
    assert g.custom_script_warning("Read", {"file_path": str(tmp_path / "decimate.py")}, "x") == ""


def run_main(payload: dict) -> str:
    out = io.StringIO()
    with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), redirect_stdout(out):
        g.main()
    return out.getvalue()


def test_main_denies_the_run_and_only_warns_for_scripts(tmp_path):
    small = png(tmp_path / "front.png", 700, 700)
    denied = json.loads(run_main({"tool_name": "Bash", "cwd": str(tmp_path), "tool_input": {"command": f"python3 run_trellis.py {small}"}}))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "Input gate:" in denied["hookSpecificOutput"]["permissionDecisionReason"]
    warned = json.loads(run_main({"tool_name": "Write", "cwd": str(tmp_path), "tool_input": {"file_path": str(tmp_path / "decimate_x.py")}}))
    assert "permissionDecision" not in warned["hookSpecificOutput"]
    assert warned["hookSpecificOutput"]["additionalContext"].startswith("Hard gate 3")


def test_main_is_silent_and_never_crashes_on_odd_payloads(tmp_path):
    for payload in ({}, {"tool_name": "Bash"}, {"tool_name": "Bash", "tool_input": None}, {"tool_name": "Bash", "tool_input": {"command": 5}}):
        assert run_main(payload) == ""
    with mock.patch.object(sys, "stdin", io.StringIO("not json")), redirect_stdout(io.StringIO()) as out:
        g.main()
    assert out.getvalue() == ""
