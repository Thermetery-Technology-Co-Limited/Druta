# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Scope the rail research probes by board and packet geometry, not driver.

The probes' fixed experiment steps were measured on two TITAN boards, so the
board scope stays. A driver string does not establish or deny the private rail
ABI. The captured packet, record, boost word and absolute-status record do,
and they must refuse before any SET. No test here loads a GPU driver: the
escape hook, NVAPI and GPU objects are replaced with Python fakes.
"""

import copy
import ctypes
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from druta import gpuload, nvbackend
from druta.nvbackend import GPU
from druta.tools import probe_volt_rails as modern
from druta.tools import probe_volt_rails_47212 as legacy


TITAN_RTX = {"name": "NVIDIA TITAN RTX", "devid": 0x1E02,
             "subsys": 312676574, "vbios": "90.02.1E.00.02"}
TITAN_XP = {"name": "NVIDIA TITAN Xp", "devid": 0x1B02,
            "subsys": 299831518, "vbios": "86.02.3D.00.01"}
MEASURED = {"TITAN RTX": TITAN_RTX, "TITAN Xp": TITAN_XP}
UNLISTED = {
    "RTX 5080": {"name": "NVIDIA GeForce RTX 5080", "devid": 0x2C02,
                 "subsys": 0x89DE1043, "vbios": "98.03.3b.c0.6f"},
    "TITAN RTX, other VBIOS": dict(TITAN_RTX, vbios="90.02.1e.00.01"),
    "TITAN Xp, other subsystem": dict(TITAN_XP, subsys=TITAN_XP["subsys"] + 1),
}
# Neither probe may treat any of these as a reason to refuse.
UNLISTED_DRIVERS = ("591.44", "?", "")

MODERN_SIZE, MODERN_GET, MODERN_SET = 1104, 0x2080B213, 0x2080F214
LEGACY_SIZE, LEGACY_GET, LEGACY_SET = 716, 0x20803213, 0x20803214


def modern_params():
    # 3 header words, then 32 records of 8 words. Record 0 is the TITANs'
    # native type 2 with zero deltas; boost is 100% (VOLTAGE-RAILS-TITAN.md).
    params = [0] * (3 + 32 * 8)
    params[:4] = [1, 1, 100, 2]
    return params


def legacy_params():
    # Mask, boost, then 32 records of type plus four deltas (VOLTAGE-RAILS-47212.md).
    params = [0] * (2 + 32 * 5)
    params[:3] = [1, 0, 1]
    return params


def get_result(size, command, params, *, escape_status=0, rm_status=0,
               returned_command=None, returned_params_size=None,
               returned_words=None):
    """The dict rm_call() returns for an intercepted GET."""
    header = [0] * 17
    header[2] = size
    header[14] = command if returned_command is None else returned_command
    header[15] = (size - 68) if returned_params_size is None else returned_params_size
    header[16] = rm_status
    words = list(params) if returned_words is None else returned_words
    return {"intercepted": True, "escape_size": size,
            "escape_status": escape_status, "rm_status": rm_status,
            "escape_header_after": header, "escape_output_params": words,
            "output_params": list(params)}


def snapshot_result(boost_pct, *, control_status=0, abs_status=0, abs_record=None):
    """The dict snapshot() returns. Absolute words follow GPU.LIVE_RAIL_*."""
    absolute = [0] * (0xAC8 // 4)
    absolute[0], absolute[1] = 0x10AC8, 1
    if abs_status == 0:
        absolute[2] = 25000 if boost_pct else 0
        absolute[18:25] = (abs_record if abs_record is not None else
                           [1, 681250, 1068750 + absolute[2], 1093750,
                            1125000, 1068750 + absolute[2], 650000])
    control = [0] * (0xAC8 // 4)
    control[0], control[1] = 0x20AC8, 1
    return {"VoltRailsCtlGet": {1: {"status": control_status, "words": control},
                                2: {"status": -1, "words": control[:2]},
                                3: {"status": -1, "words": control[:2]}},
            "VoltRailsAbs": {1: {"status": abs_status, "words": absolute},
                             2: {"status": -1, "words": absolute[:2]},
                             3: {"status": -1, "words": absolute[:2]}},
            "vcore_mv": 681.25, "boost_pct": boost_pct}


def fake_gpu(board, driver):
    nvapi = Mock(name="nvapi")
    nvapi.ok = True
    nvapi.selected = {"devid": board["devid"], "subsys": board["subsys"]}
    nvapi.ver = lambda struct_type, version: (version << 16) | ctypes.sizeof(struct_type)
    nvapi.BoostTableGet.return_value = 0
    nvapi.BoostTableSet.return_value = 0
    nvapi.VfLockSet.return_value = 0
    nvapi.ClkDomCtlSet.return_value = 0
    gpu = Mock(name="gpu")
    gpu.nvapi = nvapi
    gpu.nvml = SimpleNamespace(ok=True, selected={"nvml_index": 0})
    gpu.pairing_error = None
    gpu.static = {"name": board["name"], "driver": driver,
                  "vbios": board["vbios"], "slot": "0000:01:00.0"}
    gpu.vfp_layout.return_value = SimpleNamespace(n_entries=4, freq_div=1)
    lock = (ctypes.c_ubyte * 8)(*range(8))
    domains = (ctypes.c_ubyte * 8)(*range(8, 16))
    gpu._vf_lock_read_raw.return_value = lock
    gpu._clkdom_get.return_value = (0, domains)
    return gpu


def gpu_class(instance):
    """A GPU class whose constructor returns *instance* without touching NVAPI."""
    class FakeGPU(GPU):
        def __new__(cls, *args, **kwargs):
            return instance
    return FakeGPU


class RmCalls:
    """Stand-in for rm_call(); a replacement means a SET would be issued.

    SETs whose 0-based index is in *failed_writes* return an RM failure.
    """

    def __init__(self, baseline, failed_writes=()):
        self.baseline = baseline
        self.failed_writes = set(failed_writes)
        self.calls = []
        self.report = None

    def __call__(self, gpu, replacement=None, control_version=0x20AC8,
                 invoke=None, capture_multiple=False):
        self.calls.append((None if replacement is None else list(replacement),
                           control_version))
        if replacement is None:
            return copy.deepcopy(self.baseline)
        failed = len(self.writes) - 1 in self.failed_writes
        return {"intercepted": True, "escape_status": 0,
                "rm_status": 0x1F if failed else 0,
                "input_params": list(replacement)}

    @property
    def writes(self):
        return [params for params, _version in self.calls if params is not None]


def run_main(module, gpu, baseline, before, *flags, failed_writes=()):
    """Run *module*.main(); rm.report is the report it last saved, if any."""
    rm = RmCalls(baseline, failed_writes)
    snapshot = Mock(side_effect=lambda *args, **kwargs: copy.deepcopy(before))
    if module is legacy:
        target = patch.object(legacy, "n", SimpleNamespace(
            GPU=gpu_class(gpu), _BoostTable=nvbackend._BoostTable,
            _set_point_masks=nvbackend._set_point_masks))
    else:
        target = patch.object(modern, "GPU", gpu_class(gpu))
    error = None
    with tempfile.TemporaryDirectory(prefix="druta probe scope ") as folder, \
            target, patch.object(module, "rm_call", rm), \
            patch.object(module, "snapshot", snapshot), \
            patch.object(module.time, "sleep"), \
            patch.object(sys, "argv", ["probe", "--gpu", "0000:01:00.0",
                                       "--output", str(Path(folder) / "out.json"),
                                       *flags]), \
            redirect_stdout(io.StringIO()):
        output = Path(folder) / "out.json"
        try:
            module.main()
        except RuntimeError as exc:
            error = exc
        if output.exists():
            rm.report = json.loads(output.read_text(encoding="utf-8"))
    return rm, error


def assert_no_other_writes(case, gpu):
    for name in ("BoostTableSet", "VfLockSet", "ClkDomCtlSet"):
        getattr(gpu.nvapi, name).assert_not_called()
    written = [c[0] for c in gpu.method_calls
               if c[0].startswith(("set_", "apply_", "lock_", "reset_"))]
    case.assertEqual(written, [])


class ModernProbeScopeTests(unittest.TestCase):
    def identity(self, board, driver, baseline=None, before=None):
        gpu = fake_gpu(board, driver)
        baseline = baseline or get_result(MODERN_SIZE, MODERN_GET, modern_params())
        before = before or snapshot_result(100)
        rm, error = run_main(modern, gpu, baseline, before, "--identity")
        return gpu, rm, error

    def test_measured_titan_on_any_driver_string_runs_identity_writes(self):
        native = modern_params()
        type5 = native.copy()
        type5[3], type5[10] = 5, 1
        for label, board in MEASURED.items():
            for driver in (*UNLISTED_DRIVERS, "472.12"):
                with self.subTest(board=label, driver=driver):
                    _gpu, rm, error = self.identity(board, driver)
                    self.assertIsNone(error)
                    self.assertEqual(rm.writes, [native, type5])

    def test_unlisted_board_is_refused_before_any_write(self):
        for label, board in UNLISTED.items():
            for driver in ("580.97", "591.44"):
                with self.subTest(board=label, driver=driver):
                    gpu, rm, error = self.identity(board, driver)
                    self.assertIsInstance(error, RuntimeError)
                    self.assertIn("scoped", str(error))
                    self.assertEqual(rm.writes, [])
                    assert_no_other_writes(self, gpu)

    def test_misunderstood_native_packet_is_refused_before_any_write(self):
        # 580.97 lets unmodified code reach its record checks; the variants
        # refused only after this change were previously written.
        good = modern_params()

        def params(**changes):
            out = good.copy()
            for index, value in changes.items():
                out[int(index[1:])] = value
            return out

        truncated = good[:(976 - 68) // 4]
        cases = {
            "record type 5": (get_result(MODERN_SIZE, MODERN_GET, params(w3=5)), None),
            "nonzero record delta": (get_result(MODERN_SIZE, MODERN_GET, params(w4=12500)), None),
            "NTSTATUS failure with RM status 0": (
                get_result(MODERN_SIZE, MODERN_GET, good, escape_status=0xC0000001), None),
            "returned SET command": (
                get_result(MODERN_SIZE, MODERN_GET, good, returned_command=MODERN_SET), None),
            "returned parameter size": (
                get_result(MODERN_SIZE, MODERN_GET, good, returned_params_size=908), None),
            "returned packet shrank": (
                get_result(MODERN_SIZE, MODERN_GET, good, returned_words=truncated), None),
            "boost word differs from NVAPI boost": (
                get_result(MODERN_SIZE, MODERN_GET, good), snapshot_result(0)),
            "boost word out of range": (
                get_result(MODERN_SIZE, MODERN_GET, params(w2=0xFFFFFFFF)),
                snapshot_result(0xFFFFFFFF)),
            "absolute getter failed": (
                get_result(MODERN_SIZE, MODERN_GET, good), snapshot_result(100, abs_status=-9)),
            "absolute record is not NVVDD": (
                get_result(MODERN_SIZE, MODERN_GET, good),
                snapshot_result(100, abs_record=[3, 681250, 1093750, 1093750,
                                                 1125000, 1093750, 650000])),
            "control getter failed": (
                get_result(MODERN_SIZE, MODERN_GET, good), snapshot_result(100, control_status=-1)),
        }
        for label, (baseline, before) in cases.items():
            with self.subTest(label):
                gpu, rm, error = self.identity(TITAN_RTX, "580.97", baseline, before)
                self.assertIsInstance(error, RuntimeError)
                self.assertIn("refusing experiment", str(error))
                self.assertEqual(rm.writes, [])
                assert_no_other_writes(self, gpu)


class LegacyProbeScopeTests(unittest.TestCase):
    def run_probe(self, board, driver, baseline=None, before=None, failed_writes=()):
        gpu = fake_gpu(board, driver)
        baseline = baseline or get_result(LEGACY_SIZE, LEGACY_GET, legacy_params())
        before = before or snapshot_result(0)
        rm, error = run_main(legacy, gpu, baseline, before, failed_writes=failed_writes)
        return gpu, rm, error

    def test_measured_titan_on_any_driver_string_runs_identity_and_restore(self):
        params = legacy_params()
        for label, board in MEASURED.items():
            for driver in (*UNLISTED_DRIVERS, "580.97"):
                with self.subTest(board=label, driver=driver):
                    gpu, rm, error = self.run_probe(board, driver)
                    self.assertIsNone(error)
                    # The unchanged identity write, then the final restore.
                    self.assertEqual(rm.writes, [params, params])
                    self.assertTrue(all(version == 0x10AC8 for _p, version in rm.calls))
                    gpu.nvapi.BoostTableSet.assert_called_once()
                    gpu.nvapi.VfLockSet.assert_called_once()
                    gpu.nvapi.ClkDomCtlSet.assert_called_once()

    def test_unlisted_board_is_refused_before_any_rail_access(self):
        for label, board in UNLISTED.items():
            for driver in ("472.12", "591.44"):
                with self.subTest(board=label, driver=driver):
                    gpu, rm, error = self.run_probe(board, driver)
                    self.assertIsInstance(error, RuntimeError)
                    self.assertIn("board", str(error))
                    self.assertEqual(rm.calls, [])
                    assert_no_other_writes(self, gpu)

    def test_misunderstood_legacy_packet_is_refused_before_any_set(self):
        # 472.12 lets unmodified code reach its layout checks; the variants
        # refused only after this change were previously written.
        good = legacy_params()

        def params(index, value):
            out = good.copy()
            out[index] = value
            return out

        cases = {
            "Blackwell type-5 record": (get_result(LEGACY_SIZE, LEGACY_GET, params(2, 5)), None),
            "mask 0": (get_result(LEGACY_SIZE, LEGACY_GET, params(0, 0)), None),
            "NTSTATUS failure": (
                get_result(LEGACY_SIZE, LEGACY_GET, good, escape_status=0xC0000001), None),
            "returned SET command": (
                get_result(LEGACY_SIZE, LEGACY_GET, good, returned_command=LEGACY_SET), None),
            "returned parameter size": (
                get_result(LEGACY_SIZE, LEGACY_GET, good, returned_params_size=644), None),
            "returned packet shrank": (
                get_result(LEGACY_SIZE, LEGACY_GET, good, returned_words=good[:-1]), None),
            "boost word differs from NVAPI boost": (
                get_result(LEGACY_SIZE, LEGACY_GET, params(1, 100)), snapshot_result(0)),
            "boost word out of range": (
                get_result(LEGACY_SIZE, LEGACY_GET, params(1, 0xFFFFFFFF)),
                snapshot_result(0xFFFFFFFF)),
            "absolute getter failed": (
                get_result(LEGACY_SIZE, LEGACY_GET, good), snapshot_result(0, abs_status=-9)),
            "absolute record is empty": (
                get_result(LEGACY_SIZE, LEGACY_GET, good),
                snapshot_result(0, abs_record=[0] * 7)),
        }
        for label, (baseline, before) in cases.items():
            with self.subTest(label):
                gpu, rm, error = self.run_probe(TITAN_XP, "472.12", baseline, before)
                self.assertIsInstance(error, RuntimeError)
                self.assertEqual(rm.writes, [])
                assert_no_other_writes(self, gpu)
                # The refused GET and the NVAPI reading it was judged
                # against are on disk.
                self.assertIsNotNone(rm.report)
                self.assertEqual(rm.report["baseline_rm"]["output_params"],
                                 baseline["output_params"])
                self.assertEqual(rm.report["before"]["boost_pct"],
                                 (before or snapshot_result(0))["boost_pct"])

    def test_boost_refusal_names_both_values(self):
        native = legacy_params()
        native[1] = 100
        _gpu, _rm, error = self.run_probe(
            TITAN_XP, "472.12", get_result(LEGACY_SIZE, LEGACY_GET, native),
            snapshot_result(0))
        self.assertIn("boost word 100", str(error))
        self.assertIn("voltage boost 0", str(error))

    def test_failed_rail_restore_still_restores_table_lock_and_domains(self):
        params = legacy_params()
        # Write 0 is the identity SET, write 1 the final rail restore. With
        # every SET failing, the identity failure also reaches the restore.
        for label, failed in (("final restore", {1}), ("every SET", {0, 1})):
            with self.subTest(label):
                gpu, rm, error = self.run_probe(TITAN_RTX, "472.12", failed_writes=failed)
                self.assertIsInstance(error, RuntimeError)
                self.assertIn("rail_restore_ok", str(error))
                self.assertEqual(rm.writes, [params, params])
                gpu.nvapi.ClkDomCtlSet.assert_called_once()
                gpu.nvapi.BoostTableSet.assert_called_once()
                gpu.nvapi.VfLockSet.assert_called_once()
                report = rm.report
                self.assertFalse(report["rail_restore_ok"])
                self.assertEqual(report["restore"]["rm_status"], 0x1F)
                for key in ("domain_restore_status", "table_restore_status",
                            "lock_restore_status"):
                    self.assertEqual(report[key], 0)
                for key in ("table_restored", "locks_restored",
                            "domains_restored", "rails_restored"):
                    self.assertTrue(report[key])

    def test_unreadable_lock_after_restore_is_reported_not_raised_over(self):
        gpu = fake_gpu(TITAN_RTX, "472.12")
        lock = gpu._vf_lock_read_raw.return_value
        gpu._vf_lock_read_raw.side_effect = [lock, None]
        rm, error = run_main(legacy, gpu,
                             get_result(LEGACY_SIZE, LEGACY_GET, legacy_params()),
                             snapshot_result(0))
        self.assertIn("locks_restored", str(error))
        self.assertFalse(rm.report["locks_restored"])
        self.assertTrue(rm.report["domains_restored"])


class LegacyLoadTestClockLockTests(unittest.TestCase):
    """The NVML core-clock lock taken for the offset trials is always released."""

    def run_load_tests(self, board=TITAN_RTX, lock=(True, "locked"), **configure):
        gpu = fake_gpu(board, "472.12")
        gpu.lock_gpu_clocks.return_value = lock
        gpu.reset_gpu_clocks.return_value = (True, "released")
        gpu.read_rail_offset_mv.return_value = 0.0
        gpu.set_rail_offset_mv.return_value = (True, "offset set")
        gpu.read.return_value = {"core": 1500, "vcore_mv": 781.25, "power_w": 100.0,
                                 "temp_edge": 40, "pstate": 0}
        gpu.read_vf_curve.return_value = ([], "curve read failed")
        gpu.configure_mock(**configure)
        load = Mock(device_name=gpu.static["name"])
        load.wait_started.return_value = True
        report = {}
        rm = RmCalls(None)
        with patch.object(gpuload, "BandwidthLoad", return_value=load), \
                patch.object(legacy, "snapshot", Mock(side_effect=lambda *a, **k: snapshot_result(0))), \
                patch.object(legacy, "rm_call", rm), \
                patch.object(legacy.time, "sleep"), \
                redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError) as raised:
                legacy.load_tests(gpu, report, Mock(), legacy_params())
        load.stop.assert_called_once()
        self.assertEqual(rm.writes, [])
        return gpu, report["load_tests"], raised.exception

    def test_abort_during_offset_trials_releases_the_lock(self):
        aborts = {
            "offset write refused": {"set_rail_offset_mv.return_value": (False, "offset refused")},
            "temperature stop": {"read.return_value": {"temp_edge": 85}},
            "offset unreadable": {"read_rail_offset_mv.return_value": None},
        }
        for label, configure in aborts.items():
            with self.subTest(label):
                gpu, run, _error = self.run_load_tests(**configure)
                gpu.reset_gpu_clocks.assert_called_once_with()
                self.assertEqual(run["frequency_unlock"], (True, "released"))

    def test_release_after_the_offset_trials_is_not_repeated(self):
        gpu, run, error = self.run_load_tests()
        self.assertIn("curve read failed", str(error))
        self.assertEqual(len(run["offset_trials"]), 5)
        gpu.reset_gpu_clocks.assert_called_once_with()
        self.assertEqual(run["frequency_unlock"], (True, "released"))

    def test_failed_release_is_retried_and_recorded(self):
        gpu, run, error = self.run_load_tests(
            **{"reset_gpu_clocks.side_effect": [(False, "busy"), (False, "still busy")]})
        self.assertIn("busy", str(error))
        self.assertEqual(gpu.reset_gpu_clocks.call_count, 2)
        self.assertEqual(run["frequency_unlock"], (False, "still busy"))

    def test_lock_that_was_not_taken_is_not_released(self):
        # TITAN Xp continues without the lock when NVML refuses it.
        gpu, run, _error = self.run_load_tests(
            board=TITAN_XP, lock=(False, "SetGpuLockedClocks not available"),
            **{"set_rail_offset_mv.return_value": (False, "offset refused")})
        gpu.reset_gpu_clocks.assert_not_called()
        self.assertNotIn("frequency_unlock", run)


class LegacyLiveClampTargetTests(unittest.TestCase):
    """Live clamp requests are computed from a fresh absolute reading."""

    def run_live_fields(self, reading):
        gpu = fake_gpu(TITAN_RTX, "591.44")
        gpu.set_vf_lock.return_value = (True, "held")
        gpu.read.return_value = {"core": 1500, "vcore_mv": 1000.0, "power_w": 100.0,
                                 "temp_edge": 40, "pstate": 0}
        load = Mock(device_name=gpu.static["name"])
        load.wait_started.return_value = True
        rm = RmCalls(None)
        with patch.object(gpuload, "BandwidthLoad", return_value=load), \
                patch.object(legacy, "snapshot", Mock(side_effect=lambda *a, **k: copy.deepcopy(reading))), \
                patch.object(legacy, "rm_call", rm), \
                patch.object(legacy.time, "sleep"), \
                redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError) as raised:
                legacy.live_field_tests(gpu, {}, Mock(), legacy_params())
        load.stop.assert_called()
        return rm, raised.exception

    def test_unusable_absolute_reading_never_becomes_a_clamp_request(self):
        # A failed getter leaves the prefilled zeros: 875 mV - 0 would be sent
        # as a +875 mV ceiling delta.
        readings = {
            "getter failed": snapshot_result(0, abs_status=-9),
            "empty record": snapshot_result(0, abs_record=[0] * 7),
            "not an NVVDD record": snapshot_result(0, abs_record=[5, 681250, 1068750, 1093750,
                                                                  1125000, 1068750, 650000]),
        }
        for label, reading in readings.items():
            with self.subTest(label):
                rm, _error = self.run_live_fields(reading)
                self.assertEqual(rm.writes, [])

    def test_valid_absolute_reading_still_targets_875_mv_then_restores(self):
        params = legacy_params()
        rm, error = self.run_live_fields(snapshot_result(0))
        self.assertIn("Individual live clamp", str(error))
        clamp = params.copy()
        clamp[3] = (875000 - 1068750) & 0xFFFFFFFF
        self.assertEqual(rm.writes, [clamp, params])


if __name__ == "__main__":
    unittest.main()
