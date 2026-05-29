"""Tests for the SDR white-level <-> percent conversion in brightness/core.py.

This is the math that maps a user-facing 0..100 brightness onto the HDR
SDRWhiteLevel range; off-by-one or clamp regressions here are otherwise
invisible until a dial press does the wrong thing on an HDR display.
"""

import unittest

from brightness.core import (
    SDR_WL_MAX,
    SDR_WL_MIN,
    sdr_pct_to_wl,
    sdr_wl_to_pct,
)
from brightness.protocol import format_status, parse_status


class PctToWlTests(unittest.TestCase):
    def test_endpoints(self):
        self.assertEqual(sdr_pct_to_wl(0), SDR_WL_MIN)
        self.assertEqual(sdr_pct_to_wl(100), SDR_WL_MAX)

    def test_midpoint(self):
        self.assertEqual(sdr_pct_to_wl(50), SDR_WL_MIN + (SDR_WL_MAX - SDR_WL_MIN) // 2)

    def test_clamps_out_of_range(self):
        self.assertEqual(sdr_pct_to_wl(-20), SDR_WL_MIN)
        self.assertEqual(sdr_pct_to_wl(250), SDR_WL_MAX)


class WlToPctTests(unittest.TestCase):
    def test_endpoints(self):
        self.assertEqual(sdr_wl_to_pct(SDR_WL_MIN), 0)
        self.assertEqual(sdr_wl_to_pct(SDR_WL_MAX), 100)


class RoundTripTests(unittest.TestCase):
    def test_pct_survives_round_trip(self):
        for pct in range(0, 101, 5):
            self.assertEqual(sdr_wl_to_pct(sdr_pct_to_wl(pct)), pct)


class StatusWireTests(unittest.TestCase):
    def test_format_encodes_unknown_as_dash(self):
        self.assertEqual(format_status([(0, 40), (1, None), (2, 50)]), "0:40 1:- 2:50")

    def test_parse_reads_values_and_unknowns(self):
        self.assertEqual(parse_status("0:40 1:- 2:50"), [40, None, 50])

    def test_parse_rejects_empty_and_errors(self):
        self.assertEqual(parse_status(""), [])
        self.assertEqual(parse_status("err no displays"), [])

    def test_round_trip(self):
        pairs = [(0, 35), (1, None), (2, 100)]
        self.assertEqual(parse_status(format_status(pairs)), [35, None, 100])

    def test_parse_fills_gaps_by_index(self):
        # indices need not be contiguous in the wire string
        self.assertEqual(parse_status("2:50 0:10"), [10, None, 50])


if __name__ == "__main__":
    unittest.main()
