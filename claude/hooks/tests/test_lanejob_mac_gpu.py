"""Mac GPU slot (Dom's word via Lead and the COO, 2026-10-12: order Mac -> VPS -> HF for release browser rows and stills): the lanejob wrapper may carry
`bash scripts/lanejob/lanejob-mac.sh --gpu node scripts/<release-checks|*still*>.mjs [simple args]` as the ENTIRE command. Same shape and trust model as
test_lanejob_mac_exempt.py (relative path, wrapper + scripts/lanejob equal trunk, no redirecting env). Every other script, and every other way to say it,
stays refused; the CPU-only form keeps refusing browser jobs."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS_DIR))

import deploy_guard as g  # noqa: E402

W = "bash scripts/lanejob/lanejob-mac.sh --gpu --"
ROW = "node scripts/pit-landscape-check.mjs --engine chromium"   # a release_commands row of the fixture
COUNTED = "node scripts/arena-gl-check.mjs"
WEBKIT = "node scripts/pit-landscape-webkit.mjs --engine webkit"      # release row 54: a Mac-only WebKit row
SPACED = ["bash", "-c", "node scripts/x-check.mjs"]                  # one argv element holds spaces: the joined text is ambiguous


@pytest.fixture
def repo(tmp_path, monkeypatch):
    def git(*a):
        subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (tmp_path / "scripts" / "lanejob").mkdir(parents=True)
    (tmp_path / "scripts" / "lanejob" / "lanejob-mac.sh").write_text("#!/bin/bash\n# runs a job under gates\n")
    (tmp_path / ".quality-gate.json").write_text(json.dumps({"release_commands": [ROW.split(), COUNTED.split(), WEBKIT.split(), SPACED]}))
    git("add", ".")
    git("commit", "-qm", "x")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    for key in [k for k in os.environ if k.startswith("LANEJOB_") or k in g.LANEJOB_MAC_ENV]:
        monkeypatch.delenv(key)
    return tmp_path


def _decide(cmd, repo):
    return g.decide(cmd, None, cwd=str(repo))


@pytest.mark.parametrize("cmd", [
    f"{W} {ROW}",
    f"{W} {COUNTED} 2>&1",
    f"{W} node scripts/zone1-signin-stills.mjs",
    f"{W} node scripts/stance-stills.mjs --label x",
    f"{W} node scripts/gear-paperdoll-still.mjs",
    f"{W} node ./scripts/zone-theme-stills.mjs",
    f"{W} node scripts/hero-contact-sheet.mjs",
    f"{W} node scripts/zone1-stills-mac.mjs --label ./out",
])
def test_the_gpu_forms_are_allowed(cmd, repo):
    assert _decide(cmd, repo) == "", cmd


@pytest.mark.parametrize("cmd", [
    f"{W} node scripts/browser-replay-check.mjs --engine webkit",   # LOW 1 (a listed WebKit row is a Mac-only row)
    f"{W} bash -c node scripts/x-check.mjs",                              # LOW 3: rows are compared as argv, not as joined text
    f"{W} node scripts/zone1-signin-stills.mjs /etc/passwd",              # LOW 4: no absolute-path args
    f"{W} node scripts/zone1-signin-stills.mjs --out=/Users/x/.ssh",      # Auditor on #134: an absolute path after =
    f"{W} node scripts/browser-replay-check.mjs",                         # a release script that is not a row of trunk's list
    f"{W} {ROW} --extra",                                                 # a row is matched exactly, not by prefix
    f"{W} node scripts/pit-landscape-check.mjs --engine webkit",
    f"{W} npx playwright test",
    f"{W} npm run test:all",
    f"{W} npm test",
    f"{W} {ROW} && npm run test:all",
    f"{W} {ROW}; npm test",
    f"{W} {ROW} | tee out",
    f"{W} {ROW} $(npm run test:all)",
    f"{W} node scripts/zone1-signin-stills.mjs ../evil",
    f"{W} node scripts/zone1-signin-stills.mjs > scripts/lanejob/lanejob-mac.sh",
    f"{W} node scripts/zone1-signin-stills.mjs && npm test",
    f"{W} node scripts/zone1-signin-stills.mjs --x=$(npm test)",
    "bash scripts/lanejob/lanejob-mac.sh --gpu node scripts/zone1-signin-stills.mjs && npm test",   # no `--`
    "bash scripts/lanejob/lanejob-mac.sh -- node scripts/pit-landscape-check.mjs --engine chromium",   # no --gpu: the CPU-only form refuses browsers
    f"cd /tmp/x && {W} {ROW}",
    f"LANEJOB_MAC_LOAD=1 {W} {ROW}",
    f"sh scripts/lanejob/lanejob-mac.sh --gpu -- {ROW}",
    f"{W} node scripts/pit-landscape-check.mjs --engine chromium && node scripts/other-check.mjs",
])
def test_everything_else_stays_refused(cmd, repo):
    assert _decide(cmd, repo) == g.VPS_REASON, cmd


def test_an_edited_wrapper_refuses_the_gpu_form(repo):
    (repo / "scripts" / "lanejob" / "lanejob-mac.sh").write_text("#!/bin/bash\nnpm run test:all\n")
    assert _decide(f"{W} {ROW}", repo) == g.VPS_REASON


def test_webkit_row_and_stillness_name_are_not_heavy_so_the_guard_never_sees_them_but_the_job_set_excludes_them(repo):
    # LOW 1 and 2: neither command is heavy by HEAVY_RE (the guard only gates heavy commands); the job set itself refuses them, and the wrapper does too.
    assert ("node", "scripts/pit-landscape-webkit.mjs", "--engine", "webkit") not in g.trunk_release_rows(str(repo))
    assert not g.lanejob_gpu_job_ok(["node", "scripts/deploy-stillness.mjs"], str(repo))
    assert g.lanejob_gpu_job_ok(["node", "scripts/zone1-signin-stills.mjs"], str(repo))
