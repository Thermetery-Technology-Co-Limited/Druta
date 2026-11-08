# Druta - a monitor and tuner for NVIDIA GPUs.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native current-limit tables against nvbackend.GPU's.

Every power-GET key in _CURRENT_LIMIT_LAYOUTS and every architecture (known,
unknown and absent) is looked up on both sides and compared whole with ``==``,
including the order of the layout and its fallbacks and of the policies."""
import ctypes
import unittest

from druta import native, native_power_layouts as npl
from druta.native import DC_E_ARGUMENT, DC_OK
from druta.nvbackend import GPU

if not native.available() or not npl.available():
    raise unittest.SkipTest("native core not built (native/build.sh linux)")

ARCHES = (None, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 2**32 - 1)
FALLBACK_KEY = (0x2080A612, 54420, 54352)


def python_layouts(key):
    return (GPU._CURRENT_LIMIT_LAYOUTS[key], *GPU._CURRENT_LIMIT_LAYOUT_FALLBACKS.get(key, ()))


def raw_lookup(key, rows, capacity):
    count = ctypes.c_uint32(0xDEADBEEF)
    status = npl.load().dc_power_layouts_lookup(*key, rows, capacity, ctypes.byref(count))
    return status, count.value


def layout_rows(n):
    rows = (npl._PowerLayout * n)()
    for row in rows:
        row.size = ctypes.sizeof(npl._PowerLayout)
    return rows


class TransportLayouts(unittest.TestCase):
    def test_every_key_matches_python(self):
        self.assertTrue(GPU._CURRENT_LIMIT_LAYOUTS)
        for key in GPU._CURRENT_LIMIT_LAYOUTS:
            with self.subTest(key=tuple(hex(k) for k in key)):
                got = npl.transport_layouts(*key)
                self.assertEqual(got, python_layouts(key))
                # Dict key order and the optional keys follow the Python dicts too.
                self.assertEqual([list(d) for d in got], [list(d) for d in python_layouts(key)])

    def test_fallbacks_follow_the_layout_in_python_order(self):
        got = npl.transport_layouts(*FALLBACK_KEY)
        self.assertEqual(len(got), 3)
        self.assertEqual([d["sizes"][0x2080A618] for d in got], [20000, 8632, 20512])

    def test_every_fallback_key_has_a_layout(self):
        for key in GPU._CURRENT_LIMIT_LAYOUT_FALLBACKS:
            self.assertIn(key, GPU._CURRENT_LIMIT_LAYOUTS)

    def test_unknown_keys(self):
        for key in ((0, 0, 0), (0x2080A612, 54420, 54353), (0x2080A612, 54421, 54352),
                    (0x20802612, 11748, 11680), (0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF)):
            with self.subTest(key=key):
                self.assertNotIn(key, GPU._CURRENT_LIMIT_LAYOUTS)
                self.assertEqual(npl.transport_layouts(*key), ())

    def test_right_geometry_wrong_command(self):
        self.assertIn((0x20802612, 2524, 2456), GPU._CURRENT_LIMIT_LAYOUTS)
        self.assertNotIn((0x2080A612, 2524, 2456), GPU._CURRENT_LIMIT_LAYOUTS)
        self.assertEqual(npl.transport_layouts(0x2080A612, 2524, 2456), ())

    def test_command_is_part_of_the_key(self):
        under_2612 = npl.transport_layouts(0x20802612, 3548, 3480)
        under_a612 = npl.transport_layouts(0x2080A612, 3548, 3480)
        self.assertTrue(under_2612 and under_a612)
        self.assertNotEqual(under_2612, under_a612)
        self.assertIn("commands", under_2612[0])
        self.assertNotIn("commands", under_a612[0])
        self.assertNotEqual(under_2612[0]["info"], under_a612[0]["info"])

    def test_values_python_cannot_key_give_nothing(self):
        for key in ((-1, 2524, 2456), (0x20802612, 2**32 + 2524, 2456), (0x20802612, 2524.0, 2456),
                    (None, 2524, 2456)):
            with self.subTest(key=key):
                self.assertEqual(npl.transport_layouts(*key), ())


class RawAbi(unittest.TestCase):
    def test_capacity_zero_reports_the_full_count(self):
        self.assertEqual(raw_lookup(FALLBACK_KEY, None, 0), (DC_OK, 3))
        self.assertEqual(raw_lookup((0, 0, 0), None, 0), (DC_OK, 0))

    def test_capacity_one_writes_one_row(self):
        rows = layout_rows(2)
        self.assertEqual(raw_lookup(FALLBACK_KEY, rows, 1), (DC_OK, 3))
        self.assertEqual(npl._layout_dict(rows[0]), GPU._CURRENT_LIMIT_LAYOUTS[FALLBACK_KEY])
        self.assertEqual(list(rows[1].sizes), [0, 0, 0, 0])  # the second row is untouched

    def test_wrong_row_size_is_refused(self):
        rows = layout_rows(3)
        rows[1].size = ctypes.sizeof(npl._PowerLayout) - 4
        self.assertEqual(raw_lookup(FALLBACK_KEY, rows, 3)[0], DC_E_ARGUMENT)
        rows = layout_rows(1)
        rows[0].size = 0
        self.assertEqual(raw_lookup(FALLBACK_KEY, rows, 1)[0], DC_E_ARGUMENT)

    def test_size_of_unwritten_rows_is_not_checked(self):
        rows = layout_rows(3)
        rows[2].size = 0
        self.assertEqual(raw_lookup((0x20802612, 2524, 2456), rows, 3), (DC_OK, 1))

    def test_null_arguments(self):
        library = npl.load()
        count = ctypes.c_uint32()
        self.assertEqual(library.dc_power_layouts_lookup(*FALLBACK_KEY, layout_rows(1), 1, None), DC_E_ARGUMENT)
        self.assertEqual(library.dc_power_layouts_lookup(*FALLBACK_KEY, None, 1, ctypes.byref(count)),
                         DC_E_ARGUMENT)
        self.assertEqual(library.dc_generation_policies(1, 10, None, 0, None), DC_E_ARGUMENT)
        self.assertEqual(library.dc_generation_policies(1, 10, None, 1, ctypes.byref(count)), DC_E_ARGUMENT)

    def test_policy_capacity(self):
        library = npl.load()
        count = ctypes.c_uint32()
        self.assertEqual(library.dc_generation_policies(1, 10, None, 0, ctypes.byref(count)), DC_OK)
        self.assertEqual(count.value, 2)
        rows = (npl._GenerationPolicy * 2)()
        self.assertEqual(library.dc_generation_policies(1, 10, rows, 1, ctypes.byref(count)), DC_OK)
        self.assertEqual((count.value, rows[0].policy, rows[1].policy), (2, 13, 0))
        self.assertEqual(library.dc_generation_policies(0, 10, rows, 2, ctypes.byref(count)), DC_OK)
        self.assertEqual(count.value, 0)  # has_arch = 0 is None, whatever arch says


class GenerationPolicies(unittest.TestCase):
    def test_every_arch_matches_python(self):
        for arch in ARCHES:
            with self.subTest(arch=arch):
                expected = GPU.CURRENT_LIMIT_GENERATION_POLICIES.get(arch, {})
                got = npl.generation_policies(arch)
                self.assertEqual(got, expected)
                self.assertEqual(list(got), list(expected))

    def test_every_python_arch_is_covered(self):
        for arch in GPU.CURRENT_LIMIT_GENERATION_POLICIES:
            self.assertIn(arch, ARCHES)

    def test_blackwell_policy_order(self):
        self.assertEqual(list(npl.generation_policies(10)), [13, 14])


if __name__ == "__main__":
    unittest.main()
