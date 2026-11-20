# Druta - a monitor and tuner for NVIDIA GPUs.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native power-arbitration decoder against power_arbitration.decode.

Every case is built with the packet/vector builders of tests/test_power_arbitration.py and run
through both decoders; the reports must be equal, including key order and value types (True is
not 1). tests/test_power_arbitration_integration.py has no pure decoder fixture (it mocks decode
behind GPU.read_power_arbitration), so the legacy layouts come from nvbackend's own tables instead.
"""
import copy
import random
import struct
import unittest

from druta import native, native_power_arbitration as npa, power_arbitration as pa
from druta.nvbackend import GPU

if not native.available() or not npa.available():
    raise unittest.SkipTest("native core not built (native/build.sh linux)")

from tests.test_power_arbitration import CONTROL, INFO, STATUS, packets, vector  # noqa: E402

UINT32_MAX = (1 << 32) - 1


def info_word(index):
    return (0xCC + index * 0x104) // 4


def status_word(index):
    return (0x9C + index * 0x1730) // 4


def control_word(index):
    return (0x14 + index * 0xC4) // 4


def ordered(value):
    """The value with dict key order and leaf types made part of equality."""
    if isinstance(value, dict):
        return [(key, ordered(item)) for key, item in value.items()]
    if isinstance(value, list):
        return [ordered(item) for item in value]
    return (type(value).__name__, value)


def python_geometry(layout):
    try:
        pa._geometry(layout)
    except pa._PacketError as exc:
        return str(exc)
    return None


def all_table_layouts():
    for key, layout in GPU._CURRENT_LIMIT_LAYOUTS.items():
        yield key, layout
    for key, fallbacks in GPU._CURRENT_LIMIT_LAYOUT_FALLBACKS.items():
        for layout in fallbacks:
            yield key, layout


def snapshot(args):
    """A copy of the arguments: packets are flat lists of immutable items, so a shallow copy is deep."""
    return [list(item) if isinstance(item, list) else copy.deepcopy(item) for item in args]


class Parity(unittest.TestCase):
    def check(self, args):
        args = list(args)
        before = snapshot(args)
        expected = pa.decode(*snapshot(args))
        actual = npa.decode(*args)
        self.assertEqual(args, before, "the native decoder changed its input")
        self.assertEqual(actual, expected)
        self.assertEqual(ordered(actual), ordered(expected))
        layout_reason = python_geometry(args[0])
        self.assertEqual(npa.geometry(args[0]), layout_reason)
        return expected


class RowFormats(Parity):
    def test_row_formats_match(self):
        self.assertEqual(npa.row_formats(), pa.ROW_FORMATS)

    def test_v3_and_v4_rows_decode_identically(self):
        for version in (0x30, 0x40):
            with self.subTest(version=version):
                args = packets((5,))
                i, s, c = info_word(5), status_word(5), control_word(5)
                args[1][i], args[1][i + 1] = version, 3 | (4 << 8)
                args[2][c:c + 2] = [3, 378000]
                args[3][s:s + 3] = [3, 396918, 21561]
                vector(args[3], 5, 0x40, [(254, 378000)], output=378000)
                vector(args[3], 5, 0xE0, [(254, 378000)], output=378000)
                report = self.check(args)
                self.assertTrue(report["policies"][0]["available"])

    def test_unknown_row_versions_remain_unavailable(self):
        for version in (0x00, 0x20, 0x31, 0x41, 0x50, 0x130, 0xFFFFFF40):
            with self.subTest(version=version):
                args = packets()
                args[1][info_word(9)] = version
                report = self.check(args)
                self.assertEqual(report["policies"][0]["available"], version & 255 in pa.ROW_FORMATS)


class Rows(Parity):
    def test_saved_input_current_layers(self):
        report = self.check(packets())
        self.assertEqual(len(report["policies"][0]["observations"]), 1)

    def test_effective_differs_from_pmu(self):
        args = packets((2,))
        args[3][status_word(2) + 1] = 370811
        vector(args[3], 2, 0xE0, [(254, 450000), (8, 360000)], output=360000, initial=450000)
        self.check(args)

    def test_signed_delta(self):
        for delta in (-2147483648, -125, -1, 0, 1, 125, 2147483647):
            with self.subTest(delta=delta):
                args = packets()
                args[3][status_word(9) + 3] = delta & UINT32_MAX
                self.assertEqual(self.check(args)["policies"][0]["delta"], delta)

    def test_alternate_indices_classes_channels_and_units(self):
        for index, kind, channel, unit in ((0, 17, 200, 0), (31, 18, 44, 1), (8, 253, 255, 4), (16, 0, 0, 255)):
            with self.subTest(index=index, unit=unit):
                args = packets((index,))
                i, s, c = info_word(index), status_word(index), control_word(index)
                args[1][i + 1] = kind | (channel << 8) | (unit << 16) | (0xA5 << 24)
                args[2][c] = kind
                args[3][s] = kind | (0xBB << 24)
                self.check(args)

    def test_each_method_and_uint32_sum(self):
        cases = ((0, [9, 3, 8], 3), (1, [9, 3, 8], 9), (2, [9, 3, 8], 20), (2, [0xFFFFFFFF, 2], 1),
                 (0, [7], 6), (2, [UINT32_MAX] * 8, 0))
        for method, values, output in cases:
            for offset in (0x40, 0xE0):
                with self.subTest(method=method, values=values, offset=offset):
                    args = packets()
                    vector(args[3], 9, offset, list(enumerate(values)), method=method, output=output)
                    self.check(args)

    def test_zero_inputs_claim_no_computed_output(self):
        args = packets()
        vector(args[3], 9, 0x40, [], output=456, initial=789)
        vector(args[3], 9, 0xE0, [], method=2, output=0, initial=0)
        self.check(args)

    def test_eight_inputs_and_ignored_padding(self):
        args = packets()
        vector(args[3], 9, 0xE0, [(index, 80 - index) for index in range(8)], output=73)
        word = status_word(9) + 0xE0 // 4
        args[3][word] |= 0xFEEF0000
        args[3][word + 19] = 0xDEADBEEF
        self.check(args)

    def test_bad_vectors_preserve_their_peer(self):
        for offset in (0x40, 0xE0):
            for bad in ("method3", "method255", "count9", "count255", "duplicate"):
                with self.subTest(offset=offset, bad=bad):
                    args = packets()
                    word = status_word(9) + offset // 4
                    if bad.startswith("method"):
                        args[3][word] = (args[3][word] & ~255) | int(bad[6:])
                    elif bad.startswith("count"):
                        args[3][word] = (args[3][word] & ~0xFF00) | int(bad[5:]) << 8
                    else:
                        vector(args[3], 9, offset, [(254, 1), (254, 2)], output=1)
                    row = self.check(args)["policies"][0]
                    self.assertFalse(row["host" if offset == 0x40 else "pmu"]["available"])

    def test_both_vectors_bad(self):
        args = packets()
        for offset in (0x40, 0xE0):
            word = status_word(9) + offset // 4
            args[3][word] = (args[3][word] & ~255) | 7
        self.check(args)

    def test_inconsistent_outputs_and_every_observation(self):
        args = packets()
        vector(args[3], 9, 0x40, [(254, 1), (12, 2)], output=999)
        vector(args[3], 9, 0xE0, [(254, 5), (12, 6)], output=998)
        args[3][status_word(9) + 3] = 1
        report = self.check(args)
        self.assertEqual(len(report["policies"][0]["observations"]), 6)

    def test_wrong_row_identity_does_not_hide_other_rows(self):
        for block in ("info_version", "control_type", "control_high_bits", "status_type"):
            with self.subTest(block=block):
                args = packets((4, 29))
                if block == "info_version":
                    args[1][info_word(4)] = 0x50
                elif block == "control_type":
                    args[2][control_word(4)] = 17
                elif block == "control_high_bits":
                    args[2][control_word(4)] = 30 | 1 << 8  # CONTROL compares the whole word
                else:
                    args[3][status_word(4)] = 18
                self.check(args)

    def test_every_policy_bit(self):
        # A 32-bit mask cannot exceed the understood capacity of 32 rows, so the capacity refusal
        # is unreachable through a validated INFO word; bit 31 is the boundary that must decode.
        report = self.check(packets(tuple(range(32))))
        self.assertEqual(report["mask"], UINT32_MAX)
        self.assertEqual(len(report["policies"]), 32)

    def test_mixed_rows(self):
        args = packets((0, 4, 9, 31))
        args[1][info_word(0)] = 0x31
        args[3][status_word(31)] = 3
        vector(args[3], 9, 0xE0, [(1, 5), (1, 6)])
        self.check(args)

    def test_random_mutations(self):
        rng = random.Random(0xA618)
        for case in range(80):
            with self.subTest(case=case):
                indices = tuple(sorted(rng.sample(range(32), rng.randint(1, 4))))
                args = packets(indices)
                for _ in range(rng.randint(1, 6)):
                    index = rng.choice(indices)
                    which = rng.choice(("info", "control", "status", "prefix"))
                    if which == "info":
                        word, packet = info_word(index) + rng.randint(0, 1), args[1]
                    elif which == "control":
                        word, packet = control_word(index) + rng.randint(0, 1), args[2]
                    elif which == "status":
                        word, packet = status_word(index) + rng.randint(0, 3), args[3]
                    else:
                        word, packet = status_word(index) + rng.randint(16, 75), args[3]
                    flipped = packet[word] ^ 1 << rng.randint(0, 31)
                    packet[word] = rng.choice((rng.getrandbits(32), rng.getrandbits(8), flipped))
                self.check(args)


class Vector(unittest.TestCase):
    def test_vector_refusals_and_truncation(self):
        whole = bytearray(0x130)
        whole[0x40:0x42] = bytes((1, 2))
        struct.pack_into("<IIBxxxIBxxxI", whole, 0x44, 9, 8, 3, 7, 4, 9)
        cases = [(bytes(whole), 0x40), (bytes(whole), 0xE0), (bytes(whole), 0xE1), (bytes(whole), 0x130),
                 (bytes(whole), 0x1000), (b"", 0), (bytes(0x4F), 0), (bytes(0x50), 0), (bytes((3,)) + bytes(0x4F), 0),
                 (bytes((0, 9)) + bytes(0x4E), 0)]
        for row, offset in cases:
            with self.subTest(size=len(row), offset=offset):
                expected = pa._arbitration(row, offset)
                actual = npa.arbitration(row, offset)
                self.assertEqual(actual, expected)
                self.assertEqual(ordered(actual), ordered(expected))


class Packets(Parity):
    def test_untraced_geometries(self):
        mutations = [
            lambda layout: layout.update(info=(0xCC, 0xFC)),
            lambda layout: layout.update(status=(0x9C, 0x1720)),
            lambda layout: layout.update(control=(0x14, 0xB8)),
            lambda layout: layout["sizes"].update({INFO: 20000}),
            lambda layout: layout["sizes"].update({STATUS: 397044}),
            lambda layout: layout["sizes"].update({CONTROL: 13880}),
            lambda layout: layout["sizes"].update({CONTROL: True}),
            lambda layout: layout["sizes"].update({CONTROL: 13876.0}),
            lambda layout: layout["sizes"].pop(STATUS),
            lambda layout: layout.update(status_mask_word=0),
            lambda layout: layout.update(status_mask_word=True),
            lambda layout: layout.update(status_mask_word=1.0),
            lambda layout: layout.update(status_mask_word=None),
            lambda layout: layout.update(status_mask_word=-1),
            lambda layout: layout.update(status_mask_word=1 << 32 | 1),
            lambda layout: layout.update(status_mask_word=1),
            lambda layout: layout.update(commands={INFO: 0x20802618}),
            lambda layout: layout.update(commands={str(CONTROL): 0x2080261A}),
            lambda layout: layout.update(commands={INFO: INFO, STATUS: STATUS, CONTROL: CONTROL}),
            lambda layout: layout.update(commands={str(INFO): INFO, 0x2080E61B: 5}),
            lambda layout: layout.update(commands={STATUS: float(STATUS)}),
            lambda layout: layout.update(commands=[INFO]),
            lambda layout: layout.update(commands=None),
            lambda layout: layout.update(info=[0xCC, 0x104]),
            lambda layout: layout.update(info=(0xCC, 0x104, 0)),
            lambda layout: layout.update(info=(0xCC, True)),
            lambda layout: layout.update(control=(20.0, 0xC4)),
            lambda layout: layout.update(status=(0x9C - (1 << 32), 0x1730)),
            lambda layout: layout.update(status="ab"),
            lambda layout: layout.pop("control"),
            # Both geometry and size wrong: the earlier GET's refusal wins.
            lambda layout: (layout.update(status=(0, 0)), layout["sizes"].update({INFO: 1})),
            lambda layout: (layout.update(info=(0, 0)), layout.update(status_mask_word=0)),
        ]
        for number, mutate in enumerate(mutations):
            with self.subTest(mutation=number):
                args = packets()
                mutate(args[0])
                self.check(args)

    def test_missing_layouts(self):
        for layout in (None, [], {"sizes": []}, {"sizes": {}}, {"sizes": None}, "sizes", {}):
            with self.subTest(layout=layout):
                self.check([layout, None, None, None])
                self.check([layout, *packets()[1:]])

    def test_legacy_table_layouts(self):
        for key, layout in all_table_layouts():
            with self.subTest(key=key, layout=layout):
                _, info, control, status = packets()
                report = self.check([copy.deepcopy(layout), info, control, status])
                understood = layout["info"] == (0xCC, 0x104) and layout.get("status_mask_word", 1) == 1
                if not understood:
                    self.assertFalse(report["available"])

    def test_words_are_strict_unsigned_ints(self):
        for which in (1, 2, 3):
            for bad in (-1, 1 << 32, 1.0, True, "1", None):
                for position in (0, 1, -1):
                    with self.subTest(which=which, bad=bad, position=position):
                        args = packets()
                        args[which][position] = bad
                        self.check(args)

    def test_packet_lengths_and_types(self):
        for which in (1, 2, 3):
            for shape in ("short", "long", "empty", "tuple", "str", "bytes", "dict", "int", "none", "short_bad"):
                with self.subTest(which=which, shape=shape):
                    args = list(packets())
                    words = args[which]
                    args[which] = {
                        "short": words[:-1], "long": words + [0], "empty": [], "tuple": tuple(words),
                        "str": "x" * len(words), "bytes": bytes(len(words)), "dict": dict(enumerate(words)),
                        "int": 7, "none": None, "short_bad": words[:-2] + [-1],
                    }[shape]
                    self.check(args)

    def test_length_refused_before_value_and_in_packet_order(self):
        args = list(packets())
        args[2] = args[2][:-1]
        args[3][-1] = -1
        self.check(args)
        args = list(packets())
        args[1][-1] = 1.5
        args[2] = None
        self.check(args)

    def test_masks_and_control_header_must_agree(self):
        for which, offset in ((3, 1), (2, 4), (2, 3), (2, 0), (2, 1), (2, 2)):
            with self.subTest(which=which, offset=offset):
                args = packets()
                args[which][offset] ^= 1
                self.check(args)

    def test_empty_mask(self):
        _, info, control, status = packets(())
        layout = packets(())[0]
        for pair in ((None, None), (control, status), (None, status), (control, None)):
            with self.subTest(control=pair[0] is None, status=pair[1] is None):
                self.check([copy.deepcopy(layout), info, *pair])
        bad = list(control)
        bad[3] = 0
        self.check([copy.deepcopy(layout), info, bad, None])

    def test_nonempty_mask_cannot_skip_packets(self):
        args = list(packets())
        for pair in ((None, None), (args[2], None), (None, args[3])):
            with self.subTest(control=pair[0] is None, status=pair[1] is None):
                self.check([args[0], args[1], *pair])

    def test_json_layout_keys(self):
        args = packets((0, 31))
        args[0]["sizes"] = {str(key): value for key, value in args[0]["sizes"].items()}
        args[0]["info"] = list(args[0]["info"])
        self.assertTrue(self.check(args)["available"])

    def test_int_key_wins_over_str_key(self):
        args = packets()
        args[0]["sizes"][str(INFO)] = 1
        self.check(args)
        args[0]["sizes"][INFO], args[0]["sizes"][str(INFO)] = 1, 20512
        self.check(args)


if __name__ == "__main__":
    unittest.main()
