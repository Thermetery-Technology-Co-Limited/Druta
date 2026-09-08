# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Legacy P0 UI ownership and rollback tests without GPU or GUI initialization."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from druta.druta import Druta
from druta.nvbackend import GPU, ResetStep


class LegacyP0UiTests(unittest.TestCase):
    def test_p0_copy_uses_current_cards_measured_behavior(self):
        from tests.test_legacy_p0 import card
        from druta.nvbackend import GPU
        gpu = card()
        self.app.gpu = gpu
        text = self.app.legacy_p0_measurement()
        self.assertIn("GTX 745", text)
        self.assertIn("540 MHz", text)
        self.assertIn("1072 MHz", text)
        gpu.arch.return_value = GPU.ARCH_KEPLER
        gpu.nvapi.selected = {"devid": 0x1188, "subsys": 0x84061043}
        gpu.static = {"driver": "472.12", "vbios": "80.04.1e.00.18"}
        text = self.app.legacy_p0_measurement()
        self.assertIn("GTX 690", text)
        self.assertIn("705 MHz", text)
        self.assertIn("1201 MHz", text)
        self.assertNotIn("GTX 745", text)
        gpu.static["driver"] = "unknown"
        self.assertNotIn("705 MHz", self.app.legacy_p0_measurement())

    def setUp(self):
        for name, kwargs in (("does_item_exist", {"return_value": False}),
                             ("set_value", {}), ("configure_item", {})):
            p = patch("druta.druta.dpg." + name, **kwargs)
            setattr(self, name, p.start())
            self.addCleanup(p.stop)
        self.events = []
        self.owned = False
        self.release_ok = True
        self.app = Druta.__new__(Druta)
        self.app._clk_lock = None
        self.app.guard = Mock(return_value=True)
        self.app.log = Mock()
        self.app.report = Mock()
        self.app.sync_knob_boxes = Mock()
        self.app.autosave_before = Mock(side_effect=self.capture)
        self.gpu = self.app.gpu = SimpleNamespace(
            legacy_p0_supported=Mock(return_value=True),
            legacy_p0_owned=Mock(side_effect=lambda: self.owned),
            hold_legacy_p0=Mock(side_effect=self.hold),
            release_legacy_p0=Mock(side_effect=self.release),
            fan_capabilities=Mock(return_value={"manual": True, "auto": True}),
            set_fan=Mock(side_effect=self.fan),
            set_clock_offset=Mock(), set_power_limit_mw=Mock(),
            set_voltage_boost=Mock(), apply_vf_deltas=Mock(),
            reset_gpu_clocks=Mock(), clear_vf_lock=Mock(), static={})

    def capture(self, _action):
        self.events.append("capture")
        return True

    def hold(self):
        self.events.append("hold")
        self.owned = True
        return True, "P0 held"

    def release(self):
        self.events.append("release")
        if self.release_ok:
            self.owned = False
        return self.release_ok, "release result"

    def fan(self, duty):
        self.events.append(("fan", duty))
        return True, "fan written"

    def assert_no_tune_writes(self):
        for method in ("set_clock_offset", "set_power_limit_mw",
                       "set_voltage_boost", "apply_vf_deltas"):
            getattr(self.gpu, method).assert_not_called()

    def test_unsupported_read_only_or_missing_fan_never_writes(self):
        for refusal in ("support", "gate", "fan"):
            with self.subTest(refusal=refusal):
                self.gpu.legacy_p0_supported.return_value = refusal != "support"
                self.app.guard.return_value = refusal != "gate"
                self.gpu.fan_capabilities.return_value["manual"] = refusal != "fan"
                self.app.lock_p0_and_max_fan()
                self.assertEqual(self.events, [])
                self.assert_no_tune_writes()

    def test_missing_undo_capture_refuses_before_hold_or_fan(self):
        self.app.autosave_before.side_effect = None
        self.app.autosave_before.return_value = False
        self.app.lock_p0_and_max_fan()
        self.gpu.hold_legacy_p0.assert_not_called()
        self.gpu.set_fan.assert_not_called()
        self.assert_no_tune_writes()

    def test_success_captures_then_holds_then_writes_only_fan(self):
        self.does_item_exist.side_effect = lambda tag: tag == "sl_fan"
        self.app.lock_p0_and_max_fan()
        self.assertEqual(self.events, ["capture", "hold", ("fan", 100)])
        self.assertEqual(self.app._clk_lock, {"kind": Druta.LOCK_P0, "verified": True})
        self.set_value.assert_called_once_with("sl_fan", 100)
        self.assert_no_tune_writes()

    def test_failed_hold_does_not_write_fan_and_tracks_failed_cleanup(self):
        def fail_hold():
            self.owned = True  # Backend could not release after verification failed.
            return False, "verification and cleanup failed"
        self.gpu.hold_legacy_p0.side_effect = fail_hold
        self.app.lock_p0_and_max_fan()
        self.gpu.set_fan.assert_not_called()
        self.assertEqual(self.app._clk_lock, {"kind": Druta.LOCK_P0, "verified": False})
        self.assert_no_tune_writes()

    def test_failed_hold_with_successful_cleanup_has_no_indicator(self):
        self.gpu.hold_legacy_p0.side_effect = None
        self.gpu.hold_legacy_p0.return_value = False, "hold failed and released"
        self.app.lock_p0_and_max_fan()
        self.gpu.set_fan.assert_not_called()
        self.assertIsNone(self.app._clk_lock)

    def test_failed_fan_releases_new_hold_but_preserves_existing_hold(self):
        self.gpu.set_fan.side_effect = None
        self.gpu.set_fan.return_value = False, "fan failed"
        self.app.lock_p0_and_max_fan()
        self.gpu.release_legacy_p0.assert_called_once()
        self.assertFalse(self.owned)
        self.assertIsNone(self.app._clk_lock)
        self.gpu.release_legacy_p0.reset_mock()
        self.owned = True
        self.app._clk_lock = {"kind": Druta.LOCK_P0, "verified": True}
        self.app.lock_p0_and_max_fan()
        self.gpu.release_legacy_p0.assert_not_called()
        self.assertTrue(self.owned)
        self.assertIsNotNone(self.app._clk_lock)

    def test_failed_fan_and_release_keep_indicator(self):
        self.gpu.set_fan.side_effect = RuntimeError("fan failure")
        self.release_ok = False
        self.app.lock_p0_and_max_fan()
        self.assertTrue(self.owned)
        self.assertEqual(self.app._clk_lock["kind"], Druta.LOCK_P0)

    def test_p0_release_without_hold_never_dispatches_nvml(self):
        self.app.release_p0()
        self.gpu.release_legacy_p0.assert_called_once()
        self.gpu.reset_gpu_clocks.assert_not_called()
        self.assertIsNone(self.app._clk_lock)

    def test_release_is_gated_and_failed_release_keeps_indicator(self):
        self.owned = True
        self.app._clk_lock = {"kind": Druta.LOCK_P0, "verified": True}
        self.app.guard.return_value = False
        self.app.release_p0()
        self.gpu.release_legacy_p0.assert_not_called()
        self.app.guard.return_value = True
        self.release_ok = False
        self.app.release_p0()
        self.assertEqual(self.app._clk_lock["kind"], Druta.LOCK_P0)
        self.release_ok = True
        self.app.release_p0()
        self.assertIsNone(self.app._clk_lock)

    def test_exit_releases_owned_p0_even_when_controls_are_locked(self):
        self.owned = True
        self.app._clk_lock = {"kind": Druta.LOCK_P0, "verified": True}
        self.app.guard.return_value = False
        with patch("builtins.print"):
            self.app.release_on_exit()
        self.gpu.release_legacy_p0.assert_called_once()
        self.gpu.reset_gpu_clocks.assert_not_called()
        self.assertIsNone(self.app._clk_lock)

    def test_failed_exit_release_keeps_ownership(self):
        self.owned = True
        self.release_ok = False
        self.app._clk_lock = {"kind": Druta.LOCK_P0, "verified": True}
        with patch("builtins.print"):
            self.app.release_on_exit()
        self.assertTrue(self.owned)
        self.assertEqual(self.app._clk_lock["kind"], Druta.LOCK_P0)

    def test_card_switch_refuses_while_p0_is_owned(self):
        self.gpu.slot = Mock(return_value="0000:02:00.0")
        self.app.gpu_list = [{"slot": "0000:01:00.0", "name": "RTX"}]
        self.app._clk_lock = {"kind": Druta.LOCK_P0, "verified": True}
        self.app.swap_gpu = Mock()
        self.app.switch_gpu(user_data="0000:01:00.0")
        self.app.swap_gpu.assert_not_called()
        self.assertEqual(self.app._clk_lock["kind"], Druta.LOCK_P0)

    def test_reset_cannot_clear_indicator_while_backend_still_owns_p0(self):
        self.app._reset_armed = True
        self.app.rail = None
        self.app.refresh_volt_limits = Mock()
        self.app.vf_read = Mock()
        self.gpu.read_voltage_boost = Mock(return_value=None)
        self.gpu.reset_all = Mock(return_value=[ResetStep(GPU.P0_LOCK_STEP, (False, "failed"))])
        self.owned = True
        self.app._clk_lock = {"kind": Druta.LOCK_P0, "verified": True}
        self.app.reset_all()
        self.assertEqual(self.app._clk_lock["kind"], Druta.LOCK_P0)
        self.app._reset_armed = True
        self.owned = False
        self.gpu.reset_all.return_value = [ResetStep(GPU.P0_LOCK_STEP, (True, "released"))]
        self.app.reset_all()
        self.assertIsNone(self.app._clk_lock)


if __name__ == "__main__":
    unittest.main()
