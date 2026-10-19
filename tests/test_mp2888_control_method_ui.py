# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Control recipe selection preserves one physical route and separate authorization."""
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

import dearpygui.dearpygui as real_dpg

from druta import profiles
from druta.druta import Druta
from tests import test_mp2888_pmbus_ui as pmbus_ui
from tests import test_tune_profiles as tune_profiles


class ControlMethodUiTests(unittest.TestCase):
    def setUp(self):
        self.fixture = pmbus_ui.PmbusUiTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.app, self.ui, self.values = self.fixture.app, self.fixture.ui, self.fixture.values
        self.manual = self.app.rail
        profile = deepcopy(self.manual.p)
        profile.src = {"control": "legacy-offset", "bus": {"port": 1, "addr7": 0x20}}
        profile.env_min, profile.env_max = 0, 100
        profile.hw_min_mv, profile.hw_max_mv = -693.75, 700
        profile.lsb_mv = 6.25
        profile.rungs = [6.25, 12.5, 25, 50, 75]
        self.offset = SimpleNamespace(
            p=profile, addr7=0x20, control_method="offset", requires_verification=True,
            present=Mock(return_value=True), read_vout=Mock(return_value=1066),
            plan=Mock(return_value=(True, "offset request")),
            set_offset_mv=Mock(return_value=(True, "offset accepted")),
            reset=Mock(return_value=(True, "offset reset")),
            telemetry=Mock(return_value={"vout_mv": 1066, "offset_mv": 43.75}))
        self.offset.discovery_candidate = self.offset
        self.manual.discovery_candidate = self.offset
        self.manual.control_method = "pmbus"
        self.methods = {"offset": self.offset, "pmbus": self.manual}
        self.offset.for_control_method = Mock(side_effect=self.methods.__getitem__)
        self.manual.for_control_method = self.offset.for_control_method
        self.app.rail = self.offset
        self.app._rail_candidates = [self.offset]
        self.app._i2c_discovery_complete = True
        self.app._i2c_verified_for = self.app.i2c_connection()
        self.app._i2c_recovery_for = self.app.i2c_connection()
        self.app.refresh_i2c_candidates = Mock()
        self.app.s = lambda value: value
        self.values["i2c_control_method"] = Druta.I2C_VOLTAGE_METHODS["offset"]

    def select(self, method):
        return self.app.select_i2c_control_method(app_data=Druta.I2C_VOLTAGE_METHODS[method])

    def test_default_and_selector_use_requested_names_without_generation_or_board_gates(self):
        self.assertEqual(self.app.rail.control_method, "offset")
        self.app.build_i2c_control_method()
        combo = self.ui.add_combo.call_args
        self.assertEqual(combo.args[0], ["MP2888A (ASUS Turing)", "MP2888A (ASUS Ampere)"])
        self.assertEqual(combo.kwargs["default_value"], "MP2888A (ASUS Turing)")
        self.assertTrue(any("GPU VID offset" in call.args[0] for call in self.ui.add_text.call_args_list))
        self.assertTrue(self.select("pmbus"))
        self.app.build_i2c_control_method()
        self.assertTrue(any("Manual nominal PMBus" in call.args[0]
                            for call in self.ui.add_text.call_args_list))

    def test_selection_changes_only_adapter_and_writes_no_controller_or_clock(self):
        canonical_profile = deepcopy(self.offset.p.src)
        self.assertTrue(self.select("pmbus"))
        self.assertIs(self.app.rail, self.manual)
        self.assertEqual(self.app._rail_candidates, [self.offset])
        self.assertEqual(self.offset.p.src, canonical_profile)
        self.assertEqual(self.fixture.events, [])
        self.manual.capture_control.assert_not_called()
        self.manual.set_voltage_mv.assert_not_called()
        self.manual.reset.assert_not_called()
        self.manual.restore_control.assert_not_called()
        self.app.gpu.lock_gpu_clocks.assert_not_called()
        self.app.refresh_i2c_candidates.assert_called_once()

    def test_method_change_requires_fresh_verify_in_both_directions(self):
        offset_token = self.app.i2c_connection()
        self.assertTrue(self.app.i2c_verified())
        self.assertTrue(self.select("pmbus"))
        self.assertFalse(self.app.i2c_verified())
        self.assertIsNone(self.app._i2c_recovery_for)
        self.app.apply_i2c_rail(1000)
        self.manual.set_voltage_mv.assert_not_called()
        manual_token = self.app.i2c_connection()
        self.assertNotEqual(offset_token, manual_token)
        self.app._i2c_verified = True
        self.app._i2c_verified_for = manual_token
        self.assertTrue(self.select("offset"))
        self.assertFalse(self.app.i2c_verified())
        self.app.apply_i2c_rail(50)
        self.offset.set_offset_mv.assert_not_called()

    def test_write_status_names_only_the_current_methods_verification(self):
        self.app._i2c_status_built = True
        self.values["i2c_write_status"] = ""
        self.app.sync_i2c_write_status()
        self.assertEqual(self.values["i2c_write_status"], "GPU VID offset method verified this session")
        token = self.app.i2c_connection()
        self.select("pmbus")
        self.app._i2c_verified = True
        self.app._i2c_verified_for = token
        self.app.sync_i2c_write_status()
        self.assertEqual(self.values["i2c_write_status"], "Manual PMBus method unverified - Verify first")

    def test_candidate_label_and_saved_route_remain_the_physical_offset_candidate(self):
        original_label = self.app.i2c_candidate_label(self.offset)
        self.select("pmbus")
        self.assertEqual(self.app.i2c_candidate_label(self.manual), original_label)
        self.assertTrue(original_label.startswith("1:"))
        self.assertFalse(self.app.select_i2c_candidate(app_data=original_label))
        self.assertIs(self.app.rail, self.manual)
        with patch("druta.druta.i2c_cache.remember") as remember:
            self.app.remember_i2c_route()
        remember.assert_called_once_with(self.app.gpu, self.offset)

    def test_owned_manual_entry_is_restored_before_method_switch_releases_clamp(self):
        self.select("pmbus")
        self.app._i2c_verified = True
        self.app._i2c_verified_for = self.app.i2c_connection()
        self.fixture.apply()
        self.fixture.events.clear()
        self.assertTrue(self.select("offset"))
        self.assertEqual(self.fixture.events, ["restore", "release"])
        self.assertEqual(self.fixture.control, self.fixture.entry)
        self.assertIsNone(self.fixture.clock)
        self.assertIs(self.app.rail, self.offset)

    def test_failed_manual_handback_keeps_method_controller_owner_and_clock(self):
        self.select("pmbus")
        self.app._i2c_verified = True
        self.app._i2c_verified_for = self.app.i2c_connection()
        self.fixture.apply()
        self.fixture.control["command"] += 1
        self.fixture.events.clear()
        self.values["i2c_control_method"] = Druta.I2C_VOLTAGE_METHODS["offset"]
        self.assertFalse(self.select("offset"))
        self.assertEqual(self.fixture.events, ["restore"])
        self.assertIs(self.app.rail, self.manual)
        self.assertIsNotNone(self.app._i2c_manual_voltage)
        self.assertEqual(self.fixture.clock, (405, 405))
        self.assertEqual(self.values["i2c_control_method"], Druta.I2C_VOLTAGE_METHODS["pmbus"])
        self.assertTrue(self.app._i2c_restore_failed)

    def test_method_change_cannot_interrupt_verification_scan_or_profile_restore(self):
        for attr, value in (("_i2c_busy", True), ("_i2c_scan_busy", True),
                            ("_profile_pending", ("name", {}, False)), ("_profile_applying", True)):
            with self.subTest(attr=attr):
                setattr(self.app, attr, value)
                self.values["i2c_control_method"] = Druta.I2C_VOLTAGE_METHODS["pmbus"]
                self.assertFalse(self.select("pmbus"))
                self.assertIs(self.app.rail, self.offset)
                self.assertEqual(self.values["i2c_control_method"], Druta.I2C_VOLTAGE_METHODS["offset"])
                setattr(self.app, attr, None if attr == "_profile_pending" else False)
        self.offset.for_control_method.assert_not_called()
        self.assertEqual(self.fixture.events, [])

    def test_busy_ui_disables_method_selection(self):
        self.app._i2c_busy = True
        self.app.sync_verification_ui()
        self.ui.configure_item.assert_any_call("i2c_control_method", enabled=False)

    def test_offset_and_manual_rows_keep_distinct_units_ranges_and_reset_actions(self):
        self.app.slider_row = Mock()
        self.app.build_i2c_rail_row()
        offset = self.app.slider_row.call_args
        self.assertIn("offset at VRM (mV)", offset.args[1])
        self.assertEqual(offset.args[2:4], (0, 100))
        self.assertEqual(offset.kwargs["extra"][1][0], "Stock")
        self.select("pmbus")
        self.app.build_i2c_rail_row()
        manual = self.app.slider_row.call_args
        self.assertIn("nominal PMBus request (mV)", manual.args[1])
        self.assertEqual(manual.args[2:4], (300, 1200))
        self.assertEqual(manual.kwargs["extra"][1][0], "Auto")

    def test_profile_resolution_uses_canonical_offset_without_modifying_saved_data(self):
        saved = {"i2c": {**profiles.rail_identity(self.offset), "offset_mv": 43.75}}
        original = deepcopy(saved)
        self.select("pmbus")
        self.assertIs(self.app.rail_for_profile(saved), self.offset)
        self.assertIs(self.app.rail_for_profile({}), self.offset)
        self.assertEqual(saved, original)

    def test_profile_cleanup_precedes_canonical_offset_preflight(self):
        self.select("pmbus")
        self.app._i2c_verified = True
        self.app._i2c_verified_for = self.app.i2c_connection()
        self.fixture.apply()
        self.fixture.events.clear()
        self.app.profile_failure = Mock()
        saved = {"i2c": {**profiles.rail_identity(self.offset), "offset_mv": 43.75}}

        def preflight(_gpu, _saved, rail):
            self.assertEqual(self.fixture.control, self.fixture.entry)
            self.assertIs(rail, self.offset)
            return "stop after ordering check"

        with patch("druta.druta.profiles.preflight", side_effect=preflight):
            self.app.begin_profile_load("legacy", saved)
        self.assertEqual(self.fixture.events, ["restore", "release"])
        self.app.profile_failure.assert_called_once_with("stop after ordering check", False)

    def test_manual_choice_in_auto_captures_and_replays_existing_legacy_offset(self):
        bus = self.fixture.use_register_backend()
        manual = self.app.rail
        canonical = manual.discovery_candidate
        self.app._rail_candidates = [canonical]
        self.app.gpu, _, _ = tune_profiles.hardware()
        bus.regs[0x23] = 7
        state = profiles.capture(self.app.gpu, manual)
        self.assertEqual(state["i2c"]["offset_mv"], 43.75)
        for key, value in profiles.rail_identity(canonical).items():
            self.assertEqual(state["i2c"][key], value)
        self.assertFalse(bus.regs[0xE1] & 0x10)
        bus.regs[0x23] = 0

        def verify_offset():
            self.assertIs(self.app.rail, canonical)
            self.app._i2c_verified = True
            self.app._i2c_verified_for = self.app.i2c_connection()

        self.app.verify_i2c_rail = Mock(side_effect=verify_offset)
        self.app.profile_failure = Mock()
        for name in ("sync_risk_ui", "sync_sliders_from_gpu", "apply_profile_policy_names",
                     "refresh_volt_limits", "vf_read", "sync_hold_headroom"):
            setattr(self.app, name, Mock())
        self.app.begin_profile_load("old offset profile", state)
        self.app.profile_failure.assert_not_called()
        self.app.verify_i2c_rail.assert_called_once()
        self.assertIs(self.app.rail, canonical)
        self.assertEqual(bus.writes, [(0x23, 7, 2)])
        self.assertEqual(bus.regs[0x23], 7)
        self.assertFalse(bus.regs[0xE1] & 0x10)

    def assert_named_profile_requires_auto(self, *, undo):
        bus = self.fixture.use_register_backend()
        canonical = self.app.rail.discovery_candidate
        self.app._rail_candidates = [canonical]
        full_gpu, _, _ = tune_profiles.hardware()
        self.app.gpu.static.update(full_gpu.static)
        for name, value in vars(full_gpu).items():
            if name != "static" and not hasattr(self.app.gpu, name):
                setattr(self.app.gpu, name, value)
        state = {"schema": 1, "device": dict(self.app.gpu.static)}
        self.fixture.apply(1000)
        self.assertTrue(canonical.has_owned_manual_control())
        owned = self.app._i2c_manual_voltage
        reads, writes, events = list(bus.reads), list(bus.writes), list(self.fixture.events)
        self.app.profile_failure = Mock()
        self.app.refresh_profile_list = Mock()
        self.app.finish_profile_load = Mock()
        invoke = self.app.undo_last_write if undo else lambda: self.app.load_profile("saved")
        autosaves = [("saved", "in-memory-profile.json", "", True)]
        with patch("druta.druta.profiles.load", return_value=state), \
                patch("druta.druta.profiles.list_profiles", return_value=autosaves), \
                patch.object(self.app, "_finish_manual_voltage", wraps=self.app._finish_manual_voltage) as handback:
            invoke()
            self.app.profile_failure.assert_called_once()
            self.assertIn("Auto before loading a profile", self.app.profile_failure.call_args.args[0])
            self.app.finish_profile_load.assert_not_called()
            handback.assert_not_called()
            self.assertIs(self.app._i2c_manual_voltage, owned)
            self.assertEqual(self.fixture.clock, (405, 405))
            self.assertEqual(bus.reads, reads)
            self.assertEqual(bus.writes, writes)
            self.assertEqual(self.fixture.events, events)

            self.app.stock_knob("i2crail")  # Explicit Auto, through the actual row action.
            self.assertFalse(canonical.has_owned_manual_control())
            self.assertIsNone(self.app._i2c_manual_voltage)
            self.assertIsNone(self.fixture.clock)
            self.assertFalse(bus.regs[0xE1] & 0x10)
            self.app.profile_failure.reset_mock()
            invoke()
        self.app.profile_failure.assert_not_called()
        self.app.finish_profile_load.assert_called_once_with("saved", state, False)
        self.assertIs(self.app.rail, canonical)

    def test_named_load_refuses_owned_manual_without_handback_until_explicit_auto(self):
        self.assert_named_profile_requires_auto(undo=False)

    def test_named_undo_refuses_owned_manual_without_handback_until_explicit_auto(self):
        self.assert_named_profile_requires_auto(undo=True)

    def test_other_controllers_do_not_acquire_a_method_selector(self):
        self.app.rail = SimpleNamespace(p=self.offset.p)
        self.app.build_i2c_control_method()
        self.ui.add_combo.assert_not_called()

    def test_real_dpg_combo_builds_and_callback_selects_recipe_without_a_write(self):
        real_dpg.create_context()
        try:
            with patch("druta.druta.dpg", real_dpg), real_dpg.window():
                self.app.build_i2c_control_method()
                config = real_dpg.get_item_configuration("i2c_control_method")
                self.assertEqual(config["items"], list(Druta.I2C_VOLTAGE_METHODS.values()))
                self.assertEqual(real_dpg.get_value("i2c_control_method"), "MP2888A (ASUS Turing)")
                config["callback"]("i2c_control_method", "MP2888A (ASUS Ampere)", None)
            self.assertIs(self.app.rail, self.manual)
            self.assertEqual(self.fixture.events, [])
        finally:
            real_dpg.destroy_context()
