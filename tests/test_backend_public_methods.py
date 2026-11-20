# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""G4f: the public nvbackend.GPU methods that replace the front ends' private
reaches, their call sites, and the request envelopes profiles take from the
backend (never from an assumed number). No driver, GUI or hardware."""

import ctypes
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import tests.test_clock_capability_refresh as clock_fixture
import tests.test_legacy_offsets as offset_fixture
import tests.test_live_power_ui as live_power
import tests.test_tuning_action_profiles as tuning_fixture
from druta import nvbackend as n
from druta import profiles, tuning_profiles
from tests.test_tune_profiles import hardware

ENVELOPES = ("VOLT_LIMIT_MIN_MV", "VOLT_LIMIT_MAX_MV", "VOLT_LIMIT_XOC_MAX_MV",
             "RAIL_OFFSET_XOC_MAX_MV", "MAX_ABS_DELTA_KHZ")


class EnforcedPowerDriver:
    """NVML with only the enforced-limit getter; every call is recorded."""

    def __init__(self, value=349200, status=0):
        self.value, self.status, self.calls = value, status, []

    def nvmlDeviceGetEnforcedPowerLimit(self, device, pointer):
        self.calls.append(device)
        if self.status:
            return self.status
        ctypes.cast(pointer, ctypes.POINTER(n.u32))[0] = self.value
        return 0


def power_gpu(driver, *, ok=True, has=True):
    gpu = n.GPU.__new__(n.GPU)
    gpu._lock = threading.RLock()
    gpu.static = {}
    # Any NVAPI or other NVML use would fail: the reader makes one call only.
    gpu.nvapi = Mock(side_effect=AssertionError("NVAPI must not be called"))
    gpu.read_power_limit_mw = Mock(side_effect=AssertionError("the configured request is not read"))
    gpu.nvml = SimpleNamespace(ok=ok, dev="device", dll=driver,
                               has=lambda name: has and hasattr(driver, name))
    return gpu


class ReadEnforcedPowerLimitTests(unittest.TestCase):
    def test_returns_the_enforced_limit_in_milliwatts_from_one_driver_call(self):
        driver = EnforcedPowerDriver(349200)
        gpu = power_gpu(driver)
        self.assertEqual(gpu.read_enforced_power_limit_mw(), 349200)
        self.assertEqual(driver.calls, ["device"])

    def test_every_call_reads_the_driver_afresh(self):
        driver = EnforcedPowerDriver(349200)
        gpu = power_gpu(driver)
        gpu.read_enforced_power_limit_mw()
        driver.value = 300000
        self.assertEqual(gpu.read_enforced_power_limit_mw(), 300000)
        self.assertEqual(len(driver.calls), 2)

    def test_matches_the_telemetry_pl_now_mw_and_not_the_configured_request(self):
        driver = EnforcedPowerDriver(349200)
        gpu = power_gpu(driver)
        gpu.read_power_limit_mw = Mock(return_value=360000)
        gpu.nvapi = SimpleNamespace(ok=False)
        telemetry = {}
        gpu._read_power(telemetry)
        self.assertEqual(telemetry["pl_requested_mw"], 360000)
        self.assertEqual(gpu.read_enforced_power_limit_mw(), telemetry["pl_now_mw"])
        self.assertEqual(len(driver.calls), 2)

    def test_none_when_nvml_is_not_live_the_export_is_absent_or_the_call_fails(self):
        for label, driver in (("nvml not live", EnforcedPowerDriver()),
                              ("export absent", EnforcedPowerDriver()),
                              ("status", EnforcedPowerDriver(status=3))):
            with self.subTest(label):
                gpu = power_gpu(driver, ok=label != "nvml not live", has=label != "export absent")
                self.assertIsNone(gpu.read_enforced_power_limit_mw())
                self.assertEqual(driver.calls, [] if label != "status" else ["device"])


def offsets_gpu():
    return offset_fixture.gpu()       # NVAPI P-state fallback, NVML without offsets


class ClockOffsetRangeTests(unittest.TestCase):
    def test_returns_what_the_private_reader_returns_for_each_type(self):
        gpu = offsets_gpu()
        for ctype in (0, 2):
            with self.subTest(ctype=ctype):
                self.assertEqual(gpu.clock_offset_range(ctype), gpu._offset_range(ctype))
        self.assertEqual(gpu.clock_offset_range(0), (-1000, 1000, 30))
        self.assertEqual(gpu.clock_offset_range(2), (-2000, 6000, 160))

    def test_none_for_any_other_type_and_when_unreadable(self):
        gpu = offsets_gpu()
        for ctype in (1, 3, -1):
            with self.subTest(ctype=ctype):
                self.assertIsNone(gpu.clock_offset_range(ctype))
        gpu.nvapi.status = 0
        gpu.nvapi.Pstates20Get = lambda handle, pointer: 5
        self.assertIsNone(gpu.clock_offset_range(2))

    def test_never_cached_a_driver_change_shows_on_the_next_call(self):
        gpu = offsets_gpu()
        reads = len(gpu.nvapi.reads)
        self.assertEqual(gpu.clock_offset_range(2)[2], 160)
        memory = [c for c in gpu.nvapi.info.pstates[1].clocks if c.domain == 4][0]
        memory.delta.value = 100000
        self.assertEqual(gpu.clock_offset_range(2)[2], 200)
        self.assertGreater(len(gpu.nvapi.reads), reads + 1)

    def test_nvml_offsets_are_preferred_when_the_export_answers(self):
        gpu = offsets_gpu()

        def offsets(device, pointer):
            pointer._obj.mn, pointer._obj.mx, pointer._obj.off = -500, 1500, 250
            return 0
        gpu.nvml = SimpleNamespace(ok=True, dev=object(), ver=lambda typ, v: v,
                                   has=lambda name: name == "nvmlDeviceGetClockOffsets",
                                   dll=SimpleNamespace(nvmlDeviceGetClockOffsets=offsets))
        self.assertEqual(gpu.clock_offset_range(2), (-500, 1500, 250))


class MsvddOffsetDomainsTests(unittest.TestCase):
    def test_empty_before_any_discovery_or_write(self):
        gpu, _, _ = clock_fixture.clock_gpu()
        self.assertEqual(gpu.msvdd_offset_domains(), (frozenset(), frozenset()))

    def test_reports_domains_seen_by_discovery_and_written_by_a_request(self):
        gpu, _, _ = clock_fixture.clock_gpu()
        self.assertTrue(gpu.rail_offset_capability(rail=1, domain=5)["available"])
        gpu.msvdd_write_enabled = True
        self.assertTrue(gpu.set_rail_offset_mv(25, 3, rail=1)[0])
        written, seen = gpu.msvdd_offset_domains()
        self.assertEqual(written, frozenset({3}))
        self.assertEqual(seen, frozenset({3, 5}))
        self.assertIsInstance(written, frozenset)
        self.assertIsInstance(seen, frozenset)

    def test_nvvdd_discovery_is_not_an_msvdd_domain(self):
        gpu, _, _ = clock_fixture.clock_gpu()
        gpu.rail_offset_capability(rail=0, domain=2)
        self.assertEqual(gpu.msvdd_offset_domains(), (frozenset(), frozenset()))

    def test_a_returned_copy_does_not_follow_later_session_state(self):
        gpu, _, _ = clock_fixture.clock_gpu()
        gpu.rail_offset_capability(rail=1, domain=1)
        before = gpu.msvdd_offset_domains()
        gpu.rail_offset_capability(rail=1, domain=2)
        self.assertEqual(before, (frozenset(), frozenset({1})))
        self.assertEqual(gpu.msvdd_offset_domains()[1], frozenset({1, 2}))

    def test_the_result_is_a_copy_not_the_live_private_sets(self):
        # The writers replace the private sets whole, so only an in-place change
        # tells a copy from an alias.
        gpu, _, _ = clock_fixture.clock_gpu()
        gpu.rail_offset_capability(rail=1, domain=1)
        gpu.msvdd_write_enabled = True
        self.assertTrue(gpu.set_rail_offset_mv(25, 3, rail=1)[0])
        written, seen = gpu.msvdd_offset_domains()
        self.assertIsNot(written, gpu._msvdd_offset_domains_written)
        self.assertIsNot(seen, gpu._msvdd_offset_domains_seen)
        gpu._msvdd_offset_domains_written.add(7)
        gpu._msvdd_offset_domains_seen.add(8)
        self.assertEqual((written, seen), (frozenset({3}), frozenset({1, 3})))
        self.assertEqual(gpu.msvdd_offset_domains(), (frozenset({3, 7}), frozenset({1, 3, 8})))

    def test_rail_offset_capability_still_holds_the_gpu_lock(self):
        # msvdd_offset_domains was added beside it; the discovery keeps its lock.
        self.assertTrue(hasattr(n.GPU.rail_offset_capability, "__wrapped__"))
        self.assertFalse(hasattr(n.GPU.msvdd_offset_domains, "__wrapped__"))


class MsvddCaptureTests(unittest.TestCase):
    def test_capture_includes_a_written_domain_and_a_seen_only_domain(self):
        gpu, _, _ = clock_fixture.clock_gpu()
        gpu.msvdd_write_enabled = True
        self.assertTrue(gpu.set_rail_offset_mv(25, 3, rail=1)[0])
        gpu.rail_offset_capability(rail=1, domain=5)
        state = {profiles.INCOMPLETE_KEY: []}
        profiles.capture_rails(gpu, state, None)
        self.assertEqual(state["msvdd_offsets_mv"], {"0": 12.5, "3": 25, "5": 12.5})
        self.assertFalse([m for m in state[profiles.INCOMPLETE_KEY] if "MSVDD" in m])

    def test_a_backend_without_the_method_is_reported_not_taken_for_none(self):
        capability = Mock(side_effect=lambda rail, domain=None: {
            "available": True, "domain": 0 if domain is None else domain, "value_mv": 12.5})
        gpu = SimpleNamespace(rail_offset_capability=capability)
        state = {profiles.INCOMPLETE_KEY: []}
        profiles.capture_rails(gpu, state, None)
        self.assertEqual(state["msvdd_offsets_mv"], {"0": 12.5})
        self.assertIn("MSVDD requests written this session NOT captured (the backend does not report them)",
                      state[profiles.INCOMPLETE_KEY])
        self.assertTrue(profiles.incomplete(state))

    def test_session_domains_come_from_the_public_method(self):
        values = {0: 12.5, 4: -10, 6: 30}
        capability = Mock(side_effect=lambda rail, domain=None: {
            "available": True, "domain": 0 if domain is None else domain,
            "value_mv": values[0 if domain is None else domain]})
        gpu = SimpleNamespace(rail_offset_capability=capability,
                              msvdd_offset_domains=Mock(return_value=(frozenset({4}), frozenset({6}))),
                              # a stale private set must not be what capture reads
                              _msvdd_offset_domains_written={9}, _msvdd_offset_domains_seen={9})
        state = {profiles.INCOMPLETE_KEY: []}
        profiles.capture_rails(gpu, state, None)
        self.assertEqual(state["msvdd_offsets_mv"], {"0": 12.5, "4": -10, "6": 30})
        gpu.msvdd_offset_domains.assert_called_once_with()


class TuningCallSiteTests(unittest.TestCase):
    def test_capture_and_preflight_read_the_public_range(self):
        gpu = tuning_fixture.GPU()
        gpu.clock_offset_range = Mock(side_effect=lambda kind: (-200, 200, gpu.memory))
        state = tuning_profiles.capture(gpu, "memory")
        self.assertEqual(state["tuning_action"]["value"], 17)
        self.assertIsNone(tuning_profiles.preflight(gpu, state))
        self.assertTrue(gpu.clock_offset_range.call_args_list)
        self.assertTrue(all(c.args == (2,) for c in gpu.clock_offset_range.call_args_list))

    def test_preflight_bounds_come_from_a_fresh_public_read(self):
        gpu = tuning_fixture.GPU()
        state = tuning_profiles.capture(gpu, "memory")
        gpu.clock_offset_range = Mock(return_value=(-10, 10, 17))
        self.assertIn("outside the current control range", tuning_profiles.preflight(gpu, state))


class TuningMissingRangeTests(unittest.TestCase):
    def test_a_backend_with_only_the_private_reader_is_refused(self):
        source = tuning_fixture.GPU()
        state = tuning_profiles.capture(source, "memory")

        class PrivateOnly(tuning_fixture.GPU):
            clock_offset_range = property(lambda self: (_ for _ in ()).throw(AttributeError("clock_offset_range")))

            def _offset_range(self, kind):
                return (-200, 200, self.memory)
        gpu = PrivateOnly()
        self.assertIn("clock_offset_range", tuning_profiles.preflight(gpu, state))
        with self.assertRaises(AttributeError):
            tuning_profiles.capture(gpu, "memory")


class PowerNoteTests(unittest.TestCase):
    def setUp(self):
        helper = live_power.LivePowerUiTests("test_native_metadata_can_supply_missing_nvml_slider_fields")
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        self.app, self.values = helper.app, helper.values
        self.values.update(sl_pl=400, in_pl=400, power_request_status="")
        self.app.gpu.read_power_limit_mw.side_effect = [350000, 400000]

    def message(self):
        ok, message = self.app.report.call_args.args[0]
        self.assertTrue(ok)
        return message

    def test_the_note_uses_the_public_enforced_reader(self):
        self.app.gpu.read_enforced_power_limit_mw = Mock(return_value=350000)
        self.app.gpu._read_power = Mock(side_effect=AssertionError("the private reader is not used"))
        self.app.apply_pl(400)
        self.assertIn("configured request 400 W, NVML-reported enforced limit 350 W", self.message())
        self.app.gpu.read_enforced_power_limit_mw.assert_called_once_with()

    def test_a_raising_reader_only_leaves_the_note_out(self):
        self.app.gpu.read_enforced_power_limit_mw = Mock(side_effect=OSError("driver gone"))
        self.app.apply_pl(400)
        message = self.message()
        self.assertNotIn("enforced limit", message)
        self.assertNotIn("not checked", message)
        self.assertEqual((self.values["sl_pl"], self.values["in_pl"]), (400, 400))

    def test_unusable_values_are_still_rejected(self):
        for value in (None, True, "350000", 0, -1, 1 << 32, 350000.0):
            with self.subTest(value=value):
                self.app.gpu.read_power_limit_mw.side_effect = [350000, 400000]
                self.app.gpu.read_enforced_power_limit_mw = Mock(return_value=value)
                self.app.apply_pl(400)
                self.assertNotIn("enforced limit", self.message())

    def test_a_backend_without_the_reader_is_said_so(self):
        self.assertFalse(hasattr(self.app.gpu, "read_enforced_power_limit_mw"))
        self.app.gpu._read_power = Mock(side_effect=lambda state: state.update(pl_now_mw=350000))
        self.app.apply_pl(400)
        message = self.message()
        self.assertIn("enforced power limit not checked: this backend does not report it", message)
        self.app.gpu._read_power.assert_not_called()


def envelope_gpu(drop=None):
    """hardware()'s profile fake with the real constants, less one."""
    gpu, rail, _ = hardware()
    if drop:
        delattr(gpu, drop)
    return gpu, rail


class EnvelopeTests(unittest.TestCase):
    def test_the_helper_returns_the_backends_own_value(self):
        for name in ENVELOPES:
            with self.subTest(name=name):
                self.assertEqual(profiles.backend_envelope(n.GPU, name), getattr(n.GPU, name))

    def test_the_helper_refuses_a_missing_or_unusable_constant_naming_what_it_bounds(self):
        for name in ENVELOPES:
            meaning = profiles.ENVELOPE_MEANINGS[name]
            with self.subTest(name=name):
                with self.assertRaises(ValueError) as caught:
                    profiles.backend_envelope(SimpleNamespace(), name)
                self.assertIn(meaning, str(caught.exception))
                self.assertIn(name, str(caught.exception))
                for bad in (None, "1200", True, float("nan"), float("inf")):
                    with self.assertRaises(ValueError) as caught:
                        profiles.backend_envelope(SimpleNamespace(**{name: bad}), name)
                    self.assertIn(meaning, str(caught.exception))

    def test_rail_limits_are_refused_without_each_rail_envelope(self):
        for name, xoc in (("VOLT_LIMIT_MIN_MV", False), ("VOLT_LIMIT_MAX_MV", False),
                          ("VOLT_LIMIT_XOC_MAX_MV", True)):
            with self.subTest(name=name):
                gpu, rail = envelope_gpu()
                state = profiles.capture(gpu)
                state["xoc"] = xoc
                self.assertIsNone(profiles.preflight(gpu, state))
                delattr(gpu, name)
                error = profiles.preflight(gpu, state)
                self.assertIn(profiles.ENVELOPE_MEANINGS[name], error)
                self.assertIn(name, error)

    def test_deltas_are_refused_without_the_delta_envelope(self):
        gpu, _ = envelope_gpu()
        state = profiles.capture(gpu)
        self.assertIsNone(profiles.preflight(gpu, state))
        del gpu.MAX_ABS_DELTA_KHZ
        self.assertIn("largest V/F point delta", profiles.preflight(gpu, state))
        results = profiles.restore(gpu, state)
        self.assertFalse(any(ok for ok, _ in results))
        gpu.apply_vf_deltas.assert_not_called()

    def test_a_profile_without_deltas_does_not_need_the_delta_envelope(self):
        gpu, _ = envelope_gpu("MAX_ABS_DELTA_KHZ")
        gpu.read_vf_curve.return_value = ([], None)
        state = profiles.capture(gpu)
        self.assertIsNone(profiles.preflight(gpu, state))

    def test_msvdd_is_refused_without_its_xoc_envelope(self):
        gpu, _ = envelope_gpu()
        gpu.rail_offset_capability = Mock(side_effect=lambda rail, domain=None: {
            "available": True, "domain": 0 if domain is None else domain, "value_mv": 0})
        gpu.msvdd_offset_domains = Mock(return_value=(frozenset(), frozenset()))
        state = profiles.capture(gpu)
        state["msvdd_offsets_mv"] = {"0": 25}
        self.assertIsNone(profiles.preflight(gpu, state))
        del gpu.RAIL_OFFSET_XOC_MAX_MV
        error = profiles.preflight(gpu, state)
        self.assertIn("experimental (XOC) rail-offset maximum", error)
        gpu.set_rail_offset_mv.assert_not_called()

    def test_capture_records_an_undeterminable_envelope_instead_of_raising(self):
        gpu, _ = envelope_gpu("VOLT_LIMIT_MAX_MV")
        gpu.voltage_xoc_enabled = False
        gpu.clkdom_controls_for_ui.return_value = [0, 7]     # so only the envelope decides
        state = profiles.capture(gpu)
        self.assertTrue(state["xoc"])
        self.assertTrue([m for m in state[profiles.INCOMPLETE_KEY]
                         if m.startswith("per-rail limit envelope NOT determined")
                         and "per-rail voltage-limit maximum" in m])

    def test_capture_compares_carryover_with_the_backends_maximum(self):
        gpu, _ = envelope_gpu()
        gpu.voltage_xoc_enabled = False
        gpu.clkdom_controls_for_ui.return_value = [0, 7]     # no additional-memory carryover
        self.assertFalse(profiles.capture(gpu)["xoc"])
        gpu.VOLT_LIMIT_MAX_MV = 1000.0
        self.assertTrue(profiles.capture(gpu)["xoc"])


class PythonBackendRailFloorTests(unittest.TestCase):
    """The saved-limit floor is nvbackend.GPU's own VOLT_LIMIT_MIN_MV."""

    def gpu(self):
        source, _, _ = hardware()
        gpu = n.GPU.__new__(n.GPU)
        for key, value in vars(source).items():
            if not key.isupper():               # the constants come from the class
                setattr(gpu, key, value)
        source.read_volt_rail_limits.return_value[0]["_base_mv"]["vmin"] = n.GPU.VOLT_LIMIT_MIN_MV + 50
        gpu.stock_limit_mv = Mock(side_effect=lambda rail, field: n.GPU.VOLT_LIMIT_MIN_MV + 40)
        return gpu, dict(source.static)

    def preflight(self, vmin):
        gpu, device = self.gpu()
        return profiles.preflight(gpu, {"device": device, "xoc": False,
                                        "rail_limits_mv": {"0": {"vmin": vmin}}})

    def test_a_limit_below_the_minimum_current_and_stock_is_refused(self):
        floor = n.GPU.VOLT_LIMIT_MIN_MV
        for vmin in (floor - 1, floor - 150, 300.0):
            with self.subTest(vmin=vmin):
                self.assertIn("outside the saved mode's voltage bounds", self.preflight(vmin))

    def test_a_limit_inside_the_bounds_is_accepted(self):
        floor = n.GPU.VOLT_LIMIT_MIN_MV
        for vmin in (floor, floor + 10, floor + 50):
            with self.subTest(vmin=vmin):
                self.assertIsNone(self.preflight(vmin))


if __name__ == "__main__":
    unittest.main()
