"""Mac middle tier, piece 2 (Strategy + Dom, 2026-10-12): `bash scripts/lanejob/lanejob-mac.sh CMD...` is the only way a CPU-only heavy-looking
job (targeted `node --test a b`) may run on the Mac. NARROW, same shape as capture-mac / gpu-run: the call is the entire command, relative
script path, the script AND scripts/lanejob equal trunk's, nothing in the environment redirects it, and the inner command is never a
browser, full suite, build or Blender run. Everything else stays refused."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS_DIR))

import deploy_guard as g  # noqa: E402

CALL = "bash scripts/lanejob/lanejob-mac.sh node --test tests/a.test.ts tests/b.test.ts"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    def git(*a):
        subprocess.run(
            ["git", "-C", str(tmp_path), *a], check=True, capture_output=True
        )

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (tmp_path / "scripts" / "lanejob").mkdir(parents=True)
    (tmp_path / "scripts" / "lanejob" / "lanejob-mac.sh").write_text(
        "#!/bin/bash\n# runs a CPU-only job under gates\n"
    )
    git("add", ".")
    git("commit", "-qm", "x")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    for key in [
        k for k in os.environ if k.startswith("LANEJOB_") or k in g.LANEJOB_MAC_ENV
    ]:
        monkeypatch.delenv(key)
    return tmp_path


def _decide(cmd, repo):
    return g.decide(cmd, None, cwd=str(repo))


def test_the_wrapper_call_is_allowed(repo, monkeypatch):
    monkeypatch.setenv("LANEJOB_NO_MAC", "1")  # the off-switch only turns the tier off
    for cmd in (CALL, CALL.replace("bash scripts", "bash ./scripts"), CALL + " 2>&1",
                    "bash scripts/lanejob/lanejob-mac.sh node --test --test-concurrency=2 tests/a.test.ts tests/b.test.ts"):
        assert _decide(cmd, repo) == "", cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "bash scripts/lanejob/lanejob-mac.sh npm run test:all",  # full suite
        "bash scripts/lanejob/lanejob-mac.sh npx playwright test tests/a.spec.ts",  # browser
        "bash scripts/lanejob/lanejob-mac.sh npm run build",
    "bash scripts/lanejob/lanejob-mac.sh npm test",                                  # Auditor on #129: wrapped is never looser than bare
    "bash scripts/lanejob/lanejob-mac.sh node scripts/origins-signedin-check.mjs",
    "bash scripts/lanejob/lanejob-mac.sh node scripts/pit-landscape-check.mjs",
    "bash scripts/lanejob/lanejob-mac.sh node --test tests/*",                       # a whole-suite glob
    "bash scripts/lanejob/lanejob-mac.sh node --test tests/*.test.ts tests/a.test.ts",
    "bash scripts/lanejob/lanejob-mac.sh vite build",
        "bash scripts/lanejob/lanejob-mac.sh node --test tests/a.test.ts tests/b.test.ts --project webkit",
        "bash scripts/lanejob/lanejob-mac.sh blender -b x.blend",
        "bash scripts/lanejob/lanejob-mac.sh node --test tests/a.test.ts tests/b.test.ts && npm run test:all",
        CALL + "; npm run test:all",
        CALL + " | tee out",
        "bash scripts/lanejob/lanejob-mac.sh node --test $(npm run test:all) tests/b.test.ts",
        "cd /tmp/x && " + CALL,
        "LANEJOB_MAC_LOAD=99 " + CALL,  # env prefix
        "bash /tmp/x/scripts/lanejob/lanejob-mac.sh node --test tests/a.test.ts tests/b.test.ts",
        "bash ../evil/scripts/lanejob/lanejob-mac.sh node --test tests/a.test.ts tests/b.test.ts",
        "sh scripts/lanejob/lanejob-mac.sh node --test tests/a.test.ts tests/b.test.ts",
        "bash scripts/lanejob/other.sh node --test tests/a.test.ts tests/b.test.ts",
        CALL + " > scripts/lanejob/lanejob-mac.sh",  # redirect is a write
        "node --test tests/a.test.ts tests/b.test.ts",  # no wrapper
    ],
)
def test_everything_else_stays_refused(cmd, repo):
    assert _decide(cmd, repo) == g.VPS_REASON, cmd


def test_an_edited_wrapper_or_extra_file_refuses_it(repo):
    script = repo / "scripts" / "lanejob" / "lanejob-mac.sh"
    script.write_text("#!/bin/bash\nnpm run test:all\n")
    assert _decide(CALL, repo) == g.VPS_REASON
    subprocess.run(["git", "-C", str(repo), "checkout", "--", "scripts"], check=True)
    assert _decide(CALL, repo) == ""
    (repo / "scripts" / "lanejob" / "extra.sh").write_text(
        "x\n"
    )  # untracked sibling the wrapper could source
    assert _decide(CALL, repo) == g.VPS_REASON


@pytest.mark.parametrize(
    "name",
    [
        "LANEJOB_MAC_LOAD",
        "LANEJOB_MAC_LOCK",
        "LANEJOB_MAC_LOG",
        "LANEJOB_SSH",
        "LANEJOB_MAC_CAP",
        "NODE_OPTIONS",
        "NODE_PATH",
        "BASH_ENV",
    ],
)
def test_a_redirecting_environment_variable_refuses_it(name, repo, monkeypatch):
    monkeypatch.setenv(name, "x")
    assert _decide(CALL, repo) == g.VPS_REASON
