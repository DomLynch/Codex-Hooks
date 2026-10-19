import importlib.util
import os
import tempfile
import unittest

_spec = importlib.util.spec_from_file_location(
    "volume_mount_check",
    os.path.join(
        os.path.dirname(__file__), "..", "..", "scripts", "volume_mount_check.py"
    ),
)
vmc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vmc)

VOL = "/mnt/HC_Volume_107195252"
A = "/opt/frankendom-shadow/work/char-je"
B = "/srv/dev-jobs/world-z1look"

FSTAB = """\
/dev/disk/by-id/scsi-0HC_Volume_107195252 /mnt/HC_Volume_107195252 ext4 discard,nofail,defaults 0 0
/mnt/HC_Volume_107195252/work/char-je /opt/frankendom-shadow/work/char-je none bind,nofail,x-systemd.requires-mounts-for=/mnt/HC_Volume_107195252 0 0
/mnt/HC_Volume_107195252/dev-jobs/world-z1look /srv/dev-jobs/world-z1look none bind,nofail,x-systemd.requires-mounts-for=/mnt/HC_Volume_107195252 0 0
UUID=abc / ext4 defaults 0 1
"""


def info(*pairs):
    """mountinfo lines: (mountpoint, maj:min, root)."""
    return "".join(
        "%d 1 %s %s %s rw,relatime shared:1 - ext4 /dev/x rw\n" % (30 + i, mm, root, mp)
        for i, (mp, mm, root) in enumerate(pairs)
    )


class VolumeMountCheckTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.fstab = self.w("fstab", FSTAB)
        self.marker = os.path.join(self.d, "VOLUME-ALERT")
        self.log = os.path.join(self.d, "check.log")

    def w(self, name, text):
        p = os.path.join(self.d, name)
        open(p, "w").write(text)
        return p

    def check(self, mountinfo_text):
        mi = self.w("mountinfo", mountinfo_text)
        return vmc.run(self.fstab, mi, self.marker, self.log, VOL)

    GOOD = (
        (VOL, "8:16", "/"),
        (A, "8:16", "/work/char-je"),
        (B, "8:16", "/dev-jobs/world-z1look"),
    )

    def test_expected_binds_come_from_fstab(self):
        self.assertEqual(vmc.expected_binds(self.fstab, VOL), [A, B])

    def test_all_mounted_is_quiet(self):
        self.assertEqual(self.check(info(*self.GOOD)), 0)
        self.assertFalse(os.path.exists(self.marker))
        self.assertFalse(os.path.exists(self.log))

    def test_missing_bind_mount_alerts(self):
        rc = self.check(info(self.GOOD[0], self.GOOD[1]))
        self.assertEqual(rc, 1)
        self.assertIn(B, open(self.marker).read())
        self.assertIn("bind mount missing: " + B, open(self.log).read())

    def test_volume_not_mounted_alerts(self):
        rc = self.check(info((A, "8:1", "/x")))
        self.assertEqual(rc, 1)
        self.assertIn("volume not mounted", open(self.marker).read())

    def test_bind_on_the_wrong_device_alerts(self):
        rc = self.check(info(self.GOOD[0], (A, "8:1", "/work/char-je"), self.GOOD[2]))
        self.assertEqual(rc, 1)
        self.assertIn("not on the volume: " + A, open(self.marker).read())

    def test_alerts_only_once_while_it_stays_broken(self):
        broken = info(self.GOOD[0])
        self.check(broken)
        first = open(self.log).read()
        self.assertEqual(self.check(broken), 1)
        self.assertEqual(open(self.log).read(), first)

    def test_recovery_removes_the_marker_and_logs_it(self):
        self.check(info(self.GOOD[0]))
        self.assertTrue(os.path.exists(self.marker))
        self.assertEqual(self.check(info(*self.GOOD)), 0)
        self.assertFalse(os.path.exists(self.marker))
        self.assertIn("OK again", open(self.log).read())

    def test_it_never_writes_into_the_watched_folders(self):
        watched = os.path.join(self.d, "work")
        os.makedirs(os.path.join(watched, "char-je"))
        self.check(info(self.GOOD[0]))
        self.assertEqual(os.listdir(os.path.join(watched, "char-je")), [])
        self.assertTrue(self.marker.startswith(self.d))


if __name__ == "__main__":
    unittest.main()
