"""COO, 2026-10-09 (Web hit it): `scripts/capture-mac.sh --dir <VPS dir> --prio 3 --hf-ok web npm run test:all` was refused as a
heavy run because the guard reads the test command's TEXT; the wrapper only ssh's to the VPS and launches a Hugging Face job. NARROW
exemption: the command starts with scripts/capture-mac.sh, carries --hf-ok among its options (before the lane), and uses no --host
or leading env (either could point the "VPS" back at this Mac). Every other heavy command stays refused. Not installed until Dom says yes."""
from __future__ import annotations

import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS_DIR))

import deploy_guard as g  # noqa: E402

ALLOWED = [
    "scripts/capture-mac.sh --dir /srv/x --prio 3 --hf-ok web npm run test:all",
    "./scripts/capture-mac.sh --hf-ok --dir /srv/x web npm run test:all",
    "scripts/capture-mac.sh --dir /srv/x --hf-ok web npm run test:all 2>&1",
]
REFUSED = [
    "scripts/capture-mac.sh --dir /srv/x --prio 3 web npm run test:all",                     # no --hf-ok
    "scripts/capture-mac.sh --dir /srv/x web npm run test:all --hf-ok",                      # --hf-ok after the lane is the command's own
    "scripts/capture-mac.sh --host localhost --dir /srv/x --hf-ok web npm run test:all",     # would run on this Mac
    "CAPTURE_HOST=localhost scripts/capture-mac.sh --dir /srv/x --hf-ok web npm run test:all",
    "CAPTURE_SSH=bash scripts/capture-mac.sh --dir /srv/x --hf-ok web npm run test:all",
    "scripts/other.sh --dir /srv/x --hf-ok web npm run test:all",                            # not the wrapper
    "scripts/capture-mac.sh --dir /srv/x --hf-ok web npm run test:all && npm run test:all",  # a second, local heavy run
    "npm run test:all",
    "bash scripts/capture-mac.sh --dir /srv/x --hf-ok web npm run test:all",
    "/tmp/x/scripts/capture-mac.sh --dir /srv/x --hf-ok web npm run test:all",               # Auditor #113: any directory
    "../evil/scripts/capture-mac.sh --dir /srv/x --hf-ok web npm run test:all",
    "/Users/d/Developer/frankendom-web/scripts/capture-mac.sh --dir /srv/x --hf-ok web npx playwright test",
]


def test_the_hf_capture_wrapper_is_allowed(monkeypatch):
    monkeypatch.setattr(g, "capture_script_is_trunk", lambda cwd: True)
    for key in [k for k in __import__("os").environ if k.startswith("CAPTURE_")]:
        monkeypatch.delenv(key)
    for cmd in ALLOWED:
        assert g.decide(cmd, None, cwd="/repo") == "", cmd


def test_everything_else_stays_refused(monkeypatch):
    monkeypatch.setattr(g, "capture_script_is_trunk", lambda cwd: True)
    for key in [k for k in __import__("os").environ if k.startswith("CAPTURE_")]:
        monkeypatch.delenv(key)
    for cmd in REFUSED:
        assert g.decide(cmd, None, cwd="/repo") == g.VPS_REASON, cmd


def test_an_exported_capture_variable_refuses_the_wrapper(monkeypatch):
    monkeypatch.setattr(g, "capture_script_is_trunk", lambda cwd: True)
    monkeypatch.setenv("CAPTURE_SSH", "bash")
    assert g.decide(ALLOWED[0], None, cwd="/repo") == g.VPS_REASON


def test_the_script_must_equal_trunks_blob_and_fails_closed(tmp_path):
    import subprocess
    def git(*a):
        subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (tmp_path / "scripts").mkdir()
    script = tmp_path / "scripts" / "capture-mac.sh"
    script.write_text("#!/bin/bash\nssh vps capture \"$@\"\n")
    git("add", ".")
    git("commit", "-qm", "x")
    assert g.capture_script_is_trunk(str(tmp_path)) is False          # no origin/HEAD: fail closed
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    assert g.capture_script_is_trunk(str(tmp_path)) is True
    script.write_text("#!/bin/bash\nbash -c \"$@\"\n")              # an edited copy on a lane branch
    assert g.capture_script_is_trunk(str(tmp_path)) is False
    assert g.capture_script_is_trunk("") is False
