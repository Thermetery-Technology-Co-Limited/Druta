# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Original BIOS provenance feeds the existing power knob's visual state."""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from druta.druta import TEXT, WARN, Druta, KnobRange


class PowerSliderUiTests(unittest.TestCase):
    def setUp(self):
        self.app = Druta.__new__(Druta)
        self.app.gpu = SimpleNamespace(static={"pl_min_mw": 250000, "pl_def_mw": 360000, "pl_max_mw": 999000})
        self.app.s = lambda value: value
        self.app.log = Mock()
        self.app.note_gated_knob = Mock()
        self.app._knob_sync = False
        self.app._xoc_bounds = False
        self.app._carryover_hi = {}
        self.app._slider_ranges = {"pl": KnobRange("Power", 250, 999, None, None)}
        self.values = {"sl_pl": 360.0, "in_pl": 360.0}
        self.ui = MagicMock()
        self.ui.does_item_exist.side_effect = lambda key: key in self.values
        self.ui.get_value.side_effect = self.values.get
        self.ui.set_value.side_effect = self.values.__setitem__
        self.ui.is_item_focused.return_value = False
        self.ui.is_item_active.return_value = False
        self.start_patch(patch("druta.druta.dpg", self.ui))

    def start_patch(self, item):
        result = item.start()
        self.addCleanup(item.stop)
        return result

    def setup_style(self, baseline):
        reader = self.start_patch(patch("druta.power_range_baseline.read", return_value=baseline))
        factory = self.start_patch(patch("druta.power_slider_style.PowerSliderStyle"))
        self.app.setup_power_slider_style()
        reader.assert_called_once_with(self.app.gpu)
        return factory

    def test_original_range_not_current_ceiling_or_default_is_supplied_in_watts(self):
        factory = self.setup_style(
            {"minimum_mw": 250000, "maximum_mw": 450000, "source": "receipt-original", "reason": None}
        )
        factory.assert_called_once_with(
            slider="sl_pl", input_item="in_pl", original_bounds=(250, 450), amber=WARN, normal_text=TEXT
        )
        message = self.ui.add_text.call_args.args[0]
        self.assertIn("250..450 W", message)
        self.assertNotIn("999", message)
        self.assertNotIn("360", message)

    def test_alternate_board_range_is_not_replaced_by_tested_board_numbers(self):
        factory = self.setup_style(
            {"minimum_mw": 180001, "maximum_mw": 240125, "source": "receipt-original", "reason": None}
        )
        self.assertEqual(factory.call_args.kwargs["original_bounds"], (180.001, 240.125))

    def test_unknown_original_range_keeps_control_and_reports_missing_provenance(self):
        factory = self.setup_style(
            {"minimum_mw": None, "maximum_mw": None, "source": "unavailable", "reason": "No original image"}
        )
        self.assertIsNone(factory.call_args.kwargs["original_bounds"])
        self.assertIn("No original image", self.ui.add_text.call_args.args[0])
        self.assertIn("sl_pl", self.values)
        self.ui.delete_item.assert_not_called()

    def test_read_error_affects_coloring_only(self):
        self.start_patch(patch("druta.power_range_baseline.read", side_effect=RuntimeError("Read failed")))
        factory = self.start_patch(patch("druta.power_slider_style.PowerSliderStyle"))
        self.app.setup_power_slider_style()
        self.assertIsNone(factory.call_args.kwargs["original_bounds"])
        self.assertIn("Read failed", self.ui.add_text.call_args.args[0])
        self.ui.delete_item.assert_not_called()

    def test_new_style_releases_previous_items_and_uses_new_baseline(self):
        previous = self.app._power_slider_style = Mock()
        self.setup_style({"minimum_mw": 250000, "maximum_mw": 450000, "source": "live-bios", "reason": None})
        previous.close.assert_called_once_with()
        self.assertIsNot(self.app._power_slider_style, previous)

    def test_drag_and_typing_refresh_after_both_values_match(self):
        observed = []
        self.app._power_slider_style = Mock()
        self.app._power_slider_style.refresh.side_effect = lambda: observed.append(dict(self.values))
        for action in ("drag", "type"):
            for value in (250.0, 450.0, 450.001, 600.0, 400.0):
                with self.subTest(action=action, value=value):
                    if action == "drag":
                        self.values["sl_pl"] = value
                        self.app.knob_dragged("pl")
                    else:
                        self.values["in_pl"] = value
                        self.app.knob_typed("pl")
                    self.assertEqual(observed[-1], {"sl_pl": value, "in_pl": value})

    def test_programmatic_profile_or_stock_sync_refreshes_numeric_color(self):
        self.app._power_slider_style = Mock()
        seen = []
        self.app._power_slider_style.refresh.side_effect = lambda: seen.append(self.values["in_pl"])
        for value in (700.0, 450.0, 360.0):
            self.values["sl_pl"] = value
            self.app.sync_knob_boxes()
            self.assertEqual(seen[-1], value)

    def test_focused_input_is_not_overwritten_for_coloring(self):
        self.app._power_slider_style = Mock()
        self.values.update(sl_pl=360, in_pl=650)
        self.ui.is_item_focused.return_value = True
        self.app.sync_knob_boxes()
        self.assertEqual(self.values["in_pl"], 650)
        self.app._power_slider_style.refresh.assert_called_once_with()

    def test_absent_power_widget_needs_no_style(self):
        self.app.refresh_power_slider_style()
        self.assertFalse(hasattr(self.app, "_power_slider_style"))


if __name__ == "__main__":
    unittest.main()
