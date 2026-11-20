# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native telemetry readers against their Python twins.

One set of fake driver callbacks serves both: nvbackend.GPU's _read_* methods
call them through SimpleNamespace halves, the native core through the
resolver seam. Every key the Python reader produces must come back from the
core with the same value and type."""
import ctypes
import unittest

from druta import native, nvbackend
from tests.test_native_identity import install

if not native.available():
    raise unittest.SkipTest("native core not built (native/build.sh linux)")

CDECL = ctypes.CFUNCTYPE
NV = nvbackend


from tests.fake_driver import TITAN, Rig, struct_callback, u32_callback  # noqa: E402, F401


def python_read(gpu, readers):
    d = {}
    for reader in readers:
        getattr(gpu, reader)(d)
    return d


READERS = ["_read_temps", "_read_power", "_read_fan", "_read_util", "_read_throttle", "_read_pcie", "_read_misc"]


class TelemetryParityTests(unittest.TestCase):
    def tearDown(self):
        native.set_export_resolver(None)

    def assert_parity(self, rig, readers=READERS, keys=None):
        expected = python_read(rig.python_gpu(), readers)
        with rig.native_gpu() as gpu:
            actual = gpu.read()
        compare = sorted(keys or expected)
        for key in compare:
            self.assertEqual(actual.get(key), expected.get(key), key)
            self.assertIs(type(actual.get(key)), type(expected.get(key)), key)
        return expected, actual

    def test_every_reader_matches_on_a_full_rig(self):
        expected, actual = self.assert_parity(Rig())
        # the fixtures exercised real values, not an empty intersection
        self.assertEqual(expected["temp_edge"], 61)
        self.assertEqual(expected["temp_hotspot"], 72.25)
        self.assertEqual(expected["temp_sensors"], [61.0, 72.2, 70.5, 0.0])  # round-half-even: 72.25 -> 72.2; 5/256 C is kept as 0.0
        self.assertEqual(expected["fans"], [(41, 1500), (42, 1600)])
        self.assertEqual(expected["pcie_err"], {"correctable": 7, "non_fatal": 2, "fatal": 1, "lcrc": 40})
        self.assertEqual(expected["pcie_err_total"], 10)
        self.assertEqual(expected["clk_lock_mhz"], (1200, 1500))
        self.assertEqual(expected["vf_locked_domains"], [0])
        self.assertEqual(expected["vf_lock_mv"], 893.75)
        self.assertEqual(expected["core_p0max"], 2100)
        self.assertEqual(expected["mem_p0max"], 7000)  # the Pstates20 SINGLE fallback
        self.assertEqual(expected["event_mask"], 0x104)

    def test_clocks_match_including_private_fallbacks_and_offsets(self):
        rig = Rig()
        gpu = rig.python_gpu()
        expected = {}
        gpu._read_clocks(expected)
        with rig.native_gpu() as g:
            actual = g.read()
        for key in ("core", "mem", "video", "xbar", "core_off", "mem_off"):
            self.assertEqual(actual.get(key), expected.get(key), key)
        self.assertEqual(expected["xbar"], 1600)
        self.assertEqual(expected["core_off"], 150)   # Pstates20 fallback: 150000 kHz -> 150
        self.assertEqual(expected["mem_off"], 500)    # memory doubled: 250000 * 2 / 1000
        self.assertEqual(actual["priv_clocks"][64:66], [1920000, 7])

    def test_classic_cooler_and_tach(self):
        expected, actual = self.assert_parity(Rig(cooler="classic"), ["_read_fan"])
        self.assertEqual(expected["num_fans"], 1)
        self.assertEqual(expected["fans"], [(41, 1500)])  # NVML values win over the native level/tach

    def test_without_nvapi_only_nvml_keys_remain(self):
        expected, actual = self.assert_parity(Rig(with_nvapi=False))
        self.assertNotIn("temp_edge", expected)
        self.assertIn("power_w", expected)
        self.assertEqual(actual["fans"], expected["fans"])

    def test_without_nvml_only_nvapi_keys_remain(self):
        expected, actual = self.assert_parity(Rig(with_nvml=False))
        self.assertNotIn("power_w", expected)
        self.assertEqual(expected["num_fans"], 2)
        self.assertEqual(expected["fans"], [(40, 1400), (55, 1700)])  # native client fans, ids 5 and 7 in control order

    def test_pcie_error_baseline_is_per_gpu_object(self):
        rig = Rig()
        with rig.native_gpu() as g:
            first = g.read()
            second = g.read()
        self.assertEqual(first["pcie_err_since"], 0)
        self.assertEqual(second["pcie_err_since"], 0)
        self.assertIs(type(first["pcie_err_since"]), int)

    def test_boost_lock_version_fallback(self):
        rig = Rig(boost_version=3)
        expected, actual = self.assert_parity(rig, ["_read_misc"])
        self.assertEqual(expected["clk_lock_mhz"], (1200, 1500))

    def test_capabilities_match_read_static_keys(self):
        rig = Rig()
        gpu = rig.python_gpu()
        install(rig.nvml, rig.nvapi)
        with native.Gpu() as g:
            caps = g.capabilities()
            self.assertEqual(g.arch(), 7)
        self.assertEqual(caps["pl_min_mw"], 100000)
        self.assertEqual(caps["pl_max_mw"], 320000)
        self.assertEqual(caps["pl_def_mw"], 280000)
        self.assertEqual(caps["mem_clocks"], [405, 810, 5001, 7001])
        self.assertEqual((caps["gfx_min"], caps["gfx_max"]), (615, 2100))
        self.assertEqual((caps["fan_min"], caps["fan_max"]), (25, 100))
        self.assertEqual((caps["mem_type_id"], caps["mem_type"], caps["mem_div"]), (14, "GDDR6", 4))
        self.assertEqual(caps["core_off_range"], gpu._offset_range(0))
        self.assertEqual(caps["mem_off_range"], gpu._offset_range(2))
        self.assertEqual(caps["mem_clocks"], gpu._supported_nvml_clocks("nvmlDeviceGetSupportedMemoryClocks"))
        self.assertEqual(gpu.arch(), 7)


class BindingPrerequisiteTests(unittest.TestCase):
    """The nvbackend.GPU surface the front end and the profiles use directly: the RLock, the
    capability keys on `static`, arch_name() and vf_curve_applicable()."""

    def test_lock_is_reentrant(self):
        rig = Rig()
        install(rig.nvml, rig.nvapi)
        with native.Gpu() as g:
            with g._lock:
                with g._lock:  # nvbackend._synchronized nests through its own callers
                    acquired = True
        self.assertTrue(acquired)

    def test_static_carries_the_capability_keys_on_first_use(self):
        rig = Rig()
        gpu = rig.python_gpu()
        install(rig.nvml, rig.nvapi)
        with native.Gpu() as g:
            identity_only = dict.keys(g.static)
            self.assertNotIn("pl_min_mw", list(identity_only))
            self.assertEqual((g.static.get("pl_min_mw"), g.static["pl_max_mw"], g.static.get("pl_def_mw")),
                             (100000, 320000, 280000))
            self.assertIn("gfx_min", g.static)
            self.assertEqual(g.static["mem_clocks"], gpu._supported_nvml_clocks("nvmlDeviceGetSupportedMemoryClocks"))
            self.assertEqual(g.static["name"], gpu._read_static()["name"])
            g.static["pl_min_mw"] = 1  # read_live_power_request back-fills missing constraints this way
            self.assertEqual(g.static["pl_min_mw"], 1)
            self.assertIsNone(g.static.get("absent"))

    def test_arch_name_and_vf_curve_applicable(self):
        for arch, name, applicable in ((7, "Ampere", True), (2, "Kepler", False), (None, None, True)):
            with self.subTest(arch=arch):
                rig = Rig()
                if arch is None:
                    rig.nvml.exports.pop("nvmlDeviceGetArchitecture", None)
                else:
                    rig.nvml.exports["nvmlDeviceGetArchitecture"] = u32_callback(arch)
                gpu = rig.python_gpu()
                install(rig.nvml, rig.nvapi)
                with native.Gpu() as g:
                    self.assertEqual((g.arch(), g.arch_name(), g.vf_curve_applicable()), (arch, name, applicable))
                    self.assertEqual((gpu.arch(), gpu.arch_name(), gpu.vf_curve_applicable()),
                                     (g.arch(), g.arch_name(), g.vf_curve_applicable()))


class ReadVcoreParityTests(unittest.TestCase):
    """GPU.read_vcore_mv (one VoltRailsStatus call) against native.Gpu.read_vcore_mv on the same fake:
    the result, its type and the logged call (handle and struct version) on both sides."""

    def tearDown(self):
        native.set_export_resolver(None)

    def both(self, rig, *, status=0, value_uv=1043750, interface=True):
        calls = []

        def volt(vs):
            calls.append((vs.version, vs.value_uV))
            vs.value_uV = value_uv
            return status
        callback = struct_callback(NV._VoltStatus, volt)
        if interface:
            rig.names["VoltRailsStatus"] = rig.nvapi.interfaces[0x465F9BCF] = callback
        else:
            rig.names["VoltRailsStatus"] = None
            rig.nvapi.interfaces.pop(0x465F9BCF, None)
        runs = []
        for kind in ("python", "native"):
            calls.clear()
            if kind == "python":
                value = rig.python_gpu().read_vcore_mv()
            else:
                with rig.native_gpu() as gpu:
                    calls.clear()  # only the reader's own calls are compared
                    value = gpu.read_vcore_mv()
            runs.append((value, type(value), list(calls)))
        self.assertEqual(runs[0], runs[1], "Python and native runs differ")
        return runs[0]

    def test_a_value_is_returned_exactly_in_millivolts(self):
        version = NV.NvAPI.ver(NV._VoltStatus, 1)
        for value_uv, expected in ((1043750, 1043.75), (743001, 743.001), (1, 0.001), (0xFFFFFFFF, 4294967.295)):
            with self.subTest(value_uv=value_uv):
                value, kind, calls = self.both(Rig(), value_uv=value_uv)
                self.assertEqual((value, kind), (expected, float))
                self.assertEqual(calls, [(version, 0)])  # one call, version 1, the struct zeroed

    def test_absent_interface_nonzero_status_and_zero_value_are_none(self):
        version = NV.NvAPI.ver(NV._VoltStatus, 1)
        for options, called in ((dict(interface=False), False), (dict(status=-5), True),
                                (dict(status=1, value_uv=900000), True), (dict(value_uv=0), True)):
            with self.subTest(**options):
                value, _, calls = self.both(Rig(), **options)
                self.assertIsNone(value)
                self.assertEqual(calls, [(version, 0)] if called else [])

    def test_nvapi_down_makes_no_call(self):
        value, _, calls = self.both(Rig(with_nvapi=False))
        self.assertEqual((value, calls), (None, []))


if __name__ == "__main__":
    unittest.main()
