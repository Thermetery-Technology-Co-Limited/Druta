"""
TitanTune - GPU backend (NVAPI + NVML), read + guarded write.

Built for the Titan RTX (TU102, DEV_1E02) on the ASUS 2080 Ti Strix PCB, but
falls back to GPU index 0 for any NVIDIA card. All struct layouts are lifted
verbatim from the read-only probes verified live on this card (driver 591.44):
NVAPI ids and NVML field numbers were confirmed against the hardware, not guessed.

Design rule: readers never change state. Writers are explicit, each returns
(ok, message), and only the reversible knobs are wired (clock offsets, power
limit, locked clocks, fan). Footgun knobs (force P-state, TCC, CUDA-clocks) are
surfaced as telemetry + documented commands elsewhere, never fired blind.
"""
import ctypes
import sys
import threading

u8, u32, i32 = ctypes.c_uint8, ctypes.c_uint32, ctypes.c_int32
u64, i64 = ctypes.c_uint64, ctypes.c_int64
PTR = ctypes.c_void_p

TITAN_DEVID = 0x1E02


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# --------------------------------------------------------------------------- #
#  NVAPI                                                                       #
# --------------------------------------------------------------------------- #
class NvAPI:
    def __init__(self):
        self.ok = False
        self.gpu = None
        self.err_detail = ""
        try:
            self.dll = ctypes.WinDLL("nvapi64.dll")
        except Exception as e:
            self.err_detail = f"nvapi64.dll not loadable: {e}"
            return
        qi = self.dll.nvapi_QueryInterface
        qi.restype = ctypes.c_void_p
        qi.argtypes = [u32]
        self._qi = qi

        self.Initialize = self._i(0x0150E828)
        self.EnumGPUs = self._i(0xE5AC921F, PTR, ctypes.POINTER(u32))
        self.GetPCIIds = self._i(0x2DDFB66E, PTR, ctypes.POINTER(u32),
                                 ctypes.POINTER(u32), ctypes.POINTER(u32),
                                 ctypes.POINTER(u32))
        self.GetErrMsg = self._i(0x6C2D048C, ctypes.c_int, ctypes.c_char_p)

        # readers
        self.ThermalSettings = self._i(0xE3640A56, PTR, u32, PTR)
        self.ThermalSensors = self._i(0x65FE3AAD, PTR, PTR)
        self.VoltRailsStatus = self._i(0x465F9BCF, PTR, PTR)
        self.PowerTopo = self._i(0x0EDCF624E, PTR, PTR)
        self.PowerPolInfo = self._i(0x34206D86, PTR, PTR)
        self.PowerPolStatus = self._i(0x70916171, PTR, PTR)
        self.PerfDecrease = self._i(0x7F7F4600, PTR, ctypes.POINTER(u32))
        self.DynPstates = self._i(0x60DED2ED, PTR, PTR)
        self.CurrentPstate = self._i(0x927DA4F6, PTR, ctypes.POINTER(u32))
        self.AllClocks = self._i(0xDCB616C3, PTR, PTR)
        self.AllClocksPriv = self._i(0x1BD69F49, PTR, PTR)
        self.BoostLock = self._i(0xE440B867, PTR, PTR)
        # VF curve: evaluated points, per-point delta table (get/set = AB's pair)
        self.VfpCurve = self._i(0x21537AD4, PTR, PTR)
        self.BoostTableGet = self._i(0x23F1B133, PTR, PTR)
        self.BoostTableSet = self._i(0x0733E009, PTR, PTR)
        # over-voltage % ("Core Voltage" slider): get/set share a 40-byte V1
        # struct. Set id 0xB9306D9B verified vs arcnmx + falahati + live exports.
        self.RamType = self._i(0x57F7CAAC, PTR, ctypes.POINTER(u32))
        self.VoltCtrlGet = self._i(0x9DF23CA1, PTR, PTR)
        self.VoltCtrlSet = self._i(0xB9306D9B, PTR, PTR)

        if self.Initialize is None:
            self.err_detail = "NvAPI_Initialize not resolvable"
            return
        st = self.Initialize()
        if st != 0:
            self.err_detail = f"NvAPI_Initialize status {st}"
            return

        handles = (PTR * 64)()
        cnt = u32(0)
        self.EnumGPUs(handles, ctypes.byref(cnt))
        first = None
        for i in range(cnt.value):
            did, sub, rev, ext = u32(0), u32(0), u32(0), u32(0)
            self.GetPCIIds(handles[i], ctypes.byref(did), ctypes.byref(sub),
                           ctypes.byref(rev), ctypes.byref(ext))
            if first is None:
                first = handles[i]
            if (did.value >> 16) == TITAN_DEVID:
                self.gpu = handles[i]
        if self.gpu is None:
            self.gpu = first
        self.ok = self.gpu is not None

    def _i(self, offset, *argtypes):
        p = self._qi(offset)
        if not p:
            return None
        return ctypes.CFUNCTYPE(ctypes.c_int, *argtypes)(p)

    @staticmethod
    def ver(S, v):
        return ctypes.sizeof(S) | (v << 16)


# ---- NVAPI struct layouts (verified) ------------------------------------- #
class _Sensor(ctypes.Structure):
    _fields_ = [("controller", u32), ("dmin", i32), ("dmax", i32),
                ("cur", i32), ("target", u32)]


class _ThermalSettings(ctypes.Structure):
    _fields_ = [("version", u32), ("count", u32), ("sensor", _Sensor * 3)]


class _ThermalSensorsEx(ctypes.Structure):
    _fields_ = [("version", u32), ("mask", u32),
                ("reserved", i32 * 8), ("temps", i32 * 32)]


class _VoltStatus(ctypes.Structure):
    _fields_ = [("version", u32), ("flags", u32), ("rsvd", u32 * 8),
                ("value_uV", u32), ("tail", u32 * 8)]


class _PwrTopoEntry(ctypes.Structure):
    _fields_ = [("domain", u32), ("unk1", u32), ("power_pcm", u32), ("unk2", u32)]


class _PwrTopo(ctypes.Structure):
    _fields_ = [("version", u32), ("count", u32), ("entries", _PwrTopoEntry * 4)]


class _PwrPolInfoEntry(ctypes.Structure):
    _fields_ = [("pstate", u32), ("unk1", u32), ("unk2", u32), ("min_pcm", u32),
                ("unk3", u32), ("unk4", u32), ("def_pcm", u32), ("unk5", u32),
                ("unk6", u32), ("max_pcm", u32), ("unk7", u32)]


class _PwrPolInfo(ctypes.Structure):
    _fields_ = [("version", u32), ("valid", u8), ("count", u8),
                ("pad", u8 * 2), ("entries", _PwrPolInfoEntry * 4)]


class _PwrPolStatusEntry(ctypes.Structure):
    _fields_ = [("pstate", u32), ("unk1", u32), ("target_pcm", u32), ("unk2", u32)]


class _PwrPolStatus(ctypes.Structure):
    _fields_ = [("version", u32), ("count", u32),
                ("entries", _PwrPolStatusEntry * 4)]


class _UtilDomain(ctypes.Structure):
    _fields_ = [("present", u32), ("percentage", u32)]


class _DynPstates(ctypes.Structure):
    _fields_ = [("version", u32), ("flags", u32), ("util", _UtilDomain * 8)]


class _ClkDomain(ctypes.Structure):
    _fields_ = [("present", u32), ("frequency", u32)]


class _ClkFreqs(ctypes.Structure):
    _fields_ = [("version", u32), ("clockType", u32), ("domain", _ClkDomain * 32)]


# Private NvAPI_GPU_GetAllClocks (0x1BD69F49). Community docs call it
# "probably deprecated"; it answers on Turing (status 0) and is the only
# user-mode path to the domains the public getter hides - XBAR in particular.
#
# Layout verified on TU102 over a 192-sample sweep: the 288 dwords are TWO
# arrays over the same 32 domains, an exact partition -
#     A: dwords 0..63,   2 per domain at 2*d,      {freq_kHz, capability flags}
#     B: dwords 64..287, 7 per domain at 64+7*d,   {freq_kHz, srcid, 0,0,0,0,0}
# They are NOT two views of one number. A is the PROGRAMMED target: always
# exactly on the 15 MHz grid, and bit-identical across samples for a fixed
# domain. B is a MEASURED counter: it jitters 1-3 Hz and never lands on the
# grid. Anything quoting one of them has to say WHICH.
#
# HOW FAR APART THEY ACTUALLY RUN, measured on this card under ~99% GPU load,
# sampled >=8 s after the clock last changed (40 samples per locked case,
# 20 free-boosting), GPC:
#     free-boosting at 1950   A 1950.0   B 1949.90          -0.10 MHz
#     locked at 1920          A 1920.0   B 1917.03-1921.37  within 3 MHz
#     locked at 1350          A 1350.0   B 1364.91-1364.94  +14.9, dead steady
#     XBAR and domains 2/5, all three cases                 within 0.14 MHz
# Settled AND loaded they agree to a few MHz. Where they do not, B is HIGHER,
# not lower: at the 1350 lock the card really is running one 15 MHz bin above
# what array A reports (domain 2's own programmed word reads 1365 there too).
#
# The two WIDE cases are real, and neither is a steady state:
#   * for ~1-2 s after any clock change, either sign, hundreds of MHz up to
#     1.7 GHz (+600 locking down from 1950; -1700 locking up from idle). An
#     earlier "A 1920.0 vs B 1886.7" reading came from a sweep that settled
#     0.22 s - that is this transient, not a steady divergence, and it was
#     re-measured to 3 MHz once the clock was given time to arrive.
#   * at IDLE it never settles at all: with no work the GPC clock gates and B
#     measures the average of a mostly-off clock, so at a 1350 lock B wandered
#     470-573 MHz for tens of seconds (delta ~ -840). A wide delta on an idle
#     card is expected and says nothing about the tune.
#
# PRIV_SLOT is in array-A dword numbers (slot = 2 * domain), i.e. the
# PROGRAMMED figure - that is what the tiles have always shown.
_PRIV_CLK_DWORDS = 288
PRIV_SLOT = {"core": 0, "xbar": 2, "mem": 8, "video": 42}
PRIV_A_BASE, PRIV_A_STRIDE = 0, 2
PRIV_B_BASE, PRIV_B_STRIDE = 64, 7
PRIV_N_DOMAINS = 32
# The domains that carry anything in either array on this card. Kept as a
# constant so a monitor's row set is stable across ticks; read_clock_domains()
# also reports any domain OUTSIDE it that turns up non-zero, so a surprise is
# visible rather than filtered away.
PRIV_POPULATED = (0, 1, 2, 3, 4, 5, 6, 20, 21, 22, 31)
PRIV_UNAVAIL = "private NvAPI_GPU_GetAllClocks (0x1BD69F49) did not answer"

# the partition is exact - a wrong stride would read B's srcids as frequencies
assert PRIV_B_BASE == PRIV_N_DOMAINS * PRIV_A_STRIDE
assert PRIV_B_BASE + PRIV_N_DOMAINS * PRIV_B_STRIDE == _PRIV_CLK_DWORDS

# How far each domain's NAME may be trusted. The frequencies are measured
# either way; the grade is about our right to put a word next to them. A wrong
# name on a monitor page is worse than a bare index: it sends someone debugging
# the wrong domain and nothing on screen says they were misled.
PRIV_CONFIRMED = "confirmed"   # identified against known behaviour
PRIV_LIKELY = "likely"         # behaviour confirmed, NAME only by elimination
PRIV_UNNAMED = "unnamed"       # populated, but no name has been earned

PRIV_FREQ, PRIV_PCIE_GEN = "freq", "pcie_gen"

PRIV_DOMAIN_ID = {
    0:  ("GPC", PRIV_CONFIRMED, PRIV_FREQ),
    1:  ("XBAR", PRIV_CONFIRMED, PRIV_FREQ),
    # a third core-rail domain with its own V/F table. The BEHAVIOUR is
    # confirmed; SYSCLK is a guess by elimination, so it may only ever be
    # displayed hedged.
    2:  ("SYSCLK", PRIV_LIKELY, PRIV_FREQ),
    4:  ("MEM", PRIV_CONFIRMED, PRIV_FREQ),
    # a fourth core-rail domain, ceilings hard at 1350 MHz. Same hedge.
    5:  ("LTCCLK", PRIV_LIKELY, PRIV_FREQ),
    21: ("VIDEO", PRIV_CONFIRMED, PRIV_FREQ),
    # NOT a clock. Dword 62 holds the PCIe link generation (1/2/3), tracks the
    # pstate and ceilings at nvmlDeviceGetMaxPcieLinkGeneration. Rendered in
    # kHz it would read as a perfectly believable 0.003 MHz domain.
    31: ("PCIe link gen", PRIV_LIKELY, PRIV_PCIE_GEN),
}
# Domains 3, 6, 20 and 22 are deliberately absent: their values are confirmed
# static here (405 / 1080 / 540 / 108 MHz) but no NAME for them has been
# earned, so they stay numbered.


class _AllClocksPriv(ctypes.Structure):
    _fields_ = [("version", u32), ("w", u32 * _PRIV_CLK_DWORDS)]


class _LockEntry(ctypes.Structure):
    _fields_ = [("domain", u32), ("unk1", u32), ("lockMode", u32),
                ("unk2", u32), ("volt_uV", u32), ("unk3", u32)]


class _ClockLock(ctypes.Structure):
    _fields_ = [("version", u32), ("flags", u32), ("count", u32),
                ("locks", _LockEntry * 32)]


# VF structs: Turing has 103 CONTIGUOUS GPU points (the 80+23 split in older
# community layouts is a Pascal artefact). Total sizes must equal the original
# community structs (7208 / 9248 bytes) because the driver validates version
# = sizeof | ver<<16.
VFP_POINTS = 103
# The GPU's legal core clocks are EXACTLY multiples of 15 MHz (verified live:
# nvmlDeviceGetSupportedGraphicsClocks = 121 entries, 360..2160, step 15).
# The driver evaluates a VF point as floor((base + delta) / 15MHz) * 15MHz and
# stores the delta verbatim. `base` is NOT readable: the reported frequency is
# already floored, so base carries an unknowable remainder in [0,15). Therefore
# never compute a delta from an absolute target - only change a delta by whole
# multiples of VF_STEP_KHZ, which shifts the evaluated clock by exactly that
# much. (A mid-bin delta silently floors: e.g. asking 2150 yields 2145, which
# collides with the point below and re-creates the flat you were removing.)
VF_STEP_KHZ = 15000


class _VfpEntry(ctypes.Structure):
    _fields_ = [("u0", u32), ("freq_kHz", u32), ("volt_uV", u32),
                ("u3", u32), ("u4", u32), ("u5", u32), ("u6", u32)]


class _VfpCurve(ctypes.Structure):
    _fields_ = [("version", u32), ("masks", u32 * 4), ("unk", u32 * 12),
                ("entries", _VfpEntry * VFP_POINTS), ("tail", u32 * 1064)]


class _BoostRow(ctypes.Structure):
    _fields_ = [("w", i32 * 9)]   # w[5] = freqDelta_kHz


class _BoostTable(ctypes.Structure):
    _fields_ = [("version", u32), ("masks", u32 * 4), ("unk", u32 * 12),
                ("rows", _BoostRow * VFP_POINTS), ("tail", u32 * 1368)]


assert ctypes.sizeof(_VfpCurve) == 7208
assert ctypes.sizeof(_BoostTable) == 9248


def below_cap(volt_mv, cap_mv):
    """Single definition of 'at or below the voltage cap' (shared by the planner
    and every readout, so the number in a dialog is the number that was planned)."""
    return volt_mv <= cap_mv + 0.01


def _set_all_point_masks(obj):
    # bits 0..102: reads return empty unless the point-select masks are set
    obj.masks[0] = 0xFFFFFFFF
    obj.masks[1] = 0xFFFFFFFF
    obj.masks[2] = 0xFFFFFFFF
    obj.masks[3] = 0x7F


# --------------------------------------------------------------------------- #
#  NVML                                                                        #
# --------------------------------------------------------------------------- #
class _NvmlValue(ctypes.Union):
    _fields_ = [("d", ctypes.c_double), ("ui", u32), ("ul", ctypes.c_ulong),
                ("ull", u64), ("sll", i64), ("si", i32)]


class _FieldValue(ctypes.Structure):
    _fields_ = [("fieldId", u32), ("scopeId", u32), ("timestamp", i64),
                ("latencyUsec", i64), ("valueType", u32), ("nvmlReturn", u32),
                ("value", _NvmlValue)]


# NVAPI reports GDDR clocks at half the data rate (7001 MHz ~= 14 Gbps), while
# GPU-Z and the vendors quote the TRUE memory clock. The ratio depends on the
# memory technology, and the public NV_GPU_RAM_TYPE enum only documents up to
# GDDR5X (10) - 14 = GDDR6 is verified on this card (7254 / 4 = 1813.5 MHz,
# byte-for-byte what GPU-Z shows). Only positively identified types are scaled;
# an unknown id is displayed raw rather than risking a wrong number.
MEM_TYPES = {
    8:  ("GDDR5", 2),
    10: ("GDDR5X", 4),
    14: ("GDDR6", 4),
}


class _VoltBoost(ctypes.Structure):
    # NV_GPU_CLIENT_VOLT_RAILS_CONTROL_V1 (40 bytes). percent is i32 (signed:
    # negative would undervolt); we only ever write 0..100.
    _fields_ = [("version", u32), ("percent", i32), ("unknown", u32 * 8)]


assert ctypes.sizeof(_VoltBoost) == 40


def _field_val(fv):
    """Read the union member that matches nvmlValueType, not blindly .ull."""
    return {0: fv.value.d, 1: fv.value.ui, 2: fv.value.ul,
            3: fv.value.ull, 4: fv.value.sll, 5: fv.value.si}.get(
        fv.valueType, fv.value.ull)


class _ClockOffset(ctypes.Structure):
    _fields_ = [("version", u32), ("type", u32), ("pstate", u32),
                ("off", i32), ("mn", i32), ("mx", i32)]


class _FanSpeedInfo(ctypes.Structure):
    _fields_ = [("version", u32), ("fan", u32), ("speed", u32)]


PCIE_ERR_FIELDS = {
    173: "correctable", 174: "naks_rx", 175: "receiver", 176: "bad_tlp",
    177: "naks_tx", 178: "bad_dllp", 179: "non_fatal", 180: "fatal",
    181: "unsupp_req", 182: "lcrc", 183: "lane",
}

# NVML clocks-event-reason bits (9 reasons -> supported mask 0x1FF)
EVENT_REASONS = [
    (0x001, "Idle"),
    (0x002, "App clock setting"),
    (0x004, "SW power cap"),
    (0x008, "HW slowdown"),
    (0x010, "Sync boost"),
    (0x020, "SW thermal"),
    (0x040, "HW thermal"),
    (0x080, "HW power brake"),
    (0x100, "Display clock"),
]

PERF_DECREASE_BITS = [
    (0x01, "Thermal"),
    (0x02, "Power"),
    (0x04, "AC/battery"),
    (0x08, "API triggered"),
    (0x10, "Insufficient aux power"),
    # NVAPI's REASON_UNKNOWN. It sits far outside the 0x01..0x10 run, so it is
    # easy to leave off the end of the list - and then the driver can report a
    # decrease while every lamp on the panel stays dark, which reads as "nothing
    # is holding the card back". A named lamp says the driver knows something we
    # cannot decode, which is the honest answer.
    (0x80000000, "Unknown"),
]


class Nvml:
    def __init__(self):
        self.ok = False
        self.dev = None
        self.err_detail = ""
        try:
            self.dll = ctypes.CDLL(r"C:\Windows\System32\nvml.dll")
        except Exception as e:
            self.err_detail = f"nvml.dll not loadable: {e}"
            return
        self.dll.nvmlErrorString.restype = ctypes.c_char_p
        st = self.dll.nvmlInit_v2()
        if st != 0:
            self.err_detail = f"nvmlInit_v2 status {st}"
            return
        dev = PTR()
        st = self.dll.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev))
        if st != 0:
            self.err_detail = f"GetHandleByIndex status {st}"
            return
        self.dev = dev
        self.ok = True

    def has(self, name):
        try:
            getattr(self.dll, name)
            return True
        except AttributeError:
            return False

    @staticmethod
    def ver(S, v):
        return ctypes.sizeof(S) | (v << 24)

    def errstr(self, st):
        try:
            return self.dll.nvmlErrorString(st).decode(errors="replace")
        except Exception:
            return str(st)


# --------------------------------------------------------------------------- #
#  GPU facade                                                                  #
# --------------------------------------------------------------------------- #
def _synchronized(fn):
    """Serialize driver access: the UI thread issues writes while a background
    thread polls telemetry, and NVAPI read-modify-write (the VF table) must not
    interleave with a concurrent read."""
    def wrapper(self, *a, **kw):
        with self._lock:
            return fn(self, *a, **kw)
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


class ResetStep(tuple):
    """One step of reset_all: still the plain (ok, message) pair every caller
    unpacks, plus the name of the knob it moved.

    The name is there because one step - the clock-lock release - has a
    consequence past its log line: the UI's record of what IT has locked may
    only be cleared when that particular step succeeded, and a caller reading a
    flat list of pairs cannot tell which pair that was (nor should it count
    positions, since the tail steps are conditional)."""

    def __new__(cls, name, res):
        step = super().__new__(cls, res)
        step.name = name
        return step


class GPU:
    # names for the reset_all steps a caller has to single out (see ResetStep)
    LOCK_STEP = "clock lock"

    def __init__(self):
        self._lock = threading.RLock()
        self.nvapi = NvAPI()
        self.nvml = Nvml()
        self._pcie_baseline = None
        self.static = self._read_static()

    # ---- helpers ---------------------------------------------------------- #
    def available(self):
        return self.nvapi.ok or self.nvml.ok

    def status_line(self):
        a = "ok" if self.nvapi.ok else f"FAIL ({self.nvapi.err_detail})"
        n = "ok" if self.nvml.ok else f"FAIL ({self.nvml.err_detail})"
        return f"NVAPI: {a}   |   NVML: {n}"

    # ---- static identity -------------------------------------------------- #
    def _read_static(self):
        s = {"name": "GPU", "driver": "?", "vbios": "?", "admin": is_admin()}
        nv = self.nvml
        if nv.ok:
            buf = ctypes.create_string_buffer(96)
            try:
                nv.dll.nvmlDeviceGetName(nv.dev, buf, 96)
                s["name"] = buf.value.decode(errors="replace")
            except Exception:
                pass
            try:
                nv.dll.nvmlSystemGetDriverVersion(buf, 96)
                s["driver"] = buf.value.decode(errors="replace")
            except Exception:
                pass
            try:
                nv.dll.nvmlDeviceGetVbiosVersion(nv.dev, buf, 96)
                s["vbios"] = buf.value.decode(errors="replace")
            except Exception:
                pass
            # power-limit constraints (mW)
            try:
                mn, mx = u32(0), u32(0)
                if nv.dll.nvmlDeviceGetPowerManagementLimitConstraints(
                        nv.dev, ctypes.byref(mn), ctypes.byref(mx)) == 0:
                    s["pl_min_mw"], s["pl_max_mw"] = mn.value, mx.value
                d = u32(0)
                if nv.dll.nvmlDeviceGetPowerManagementDefaultLimit(
                        nv.dev, ctypes.byref(d)) == 0:
                    s["pl_def_mw"] = d.value
            except Exception:
                pass
            # supported clock range (for locked-clock UI bounds)
            try:
                cnt = u32(64)
                arr = (u32 * 64)()
                if nv.dll.nvmlDeviceGetSupportedMemoryClocks(
                        nv.dev, ctypes.byref(cnt), arr) == 0 and cnt.value:
                    memclks = sorted(arr[i] for i in range(cnt.value))
                    s["mem_clocks"] = memclks
                    n = u32(256)
                    ga = (u32 * 256)()
                    if nv.dll.nvmlDeviceGetSupportedGraphicsClocks(
                            nv.dev, memclks[-1], ctypes.byref(n), ga) == 0:
                        g = [ga[i] for i in range(n.value)]
                        if g:
                            s["gfx_min"], s["gfx_max"] = min(g), max(g)
            except Exception:
                pass
            # fan min/max (manual-duty floor)
            try:
                mn, mx = u32(0), u32(0)
                if nv.has("nvmlDeviceGetMinMaxFanSpeed") and \
                        nv.dll.nvmlDeviceGetMinMaxFanSpeed(
                            nv.dev, ctypes.byref(mn), ctypes.byref(mx)) == 0:
                    s["fan_min"], s["fan_max"] = mn.value, mx.value
            except Exception:
                pass
        # memory technology -> true-clock divisor (None = unknown, show raw)
        a = self.nvapi
        s["mem_div"] = None
        s["mem_type"] = "unknown"
        if a.ok and a.RamType:
            v = u32(0)
            if a.RamType(a.gpu, ctypes.byref(v)) == 0:
                s["mem_type_id"] = v.value
                name, div = MEM_TYPES.get(v.value, (f"RAM type {v.value}", None))
                s["mem_type"], s["mem_div"] = name, div
        # clock-offset editable ranges (NVML)
        s["core_off_range"] = self._offset_range(0)
        s["mem_off_range"] = self._offset_range(2)
        return s

    def _offset_range(self, ctype):
        nv = self.nvml
        if not nv.ok or not nv.has("nvmlDeviceGetClockOffsets"):
            return None
        co = _ClockOffset(version=nv.ver(_ClockOffset, 1), type=ctype, pstate=0)
        if nv.dll.nvmlDeviceGetClockOffsets(nv.dev, ctypes.byref(co)) == 0:
            return (co.mn, co.mx, co.off)
        return None

    # ---- live telemetry --------------------------------------------------- #
    def read(self):
        d = {}
        # ONE private-getter call per tick, shared: the tiles take their four
        # slots out of it and the all-domains readout takes all 32 out of the
        # SAME instant. Two round trips would also compare a programmed target
        # against a counter sampled at a different moment, which is precisely
        # the comparison read_clock_domains exists to make honest.
        pc = self._priv_clocks()
        self._read_clocks(d, pc)
        if pc is None:
            d["clk_domains"], d["clk_domains_err"] = None, PRIV_UNAVAIL
        else:
            d["clk_domains"], d["clk_domains_err"] = self.read_clock_domains(pc)
        self._read_temps(d)
        self._read_power(d)
        self._read_fan(d)
        self._read_util(d)
        self._read_throttle(d)
        self._read_pcie(d)
        self._read_misc(d)
        return d

    def _na(self, api):
        return self.nvapi if api == "a" else self.nvml

    def _priv_clocks(self):
        """One raw private-getter payload, or None if it did not answer. Split
        out so the tile path below and read_clock_domains can be fed from a
        single call per tick (see read())."""
        a = self.nvapi
        if not (a.ok and a.AllClocksPriv):
            return None
        pc = _AllClocksPriv(version=a.ver(_AllClocksPriv, 2))
        if a.AllClocksPriv(a.gpu, ctypes.byref(pc)) != 0:
            return None
        return pc

    def read_clock_domains(self, pc=None):
        """(rows, err) - every populated domain of the private getter, BOTH
        arrays, one dict per domain:

            domain      index 0..31
            name        '' when no name has been earned
            grade       PRIV_CONFIRMED / PRIV_LIKELY / PRIV_UNNAMED - how far
                        `name` may be trusted, never how good the reading is
            kind        PRIV_FREQ, or PRIV_PCIE_GEN for domain 31, which is a
                        link generation and not a frequency at all
            prog_khz    array-A dword: the PROGRAMMED target
            meas_khz    array-B dword: the MEASURED counter
            prog_mhz / meas_mhz / delta_mhz
                        the same in MHz, None when the row is not a frequency
            flags       array-A's odd dword, the per-domain capability field
                        (constant across every sample of a given domain)
            srcid       array-B's second dword

        delta is measured MINUS programmed, so a card running slower than it
        was told to reads negative - which is the normal case under load.

        `pc` lets a caller that already read a payload this tick hand it over
        instead of paying for a second round trip."""
        if pc is None:
            pc = self._priv_clocks()
        if pc is None:
            return None, PRIV_UNAVAIL
        rows = []
        for dom in range(PRIV_N_DOMAINS):
            ai = PRIV_A_BASE + PRIV_A_STRIDE * dom
            bi = PRIV_B_BASE + PRIV_B_STRIDE * dom
            prog, flags = pc.w[ai], pc.w[ai + 1]
            meas, srcid = pc.w[bi], pc.w[bi + 1]
            # an unlisted domain is reported only if it actually carries
            # something: silently dropping one would make the panel lie by
            # omission, but listing 21 empty rows would bury the 11 real ones
            if dom not in PRIV_POPULATED and not (prog or meas or flags):
                continue
            name, grade, kind = PRIV_DOMAIN_ID.get(
                dom, ("", PRIV_UNNAMED, PRIV_FREQ))
            row = {"domain": dom, "name": name, "grade": grade, "kind": kind,
                   "prog_khz": prog, "meas_khz": meas,
                   "flags": flags, "srcid": srcid,
                   "prog_mhz": None, "meas_mhz": None, "delta_mhz": None}
            if kind == PRIV_FREQ:
                row["prog_mhz"] = prog / 1000.0
                row["meas_mhz"] = meas / 1000.0
                if prog and meas:
                    row["delta_mhz"] = (meas - prog) / 1000.0
            rows.append(row)
        return rows, None

    def _read_clocks(self, d, pc=None):
        a = self.nvapi
        if a.ok and a.AllClocks:
            cf = _ClkFreqs()
            cf.version = a.ver(_ClkFreqs, 2)
            cf.clockType = 0  # CURRENT
            if a.AllClocks(a.gpu, ctypes.byref(cf)) == 0:
                dom = {0: "core", 4: "mem", 8: "video"}
                for i in range(32):
                    if cf.domain[i].present & 1 and i in dom:
                        d[dom[i]] = cf.domain[i].frequency // 1000
        # XBAR (and a fallback for the domains above) via the private getter.
        # XBAR is not a fixed offset from GPC: it has its own V/F table on the
        # same rail, so it must be read, not derived.
        # These are array-A slots, i.e. the PROGRAMMED target - not what the
        # card is measured to be running. read_clock_domains() reports both.
        if pc is None:
            pc = self._priv_clocks()
        if pc is not None:
            for key, slot in PRIV_SLOT.items():
                v = pc.w[slot] // 1000
                if v and (key == "xbar" or key not in d):
                    d[key] = v
        # applied offsets (NVML) - note: invisible knob vs the VF-point table
        nv = self.nvml
        if nv.ok and nv.has("nvmlDeviceGetClockOffsets"):
            for ctype, key in ((0, "core_off"), (2, "mem_off")):
                co = _ClockOffset(version=nv.ver(_ClockOffset, 1),
                                  type=ctype, pstate=0)
                if nv.dll.nvmlDeviceGetClockOffsets(nv.dev, ctypes.byref(co)) == 0:
                    d[key] = co.off

    def _read_temps(self, d):
        a = self.nvapi
        if not a.ok:
            return
        # edge
        if a.ThermalSettings:
            ts = _ThermalSettings(version=a.ver(_ThermalSettings, 2))
            if a.ThermalSettings(a.gpu, 15, ctypes.byref(ts)) == 0:
                for k in range(min(ts.count, 3)):
                    if ts.sensor[k].target == 1:  # GPU
                        d["temp_edge"] = ts.sensor[k].cur
        # hotspot array
        if a.ThermalSensors:
            best = None
            for nbits in range(1, 33):
                tx = _ThermalSensorsEx(version=a.ver(_ThermalSensorsEx, 2),
                                       mask=(1 << nbits) - 1)
                if a.ThermalSensors(a.gpu, ctypes.byref(tx)) == 0:
                    best = (tx, nbits)
                else:
                    break
            if best:
                tx, nbits = best
                # keep only physically plausible channels: some reserved slots
                # return spurious values, so bound to (0, 150) C before max().
                temps = [tx.temps[k] / 256.0 for k in range(nbits)
                         if 0 < tx.temps[k] < 150 * 256]
                if temps:
                    d["temp_hotspot"] = max(temps)
                    d["temp_sensors"] = [round(t, 1) for t in temps]
        if "temp_hotspot" in d and "temp_edge" in d:
            d["temp_delta"] = d["temp_hotspot"] - d["temp_edge"]

    def _read_power(self, d):
        nv = self.nvml
        if nv.ok:
            v = u32(0)
            if nv.dll.nvmlDeviceGetPowerUsage(nv.dev, ctypes.byref(v)) == 0:
                d["power_w"] = v.value / 1000.0
            if nv.dll.nvmlDeviceGetEnforcedPowerLimit(nv.dev, ctypes.byref(v)) == 0:
                d["pl_now_mw"] = v.value
        a = self.nvapi
        if a.ok and a.PowerTopo:
            pt = _PwrTopo(version=a.ver(_PwrTopo, 1))
            if a.PowerTopo(a.gpu, ctypes.byref(pt)) == 0:
                for k in range(min(pt.count, 4)):
                    e = pt.entries[k]
                    name = {0: "pwr_gpu_pct", 1: "pwr_board_pct"}.get(e.domain)
                    if name:
                        d[name] = e.power_pcm / 1000.0
        if a.ok and a.PowerPolStatus:
            ps = _PwrPolStatus(version=a.ver(_PwrPolStatus, 1))
            if a.PowerPolStatus(a.gpu, ctypes.byref(ps)) == 0 and ps.count:
                d["pl_target_pct"] = ps.entries[0].target_pcm / 1000.0
        if a.ok and a.VoltRailsStatus:
            vs = _VoltStatus(version=a.ver(_VoltStatus, 1))
            if a.VoltRailsStatus(a.gpu, ctypes.byref(vs)) == 0 and vs.value_uV:
                d["vcore_mv"] = vs.value_uV / 1000.0

    def _read_fan(self, d):
        nv = self.nvml
        if not nv.ok:
            return
        nf = u32(0)
        if nv.dll.nvmlDeviceGetNumFans(nv.dev, ctypes.byref(nf)) == 0:
            d["num_fans"] = nf.value
        fans = []
        for f in range(max(nf.value, 1)):
            duty = u32(0)
            rpm = None
            if nv.has("nvmlDeviceGetFanSpeed_v2"):
                if nv.dll.nvmlDeviceGetFanSpeed_v2(
                        nv.dev, f, ctypes.byref(duty)) != 0:
                    duty = u32(0)
            if nv.has("nvmlDeviceGetFanSpeedRPM"):
                fi = _FanSpeedInfo(version=nv.ver(_FanSpeedInfo, 1), fan=f)
                if nv.dll.nvmlDeviceGetFanSpeedRPM(nv.dev, ctypes.byref(fi)) == 0:
                    rpm = fi.speed
            fans.append((duty.value, rpm))
        d["fans"] = fans

    def _read_util(self, d):
        a = self.nvapi
        if a.ok and a.DynPstates:
            dp = _DynPstates(version=a.ver(_DynPstates, 1))
            if a.DynPstates(a.gpu, ctypes.byref(dp)) == 0:
                names = ["gpu", "fb", "vid", "bus"]
                for k in range(4):
                    if dp.util[k].present:
                        d[f"util_{names[k]}"] = dp.util[k].percentage

    def _read_throttle(self, d):
        nv = self.nvml
        if nv.ok and nv.has("nvmlDeviceGetCurrentClocksEventReasons"):
            v = u64(0)
            if nv.dll.nvmlDeviceGetCurrentClocksEventReasons(
                    nv.dev, ctypes.byref(v)) == 0:
                d["event_mask"] = v.value
        a = self.nvapi
        if a.ok and a.PerfDecrease:
            pd = u32(0)
            if a.PerfDecrease(a.gpu, ctypes.byref(pd)) == 0:
                d["perf_decrease"] = pd.value

    def _read_pcie(self, d):
        nv = self.nvml
        if not nv.ok:
            return
        for fn, key in (("nvmlDeviceGetCurrPcieLinkGeneration", "pcie_gen"),
                        ("nvmlDeviceGetCurrPcieLinkWidth", "pcie_width")):
            if nv.has(fn):
                v = u32(0)
                if getattr(nv.dll, fn)(nv.dev, ctypes.byref(v)) == 0:
                    d[key] = v.value
        # error counters via field values
        if nv.has("nvmlDeviceGetFieldValues"):
            ids = sorted(PCIE_ERR_FIELDS)
            arr = (_FieldValue * len(ids))()
            for i, fid in enumerate(ids):
                arr[i].fieldId = fid
            if nv.dll.nvmlDeviceGetFieldValues(nv.dev, len(ids), arr) == 0:
                per = {}
                for i, fid in enumerate(ids):
                    fv = arr[i]
                    if fv.nvmlReturn == 0:
                        per[PCIE_ERR_FIELDS[fid]] = _field_val(fv)
                # headline = the three non-overlapping AER aggregates only, so a
                # single event is not counted 2-3x by also adding its subtypes.
                total = (per.get("correctable", 0) + per.get("non_fatal", 0)
                         + per.get("fatal", 0))
                d["pcie_err_total"] = total
                d["pcie_err"] = per
                if self._pcie_baseline is None:
                    self._pcie_baseline = total
                d["pcie_err_since"] = total - self._pcie_baseline

    def _read_misc(self, d):
        a = self.nvapi
        if a.ok and a.CurrentPstate:
            v = u32(0)
            if a.CurrentPstate(a.gpu, ctypes.byref(v)) == 0:
                d["pstate"] = v.value
        vb = self.read_voltage_boost()
        if vb is not None:
            d["vboost_pct"] = vb
        nvl = self.nvml
        if nvl.ok and nvl.has("nvmlDeviceGetMinMaxClockOfPState"):
            for ctype, key in ((0, "core_p0max"), (2, "mem_p0max")):
                mn, mx = u32(0), u32(0)
                if nvl.dll.nvmlDeviceGetMinMaxClockOfPState(
                        nvl.dev, ctype, 0, ctypes.byref(mn),
                        ctypes.byref(mx)) == 0:
                    d[key] = mx.value
        nv = self.nvml
        if nv.ok and nv.has("nvmlDeviceGetTotalEnergyConsumption"):
            v = u64(0)
            if nv.dll.nvmlDeviceGetTotalEnergyConsumption(
                    nv.dev, ctypes.byref(v)) == 0:
                d["energy_j"] = v.value / 1000.0
        # hard VF lock state (read only; write path documented, not fired)
        if a.ok and a.BoostLock:
            for ver in (2, 1, 3):
                bl = _ClockLock(version=a.ver(_ClockLock, ver))
                if a.BoostLock(a.gpu, ctypes.byref(bl)) == 0:
                    locked = [bl.locks[k].domain for k in range(min(bl.count, 32))
                              if bl.locks[k].lockMode != 0]
                    d["vf_locked_domains"] = locked
                    break

    # ---- writers (guarded, reversible) ----------------------------------- #
    def mem_offset_scale(self):
        """NVML mem-offset units per 1 unit of the value the mem slider shows.
        NVML mem units are DDR-doubled: reported clock delta = NVML/2. For a known
        GDDR type the slider is in TRUE memory MHz (reported = true*div), so
        NVML = true * 2 * div. For an unknown type the slider stays in the raw
        reported ('effective') scale, NVML = eff * 2."""
        div = self.static.get("mem_div")
        return (2 * div, "MHz true") if div else (2, "MHz eff")

    def set_clock_offset(self, ctype, mhz):
        """ctype 0=GRAPHICS (mhz in MHz, snapped to the 15 MHz grid),
        2=MEM (mhz in TRUE memory MHz for a known GDDR type, else raw/effective).
        The method converts to the driver's internal units. Reset via 0."""
        nv = self.nvml
        if not nv.ok or not nv.has("nvmlDeviceSetClockOffsets"):
            return False, "nvmlDeviceSetClockOffsets not available"
        dom = "core" if ctype == 0 else "mem"
        mhz = int(mhz)
        if ctype == 0:
            # The core offset lands in the same per-point VF delta table, so an
            # offset that is not a whole 15 MHz bin de-phases the curve: points
            # cross bin boundaries at different offsets and flats reappear.
            mhz = int(round(mhz / (VF_STEP_KHZ / 1000)) * (VF_STEP_KHZ / 1000))
            scale, unit = 1, "MHz"
        else:
            scale, unit = self.mem_offset_scale()
        units = mhz * scale
        rng = self._offset_range(ctype)
        lo, hi = rng[:2] if rng else ((-1000, 1000) if ctype == 0 else (-2000, 6000))
        if not (lo <= units <= hi):
            elo, ehi = int(lo / scale), int(hi / scale)
            return False, f"{dom} offset {mhz:+d} {unit} out of range [{elo}..{ehi}]"
        co = _ClockOffset(version=nv.ver(_ClockOffset, 1), type=ctype,
                          pstate=0, off=units)
        st = nv.dll.nvmlDeviceSetClockOffsets(nv.dev, ctypes.byref(co))
        if st == 0:
            return True, f"{dom} offset set to {mhz:+d} {unit}"
        return False, f"{dom} offset failed: {nv.errstr(st)}"

    def set_power_limit_mw(self, mw):
        nv = self.nvml
        if not nv.ok:
            return False, "NVML unavailable"
        mw = int(mw)
        # driver constraints if known, else a conservative sanity envelope
        mn = self.static.get("pl_min_mw", 50000)
        mx = self.static.get("pl_max_mw", 400000)
        if not (mn <= mw <= mx):
            return False, f"limit {mw/1000:.0f} W out of [{mn/1000:.0f}..{mx/1000:.0f}] W"
        st = nv.dll.nvmlDeviceSetPowerManagementLimit(nv.dev, u32(mw))
        if st == 0:
            return True, f"power limit set to {mw/1000:.0f} W"
        return False, f"power limit failed: {nv.errstr(st)}"

    @_synchronized
    def lockable_clocks_by_mem(self):
        """[(mem_mhz, [graphics clocks])] - the driver enumerates a DIFFERENT
        lockable set per memory clock (on TU102: 24 clocks 300-645 at mem 405,
        but 121 clocks 360-2160 at the top mem clock). static['gfx_min/max']
        only carries the top-mem row, which is why a lock that the driver
        would accept at one memory state is refused at another."""
        nv = self.nvml
        out = []
        if not (nv.ok and nv.has("nvmlDeviceGetSupportedGraphicsClocks")):
            return out
        for m in (self.static.get("mem_clocks") or []):
            n = u32(256)
            arr = (u32 * 256)()
            if nv.dll.nvmlDeviceGetSupportedGraphicsClocks(
                    nv.dev, u32(m), ctypes.byref(n), arr) != 0:
                continue
            g = sorted(arr[i] for i in range(min(n.value, 256)))
            if g:
                out.append((m, g))
        return out

    def lock_gpu_clocks(self, mn_mhz, mx_mhz):
        nv = self.nvml
        if not nv.ok or not nv.has("nvmlDeviceSetGpuLockedClocks"):
            return False, "SetGpuLockedClocks not available"
        mn_mhz, mx_mhz = int(mn_mhz), int(mx_mhz)
        if mn_mhz > mx_mhz:
            return False, f"lock: min {mn_mhz} > max {mx_mhz} MHz"
        lo = self.static.get("gfx_min")
        hi = self.static.get("gfx_max")
        if lo is not None and hi is not None and not (
                lo <= mn_mhz <= hi and lo <= mx_mhz <= hi):
            return False, f"lock: values must be within [{lo}..{hi}] MHz"
        st = nv.dll.nvmlDeviceSetGpuLockedClocks(nv.dev, u32(mn_mhz), u32(mx_mhz))
        if st == 0:
            return True, f"GPU clock locked to [{mn_mhz}..{mx_mhz}] MHz"
        return False, f"lock failed: {nv.errstr(st)} (needs admin)"

    def reset_gpu_clocks(self):
        nv = self.nvml
        if not nv.ok or not nv.has("nvmlDeviceResetGpuLockedClocks"):
            return False, "ResetGpuLockedClocks not available"
        st = nv.dll.nvmlDeviceResetGpuLockedClocks(nv.dev)
        if st == 0:
            return True, "GPU clock lock released"
        return False, f"reset failed: {nv.errstr(st)}"

    def set_fan(self, pct):
        nv = self.nvml
        if not nv.ok or not nv.has("nvmlDeviceSetFanSpeed_v2"):
            return False, "SetFanSpeed_v2 not available"
        pct = int(pct)
        floor = self.static.get("fan_min", 30)
        if pct < floor:
            return False, (f"fan {pct}% below hardware minimum {floor}% "
                           f"- use Auto for the zero-RPM idle curve")
        pct = max(0, min(100, pct))
        nf = u32(1)
        nv.dll.nvmlDeviceGetNumFans(nv.dev, ctypes.byref(nf))
        errs = []
        for f in range(max(nf.value, 1)):
            st = nv.dll.nvmlDeviceSetFanSpeed_v2(nv.dev, u32(f), u32(pct))
            if st != 0:
                errs.append(f"fan{f}:{nv.errstr(st)}")
        if not errs:
            return True, f"fans set to manual {pct}%"
        return False, "; ".join(errs) + " (needs admin)"

    def reset_fan(self):
        nv = self.nvml
        if not nv.ok or not nv.has("nvmlDeviceSetDefaultFanSpeed_v2"):
            return False, "SetDefaultFanSpeed_v2 not available"
        nf = u32(1)
        nv.dll.nvmlDeviceGetNumFans(nv.dev, ctypes.byref(nf))
        errs = []
        for f in range(max(nf.value, 1)):
            st = nv.dll.nvmlDeviceSetDefaultFanSpeed_v2(nv.dev, u32(f))
            if st != 0:
                errs.append(f"fan{f}:{nv.errstr(st)}")
        if not errs:
            return True, "fans returned to automatic"
        return False, "; ".join(errs)

    # ---- over-voltage % (AB "Core Voltage" slider) ------------------------ #
    def read_voltage_boost(self):
        a = self.nvapi
        if not (a.ok and a.VoltCtrlGet):
            return None
        vc = _VoltBoost(version=a.ver(_VoltBoost, 1))
        if a.VoltCtrlGet(a.gpu, ctypes.byref(vc)) == 0:
            return vc.percent
        return None

    def set_voltage_boost(self, pct):
        """0..100 % of the VBIOS over-voltage headroom. Read-modify-write to
        preserve the reserved fields. Reversible via pct=0 / reboot."""
        a = self.nvapi
        if not (a.ok and a.VoltCtrlGet and a.VoltCtrlSet):
            return False, "voltage-control APIs unavailable"
        pct = int(pct)
        if not (0 <= pct <= 100):
            return False, f"voltage boost {pct}% out of [0..100]"
        vc = _VoltBoost(version=a.ver(_VoltBoost, 1))
        if a.VoltCtrlGet(a.gpu, ctypes.byref(vc)) != 0:
            return False, "voltage GetControl failed (cannot read-modify-write)"
        vc.version = a.ver(_VoltBoost, 1)   # re-stamp; preserve unknown[8]
        vc.percent = pct
        st = a.VoltCtrlSet(a.gpu, ctypes.byref(vc))
        if st == 0:
            return True, f"core voltage boost set to {pct}% (ceiling unlock)"
        return False, f"voltage set failed (status {st}) - needs admin"

    # ---- VF curve --------------------------------------------------------- #
    def read_vf_curve(self):
        """Return (points, err). points = list of dicts sorted by curve index:
        {idx, volt_mv, freq_mhz (evaluated, includes current deltas), delta_khz}."""
        a = self.nvapi
        if not (a.ok and a.VfpCurve and a.BoostTableGet):
            return None, "VF curve APIs unavailable"
        cv = _VfpCurve(version=a.ver(_VfpCurve, 1))
        _set_all_point_masks(cv)
        st = a.VfpCurve(a.gpu, ctypes.byref(cv))
        if st != 0:
            return None, f"curve read failed (status {st})"
        bt = _BoostTable(version=a.ver(_BoostTable, 1))
        _set_all_point_masks(bt)
        st = a.BoostTableGet(a.gpu, ctypes.byref(bt))
        if st != 0:
            return None, f"delta-table read failed (status {st})"
        points = []
        for i in range(VFP_POINTS):
            e = cv.entries[i]
            if e.freq_kHz == 0:
                continue
            points.append({"idx": i,
                           "volt_mv": e.volt_uV / 1000.0,
                           "freq_mhz": e.freq_kHz / 1000.0,
                           "delta_khz": bt.rows[i].w[5]})
        if not points:
            return None, "curve read returned no points"
        return points, None

    @staticmethod
    def peak_info(points):
        """(peak_mhz, park_idx, park_mv, n_at_peak) - the peak frequency and the
        LOWEST-voltage point holding it. When several voltages map to the same
        frequency the driver runs the lowest of them, so this - not the
        highest-voltage point below the cap - is where the card actually sits."""
        if not points:
            return 0.0, None, 0.0, 0
        peak = max(p["freq_mhz"] for p in points)
        at = [p for p in points if p["freq_mhz"] == peak]
        return peak, at[0]["idx"], at[0]["volt_mv"], len(at)

    EXTRA_POINTS_ABOVE_CAP = 1   # target one VF point past the cap (safety)

    @staticmethod
    def compute_deflatten(points, vcap_mv, max_khz=None, extra_points_above=None):
        """Make the boundary point - the last point at/below vcap PLUS
        `extra_points_above` points above it - the UNIQUE maximum, so the boost
        arbiter (which runs the lowest voltage of any peak-frequency flat) parks
        there, at the highest voltage/clock the cap allows. Mechanism: raise the
        boundary point to one 15 MHz bin above the highest point BELOW it (capped
        at the hardware max), then level every point ABOVE the boundary onto that
        value - a clean flat top whose lowest-voltage member is the boundary.

        Points BELOW the boundary are left untouched. That is deliberate: the
        low-voltage floor is many points pinned at the minimum clock, and a
        strict-rise-from-the-bottom pass would ramp them into demanding high
        clocks at tiny voltages - instant instability. de-flatten only removes
        the top tie (about +1 bin); the overall ceiling is raised by the core
        offset, not here. Every move is a whole 15 MHz bin.
        Returns (changes, ceil_before_mhz, ceil_after_mhz, meta)."""
        if extra_points_above is None:
            extra_points_above = GPU.EXTRA_POINTS_ABOVE_CAP
        n = len(points)
        khz = [int(round(p["freq_mhz"] * 1000)) for p in points]
        below = [i for i in range(n) if below_cap(points[i]["volt_mv"], vcap_mv)]
        if n == 0 or not below:
            return [], 0.0, 0.0, {"clamped": False, "boundary_idx": None,
                                  "unique": False}
        B = min(max(below) + max(0, extra_points_above), n - 1)
        ceil_before = khz[max(below)]
        peak_below = max(khz[:B]) if B > 0 else -1
        target = max(khz[B], peak_below + VF_STEP_KHZ)
        clamped = False
        if max_khz is not None and target > max_khz:
            target = max_khz
            clamped = True
        new = {}
        if khz[B] != target:
            new[B] = target
        for i in range(B + 1, n):          # flat top at target; park = boundary
            if khz[i] != target:
                new[i] = target
        changes = [(points[i]["idx"], points[i]["volt_mv"], khz[i] / 1000.0,
                    new[i] / 1000.0,
                    points[i]["delta_khz"] + (new[i] - khz[i]))
                   for i in sorted(new)]
        return (changes, ceil_before / 1000.0, target / 1000.0,
                {"clamped": clamped, "boundary_idx": points[B]["idx"],
                 "unique": target > peak_below})

    def rephase_deltas(self):
        """Force every delta onto ONE 15 MHz phase. Uniform offsets (the core
        slider, or any whole-curve move) only stay grid-exact if all deltas share
        a remainder mod 15 MHz; a point left on another phase crosses bin
        boundaries at different offsets and silently re-creates a flat. Off-phase
        deltas are rounded DOWN to the common phase, so a point can only lose a
        bin, never gain one unasked."""
        pts, err = self.read_vf_curve()
        if err:
            return False, err
        counts = {}
        for p in pts:
            r = p["delta_khz"] % VF_STEP_KHZ
            counts[r] = counts.get(r, 0) + 1
        target = max(counts, key=lambda r: counts[r])
        new = {p["idx"]: p["delta_khz"] - ((p["delta_khz"] % VF_STEP_KHZ - target)
                                           % VF_STEP_KHZ)
               for p in pts if p["delta_khz"] % VF_STEP_KHZ != target}
        if not new:
            return True, f"all {len(pts)} deltas already share one 15 MHz phase"
        ok, m = self.apply_vf_deltas(new)
        if ok:
            return True, (f"re-phased {len(new)} off-phase point(s) "
                          f"(idx {sorted(new)}) onto the common 15 MHz grid")
        return False, m

    MAX_ABS_DELTA_KHZ = 1_000_000  # garbage guard only: |delta| never exceeds 1 GHz

    def apply_vf_deltas(self, new_deltas):
        """Read-modify-write the whole delta table (AB-style). new_deltas maps
        idx -> absolute delta_khz; only differing rows are touched. Bounds only
        against garbage (|delta| <= 1 GHz), so legitimate de-flatten compounding
        and deliberate editor moves are never blocked."""
        a = self.nvapi
        if not (a.ok and a.BoostTableGet and a.BoostTableSet):
            return False, "boost-table APIs unavailable"
        bt = _BoostTable(version=a.ver(_BoostTable, 1))
        _set_all_point_masks(bt)
        st = a.BoostTableGet(a.gpu, ctypes.byref(bt))
        if st != 0:
            return False, f"pre-write table read failed (status {st})"
        nchg = 0
        for idx, delta in new_deltas.items():
            idx, delta = int(idx), int(delta)
            if not (0 <= idx < VFP_POINTS):
                return False, f"point index {idx} out of range"
            if abs(delta) > self.MAX_ABS_DELTA_KHZ:
                return False, (f"refusing point {idx}: delta {delta // 1000} MHz "
                               f"exceeds the +/-1000 MHz sanity bound")
            if bt.rows[idx].w[5] != delta:
                bt.rows[idx].w[5] = delta
                nchg += 1
        if nchg == 0:
            return True, "no delta changes to apply"
        bt.version = a.ver(_BoostTable, 1)
        _set_all_point_masks(bt)
        st = a.BoostTableSet(a.gpu, ctypes.byref(bt))
        if st == 0:
            return True, f"VF delta table written ({nchg} points changed)"
        return False, f"VF table write failed (status {st})"

    def reset_vf_curve(self):
        """Zero every point's delta = factory VF curve. Removes all offsets and
        any de-flatten/editor edits. Stock Turing deltas are 0, so this is the
        unambiguous 'back to stock' with no persisted-baseline to be poisoned."""
        return self.apply_vf_deltas({i: 0 for i in range(VFP_POINTS)})

    def reset_all(self):
        """Return a list of (ok, message) so the caller can flag partial resets.
        Each element is a ResetStep, so it also unpacks as that pair while
        naming the knob it moved - GPU.LOCK_STEP is the clock-lock release."""
        steps = [ResetStep("core offset", self.set_clock_offset(0, 0)),
                 ResetStep("mem offset", self.set_clock_offset(2, 0))]
        if self.static.get("pl_def_mw"):
            steps.append(ResetStep(
                "power limit", self.set_power_limit_mw(self.static["pl_def_mw"])))
        else:
            steps.append(ResetStep(
                "power limit",
                (False, "power limit: default unknown, left unchanged")))
        steps.append(ResetStep(self.LOCK_STEP, self.reset_gpu_clocks()))
        steps.append(ResetStep("fan", self.reset_fan()))
        if self.nvapi.ok and self.nvapi.VoltCtrlGet and self.nvapi.VoltCtrlSet:
            steps.append(ResetStep("voltage boost", self.set_voltage_boost(0)))
        if self.nvapi.ok and self.nvapi.BoostTableSet:
            # curve edits live here too
            steps.append(ResetStep("vf curve", self.reset_vf_curve()))
        return steps


# --------------------------------------------------------------------------- #
#  Standalone snapshot for testing                                            #
# --------------------------------------------------------------------------- #
def _fmt_snapshot(g):
    s = g.static
    out = [f"device : {s['name']}  driver {s['driver']}  vbios {s['vbios']}",
           f"admin  : {s['admin']}",
           f"backend: {g.status_line()}"]
    d = g.read()
    out.append("")
    out.append("--- clocks ---")
    out.append(f"  core {d.get('core','?')} MHz   mem {d.get('mem','?')} MHz   "
               f"video {d.get('video','?')} MHz   pstate P{d.get('pstate','?')}")
    mem_off = d.get("mem_off")
    mem_eff = f"{mem_off // 2} MHz(eff)" if isinstance(mem_off, int) else "?"
    out.append(f"  applied offsets: core {d.get('core_off','?')} MHz  mem {mem_eff}")
    out.append("--- thermals ---")
    out.append(f"  edge {d.get('temp_edge','?')} C   "
               f"hotspot {d.get('temp_hotspot','?')} C   "
               f"delta {d.get('temp_delta','?')} C")
    out.append(f"  Vcore {d.get('vcore_mv','?')} mV")
    out.append("--- power ---")
    out.append(f"  draw {d.get('power_w','?')} W   "
               f"GPU {d.get('pwr_gpu_pct','?')}%  BOARD {d.get('pwr_board_pct','?')}%  "
               f"target {d.get('pl_target_pct','?')}% of TDP")
    out.append(f"  enforced limit {d.get('pl_now_mw','?')} mW   "
               f"constraints [{s.get('pl_min_mw','?')}..{s.get('pl_max_mw','?')}] "
               f"def {s.get('pl_def_mw','?')} mW")
    out.append("--- fan / util ---")
    out.append(f"  fans {d.get('fans','?')}")
    out.append(f"  util gpu {d.get('util_gpu','?')}%  fb {d.get('util_fb','?')}%  "
               f"vid {d.get('util_vid','?')}%  bus {d.get('util_bus','?')}%")
    out.append("--- throttle ---")
    em = d.get("event_mask", 0)
    active = [n for b, n in EVENT_REASONS if em & b] or ["none"]
    out.append(f"  event reasons: {active}")
    pdv = d.get("perf_decrease", 0)
    pda = [n for b, n in PERF_DECREASE_BITS if pdv & b] or ["none"]
    out.append(f"  perf-decrease: {pda}")
    out.append("--- pcie ---")
    out.append(f"  gen {d.get('pcie_gen','?')} x{d.get('pcie_width','?')}   "
               f"errors total {d.get('pcie_err_total','?')} "
               f"(since start {d.get('pcie_err_since','?')})")
    out.append(f"  energy {d.get('energy_j','?')} J   "
               f"vf-locked domains {d.get('vf_locked_domains','?')}")
    out.append(f"  offset ranges: core {s.get('core_off_range')}  "
               f"mem {s.get('mem_off_range')}")
    return "\n".join(out)


if __name__ == "__main__":
    g = GPU()
    if not g.available():
        print("No GPU backend available.")
        print(g.status_line())
        sys.exit(1)
    print(_fmt_snapshot(g))


# Serialize every driver-touching entry point (RLock => nested calls are fine).
for _m in ("read", "read_clock_domains", "read_vf_curve", "apply_vf_deltas",
           "reset_vf_curve",
           "rephase_deltas", "set_clock_offset", "set_power_limit_mw",
           "lock_gpu_clocks", "reset_gpu_clocks", "set_fan", "reset_fan",
           "set_voltage_boost", "read_voltage_boost", "reset_all"):
    setattr(GPU, _m, _synchronized(getattr(GPU, _m)))
