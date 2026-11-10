# Druta - a monitor and tuner for NVIDIA GPUs.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""One fake GPU driver that serves both backends: nvbackend.GPU and the native core (native.Gpu).

Two layers live here.

The per-feature rigs (lifted unchanged from the parity tests that built them; those modules import
them back from here): FakeNvml / FakeNvapi / install (tests/test_native_identity.py), Rig
(test_native_telemetry.py), TransportRig (test_native_transport.py), Driver / CLRig
(test_native_current_limits.py), LockRig (test_native_vflock.py), CurveRig
(test_native_vfcurve.py), FanRig (test_native_fan.py), ClockRig (test_native_clocks.py), ClkDomRig
(test_native_clkdom.py), P0Rig (test_native_legacy_p0.py), the rail getters' RailFake
(test_native_rails.py) and config / live_record (test_native_rail_reference.py). They live in a
module that imports without the native library, so the Python half of a comparison runs (and the
native half skips) where libdrutacore is not built.

The shared card (FakeDriver, card()): one card model, composed from those rigs, that answers a
whole session, installed for BOTH paths at once:

- the core reaches it through native.set_export_resolver (libraries "nvml", "nvapi", "gdi32"),
  with native.set_sleep_hook and native.set_clock_hook on the card's own clock;
- nvbackend reaches it through a patched ctypes.WinDLL ("nvapi64.dll" -> nvapi_QueryInterface,
  "gdi32.dll" -> the D3DKMT exports, "kernel32" -> a refused VirtualProtect, see _Kernel32;
  anything else is not loadable) and a patched nvbackend._load_nvml_library, with nvbackend's own
  `time` name (sleep and monotonic) on the same clock; the time module itself stays real.

Every export the card serves is reached through one gate per export, so both kinds land in the same
driver-call log, in one format, and a test can change the card mid-session: fail the next call of a
named export, replace an export, remove one. The card's values (identities, limits, tables,
voltages) are inputs to the logic under test, not claims about any board. Nothing here decides by
driver version, device id, subsystem id or VBIOS version, and nothing should (AGENTS.md).
"""
import contextlib
import copy
import ctypes
import re
import threading
import weakref
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from druta import gpu_backend, native, nvbackend

CDECL = ctypes.CFUNCTYPE
NV = nvbackend


# ---- from tests/test_native_identity.py ----------------------------------------

def write_u32(pointer, value):
    ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32))[0] = value


def write_text(pointer, text, size):
    data = text.encode()[:size - 1] + b"\0"
    ctypes.memmove(pointer, data, len(data))


class FakeNvml:
    """An NVML export table: a dict of name -> ctypes callback."""

    def __init__(self, devices, *, modern=True, pci_version=3, init_status=0, count_status=0,
                 extra=None, missing=()):
        # devices: list of dicts {domain,bus,device,devid,subsys,name,uuid,pci_status}
        self.devices = devices
        self.exports = {}
        suffix = "_v2" if modern else ""

        @CDECL(ctypes.c_int)
        def init():
            return init_status
        self.exports["nvmlInit" + suffix] = init

        @CDECL(ctypes.c_int, ctypes.c_void_p)
        def count(pointer):
            write_u32(pointer, len(devices))
            return count_status
        self.exports["nvmlDeviceGetCount" + suffix] = count

        @CDECL(ctypes.c_int, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))
        def handle(index, out):
            out[0] = 0x1000 + index
            return 0
        self.exports["nvmlDeviceGetHandleByIndex" + suffix] = handle

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def pci(device, pointer):
            d = devices[device - 0x1000]
            if d.get("pci_status", 0):
                return d["pci_status"]
            words = (ctypes.c_uint32 * 5)(d["domain"], d["bus"], d["device"], d["devid"] << 16, d["subsys"])
            ctypes.memmove(pointer + 16, words, 20)
            return 0
        if pci_version:
            self.exports[f"nvmlDeviceGetPciInfo_v{pci_version}"] = pci
        else:
            self.exports["nvmlDeviceGetPciInfo"] = pci

        for export, key in (("nvmlDeviceGetName", "name"), ("nvmlDeviceGetUUID", "uuid"),
                            ("nvmlDeviceGetVbiosVersion", "vbios")):
            @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint)
            def text(device, pointer, size, _key=key):
                value = devices[device - 0x1000].get(_key)
                if value is None:
                    return 1
                write_text(pointer, value, size)
                return 0
            self.exports[export] = text

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint)
        def driver(pointer, size):
            write_text(pointer, "581.57", size)
            return 0
        self.exports["nvmlSystemGetDriverVersion"] = driver
        self.exports.update(extra or {})
        for name in missing:
            self.exports.pop(name, None)


class FakeNvapi:
    """NVAPI through nvapi_QueryInterface: ids -> callbacks."""

    def __init__(self, gpus, *, init_status=0, missing=()):
        # gpus: list of dicts {bus, device, devid, subsys, name}
        self.interfaces = {}

        @CDECL(ctypes.c_int)
        def initialize():
            return init_status
        self.interfaces[0x0150E828] = initialize

        @CDECL(ctypes.c_int, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p)
        def enum_gpus(handles, count):
            for i in range(len(gpus)):
                handles[i] = 0x2000 + i
            write_u32(count, len(gpus))
            return 0
        self.interfaces[0xE5AC921F] = enum_gpus

        @CDECL(ctypes.c_int, ctypes.c_void_p, *([ctypes.c_void_p] * 4))
        def pci_ids(handle, did, sub, rev, ext):
            g = gpus[handle - 0x2000]
            write_u32(did, (g["devid"] << 16) | 0x10DE)
            write_u32(sub, g["subsys"])
            return 0
        self.interfaces[0x2DDFB66E] = pci_ids

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def bus_id(handle, out):
            write_u32(out, gpus[handle - 0x2000]["bus"])
            return 0
        self.interfaces[0x1BE0B8E5] = bus_id

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def bus_slot(handle, out):
            write_u32(out, gpus[handle - 0x2000]["device"])
            return 0
        self.interfaces[0x2A0A350F] = bus_slot

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def full_name(handle, out):
            write_text(out, gpus[handle - 0x2000]["name"], 64)
            return 0
        self.interfaces[0xCEEE8E9F] = full_name

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def driver(version, branch):
            write_u32(version, 58157)
            write_text(branch, "r581_00", 64)
            return 0
        self.interfaces[0x2926AAAD] = driver
        for identifier in missing:
            self.interfaces.pop(identifier, None)

        @CDECL(ctypes.c_void_p, ctypes.c_uint)
        def query(identifier):
            fn = self.interfaces.get(identifier)
            return ctypes.cast(fn, ctypes.c_void_p).value if fn else None
        self.query = query


def install(nvml=None, nvapi=None, gdi=None):
    """Serve the core's driver exports from fakes through native.set_export_resolver.

    nvml: a FakeNvml (or None: "nvml not loadable"); nvapi: a FakeNvapi (or None); gdi: a dict
    name -> callback for library "gdi32" (or None: "gdi32 not loadable"). (The two-argument form of
    tests/test_native_identity.py and the three-argument form of tests/test_native_transport.py.)"""
    def resolve(library, name):
        table = {"nvml": nvml, "nvapi": nvapi, "gdi32": gdi}.get(library)
        if table is None:
            return None
        if name == "":
            return 1
        if library == "nvapi":
            return ctypes.cast(table.query, ctypes.c_void_p).value if name == "nvapi_QueryInterface" else None
        fn = (table if library == "gdi32" else table.exports).get(name)
        return ctypes.cast(fn, ctypes.c_void_p).value if fn else None
    native.set_export_resolver(resolve)


TITAN = {"domain": 0, "bus": 1, "device": 0, "devid": 0x1E02, "subsys": 0x12A010DE, "name": "TITAN RTX",
         "uuid": "GPU-aaaa", "vbios": "90.02.0B.00.01"}
XP = {"domain": 0, "bus": 2, "device": 0, "devid": 0x1B02, "subsys": 0x119A10DE, "name": "TITAN Xp",
      "uuid": "GPU-bbbb", "vbios": "86.02.39.00.01"}


# ---- from tests/test_native_telemetry.py ----------------------------------------

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
        # TITAN is read when the rig is built: a test that needs another identity patches
        # tests.fake_driver.TITAN around the construction (tests/test_native_clock_bios.py).
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
                    raise AttributeError(name) from None

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


# ---- from tests/test_native_transport.py ----------------------------------------

u32 = ctypes.c_uint32
KMT = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p)

ENUM2, QUERY, CLOSE, ESCAPE = ("D3DKMTEnumAdapters2", "D3DKMTQueryAdapterInfo",
                               "D3DKMTCloseAdapter", "D3DKMTEscape")
ENUM, FROM_NAME, FROM_HDC = ("D3DKMTEnumAdapters", "D3DKMTOpenAdapterFromGdiDisplayName",
                             "D3DKMTOpenAdapterFromHdc")
REQUIRED = (ENUM2, QUERY, CLOSE, ESCAPE)
TRANSPORT_FIELDS = {"hAdapter": 999, "hDevice": 0, "Type": 0, "Flags": 8, "hContext": 0}
NTSTATUS_INVALID_PARAMETER = -1073741811  # 0xC000000D as a signed c_long


# ctypes mirrors of native/src/win/d3dkmt.h (x64 layout)
class Adapter(ctypes.Structure):
    _fields_ = [("handle", u32), ("luid", u32 * 2), ("sources", u32), ("preferred", ctypes.c_int32)]


class Enum2(ctypes.Structure):
    _fields_ = [("count", u32), ("items", ctypes.POINTER(Adapter))]


class Query(ctypes.Structure):
    _fields_ = [("handle", u32), ("kind", u32), ("data", ctypes.c_void_p), ("size", u32)]


assert ctypes.sizeof(Adapter) == 20





def slot_of(bus, device):
    return f"0000:{bus:02x}:{device:02x}.0"  # NVAPI exposes bus and device only


class TransportRig(Rig):
    """Rig with the card at (bus, device) and a fake gdi32.

    adapters: (handle, bus, device, function) the kernel enumerates
    enum_status / escape_status: the return codes; query_fail: handles whose query fails
    escape_writes: packet words the driver writes; missing: exports left out; gdi=False: no gdi32
    """

    def __init__(self, *, bus=1, device=0, adapters=None, enum_status=0, query_fail=(), escape_status=0,
                 escape_writes=None, missing=(), gdi=True, nvml_devid=None, **kw):
        super().__init__(**kw)
        self.bus, self.device = bus, device
        card = dict(TITAN, bus=bus, device=device)
        if nvml_devid is not None:
            card["devid"] = nvml_devid
        self.nvml.devices[0] = card  # FakeNvml's callbacks read this list
        self.nvapi.interfaces[0x1BE0B8E5] = u32_callback(bus)      # GetBusId
        self.nvapi.interfaces[0x2A0A350F] = u32_callback(device)   # GetBusSlotId
        self.adapters = [(111, 1, 0, 0), (222, 2, 0, 0), (333, 3, 0, 0)] if adapters is None else adapters
        self.enum_status, self.query_fail, self.escape_status = enum_status, set(query_fail), escape_status
        self.escape_writes = {16: 0, 17: 2} if escape_writes is None else escape_writes
        self.missing, self.with_gdi = set(missing), gdi
        self.calls = []
        self.packet = None
        self.windll = None
        self.native_error = "unset"
        self._callbacks = self._fake_gdi()
        self.reset()

    def _fake_gdi(self):
        @KMT
        def enum2(pointer):
            self.calls.append(("EnumAdapters2",))
            if self.enum_status:
                return self.enum_status
            request = Enum2.from_address(pointer)
            capacity = request.count
            request.count = len(self.adapters)
            for k, (handle, *_) in enumerate(self.adapters[:capacity]):
                request.items[k].handle = handle
                request.items[k].luid[0], request.items[k].luid[1] = 0x1000 + k, 0
            return 0

        @KMT
        def query(pointer):
            request = Query.from_address(pointer)
            self.calls.append(("QueryAdapterInfo", request.handle, request.kind, request.size))
            hit = [a for a in self.adapters if a[0] == request.handle]
            if request.handle in self.query_fail or not hit or request.kind != 6 or request.size != 12:
                return -1
            (u32 * 3).from_address(request.data)[:] = hit[0][1:]
            return 0

        @KMT
        def close(pointer):
            self.calls.append(("CloseAdapter", u32.from_address(pointer).value))
            return 0

        @KMT
        def escape(pointer):
            e = NV.GPU._Escape.from_address(pointer)
            self.calls.append(("Escape", e.hAdapter, e.hDevice, e.Type, e.Flags, e.hContext,
                               e.PrivateDriverDataSize, ctypes.string_at(e.pPrivateDriverData,
                                                                         e.PrivateDriverDataSize)))
            words = (u32 * (e.PrivateDriverDataSize // 4)).from_address(e.pPrivateDriverData)
            for index, value in self.escape_writes.items():
                words[index] = value
            return self.escape_status

        def unexpected(name):
            @KMT
            def callback(pointer):
                self.calls.append(("Unexpected", name))
                return -1
            return callback

        table = {ENUM2: enum2, QUERY: query, CLOSE: close, ESCAPE: escape}
        for name in (ENUM, FROM_NAME, FROM_HDC):  # reported by the capability, never called here
            table[name] = unexpected(name)
        return table

    def gdi_table(self):
        if not self.with_gdi:
            return None
        return {name: fn for name, fn in self._callbacks.items() if name not in self.missing}

    def reset(self):
        self.calls = []
        self.packet = (NV.u32 * 21)(*(0xA5A50000 | k for k in range(21)))

    def state(self):
        return (list(self.packet),)

    def python_gpu(self):
        gpu = super().python_gpu()
        gpu.nvapi.selected = {"slot": slot_of(self.bus, self.device)}
        gpu.nvml.selected = {"slot": slot_of(self.bus, self.device)}
        return gpu

    def native_gpu(self):
        install(self.nvml if self.with_nvml else None, self.nvapi if self.with_nvapi else None, self.gdi_table())
        return native.Gpu()

    def python_windll(self):
        table = self.gdi_table()
        if table is None:
            return patch.object(NV.ctypes, "WinDLL", create=True, side_effect=OSError("gdi32.dll not found"))
        return patch.object(NV.ctypes, "WinDLL", create=True, return_value=SimpleNamespace(**table))

    def escape(self, fields=TRANSPORT_FIELDS):
        """The action for ParityCase.both: one _legacy_clk_escape on the rig's packet."""
        def action(gpu):
            if isinstance(gpu, NV.GPU):
                with self.python_windll() as windll:
                    result = gpu._legacy_clk_escape(self.packet, fields)
                self.windll = windll
                return result
            result = gpu._legacy_clk_escape(self.packet, fields)
            self.native_error = gpu.last_escape_error
            return result
        return action


# ---- from tests/test_native_current_limits.py ----------------------------------------

CL_FIELDS = {"hAdapter": 3, "hDevice": 0, "Type": 0, "Flags": 8, "hContext": 0}  # the fixture's transport fields
MATCHED_ADAPTER = 111  # TransportRig's adapter at the card's PCI location

class Driver:
    """The fixture's escape behind a log. fail: NTSTATUS returned (packet untouched) for the
    escapes whose 0-based index is in fail_at; refuse(packet) -> an RM code written to word 16
    instead of answering, or None to answer."""

    def __init__(self, inner):
        self.inner = inner
        self.log = []
        self.fail_at, self.fail = set(), -1
        self.refuse = None

    def __call__(self, packet, fields):
        index = len(self.log)
        entry = [ctypes.sizeof(packet), tuple(packet[:17]), tuple(packet[17:]), dict(fields)]
        self.log.append(entry)
        if index in self.fail_at:
            entry += [self.fail, packet[16]]
            return self.fail
        rm = self.refuse(packet) if self.refuse else None
        if rm is not None:
            packet[16] = rm
            status = 0
        else:
            status = self.inner(packet, fields)
        entry += [status, packet[16]]
        return status


class CLRig(TransportRig):
    """TransportRig whose fake D3DKMTEscape is a Driver: the core's escapes reach the fixture."""

    def __init__(self, arch, driver):
        self.driver = driver
        super().__init__()
        self.errors = []
        if arch is None:  # NVML cannot report an architecture
            self.nvml.exports["nvmlDeviceGetArchitecture"] = u32_callback(0, status=3)
        else:
            self.nvml.exports["nvmlDeviceGetArchitecture"] = u32_callback(arch)

    def _fake_gdi(self):
        table = super()._fake_gdi()

        @KMT
        def escape(pointer):
            try:
                e = NV.GPU._Escape.from_address(pointer)
                words = (u32 * (e.PrivateDriverDataSize // 4)).from_address(e.pPrivateDriverData)
                packet = (u32 * len(words))(*words)
                fields = {name: getattr(e, name) for name in ("hAdapter", "hDevice", "Type", "Flags", "hContext")}
                # The real _legacy_clk_escape swaps in its own PCI-matched adapter handle (111, the
                # card on bus 1; test_native_transport compares that); the twin's escape is mocked
                # below that point and sees the transport's. Map exactly that swap back.
                if fields["hAdapter"] == MATCHED_ADAPTER:
                    fields["hAdapter"] = CL_FIELDS["hAdapter"]
                status = self.driver(packet, fields)
                words[:] = packet[:]
                return status
            except BaseException as exc:  # the fixture's own asserts: surfaced through rig.errors
                self.errors.append(repr(exc))
                return -1
        table[ESCAPE] = escape
        return table


# ---- from tests/test_native_vflock.py ----------------------------------------

BOOST_LOCK, VF_LOCK_SET = 0xE440B867, 0x39442CFB
VOLT_CTRL_GET, VOLT_CTRL_SET = 0x9DF23CA1, 0xB9306D9B


def entry(domain, mode=0, volt=0):
    return {"domain": domain, "unk1": 0x11000000 | domain, "lockMode": mode, "unk2": 0x22000000 | domain,
            "volt_uV": volt, "unk3": 0x33000000 | domain}


class LockRig(Rig):
    """Rig plus a stateful 780-byte lock table and voltage-control block.

    table:   the entries the getter reports (count = len(table) unless count is set)
    set_status: the setter's return code; reassert: the setter returns 0 but changes nothing
    fail_get_after_set: the getter stops answering after the first accepted write (until reset)
    """

    def __init__(self, *, arch=7, with_setter=True, set_status=0, reassert=False, fail_get_after_set=False,
                 volt_set_status=0, with_volt_set=True, volt_get_status=0, **kw):
        super().__init__(**kw)
        self.set_status, self.reassert = set_status, reassert
        self.fail_get_after_set_once = fail_get_after_set  # re-armed by reset(), consumed by one accepted write
        self.volt_set_status, self.volt_get_status = volt_set_status, volt_get_status
        self.reset()
        e, i, n = self.nvml.exports, self.nvapi.interfaces, self.names
        e["nvmlDeviceGetArchitecture"] = u32_callback(arch)

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def boost_lock(handle, pointer):
            self.calls.append(("BoostLock",))
            if self.get_status:
                return self.get_status
            cl = NV._ClockLock.from_address(pointer)
            if cl.version != NV.NvAPI.ver(NV._ClockLock, 2):
                return -9
            cl.flags = self.flags
            cl.count = self.count if self.count is not None else len(self.table)
            for k, row in enumerate(self.table):
                for field, value in row.items():
                    setattr(cl.locks[k], field, value)
            return 0
        n["BoostLock"] = i[BOOST_LOCK] = boost_lock

        if with_setter:
            @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
            def vf_lock_set(handle, pointer):
                cl = NV._ClockLock.from_address(pointer)
                rows = [{f: getattr(cl.locks[k], f) for f, _ in NV._LockEntry._fields_} for k in range(min(cl.count, 32))]
                self.calls.append(("VfLockSet", cl.version, cl.flags, cl.count, rows))
                if self.set_status:
                    return self.set_status
                if not self.reassert:
                    self.table = copy.deepcopy(rows)
                if self.fail_get_after_set:
                    self.get_status = -1
                    self.fail_get_after_set = False
                return 0
            n["VfLockSet"] = i[VF_LOCK_SET] = vf_lock_set

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def volt_get(handle, pointer):
            if self.volt_get_status:
                return self.volt_get_status
            vc = NV._VoltBoost.from_address(pointer)
            vc.percent = self.vboost["percent"]
            for k, word in enumerate(self.vboost["unknown"]):
                vc.unknown[k] = word
            return 0
        n["VoltCtrlGet"] = i[VOLT_CTRL_GET] = volt_get

        if with_volt_set:
            @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
            def volt_set(handle, pointer):
                vc = NV._VoltBoost.from_address(pointer)
                self.calls.append(("VoltCtrlSet", vc.version, vc.percent, tuple(vc.unknown)))
                if self.volt_set_status:
                    return self.volt_set_status
                self.vboost = {"percent": vc.percent, "unknown": list(vc.unknown)}
                return 0
            n["VoltCtrlSet"] = i[VOLT_CTRL_SET] = volt_set

    def reset(self):
        self.calls = []
        self.get_status = 0
        self.fail_get_after_set = self.fail_get_after_set_once
        self.flags = 0xF00D
        self.count = None
        self.table = [entry(d) for d in range(7)]
        self.vboost = {"percent": 25, "unknown": [0x5A000000 | k for k in range(8)]}

    def state(self):
        return copy.deepcopy((self.table, self.vboost))

    def python_gpu(self):
        gpu = super().python_gpu()
        for name in ("VfLockSet", "VoltCtrlSet"):
            setattr(gpu.nvapi, name, self.names.get(name))
        gpu.nvapi.VoltRailsCtlGet = None  # the Turing legacy capture has no transport here, on either side
        return gpu


# ---- from tests/test_native_vfcurve.py ----------------------------------------

VFP_CURVE, BOOST_GET, BOOST_SET = 0x21537AD4, 0x23F1B133, 0x0733E009


def mask_bits(masks):
    return sum(bin(m).count("1") for m in masks)


def turing_rows(n=128):
    return [(400000 + 6250 * i, 300000 + 15000 * i) for i in range(n)]


def pascal_rows():
    gpu = [(412500 + 10000 * i, 2 * (139000 + 12657 * i)) for i in range(80)]
    memory = [(550000, 405000), (550000, 810000), (550000, 5001000), (550000, 7001000)]
    return gpu + memory


class CurveRig(Rig):
    """Rig plus a V/F table of `rows` (volt_uV, freq_kHz) the driver answers for
    up to len(rows) mask bits (one more bit fails with status -1), and a delta
    table the setter changes."""

    def __init__(self, *, arch=6, rows=None, with_curve=True, with_get=True, with_set=True, get_status=0,
                 set_status=0, curve_status=0, **kw):
        super().__init__(**kw)
        self.rows_config = list(rows if rows is not None else turing_rows())
        self.get_status, self.set_status, self.curve_status = get_status, set_status, curve_status
        self.reset()
        e, i, n = self.nvml.exports, self.nvapi.interfaces, self.names
        e["nvmlDeviceGetArchitecture"] = u32_callback(arch)

        if with_curve:
            @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
            def vfp_curve(handle, pointer):
                cv = NV._VfpCurve.from_address(pointer)
                bits = mask_bits(cv.masks)
                self.calls.append(("VfpCurve", bits))
                if cv.version != NV.NvAPI.ver(NV._VfpCurve, 1):
                    return -9
                if self.curve_status:
                    return self.curve_status
                if bits > len(self.rows):
                    return -1
                for k in range(bits):
                    cv.entries[k].volt_uV, cv.entries[k].freq_kHz = self.rows[k]
                return 0
            n["VfpCurve"] = i[VFP_CURVE] = vfp_curve

        if with_get:
            @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
            def boost_get(handle, pointer):
                bt = NV._BoostTable.from_address(pointer)
                bits = mask_bits(bt.masks)
                self.calls.append(("BoostTableGet", bits))
                if self.get_status:
                    return self.get_status
                for k in range(bits):
                    bt.rows[k].w[5] = self.deltas[k]
                return 0
            n["BoostTableGet"] = i[BOOST_GET] = boost_get

        if with_set:
            @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
            def boost_set(handle, pointer):
                bt = NV._BoostTable.from_address(pointer)
                bits = mask_bits(bt.masks)
                written = tuple(bt.rows[k].w[5] for k in range(bits))
                self.calls.append(("BoostTableSet", bt.version, bits, written))
                if self.set_status:
                    return self.set_status
                self.deltas[:bits] = written
                return 0
            n["BoostTableSet"] = i[BOOST_SET] = boost_set

    def reset(self):
        self.calls = []
        self.rows = list(self.rows_config)
        self.deltas = [0] * 128

    def state(self):
        return copy.deepcopy((self.rows, self.deltas))

    def python_gpu(self):
        gpu = super().python_gpu()
        for name in ("VfpCurve", "BoostTableGet", "BoostTableSet"):
            setattr(gpu.nvapi, name, self.names.get(name))
        gpu.static.update(gfx_max=2100, mem_clocks=[405, 810, 5001, 7001])  # what _read_static reads here
        return gpu


# ---- from tests/test_native_fan.py ----------------------------------------

# nvmlErrorString returns a pointer into the library: the fake hands out addresses of
# buffers that live as long as the module (a bytes return would leak per call).
ERROR_TEXT = {status: ctypes.create_string_buffer(text) for status, text in (
    (0, b"Success"), (2, b"Invalid Argument"), (3, b"Not Supported"), (4, b"Insufficient Permissions"),
    (-1, b"Unknown Error"))}


class FanRig(Rig):
    """Rig plus the fan getters/setters, with state the writes change.

    nvml_fans: NVML fan index -> {policy, level} (policy getters answer from it)
    classic:   the single classic cooler {level, policy, default}
    client:    ClientFanCoolers id -> {level, mode}
    settle_after: a write becomes visible only after that many reads
    """

    def __init__(self, *, nvml_setters=True, nvml_policy=True, error_string=True, set_status=0,
                 settle_after=0, **kw):
        super().__init__(**kw)
        self.set_status = set_status
        self.settle_after = settle_after
        self.reset()
        e, i, n = self.nvml.exports, self.nvapi.interfaces, self.names

        def tick():
            for pending in list(self.pending):
                pending[0] -= 1
                if pending[0] <= 0:
                    pending[1]()
                    self.pending.remove(pending)

        def write(apply):
            if self.settle_after:
                self.pending.append([self.settle_after, apply])
            else:
                apply()

        if nvml_policy:
            @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p)
            def policy(device, fan, pointer):
                tick()
                if fan not in self.nvml_fans:
                    return 3
                write_u32(pointer, self.nvml_fans[fan]["policy"])
                return 0
            e["nvmlDeviceGetFanControlPolicy_v2"] = policy

            @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p)
            def target(device, fan, pointer):
                tick()
                if fan not in self.nvml_fans:
                    return 3
                write_u32(pointer, self.nvml_fans[fan]["level"])
                return 0
            e["nvmlDeviceGetTargetFanSpeed"] = target

        if nvml_setters:
            @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32)
            def set_speed(device, fan, speed):
                self.calls.append(("nvmlDeviceSetFanSpeed_v2", fan, speed))
                if self.set_status:
                    return self.set_status
                write(lambda: self.nvml_fans[fan].update(policy=1, level=speed))
                return 0
            e["nvmlDeviceSetFanSpeed_v2"] = set_speed

            @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
            def set_default(device, fan):
                self.calls.append(("nvmlDeviceSetDefaultFanSpeed_v2", fan))
                if self.set_status:
                    return self.set_status
                write(lambda: self.nvml_fans[fan].update(policy=0))
                return 0
            e["nvmlDeviceSetDefaultFanSpeed_v2"] = set_default

        if error_string:
            @CDECL(ctypes.c_char_p, ctypes.c_int)
            def error_text(status):
                return ctypes.addressof(ERROR_TEXT.get(status, ERROR_TEXT[-1]))
            e["nvmlErrorString"] = error_text

        # NVAPI coolers answering from state (the Rig's fixed ones are replaced).
        if self.cooler == "classic":
            def cooler(cs, index):
                tick()
                if index != 7:
                    return 1
                cs.count = 1
                row = cs.entries[0]
                row.current_min, row.current_max = 30, 100
                row.current_level = self.classic["level"]
                row.current_policy, row.default_policy = self.classic["policy"], self.classic["default"]
                return 0
            n["CoolerSettings"] = i[0xDA141340] = struct_callback(NV._CoolerSettings, cooler,
                                                                  extra_args=(ctypes.c_uint32,))

            @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32)
            def levels_set(handle, index, pointer, count):
                buf = NV._CoolerLevels.from_address(pointer)
                self.calls.append(("CoolerLevelsSet", index, buf.version, count,
                                   buf.entries[0].level, buf.entries[0].policy))
                if self.set_status:
                    return self.set_status
                level = buf.entries[0].level
                write(lambda: self.classic.update(level=level, policy=1))  # engages manual regardless
                return 0
            n["CoolerLevelsSet"] = i[0x891FA0AE] = levels_set

            @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32)
            def restore(handle, pointer, count):
                indexes = tuple(ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32))[:count])
                self.calls.append(("CoolerRestore", indexes))
                if self.set_status:
                    return self.set_status
                write(lambda: self.classic.update(policy=self.classic["default"]))
                return 0
            n["CoolerRestore"] = i[0x8F6ED0FB] = restore
        elif self.cooler == "client":
            def control(ctl):
                tick()
                ctl.count = len(self.client)
                for k, (fid, fan) in enumerate(self.client.items()):
                    ctl.entries[k].id, ctl.entries[k].level, ctl.entries[k].mode = fid, fan["level"], fan["mode"]
                    ctl.entries[k].reserved[0] = 0x5A5A0000 | fid  # an opaque field the setter must keep
                return 0
            n["FanCoolersControl"] = i[0x814B209F] = struct_callback(NV._FanCoolersControl, control)

            def status(st):
                st.count = len(self.client)
                for k, (fid, fan) in enumerate(reversed(list(self.client.items()))):
                    row = st.entries[k]
                    row.id, row.rpm, row.min, row.max, row.level = fid, 1000 + 100 * fid, 30, 100, fan["level"]
                return 0
            n["FanCoolersStatus"] = i[0x35AED5E8] = struct_callback(NV._FanCoolersStatus, status)

            @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
            def set_control(handle, pointer):
                buf = NV._FanCoolersControl.from_address(pointer)
                rows = tuple((row.id, row.level, row.mode, row.reserved[0]) for row in buf.entries[:buf.count])
                self.calls.append(("FanCoolersSetControl", buf.version, buf.count, rows))
                if self.set_status:
                    return self.set_status

                def apply():
                    for fid, level, mode, _ in rows:
                        self.client[fid].update(level=level, mode=mode)
                write(apply)
                return 0
            n["FanCoolersSetControl"] = i[0xA58971A5] = set_control

    def reset(self):
        self.calls = []
        self.pending = []
        self.nvml_fans = {0: {"policy": 0, "level": 41}, 1: {"policy": 0, "level": 42}}
        self.classic = {"level": 44, "policy": 16, "default": 16}
        self.client = {5: {"level": 40, "mode": 0}, 7: {"level": 55, "mode": 0}}

    def state(self):
        return copy.deepcopy((self.nvml_fans, self.classic, self.client))

    def python_gpu(self):
        gpu = super().python_gpu()
        for name in ("CoolerLevelsSet", "CoolerRestore", "FanCoolersSetControl"):
            setattr(gpu.nvapi, name, self.names.get(name))
        # GPU.static as _read_static fills it from these fakes (test_capabilities_match_read_static_keys)
        gpu.static.update(fan_min=25, fan_max=100)
        return gpu


# ---- from tests/test_native_clocks.py ----------------------------------------

PSTATES20_GET, PSTATES20_SET = 0x6FF81213, 0x0F4DAE6B


def gp102_clocks():
    return [round(139 + 12.657 * k) for k in range(141)]


class ClockRig(CurveRig):
    """CurveRig plus NVML clock offsets, the locked-clock pair and a state-backed P-state table.

    offsets:     NVML ctype -> {off, mn, mx} (GetClockOffsets answers from it, SetClockOffsets changes off)
    p0:          NVAPI domain -> {value, min, max, flags} for the single P0 entry
    grid:        the graphics clocks enumerated at the top memory clock
    log_grid:    opt-in (default off, so every existing call log is unchanged): each
                 nvmlDeviceGetSupportedGraphicsClocks call appends ("GfxClocks", mem, filled),
                 filled False for the count query and True for the call given a buffer
    """

    def __init__(self, *, nvml_offsets=True, nvml_set=True, nvml_set_status=0, nvml_apply=True, pstates=True,
                 pstates_set=True, pstates_set_status=0, grid=None, with_lock=True, lock_status=0,
                 reset_status=0, mem_div=4, log_grid=False, **kw):
        self.log_grid = log_grid
        self.grid_config = list(grid if grid is not None else [2100 - 15 * k for k in range(100)])
        self.nvml_set_status, self.nvml_apply = nvml_set_status, nvml_apply
        self.pstates_set_status, self.lock_status, self.reset_status = pstates_set_status, lock_status, reset_status
        self.mem_div = mem_div
        super().__init__(**kw)  # calls reset()
        e, i, n = self.nvml.exports, self.nvapi.interfaces, self.names
        if mem_div is None:
            n["RamType"] = i[0x57F7CAAC] = u32_callback(99)  # an unmapped memory type: no true-MHz scale
        e["nvmlErrorString"] = CDECL(ctypes.c_char_p, ctypes.c_int)(lambda st: ctypes.addressof(ERROR_TEXT.get(st, ERROR_TEXT[-1])))

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p)
        def gfxclocks(device, mem, count, values):
            if self.log_grid:
                self.calls.append(("GfxClocks", mem, bool(values)))
            wanted = self.grid if mem == 7001 else [300]
            k = ctypes.cast(count, ctypes.POINTER(ctypes.c_uint32))
            if not values or k[0] < len(wanted):
                k[0] = len(wanted)
                return 7
            k[0] = len(wanted)
            ctypes.memmove(values, (ctypes.c_uint32 * len(wanted))(*wanted), 4 * len(wanted))
            return 0
        e["nvmlDeviceGetSupportedGraphicsClocks"] = gfxclocks

        if nvml_offsets:
            @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
            def get_offsets(device, pointer):
                co = NV._ClockOffset.from_address(pointer)
                self.calls.append(("GetClockOffsets", co.type, co.pstate))
                state = self.offsets.get(co.type)
                if state is None:
                    return 3
                co.off, co.mn, co.mx = state["off"], state["mn"], state["mx"]
                return 0
            e["nvmlDeviceGetClockOffsets"] = get_offsets
        if nvml_set:
            @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
            def set_offsets(device, pointer):
                co = NV._ClockOffset.from_address(pointer)
                self.calls.append(("SetClockOffsets", co.version, co.type, co.pstate, co.off))
                if self.nvml_set_status:
                    return self.nvml_set_status
                if self.nvml_apply and co.type in self.offsets:
                    self.offsets[co.type]["off"] = co.off
                return 0
            e["nvmlDeviceSetClockOffsets"] = set_offsets
        if with_lock:
            @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32)
            def lock(device, lo, hi):
                self.calls.append(("SetGpuLockedClocks", lo, hi))
                return self.lock_status
            e["nvmlDeviceSetGpuLockedClocks"] = lock

            @CDECL(ctypes.c_int, ctypes.c_void_p)
            def unlock(device):
                self.calls.append(("ResetGpuLockedClocks",))
                return self.reset_status
            e["nvmlDeviceResetGpuLockedClocks"] = unlock

        if pstates:
            def table(info):
                if info.version != NV.NvAPI.ver(NV._Pstates20V2, 3):
                    return -9
                self.calls.append(("Pstates20Get",))
                info.num_pstates, info.num_clocks, info.num_voltages = 2, 2, 1
                p0 = info.pstates[0]
                p0.pstate = 0
                for k, (domain, kind) in enumerate(((0, 1), (4, 0))):
                    row = self.p0[domain]
                    clock = p0.clocks[k]
                    clock.domain, clock.kind, clock.flags = domain, kind, row["flags"]
                    clock.delta.value, clock.delta.minimum, clock.delta.maximum = row["value"], row["min"], row["max"]
                p0.clocks[0].data[0], p0.clocks[0].data[1] = 300000, 2100000
                p0.clocks[1].data[0] = 7000500
                info.pstates[1].pstate = 8
                return 0
            n["Pstates20Get"] = i[PSTATES20_GET] = struct_callback(NV._Pstates20V2, table)
            if pstates_set:
                @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
                def pstates_set_fn(handle, pointer):
                    req = NV._Pstates20V2.from_address(pointer)
                    clock = req.pstates[0].clocks[0]
                    self.calls.append(("Pstates20Set", req.version, req.num_pstates, req.num_clocks,
                                       req.pstates[0].pstate, clock.domain, clock.delta.value))
                    if self.pstates_set_status:
                        return self.pstates_set_status
                    self.p0[clock.domain]["value"] = clock.delta.value
                    return 0
                n["Pstates20Set"] = i[PSTATES20_SET] = pstates_set_fn
        else:
            n.pop("Pstates20Get", None)
            i.pop(PSTATES20_GET, None)

    def reset(self):
        super().reset()
        self.grid = list(self.grid_config)
        self.offsets = {0: {"off": 0, "mn": -1000, "mx": 1000}, 2: {"off": 0, "mn": -2000, "mx": 6000}}
        self.p0 = {0: {"value": 0, "min": -1000000, "max": 1000000, "flags": 1},
                   4: {"value": 0, "min": -500500, "max": 1500500, "flags": 1}}

    def state(self):
        return copy.deepcopy((self.rows, self.deltas, self.offsets, self.p0))

    def python_gpu(self):
        gpu = super().python_gpu()
        gpu.nvapi.Pstates20Set = self.names.get("Pstates20Set")
        gpu.nvapi.Pstates20Get = self.names.get("Pstates20Get")
        gpu.static.update(mem_div=self.mem_div, gfx_min=615, gfx_max=2100)  # what _read_static reads here
        return gpu


# ---- from tests/test_native_clkdom.py ----------------------------------------

UNKNOWN = object()  # ClkDomRig(arch=UNKNOWN): no architecture export at all

GET_ID, SET_ID, MEASURE_ID = 0xF58938F5, 0xD14B69CF, 0x527FC458
VERSION, CLKDOM_SIZE = 0x000261A4, 0x61A4
MEASURE_VERSION = 0x0001000C
DRIFT_WORD = 3  # a header word the drifting fake bumps on every GET
CLKDOM_TURING, CLKDOM_BLACKWELL = NV.CLKDOM_LAYOUT_TURING, NV.CLKDOM_LAYOUT_BLACKWELL

def word(layout, control, field):
    """Dword index of `field` (a layout byte offset) in control `control`'s entry."""
    return (layout.header + control * layout.stride + field) // 4


def filler(control, k):
    return (0xC0DE0000 | (control << 8) | k) & 0xFFFFFFFF


class Block(bytes):
    """The SET's bytes; a short repr keeps a failing parity diff readable."""

    def __repr__(self):
        return f"Block(len={len(self)}, crc={hash(bytes(self)) & 0xFFFFFFFF:08x})"


def diff_words(a, b):
    wa = (ctypes.c_uint32 * (len(a) // 4)).from_buffer_copy(a)
    wb = (ctypes.c_uint32 * (len(b) // 4)).from_buffer_copy(b)
    return [(i, hex(wa[i]), hex(wb[i])) for i in range(len(wa)) if wa[i] != wb[i]]


class ClkDomRig(Rig):
    """Rig plus a stateful clock-control block.

    accepted:     control indices the fake driver accepts (any other mask bit fails the WHOLE GET with -1)
    get_status:   every GET fails with this status
    fail_after:   GETs fail with fail_status once this many GETs have succeeded (fail_times times, or forever)
    echo_version / echo_mask: what a successful GET echoes, from the echo_from'th successful GET on
    drift:        every GET bumps header word 3, so two reads never match
    reassert:     the SET returns 0 and changes nothing
    preload:      {control: {field: value}} over the default entries
    """

    def __init__(self, *, arch=6, accepted=None, with_get=True, with_set=True, get_status=0, set_status=0,
                 echo_version=VERSION, echo_mask=True, echo_from=0, drift=False, reassert=False,
                 fail_after=None, fail_status=-5, fail_times=None, with_measure=False, measure_values=None,
                 preload=None, **kw):
        super().__init__(**kw)
        self.accepted_config = list(range(10)) if accepted is None else list(accepted)
        self.get_status, self.set_status = get_status, set_status
        self.echo_version, self.echo_mask, self.echo_from = echo_version, echo_mask, echo_from
        self.drift, self.reassert = drift, reassert
        self.fail_after, self.fail_status, self.fail_times = fail_after, fail_status, fail_times
        self.measure_values = dict(measure_values or {})
        self.preload = copy.deepcopy(preload or {})
        # the fake driver's field placement for this generation (Kepler/Maxwell/unknown: the Turing shape)
        self.layout = CLKDOM_BLACKWELL if arch == 10 else CLKDOM_TURING
        e, i, n = self.nvml.exports, self.nvapi.interfaces, self.names
        if arch is UNKNOWN:
            e.pop("nvmlDeviceGetArchitecture", None)
        else:
            e["nvmlDeviceGetArchitecture"] = u32_callback(arch)
        self.reset()

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def get(handle, pointer):
            words = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32))
            version, mask = words[0], words[2]
            self.calls.append(("ClkDomCtlGet", mask))
            self.gets += 1
            if version != VERSION:
                return -9
            if self.get_status:
                return self.get_status
            if self.fail_after is not None and self.successes >= self.fail_after and (
                    self.fail_times is None or self.failures < self.fail_times):
                self.failures += 1
                return self.fail_status
            allowed = 0
            for d in self.accepted:
                allowed |= 1 << d
            if mask == 0 or mask & ~allowed:
                return -1
            self.fill(words, mask, bad_echo=self.successes >= self.echo_from)
            self.successes += 1
            return 0

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def set_(handle, pointer):
            data = Block(ctypes.string_at(pointer, CLKDOM_SIZE))
            self.calls.append(("ClkDomCtlSet", data))
            if self.set_status:
                return self.set_status
            if not self.reassert:
                words = (ctypes.c_int32 * (CLKDOM_SIZE // 4)).from_buffer_copy(data)
                mask = words[2] & 0xFFFFFFFF
                lay = self.layout
                for d in range(32):
                    if mask >> d & 1:
                        self.entries[d] = {
                            "mode": words[word(lay, d, lay.mode)], "freq": words[word(lay, d, lay.freq_khz)],
                            "nvvdd": words[word(lay, d, lay.nvvdd_uv)], "msvdd": words[word(lay, d, lay.msvdd_uv)]}
            return 0

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def measure(handle, pointer):
            words = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32))
            self.calls.append(("ClkMeasureFreq", words[0], words[1]))
            if words[0] != MEASURE_VERSION:
                return -9
            if words[1] not in self.measure_values:
                return -104
            words[2] = self.measure_values[words[1]]
            return 0

        if with_get:
            n["ClkDomCtlGet"] = i[GET_ID] = get
        if with_set:
            n["ClkDomCtlSet"] = i[SET_ID] = set_
        if with_measure:
            n["ClkMeasureFreq"] = i[MEASURE_ID] = measure

    def fill(self, words, mask, *, bad_echo=True):
        lay = self.layout
        signed = ctypes.cast(words, ctypes.POINTER(ctypes.c_int32))
        for d in range(32):
            if not mask >> d & 1:
                continue
            base = (lay.header + d * lay.stride) // 4
            for k in range(lay.stride // 4):
                words[base + k] = filler(d, k)
            entry = self.entries[d]
            signed[word(lay, d, lay.mode)] = entry["mode"]
            signed[word(lay, d, lay.freq_khz)] = entry["freq"]
            signed[word(lay, d, lay.nvvdd_uv)] = entry["nvvdd"]
            signed[word(lay, d, lay.msvdd_uv)] = entry["msvdd"]
        words[0] = self.echo_version if bad_echo else VERSION
        words[2] = mask if (self.echo_mask or not bad_echo) else 0
        if self.drift:
            words[DRIFT_WORD] = self.gets

    def expected_block(self, mask, changes=()):
        """The declared block a GET of `mask` produces from the configured state, with `changes` applied."""
        saved = (self.entries, self.gets)
        self.entries = self.initial_entries()
        try:
            buf = (ctypes.c_uint32 * (CLKDOM_SIZE // 4))()
            buf[0], buf[2] = VERSION, mask
            self.fill(buf, mask)
        finally:
            self.entries, self.gets = saved
        signed = ctypes.cast(buf, ctypes.POINTER(ctypes.c_int32))
        for index, value in dict(changes).items():
            signed[index] = value
        return bytes(buf)

    def initial_entries(self):
        entries = {d: {"mode": 8 if d % 2 == 0 else 9, "freq": 0, "nvvdd": 0, "msvdd": 0} for d in range(32)}
        for d, fields in self.preload.items():
            entries[d].update(fields)
        return entries

    def reset(self):
        self.calls = []
        self.accepted = list(self.accepted_config)
        self.entries = self.initial_entries()
        self.successes = self.failures = self.gets = 0

    def state(self):
        return copy.deepcopy(self.entries)

    def python_gpu(self):
        gpu = super().python_gpu()
        for name in ("ClkDomCtlGet", "ClkDomCtlSet", "ClkMeasureFreq"):
            setattr(gpu.nvapi, name, self.names.get(name))
        return gpu


# ---- from tests/test_native_legacy_p0.py ----------------------------------------

FORCE_PSTATE = 0x025BFB10
VOLT_RAILS_STATUS = 0x465F9BCF
VOLT_STATUS_V1 = NV.NvAPI.ver(NV._VoltStatus, 1)
ALL_CLOCKS, ALL_CLOCKS_PRIV, CURRENT_PSTATE, PSTATES20 = 0xDCB616C3, 0x1BD69F49, 0x927DA4F6, 0x6FF81213
KEPLER, MAXWELL, TURING = 2, 3, 6
GOOD = {"pstate": 0, "core": 705, "mem": 3004, "top": 3004}
P8 = dict(GOOD, pstate=8, mem=324)


def serve_vcore(rig, log, value_uv=1043750, status=0):
    """Serve VoltRailsStatus from the rig: log(handle, version) on each call, then status, and on
    status 0 value_uv (rig.vcore, when the rig carries one, overrides both)."""
    @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
    def volt(handle, pointer):
        vs = NV._VoltStatus.from_address(pointer)
        log(handle, vs.version)
        code, value = getattr(rig, "vcore", (status, value_uv))
        if code:
            return code
        vs.value_uV = value
        return 0
    rig._volt_keep = volt
    rig.names["VoltRailsStatus"] = rig.nvapi.interfaces[VOLT_RAILS_STATUS] = volt

class P0Rig(Rig):
    """Rig with scripted telemetry samples and a ForcePstate that logs into one event list.

    samples: one dict per read (pstate, core, mem, top; None leaves that value out); the last one
             repeats. force: the statuses ForcePstate returns, in order (0 once exhausted).
    """

    def __init__(self, *, arch=KEPLER, with_force=True, nvapi_init=0, pairing=False):
        super().__init__()
        if nvapi_init or pairing:  # the same interfaces behind a failing or mismatched NVAPI
            old = self.nvapi
            self.nvapi = FakeNvapi([dict(TITAN, devid=0x1B02) if pairing else TITAN], init_status=nvapi_init)
            for ident, fn in old.interfaces.items():
                self.nvapi.interfaces.setdefault(ident, fn)
        i, n, e = self.nvapi.interfaces, self.names, self.nvml.exports
        if arch is None:
            e.pop("nvmlDeviceGetArchitecture", None)
        else:
            e["nvmlDeviceGetArchitecture"] = u32_callback(arch)
        i.pop(ALL_CLOCKS_PRIV, None)
        n["AllClocksPriv"] = None

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def pstates(handle, pointer):  # absent table: no Pstates20 fallback for the P0 maxima
            return -9
        n["Pstates20Get"] = i[PSTATES20] = pstates

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def clocks(handle, pointer):
            self.reads += 1
            self.events.append(("read", self.reads))
            cf = NV._ClkFreqs.from_address(pointer)
            if cf.clockType != 0:
                return 1
            s = self.sample()
            for domain, key in ((0, "core"), (4, "mem")):
                if s[key] is not None:
                    cf.domain[domain].present, cf.domain[domain].frequency = 1, s[key] * 1000
            return 0
        n["AllClocks"] = i[ALL_CLOCKS] = clocks

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def pstate(handle, pointer):
            value = self.sample()["pstate"]
            if value is None:
                return 1
            write_u32(pointer, value)
            return 0
        n["CurrentPstate"] = i[CURRENT_PSTATE] = pstate

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p)
        def minmax(device, ctype, pstate_index, low, high):
            value = {0: 2100, 2: self.sample()["top"]}.get(ctype) if pstate_index == 0 else None
            if value is None:
                return 3
            write_u32(low, 0)
            write_u32(high, value)
            return 0
        e["nvmlDeviceGetMinMaxClockOfPState"] = minmax

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32)
        def force(handle, pstate_index, flags):
            self.events.append(("force", handle, pstate_index, flags))
            return self.force.pop(0) if self.force else 0
        self._force_keep = force
        if with_force:
            i[FORCE_PSTATE] = force
        # every VoltRailsStatus call (each telemetry sample's, and read_vcore_mv's) in its own log
        serve_vcore(self, lambda handle, version: self.vcore_calls.append((handle, version)))
        self.reset()

    def reset(self, samples=(GOOD,), force=()):
        self.samples, self.force = [dict(s) for s in samples], list(force)
        self.events, self.reads, self.vcore_calls = [], 0, []

    def sample(self):
        return self.samples[min(max(self.reads, 1), len(self.samples)) - 1]

    def twin(self, opened):
        """An nvbackend.GPU reading this fake, carrying the identity of the native handle."""
        gpu = self.python_gpu()
        gpu.nvapi.ok, gpu.nvapi.gpu = opened.nvapi.ok, opened.nvapi.gpu
        gpu.pairing_error = opened.pairing_error
        binder = SimpleNamespace(_qi=self.nvapi.query)
        gpu.nvapi.ForcePstate = NV.NvAPI._i(binder, FORCE_PSTATE, NV.PTR, NV.u32, NV.u32)  # as NvAPI binds it
        gpu.nvapi.GetArchInfo = None
        gpu._legacy_p0_owned = False  # GPU.__init__
        gpu._vf_lock_recovery = None
        return gpu


# ---- from tests/test_native_rails.py ----------------------------------------

CONTROL_ID, ABS_ID = 0xA3070DB0, 0x5D0634EE
V2, V1 = 0x00020AC8, 0x00010AC8
RAIL_SIZE = 8192

STOCK = [1, 800000, 1068750, 1093750, 1125000, 1068750, 650000]

class RailFake:
    """Both rail getters over one record set.

    control / live: {rail: values}; a rail missing here answers -8 (absent)
    accept:  {kind: versions the getter understands}; any other version answers -9
    script:  {kind: [entry per call]}: None answers normally, an int is returned without answering,
             ("echo", word, value) answers and then overwrites that u32 word
    boost:   the live block's header word 2 (microvolts): one word for every rail, or {rail: word}
    control_type: {rail: the control record's first (signed) word}; 0 for a rail not listed
    empty_absent: the control getter answers 0 without writing a record for a rail missing from
             control (the R470 V1 behaviour of tests/test_volt_rails_legacy.py)
    hook:    hook(fake, kind) runs as each call arrives, before it is answered (state changes
             between the samples of one read)
    """

    def __init__(self, *, control=None, live=None, accept=None, boost=0, control_type=None, empty_absent=False):
        self.control = copy.deepcopy({0: [0, 0, 0, 0]} if control is None else control)
        self.control_type = dict(control_type or {})
        self.live = copy.deepcopy({0: list(STOCK)} if live is None else live)
        self.accept = {"control": {V2}, "live": {V1}}
        self.accept.update(copy.deepcopy(accept or {}))
        self.script = {"control": [], "live": []}
        self.boost = boost
        self.empty_absent = empty_absent
        self.hook = None
        self.calls = []

    def answer(self, kind, handle, pointer):
        words = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32))
        self.calls.append((kind, handle, words[0], words[1], Block(ctypes.string_at(pointer, RAIL_SIZE))))
        if self.hook is not None:
            self.hook(self, kind)
        entry = self.script[kind].pop(0) if self.script[kind] else None
        if isinstance(entry, int):
            return entry
        if words[0] not in self.accept[kind]:
            return -9
        if words[1] not in (1, 2):
            return -8
        rail = 0 if words[1] == 1 else 1
        records = self.control if kind == "control" else self.live
        if rail not in records:
            return 0 if kind == "control" and self.empty_absent else -8
        values = [self.control_type.get(rail, 0)] + records[rail] if kind == "control" else records[rail]
        signed = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_int32))
        base = (0x48 + rail * 0x54) // 4
        for index, value in enumerate(values):
            if kind == "control":
                signed[base + index] = value
            else:
                words[base + index] = value
        if kind == "live":
            words[2] = self.boost.get(rail, 0) if isinstance(self.boost, dict) else self.boost
        if entry is not None:
            _, word, value = entry
            words[word] = value
        return 0


# ---- from tests/test_native_rail_reference.py ----------------------------------------

LIVE_TYPE = {0: 1, 1: 3}
BASES = {"turing": (1068750, 1093750, 1125000, 650000),
         "pascal": (1068750, 1093750, 1200000, 650000),
         "blackwell": (1040000, 1060000, 1200000, 800000)}
ARCH = {"pascal": 4, "turing": 6, "ampere": 7, "blackwell": 10}

def live_record(rail, bases, deltas):
    """FakeRailAPI.sync_live's record for one rail."""
    reliability, alternate, overvoltage, vmin = [b + d for b, d in zip(bases, deltas, strict=False)]
    return [LIVE_TYPE[rail], 800000, reliability, alternate, overvoltage,
            min(reliability, alternate, overvoltage), vmin]

def config(kind="turing", *, rails=None, bases=None, control=None, architecture=None, legacy=False):
    """Rig options for tests/test_volt_rails.py::fake_gpu(kind) (legacy: legacy_gpu(kind))."""
    rails = rails or ((0, 1) if kind == "blackwell" else (0,))
    if control is None:
        control = {r: [0, 0, 0, 0] for r in rails}
        if kind == "blackwell" and 1 in control:
            control[1][0] = -50000
    bases = bases or BASES[kind]
    live = {r: live_record(r, bases, control[r]) for r in rails if r in control}
    card = {"arch": ARCH[kind] if architecture is None else architecture}
    options = {"control": control, "live": live, "card": card}
    if legacy:
        if kind == "pascal":  # R470's V1 control block uses the 1068.75 mV reliability base
            live[0][2] = 1068750
            live[0][5] = min(live[0][2:5])
        options.update(accept={"control": {V1}}, control_type={0: 1}, empty_absent=True)
    return options


# ==== the shared card ============================================================================ #

# nvbackend.NvAPI's attribute name for each QueryInterface id it binds: the driver-call log names an
# NVAPI call by it on both kinds (an id nvbackend does not bind is logged as its hex value).
NVAPI_NAMES = {int(ident, 16): name for name, ident in reversed(re.findall(
    r"self\.(\w+)\s*=\s*self\._i\(\s*(0x[0-9A-Fa-f]+)", Path(NV.__file__).read_text()))}
NVAPI_IDS = {name: ident for ident, name in NVAPI_NAMES.items()}

MODERN_ARCH = 6   # NVML's architecture enum value the modern card answers (Turing-class)
KEPLER_ARCH = KEPLER
SECOND_SLOT = "0000:02:00.0"
NOT_PROVIDED = "not provided by the test resolver"  # the core's text for a library its resolver lacks
NVML_UNKNOWN, NVAPI_ERROR, NVAPI_NO_IMPLEMENTATION = 999, -1, -3

# What each per-knob rig serves on the modern card (tests/test_native_reset_all.py's Card does the
# same composition): (NVML exports, NVAPI interface names).
SERVES = {
    "clock": (("nvmlErrorString", "nvmlDeviceGetSupportedGraphicsClocks", "nvmlDeviceGetClockOffsets",
               "nvmlDeviceSetClockOffsets", "nvmlDeviceSetGpuLockedClocks", "nvmlDeviceResetGpuLockedClocks"),
              ("VfpCurve", "BoostTableGet", "BoostTableSet", "Pstates20Get", "Pstates20Set")),
    "lock": ((), ("BoostLock", "VfLockSet", "VoltCtrlGet", "VoltCtrlSet")),
    "clkdom": ((), ("ClkDomCtlGet", "ClkDomCtlSet")),
    "fan": (("nvmlDeviceGetFanControlPolicy_v2", "nvmlDeviceGetTargetFanSpeed", "nvmlDeviceSetFanSpeed_v2",
             "nvmlDeviceSetDefaultFanSpeed_v2"), ("FanCoolersControl", "FanCoolersStatus", "FanCoolersSetControl")),
}


# NVAPI interfaces whose first argument is not a physical-GPU handle (the library-wide calls): every
# other interface's first argument is checked against the handles the fake issued.
NVAPI_NO_HANDLE = {0x0150E828, 0xE5AC921F, 0x2926AAAD}  # Initialize, EnumPhysicalGPUs, driver version
NVML_NO_HANDLE = ("nvmlDeviceGetCount", "nvmlDeviceGetHandleBy")  # name prefixes taking no device handle
NVML_HANDLE_BASE, NVAPI_HANDLE_BASE = 0x1000, 0x2000  # FakeNvml / FakeNvapi: card i is BASE + i

# What the second card answers differently from the first (second_card=True): inputs, not a board.
SECOND_POWER_MW = 99000          # nvmlDeviceGetPowerUsage on card 1 (card 0: 180500)
SECOND_EDGE_C = 47               # ThermalSettings sensor 0 on card 1 (card 0: 61)

CLOCK_GUARD_READS = 1_000_000    # clock reads without a wait before the guard trips
FAR_FUTURE_S = 1.0e9             # what the tripped guard answers, so a deadline loop ends

# Every resolver / hook / gate object a card handed to the core stays referenced for the life of the
# process. native.set_export_resolver(None) drops its own reference to the trampoline, and a native
# handle opened while the card was installed may still hold an address the core resolved through
# it; if the trampoline or a gate were freed, a later call through that handle would jump into freed
# memory and kill the process instead of raising. A few objects per native card; through their
# closures they also keep that card's FakeDriver alive (a few hundred cards in a full run).
_KEEPALIVE = []
_OPEN_CARDS = []  # the Session currently installed (at most one: nesting is refused)


class _Kernel32Call:
    """One kernel32 export for nvbackend: settable argtypes / restype, a fixed answer, logged."""

    def __init__(self, driver, name, answer):
        self.driver, self.name, self.answer = driver, name, answer
        self.argtypes, self.restype = None, None

    def __call__(self, *args):
        self.driver.log.append(("kernel32", self.name))
        return self.answer


class _Kernel32:
    """What nvbackend's in-memory capture hooks (_capture_current_limit_transport and
    _capture_legacy_clk_transport) load as kernel32. Patching gdi32's code in memory cannot be
    served by a fake, so VirtualProtect refuses (answers 0): the capture gives up before the hook
    is installed, which is nvbackend's own path for a refused page protection. The core's capture
    hooks the escape it resolved itself and needs none of this."""

    def __init__(self, driver):
        for name, answer in (("VirtualProtect", 0), ("GetCurrentThreadId", 1), ("GetCurrentProcess", 1),
                             ("FlushInstructionCache", 1)):
            setattr(self, name, _Kernel32Call(driver, name, answer))


class _TimeProxy:
    """The `time` name of a module on nvbackend's path while a python card is open: sleep and
    monotonic on the card's clock, everything else the real module."""

    def __init__(self, driver, real):
        self._real = real
        self.sleep, self.monotonic = driver.python_sleep, driver.now

    def __getattr__(self, name):
        return getattr(self._real, name)


# The modules the Python backend's path runs that call time.sleep / time.monotonic (found by grep
# over src/druta: only nvbackend; perf_diagnostics calls time.time for report timestamps only, which
# stays real on both kinds). Their `time` name is replaced, never the time module itself.
PYTHON_TIME_MODULES = (NV,)


class FakeDriver:
    """One fake card (two with second_card) serving nvbackend and the core from the same tables.

    architecture "modern": the composite of tests/test_native_reset_all.py's Card on a Turing-class
        card (MODERN_ARCH): telemetry (Rig), the gdi32 escape transport (TransportRig) in front of
        the measured policy table (tests/test_power_policies.PolicyCard through CLRig's Driver),
        NVML's power limit answering from that table, clock offsets / frequency lock / V/F curve
        (ClockRig), the V/F point lock and voltage boost (LockRig), the clock-control block
        (ClkDomRig), the fans (FanRig) and, with rails=True, both rail getters (RailFake over the
        Turing record set of tests/test_native_rail_reference.config).
    architecture "kepler": the Kepler-class legacy P0 card of tests/test_native_legacy_p0.py (P0Rig):
        ForcePstate, scripted P-state / clock samples, no clock-domain control path.
    with_nvapi / with_nvml=False: that library is not loadable on either path.
    second_card: a second card (tests.fake_driver.XP) at SECOND_SLOT. It answers every reader from
        the first card's state except NVML power usage (SECOND_POWER_MW) and the ThermalSettings
        edge sensor (SECOND_EDGE_C), so a read sent to the wrong card shows in the values.
    transient: {export label: status}: the first call of each named export answers that status
        without reaching the card (status None: NVML_UNKNOWN for an NVML export, NVAPI_ERROR else).
    absent: export labels the card does not serve at all (removed before the card opens).
    live_power (modern only): the client-power wire model of tests/test_client_power_runtime.FakeGpu
        answers the client-power commands (dispatched by command and size; the policy table keeps its
        own sizes) behind the understood transport envelope, so the live power request is readable on
        both kinds and set_power_limit_mw reaches its range refusal.

    An export label is the NVML or gdi32 export name, or nvbackend.NvAPI's attribute name for an
    NVAPI interface (NVAPI_NAMES). The driver-call log (self.log) is one list for both kinds:
      (library, label)          a call that reached a gate ("nvml" | "nvapi" | "gdi32");
      (library, label, card)    a call whose device / GPU handle addressed card `card` (1 for the
                                second card) or, as the text "handle 0x...", a handle the fake never
                                issued; a call that addressed the first card carries no third item;
      ("sleep", ms)             a wait;
      ("kernel32", name)        a call of nvbackend's in-memory capture (_Kernel32; Python only).
    self.errors collects what went wrong inside the fake (an exception raised in any fake callback,
    on either kind, through the card's sys.unraisablehook; a tripped clock guard): a case that
    passes must end with it empty.
    """

    def __init__(self, *, architecture="modern", with_nvapi=True, with_nvml=True, second_card=False,
                 transient=None, rails=True, absent=(), live_power=False):
        if architecture not in ("modern", "kepler"):
            raise ValueError("architecture must be 'modern' or 'kepler'")
        if live_power and architecture != "modern":
            raise ValueError("live_power needs the modern card's escape transport")
        self.architecture = architecture
        self.with_nvapi, self.with_nvml = with_nvapi, with_nvml
        self.log, self.errors = [], []
        self.clock, self.clock_reads, self.last_now = 100.0, 0, None
        self._failures = {}
        self._gates = {}
        self._keep = []
        self.opened = False
        self._nvapi_at_open = None
        self.parts, self.rails, self.card, self.escapes, self.live = {}, None, None, None, None
        if architecture == "modern":
            self._build_modern(rails, live_power)
        else:
            self.base = P0Rig(arch=KEPLER_ARCH)
        if second_card:
            self._add_second_card()
        self.nvml_table = self.base.nvml.exports
        self.nvapi_table = self.base.nvapi.interfaces
        self.gdi_table = self.base.gdi_table() if isinstance(self.base, TransportRig) else None
        for label in absent:
            self.remove(label)
        for label, status in (transient or {}).items():
            self.fail_next(label, status)
        self.query = CDECL(ctypes.c_void_p, ctypes.c_uint)(self._query)
        self.kernel32 = _Kernel32(self)

    # ---- composition ---------------------------------------------------------------------------

    def _build_modern(self, rails, live_power):
        from tests.test_power_policies import A612, PolicyCard
        arch = MODERN_ARCH
        self.card = PolicyCard()
        self.header = [0] * 17
        self.header[2], self.header[14], self.header[15] = 54420, A612, 54352
        self.escapes = Driver(self.card.escape)
        self.base = CLRig(arch, self.escapes)
        self.parts = {"clock": ClockRig(arch=arch), "lock": LockRig(arch=arch), "clkdom": ClkDomRig(arch=arch),
                      "fan": FanRig()}
        e, i, n = self.base.nvml.exports, self.base.nvapi.interfaces, self.base.names
        for key, part in self.parts.items():
            exports, names = SERVES[key]
            for name in exports:
                if name in part.nvml.exports:
                    e[name] = part.nvml.exports[name]
                else:
                    e.pop(name, None)
            for name in names:
                fn = part.names.get(name)
                n[name] = fn
                for ident in [k for k, v in part.nvapi.interfaces.items() if v is fn and fn is not None]:
                    i[ident] = fn

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def limit_get(device, pointer):
            ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32))[0] = self.card.request[self.card.board]
            return 0

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
        def limit_set(device, mw):
            self.base.calls.append(("SetPowerManagementLimit", mw))
            self.card.request[self.card.board] = mw
            self.card.derive()  # measured: the coupled policies follow the board limit
            return 0
        self._keep += [limit_get, limit_set]
        e["nvmlDeviceGetPowerManagementLimit"] = limit_get
        e["nvmlDeviceSetPowerManagementLimit"] = limit_set
        e["nvmlDeviceGetArchitecture"] = u32_callback(arch)
        self._keep.append(e["nvmlDeviceGetArchitecture"])
        if rails:
            options = config("turing")
            options.pop("card")
            self.rails = RailFake(**options)

            @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
            def control(handle, pointer):
                return self.rails.answer("control", handle, pointer)

            @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
            def live(handle, pointer):
                return self.rails.answer("live", handle, pointer)
            self._keep += [control, live]
            i[CONTROL_ID], i[ABS_ID] = control, live
        if live_power:
            self._add_live_power()

    def _add_live_power(self):
        """The client-power wire model behind the escape, for its own commands and sizes."""
        from druta import client_power_runtime as cp
        from tests.test_client_power_runtime import FakeGpu
        self.live = FakeGpu(architecture=MODERN_ARCH)
        for index, value in cp.clock._ENVELOPE.items():  # the envelope the controller understands
            self.header[index] = value
        model_fields = {"hAdapter": 12, "hDevice": 0, "Type": 0, "Flags": 1, "hContext": 0}  # FakeGpu's own
        sizes = set(cp.SIZES.items())
        policy = self.escapes.inner

        def escape(packet, fields):
            if (int(packet[14]), int(packet[15])) not in sizes:
                return policy(packet, fields)
            lock = self.live._lock
            previous, lock.held = lock.held, True  # the model asserts its caller holds the GPU lock
            try:
                return self.live._legacy_clk_escape(packet, model_fields)
            finally:
                lock.held = previous
        self.escapes.inner = escape

    def _add_second_card(self):
        """XP at SECOND_SLOT in both libraries; NVAPI is rebuilt with both GPUs (its own identity and
        bus interfaces), keeping every other interface the card serves. Two readers answer card 1
        with its own values (SECOND_POWER_MW, SECOND_EDGE_C)."""
        self.base.nvml.devices.append(dict(XP))
        old = self.base.nvapi
        self.base.nvapi = FakeNvapi([dict(TITAN, bus=self.base.bus, device=self.base.device) if isinstance(
            self.base, TransportRig) else TITAN, XP])
        for ident, fn in old.interfaces.items():
            self.base.nvapi.interfaces.setdefault(ident, fn)
        self._keep.append(old)
        e, i = self.base.nvml.exports, self.base.nvapi.interfaces
        first_power, first_thermal = e["nvmlDeviceGetPowerUsage"], i[0xE3640A56]
        self._keep += [first_power, first_thermal]

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def power(device, pointer):
            if device == NVML_HANDLE_BASE + 1:
                write_u32(pointer, SECOND_POWER_MW)
                return 0
            return first_power(device, pointer)

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p)
        def thermal(handle, sensor_index, pointer):
            status = first_thermal(handle, sensor_index, pointer)
            if status == 0 and handle == NVAPI_HANDLE_BASE + 1:
                NV._ThermalSettings.from_address(pointer).sensor[0].cur = SECOND_EDGE_C
            return status
        self._keep += [power, thermal]
        e["nvmlDeviceGetPowerUsage"], i[0xE3640A56] = power, thermal

    # ---- the gates -----------------------------------------------------------------------------

    @staticmethod
    def label(library, key):
        if library == "nvapi":
            return NVAPI_NAMES.get(key, f"0x{key:08X}")
        return key

    def _key(self, label):
        """(library, table, key) for an export label."""
        if label in self.nvml_table or label in self._gates_by_label("nvml"):
            return "nvml", self.nvml_table, label
        if self.gdi_table is not None and (label in self.gdi_table or label in self._gates_by_label("gdi32")):
            return "gdi32", self.gdi_table, label
        if label in NVAPI_IDS:
            return "nvapi", self.nvapi_table, NVAPI_IDS[label]
        if isinstance(label, int):
            return "nvapi", self.nvapi_table, label
        raise KeyError(f"the fake card serves no export {label!r}")

    def _gates_by_label(self, library):
        return {key for lib, key in self._gates if lib == library}

    @staticmethod
    def _takes_handle(library, key, proto):
        """"nvml" / "nvapi" when the export's first argument is a device / physical-GPU handle."""
        argtypes = getattr(proto, "_argtypes_", ()) or ()
        if not argtypes or argtypes[0] is not ctypes.c_void_p:
            return None
        if library == "nvml":
            return "nvml" if key.startswith("nvmlDevice") and not key.startswith(NVML_NO_HANDLE) else None
        if library == "nvapi":
            return None if key in NVAPI_NO_HANDLE else "nvapi"
        return None

    def card_of(self, kind, handle):
        """The card index a handle the fake issued addresses, else the text naming the handle."""
        base = NVML_HANDLE_BASE if kind == "nvml" else NVAPI_HANDLE_BASE
        count = len(self.base.nvml.devices) if kind == "nvml" else self._nvapi_gpus()
        if isinstance(handle, int) and 0 <= handle - base < count:
            return handle - base
        return "handle None" if handle is None else f"handle {handle:#x}"

    def _nvapi_gpus(self):
        return 2 if len(self.base.nvml.devices) > 1 else 1

    def _gate(self, library, key, table):
        """The stable callback both kinds reach for one export (None while the export is absent)."""
        if table.get(key) is None:
            return None
        gate = self._gates.get((library, key))
        if gate is not None:
            return gate
        proto = type(table[key])
        if getattr(proto, "_restype_", None) not in (ctypes.c_int, ctypes.c_long):
            return table[key]  # nvmlErrorString: a text pointer, not a status; never failed or logged
        label = self.label(library, key)
        handle_kind = self._takes_handle(library, key, proto)

        def dispatch(*args):
            entry = (library, label)
            if handle_kind is not None and args:
                card = self.card_of(handle_kind, args[0])
                if card != 0:
                    entry += (card,)
            self.log.append(entry)
            armed = self._failures.get(label)
            if armed:
                status = armed.pop(0)
                if not armed:
                    del self._failures[label]
                return status
            current = table.get(key)
            if current is None:  # removed after this kind bound it
                return NVML_UNKNOWN if library == "nvml" else NVAPI_NO_IMPLEMENTATION
            # An exception inside `current` (itself a ctypes callback) never reaches this frame:
            # ctypes reports it through sys.unraisablehook (the card records it in self.errors) and
            # answers 0.
            return current(*args)
        gate = proto(dispatch)
        self._gates[(library, key)] = gate
        return gate

    def _address(self, library, key, table):
        gate = self._gate(library, key, table)
        return ctypes.cast(gate, ctypes.c_void_p).value if gate is not None else None

    def _query(self, ident):
        return self._address("nvapi", ident, self.nvapi_table)

    # ---- changing the card mid-session ---------------------------------------------------------

    def freeze(self):
        """The card is open: record what is served, so mid-session changes the two kinds would see
        differently are refused (replace / remove below)."""
        self.opened = True
        self._nvapi_at_open = {key for key, fn in self.nvapi_table.items() if fn is not None}

    def fail_next(self, label, status=None, times=1):
        """The next `times` calls of export `label` answer `status` without reaching the card."""
        library, _, _ = self._key(label)
        if status is None:
            status = NVML_UNKNOWN if library == "nvml" else NVAPI_ERROR
        self._failures.setdefault(label, []).extend([status] * times)

    def replace(self, label, callback):
        """Serve `label` from `callback` (same signature) from the next call on, on both kinds.

        An NVAPI interface that was absent when the card opened cannot be added: nvbackend binds
        every interface once, at construction, while the core looks it up per call, so only the
        native kind would see it and the two kinds would no longer drive the same card. Replacing
        an interface that was present stays allowed (both kinds reach it through its gate)."""
        library, table, key = self._key(label)
        if library == "nvapi" and self.opened and key not in self._nvapi_at_open:
            raise ValueError(f"NVAPI interface {label!r} was absent when the card opened: nvbackend binds "
                             "interfaces at construction and would never see it, the core would")
        self._keep.append(callback)
        table[key] = callback

    def answer_u32(self, label, value, status=0):
        """Replace a status(handle, u32*) reader with one answering `value` (the rules of replace)."""
        self.replace(label, u32_callback(value, status))

    def remove(self, label):
        """Make `label` absent. Before the card opens (the `absent` option) it is absent on both kinds.

        On an open card: an NVML export is absent for every later call on both kinds (both look
        NVML exports up per call through the gate). A gdi32 export is refused (ValueError): the core
        caches gdi32's capability per loader generation and calls the export again without a NULL
        check, since a real DLL's export cannot vanish; the native kind would crash. An NVAPI
        interface removed mid-session stays bound in nvbackend (it binds at construction) and
        answers NVAPI_NO_IMPLEMENTATION there, while the core sees it absent: do not use that when
        comparing the two kinds."""
        library, table, key = self._key(label)
        if library == "gdi32" and self.opened:
            raise ValueError(f"gdi32 export {label!r} cannot be removed from an open card: the core calls a "
                             "gdi32 export it resolved once without a NULL check; pass it in `absent` instead")
        self._keep.append(table.pop(key, None))

    # ---- the clock ---------------------------------------------------------------------------

    def now(self):
        """The card's monotonic clock in seconds: it moves only when a backend waits.

        The guard: CLOCK_GUARD_READS reads with no wait in between is a loop polling the clock
        without ever waiting. It is recorded in self.errors and answered with a time far in the
        future, so a deadline loop on either kind ends (an exception raised here, inside the core's
        clock hook, would never reach the test)."""
        self.clock_reads += 1
        if self.clock_reads > CLOCK_GUARD_READS:
            if self.clock_reads == CLOCK_GUARD_READS + 1:
                self.errors.append(f"fake driver clock: read {CLOCK_GUARD_READS} times without a wait")
            self.last_now = self.clock + FAR_FUTURE_S
            return self.last_now
        self.last_now = self.clock
        return self.clock

    def wait_ms(self, ms):
        self.log.append(("sleep", ms))
        self.clock += ms / 1000.0
        self.clock_reads = 0

    def python_sleep(self, seconds):
        self.wait_ms(round(seconds * 1000))

    # ---- the two loaders ---------------------------------------------------------------------

    def resolve(self, library, name):
        """native.set_export_resolver's callback."""
        if library == "nvml":
            if not self.with_nvml:
                return None
            return 1 if name == "" else self._address("nvml", name, self.nvml_table)
        if library == "nvapi":
            if not self.with_nvapi:
                return None
            if name == "":
                return 1
            return ctypes.cast(self.query, ctypes.c_void_p).value if name == "nvapi_QueryInterface" else None
        if library == "gdi32" and self.gdi_table is not None:
            return 1 if name == "" else self._address("gdi32", name, self.gdi_table)
        return None

    def windll(self, name, *args, **kwargs):
        """The patched ctypes.WinDLL nvbackend loads nvapi64.dll and gdi32.dll through."""
        base = str(name).lower()
        base = base[:-4] if base.endswith(".dll") else base
        if base in ("nvapi64", "nvapi") and self.with_nvapi:
            return SimpleNamespace(nvapi_QueryInterface=self.query)
        if base == "gdi32" and self.gdi_table is not None:
            return SimpleNamespace(**{label: self._gate("gdi32", label, self.gdi_table)
                                      for label in self.gdi_table})
        if base == "kernel32":
            return self.kernel32
        raise OSError(NOT_PROVIDED)

    def load_nvml(self):
        """The patched nvbackend._load_nvml_library: (dll, path), or OSError as when no NVML loads."""
        if not self.with_nvml:
            raise OSError(f"{ctypes.sizeof(ctypes.c_void_p) * 8}-bit process; {NOT_PROVIDED}")
        driver = self

        class Dll:
            def __getattr__(self, name):
                gate = driver._gate("nvml", name, driver.nvml_table)
                if gate is None:
                    raise AttributeError(name)
                return gate
        return Dll(), "<resolver:nvml>"

    # ---- what a test compares ----------------------------------------------------------------

    def mark(self):
        return len(self.log)

    def since(self, mark):
        return list(self.log[mark:])

    def part_logs(self):
        """Each rig's own call log (content of the writes, not just names), by part."""
        out = {key: copy.deepcopy(part.calls) for key, part in self.parts.items()}
        out["base"] = copy.deepcopy(getattr(self.base, "calls", []))
        if self.escapes is not None:
            out["escapes"] = copy.deepcopy(self.escapes.log)
        if self.rails is not None:
            out["rails"] = [call[:4] for call in self.rails.calls]
        if isinstance(self.base, P0Rig):
            out["p0"] = copy.deepcopy(self.base.events)
        if self.live is not None:
            out["live"] = copy.deepcopy(self.live.calls)
        return out

    def clear_part_logs(self):
        for part in self.parts.values():
            part.calls.clear()
        if hasattr(self.base, "calls"):
            self.base.calls.clear()
        if self.escapes is not None:
            self.escapes.log.clear()
        if self.rails is not None:
            self.rails.calls.clear()
        if isinstance(self.base, P0Rig):
            self.base.events.clear()
        if self.live is not None:
            self.live.calls.clear()

    def state(self):
        """The card's state: what every write so far left behind."""
        out = {key: part.state() for key, part in self.parts.items()}
        if self.card is not None:
            out["policies"] = dict(self.card.request)
            out["policy sets"] = list(self.card.sets)
        if self.rails is not None:
            out["rails"] = copy.deepcopy((self.rails.control, self.rails.live))
        if self.live is not None:
            out["live request"] = self.live.requested
        return out


class Session:
    """One backend kind on one FakeDriver: .gpu (the first card), .driver, .kind, open(slot), and
    .natives (weak references to every native.Gpu constructed while the card is open)."""

    def __init__(self, kind, driver):
        if kind not in ("python", "native"):
            raise ValueError("kind must be 'python' or 'native'")
        self.kind, self.driver = kind, driver
        self.gpu = None
        self.natives = []
        self._stack = contextlib.ExitStack()

    def _unraisable(self, previous):
        driver = self.driver

        def hook(unraisable):
            where = getattr(unraisable, "object", None) or getattr(unraisable, "err_msg", None)
            driver.errors.append(f"{type(unraisable.exc_value).__name__}: {unraisable.exc_value}"
                                 f" (in {where!r})")
            previous(unraisable)
        return hook

    def _install(self):
        import sys
        driver, stack = self.driver, self._stack
        previous_hook = sys.unraisablehook
        stack.callback(setattr, sys, "unraisablehook", previous_hook)
        sys.unraisablehook = self._unraisable(previous_hook)
        if self.kind == "native":
            if not native.available():
                raise RuntimeError("the native core is not built: skip native cases before opening a card")
            # The factory refuses a forced native until the front-end surface is complete; for the life
            # of the card the surface gate reports nothing missing.
            previous = gpu_backend.surface_gate
            stack.callback(setattr, gpu_backend, "surface_gate", previous)
            gpu_backend.surface_gate = lambda native_module: ()
            # Every native.Gpu constructed while the card is open, whoever constructs it (the code
            # under test opens its own through gpu_backend.open_gpu): tracked by wrapping the class's
            # __init__, kept as weak references, closed on exit before the resolver goes.
            original_init = native.Gpu.__init__
            natives = self.natives

            def tracking_init(gpu, *args, **kwargs):
                natives.append(weakref.ref(gpu))
                original_init(gpu, *args, **kwargs)
            stack.enter_context(patch.object(native.Gpu, "__init__", tracking_init))
            stack.callback(native.set_export_resolver, None)
            native.set_export_resolver(driver.resolve)
            stack.callback(native.set_sleep_hook, None)
            native.set_sleep_hook(driver.wait_ms)
            stack.callback(native.set_clock_hook, None)
            native.set_clock_hook(driver.now)
            _KEEPALIVE.extend((driver, native._resolver_keepalive, native._sleep_keepalive,
                               native._clock_keepalive))
        else:
            stack.enter_context(patch.object(ctypes, "WinDLL", create=True, side_effect=driver.windll))
            if not hasattr(ctypes, "WINFUNCTYPE"):  # stdcall prototypes are cdecl off Windows
                stack.enter_context(patch.object(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, create=True))
            stack.enter_context(patch.object(NV, "_load_nvml_library", side_effect=driver.load_nvml))
            for module in PYTHON_TIME_MODULES:
                stack.enter_context(patch.object(module, "time", _TimeProxy(driver, module.time)))

    def open(self, slot=None):
        """A backend object of this session's kind for `slot`, through gpu_backend.open_gpu.

        The power transport is seeded on it as the per-feature rigs seed it: nvbackend captures it by
        patching gdi32's D3DKMTEscape in memory, which no fake can serve, so both kinds start from the
        same captured (header, fields) and every later escape goes through the fake gdi32."""
        gpu = gpu_backend.open_gpu(slot, kind=self.kind)
        if self.driver.architecture == "modern" and gpu.nvapi.ok:
            gpu._current_limit_transport = (list(self.driver.header), dict(CL_FIELDS))
        return gpu

    def close(self):
        """Close every native object still alive (each in its own try), restore everything the card
        installed, always clear the backend pin, then raise the first failure."""
        failures = []
        for ref in self.natives:
            gpu = ref()
            if gpu is None:  # dropped: nothing can call through it any more
                continue
            try:
                gpu.close()  # a no-op on one already closed or never opened
            except BaseException as exc:  # noqa: BLE001 - re-raised below, after the restore
                failures.append(exc)
        self.natives.clear()
        try:
            self._stack.close()
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)
        finally:
            gpu_backend.reset_for_tests()
            if self in _OPEN_CARDS:
                _OPEN_CARDS.remove(self)
        if failures:
            raise failures[0]


@contextlib.contextmanager
def card(kind, slot=None, **options):
    """Install one FakeDriver(**options) for `kind` ("python" or "native") and open `slot` on it.

    Yields a Session (.gpu, .driver, .kind, .open(slot)). For its lifetime the card owns the
    process-wide state both backends load through: the core's export resolver and its sleep and
    clock hooks (native), ctypes.WinDLL / nvbackend's NVML loader and the `time` name of the modules
    in PYTHON_TIME_MODULES (python), the surface gate, sys.unraisablehook and the backend pin
    (gpu_backend.open_gpu(kind=kind): a card of the other kind inside it raises BackendMismatch).
    On exit every native object constructed meanwhile is closed, everything is restored (the
    resolver and hooks to the platform defaults, not to whatever was installed before) and
    gpu_backend.reset_for_tests() runs, so the next card can be the other kind. Cards do not nest
    (RuntimeError, before anything is touched), and a card must not be combined in one test with
    install() or a per-feature rig's own resolver."""
    if _OPEN_CARDS:
        raise RuntimeError("a fake card is already open in this process; cards do not nest (the export "
                           "resolver, the hooks and the backend pin are process-wide)")
    session = Session(kind, FakeDriver(**options))
    _OPEN_CARDS.append(session)
    try:
        session._install()
        session.gpu = session.open(slot)
        session.driver.freeze()
        yield session
    finally:
        session.close()
