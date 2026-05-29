"""Tests for the LibreHardwareMonitor JSON-tree parsing in bar/main.py.

These exercise the reverse-engineered shape of the LHM /data.json tree —
the trickiest, most brittle parsing in the project — without needing LHM or
any hardware running.
"""

import unittest

from bar.main import (
    DISKS,
    _disk_subtree_sensors,
    _parse_lhm,
    _parse_lhm_value,
)


def node(text="", ntype="", value=None, children=None):
    return {"Text": text, "Type": ntype, "Value": value, "Children": children or []}


class ParseValueTests(unittest.TestCase):
    def test_strips_units(self):
        self.assertEqual(_parse_lhm_value("62.0 °C"), 62.0)
        self.assertEqual(_parse_lhm_value("18.2 %"), 18.2)
        self.assertEqual(_parse_lhm_value("800 RPM"), 800.0)

    def test_comma_decimal(self):
        # LHM uses the locale decimal separator; comma must parse.
        self.assertEqual(_parse_lhm_value("18,2 %"), 18.2)

    def test_rejects_garbage(self):
        self.assertIsNone(_parse_lhm_value(None))
        self.assertIsNone(_parse_lhm_value(""))
        self.assertIsNone(_parse_lhm_value("   "))
        self.assertIsNone(_parse_lhm_value("n/a"))


class DiskSubtreeTests(unittest.TestCase):
    def test_composite_preferred_over_plain(self):
        dev = node("CT1000P2SSD8", "", None, [
            node("Temperatures", "", None, [
                node("Temperature", "Temperature", "45.0 °C"),
                node("Composite Temperature", "Temperature", "40.0 °C"),
            ]),
            node("Load", "", None, [
                node("Total Activity", "Load", "7.0 %"),
            ]),
        ])
        temp, activity = _disk_subtree_sensors(dev)
        self.assertEqual(temp, 40.0)
        self.assertEqual(activity, 7.0)

    def test_plain_fallback_when_no_composite(self):
        dev = node("BX500", "", None, [
            node("Temperature", "Temperature", "33.0 °C"),
            node("Total Activity", "Load", "0.0 %"),
        ])
        temp, activity = _disk_subtree_sensors(dev)
        self.assertEqual(temp, 33.0)
        self.assertEqual(activity, 0.0)


class ParseLhmTests(unittest.TestCase):
    def test_cpu_temp_and_power_preferred_labels(self):
        tree = node("root", "", None, [
            node("CPU Package", "Temperature", "65.0 °C"),
            node("Core Average", "Temperature", "55.0 °C"),
            node("CPU Package", "Power", "88.0 W"),
        ])
        r = _parse_lhm(tree)
        self.assertEqual(r.cpu_temp_c, 65.0)   # Package wins over Core Average
        self.assertEqual(r.cpu_power_w, 88.0)

    def test_cpu_temp_falls_back_to_core_average(self):
        tree = node("root", "", None, [
            node("Core Average", "Temperature", "57.0 °C"),
        ])
        r = _parse_lhm(tree)
        self.assertEqual(r.cpu_temp_c, 57.0)

    def test_gpu_fan_is_averaged(self):
        tree = node("root", "", None, [
            node("GPU Fan 1", "Fan", "1000 RPM"),
            node("GPU Fan 2", "Fan", "1400 RPM"),
        ])
        r = _parse_lhm(tree)
        self.assertEqual(r.gpu_fan_rpm, 1200.0)

    def test_motherboard_fan_numbers(self):
        tree = node("root", "", None, [
            node("Fan #2", "Fan", "820 RPM"),
            node("Fan #6", "Fan", "1500 RPM"),
        ])
        r = _parse_lhm(tree)
        self.assertEqual(r.motherboard_fans.get(2), 820.0)
        self.assertEqual(r.motherboard_fans.get(6), 1500.0)

    def test_duplicate_disk_model_occurrence_ordering(self):
        # Two drives share a model name; they must land at occurrence 0 and 1
        # in tree order so DISKS' lhm_index pairing works.
        model = "CT2000BX500SSD1"
        # confirm the fixture matches the project's actual disk config
        self.assertGreaterEqual(sum(1 for d in DISKS if d.lhm_model == model), 2)
        tree = node("root", "", None, [
            node(f"{model} #A", "", None, [
                node("Temperature", "Temperature", "31.0 °C"),
                node("Total Activity", "Load", "1.0 %"),
            ]),
            node(f"{model} #B", "", None, [
                node("Temperature", "Temperature", "44.0 °C"),
                node("Total Activity", "Load", "2.0 %"),
            ]),
        ])
        r = _parse_lhm(tree)
        self.assertEqual(r.disks[(model, 0)], (31.0, 1.0))
        self.assertEqual(r.disks[(model, 1)], (44.0, 2.0))


if __name__ == "__main__":
    unittest.main()
