# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native telemetry readers against their Python twins.

One set of fake driver callbacks serves both: nvbackend.GPU's _read_* methods
call them through SimpleNamespace halves, the native core through the
resolver seam. Every key the Python reader produces must come back from the
core with the same value and type."""
import ctypes
import threading
from types import SimpleNamespace
import unittest

from druta import native, nvbackend
from tests.test_native_identity import FakeNvapi, FakeNvml, TITAN, install, write_u32

if not native.available():
    raise unittest.SkipTest("native core not built (native/build.sh linux)")

CDECL = ctypes.CFUNCTYPE
NV = nvbackend


def struct_callback(struct_type, fill, *, extra_args=()):
    """status(handle, [extra...], struct*) that fills the Python ctypes layout in place."""
    @CDECL(ctypes.c_int, ctypes.c_void_p, *extra_args, ctypes.c_void_p)
    def callback(handle, *args):
        pointer = args[-1]
        return fill(struct_type.from_address(pointer), *args[:-1])
    return callback


def u32_callback(value, status=0):
    @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
    def callback(handle, pointer):
        write_u32(pointer, value)
        return status
    return callback


class Rig:
    """A fake Turing-class card: every public reader answers."""

    def __init__(self, *, with_nvapi=True, with_nvml=True, cooler="client", thermal_bits=6, boost_version=2):
        self.nvml = FakeNvml([TITAN])
        self.nvapi = FakeNvapi([TITAN])
        self.cooler = cooler
        self.boost_version = boost_version
        self.thermal_bits = thermal_bits
        self.with_nvapi, self.with_nvml = with_nvapi, with_nvml
        self.names = {}  # Python attribute name -> callback
        self._nvapi()
        self._nvml()

    def _nvapi(self):
        i, n = self.nvapi.interfaces, self.names

        def thermal(ts, sensor_index):
            if sensor_index != 15:
                return 1
            ts.count = 2
            ts.sensor[0].target = 1
            ts.sensor[0].cur = 61
            ts.sensor[1].target = 7
            ts.sensor[1].cur = 99
            return 0
        n["ThermalSettings"] = i[0xE3640A56] = struct_callback(NV._ThermalSettings, thermal, extra_args=(ctypes.c_uint32,))

        def sensors(tx):
            nbits = tx.mask.bit_length() if tx.mask != 0xFFFFFFFF else 32
            if nbits > self.thermal_bits:
                return -1
            values = [61 * 256, 72 * 256 + 64, 0, 200 * 256, 70 * 256 + 128, 5]
            for k in range(nbits):
                tx.temps[k] = values[k]
            return 0
        n["ThermalSensors"] = i[0x65FE3AAD] = struct_callback(NV._ThermalSensorsEx, sensors)

        def topo(pt):
            pt.count = 2
            pt.entries[0].domain, pt.entries[0].power_pcm = 0, 45678
            pt.entries[1].domain, pt.entries[1].power_pcm = 1, 50123
            return 0
        n["PowerTopo"] = i[0x0EDCF624E] = struct_callback(NV._PwrTopo, topo)

        def pol_status(ps):
            ps.count = 1
            ps.entries[0].target_pcm = 112000
            return 0
        n["PowerPolStatus"] = i[0x70916171] = struct_callback(NV._PwrPolStatus, pol_status)

        def volt(vs):
            vs.value_uV = 1043750
            return 0
        n["VoltRailsStatus"] = i[0x465F9BCF] = struct_callback(NV._VoltStatus, volt)

        def dyn(dp):
            for k, (present, pct) in enumerate(((1, 97), (1, 54), (0, 0), (1, 12))):
                dp.util[k].present, dp.util[k].percentage = present, pct
            return 0
        n["DynPstates"] = i[0x60DED2ED] = struct_callback(NV._DynPstates, dyn)
        n["PerfDecrease"] = i[0x7F7F4600] = u32_callback(0x2)
        n["CurrentPstate"] = i[0x927DA4F6] = u32_callback(0)
        n["RamType"] = i[0x57F7CAAC] = u32_callback(14)

        def vboost(vc):
            vc.percent = 25
            return 0
        n["VoltCtrlGet"] = i[0x9DF23CA1] = struct_callback(NV._VoltBoost, vboost)

        def clocks(cf):
            if cf.clockType != 0:
                return 1
            for domain, khz in ((0, 1935000), (4, 7000500), (8, 1710000)):
                cf.domain[domain].present, cf.domain[domain].frequency = 1, khz
            return 0
        n["AllClocks"] = i[0xDCB616C3] = struct_callback(NV._ClkFreqs, clocks)

        def priv(pc):
            pc.w[0], pc.w[2], pc.w[8], pc.w[42] = 1935000, 1600500, 7000500, 1710000
            pc.w[64], pc.w[65] = 1920000, 7
            return 0
        n["AllClocksPriv"] = i[0x1BD69F49] = struct_callback(NV._AllClocksPriv, priv)

        def lock(bl):
            if bl.version != NV.NvAPI.ver(NV._ClockLock, self.boost_version):
                return -9
            bl.count = 3
            bl.locks[0].domain, bl.locks[0].lockMode, bl.locks[0].volt_uV = 0, 3, 893750
            bl.locks[1].domain, bl.locks[1].lockMode, bl.locks[1].volt_uV = 0, 2, 1500000
            bl.locks[2].domain, bl.locks[2].lockMode, bl.locks[2].volt_uV = 1, 2, 1200000
            return 0
        n["BoostLock"] = i[0xE440B867] = struct_callback(NV._ClockLock, lock)

        def pstates(info):
            # V2 version 3 accepted, with a RANGE core clock and a SINGLE memory clock in P0
            if info.version != NV.NvAPI.ver(NV._Pstates20V2, 3):
                return -9
            info.num_pstates, info.num_clocks, info.num_voltages = 2, 2, 1
            p0 = info.pstates[0]
            p0.pstate = 0
            p0.clocks[0].domain, p0.clocks[0].kind, p0.clocks[0].flags = 0, 1, 1
            p0.clocks[0].delta.value, p0.clocks[0].delta.minimum, p0.clocks[0].delta.maximum = 150000, -1000000, 1000000
            p0.clocks[0].data[0], p0.clocks[0].data[1] = 300000, 2100000
            p0.clocks[1].domain, p0.clocks[1].kind, p0.clocks[1].flags = 4, 0, 1
            p0.clocks[1].delta.value, p0.clocks[1].delta.minimum, p0.clocks[1].delta.maximum = 250000, -500500, 1500500
            p0.clocks[1].data[0] = 7000500
            info.pstates[1].pstate = 8
            return 0
        n["Pstates20Get"] = i[0x6FF81213] = struct_callback(NV._Pstates20V2, pstates)

        if self.cooler == "classic":
            def cooler(cs, index):
                if index != 7:
                    return 1
                cs.count = 1
                e = cs.entries[0]
                e.current_min, e.current_max, e.current_level = 30, 100, 44
                e.current_policy, e.default_policy = 1, 32 // 2
                return 0
            n["CoolerSettings"] = i[0xDA141340] = struct_callback(NV._CoolerSettings, cooler, extra_args=(ctypes.c_uint32,))
            n["TachReading"] = i[0x5F608315] = u32_callback(1480)
        elif self.cooler == "client":
            def control(ctl):
                ctl.count = 2
                for k, (fid, level, mode) in enumerate(((5, 40, 0), (7, 55, 1))):
                    ctl.entries[k].id, ctl.entries[k].level, ctl.entries[k].mode = fid, level, mode
                return 0
            n["FanCoolersControl"] = i[0x814B209F] = struct_callback(NV._FanCoolersControl, control)

            def status(st):
                st.count = 2
                for k, (fid, rpm, lo, hi, level) in enumerate(((7, 1700, 30, 100, 55), (5, 1400, 30, 100, 40))):
                    e = st.entries[k]
                    e.id, e.rpm, e.min, e.max, e.level = fid, rpm, lo, hi, level
                return 0
            n["FanCoolersStatus"] = i[0x35AED5E8] = struct_callback(NV._FanCoolersStatus, status)

    def _nvml(self):
        e = self.nvml.exports
        for name, value in (("nvmlDeviceGetPowerUsage", 180500), ("nvmlDeviceGetEnforcedPowerLimit", 280000),
                            ("nvmlDeviceGetPowerManagementLimit", 285000), ("nvmlDeviceGetCurrPcieLinkGeneration", 3),
                            ("nvmlDeviceGetCurrPcieLinkWidth", 16), ("nvmlDeviceGetArchitecture", 7)):
            e[name] = u32_callback(value)

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def reasons(device, pointer):
            ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint64))[0] = 0x104
            return 0
        e["nvmlDeviceGetCurrentClocksEventReasons"] = reasons

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def energy(device, pointer):
            ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint64))[0] = 123456789
            return 0
        e["nvmlDeviceGetTotalEnergyConsumption"] = energy

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p)
        def fields(device, count, pointer):
            arr = (NV._FieldValue * count).from_address(pointer)
            for fv in arr:
                fv.nvmlReturn = 0 if fv.fieldId in (173, 179, 180, 182) else 3
                if fv.fieldId == 173:
                    fv.valueType, fv.value.ull = 3, 7
                elif fv.fieldId == 179:
                    fv.valueType, fv.value.ui = 1, 2
                elif fv.fieldId == 180:
                    fv.valueType, fv.value.si = 5, 1
                elif fv.fieldId == 182:
                    fv.valueType, fv.value.ull = 3, 40
            return 0
        e["nvmlDeviceGetFieldValues"] = fields

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p)
        def minmax(device, ctype, pstate, mn, mx):
            if ctype == 0 and pstate == 0:
                write_u32(mn, 300)
                write_u32(mx, 2100)
                return 0
            return 3
        e["nvmlDeviceGetMinMaxClockOfPState"] = minmax
        e["nvmlDeviceGetNumFans"] = u32_callback(2)

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p)
        def speed_v2(device, fan, pointer):
            if fan > 1:
                return 3
            write_u32(pointer, 41 + fan)
            return 0
        e["nvmlDeviceGetFanSpeed_v2"] = speed_v2

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def rpm(device, pointer):
            fi = NV._FanSpeedInfo.from_address(pointer)
            if fi.fan > 1:
                return 3
            fi.speed = 1500 + 100 * fi.fan
            return 0
        e["nvmlDeviceGetFanSpeedRPM"] = rpm

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
        def minmaxfan(device, mn, mx):
            write_u32(mn, 25)
            write_u32(mx, 100)
            return 0
        e["nvmlDeviceGetMinMaxFanSpeed"] = minmaxfan

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
        def constraints(device, mn, mx):
            write_u32(mn, 100000)
            write_u32(mx, 320000)
            return 0
        e["nvmlDeviceGetPowerManagementLimitConstraints"] = constraints
        e["nvmlDeviceGetPowerManagementDefaultLimit"] = u32_callback(280000)

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
        def memclocks(device, count, values):
            wanted = [7001, 5001, 810, 405]
            n = ctypes.cast(count, ctypes.POINTER(ctypes.c_uint32))
            if not values or n[0] < len(wanted):
                n[0] = len(wanted)
                return 7
            n[0] = len(wanted)
            ctypes.memmove(values, (ctypes.c_uint32 * 4)(*wanted), 16)
            return 0
        e["nvmlDeviceGetSupportedMemoryClocks"] = memclocks

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p)
        def gfxclocks(device, mem, count, values):
            wanted = [2100 - 15 * k for k in range(100)] if mem == 7001 else [300]
            n = ctypes.cast(count, ctypes.POINTER(ctypes.c_uint32))
            if not values or n[0] < len(wanted):
                n[0] = len(wanted)
                return 7
            n[0] = len(wanted)
            ctypes.memmove(values, (ctypes.c_uint32 * len(wanted))(*wanted), 4 * len(wanted))
            return 0
        e["nvmlDeviceGetSupportedGraphicsClocks"] = gfxclocks

    # ---- the Python twin -------------------------------------------------------------------------

    def python_gpu(self):
        exports = self.nvml.exports

        class Dll:
            def __getattr__(self, name):
                try:
                    return exports[name]
                except KeyError:
                    raise AttributeError(name)

        gpu = NV.GPU.__new__(NV.GPU)
        gpu._lock = threading.RLock()
        nvapi = SimpleNamespace(ok=self.with_nvapi, gpu=0x2000, selected={"slot": "0000:01:00.0"},
                                ver=NV.NvAPI.ver, _i=lambda *a: None)
        for name in ("ThermalSettings", "ThermalSensors", "PowerTopo", "PowerPolStatus", "VoltRailsStatus",
                     "DynPstates", "PerfDecrease", "CurrentPstate", "RamType", "VoltCtrlGet", "AllClocks",
                     "AllClocksPriv", "BoostLock", "Pstates20Get", "CoolerSettings", "TachReading",
                     "FanCoolersControl", "FanCoolersStatus"):
            setattr(nvapi, name, self.names.get(name))
        nvml = NV.Nvml.__new__(NV.Nvml)
        nvml.ok, nvml.dev, nvml.dll, nvml.selected = self.with_nvml, 0x1000, Dll(), {"slot": "0000:01:00.0"}
        nvml.err_detail = ""
        gpu.nvapi, gpu.nvml = nvapi, nvml
        gpu._pcie_baseline = None
        gpu._hold_headroom = None
        gpu.static = {}
        gpu.pairing_error = ""
        gpu.requested_slot = ""
        return gpu

    def native_gpu(self):
        install(self.nvml if self.with_nvml else None, self.nvapi if self.with_nvapi else None)
        return native.Gpu()


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
