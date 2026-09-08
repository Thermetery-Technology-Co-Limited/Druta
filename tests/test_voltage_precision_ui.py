# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Voltage request precision through fake widgets and in-memory transports."""
import ctypes
from unittest.mock import Mock

from druta import ncp4206
from druta.druta import Druta, KnobRange
from druta.nvbackend import CLKDOM_LAYOUT_TURING, GPU, i32
from tests.test_arch_ui_regressions import FakeUiTest
from tests import test_ncp4206
from tests.test_verification_lifecycle import offset_rail
from tests.test_volt_rails import fake_gpu


def offset_gpu():
    gpu = fake_gpu("pascal")
    layout = CLKDOM_LAYOUT_TURING
    gpu.clkdom_ok = Mock(return_value=True)
    gpu.clkdom_layout = Mock(return_value=layout)
    current = [12500]
    dw = (layout.header + layout.nvvdd_uv) // 4

    def get(_mask):
        buf = (ctypes.c_ubyte * gpu._CLKDOM_BUF)()
        ctypes.cast(buf, ctypes.POINTER(i32))[dw] = current[0]
        return 0, buf

    def write(_handle, buf):
        current[0] = ctypes.cast(buf, ctypes.POINTER(i32))[dw]
        return 0

    gpu._clkdom_get = get
    gpu.nvapi.ClkDomCtlSet = Mock(side_effect=write)
    gpu.read_clk_domain_offsets = Mock(return_value=(None, "no extra domains"))
    gpu.read_vf_lock = Mock(return_value=None)
    return gpu, current


class VoltagePrecisionUiTests(FakeUiTest):
    def setUp(self):
        super().setUp()
        self.app._knob_sync = False
        self.app._xoc_bounds = False
        self.app._carryover_hi = {}
        self.app.rail = None
        for key, value, lo, hi in (("rail", 12.5, -100, 200),
                                   ("i2crail", 18.75, -100, 100),
                                   ("vlim_rel", 1068.75, 650, 1200)):
            self.values["sl_" + key] = self.values["in_" + key] = value
            self.app._slider_ranges[key] = KnobRange(key, lo, hi, -500, 1500)

    def test_nvvdd_fractional_readback_apply_and_reapply_preserve_wire_microvolts(self):
        self.app.gpu, wire = offset_gpu()
        self.app.sync_profile_rail_sliders()
        self.assertEqual(self.values["sl_rail"], 12.5)
        self.assertEqual(self.values["in_rail"], 12.5)
        for mv, uv in ((12.5, 12500), (6.25, 6250), (-12.5, -12500),
                       (12.501, 12501)):
            with self.subTest(mv=mv):
                # DPG float widgets use a C float internally. The UI restores
                # its harmless representation error to exact wire microvolts.
                self.app.apply_rail(ctypes.c_float(mv).value)
                self.assertEqual(wire[0], uv)
                self.assertEqual(self.values["in_rail"], mv)
                self.app.sync_profile_rail_sliders()
                self.app.apply_rail(self.values["sl_rail"])
                self.assertEqual(wire[0], uv)

    def test_offset_and_limit_drag_type_mirror_and_range_refresh_keep_fractions(self):
        for key, value in (("rail", 12.5), ("vlim_rel", 1068.75)):
            for action in ("drag", "type"):
                with self.subTest(key=key, action=action):
                    self.values[("sl_" if action == "drag" else "in_") + key] = value
                    (self.app.knob_dragged if action == "drag" else self.app.knob_typed)(key)
                    for xoc in (True, False):
                        self.app.sync_slider_ranges(xoc)
                        self.assertEqual(self.values["sl_" + key], value)
                        self.assertEqual(self.values["in_" + key], value)

    def test_every_voltage_row_is_float_while_other_knobs_remain_integer(self):
        self.app._knob_cb = {}
        self.app._ctl_widgets = []
        self.app.bind = Mock()
        self.app.s = lambda value: value
        for key in ("rail", "i2crail", "vlim_rel", "vlim1_alt", "core", "volt", "pl", "fan"):
            self.app.slider_row(key, key, 0, 1500, 12.5, Mock())
        floats = {call.kwargs["tag"] for call in self.ui.add_input_float.call_args_list}
        ints = {call.kwargs["tag"] for call in self.ui.add_input_int.call_args_list}
        self.assertEqual(floats, {"in_rail", "in_i2crail", "in_vlim_rel", "in_vlim1_alt", "in_pl"})
        self.assertEqual(ints, {"in_core", "in_volt", "in_fan"})

    def test_fractional_limits_preserve_all_fields_on_both_rails_through_real_setter(self):
        self.app.gpu = fake_gpu("blackwell")
        self.app.gpu.volt_limits_write_enabled = True
        self.app.gpu.read_rail_offset_mv = Mock(return_value=None)
        self.app.sync_vcap_to_ceiling = Mock()
        for rail in (0, 1):
            for short, field in (("rel", "reliability"), ("alt", "alt_reliability"),
                                  ("ov", "overvoltage"), ("lo", "vmin")):
                with self.subTest(rail=rail, field=field):
                    key = f"vlim{'1' if rail else ''}_{short}"
                    self.values["sl_" + key] = self.values["in_" + key] = 0
                    self.app.apply_vlim(rail, **{field: 1068.75})
                    raw = self.app.gpu.read_volt_rail_limits()[rail]
                    self.assertEqual(GPU.abs_limit_mv(raw, field), 1068.75)
                    self.assertEqual(self.values["sl_" + key], 1068.75)
                    self.assertEqual(self.values["in_" + key], 1068.75)
                    self.app.apply_vlim(rail, **{field: self.values["sl_" + key]})
                    self.assertEqual(GPU.abs_limit_mv(self.app.gpu.read_volt_rail_limits()[rail],
                                                     field), 1068.75)

    def test_fractional_over_normal_carryover_does_not_clamp_the_request(self):
        self.app.gpu = fake_gpu("pascal")
        self.app.gpu.volt_limits_write_enabled = self.app.gpu.voltage_xoc_enabled = True
        self.assertTrue(self.app.gpu.set_volt_rail_limits(0, reliability=1200.25)[0])
        self.app.gpu.read_rail_offset_mv = Mock(return_value=200.25)
        self.app.ov_carryover(self.app.gpu.read_volt_rail_limits())
        self.values.update(sl_rail=200.25, in_rail=200.25,
                           sl_vlim_rel=1200.25, in_vlim_rel=1200.25)
        self.app.sync_slider_ranges(False)
        self.assertEqual(self.values["sl_rail"], 200.25)
        self.assertEqual(self.values["in_vlim_rel"], 1200.25)
        self.assertEqual(self.app.knob_bounds("vlim_rel")[1], 1200.25)

    def test_linked_limits_and_microvolt_widget_roundoff_keep_exact_request(self):
        self.app.gpu = fake_gpu("pascal")
        self.app.gpu.volt_limits_write_enabled = True
        self.app.gpu.read_rail_offset_mv = Mock(return_value=None)
        self.app.sync_vcap_to_ceiling = Mock()
        self.values["vlim_link"] = True
        self.app.apply_vlim(0, reliability=ctypes.c_float(1068.751).value)
        raw = self.app.gpu.read_volt_rail_limits()[0]
        for field in self.app.VLIM_LINKED:
            self.assertEqual(GPU.abs_limit_mv(raw, field), 1068.751)
        self.assertEqual(self.values["in_vlim_rel"], 1068.751)
        self.assertIn("1068.751", self.app.volt_limits_cells({0: raw}, 0)[1])

    def test_negative_nvvdd_carryover_keeps_exact_floor_and_only_returns_toward_normal(self):
        self.app.gpu, wire = offset_gpu()
        wire[0] = -100250
        self.values.update(sl_rail=-100.25, in_rail=-100.25)
        self.app.ov_carryover(None)
        self.app.sync_slider_ranges(False)
        self.assertEqual(self.values["sl_rail"], -100.25)
        self.assertEqual(self.values["in_rail"], -100.25)
        self.assertEqual(self.app.knob_bounds("rail"), (-100.25, 200))
        self.values["in_rail"] = -101
        self.app.knob_typed("rail")
        self.assertEqual(self.values["sl_rail"], -100.25)
        self.app.apply_rail(self.values["sl_rail"])
        self.assertTrue(self.app.report.call_args.args[0][0])
        self.assertEqual(wire[0], -100250)
        self.app.gpu.nvapi.ClkDomCtlSet.assert_not_called()  # Exact no-op.
        self.app.apply_rail(-100.5)  # Direct callback cannot bypass the live bound.
        self.assertFalse(self.app.report.call_args.args[0][0])
        self.assertEqual(wire[0], -100250)
        self.app.apply_rail(-100.125)
        self.assertTrue(self.app.report.call_args.args[0][0])
        self.assertEqual(wire[0], -100125)
        self.app.ov_carryover(None)
        self.app.sync_slider_ranges(False)
        self.assertEqual(self.app.knob_bounds("rail")[0], -100.125)
        self.app.apply_rail(-100.25)
        self.assertFalse(self.app.report.call_args.args[0][0])
        self.assertEqual(wire[0], -100125)
        self.app.apply_rail(-100)
        self.app.ov_carryover(None)
        self.app.sync_slider_ranges(False)
        self.assertEqual(self.app.knob_bounds("rail")[0], -100)
        self.assertEqual(wire[0], -100000)

    def test_missing_nvvdd_read_keeps_last_fractional_carryover_endpoint(self):
        self.app.gpu, wire = offset_gpu()
        wire[0] = -100250
        self.app.ov_carryover(None)
        self.app.gpu.read_rail_offset_mv = Mock(return_value=None)
        self.app.ov_carryover(None)
        self.values.update(sl_rail=-100.25, in_rail=-100.25)
        self.app.sync_slider_ranges(False)
        self.assertEqual(self.values["sl_rail"], -100.25)
        self.assertEqual(self.app.knob_bounds("rail")[0], -100.25)

    def test_i2c_offset_uses_recipe_grid_and_preserves_fractional_readback(self):
        self.app.gpu, _ = offset_gpu()
        self.app.rail = rail = offset_rail()
        self.app.i2c_gate = Mock(return_value=(True, ""))
        self.app.i2c_verified = Mock(return_value=True)
        for requested, expected in ((18.75, 18.75), (19, 18.75), (-19, -18.75)):
            with self.subTest(requested=requested):
                self.values["in_i2crail"] = requested
                self.app.knob_typed("i2crail")
                self.assertEqual(self.values["sl_i2crail"], expected)
                self.app.apply_i2c_rail(self.values["sl_i2crail"])
                self.assertTrue(self.app.report.call_args.args[0][0])
                self.assertEqual(rail.telemetry()["offset_mv"], expected)
                self.assertEqual(self.values["in_i2crail"], expected)
                self.app.apply_i2c_rail(self.values["sl_i2crail"])
                self.assertEqual(rail.telemetry()["offset_mv"], expected)

    def test_ncp_absolute_target_floors_to_vid_and_reapply_keeps_command(self):
        self.app.gpu, _ = offset_gpu()
        self.app.rail = rail = test_ncp4206.NCPTests().rail()
        self.app.i2c_gate = Mock(return_value=(True, ""))
        self.app.i2c_verified = Mock(return_value=True)
        self.app._slider_ranges["i2crail"] = KnobRange("VID", 800, 1281, 800, 1600)
        for requested in (1068.75, 1070):
            with self.subTest(requested=requested):
                self.values["in_i2crail"] = requested
                self.app.knob_typed("i2crail")
                self.assertEqual(self.values["sl_i2crail"], 1068.75)
                self.app.apply_i2c_rail(self.values["sl_i2crail"])
                self.assertTrue(self.app.report.call_args.args[0][0])
                self.assertEqual(ncp4206.decode_vid(rail.regs[0x21]), 1068.75)
                self.assertEqual(self.values["in_i2crail"], 1068.75)
                command = rail.regs[0x21]
                self.app.apply_i2c_rail(self.values["sl_i2crail"])
                self.assertEqual(rail.regs[0x21], command)

    def test_i2c_rounding_past_custom_envelope_is_refused_before_write(self):
        self.app.gpu, _ = offset_gpu()
        self.app.rail = rail = offset_rail()
        rail.xoc = False
        rail.p.env_max = 18.7
        self.app.i2c_gate = Mock(return_value=(True, ""))
        self.app.i2c_verified = Mock(return_value=True)
        self.app.apply_i2c_rail(18.7)  # Encodes as18.75, outside the custom bound.
        self.assertEqual(rail.writes, [])
        self.app.autosave_before.assert_not_called()

    def test_invalid_voltage_inputs_never_reach_driver(self):
        self.app.gpu, _ = offset_gpu()
        self.app.gpu.set_volt_rail_limits = Mock()
        for invalid in (True, float("nan"), float("inf"), "invalid"):
            with self.subTest(invalid=invalid):
                self.app.apply_rail(invalid)
                self.app.apply_vlim(0, reliability=invalid)
        self.app.gpu.nvapi.ClkDomCtlSet.assert_not_called()
        self.app.gpu.set_volt_rail_limits.assert_not_called()

    def test_limit_readout_keeps_fractional_requested_and_live_values(self):
        gpu = fake_gpu("pascal")
        cells = self.app.volt_limits_cells(gpu.read_volt_rail_limits(), 0,
                                            gpu.read_volt_rail_state())
        self.assertIn("1062.5", cells[1])
        self.assertIn("1093.75", cells[1])
        self.assertIn("1068.75", cells[4])
