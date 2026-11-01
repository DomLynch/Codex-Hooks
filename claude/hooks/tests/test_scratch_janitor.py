import importlib.util
import io
import os
import tempfile
import time
import unittest
from unittest import mock

_spec = importlib.util.spec_from_file_location(
    "scratch_janitor",
    os.path.join(
        os.path.dirname(__file__), "..", "..", "scripts", "scratch_janitor.py"
    ),
)
jan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(jan)

SID = "11111111-2222-3333-4444-555555555555"


def mk(root, rel, age_h=48, is_dir=False):
    p = os.path.join(root, rel)
    if is_dir:
        os.makedirs(p, exist_ok=True)
        f = os.path.join(p, "a.txt")
    else:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        f = p
    open(f, "w").write("x")
    t = time.time() - age_h * 3600
    os.utime(f, (t, t))
    if is_dir:
        os.utime(p, (t, t))
    return p


class ScratchJanitorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "root")
        os.makedirs(self.root)
        self.proj = os.path.join(self.tmp, "projects")
        os.makedirs(self.proj)

    def run_cands(self, ids=(), open_check=lambda p: False):
        return jan.candidates(
            self.root, ids=set(ids), projects=self.proj, open_check=open_check
        )

    def test_old_output_kinds_are_listed(self):
        a = mk(self.root, "lane/job/dist", is_dir=True)
        b = mk(self.root, "lane/job/out.tgz")
        c = mk(self.root, "lane/job/clips-r2", is_dir=True)
        self.assertEqual(sorted(self.run_cands()), sorted([a, b, c]))

    def test_recent_or_other_kinds_are_kept(self):
        mk(self.root, "lane/job/dist", age_h=2, is_dir=True)
        mk(self.root, "lane/job/src", is_dir=True)
        mk(self.root, "lane/job/notes.txt")
        mk(self.root, "lane/job/stills", is_dir=True)
        self.assertEqual(self.run_cands(), [])

    def test_recent_file_inside_keeps_the_folder(self):
        p = mk(self.root, "lane/job/dist", is_dir=True)
        open(os.path.join(p, "fresh.js"), "w").write("x")
        self.assertEqual(self.run_cands(), [])

    def test_git_checkout_is_never_touched(self):
        os.makedirs(os.path.join(self.root, "clone", ".git"))
        mk(self.root, "clone/node_modules", is_dir=True)
        self.assertEqual(self.run_cands(), [])

    def test_running_session_is_skipped(self):
        mk(self.root, "slug/%s/scratchpad/out" % SID, is_dir=True)
        self.assertEqual(self.run_cands(ids=[SID]), [])

    def test_fresh_transcript_means_running(self):
        mk(self.root, "slug/%s/scratchpad/out" % SID, is_dir=True)
        os.makedirs(os.path.join(self.proj, "slug"))
        open(os.path.join(self.proj, "slug", SID + ".jsonl"), "w").write("{}")
        self.assertEqual(self.run_cands(), [])

    def test_stale_transcript_is_not_running(self):
        p = mk(self.root, "slug/%s/scratchpad/out" % SID, is_dir=True)
        os.makedirs(os.path.join(self.proj, "slug"))
        t = os.path.join(self.proj, "slug", SID + ".jsonl")
        open(t, "w").write("{}")
        old = time.time() - 72 * 3600
        os.utime(t, (old, old))
        self.assertEqual(self.run_cands(), [p])

    def test_open_file_is_skipped(self):
        mk(self.root, "lane/dist", is_dir=True)
        self.assertEqual(self.run_cands(open_check=lambda p: True), [])

    def test_symlink_is_not_followed(self):
        target = mk(self.tmp, "elsewhere/dist", is_dir=True)
        os.makedirs(os.path.join(self.root, "lane"))
        os.symlink(target, os.path.join(self.root, "lane", "dist"))
        self.assertEqual(self.run_cands(), [])

    def test_report_only_deletes_nothing_even_with_apply(self):
        """Strategy 2026-10-29: the janitor never deletes by age on its own; there is no remove path and --apply does nothing."""
        p = mk(self.root, "lane/dist", is_dir=True)
        old = time.time() - 72 * 3600
        os.utime(p, (old, old))
        self.assertFalse(hasattr(jan, "remove"))
        with mock.patch.object(jan, "ROOT", self.root), mock.patch.object(jan, "LOG", os.path.join(self.tmp, "j.log")), mock.patch("sys.stdout", io.StringIO()) as out:
            self.assertEqual(jan.main(["--apply"]), 0)
        self.assertTrue(os.path.exists(p))
        self.assertIn("nothing deleted", out.getvalue())


if __name__ == "__main__":
    unittest.main()
