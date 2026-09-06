# Druta - hardware-free tests for per-adapter voltage rail support.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later

"""Test the guards and packet handling without loading a GPU driver.

Hardware evidence lives in docs/rail-probes. These tests protect its software
consequences: a readable zero delta is valid, rail presence is positional,
defaults belong to an adapter, and writes must preserve unrelated settings.
Even the escape-hook tests use Python-owned buffers and fake DLL functions.
"""

import copy
import ctypes
import math
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from nvbackend import GPU, i32, u32


ADAPTERS = {
    "turing": {
        "name": "NVIDIA TITAN RTX", "devid": 0x1E02, "subsys": 312676574,
        "vbios": "90.02.1E.00.02", "driver": "580.97",
    },
    "pascal": {
        "name": "NVIDIA TITAN Xp", "devid": 0x1B02, "subsys": 299831518,
        "vbios": "86.02.3D.00.01", "driver": "580.97",
    },
    "blackwell": {
        "name": "NVIDIA GeForce RTX 5080", "devid": 0x2C02, "subsys": 0,
        "vbios": "test", "driver": "test",
    },
}


class FakeRailAPI:
    """Singleton getters; zero is a real NVVDD control record at stock."""

    def __init__(self, identity, rails):
        self.ok = True
        self.gpu = object()
        self.selected = {k: identity[k] for k in ("devid", "subsys")}
        self.control = {r: [0, 0, 0, 0] for r in rails}
        self.live = {r: [0, 800000, 1068750, 1093750, 1125000,
                         1068750, 650000] for r in rails}
        self.calls = []
        self.escape_hook = None

    def VoltRailsCtlGet(self, handle, buf):
        if self.escape_hook is not None:
            return self.escape_hook(handle, buf)
        return self._get("control", handle, buf, 0x00020AC8)

    def VoltRailsAbs(self, handle, buf):
        return self._get("live", handle, buf, GPU.LIVE_RAIL_VER)

    def _get(self, kind, handle, buf, version):
        assert handle is self.gpu
        pu = ctypes.cast(buf, ctypes.POINTER(u32))
        self.calls.append((kind, pu[0], pu[1]))
        if pu[0] != version or pu[1] not in (1, 2):
            return -8
        rail = 0 if pu[1] == 1 else 1
        records = getattr(self, kind)
        if rail not in records:
            return -8
        base = (0x48 + rail * 0x54) // 4
        values = ([0] + records[rail] if kind == "control"
                  else records[rail])
        pi = ctypes.cast(buf, ctypes.POINTER(i32))
        for index, value in enumerate(values):
            pi[base + index] = value
        return 0


def fake_gpu(kind="turing", **identity_changes):
    identity = dict(ADAPTERS[kind], **identity_changes)
    api = FakeRailAPI(identity, (0, 1) if kind == "blackwell" else (0,))
    with patch("nvbackend.NvAPI", return_value=api), \
            patch("nvbackend.Nvml", return_value=SimpleNamespace(
                selected=dict(api.selected))), \
            patch.object(GPU, "_read_static", return_value=identity):
        gpu = GPU("0000:01:00.0")
    gpu.read_voltage_boost = Mock(return_value=37)

    def store_records(records):
        api.control = copy.deepcopy(records)
        return True, 0

    # The default fixture can never reach the native escape implementation.
    gpu._write_rail_records = Mock(side_effect=store_records)
    return gpu


class VoltageModeTests(unittest.TestCase):
    def test_all_rail_fields_use_normal_and_xoc_bounds(self):
        for kind in ADAPTERS:
            for field in GPU.VOLT_LIMIT_FIELDS:
                with self.subTest(card=kind, field=field):
                    gpu = fake_gpu(kind)
                    gpu.volt_limits_write_enabled = True
                    self.assertTrue(gpu.set_volt_rail_limits(0, **{field: 1200})[0])
                    gpu._write_rail_records.reset_mock()
                    self.assertFalse(gpu.set_volt_rail_limits(0, **{field: 1201})[0])
                    gpu._write_rail_records.assert_not_called()
                    gpu.voltage_xoc_enabled = True
                    self.assertTrue(gpu.set_volt_rail_limits(0, **{field: 1500})[0])
                    gpu._write_rail_records.reset_mock()
                    self.assertFalse(gpu.set_volt_rail_limits(0, **{field: 1501})[0])
                    gpu._write_rail_records.assert_not_called()
                    gpu.voltage_xoc_enabled = False
                    self.assertTrue(gpu.set_volt_rail_limits(0, **{field: 1400})[0])
                    gpu._write_rail_records.reset_mock()
                    self.assertFalse(gpu.set_volt_rail_limits(0, **{field: 1401})[0])
                    gpu._write_rail_records.assert_not_called()

    def test_xoc_is_owned_by_each_gpu(self):
        first = fake_gpu()
        first.voltage_xoc_enabled = True
        with patch.object(GPU, "voltage_xoc_enabled", True):
            second = fake_gpu("pascal")
        self.assertFalse(second.voltage_xoc_enabled)

    def test_offset_bounds_and_carryover_reject_before_dispatch(self):
        from nvbackend import CLKDOM_LAYOUT_TURING
        gpu = fake_gpu()
        layout = CLKDOM_LAYOUT_TURING
        gpu.clkdom_ok = Mock(return_value=True)
        gpu.clkdom_layout = Mock(return_value=layout)
        current = [0]
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
        self.assertTrue(gpu.set_rail_offset_mv(200)[0])
        self.assertEqual(current[0], 200000)
        gpu.nvapi.ClkDomCtlSet.reset_mock()
        self.assertFalse(gpu.set_rail_offset_mv(201)[0])
        gpu.nvapi.ClkDomCtlSet.assert_not_called()
        gpu.voltage_xoc_enabled = True
        self.assertTrue(gpu.set_rail_offset_mv(500)[0])
        self.assertEqual(current[0], 500000)
        gpu.nvapi.ClkDomCtlSet.reset_mock()
        self.assertFalse(gpu.set_rail_offset_mv(501)[0])
        gpu.nvapi.ClkDomCtlSet.assert_not_called()
        gpu.voltage_xoc_enabled = False
        self.assertTrue(gpu.set_rail_offset_mv(400)[0])
        gpu.nvapi.ClkDomCtlSet.reset_mock()
        self.assertFalse(gpu.set_rail_offset_mv(401)[0])
        gpu.nvapi.ClkDomCtlSet.assert_not_called()


class RailReadTests(unittest.TestCase):
    def test_zero_control_record_is_present_and_combined_mask_is_never_used(self):
        gpu = fake_gpu()
        fields = gpu.read_volt_rail_limits()
        self.assertEqual(set(fields), {0})
        self.assertEqual(fields[0]["type"], 0)
        self.assertEqual([fields[0][k] for k in GPU.VOLT_LIMIT_FIELDS],
                         [0.0, 0.0, 0.0, 0.0])
        self.assertEqual(gpu.nvapi.calls,
                         [("control", 0x00020AC8, 1),
                          ("control", 0x00020AC8, 2)])
        self.assertTrue(gpu.volt_rail_limits_supported())
        self.assertEqual(gpu.nvapi.calls[-1], ("control", 0x00020AC8, 1))

    def test_live_rail_uses_its_own_mask_cache_and_positional_identity(self):
        gpu = fake_gpu()
        gpu.read_volt_rail_limits()
        state = gpu.read_volt_rail_state()
        self.assertEqual(set(state), {0})
        self.assertEqual(state[0]["type"], 0)
        self.assertEqual(state[0]["live"], 800.0)
        self.assertEqual(gpu.nvapi.calls[-2:],
                         [("live", GPU.LIVE_RAIL_VER, 1),
                          ("live", GPU.LIVE_RAIL_VER, 2)])
        self.assertIsNone(gpu.read_rail_live_mv(1))

    def test_all_zero_live_record_is_not_reported_as_zero_volts(self):
        gpu = fake_gpu()
        gpu.nvapi.live[0] = [0] * len(GPU.LIVE_RAIL_FIELDS)
        self.assertIsNone(gpu.read_volt_rail_state())
        self.assertIn(0, gpu.read_volt_rail_limits())

    def test_success_with_wrong_version_is_not_support(self):
        gpu = fake_gpu()

        def wrong_version(handle, buf):
            ctypes.cast(buf, ctypes.POINTER(u32))[0] = 0
            return 0

        gpu.nvapi.VoltRailsCtlGet = wrong_version
        self.assertIsNone(gpu.read_volt_rail_limits())
        self.assertFalse(gpu.volt_rail_limits_supported())

    def test_expected_rail_must_still_be_readable(self):
        gpu = fake_gpu("blackwell")
        del gpu.nvapi.control[1]
        self.assertFalse(gpu.volt_rail_limits_supported())


class RailProfileTests(unittest.TestCase):
    def test_each_identity_component_gates_titan_writes_and_reset(self):
        changes = ({"devid": 0x1E04}, {"subsys": 0},
                   {"vbios": "90.02.1e.00.03"}, {"driver": "591.44"})
        for identity in changes:
            with self.subTest(identity=identity):
                gpu = fake_gpu(**identity)
                self.assertIn(0, gpu.read_volt_rail_limits())
                self.assertFalse(gpu.volt_rail_limits_supported())
                self.assertEqual(gpu.volt_rail_limit_fields(0), ())
                gpu.volt_limits_write_enabled = True
                self.assertFalse(gpu.set_volt_rail_limits(
                    0, reliability=1000)[0])
                self.assertFalse(gpu.reset_volt_rail_limits()[0])
                gpu._write_rail_records.assert_not_called()

    def test_per_card_bases_stock_bounds_and_headroom(self):
        expected = {
            "turing": ([1068.75, 1093.75, 1125.0, 650.0], 25.0, 1200.0),
            "pascal": ([1062.5, 1093.75, 1200.0, 650.0], 31.25, 1200.0),
            "blackwell": ([1040.0, 1060.0, 1200.0, 800.0], 20.0, 1200.0),
        }
        for kind, (values, headroom, maximum) in expected.items():
            with self.subTest(card=kind):
                gpu = fake_gpu(kind)
                raw = gpu.read_volt_rail_limits()[0]
                self.assertEqual([gpu.abs_limit_mv(raw, k)
                                  for k in GPU.VOLT_LIMIT_FIELDS], values)
                self.assertEqual([gpu.stock_limit_mv(0, k)
                                  for k in GPU.VOLT_LIMIT_FIELDS], values)
                self.assertEqual(raw["_headroom_mv"], headroom)
                self.assertEqual(gpu.VOLT_LIMIT_MAX_MV, maximum)
                raw["reliability"] = -100.0
                self.assertEqual(gpu.rail_ceiling_mv(raw),
                                 values[0] - 100.0 + headroom)
                self.assertEqual(gpu.rail_floor_mv(raw), values[3])

    def test_absolute_view_only_contains_limits(self):
        gpu = fake_gpu("pascal")
        absolute = gpu.volt_rail_limits_mv()
        self.assertEqual(absolute, {0: dict(zip(
            GPU.VOLT_LIMIT_FIELDS, [1062.5, 1093.75, 1200.0, 650.0]))})

    def test_unknown_card_does_not_borrow_blackwell_conversions(self):
        gpu = fake_gpu(driver="unvalidated")
        fields = gpu.read_volt_rail_limits()[0]
        self.assertTrue(math.isnan(gpu.abs_limit_mv(fields, "reliability")))
        self.assertTrue(math.isnan(gpu.rail_ceiling_mv(fields)))

    def test_only_present_rails_expose_confirmed_fields(self):
        for kind in ("turing", "pascal", "blackwell"):
            with self.subTest(card=kind):
                gpu = fake_gpu(kind)
                self.assertEqual(gpu.volt_rail_limit_fields(0),
                                 GPU.VOLT_LIMIT_FIELDS)
                self.assertEqual(gpu.volt_rail_limit_fields(1),
                                 GPU.VOLT_LIMIT_FIELDS
                                 if kind == "blackwell" else ())
                self.assertEqual(gpu.volt_rail_limit_fields(2), ())

    def test_instances_do_not_inherit_previous_cards_write_permissions(self):
        first = fake_gpu("blackwell")
        first.volt_limits_write_enabled = True
        first.msvdd_write_enabled = True
        # A stale process-wide flag must not enable a freshly selected GPU.
        with patch.object(GPU, "volt_limits_write_enabled", True), \
                patch.object(GPU, "msvdd_write_enabled", True):
            second = fake_gpu("turing")
        self.assertFalse(second.volt_limits_write_enabled)
        self.assertFalse(second.msvdd_write_enabled)
        self.assertEqual(set(second.VOLT_LIMIT_POWERON), {0})
        self.assertEqual(set(first.VOLT_LIMIT_POWERON), {0, 1})
        self.assertEqual(first.stock_limit_mv(1, "reliability"), 990.0)


class RailWriteTests(unittest.TestCase):
    def test_singleton_write_uses_card_base_and_preserves_other_fields(self):
        gpu = fake_gpu("pascal")
        gpu.volt_limits_write_enabled = True
        gpu.nvapi.control[0] = [-12500, -25000, -100000, 25000]
        ok, message = gpu.set_volt_rail_limits(0, reliability=1000.0)
        self.assertTrue(ok, message)
        gpu._write_rail_records.assert_called_once_with(
            {0: [-62500, -25000, -100000, 25000]})

    def test_absent_rail_cannot_be_written_or_reset(self):
        gpu = fake_gpu()
        gpu.volt_limits_write_enabled = True
        self.assertFalse(gpu.set_volt_rail_limits(1, reliability=900)[0])
        self.assertFalse(gpu.reset_volt_rail_limits(1)[0])
        gpu._write_rail_records.assert_not_called()

    def test_other_blackwell_rail_is_preserved(self):
        gpu = fake_gpu("blackwell")
        gpu.volt_limits_write_enabled = True
        gpu.nvapi.control = {0: [-25000, -50000, -100000, 25000],
                             1: [-75000, -35000, -125000, 50000]}
        ok, message = gpu.set_volt_rail_limits(1, overvoltage=1000)
        self.assertTrue(ok, message)
        gpu._write_rail_records.assert_called_once_with(
            {0: [-25000, -50000, -100000, 25000],
             1: [-75000, -35000, -200000, 50000]})

    def test_write_rejects_out_of_bound_or_nonfinite_values(self):
        for value in (649.0, 1201.0, float("nan"), float("inf")):
            with self.subTest(value=value):
                gpu = fake_gpu()
                gpu.volt_limits_write_enabled = True
                self.assertFalse(gpu.set_volt_rail_limits(
                    0, reliability=value)[0])
                gpu._write_rail_records.assert_not_called()

    def test_readback_checks_untouched_rail_too(self):
        gpu = fake_gpu("blackwell")
        gpu.volt_limits_write_enabled = True

        def corrupt_other_rail(records):
            gpu.nvapi.control = copy.deepcopy(records)
            gpu.nvapi.control[1][0] += 1000
            return True, 0

        gpu._write_rail_records.side_effect = corrupt_other_rail
        ok, message = gpu.set_volt_rail_limits(0, reliability=1000)
        self.assertFalse(ok, message)
        self.assertIn("read-back", message)

    def test_reset_one_field_preserves_other_rail_and_fields(self):
        gpu = fake_gpu("blackwell")
        gpu.nvapi.control = {0: [-25000, -50000, -100000, 25000],
                             1: [-75000, -35000, -125000, 50000]}
        self.assertFalse(gpu.volt_limits_write_enabled)
        ok, message = gpu.reset_volt_rail_limits(1, ["reliability"])
        self.assertTrue(ok, message)
        gpu._write_rail_records.assert_called_once_with(
            {0: [-25000, -50000, -100000, 25000],
             1: [-50000, -35000, -125000, 50000]})

    def test_titan_full_reset_restores_only_its_nvvdd_record(self):
        gpu = fake_gpu("pascal")
        gpu.nvapi.control[0] = [-62500, -93750, -200000, 250000]
        ok, message = gpu.reset_volt_rail_limits()
        self.assertTrue(ok, message)
        gpu._write_rail_records.assert_called_once_with({0: [0, 0, 0, 0]})

    def test_reset_requires_confirming_readback(self):
        gpu = fake_gpu()
        gpu.nvapi.control[0][0] = -68750
        gpu._write_rail_records.side_effect = None
        gpu._write_rail_records.return_value = (True, 0)
        ok, message = gpu.reset_volt_rail_limits()
        self.assertFalse(ok, message)
        self.assertIn("read-back", message)

    def test_reset_fails_if_control_getter_disappears_after_write(self):
        gpu = fake_gpu()

        def disappear(records):
            gpu.nvapi.control = {}
            return True, 0

        gpu._write_rail_records.side_effect = disappear
        self.assertFalse(gpu.reset_volt_rail_limits()[0])

    def test_changed_voltage_boost_prevents_success(self):
        for action in ("set", "reset"):
            with self.subTest(action=action):
                gpu = fake_gpu()
                gpu.volt_limits_write_enabled = True
                gpu.read_voltage_boost.side_effect = [37, 0]
                result = (gpu.set_volt_rail_limits(0, reliability=1000)
                          if action == "set" else gpu.reset_volt_rail_limits())
                self.assertFalse(result[0], result[1])


class RailEscapeTests(unittest.TestCase):
    def run_fake_escape(self, kind="turing", packet_size=1104,
                        params_size=1036, requested_mask=None):
        """Exercise the hook using owned memory; no machine code is run."""
        gpu = fake_gpu(kind)
        records = {r: [-68750, -93750, -125000, 200000]
                   for r in gpu.nvapi.control}
        mask = sum(1 << rail for rail in records)
        payload = (u32 * (1104 // 4))()
        for index in range(len(payload)):
            payload[index] = 0x55660000 + index
        payload[14], payload[15], payload[16] = GPU._ESC_B213, params_size, 0
        payload[18] = mask if requested_mask is None else requested_mask
        # NVAPI does not populate this input from the current boost setting.
        payload[19] = 0
        escape = GPU._Escape(
            pPrivateDriverData=ctypes.addressof(payload),
            PrivateDriverDataSize=packet_size)
        original_payload = bytes(payload)
        code_buffer = ctypes.create_string_buffer(b"original bytes")
        original_code = bytes(code_buffer)
        callback_buffer = ctypes.create_string_buffer(14)
        address = ctypes.addressof(code_buffer)
        sent = []
        getter_masks = []

        def fake_real_function(pesc):
            self.assertEqual(bytes(code_buffer), original_code,
                             "restore the original hook bytes before call-through")
            self.assertEqual(pesc, ctypes.addressof(escape))
            sent.append(bytes(payload))
            return 0

        def fake_prototype(result_type, argument_type):
            def bind(target):
                if callable(target):
                    def getter(handle, buf):
                        self.assertIs(handle, gpu.nvapi.gpu)
                        pu = ctypes.cast(buf, ctypes.POINTER(u32))
                        self.assertEqual(pu[0], 0x00020AC8)
                        getter_masks.append(pu[1])
                        return target(ctypes.addressof(escape))

                    gpu.nvapi.escape_hook = getter
                    return ctypes.c_void_p(ctypes.addressof(callback_buffer))
                self.assertEqual(target, address)
                return fake_real_function

            return bind

        kernel = SimpleNamespace(VirtualProtect=Mock(return_value=1),
                                 GetCurrentThreadId=Mock(return_value=731))
        gdi = SimpleNamespace(D3DKMTEscape=ctypes.c_void_p(address))

        def fake_dll(name, **kwargs):
            self.assertIn(name, ("gdi32.dll", "kernel32"))
            return gdi if name == "gdi32.dll" else kernel

        with patch("nvbackend.ctypes.WinDLL", new=fake_dll, create=True), \
                patch("nvbackend.ctypes.WINFUNCTYPE", new=fake_prototype,
                      create=True):
            result = GPU._write_rail_records(gpu, records)
        self.assertEqual(bytes(code_buffer), original_code)
        self.assertEqual(getter_masks, [mask])
        self.assertEqual(len(sent), 1)
        return result, original_payload, sent[0], records

    def test_escape_preserves_boost_and_all_unselected_packet_fields(self):
        for kind in ("turing", "pascal", "blackwell"):
            with self.subTest(card=kind):
                result, before, after, records = self.run_fake_escape(kind)
                self.assertEqual(result, (True, 0))
                expected = (u32 * (len(before) // 4)).from_buffer_copy(before)
                expected[14] = GPU._ESC_F214
                expected[19] = 37
                signed = ctypes.cast(expected, ctypes.POINTER(i32))
                for rail, values in records.items():
                    base = GPU._ESC_REC0 + rail * GPU._ESC_STRIDE
                    signed[base] = 5
                    for index, value in enumerate(values):
                        signed[base + 1 + index] = value
                    signed[base + 7] = 1
                self.assertEqual(after, bytes(expected))

    def test_escape_refuses_unexpected_geometry_or_rail_mask(self):
        for mismatch in ({"packet_size": 1100}, {"params_size": 1032},
                         {"requested_mask": 3}):
            with self.subTest(mismatch=mismatch):
                result, before, after, _ = self.run_fake_escape(**mismatch)
                self.assertEqual(result, (False, None))
                self.assertEqual(after, before)

    def test_escape_refuses_absent_rail_or_unreadable_boost_before_loading_dll(self):
        gpu = fake_gpu()
        with patch("nvbackend.ctypes.WinDLL", create=True) as loader:
            self.assertEqual(GPU._write_rail_records(
                gpu, {0: [0] * 4, 1: [0] * 4}), (False, None))
            gpu.read_voltage_boost.return_value = None
            self.assertEqual(GPU._write_rail_records(
                gpu, {0: [0] * 4}), (False, None))
        loader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
