# Druta - hardware-free tests: a memory-type scale change must not change what
# a saved profile writes.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later

"""Profiles store the memory offset twice: the driver units the card held and
those units divided by the memory-type scale of the build that saved them.
Naming RAM type 15 as GDDR6X moved its scale from 2 to 16, so a profile, undo
point or sign-in profile saved before that would replay 8x its offset if the
true-MHz figure were used. These tests run the real GPU.mem_offset_scale and
set_clock_offset against a fake NVML offset driver."""

import ctypes
import json
import threading
import unittest
from types import SimpleNamespace

from druta import nvbackend as n
from druta import profiles


class FakeNvmlOffsets:
    """nvmlDeviceGet/SetClockOffsets with a declared memory range (NVML units)."""

    def __init__(self, lo, hi):
        self.lo, self.hi, self.current = lo, hi, 0
        self.writes = []

    def read(self, handle, pointer):
        co = ctypes.cast(pointer, ctypes.POINTER(n._ClockOffset)).contents
        co.mn, co.mx, co.off = self.lo, self.hi, self.current
        return 0

    def write(self, handle, pointer):
        co = ctypes.cast(pointer, ctypes.POINTER(n._ClockOffset)).contents
        self.writes.append((co.type, co.off))
        self.current = co.off
        return 0


def gpu_with(mem_div, lo=-2000, hi=6000, uuid="GPU-TEST", mem_type_id=None):
    """A real GPU object (no __init__, nothing opened) on a fake NVML, exposed
    through only what profiles.capture/preflight/restore use."""
    g = n.GPU.__new__(n.GPU)
    g._lock = threading.RLock()
    g.nvapi = SimpleNamespace(ok=False)
    driver = FakeNvmlOffsets(lo, hi)
    g.nvml = SimpleNamespace(
        ok=True, dev=object(), ver=n.Nvml.ver,
        has=lambda name: name in ("nvmlDeviceGetClockOffsets", "nvmlDeviceSetClockOffsets"),
        dll=SimpleNamespace(nvmlDeviceGetClockOffsets=driver.read,
                            nvmlDeviceSetClockOffsets=driver.write),
        errstr=lambda status: f"status {status}")
    g.static = {"mem_div": mem_div, "name": "test card", "uuid": uuid,
                "vbios": "rom", "driver": "0.0", "mem_type_id": mem_type_id}
    facade = SimpleNamespace(
        static=g.static,
        read=lambda: {"mem_off": driver.current},
        mem_offset_scale=g.mem_offset_scale,
        memory_offset_step_units=g.memory_offset_step_units,
        set_clock_offset=g.set_clock_offset,
        vf_curve_applicable=lambda: False,
        read_voltage_boost=lambda: None)
    return facade, driver


def saved_on(mem_div, units, **card):
    """A profile captured by a build whose scale for this card is 2 * mem_div."""
    gpu, driver = gpu_with(mem_div, **card)
    driver.current = units
    return json.loads(json.dumps(profiles.capture(gpu)))


class MemoryScaleChangeTests(unittest.TestCase):
    def test_profile_saved_before_gddr6x_was_named_writes_the_same_units(self):
        # RAM type 15 before PR #30: no MEM_TYPES entry, scale 2
        state = saved_on(None, 200)
        self.assertEqual((state["mem_off_units"], state["mem_off_scale"]), (200, 2))
        gpu, driver = gpu_with(n.MEM_TYPES[15][1])          # GDDR6X, scale 16
        self.assertIsNone(profiles.preflight(gpu, state))
        results = profiles.restore(gpu, state)
        self.assertTrue(all(ok for ok, _ in results), results)
        self.assertEqual(driver.writes, [(2, 200)])           # not 8 x 200

    def test_an_offset_that_8x_would_push_out_of_range_still_restores(self):
        state = saved_on(None, 1000)
        gpu, driver = gpu_with(8, lo=-1000, hi=1500)
        results = profiles.restore(gpu, state)
        self.assertTrue(all(ok for ok, _ in results), results)
        self.assertEqual(driver.writes, [(2, 1000)])

    def test_the_other_direction_writes_the_same_units_too(self):
        state = saved_on(8, 1600)                             # saved at scale 16
        gpu, driver = gpu_with(None)                          # replayed at scale 2
        self.assertTrue(all(ok for ok, _ in profiles.restore(gpu, state)))
        self.assertEqual(driver.writes, [(2, 1600)])

    def test_same_scale_round_trips_are_unchanged(self):
        for div, units in ((4, 800), (4, -400), (2, 300), (None, 250)):
            with self.subTest(div=div, units=units):
                state = saved_on(div, units)
                gpu, driver = gpu_with(div)
                self.assertTrue(all(ok for ok, _ in profiles.restore(gpu, state)))
                self.assertEqual(driver.writes, [(2, units)])

    def test_a_file_without_units_falls_back_to_true_mhz(self):
        state = saved_on(4, 800)
        del state["mem_off_units"]                            # older file shape
        gpu, driver = gpu_with(4)
        self.assertTrue(all(ok for ok, _ in profiles.restore(gpu, state)))
        self.assertEqual(driver.writes, [(2, 800)])

    def test_units_and_mhz_that_disagree_are_refused_before_any_write(self):
        state = saved_on(None, 200)
        state["mem_off_true_mhz"] = 400                       # altered: 400 x 2 != 200
        gpu, driver = gpu_with(8)
        self.assertIn("disagree", profiles.preflight(gpu, state))
        profiles.restore(gpu, state)
        self.assertEqual(driver.writes, [])

    def test_new_profiles_record_the_ram_type(self):
        state = saved_on(8, 1600, mem_type_id=15)
        self.assertEqual(state["device"]["mem_type_id"], 15)

    def test_unusable_saved_units_are_refused_before_any_write(self):
        for bad in ("200", True, float("nan"), 12.5):
            with self.subTest(bad=bad):
                state = saved_on(None, 200)
                state["mem_off_units"] = bad
                gpu, driver = gpu_with(8)
                self.assertIsNotNone(profiles.preflight(gpu, state))
                profiles.restore(gpu, state)
                self.assertEqual(driver.writes, [])


class CrossCardMemoryTests(unittest.TestCase):
    """A profile deliberately loaded on another card keeps the true-MHz figure
    its row shows whenever the memory is (or may be) a different type."""

    def test_different_ram_type_replays_the_true_mhz_the_row_shows(self):
        state = saved_on(4, 4000, uuid="GPU-A", mem_type_id=10)    # GDDR5X, +500 MHz true
        self.assertIn("mem +500 MHz", profiles.summarize(state))
        gpu, driver = gpu_with(2, uuid="GPU-B", mem_type_id=8)     # GDDR5, scale 4
        self.assertTrue(all(ok for ok, _ in profiles.restore(gpu, state)))
        self.assertEqual(driver.writes, [(2, 2000)])               # +500 true, not raw 4000

    def test_older_file_from_another_card_keeps_the_true_mhz_behaviour(self):
        state = saved_on(8, 1600, uuid="GPU-A")                    # no RAM type recorded
        del state["device"]["mem_type_id"]
        gpu, driver = gpu_with(4, uuid="GPU-B", mem_type_id=14)
        self.assertTrue(all(ok for ok, _ in profiles.restore(gpu, state)))
        self.assertEqual(driver.writes, [(2, 800)])                # 100 true x 8

    def test_another_card_with_the_same_ram_type_replays_the_units(self):
        # the same memory type means the same units, whatever scale either
        # build gave that type when it saved or loads the file
        state = saved_on(None, 200, uuid="GPU-A", mem_type_id=15)
        gpu, driver = gpu_with(8, uuid="GPU-B", mem_type_id=15)
        self.assertTrue(all(ok for ok, _ in profiles.restore(gpu, state)))
        self.assertEqual(driver.writes, [(2, 200)])


if __name__ == "__main__":
    unittest.main()
