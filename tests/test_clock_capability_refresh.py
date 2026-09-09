# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Private request capabilities independent of board telemetry and retry history."""
import ctypes
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from druta import profiles
from druta.nvbackend import GPU, CLKDOM_LAYOUT_TURING, CLKDOM_LAYOUT_BLACKWELL, i32, u32


def clock_gpu(architecture=6, domains=(0, 1, 2, 3, 4, 5, 9)):
    gpu = GPU.__new__(GPU)
    gpu.arch = lambda: architecture
    gpu.static = {"name": "Different PCB", "uuid": "GPU-other", "slot": "0000:02:00.0",
                  "vbios": "different", "driver": "other"}
    layout = CLKDOM_LAYOUT_BLACKWELL if architecture == 10 else CLKDOM_LAYOUT_TURING
    wire = (ctypes.c_ubyte * gpu._CLKDOM_BUF)()
    words = ctypes.cast(wire, ctypes.POINTER(i32))
    words[0] = layout.version
    for domain in domains:
        base = (layout.header + domain * layout.stride) // 4
        words[base] = 8
        words[base + 0x80 // 4] = 0x12345678
        words[base + layout.nvvdd_uv // 4] = 42000
        words[base + layout.msvdd_uv // 4] = 12500
    state = {"wire": wire, "accept": set(domains), "status": 0, "bad_echo": False,
             "ignored": False, "gets": [], "writes": []}

    def get(mask):
        state["gets"].append(mask)
        buf = (ctypes.c_ubyte * gpu._CLKDOM_BUF).from_buffer_copy(state["wire"])
        ctypes.cast(buf, ctypes.POINTER(u32))[2] = mask if not state["bad_echo"] else 0
        if state["status"]:
            return state["status"], buf
        if mask & ~sum(1 << d for d in state["accept"]):
            return -1, buf
        return 0, buf

    def write(_handle, buf):
        state["writes"].append(ctypes.string_at(buf, layout.size))
        if not state["ignored"]:
            state["wire"] = (ctypes.c_ubyte * gpu._CLKDOM_BUF).from_buffer_copy(
                ctypes.string_at(buf, gpu._CLKDOM_BUF))
        return 0

    gpu.nvapi = SimpleNamespace(ok=True, gpu=object(), ClkDomCtlGet=object(),
                                ClkDomCtlSet=write)
    gpu._clkdom_get = get
    gpu.voltage_xoc_enabled = False
    gpu.msvdd_write_enabled = False
    gpu.read = Mock(side_effect=AssertionError("optional live telemetry must not authorize a control"))
    return gpu, state, layout


class ClockCapabilityTests(unittest.TestCase):
    def test_known_controls_survive_missing_private_counters_on_pascal_and_turing(self):
        for architecture in (4, 6):
            with self.subTest(architecture=architecture):
                gpu, _, _ = clock_gpu(architecture)
                self.assertEqual(gpu.clkdom_controls_for_ui(rows=[]), [0, 1, 2, 3, 5, 9])
                self.assertNotIn(4, gpu.clkdom_controls_for_ui())
                gpu.read.assert_not_called()

    def test_transient_empty_discovery_is_cached_until_explicit_bounded_retry(self):
        gpu, state, _ = clock_gpu()
        state["status"] = -1
        self.assertIsNone(gpu.clkdom_layout())
        self.assertEqual(len(state["gets"]), 32)
        self.assertEqual(gpu._clkdom_capability_error[0], "unavailable")
        state["status"] = 0
        self.assertEqual(gpu.clkdom_controls_for_ui(), [])
        self.assertEqual(len(state["gets"]), 32)
        gpu._legacy_p0_owned = True
        gpu._refresh_volt_rail_capabilities = Mock()
        gpu.refresh_capabilities()
        self.assertEqual(gpu.clkdom_controls_for_ui(), [0, 1, 2, 3, 5, 9])
        self.assertEqual(len(state["gets"]), 65)
        self.assertTrue(gpu._legacy_p0_owned)
        gpu._refresh_volt_rail_capabilities.assert_called_once_with()
        self.assertEqual(state["writes"], [])

    def test_partial_domain_cache_is_refreshed_without_replacing_telemetry(self):
        gpu, state, _ = clock_gpu(domains=(1, 2, 3))
        state["accept"] = {2}
        self.assertEqual(gpu.clkdom_controls_for_ui(), [2])
        state["accept"] = {1, 2, 3}
        self.assertEqual(gpu.clkdom_controls_for_ui(), [2])
        gpu._clkdom_pair = {1: 99}
        gpu.refresh_capabilities()
        self.assertIsNone(gpu._clkdom_pair)
        self.assertEqual(gpu.clkdom_controls_for_ui(), [1, 2, 3])

    def test_success_without_mask_echo_is_not_a_capability(self):
        gpu, state, _ = clock_gpu()
        state["bad_echo"] = True
        self.assertEqual(gpu.clkdom_controls_for_ui(), [])
        self.assertTrue(all("echo mismatch" in reason for reason in gpu._clkdom_probe_errors.values()
                            if "status" not in reason))

    def test_validated_layout_read_exception_can_be_retried(self):
        gpu, _, _ = clock_gpu()
        gpu._clkdom_valid = [0]
        get = gpu._clkdom_get
        gpu._clkdom_get = Mock(side_effect=RuntimeError("GPU waking up"))
        self.assertIsNone(gpu.clkdom_layout())
        self.assertEqual(gpu._clkdom_capability_error[0], "read_failure")
        self.assertIn("GPU waking up", gpu._clkdom_capability_error[1])
        gpu._clkdom_get = get
        gpu.refresh_capabilities()
        self.assertIsNotNone(gpu.clkdom_layout())

    def test_msvdd_is_exposed_independently_of_a_local_negative_experiment(self):
        for architecture in (4, 6, 10):
            with self.subTest(architecture=architecture):
                gpu, _, _ = clock_gpu(architecture)
                capability = gpu.rail_offset_capability(rail=1)
                self.assertTrue(capability["available"])
                self.assertTrue(capability["experimental"])
                self.assertEqual(capability["domain"], 0)
                self.assertEqual(gpu.read_rail_offset_mv(0, rail=1), 12.5)
                self.assertEqual(gpu.read_rail_offset_mv(0), 42)

    def test_msvdd_uses_an_understood_accepted_record_when_core_control_is_missing(self):
        gpu, _, _ = clock_gpu(domains=(1, 4))
        self.assertEqual(gpu.rail_offset_capability(rail=1)["domain"], 1)
        self.assertFalse(gpu.rail_offset_capability(rail=1, domain=4)["available"])

    def test_msvdd_requires_opt_in_and_preserves_every_other_dword(self):
        gpu, state, layout = clock_gpu()
        self.assertFalse(gpu.set_rail_offset_mv(25, rail=1)[0])
        self.assertEqual(state["writes"], [])
        gpu.msvdd_write_enabled = True
        status, before = gpu._clkdom_get(1)
        ok, message = gpu.set_rail_offset_mv(25.001, rail=1)
        self.assertTrue(ok, message)
        self.assertIn("physical voltage response is unverified", message)
        self.assertEqual(gpu.read_rail_offset_mv(0, rail=1), 25.001)
        self.assertEqual(gpu.read_rail_offset_mv(0), 42)
        old = ctypes.cast(before, ctypes.POINTER(u32))
        new = ctypes.cast(ctypes.create_string_buffer(state["writes"][-1]), ctypes.POINTER(u32))
        self.assertEqual([i for i in range(layout.size // 4) if old[i] != new[i]],
                         [(layout.header + layout.msvdd_uv) // 4])
        gpu.msvdd_write_enabled = False
        self.assertTrue(gpu.reset_msvdd_offset_mv()[0])
        self.assertEqual(gpu.read_rail_offset_mv(0, rail=1), 0)

    def test_msvdd_accepted_but_ignored_write_is_not_success(self):
        gpu, state, _ = clock_gpu()
        gpu.msvdd_write_enabled = True
        state["ignored"] = True
        ok, message = gpu.set_rail_offset_mv(25, rail=1)
        self.assertFalse(ok)
        self.assertIn("readback mismatch", message)
        self.assertEqual(gpu._msvdd_offset_domains_written, {0})

    def test_msvdd_detects_prewrite_mutation_before_writing(self):
        gpu, state, layout = clock_gpu()
        gpu.msvdd_write_enabled = True
        gpu.clkdom_layout()  # Complete initial discovery before injection.
        original_get = gpu._clkdom_get
        reads = []

        def mutating_get(mask):
            reads.append(mask)
            status, buf = original_get(mask)
            if len(reads) == 3:  # Capability read, write source, immediate recheck.
                ctypes.cast(buf, ctypes.POINTER(i32))[(layout.header + 0x80) // 4] = 99
            return status, buf

        gpu._clkdom_get = mutating_get
        ok, message = gpu.set_rail_offset_mv(25, rail=1)
        self.assertFalse(ok)
        self.assertIn("changed between reads", message)
        self.assertEqual(state["writes"], [])

    def test_msvdd_bad_payload_never_writes(self):
        for invalid in (True, float("nan"), float("inf"), "25", 2147484):
            gpu, state, _ = clock_gpu()
            gpu.msvdd_write_enabled = True
            self.assertFalse(gpu.set_rail_offset_mv(invalid, rail=1)[0])
            self.assertEqual(state["writes"], [])


class MsvddProfileTests(unittest.TestCase):
    def gpu_and_profile(self):
        gpu, transport, _ = clock_gpu()
        gpu.vf_curve_applicable = lambda: False
        state = {"schema": 2, "device": gpu.static.copy(), "vf_applicable": False,
                 "msvdd_offsets_mv": {"0": 25}, "xoc": True}
        return gpu, transport, state

    def test_capture_records_request_domain_value_and_xoc_requirement(self):
        gpu, _, _ = self.gpu_and_profile()
        gpu.read_volt_rail_limits = lambda: None
        gpu.volt_rail_limit_fields = lambda rail: ()
        state = {profiles.INCOMPLETE_KEY: []}
        profiles.capture_rails(gpu, state, None)
        self.assertEqual(state["msvdd_offsets_mv"], {"0": 12.5})
        self.assertTrue(state["xoc"])
        self.assertEqual(state[profiles.INCOMPLETE_KEY], [])

    def test_unreadable_previously_exposed_request_marks_first_write_snapshot_incomplete(self):
        gpu, _, _ = self.gpu_and_profile()
        gpu.read_volt_rail_limits = lambda: None
        gpu.volt_rail_limit_fields = lambda rail: ()
        self.assertTrue(gpu.rail_offset_capability(rail=1, domain=1)["available"])
        self.assertFalse(getattr(gpu, "_msvdd_offset_domains_written", set()))
        gpu.refresh_capabilities()
        gpu.rail_offset_capability = Mock(return_value={"available": False})
        state = {profiles.INCOMPLETE_KEY: []}
        profiles.capture_rails(gpu, state, None)
        self.assertEqual(state["msvdd_offsets_mv"], {})
        self.assertIn("MSVDD request at control 1 NOT captured", state[profiles.INCOMPLETE_KEY])

    def test_restore_exact_request_and_zero_without_opt_in(self):
        gpu, _, state = self.gpu_and_profile()
        gpu.msvdd_write_enabled = True
        self.assertIsNone(profiles.preflight(gpu, state))
        self.assertTrue(all(ok for ok, _ in profiles.restore(gpu, state)))
        self.assertEqual(gpu.read_rail_offset_mv(0, rail=1), 25)
        gpu.msvdd_write_enabled = False
        state.update(msvdd_offsets_mv={"0": 0}, xoc=False)
        self.assertTrue(all(ok for ok, _ in profiles.restore(gpu, state)))
        self.assertEqual(gpu.read_rail_offset_mv(0, rail=1), 0)

    def test_unavailable_or_malformed_or_other_device_profile_never_writes(self):
        for change in ({"msvdd_offsets_mv": {"4": 25}}, {"msvdd_offsets_mv": {"00": 25}},
                       {"msvdd_offsets_mv": {"0": float("nan")}}, {"xoc": False},
                       {"device": {"uuid": "other card"}}):
            with self.subTest(change=change):
                gpu, transport, state = self.gpu_and_profile()
                state.update(change)
                self.assertFalse(profiles.restore(gpu, state)[0][0])
                self.assertEqual(transport["writes"], [])

    def test_failed_offset_restore_stops_before_other_knobs(self):
        gpu, transport, state = self.gpu_and_profile()
        gpu.msvdd_write_enabled = True
        transport["ignored"] = True
        state["volt_boost_pct"] = 100
        gpu.set_voltage_boost = Mock(return_value=(True, "set"))
        self.assertFalse(profiles.restore(gpu, state)[0][0])
        gpu.set_voltage_boost.assert_not_called()


if __name__ == "__main__":
    unittest.main()
