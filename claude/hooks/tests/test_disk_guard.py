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


if __name__ == "__main__":
    unittest.main()
