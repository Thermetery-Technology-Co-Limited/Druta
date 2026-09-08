# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fractional memory offsets survive the UI without changing integer knobs."""
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from druta.druta import Druta, KnobRange


class MemoryOffsetUiTests(unittest.TestCase):
    def setUp(self):
        self.app = Druta.__new__(Druta)
        self.app.gpu = SimpleNamespace(
            mem_offset_scale=lambda: (8, "MHz true"),
            set_clock_offset=Mock(return_value=(True, "set")),
            read=Mock(return_value={"mem_off": 100}),
            read_voltage_boost=lambda: None)
        self.app._knob_sync = False
        self.app._xoc_bounds = False
        self.app._carryover_hi = {}
        self.app._slider_ranges = {"mem": KnobRange("Memory", -250.125, 750.125,
                                                    -250.125, 750.125),
                                   "core": KnobRange("Core", -200, 300, -1000, 1000)}
        self.app.guard = Mock(return_value=True)
        self.app.report = Mock()
        self.app.log = Mock()
        self.values = {"sl_mem": 12.5, "in_mem": 0.0, "sl_core": 12, "in_core": 0}
        self.ui = MagicMock()
        self.ui.does_item_exist.side_effect = lambda tag: tag in self.values
        self.ui.get_value.side_effect = self.values.get
        self.ui.set_value.side_effect = self.values.__setitem__
        self.ui.is_item_focused.return_value = self.ui.is_item_active.return_value = False
        self.patch = patch("druta.druta.dpg", self.ui)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_profile_readback_mirrors_and_range_changes_preserve_12_point_5(self):
        self.values.update(sl_mem=0.0, in_mem=0.0)
        self.app.sync_sliders_from_gpu()
        self.assertEqual(self.values["sl_mem"], 12.5)
        self.assertEqual(self.values["in_mem"], 12.5)
        for xoc in (True, False):
            self.app.sync_slider_ranges(xoc)
            self.assertEqual(self.values["sl_mem"], 12.5)
            self.assertEqual(self.values["in_mem"], 12.5)
        self.app.apply_mem(self.values["sl_mem"])
        self.app.gpu.set_clock_offset.assert_called_once_with(2, 12.5)

    def test_drag_and_typed_input_snap_to_exact_wire_unit_in_displayed_scale(self):
        for value, expected in ((12.5, 12.5), (12.56, 12.5), (12.57, 12.625),
                                 (-12.57, -12.625), (0.125, 0.125)):
            for action in ("drag", "type"):
                with self.subTest(value=value, action=action):
                    if action == "drag":
                        self.values["sl_mem"] = value
                        self.app.knob_dragged("mem")
                    else:
                        self.values["in_mem"] = value
                        self.app.knob_typed("mem")
                    self.assertEqual(self.values["sl_mem"], expected)
                    self.assertEqual(self.values["in_mem"], expected)
                    self.assertEqual(expected * 8, int(expected * 8))

    def test_apply_snap_is_the_value_shown_by_both_widgets_and_sent(self):
        self.app.apply_mem(12.57)
        self.app.gpu.set_clock_offset.assert_called_once_with(2, 12.625)
        self.assertEqual(self.values["sl_mem"], 12.625)
        self.assertEqual(self.values["in_mem"], 12.625)

    def test_half_unit_unknown_gddr_and_quarter_unit_gddr5_are_preserved(self):
        for scale, value in ((2, 12.5), (4, 12.25), (8, 12.125)):
            with self.subTest(scale=scale):
                self.app.gpu.mem_offset_scale = lambda: (scale, "MHz")
                self.app.gpu.read.return_value = {"mem_off": value * scale}
                self.app.sync_sliders_from_gpu()
                self.assertEqual(self.values["sl_mem"], value)
                self.assertEqual(self.values["in_mem"], value)
                self.app.apply_mem(value)
                self.assertEqual(self.app.gpu.set_clock_offset.call_args.args, (2, value))

    def test_other_knobs_still_mirror_integers(self):
        self.values["sl_core"] = 13
        self.app.knob_dragged("core")
        self.assertEqual(self.values["in_core"], 13)
        self.assertIs(type(self.values["in_core"]), int)
        self.values["in_core"] = 17
        self.app.knob_typed("core")
        self.assertEqual(self.values["sl_core"], 17)
        self.assertIs(type(self.values["sl_core"]), int)

    def test_only_memory_row_uses_float_widgets(self):
        self.app._knob_cb = {}
        self.app._ctl_widgets = []
        self.app.bind = Mock()
        self.app.s = lambda value: value
        for key in ("mem", "core"):
            self.app.slider_row(key, key, -250.125 if key == "mem" else -200,
                                750.125 if key == "mem" else 300, 0, Mock())
        self.assertEqual(self.ui.add_slider_float.call_args.kwargs["tag"], "sl_mem")
        self.assertEqual(self.ui.add_input_float.call_args.kwargs["tag"], "in_mem")
        self.assertEqual(self.ui.add_slider_int.call_args.kwargs["tag"], "sl_core")
        self.assertEqual(self.ui.add_input_int.call_args.kwargs["tag"], "in_core")
        self.assertEqual(self.ui.add_slider_float.call_args.kwargs["max_value"], 750.125)

    def test_invalid_memory_input_never_reaches_backend(self):
        for value in (float("nan"), float("inf"), "invalid", True):
            with self.subTest(value=value):
                self.app.apply_mem(value)
        self.app.gpu.set_clock_offset.assert_not_called()

    def test_invalid_typed_value_leaves_last_valid_slider_in_place(self):
        self.values["in_mem"] = float("nan")
        self.app.knob_typed("mem")
        self.assertEqual(self.values["sl_mem"], 12.5)
        self.app.log.assert_called_once()


if __name__ == "__main__":
    unittest.main()
