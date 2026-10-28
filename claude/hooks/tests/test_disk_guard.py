import importlib.util
import os
import unittest

_spec = importlib.util.spec_from_file_location(
    "disk_guard",
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "disk_guard.py"),
)
dg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dg)

DF = """\
Filesystem                1024-blocks      Used Available Capacity Mounted on
/dev/sda1                   315621376 232000000  69000000      77% /
/dev/sdb                    205520896 193000000  12000000      95% /mnt/HC_Volume_107195252
u685@box:frankendom-donors 5368709120 4400000000 968709120      82% /mnt/frankendom-donors
"""


class DiskGuardTests(unittest.TestCase):
    def test_parse_df_reads_every_mount(self):
        d = dg.parse_df(DF)
        self.assertEqual(set(d), {"/", "/mnt/HC_Volume_107195252", "/mnt/frankendom-donors"})
        self.assertEqual(d["/mnt/HC_Volume_107195252"][3], 94)

    def test_85_percent_is_full_and_84_is_not(self):
        rows = dg.evaluate([("a", (100, 85, 15, 85)), ("b", (100, 84, 16, 84))])
        self.assertEqual([r[1] for r in rows], ["FULL", "OK"])

    def test_unreadable_target_is_never_ok(self):
        rows = dg.evaluate([("VPS root", None)])
        self.assertEqual(rows[0][1], "UNREADABLE")

    def test_one_line_per_target_per_day(self):
        rows = dg.evaluate([("Mac", (100, 90, 10, 90))])
        first = dg.new_lines(rows, [], "2026-10-26")
        self.assertEqual(len(first), 1)
        self.assertEqual(dg.new_lines(rows, first, "2026-10-26"), [])
        self.assertEqual(len(dg.new_lines(rows, first, "2026-10-27")), 1)

    def test_ok_target_writes_nothing(self):
        rows = dg.evaluate([("Mac", (100, 50, 50, 50))])
        self.assertEqual(dg.new_lines(rows, [], "2026-10-26"), [])

    # 2026-10-28 (Dom + Strategy): the guard runs every 2 hours now and must name the lane and the folder that filled the disk.
    # Strategy's rule: one line per target per day even when the percentage moves, or a 2-hourly run would spam the note.
    def test_second_run_same_day_adds_nothing_even_if_percent_moved(self):
        first = dg.new_lines(dg.evaluate([("VPS root", (100, 90, 10, 90))]), [], "2026-10-28")
        later = dg.evaluate([("VPS root", (100, 93, 7, 93))])
        self.assertEqual(dg.new_lines(later, first, "2026-10-28"), [])
        self.assertEqual(len(dg.new_lines(later, first, "2026-10-29")), 1)

    def test_old_format_line_still_blocks_a_duplicate(self):
        old = ["2026-10-27 disk-guard FULL Mac: 90% used, 41 GB free"]
        rows = dg.evaluate([("Mac", (100, 91, 9, 91))])
        self.assertEqual(dg.new_lines(rows, old, "2026-10-27"), [])

    def test_owner_for_names_the_lane(self):
        o = dg.owner_for
        self.assertEqual(o("/srv/dev-jobs/art-roach"), "Armagedom Art")
        self.assertEqual(o("/srv/dev-jobs/v3/pickup"), "Frankendom v3 Dev")
        self.assertEqual(o("/srv/dev-jobs/physics-rev465"), "Physics Dev")
        self.assertEqual(o("/srv/dev-jobs/pd-rev465"), "Physics Dev")
        self.assertEqual(o("/srv/dev-jobs/opentrident-site"), "OpenTrident")
        self.assertEqual(o("/srv/dev-jobs/hub-npcs"), "Armagedom Lead")
        self.assertEqual(o("/opt/armagedom-preview-staging"), "Server & Deploy")
        self.assertEqual(o("/Users/x/Developer/armagedom/.claude/worktrees/agent-a1"), "Armagedom Lead")
        self.assertEqual(o("/private/tmp/claude-501/-Users-x-Developer-frankendom-v3/abc"), "Frankendom v3 Dev")
        self.assertEqual(o("/private/tmp/claude-501/-Users-x-Developer-physics/abc"), "Physics Dev")
        self.assertEqual(o("/private/tmp/claude-501/-Users-x-Developer-armagedom--claude-worktrees-arma-deploy-38096f"), "Server & Deploy")
        self.assertEqual(o("/mnt/HC_Volume_107195252/ue-keys"), "OpenTrident")
        self.assertEqual(o("/somewhere/unknown"), "unknown owner")

    def test_parse_du_takes_the_biggest_and_skips_the_base_dir_and_small_ones(self):
        out = "500\t/srv/dev-jobs/tiny\n9216\t/srv/dev-jobs/v3\n45509\t/srv/dev-jobs\n4747\t/srv/dev-jobs/hub-npcs\n"
        top = dg.parse_du(out, bases=("/srv/dev-jobs",), min_mb=1000)
        self.assertEqual(top, [(9216, "/srv/dev-jobs/v3"), (4747, "/srv/dev-jobs/hub-npcs")])

    def test_offender_text_names_size_dir_and_lane_for_the_top_three(self):
        top = [(9216, "/srv/dev-jobs/v3"), (4747, "/srv/dev-jobs/hub-npcs"), (4123, "/srv/dev-jobs/armagedom-crowd30"), (2766, "/srv/dev-jobs/world")]
        text = dg.offender_text(top)
        self.assertIn("9 GB /srv/dev-jobs/v3 (Frankendom v3 Dev)", text)
        self.assertIn("5 GB /srv/dev-jobs/hub-npcs (Armagedom Lead)", text)
        self.assertNotIn("world", text)

    def test_full_line_carries_the_offenders(self):
        rows = dg.evaluate([("VPS root", (100, 93, 7, 93))], offenders={"VPS root": [(9216, "/srv/dev-jobs/v3")]})
        line = dg.new_lines(rows, [], "2026-10-28")[0]
        self.assertIn("VPS root: 93% used", line)
        self.assertIn("/srv/dev-jobs/v3 (Frankendom v3 Dev)", line)


if __name__ == "__main__":
    unittest.main()
