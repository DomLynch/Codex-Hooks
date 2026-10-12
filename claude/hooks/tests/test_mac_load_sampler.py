import importlib.util
import os
import tempfile
import unittest
from datetime import datetime, timezone

_spec = importlib.util.spec_from_file_location(
    "mac_load_sampler",
    os.path.join(
        os.path.dirname(__file__), "..", "..", "scripts", "mac_load_sampler.py"
    ),
)
sampler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sampler)


class MacLoadSamplerTest(unittest.TestCase):
    def test_parse_mac_and_linux_uptime(self):
        self.assertEqual(
            sampler.parse_uptime("9:29  up 1 day, load averages: 3.87 4.53 4.73"),
            ("3.87", "4.53", "4.73"),
        )
        self.assertEqual(
            sampler.parse_uptime("up 3 days, load average: 0.10, 0.20, 0.30"),
            ("0.10", "0.20", "0.30"),
        )
        self.assertIsNone(sampler.parse_uptime("garbage"))

    def test_row_format_dubai_time_and_single_header(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "mac-load-log.csv")
            now = datetime(2026, 10, 12, 5, 39, tzinfo=timezone.utc)
            sampler.append_row(p, ("3.87", "4.53", "4.73"), now)
            sampler.append_row(p, ("1.00", "2.00", "3.00"), now)
            self.assertEqual(
                open(p).read().splitlines(),
                [
                    "time_dubai,load1,load5,load15",
                    "2026-10-12T09:39,3.87,4.53,4.73",
                    "2026-10-12T09:39,1.00,2.00,3.00",
                ],
            )

    def test_main_still_logs_mac_when_vps_unreachable(self):
        with tempfile.TemporaryDirectory() as d:
            orig = sampler.VPS_CMD
            sampler.VPS_CMD = ["false"]
            try:
                self.assertEqual(sampler.main(d), 0)
            finally:
                sampler.VPS_CMD = orig
            self.assertTrue(os.path.exists(os.path.join(d, "mac-load-log.csv")))
            self.assertFalse(os.path.exists(os.path.join(d, "vps-load-log.csv")))


if __name__ == "__main__":
    unittest.main()
