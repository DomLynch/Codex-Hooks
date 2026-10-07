"""Audit D: checkpoint state race (F09), deploy-guard allow scoping (F15), one lane identity for inbox (F16)."""
from __future__ import annotations

import json
import multiprocessing
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

HOOKS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS))
import deploy_guard  # noqa: E402
from lib import inbox, state  # noqa: E402


def _worker(args):
    path, sid, n = args
    state.CHECKPOINT_PATH = Path(path)
    for i in range(n):
        state.update_checkpoint_state(sid, edits=1, files=[f"{sid}-{i}.py"])


def test_concurrent_checkpoint_updates_lose_nothing(tmp_path):
    path = tmp_path / "checkpoint_state.json"
    ctx = multiprocessing.get_context("fork")
    sids = [f"s{i}" for i in range(6)]
    with ctx.Pool(6) as pool:
        pool.map(_worker, [(str(path), s, 15) for s in sids])
    data = json.loads(path.read_text())
    assert sorted(data) == sorted(sids)
    for s in sids:
        assert data[s]["edits_since_last_commit"] == 15, s
        assert len(data[s]["files"]) == 15, s


def _lock(tmp_path):
    started = (datetime.now(timezone.utc) - timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
    p = tmp_path / "lock.json"
    p.write_text(json.dumps({"revision": "abc1234def", "started": started, "pid": os.getpid(), "cwd": "/x"}))
    with mock.patch.object(deploy_guard, "LOCK_PATH", p):
        return deploy_guard.active_lock()


def test_mentioning_the_deploy_script_does_not_exempt_a_heavy_command(tmp_path):
    active = _lock(tmp_path)
    for cmd in ("test -f scripts/deploy.sh && npm test", "ls scripts/deploy.sh; npm run test:all",
                "cat scripts/deploy.sh | npm test", "(npm test) ; echo scripts/deploy.sh",
                "echo scripts/deploy.sh && npx playwright test", "bash scripts/deploy.sh && npm test"):
        assert "is in flight" in deploy_guard.decide(cmd, active), cmd


def test_real_deploy_invocations_still_pass(tmp_path):
    active = _lock(tmp_path)
    for cmd in ("bash scripts/deploy.sh", "./scripts/deploy.sh --fast", "cd /x && bash scripts/deploy.sh",
                "FOO=1 sh /Users/x/frankendom/scripts/deploy.sh", "git status && bash scripts/deploy.sh"):
        assert deploy_guard.decide(cmd, active) == "", cmd


def test_inbox_routes_by_the_same_lane_identity_as_context_size(tmp_path):
    for folder, to in (("Frankendom - The Pit", "pit"), ("Frankendom - Expansion", "expansion"),
                       ("Frankendom - Auditor", "code-quality"), ("frankendom-deploy", "deploy")):
        cwd = f"/Users/x/Developer/{folder}"
        box = tmp_path / to
        inbox.send(to, "codex", "hi", inbox=box)
        assert [m["text"] for m in inbox.claim_for(cwd, inbox=box)] == ["hi"], folder
