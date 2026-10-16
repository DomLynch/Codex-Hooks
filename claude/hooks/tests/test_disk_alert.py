"""disk_alert: one line when the Mac is under 30 GB free, once an hour per lane, naming the biggest dir to move.
Strategy/Dom 2026-10-16: the Mac hit 94% disk with 65 side worktrees; lanes need to know before they start a large copy."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import disk_alert as d  # noqa: E402

GB = 1024 ** 3
HOUR = 3600


def line(tmp_path, free_gb, session="s1", now=1000.0, spawned=None, state=None):
    return d.disk_line(session, state or tmp_path / "disk_alert.json", free_bytes=int(free_gb * GB), now=now,
                       spawn=(spawned.append if spawned is not None else (lambda *_: None)))


def test_quiet_at_or_above_30_gb(tmp_path):
    assert line(tmp_path, 30) == "" and line(tmp_path, 105) == ""


def test_line_under_30_gb_names_free_space_and_the_rule(tmp_path):
    text = line(tmp_path, 24.4)
    assert "Mac disk 24 GB free" in text and "no large copies or builds here" in text and "move work to the VPS" in text


def test_once_an_hour_per_lane(tmp_path):
    assert line(tmp_path, 20, "a", now=0) != ""
    assert line(tmp_path, 20, "a", now=HOUR - 1) == ""          # same lane inside the hour: silent
    assert line(tmp_path, 20, "b", now=10) != ""                # another lane is told once too
    assert line(tmp_path, 20, "a", now=HOUR + 1) != ""          # the hour passed


def test_names_the_cached_biggest_offender(tmp_path):
    state = tmp_path / "disk_alert.json"
    d.save_offender(state, "/Users/x/Library/Application Support/Claude/vm_bundles", 12.1, now=900.0)
    text = line(tmp_path, 20, now=1000.0, state=state)
    assert "Biggest: /Users/x/Library/Application Support/Claude/vm_bundles (12 GB)" in text


def test_no_offender_cached_still_warns_and_starts_one_refresh(tmp_path):
    spawned: list = []
    assert "Biggest" not in line(tmp_path, 20, "a", now=0, spawned=spawned)
    assert len(spawned) == 1
    line(tmp_path, 20, "b", now=5, spawned=spawned)            # a refresh is already running: no second one
    assert len(spawned) == 1


def test_stale_offender_is_refreshed_not_trusted_blindly(tmp_path):
    state = tmp_path / "disk_alert.json"
    d.save_offender(state, "/old", 9.0, now=0.0)
    spawned: list = []
    line(tmp_path, 20, now=2 * HOUR, state=state, spawned=spawned)
    assert len(spawned) == 1


def test_biggest_offender_only_from_the_two_allowed_parents(tmp_path):
    dev = tmp_path / "Developer"
    claude = tmp_path / "Claude"
    (dev / "lane-a").mkdir(parents=True)
    (dev / "lane-a" / "f").write_bytes(b"x" * 5000)
    (claude / "vm_bundles").mkdir(parents=True)
    (claude / "vm_bundles" / "f").write_bytes(b"x" * 90000)
    other = tmp_path / "Pictures" / "big"
    other.mkdir(parents=True)
    (other / "f").write_bytes(b"x" * 900000)
    path, size = d.biggest_offender([dev, claude])
    assert path == str(claude / "vm_bundles") and size > 0


def test_prompt_hook_appends_the_line_and_only_when_there_is_one(monkeypatch):
    import user_prompt_submit as ups
    monkeypatch.setattr(ups, "disk_line", lambda session: f"LINE {session}")
    assert ups.with_disk_line("ctx", "s9") == "ctx\n\nLINE s9"
    monkeypatch.setattr(ups, "disk_line", lambda session: "")
    assert ups.with_disk_line("ctx", "s9") == "ctx"


def test_any_failure_means_say_nothing(tmp_path):
    bad = tmp_path / "not-a-dir" / "x" / "disk_alert.json"
    bad.parent.parent.write_text("a file where a directory should be")
    assert d.disk_line("s", bad, free_bytes=1 * GB, now=0.0, spawn=lambda *_: None) == ""
