# Druta - hardware-free regression tests for card switching.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import druta
from druta import Druta


def switcher():
    app = Druta.__new__(Druta)
    app.gpu = SimpleNamespace(static={"name": "card A"},
                              slot=Mock(return_value="0000:01:00.0"),
                              arch=Mock(return_value=None))
    app.gpu_list = [{"slot": "0000:01:00.0", "name": "card A"},
                    {"slot": "0000:02:00.0", "name": "card B"}]
    app.vf_orig = {i: i * 15000 for i in range(128)}
    app.vf_work = dict(app.vf_orig)
    app.vf_points = [{"idx": i, "delta_khz": d}
                     for i, d in app.vf_orig.items()]
    app._tw_pending = {}
    app._clk_lock = None
    app._switch_armed = None
    app._discard_armed = False
    app._gpu_gen = 7
    app._rebuilding = False
    app.log = Mock()
    return app


class GpuSwitchTests(unittest.TestCase):
    TARGET = "0000:02:00.0"

    def test_pascal_frequency_controls_are_disabled_but_point_release_remains(self):
        app = switcher()
        app.unlocked = Mock(return_value=True)
        app._ctl_widgets = ["lock_min", "lock_max", "go_lock", "go_lockmax", "go_release"]
        app.gpu.fan_capabilities = Mock(return_value={"manual": True, "auto": True})
        for arch in (druta.GPU.ARCH_PASCAL, druta.GPU.ARCH_TURING, None):
            with self.subTest(arch=arch):
                app.gpu.arch.return_value = arch
                with patch("druta.dpg.does_item_exist",
                           side_effect=lambda tag: tag in app._ctl_widgets), \
                        patch("druta.dpg.configure_item") as configure:
                    app.sync_lock_ui()
                got = {call.args[0]: call.kwargs["enabled"] for call in configure.call_args_list}
                self.assertTrue(got.pop("go_release"))
                self.assertTrue(all(value == (arch != druta.GPU.ARCH_PASCAL)
                                    for value in got.values()))

    def test_unlock_does_not_enable_missing_legacy_fan_functions(self):
        app = switcher()
        app.unlocked = Mock(return_value=True)
        app._ctl_widgets = ["sl_fan", "in_fan", "go_fan", "go_fan_x", "go_pl"]
        for source, manual, auto in (("unavailable", False, False),
                                     ("nvml", True, False),
                                     ("nvml", False, True),
                                     ("nvapi-client", True, True),
                                     ("nvapi-classic", True, True)):
            with self.subTest(source=source, manual=manual, auto=auto):
                app.gpu.fan_capabilities = Mock(return_value={
                    "manual": manual, "auto": auto, "source": source})
                with patch("druta.dpg.does_item_exist",
                           side_effect=lambda tag: tag in app._ctl_widgets), \
                     patch("druta.dpg.configure_item") as configure:
                    app.sync_lock_ui()
                got = {c.args[0]: c.kwargs["enabled"] for c in configure.call_args_list}
                self.assertEqual(got, {"sl_fan": manual, "in_fan": manual,
                                       "go_fan": manual, "go_fan_x": auto,
                                       "go_pl": True})

    def test_read_curve_without_edits_switches_on_first_click(self):
        app = switcher()
        app.swap_gpu = Mock(return_value=True)
        app.switch_gpu(user_data=self.TARGET)
        app.swap_gpu.assert_called_once_with(self.TARGET)
        self.assertIsNone(app._switch_armed)
        app.log.assert_not_called()

    def test_only_changed_curve_points_are_counted_for_confirmation(self):
        app = switcher()
        app.vf_work[33] += 15000
        app.vf_work[89] -= 15000
        app.swap_gpu = Mock(return_value=True)
        app.switch_gpu(user_data=self.TARGET)
        app.swap_gpu.assert_not_called()
        self.assertEqual(app._switch_armed, self.TARGET)
        self.assertIn("2 staged V/F edit(s)", app.log.call_args.args[0])
        app.switch_gpu(user_data=self.TARGET)
        app.swap_gpu.assert_called_once_with(self.TARGET)

    def test_timing_edits_still_require_confirmation(self):
        app = switcher()
        app._tw_pending = {"timing": 42}
        app.swap_gpu = Mock(return_value=True)
        app.switch_gpu(user_data=self.TARGET)
        app.swap_gpu.assert_not_called()
        self.assertIn("1 staged timing write(s)", app.log.call_args.args[0])
        app.switch_gpu(user_data=self.TARGET)
        app.swap_gpu.assert_called_once_with(self.TARGET)

    def test_unavailable_or_failed_initialization_preserves_current_state(self):
        unavailable = SimpleNamespace(available=Mock(return_value=False),
                                      status_line=Mock(return_value="GPU missing"))
        failed_validation = SimpleNamespace(
            available=Mock(side_effect=RuntimeError("driver reset")))
        for failure in (unavailable, failed_validation, RuntimeError("open failed")):
            with self.subTest(failure=failure):
                app = switcher()
                app.vf_work[33] += 15000
                original_gpu = app.gpu
                original_points = app.vf_points
                original_work = dict(app.vf_work)
                app._tw_pending = {"timing": 42}
                app.reset_card_state = Mock()
                app.build_ui = Mock()
                kwargs = ({"side_effect": failure} if isinstance(failure, Exception)
                          else {"return_value": failure})
                with patch("druta.GPU", **kwargs):
                    self.assertFalse(app.swap_gpu(self.TARGET))
                self.assertIs(app.gpu, original_gpu)
                self.assertIs(app.vf_points, original_points)
                self.assertEqual(app.vf_work, original_work)
                self.assertEqual(app._tw_pending, {"timing": 42})
                self.assertEqual(app._gpu_gen, 7)
                self.assertFalse(app._rebuilding)
                app.reset_card_state.assert_not_called()
                app.build_ui.assert_not_called()
                self.assertFalse(app.log.call_args.args[1])

    def test_failed_curve_read_reports_unavailable_or_retained_curve(self):
        for previous_curve in (False, True):
            with self.subTest(previous_curve=previous_curve):
                app = switcher()
                app.vf_work[33] += 15000
                if not previous_curve:
                    app.vf_points = None
                    app.vf_orig = {}
                    app.vf_work = {}
                points_before = app.vf_points
                work_before = dict(app.vf_work)
                app.gpu.read_vf_curve = Mock(return_value=([], "invalid V/F response"))
                with patch("druta.dpg.does_item_exist", return_value=True), \
                        patch("druta.dpg.set_value") as value, \
                        patch("druta.dpg.configure_item") as configure:
                    app.vf_read(force=True)
                prefix = ("Curve refresh failed; showing previous read: "
                          if previous_curve else "Curve unavailable: ")
                value.assert_called_once_with("vf_info", prefix + "invalid V/F response")
                configure.assert_called_once_with("vf_info", color=druta.WARN)
                self.assertIs(app.vf_points, points_before)
                self.assertEqual(app.vf_work, work_before)


class MemoryControlLabelTests(unittest.TestCase):
    def test_memory_label_is_consistent_without_changing_colour_or_pairing(self):
        knob = next(kn for kn in Druta.DOMAIN_KNOBS if kn.ctrl == 2)
        for blackwell, pairing, grade, expected_colour, expected_private in (
                (True, {}, druta.PRIV_CONFIRMED, druta.GOOD, 2),
                (False, {}, druta.PRIV_CONFIRMED, druta.DIM, None),
                (False, {2: 4}, druta.PRIV_CONFIRMED, druta.TEXT, 4),
                (False, {2: 4}, druta.PRIV_LIKELY, druta.WARN, 4)):
            with self.subTest(blackwell=blackwell, pairing=pairing, grade=grade):
                app = Druta.__new__(Druta)
                app.gpu = SimpleNamespace(
                    clkdom_is_blackwell=Mock(return_value=blackwell),
                    clkdom_control_label=Mock(return_value="MEM"),
                    clkdom_pairing=Mock(return_value=pairing))
                result = app.domain_knob_label(knob, [
                    {"domain": 4, "name": "MEM", "grade": grade}])
                self.assertEqual(result,
                                 ("Additional Memory Clock Offset (MHz)",
                                  expected_colour, expected_private))


if __name__ == "__main__":
    unittest.main()
