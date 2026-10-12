# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""iCX requests retain independent readback and clamp before a valid write."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.test_arch_ui_regressions import FakeUiTest


class IcxVoltageClampTests(FakeUiTest):
    def setUp(self):
        super().setUp()
        self.order = []
        self.control = {"firmware_ok": True, "state_available": True,
                        "nvvdd_mode": "manual", "nvvdd_request_mv": 950,
                        "fbvdd_request_mv": 1300, "write_enabled": True,
                        "nvvdd_frequency_khz": 300, "fbvdd_frequency_khz": 400,
                        "nvvdd_ocp_enabled": True, "fbvdd_ocp_enabled": True,
                        "loadline_level": 0, "frequency_state_available": True,
                        "ocp_state_available": True, "loadline_state_available": True}
        self.rail = SimpleNamespace(
            icx_mcu=True,
            set_nvvdd_mv=Mock(side_effect=self._sent),
            set_nvvdd_auto=Mock(side_effect=self._auto),
            set_fbvdd_mv=Mock(side_effect=self._fb_sent),
            set_switching_frequency_khz=Mock(side_effect=self._frequency_sent),
            set_ocp_enabled=Mock(side_effect=self._ocp_sent),
            set_loadline=Mock(side_effect=self._loadline_sent),
            telemetry=Mock(side_effect=lambda: dict(self.control)))
        self.app.rail = self.rail
        self.app.gpu = SimpleNamespace(
            static={"gfx_min": 210, "gfx_max": 2100},
            lock_gpu_clocks=Mock(side_effect=self._locked),
            last_clock_lock_request=lambda: (210, 210),
            vf_lock_recovery_pending=lambda: False)
        self.app._clk_lock = None
        self.app._icx_armed = {"icxnv", "icxfb"}
        self.app._knob_sync = False
        self.app._slider_ranges = {}
        self.app._xoc_bounds = False
        self.app._i2c_verified = True
        self.app.guard = Mock(return_value=True)
        self.app.i2c_gate = Mock(return_value=(True, ""))
        self.app.i2c_verified = Mock(return_value=True)
        self.values.update(sl_icxnv=950, in_icxnv=950,
                           sl_icxfb=1300, in_icxfb=1300,
                           live_icxnv="", live_icxfb="", icx_requests="")

    def _locked(self, low, high):
        self.order.append(("lock", low, high))
        return True, "GPU clock locked to [210..210] MHz"

    def _sent(self, mv, acknowledged=False, xoc=False):
        self.order.append(("send", mv))
        self.control.update(nvvdd_mode="manual", nvvdd_request_mv=mv)
        return True, "sent"

    def _fb_sent(self, mv, acknowledged=False):
        self.order.append(("fb-send", mv))
        self.control["fbvdd_request_mv"] = mv
        return True, "FBVDD request confirmed"

    def _auto(self, acknowledged=False):
        self.order.append(("auto",))
        self.control.update(nvvdd_mode="auto", nvvdd_request_mv=None)
        return True, "auto"

    def _frequency_sent(self, rail, value, acknowledged=False):
        self.order.append(("frequency", rail, value))
        self.control[rail.lower() + "_frequency_khz"] = value
        return True, "frequency request confirmed"

    def _ocp_sent(self, rail, enabled, acknowledged=False):
        self.order.append(("ocp", rail, enabled))
        self.control[rail.lower() + "_ocp_enabled"] = enabled
        return True, "OCP request confirmed"

    def _loadline_sent(self, level, acknowledged=False):
        self.order.append(("loadline", level))
        self.control["loadline_level"] = level
        return True, "loadline request confirmed"

    def test_apply_locks_the_minimum_clock_before_the_write(self):
        self.app.apply_icx_nv(950)
        self.assertEqual(self.order, [("lock", 210, 210), ("send", 950)])
        self.assertEqual(self.app._clk_lock["kind"], "nvml")
        self.assertEqual((self.app._clk_lock["lo"], self.app._clk_lock["hi"]),
                         (210, 210))
        self.rail.set_nvvdd_mv.assert_called_once_with(950, acknowledged=True, xoc=False)

    def test_a_failed_lock_sends_nothing(self):
        self.app.gpu.lock_gpu_clocks = Mock(
            return_value=(False, "lock failed: needs admin"))
        self.app.apply_icx_nv(950)
        self.rail.set_nvvdd_mv.assert_not_called()
        self.assertIsNone(self.app._clk_lock)
        self.app.log.assert_any_call(
            "iCX voltage was not sent: lock failed: needs admin", False)

    def test_no_lockable_floor_sends_nothing(self):
        self.app.gpu.static = {}
        self.app.apply_icx_fb(1300)
        self.rail.set_fbvdd_mv.assert_not_called()
        self.app.gpu.lock_gpu_clocks.assert_not_called()

    def test_xoc_requests_above_one_byte_keep_the_full_value(self):
        self.app._xoc_bounds = True
        self.app.apply_icx_nv(1600)
        self.assertEqual(self.order, [("lock", 210, 210), ("send", 1600)])
        self.rail.set_nvvdd_mv.assert_called_once_with(1600, acknowledged=True, xoc=True)
        self.assertEqual(self.values["live_icxnv"], "1600 req")

    def _prepare_range_controls(self):
        # Keep the real control builder, range registration, checkbox handler,
        # and input callback; replace only widgets and unrelated rail readers.
        self.app._knob_cb = {}
        self.app._ctl_widgets = []
        self.app._carryover_hi = {}
        self.app._rail_writable = set()
        self.app._risk_themes = {}
        self.app.bind = Mock()
        self.app.s = lambda value: value
        self.app.ov_carryover = Mock()
        self.app.refresh_current_limits = Mock()
        self.app.gpu.read_volt_rail_limits = Mock(return_value={})
        self.rail.p = SimpleNamespace(read_only=False)
        self.rail.enable_xoc = Mock()
        self.rail.disable_xoc = Mock()
        self.values.update(xoc_mode=False, risk_banner="")
        self.widget_config = {}
        for key in ("icxnv", "icxfb"):
            for prefix in ("sl_", "in_"):
                self.values.pop(prefix + key)

        def add_widget(*args, **config):
            tag = config.get("tag")
            if tag is None:
                return None
            self.widget_config[tag] = dict(config)
            if args:
                self.widget_config[tag]["items"] = args[0]
            self.values[tag] = config.get("default_value", args[0] if args else "")
            return tag

        def configure_widget(tag, **config):
            self.widget_config.setdefault(tag, {}).update(config)

        self.ui.add_slider_int.side_effect = add_widget
        self.ui.add_input_int.side_effect = add_widget
        self.ui.add_combo.side_effect = add_widget
        self.ui.add_button.side_effect = add_widget
        self.ui.add_text.side_effect = add_widget
        self.ui.configure_item.side_effect = configure_widget

    def _assert_icx_ranges(self, nvvdd_max):
        for key, bounds in (("icxnv", (750, nvvdd_max)), ("icxfb", (1250, 1450))):
            self.assertEqual(self.app.knob_bounds(key), bounds)
            for prefix in ("sl_", "in_"):
                config = self.widget_config[prefix + key]
                self.assertEqual((config["min_value"], config["max_value"]), bounds)

    def _assert_no_icx_writes_or_clock_locks(self):
        self.rail.set_nvvdd_mv.assert_not_called()
        self.rail.set_nvvdd_auto.assert_not_called()
        self.rail.set_fbvdd_mv.assert_not_called()
        self.rail.set_switching_frequency_khz.assert_not_called()
        self.rail.set_ocp_enabled.assert_not_called()
        self.rail.set_loadline.assert_not_called()
        self.app.gpu.lock_gpu_clocks.assert_not_called()
        self.assertEqual(self.order, [])
        self.assertIsNone(self.app._clk_lock)

    def test_built_icx_controls_follow_xoc_checkbox_both_ways_without_writes(self):
        self._prepare_range_controls()
        self.app.build_icx_controls()
        self._assert_icx_ranges(1150)

        self.values["xoc_mode"] = True
        self.app.sync_risk_ui()
        self._assert_icx_ranges(1600)
        self.values["in_icxnv"] = 1600
        self.widget_config["in_icxnv"]["callback"]()
        self.assertEqual(self.values["sl_icxnv"], 1600)
        self.assertEqual(self.values["in_icxnv"], 1600)

        self.values["xoc_mode"] = False
        self.app.sync_risk_ui()
        self._assert_icx_ranges(1150)
        self.assertEqual(self.values["sl_icxnv"], 1150)
        self.assertEqual(self.values["in_icxnv"], 1150)
        self._assert_no_icx_writes_or_clock_locks()

    def test_icx_detected_after_xoc_enabled_builds_with_extended_range(self):
        self._prepare_range_controls()
        self.app.rail = None
        self.values["xoc_mode"] = True
        self.app.sync_risk_ui()
        self.assertEqual(self.app._slider_ranges, {})

        self.app.rail = self.rail
        self.app.build_icx_controls()
        # No second risk refresh: late-discovered controls need the current
        # XOC range at construction, before another checkbox transition.
        self._assert_icx_ranges(1600)
        self.values["xoc_mode"] = False
        self.app.sync_risk_ui()
        self._assert_icx_ranges(1150)
        self._assert_no_icx_writes_or_clock_locks()

    def test_invalid_requests_do_not_lock_or_write(self):
        for setter, values in ((self.app.apply_icx_nv, (float("nan"), True, 748, 951, 1155)),
                               (self.app.apply_icx_fb, (1245, 1301, 1455))):
            for value in values:
                with self.subTest(setter=setter.__name__, value=value):
                    setter(value)
        self.rail.set_nvvdd_mv.assert_not_called()
        self.rail.set_fbvdd_mv.assert_not_called()
        self.app.gpu.lock_gpu_clocks.assert_not_called()
        self.assertIsNone(self.app._clk_lock)

    def test_auto_locks_and_refreshes_mode_without_changing_fbvdd(self):
        self.app.apply_icx_stock()
        self.assertEqual(self.order, [("lock", 210, 210), ("auto",)])
        self.rail.set_nvvdd_auto.assert_called_once_with(acknowledged=True)
        self.rail.set_fbvdd_mv.assert_not_called()
        self.assertEqual(self.values["live_icxnv"], "Auto req")
        self.assertEqual(self.values["live_icxfb"], "1300 req")
        self.assertNotIn("750", self.values["icx_requests"])

    def test_fbvdd_apply_preserves_nvvdd_auto_and_staged_manual_value(self):
        self.control.update(nvvdd_mode="auto", nvvdd_request_mv=None)
        self.values.update(sl_icxfb=1380, in_icxfb=1380)
        self.app.apply_icx_fb(1380)
        self.assertEqual(self.order, [("lock", 210, 210), ("fb-send", 1380)])
        self.rail.set_nvvdd_mv.assert_not_called()
        self.rail.set_nvvdd_auto.assert_not_called()
        self.assertEqual(self.values["live_icxnv"], "Auto req")
        self.assertEqual(self.values["sl_icxnv"], 950)
        self.assertEqual(self.values["live_icxfb"], "1380 req")

    def test_initial_read_populates_unedited_requests(self):
        self.app._icx_armed.clear()
        self.control.update(nvvdd_request_mv=1000, fbvdd_request_mv=1380)
        self.app.refresh_icx_requests()
        self.assertEqual(self.values["sl_icxnv"], 1000)
        self.assertEqual(self.values["in_icxfb"], 1380)
        self.assertEqual(self.app._icx_armed, set())
        self.rail.set_nvvdd_mv.assert_not_called()
        self.rail.set_fbvdd_mv.assert_not_called()

    def test_auto_initial_read_retains_staged_manual_proposal(self):
        self.app._icx_armed.clear()
        self.control.update(nvvdd_mode="auto", nvvdd_request_mv=None)
        self.app.refresh_icx_requests()
        self.assertEqual(self.values["sl_icxnv"], 950)
        self.assertEqual(self.values["live_icxnv"], "Auto req")

    def test_refresh_preserves_edits_and_marks_last_confirmed_values_on_failure(self):
        self.values.update(sl_icxnv=1050, in_icxnv=1050)
        self.app.refresh_icx_requests()
        self.assertEqual(self.values["sl_icxnv"], 1050)
        self.assertEqual(self.values["live_icxnv"], "950 req")
        self.control.clear()
        self.app.refresh_icx_requests()
        self.assertEqual(self.values["sl_icxnv"], 1050)
        self.assertEqual(self.values["live_icxnv"], "950 req *")
        self.assertIn("last confirmed; readback unavailable", self.values["icx_requests"])
        self.assertEqual(self.app._icx_armed, {"icxnv", "icxfb"})

    def test_unavailable_fbvdd_does_not_discard_nvvdd_readback(self):
        self.control["fbvdd_request_mv"] = None
        self.app.refresh_icx_requests()
        self.assertEqual(self.values["live_icxnv"], "950 req")
        self.assertEqual(self.values["live_icxfb"], "unavailable")

    def test_setting_builders_populate_current_state_without_arming_or_writing(self):
        self._prepare_range_controls()
        self.app.build_icx_controls()
        expected = {"icx_nv_frequency": "300 kHz", "icx_fb_frequency": "400 kHz",
                    "icx_nv_ocp": "Enable", "icx_fb_ocp": "Enable", "icx_loadline": "Level0"}
        for key, value in expected.items():
            self.assertEqual(self.values["in_" + key], value)
            self.assertEqual(self.values["live_" + key], value)
            self.assertNotIn(key, self.app._icx_armed)
            self.assertIn("in_" + key, self.app._ctl_widgets)
            self.assertIn("go_" + key, self.app._ctl_widgets)
        self.assertEqual(self.widget_config["in_icx_nv_frequency"]["items"],
                         [f"{n} kHz" for n in range(300, 1001, 100)])
        self.assertEqual(self.widget_config["in_icx_loadline"]["items"],
                         [f"Level{n}" for n in range(16)])
        self._assert_no_icx_writes_or_clock_locks()

    def test_each_setting_callback_stages_then_applies_only_its_own_field(self):
        self._prepare_range_controls()
        self.app.build_icx_controls()
        cases = (
            ("icx_nv_frequency", "500 kHz", "set_switching_frequency_khz", ("NVVDD", 500), ("frequency", "NVVDD", 500)),
            ("icx_fb_frequency", "1000 kHz", "set_switching_frequency_khz", ("FBVDD", 1000), ("frequency", "FBVDD", 1000)),
            ("icx_nv_ocp", "Disable", "set_ocp_enabled", ("NVVDD", False), ("ocp", "NVVDD", False)),
            ("icx_fb_ocp", "Disable", "set_ocp_enabled", ("FBVDD", False), ("ocp", "FBVDD", False)),
            ("icx_loadline", "Level15", "set_loadline", (15,), ("loadline", 15)),
        )
        for key, choice, setter, args, event in cases:
            with self.subTest(key=key):
                self.order.clear()
                self.app._clk_lock = None
                for method in ("set_switching_frequency_khz", "set_ocp_enabled", "set_loadline"):
                    getattr(self.rail, method).reset_mock()
                self.app.gpu.lock_gpu_clocks.reset_mock()
                self.values["in_" + key] = choice
                self.widget_config["in_" + key]["callback"]("in_" + key, choice, None)
                self.assertEqual(self.order, [])
                self.app.gpu.lock_gpu_clocks.assert_not_called()
                self.widget_config["go_" + key]["callback"]()
                self.assertEqual(self.order, [("lock", 210, 210), event])
                getattr(self.rail, setter).assert_called_once_with(*args, acknowledged=True)
                for method in {"set_switching_frequency_khz", "set_ocp_enabled", "set_loadline"} - {setter}:
                    getattr(self.rail, method).assert_not_called()
                self.assertEqual(self.values["live_" + key], choice)
                self.rail.set_nvvdd_mv.assert_not_called()
                self.rail.set_nvvdd_auto.assert_not_called()
                self.rail.set_fbvdd_mv.assert_not_called()

    def test_setting_refusals_happen_before_clock_lock_or_backend_call(self):
        self._prepare_range_controls()
        self.app.build_icx_controls()
        key = "icx_nv_frequency"
        self.widget_config["go_" + key]["callback"]()  # Readback does not arm Apply.
        self.app._icx_armed.add(key)
        self.values["in_" + key] = "200 kHz"
        self.widget_config["go_" + key]["callback"]()
        self.values["in_" + key] = "500 kHz"
        self.app.guard.return_value = False
        self.widget_config["go_" + key]["callback"]()
        self.app.guard.return_value = True
        self.app.i2c_gate.return_value = (False, "identity unavailable")
        self.widget_config["go_" + key]["callback"]()
        self.app.i2c_gate.return_value = (True, "")
        self.app.i2c_verified.return_value = False
        self.widget_config["go_" + key]["callback"]()
        self._assert_no_icx_writes_or_clock_locks()

    def test_setting_failed_clock_lock_never_calls_setter(self):
        self._prepare_range_controls()
        self.app.build_icx_controls()
        key = "icx_fb_ocp"
        self.values["in_" + key] = "Disable"
        self.widget_config["in_" + key]["callback"]("in_" + key, "Disable", None)
        self.app.gpu.lock_gpu_clocks = Mock(return_value=(False, "lock failed"))
        self.widget_config["go_" + key]["callback"]()
        self.rail.set_ocp_enabled.assert_not_called()
        self.app.log.assert_any_call("iCX setting was not sent: lock failed", False)

    def test_unavailable_setting_group_preserves_edits_and_other_readbacks(self):
        self._prepare_range_controls()
        self.app.build_icx_controls()
        key = "icx_nv_frequency"
        self.values["in_" + key] = "700 kHz"
        self.widget_config["in_" + key]["callback"]("in_" + key, "700 kHz", None)
        self.control.update(frequency_state_available=False, nvvdd_frequency_khz=None,
                            fbvdd_frequency_khz=None, loadline_level=2, nvvdd_ocp_enabled=False)
        self.app.refresh_icx_requests()
        self.assertEqual(self.values["in_" + key], "700 kHz")
        self.assertEqual(self.values["live_" + key], "300 kHz *")
        self.assertEqual(self.values["in_icx_loadline"], "Level2")
        self.assertEqual(self.values["live_icx_nv_ocp"], "Disable")
        self.assertEqual(self.values["live_icxnv"], "950 req")
        self.assertIn("last confirmed", self.values["icx_setting_requests"])
        self._assert_no_icx_writes_or_clock_locks()

    def test_unknown_group_fields_are_not_shown_as_defaults_or_other_rail_state(self):
        self._prepare_range_controls()
        self.control.update(nvvdd_frequency_khz=None, fbvdd_frequency_khz=500,
                            nvvdd_ocp_enabled=None, fbvdd_ocp_enabled=False,
                            loadline_level=None)
        self.app.build_icx_controls()
        self.assertEqual(self.values["in_icx_nv_frequency"], "Select")
        self.assertEqual(self.values["live_icx_nv_frequency"], "unavailable")
        self.assertEqual(self.values["in_icx_fb_frequency"], "500 kHz")
        self.assertEqual(self.values["in_icx_nv_ocp"], "Select")
        self.assertEqual(self.values["in_icx_fb_ocp"], "Disable")
        self.assertEqual(self.values["in_icx_loadline"], "Select")
        self._assert_no_icx_writes_or_clock_locks()

    def test_panel_rebuild_preserves_staged_setting_and_readback_independently(self):
        self._prepare_range_controls()
        self.app.build_icx_controls()
        key = "icx_nv_frequency"
        self.values["in_" + key] = "700 kHz"
        self.widget_config["in_" + key]["callback"]("in_" + key, "700 kHz", None)
        self.values["i2c_candidates"] = "container"
        self.app.build_i2c_candidates = self.app.build_icx_controls
        self.app.sync_lock_ui = Mock()
        self.app.sync_risk_ui = Mock()
        self.app.relayout = Mock()
        self.control["frequency_state_available"] = False
        self.app.refresh_i2c_candidates()
        self.assertEqual(self.values["in_" + key], "700 kHz")
        self.assertEqual(self.values["live_" + key], "300 kHz *")
        self.assertIn(key, self.app._icx_armed)
        self._assert_no_icx_writes_or_clock_locks()

    def test_failed_setting_apply_does_not_report_staged_choice_as_readback(self):
        self._prepare_range_controls()
        self.app.build_icx_controls()
        key = "icx_nv_frequency"
        self.values["in_" + key] = "500 kHz"
        self.widget_config["in_" + key]["callback"]("in_" + key, "500 kHz", None)
        self.rail.set_switching_frequency_khz.side_effect = None
        self.rail.set_switching_frequency_khz.return_value = (False, "request readback did not match")
        self.widget_config["go_" + key]["callback"]()
        self.assertEqual(self.values["in_" + key], "500 kHz")
        self.assertEqual(self.values["live_" + key], "300 kHz")
        self.app.log.assert_any_call("request readback did not match", False)
        self.rail.set_switching_frequency_khz.assert_called_once_with("NVVDD", 500, acknowledged=True)

    def test_connection_baseline_waits_for_complete_read_and_is_not_overwritten(self):
        self._prepare_range_controls()
        self.control["frequency_state_available"] = False
        self.app.build_icx_controls()
        self.assertIsNone(self.app._icx_captured_preset)
        self.control.update(frequency_state_available=True, fbvdd_request_mv=1380,
                            nvvdd_frequency_khz=500)
        self.app.refresh_icx_requests()
        captured = self.app._icx_captured_preset
        self.assertEqual(captured["settings"]["fbvdd_request_mv"], 1380)
        self.assertEqual(captured["settings"]["nvvdd_frequency_khz"], 500)
        self.control.update(fbvdd_request_mv=1360, nvvdd_frequency_khz=600)
        self.app.refresh_icx_requests()
        self.assertIs(self.app._icx_captured_preset, captured)
        self.widget_config["icx_restore_captured"]["callback"]()
        self.assertEqual(self.app._icx_pending_preset["document"]["settings"], captured["settings"])
        self.assertIn("not factory defaults", self.values["icx_capture_note"])
        self._assert_no_icx_writes_or_clock_locks()

    def test_loading_a_preset_previews_all_settings_without_changing_rows_or_hardware(self):
        from druta import icx_presets
        self._prepare_range_controls()
        self.app.build_icx_controls()
        state = dict(self.control, nvvdd_mode="auto", nvvdd_request_mv=None,
                     fbvdd_request_mv=1380, loadline_level=3, nvvdd_ocp_enabled=False)
        document = icx_presets.make_preset(state)
        self.assertTrue(self.app.stage_icx_preset(document, "test preset"))
        self.assertEqual(self.values["sl_icxfb"], 1300)
        self.assertEqual(self.values["in_icx_loadline"], "Level0")
        self.assertIn(icx_presets.describe_settings(document), self.values["icx_preset_preview"])
        self.assertIn("applies the settings", self.values["icx_preset_preview"])
        self._assert_no_icx_writes_or_clock_locks()

    def test_preset_apply_is_explicit_and_clamps_before_dispatch(self):
        self._prepare_range_controls()
        self.app.build_icx_controls()
        self.widget_config["icx_restore_captured"]["callback"]()
        pending = self.app._icx_pending_preset["document"]
        with patch("druta.icx_presets.apply_preset", side_effect=lambda *args, **kwargs:
                   (self.order.append(("preset",)), (True, "preset verified"))[1]) as apply:
            self.assertEqual(self.order, [])
            self.widget_config["icx_apply_preset"]["callback"]()
        self.assertEqual(self.order, [("lock", 210, 210), ("preset",)])
        apply.assert_called_once_with(self.rail, pending, acknowledged=True, xoc=False)

    def test_preset_xoc_validation_happens_before_clock_lock(self):
        from druta import icx_presets
        self._prepare_range_controls()
        self.app.build_icx_controls()
        document = icx_presets.make_preset(dict(self.control, nvvdd_request_mv=1600))
        self.assertTrue(self.app.stage_icx_preset(document))
        with patch("druta.icx_presets.apply_preset") as apply:
            self.widget_config["icx_apply_preset"]["callback"]()
            apply.assert_not_called()
        self._assert_no_icx_writes_or_clock_locks()

    def test_preset_apply_rechecks_connection_identity_and_lock_failure(self):
        self._prepare_range_controls()
        self.app.build_icx_controls()
        self.app.stage_captured_icx_preset()
        with patch("druta.icx_presets.apply_preset") as apply:
            self.app.i2c_gate.return_value = (False, "identity unavailable")
            self.app.apply_icx_preset()
            self.app.gpu.lock_gpu_clocks.assert_not_called()
            self.app.i2c_gate.return_value = (True, "")
            self.app.gpu.lock_gpu_clocks = Mock(return_value=(False, "clock unavailable"))
            self.app.apply_icx_preset()
            self.app._gpu_gen += 1
            self.app.apply_icx_preset()
            apply.assert_not_called()
        self.assertIsNone(self.app._icx_pending_preset)
        self.assertIsNone(self.app._icx_captured_preset)

    def test_save_freezes_complete_readback_before_mocked_file_picker(self):
        from druta import icx_presets
        self._prepare_range_controls()
        self.app.build_icx_controls()
        with TemporaryDirectory() as folder:
            target = Path(folder) / "current.json"
            with patch("druta.druta.paths.profile_dir", return_value=Path(folder)), \
                    patch("druta.druta.threading.Thread") as thread, \
                    patch.object(self.app, "pick_file_native", return_value=(str(target), "")):
                self.app.open_icx_preset(save=True)
                self.control["fbvdd_request_mv"] = 1380
                thread.call_args.kwargs["target"]()
                self.assertFalse(target.exists())
                self.app.poll_icx_preset()
            self.assertEqual(icx_presets.read_preset(target)["settings"]["fbvdd_request_mv"], 1300)
        self._assert_no_icx_writes_or_clock_locks()

    def test_file_load_only_stages_after_picker_and_current_connection_check(self):
        from druta import icx_presets
        self._prepare_range_controls()
        self.app.build_icx_controls()
        with TemporaryDirectory() as folder:
            target = Path(folder) / "load.json"
            document = icx_presets.make_preset(dict(self.control, fbvdd_request_mv=1380))
            icx_presets.write_preset(target, document)
            with patch("druta.druta.paths.profile_dir", return_value=Path(folder)), \
                    patch("druta.druta.threading.Thread") as thread, \
                    patch.object(self.app, "pick_file_native", return_value=(str(target), "")):
                self.app.open_icx_preset(save=False)
                thread.call_args.kwargs["target"]()
                self.assertIsNone(getattr(self.app, "_icx_pending_preset", None))
                self.app.poll_icx_preset()
            self.assertEqual(self.app._icx_pending_preset["document"]["settings"]["fbvdd_request_mv"], 1380)
        self._assert_no_icx_writes_or_clock_locks()

    def test_picker_results_from_previous_gpu_or_controller_are_discarded(self):
        self._prepare_range_controls()
        self.app.build_icx_controls()
        connection = self.app._icx_preset_connection()
        document = self.app._icx_captured_preset
        for change in ("generation", "controller"):
            with self.subTest(change=change), patch("druta.icx_presets.write_preset") as write:
                self.app._icx_preset_result = (connection, 0, True, "unused.json", "", document)
                if change == "generation":
                    self.app._gpu_gen += 1
                else:
                    self.app._gpu_gen = connection[2]
                    self.app.rail = SimpleNamespace(icx_mcu=True)
                self.app.poll_icx_preset()
                write.assert_not_called()
        self._assert_no_icx_writes_or_clock_locks()
