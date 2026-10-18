"""Dom + Strategy, 2026-10-18 ("3 archives max, we don't need 25 backups"): the deploy and combat scratchpads held 25 old *.tgz bundles
(17 GB) and the Mac fell to 13 GB free. Strategy's ruling: refuse CREATING an archive (tar -c, zip) in a session scratchpad when that
scratchpad already holds 3 archives over 100 MB, OR when all scratchpads together hold more than 10 GB of them. The refusal names the oldest
ones to delete and the guard never deletes anything."""
from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from unittest import mock

HOOKS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS_DIR))

import deploy_guard  # noqa: E402
from lib import archive_guard as a  # noqa: E402

PAD = "/private/tmp/claude-501/-Users-x-Developer-frankendom-deploy/sess1/scratchpad"
MB = 1024 * 1024
GB = 1024 * MB


def item(name, age, size=800 * MB):
    return (age, size, f"{PAD}/{name}")      # (mtime, bytes, path)


THREE = [item("old.tgz", 100), item("mid.tgz", 200), item("new.tgz", 300)]


def reason(command, cwd=PAD, pad=(), everywhere=()):
    return a.archive_reason(command, cwd, lambda p: list(pad), lambda: list(everywhere))


def test_a_fourth_archive_in_one_scratchpad_is_denied_naming_the_oldest():
    for cmd in (f"tar czf {PAD}/x.tgz dist", "tar -czf x.tar.gz dist", "tar --create --gzip --file=x.tgz dist",
                f"cd {PAD} && tar -cf x.tar.gz -C /tmp dist", "zip -r x.zip dist", f"zip {PAD}/x.zip a b"):
        out = reason(cmd, pad=THREE, everywhere=THREE)
        assert "3 archives" in out and "old.tgz" in out and "never deletes" in out, cmd
        assert out.index("old.tgz") < out.index("mid.tgz"), cmd    # oldest first


def test_under_the_per_scratchpad_cap_it_passes():
    assert reason("tar czf x.tgz dist", pad=THREE[:2], everywhere=THREE[:2]) == ""


def test_the_total_cap_denies_even_a_nearly_empty_scratchpad():
    other = [(50, 6 * GB, "/private/tmp/claude-501/-Users-x-Developer-frankendom-combat/s2/scratchpad/a.tgz"),
             (60, 5 * GB, "/private/tmp/claude-501/-Users-x-Developer-frankendom-web/s3/scratchpad/b.zip")]
    out = reason("tar czf x.tgz dist", pad=[], everywhere=other)
    assert "10 GB" in out and "a.tgz" in out and "never deletes" in out
    assert out.index("a.tgz") < out.index("b.zip")
    assert reason("tar czf x.tgz dist", pad=[], everywhere=other[:1]) == ""      # 6 GB total is under the cap


def test_reading_extracting_listing_and_other_places_pass():
    for cmd in ("tar xzf x.tgz", "tar -tzf x.tgz", "tar -xf x.tar.gz -C out", "unzip x.zip", "zip -h", "echo tar czf x.tgz",
                "git commit -m 'tar czf x.tgz'", "tar czf notes.txt.bak a", "tar czf x.tgz.sha a"):
        assert reason(cmd, pad=THREE, everywhere=THREE) == "", cmd
    assert reason("tar czf /Users/x/Desktop/x.tgz dist", cwd="/Users/x/Developer/frankendom-web", pad=THREE, everywhere=THREE) == ""
    assert reason("tar czf x.tgz dist", cwd="/Users/x/Developer/frankendom-web", pad=THREE, everywhere=THREE) == ""


def test_fails_open_when_it_cannot_tell():
    def boom(_):
        raise OSError("gone")
    assert a.archive_reason("tar czf x.tgz d", PAD, boom, lambda: THREE) == ""
    assert reason("tar czf $OUT d", pad=THREE, everywhere=THREE) == ""
    assert reason("tar czf x.tgz d", cwd="", pad=THREE, everywhere=THREE) == ""


def test_the_real_listing_counts_big_archives_oldest_first_and_skips_small_ones(tmp_path):
    pad = tmp_path / "lane" / "sess" / "scratchpad"
    (pad / "sub").mkdir(parents=True)
    for name, size, mtime in (("a.tgz", 120 * MB, 2000), ("b.zip", 150 * MB, 1000), ("small.tgz", 1 * MB, 500)):
        f = pad / "sub" / name
        with open(f, "wb") as fh:
            fh.truncate(size)
        os.utime(f, (mtime, mtime))
    (pad / "node_modules").mkdir()
    with open(pad / "node_modules" / "skip.tgz", "wb") as fh:
        fh.truncate(200 * MB)
    got = a.pad_archives(str(pad))
    assert [os.path.basename(p) for _, _, p in got] == ["b.zip", "a.tgz"]
    assert a.pad_archives(str(pad / "missing")) == []


def test_deploy_guard_wires_the_archive_cap_in():
    payload = {"tool_name": "Bash", "cwd": PAD, "tool_input": {"command": "tar czf x.tgz dist"}}
    out = io.StringIO()
    with mock.patch.object(deploy_guard, "_is_mac", return_value=True), \
            mock.patch.object(a, "pad_archives", lambda pad: THREE), \
            mock.patch.object(a, "all_archives", lambda: THREE), \
            mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), mock.patch("sys.stdout", out):
        deploy_guard.main()
    assert "3 archives" in json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecisionReason"]
