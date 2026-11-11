# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Memory drags use whole MHz; typed precision and actual readback survive."""
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from druta.druta import Druta, KnobRange


class MemoryOffsetUiTests(unittest.TestCase):
    def setUp(self):
        self.app = Druta.__new__(Druta)
        self.app.write_tuning_action = lambda kind, value, writer, **kwargs: writer()
        self.app.gpu = SimpleNamespace(
            mem_offset_scale=lambda: (8, "MHz true"),
            set_clock_offset=Mock(return_value=(True, "set")),
            clock_offset_range=Mock(return_value=(-2000, 6000, 100)),
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

    def test_memory_drag_uses_whole_displayed_mhz_for_each_scale(self):
        for scale in (2, 4, 8, 16):
            self.app.gpu.mem_offset_scale = lambda: (scale, "MHz")
            for value, expected in ((12.49, 12.0), (13.49, 13.0), (12.57, 13.0),
                                    (-12.57, -13.0), (0.125, 0.0), (26.9375, 27.0)):
                with self.subTest(scale=scale, value=value):
                    self.values["sl_mem"] = value
                    self.app.knob_dragged("mem")
                    self.assertEqual(self.values["sl_mem"], expected)
                    self.assertEqual(self.values["in_mem"], expected)
                    self.app.gpu.set_clock_offset.assert_not_called()
        self.app.apply_mem(self.values["sl_mem"])
        self.app.gpu.set_clock_offset.assert_called_once_with(2, 27.0)

    def test_memory_drag_stays_within_fractional_active_bounds(self):
        self.app._slider_ranges["mem"] = KnobRange("Memory", -250.625, 750.625,
                                                   -300.625, 800.625)
        for xoc, endpoints in ((False, (-250.625, 750.625)), (True, (-300.625, 800.625))):
            self.app._xoc_bounds = xoc
            for value in endpoints:
                with self.subTest(xoc=xoc, value=value):
                    self.values["sl_mem"] = value
                    self.app.knob_dragged("mem")
                    self.assertEqual(self.values["sl_mem"], value)
                    self.assertEqual(self.values["in_mem"], value)

    def test_typed_memory_input_keeps_exact_wire_unit_in_displayed_scale(self):
        for value, expected in ((12.5, 12.5), (12.56, 12.5), (12.57, 12.625),
                                 (-12.57, -12.625), (0.125, 0.125)):
            with self.subTest(value=value):
                self.values["in_mem"] = value
                self.app.knob_typed("mem")
                self.assertEqual(self.values["sl_mem"], expected)
                self.assertEqual(self.values["in_mem"], expected)
                self.assertEqual(expected * 8, int(expected * 8))

    def test_typed_odd_memory_unit_is_preserved_through_apply(self):
        self.app.gpu.mem_offset_scale = lambda: (16, "MHz true")
        self.values["in_mem"] = 26.9375
        self.app.knob_typed("mem")
        self.app.apply_mem(self.values["sl_mem"])
        self.app.gpu.set_clock_offset.assert_called_once_with(2, 26.9375)
        self.assertEqual(self.values["sl_mem"], 26.9375)
        self.assertEqual(self.values["in_mem"], 26.9375)
        self.app.gpu.clock_offset_range.assert_not_called()

    def test_failed_apply_mirrors_actual_offset_without_retrying_or_hiding_failure(self):
        self.app.gpu.mem_offset_scale = lambda: (16, "MHz true")
        failure = (False, "mem offset requested +26.9375 MHz true, but the driver reported +26.875 MHz true")
        self.app.gpu.set_clock_offset.return_value = failure
        self.app.gpu.clock_offset_range.return_value = (-2000, 6000, 430)
        self.app.apply_mem(26.9375)
        self.app.gpu.set_clock_offset.assert_called_once_with(2, 26.9375)
        self.app.gpu.clock_offset_range.assert_called_once_with(2)
        self.assertEqual(self.values["sl_mem"], 26.875)
        self.assertEqual(self.values["in_mem"], 26.875)
        self.app.report.assert_called_once_with(failure)

    def test_failed_apply_keeps_staged_value_when_readback_is_unavailable(self):
        failure = (False, "memory write failed; Undo snapshot retained")
        self.app.gpu.set_clock_offset.return_value = failure
        for readback in (None, (), (-2000, 6000, None), (-2000, 6000, True),
                         (-2000, 6000, float("nan")), (-2000, 6000, float("inf")),
                         RuntimeError("driver unavailable")):
            with self.subTest(readback=readback):
                self.app.gpu.set_clock_offset.reset_mock()
                self.app.gpu.clock_offset_range = Mock(
                    side_effect=readback if isinstance(readback, Exception) else None,
                    return_value=readback)
                self.app.apply_mem(12.625)
                self.assertEqual(self.values["sl_mem"], 12.625)
                self.assertEqual(self.values["in_mem"], 12.625)
                self.app.gpu.set_clock_offset.assert_called_once_with(2, 12.625)
                self.app.gpu.clock_offset_range.assert_called_once_with(2)
                ok, message = self.app.report.call_args.args[0]
                self.assertFalse(ok)
                self.assertIn(failure[1], message)
                self.assertIn("readback unavailable", message)
                self.assertIn("staged value retained", message)

    def test_failed_apply_keeps_staged_value_when_readback_scale_is_invalid(self):
        self.app.gpu.set_clock_offset.return_value = (False, "rejected")
        for scale in (None, True, 0, float("nan"), float("inf")):
            with self.subTest(scale=scale):
                self.app.gpu.mem_offset_scale = Mock(side_effect=[(8, "MHz"), (scale, "MHz")])
                self.app.apply_mem(12.625)
                self.assertEqual(self.values["sl_mem"], 12.625)
                self.assertEqual(self.values["in_mem"], 12.625)
                self.assertIn("readback unavailable", self.app.report.call_args.args[0][1])

    def test_apply_snap_is_the_value_shown_by_both_widgets_and_sent(self):
        self.app.apply_mem(12.57)
        self.app.gpu.set_clock_offset.assert_called_once_with(2, 12.625)
        self.assertEqual(self.values["sl_mem"], 12.625)
        self.assertEqual(self.values["in_mem"], 12.625)

    def test_half_unit_unknown_gddr_and_quarter_unit_gddr5_are_preserved(self):
        for scale, value in ((2, 12.5), (4, 12.25), (8, 12.125), (16, 12.0625)):
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

    def test_other_float_knobs_keep_fractional_drag_precision(self):
        self.values.update(sl_rail=12.3456, in_rail=0.0)
        self.app.knob_dragged("rail")
        self.assertEqual(self.values["sl_rail"], 12.346)
        self.assertEqual(self.values["in_rail"], 12.346)

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
