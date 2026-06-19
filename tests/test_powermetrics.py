"""Tests for the powermetrics plist parsing in macos/powermetrics_daemon.py.

powermetrics needs root, so the daemon can't be exercised end-to-end in CI;
these pin the mW->W conversion and the GPU active-residency math against
synthetic plist dicts shaped like Apple Silicon powermetrics output.
"""

import unittest

from macos.powermetrics_daemon import parse_sample


class ParseSampleTests(unittest.TestCase):
    def test_cpu_and_gpu_power_mw_to_w(self):
        pl = {
            "processor": {"cpu_power": 8000.0, "gpu_power": 1500.0},
            "gpu": {"idle_ratio": 0.85},
        }
        out = parse_sample(pl)
        self.assertEqual(out["cpu_power_w"], 8.0)
        self.assertEqual(out["gpu_power_w"], 1.5)

    def test_gpu_pct_from_idle_ratio(self):
        out = parse_sample({"gpu": {"idle_ratio": 0.85}})
        self.assertEqual(out["gpu_pct"], 15.0)

    def test_gpu_pct_prefers_active_ratio(self):
        out = parse_sample({"gpu": {"active_ratio": 0.40, "idle_ratio": 0.85}})
        self.assertEqual(out["gpu_pct"], 40.0)

    def test_gpu_power_under_gpu_dict(self):
        out = parse_sample({"gpu": {"gpu_power": 2200.0}})
        self.assertEqual(out["gpu_power_w"], 2.2)

    def test_missing_fields_are_omitted(self):
        self.assertEqual(parse_sample({}), {})
        out = parse_sample({"processor": {"cpu_power": 5000.0}})
        self.assertEqual(out, {"cpu_power_w": 5.0})

    def test_case_insensitive_keys(self):
        out = parse_sample({"processor": {"CPU_Power": 3000.0}})
        self.assertEqual(out["cpu_power_w"], 3.0)


if __name__ == "__main__":
    unittest.main()
