"""Lead, 2026-10-10: `node scripts/gpu-run.mjs <sha> ... -- <cmd>` only launches a Hugging Face T4 job, so the Mac heavy-job guard lets it
through. NARROW: the call is the entire command, a 40-hex sha, only --blender / --timeout, nothing in the environment that redirects it,
and the whole scripts/ folder equals trunk's. Everything else stays refused. Probes come from the #113/#120 audits."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS_DIR))

import deploy_guard as g  # noqa: E402

SHA = "a" * 40
CALL = f"node scripts/gpu-run.mjs {SHA} --timeout 30m -- npm run test:all"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    def git(*a):
        subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (tmp_path / "scripts" / "lib").mkdir(parents=True)
    (tmp_path / "scripts" / "gpu-run.mjs").write_text("// launches an HF job\n")
    (tmp_path / "scripts" / "lib" / "gpu-run.mjs").write_text("export const x = 1\n")
    git("add", ".")
    git("commit", "-qm", "x")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    for key in [k for k in os.environ if k.startswith(("GPU_RUN_", "CAPTURE_")) or k in g.GPU_RUN_ENV]:
        monkeypatch.delenv(key)
    return tmp_path


def _decide(cmd, repo):
    return g.decide(cmd, None, cwd=str(repo))


def test_the_launch_form_is_allowed(repo, monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "x")   # the login itself is fine
    for cmd in (CALL, CALL.replace("node scripts", "node ./scripts"), CALL + " 2>&1",
                f"node scripts/gpu-run.mjs {SHA} --blender -- npm run test:all",
                f"node scripts/gpu-run.mjs {SHA} -- npx playwright test --project=desktop"):
        assert _decide(cmd, repo) == "", cmd


@pytest.mark.parametrize("cmd", [
    "node scripts/gpu-run.mjs -- npm run test:all",                                   # no sha
    f"node scripts/gpu-run.mjs {'a' * 39} -- npm run test:all",                       # not 40 hex
    "node scripts/gpu-run.mjs main -- npm run test:all",                              # a branch name, not a sha
    f"node scripts/gpu-run.mjs {SHA} --host localhost -- npm run test:all",           # unknown option
    f"node scripts/gpu-run.mjs {SHA} --timeout 30 -- npm run test:all",              # malformed timeout
    f"node scripts/gpu-run.mjs {SHA} npm run test:all",                               # no `--`
    f"GPU_RUN_HF=bash node scripts/gpu-run.mjs {SHA} -- npm run test:all",            # env prefix
    f"cd /tmp/x && node scripts/gpu-run.mjs {SHA} -- npm run test:all",               # earlier segment
    f"node scripts/gpu-run.mjs {SHA} -- npm run test:all && npm run test:all",        # a second, local heavy run
    f"node scripts/gpu-run.mjs {SHA} -- npm run test:all; npm run test:all",
    f"node scripts/gpu-run.mjs {SHA} -- npm run test:all | tee out",
    f"node scripts/gpu-run.mjs {SHA} -- $(npm run test:all)",
    f"bash scripts/gpu-run.mjs {SHA} -- npm run test:all",
    f"node /tmp/x/scripts/gpu-run.mjs {SHA} -- npm run test:all",                    # any directory
    f"node ../evil/scripts/gpu-run.mjs {SHA} -- npm run test:all",
    "node scripts/other.mjs " + SHA + " -- npm run test:all",
    "npm run test:all",
])
def test_everything_else_stays_refused(cmd, repo):
    assert _decide(cmd, repo) == g.VPS_REASON, cmd


@pytest.mark.parametrize("name", ["GPU_RUN_HF", "GPU_RUN_OUT", "HF_BIN", "HF_LEDGER_DIR", "HF_WALL_ROWS_ENV_FILE", "NODE_OPTIONS", "NODE_PATH"])
def test_a_redirecting_environment_variable_refuses_it(name, repo, monkeypatch):
    monkeypatch.setenv(name, "bash")   # GPU_RUN_HF=bash would run bash on this Mac as the `hf` binary
    assert _decide(CALL, repo) == g.VPS_REASON


def test_the_scripts_folder_must_equal_trunk_and_fails_closed(repo):
    assert _decide(CALL, repo) == ""
    (repo / "scripts" / "lib" / "gpu-run.mjs").write_text("export const x = () => require('child_process').execSync('npm test')\n")
    assert _decide(CALL, repo) == g.VPS_REASON                                       # an edited import, not just the main script
    subprocess.run(["git", "-C", str(repo), "checkout", "--", "scripts"], check=True, capture_output=True)
    assert _decide(CALL, repo) == ""
    (repo / "scripts" / "extra.mjs").write_text("// untracked\n")
    assert _decide(CALL, repo) == g.VPS_REASON                                       # an untracked file in scripts/
    (repo / "scripts" / "extra.mjs").unlink()
    assert _decide(CALL, repo) == ""
    subprocess.run(["git", "-C", str(repo), "symbolic-ref", "--delete", "refs/remotes/origin/HEAD"], check=True, capture_output=True)
    assert _decide(CALL, repo) == g.VPS_REASON                                       # no origin/HEAD: cannot compare
    assert g.decide(CALL, None, cwd="") == g.VPS_REASON                              # no cwd


def test_nothing_after_the_separator_is_not_exempted(repo):
    cmd = f"node scripts/gpu-run.mjs {SHA} --"   # not a heavy command, so the guard has nothing to refuse; the exemption must still say no
    assert g.gpu_run_wrapper(cmd, str(repo), cmd) is False
