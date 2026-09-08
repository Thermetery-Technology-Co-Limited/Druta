# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Profile replay tests: fake GPUs/VRMs only; no real voltage writes."""
import copy
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from druta.druta import profiles, Druta
from druta.railctl import Rail


def hardware():
    calls = []
    def writer(label):
        return Mock(side_effect=lambda *args, **kw: (calls.append((label, args, kw)) or (True, label)))
    fields = dict(reliability=0, alt_reliability=0, overvoltage=0, vmin=0,
                  _base_mv=dict(reliability=1093.75, alt_reliability=1093.75,
                                overvoltage=1125, vmin=650))
    gpu = SimpleNamespace(
        static=dict(name="TITAN test", uuid="GPU-A", slot="0000:01:00.0",
                    vbios="board-rom", driver="472.12"),
        read=Mock(return_value=dict(core_off=90, mem_off=800, pl_now_mw=300000)),
        mem_offset_scale=Mock(return_value=(8, "true MHz")),
        read_voltage_boost=Mock(return_value=100),
        read_fan_control_state=Mock(return_value={"source": "nvml", "fans": [
            {"id": 0, "manual": True, "level": 75, "policy": 1}]}),
        read_vf_curve=Mock(return_value=([dict(idx=0, delta_khz=30000)], None)),
        vfp_layout=Mock(return_value=SimpleNamespace(gpu_idx=(0,))),
        voltage_xoc_enabled=True,
        read_volt_rail_limits=Mock(return_value={0: fields, 1: copy.deepcopy(fields)}),
        volt_rail_limit_fields=Mock(return_value=("reliability", "alt_reliability", "overvoltage", "vmin")),
        volt_rail_limits_supported=Mock(return_value=True),
        abs_limit_mv=lambda row, key: row[key] + row["_base_mv"][key],
        read_rail_offset_mv=Mock(return_value=12.5),
        clkdom_layout=Mock(return_value=SimpleNamespace(nvvdd_uv=4)),
        clkdom_controls_for_ui=Mock(return_value=[0, 2, 7]),
        read_clk_domain_offsets=Mock(return_value=({0: {"freq_khz": 90000},
                                                   2: {"freq_khz": 200000},
                                                   7: {"freq_khz": 45000}}, None)))
    for label in ("set_volt_rail_limits", "set_rail_offset_mv", "set_clk_domain_offset",
                  "set_clock_offset", "set_power_limit_mw", "set_voltage_boost",
                  "restore_fan_control_state", "apply_vf_deltas"):
        setattr(gpu, label, writer(label))
    rail = SimpleNamespace(p=SimpleNamespace(src={"limits": {"ceiling": 1200}},
                           name="test regulator", rail="NVVDD", port=1, read_only=False,
                           wreg=0x23, writable={0x23}, never={}, lsb_mv=6.25,
                           raw_min=-128, raw_max=127, env_min=-200, env_max=100),
                           addr7=0x20, present=Mock(return_value=True),
                           telemetry=Mock(return_value={"offset_mv": 18.75, "vout_mv": 1100}),
                           plan=writer("I2C plan"), set_offset_mv=writer("I2C offset"))
    rail.validate_offset_mv = lambda mv, **kw: Rail.validate_offset_mv(rail, mv, **kw)
    return gpu, rail, calls


class RailProfiles(unittest.TestCase):
    def setUp(self):
        self.gpu, self.rail, self.calls = hardware()
        self.state = json.loads(json.dumps(profiles.capture(self.gpu, self.rail)))

    def test_json_round_trip_preserves_absolute_limits_fractional_offsets_and_units(self):
        state = self.state
        self.assertEqual(state["rail_limits_mv"]["0"]["reliability"], 1093.75)
        self.assertEqual(state["rail_limits_mv"]["1"]["overvoltage"], 1125)
        self.assertEqual(state["nvvdd_offset_mv"], 12.5)
        self.assertEqual(state["i2c"]["offset_mv"], 18.75)
        self.assertNotIn("vout_mv", state["i2c"])
        self.assertEqual(state["clock_domain_offsets_mhz"], {"2": 200, "7": 45})
        results = profiles.restore(self.gpu, state, rail=self.rail, i2c_verified=True)
        self.assertTrue(all(ok for ok, _ in results), results)
        self.gpu.set_volt_rail_limits.assert_any_call(0, **state["rail_limits_mv"]["0"])
        self.gpu.set_volt_rail_limits.assert_any_call(1, **state["rail_limits_mv"]["1"])
        self.gpu.set_rail_offset_mv.assert_called_once_with(12.5, 0)
        self.rail.set_offset_mv.assert_called_once_with(18.75, acknowledged=True)
        self.gpu.set_clock_offset.assert_any_call(2, 100)
        self.gpu.set_clk_domain_offset.assert_any_call(2, 200)
        self.assertEqual(self.calls[-1], ("apply_vf_deltas", ({0: 30000},), {}))

    def test_menu_names_each_restorable_control_and_xoc(self):
        summary = profiles.summarize(self.state)
        for text in ("NVVDD limits", "MSVDD limits", "NVVDD offset +12.5", "I2C NVVDD +18.75",
                     "Additional Memory Clock Offset +200", "XOC"):
            self.assertIn(text, summary)

    def test_legacy_profiles_do_not_reset_new_controls_to_zero(self):
        state = dict(schema=1, core_off_mhz=90, vf_deltas={"0": 30000})
        result = profiles.restore(self.gpu, state)
        self.assertTrue(all(ok for ok, _ in result))
        self.gpu.set_volt_rail_limits.assert_not_called()
        self.gpu.set_rail_offset_mv.assert_not_called()
        self.assertIn("per-rail settings not saved", profiles.summarize(state))

    def test_new_profile_is_bound_to_gpu_firmware_and_driver_before_any_write(self):
        for field in ("uuid", "vbios", "driver"):
            with self.subTest(field=field):
                state = copy.deepcopy(self.state)
                state["device"][field] = "other"
                results = profiles.restore(self.gpu, state, rail=self.rail, i2c_verified=True)
                self.assertFalse(results[0][0])
                self.assertIn(field, results[0][1])
                self.assertEqual(self.calls, [])

    def test_i2c_changed_limits_or_bus_cannot_be_replayed(self):
        for key in ("sha256", "addr7", "port"):
            with self.subTest(key=key):
                state = copy.deepcopy(self.state)
                state["i2c"][key] = "different"
                self.assertFalse(profiles.restore(self.gpu, state, rail=self.rail, i2c_verified=True)[0][0])
                self.assertEqual(self.calls, [])

    def test_i2c_needs_current_session_verification_even_when_identity_matches(self):
        self.assertFalse(profiles.restore(self.gpu, self.state, rail=self.rail)[0][0])
        self.assertEqual(self.calls, [])

    def test_unsupported_limit_field_and_nonfinite_voltage_refuse_entire_profile(self):
        for mutation in (lambda s: s["rail_limits_mv"]["0"].update(unconfirmed=1200),
                         lambda s: s.update(nvvdd_offset_mv=float("nan")),
                         lambda s: s["i2c"].update(offset_mv=float("inf"))):
            state = copy.deepcopy(self.state)
            mutation(state)
            self.assertFalse(profiles.restore(self.gpu, state, rail=self.rail, i2c_verified=True)[0][0])
            self.assertEqual(self.calls, [])

    def test_failed_limit_write_stops_before_offsets_i2c_or_clocks(self):
        self.gpu.set_volt_rail_limits.return_value = (False, "driver refused")
        self.gpu.set_volt_rail_limits.side_effect = None
        result = profiles.restore(self.gpu, self.state, rail=self.rail, i2c_verified=True)
        self.assertFalse(result[0][0])
        self.assertEqual(self.calls, [])

    def test_offset_storage_must_read_back_before_curve_is_applied(self):
        self.gpu.read_rail_offset_mv.side_effect = [12.5, 0]
        result = profiles.restore(self.gpu, self.state, rail=self.rail, i2c_verified=True)
        self.assertTrue(any("read-back mismatch" in message for _, message in result))
        self.gpu.apply_vf_deltas.assert_not_called()
        self.rail.set_offset_mv.assert_not_called()

    def test_i2c_dry_run_refusal_stops_before_any_i2c_or_clock_write(self):
        self.rail.plan.side_effect = None
        self.rail.plan.return_value = False, "ceiling reached"
        result = profiles.restore(self.gpu, self.state, rail=self.rail, i2c_verified=True)
        self.assertFalse(result[-1][0])
        self.rail.set_offset_mv.assert_not_called()
        self.gpu.apply_vf_deltas.assert_not_called()

    def test_unreadable_supported_controls_make_the_snapshot_incomplete(self):
        self.gpu.read_volt_rail_limits.return_value = None
        self.gpu.read_rail_offset_mv.return_value = None
        self.rail.telemetry.return_value = {}
        state = profiles.capture(self.gpu, self.rail)
        missing = "; ".join(profiles.incomplete(state))
        self.assertIn("per-rail limits", missing)
        self.assertIn("NVVDD offset", missing)
        self.assertIn("I2C offset", missing)

    def test_read_only_i2c_profile_has_no_restorable_offset(self):
        self.rail.p.read_only = True
        state = profiles.capture(self.gpu, self.rail)
        self.assertIsNone(state["i2c"])
        self.assertFalse(profiles.incomplete(state))

    def test_autosave_includes_rail_settings(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(profiles, "DIR", folder):
            name, _, missing = profiles.autosave(self.gpu, "test", self.rail)
            self.assertFalse(missing)
            self.assertEqual(profiles.load(name)["i2c"], self.state["i2c"])

    def test_pascal_additional_memory_is_captured_without_a_private_clock_pairing(self):
        self.gpu.clkdom_controls_for_ui.return_value = []
        self.gpu.clkdom_domains = Mock(return_value=[0, 2])
        self.gpu.clkdom_delta_inert = Mock(return_value=False)
        state = profiles.capture(self.gpu)
        self.assertEqual(state["clock_domain_offsets_mhz"], {"2": 200})
        self.assertIsNone(profiles.preflight(self.gpu, state))

    def test_above_normal_carryover_records_xoc_needed_to_restore_after_reboot(self):
        self.gpu.voltage_xoc_enabled = False
        self.gpu.read_volt_rail_limits.return_value[0]["overvoltage"] = 123
        state = profiles.capture(self.gpu)
        self.assertEqual(state["rail_limits_mv"]["0"]["overvoltage"], 1248)
        self.assertTrue(state["xoc"])
        self.gpu.read_volt_rail_limits.return_value[0]["overvoltage"] = 0
        self.assertIsNone(profiles.preflight(self.gpu, state))


class ProfileFlow(unittest.TestCase):
    def setUp(self):
        self.gpu, self.rail, _ = hardware()
        self.state = profiles.capture(self.gpu, self.rail)
        self.app = Druta.__new__(Druta)
        self.app.gpu, self.app.rail = self.gpu, self.rail
        self.app._i2c_busy = self.app._i2c_verified = False
        self.app._profile_pending = None
        self.app._startup_manager = SimpleNamespace(block=Mock())
        self.app.guard = Mock(return_value=True)
        for method in ("log", "sync_risk_ui", "refresh_profile_list", "sync_sliders_from_gpu",
                       "refresh_volt_limits", "vf_read", "sync_lock_ui", "sync_profile_rail_sliders"):
            setattr(self.app, method, Mock())
        self.app.autosave_before = Mock(return_value=True)

    @patch("druta.druta.dpg.configure_item")
    @patch("druta.druta.dpg.set_value")
    def test_startup_waits_for_i2c_verify_and_restores_saved_modes(self, values, _configure):
        self.app.verify_i2c_rail = lambda: setattr(self.app, "_i2c_busy", True)
        self.app.begin_profile_load("test", self.state, automatic=True)
        self.gpu.apply_vf_deltas.assert_not_called()
        values.assert_any_call("xoc_mode", True)
        values.assert_any_call("vlim_mode", True)
        values.assert_any_call("i2c_mode", True)
        self.app.poll_profile_load()
        self.gpu.apply_vf_deltas.assert_not_called()
        self.app._i2c_busy, self.app._i2c_verified = False, True
        self.app._i2c_verified_for = self.app.i2c_connection()
        self.app.poll_profile_load()
        self.gpu.apply_vf_deltas.assert_called_once()
        self.app._startup_manager.block.assert_not_called()

    @patch("druta.druta.dpg.configure_item")
    @patch("druta.druta.dpg.set_value")
    def test_failed_verification_blocks_startup_without_applying(self, *_):
        self.app.verify_i2c_rail = lambda: setattr(self.app, "_i2c_busy", True)
        self.app.begin_profile_load("test", self.state, automatic=True)
        self.app._i2c_busy = False
        self.app.poll_profile_load()
        self.gpu.apply_vf_deltas.assert_not_called()
        self.app._startup_manager.block.assert_called_once()

    def test_startup_refuses_missing_undo_capture(self):
        self.app.autosave_before.return_value = False
        self.app.begin_profile_load("test", self.state, automatic=True)
        self.gpu.set_volt_rail_limits.assert_not_called()
        self.app._startup_manager.block.assert_called_once()

    def test_switch_refused_during_profile_verification(self):
        self.app._profile_pending = ("test", self.state, True)
        with patch("druta.druta.GPU") as constructor:
            self.assertFalse(self.app.swap_gpu("0000:02:00.0"))
        constructor.assert_not_called()

    def test_reset_refused_during_profile_verification(self):
        self.app._profile_pending = ("test", self.state, True)
        self.app.reset_all()
        self.app.autosave_before.assert_not_called()

    @patch("druta.druta.dpg.set_value")
    @patch("druta.druta.dpg.does_item_exist", return_value=True)
    def test_rail_and_additional_memory_widgets_follow_live_readback(self, _exists, value):
        Druta.sync_profile_rail_sliders(self.app)
        value.assert_any_call("sl_rail", 12.5)
        value.assert_any_call("in_rail", 12.5)
        value.assert_any_call("sl_memdom", 200)
        value.assert_any_call("sl_i2crail", 18.75)


if __name__ == "__main__":
    unittest.main()
