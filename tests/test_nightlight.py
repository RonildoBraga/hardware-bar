"""Tests for the Night Light CloudStore-blob manipulation in nightlight/core.py.

The blob layout is reverse-engineered and the on/off toggle threads a marker
insert/remove + length-byte adjust + timestamp bump through it. These tests
pin that logic against a synthetic blob so a regression is caught without
touching the live registry.
"""

import unittest

from nightlight.core import (
    ENABLE_MARKER,
    SENTINEL,
    _bump_timestamp,
    _inner_sentinel_offsets,
    _is_enabled_from_blob,
    _set_enabled_in_blob,
)

LL = 0x0A  # arbitrary inner-section length byte


def make_off_blob():
    return bytes(
        SENTINEL                       # outer sentinel (idx 0-3)
        + bytes([0x0A, 0x02, 0x01, 0x00, 0x2A, 0x06])  # header (idx 4-9)
        + bytes([0x80, 0x81, 0x82, 0x83, 0x84])        # timestamp varint (idx 10-14)
        + bytes([LL])                  # length byte (idx 15)
        + SENTINEL                     # inner sentinel (idx 16-19)
        + bytes([0x00, 0x00, 0x00])    # trailing state, no ENABLE_MARKER => off
    )


class OffsetTests(unittest.TestCase):
    def test_locates_inner_section(self):
        blob = make_off_blob()
        length_idx, after_inner = _inner_sentinel_offsets(blob)
        self.assertEqual(length_idx, 15)
        self.assertEqual(after_inner, 20)

    def test_raises_without_inner_sentinel(self):
        with self.assertRaises(RuntimeError):
            _inner_sentinel_offsets(SENTINEL + b"\x00\x00")


class EnableStateTests(unittest.TestCase):
    def test_off_blob_reads_off(self):
        self.assertFalse(_is_enabled_from_blob(make_off_blob()))

    def test_turning_on_inserts_marker_and_grows_length(self):
        off = make_off_blob()
        on = _set_enabled_in_blob(off, True)
        self.assertTrue(_is_enabled_from_blob(on))
        # marker present right after the inner sentinel
        _, pos = _inner_sentinel_offsets(on)
        self.assertEqual(on[pos:pos + len(ENABLE_MARKER)], ENABLE_MARKER)
        # length byte grew by len(marker)
        self.assertEqual(on[15], LL + len(ENABLE_MARKER))
        # blob grew by exactly the marker length
        self.assertEqual(len(on), len(off) + len(ENABLE_MARKER))

    def test_round_trip_on_then_off_restores_structure(self):
        off = make_off_blob()
        on = _set_enabled_in_blob(off, True)
        back = _set_enabled_in_blob(on, False)
        self.assertFalse(_is_enabled_from_blob(back))
        self.assertEqual(back[15], LL)            # length restored
        self.assertEqual(len(back), len(off))     # marker removed

    def test_idempotent_set_only_bumps_timestamp(self):
        off = make_off_blob()
        again = _set_enabled_in_blob(off, False)  # already off
        self.assertEqual(len(again), len(off))
        self.assertEqual(again[15], LL)
        # timestamp moved forward even though state was unchanged
        self.assertNotEqual(again[10:15], off[10:15])


class TimestampBumpTests(unittest.TestCase):
    def test_bumps_first_byte(self):
        b = bytearray([0] * 11)
        b[10] = 0x80
        _bump_timestamp(b)
        self.assertEqual(b[10], 0x81)

    def test_skips_0xff_byte(self):
        b = bytearray([0] * 12)
        b[10] = 0xFF
        b[11] = 0x10
        _bump_timestamp(b)
        self.assertEqual(b[10], 0xFF)  # untouched
        self.assertEqual(b[11], 0x11)  # bumped instead

    def test_wraps_at_0xff_when_in_range(self):
        # a non-0xFF byte at 0xFE bumps to 0xFF (no wrap needed)
        b = bytearray([0] * 11)
        b[10] = 0xFE
        _bump_timestamp(b)
        self.assertEqual(b[10], 0xFF)


if __name__ == "__main__":
    unittest.main()
