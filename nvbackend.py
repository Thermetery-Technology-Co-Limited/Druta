"""
TitanTune - GPU backend (NVAPI + NVML), read + guarded write.

Built for the Titan RTX (TU102, DEV_1E02) on the ASUS 2080 Ti Strix PCB, but
falls back to GPU index 0 for any NVIDIA card. All struct layouts are lifted
verbatim from the read-only probes verified live on this card (driver 591.44):
NVAPI ids and NVML field numbers were confirmed against the hardware, not guessed.

Design rule: readers never change state. Writers are explicit, each returns
(ok, message), and only the reversible knobs are wired (clock offsets, power
limit, locked clocks, the per-domain V/F point lock, fan). Footgun knobs (force
P-state, TCC, CUDA-clocks) are surfaced as telemetry + documented commands
elsewhere, never fired blind.

An unverified setter is not wired on the strength of a plausible struct. It
earns its place by climbing a ladder: the id RESOLVES, then an IDENTITY WRITE
of the getter's own bytes is accepted and changes nothing, then a single-field
read-modify-write moves the one thing it was supposed to move and writing the
original bytes back restores it exactly. The V/F point lock below is the worked
example, and vf_lock_self_test() keeps the middle rung runnable on any machine.
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
        # Per-domain V/F point lock. BoostLock is its GETTER; VfLockSet is the
        # matching setter, and BOTH take the same 780-byte _ClockLock. That
        # shared layout is the whole safety argument: every write hands back a
        # buffer this getter produced (see GPU.set_vf_lock).
        self.BoostLock = self._i(0xE440B867, PTR, PTR)
        self.VfLockSet = self._i(0x39442CFB, PTR, PTR)
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


# The per-domain V/F point lock, verified end to end on this card (getter
# 0xE440B867, setter 0x39442CFB, ONE _ClockLock for both). Clean-room: the
# layout came from the driver's own GET output, never from disassembly.
#
# lockMode 3 does NOT mean "run at exactly this voltage". It means LOCK TO THE
# HIGHEST V/F POINT AT OR BELOW the requested voltage - the same "<= cap" rule
# below_cap() states for the de-flatten cap. Measured here: asking 900000 uV
# delivered 893.75 mV = 143 * 6.25, a real point on the 6.25 mV grid, and core
# went 1950 -> 1740. So the point actually held may sit BELOW the one asked for.
#
# AND THE STRUCT DOES NOT TELL YOU WHICH. volt_uV stores the REQUEST verbatim:
# a 900000 uV lock reads straight back as 900000 while the rail sits at 893.75,
# and this card was found holding a 1137500 uV lock on a curve that stops at
# 1087500. Reading the getter therefore answers "what was asked for", never
# "what is held" - GPU.resolve_vf_point() answers the second, against the
# curve, and the vcore rail confirms it. A read-back is still mandatory, but
# for a different reason: it is how a concurrent tuner overwriting the lock
# gets noticed.
#
# THE NVML FREQUENCY LOCK LIVES IN THIS SAME TABLE, and the mode is the only
# thing telling them apart. nvmlDeviceSetGpuLockedClocks(lo, hi) writes TWO
# mode-2 entries whose "volt_uV" field is a FREQUENCY IN kHz, not a voltage:
# domain 0 takes hi, domain 1 takes lo (measured with an asymmetric lock -
# 1350..1800 produced domain 0 = 1800000 and domain 1 = 1350000). They coexist
# with the mode-3 entry; reset_gpu_clocks clears the mode-2 pair and leaves
# mode 3 alone. So every lookup here matches on MODE, never on "lockMode != 0":
# reading a mode-2 entry as a voltage yields a confident 1350.00 mV, and
# clearing one from this side would silently release the other mechanism's
# lock - the exact confusion the two-mechanism split exists to prevent.
VF_LOCK_VERSION = 2                 # version = sizeof | (2<<16) = 0x0002030C
VF_LOCK_MODE_OFF = 0                # entry present, not locked
VF_LOCK_MODE_FREQ = 2               # NVML locked clocks; field is kHz
VF_LOCK_MODE_POINT = 3              # locked to the point at or below volt_uV
# domain 0 carries the max and domain 1 the min of an NVML frequency lock
CLK_LOCK_DOMAIN_MAX, CLK_LOCK_DOMAIN_MIN = 0, 1
# The domain that carries the lock on this card. Only a fallback: an existing
# lock is always re-targeted at whatever domain the driver already has it on,
# so a card that uses a different one keeps working without a code change.
VF_LOCK_DOMAIN = 6
# Garbage guard only, in the spirit of MAX_ABS_DELTA_KHZ - not a safety limit.
# It cannot be one: mode 3 resolves DOWN onto an existing point, so an absurdly
# high request lands on the top point of the curve and an absurdly low one on
# the bottom. This exists to catch a caller that passed millivolts.
VF_LOCK_MIN_UV, VF_LOCK_MAX_UV = 400_000, 1_300_000

assert ctypes.sizeof(_ClockLock) == 0x030C   # 780; wrong size => version lies


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
    # names for the reset_all steps a caller has to single out (see ResetStep).
    # There are TWO lock mechanisms and a caller clearing its on-screen record
    # has to know which one the reset actually released.
    LOCK_STEP = "clock lock"
    VF_LOCK_STEP = "v/f point lock"

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
        # V/F point-lock state. Version 2 is the verified one; the other two are
        # kept as a fallback for a driver that numbers this struct differently.
        # This is the read-back for a knob the app now WRITES (set_vf_lock), so
        # it carries the lock voltage too. That voltage is the one REQUESTED,
        # not the point held (the struct echoes it back verbatim) - which is
        # exactly why it is worth showing: it is how a lock somebody else set,
        # at a value this app would never pick, becomes visible.
        if a.ok and a.BoostLock:
            for ver in (VF_LOCK_VERSION, 1, 3):
                bl = _ClockLock(version=a.ver(_ClockLock, ver))
                if a.BoostLock(a.gpu, ctypes.byref(bl)) != 0:
                    continue
                ents = [bl.locks[k] for k in range(min(bl.count, 32))]
                # split by MODE. Both mechanisms live in this table and their
                # shared field means different things (uV vs kHz), so one
                # merged "locked domains" list would print a 1350 MHz clock
                # lock as a 1350.00 mV point lock.
                d["vf_locked_domains"] = [e.domain for e in ents
                                          if e.lockMode == VF_LOCK_MODE_POINT]
                for e in ents:
                    if e.lockMode == VF_LOCK_MODE_POINT:
                        d["vf_lock_mv"] = e.volt_uV / 1000.0
                        break
                freq = {e.domain: e.volt_uV for e in ents
                        if e.lockMode == VF_LOCK_MODE_FREQ}
                if CLK_LOCK_DOMAIN_MAX in freq:
                    hi = freq[CLK_LOCK_DOMAIN_MAX]
                    d["clk_lock_mhz"] = (freq.get(CLK_LOCK_DOMAIN_MIN, hi) // 1000,
                                         hi // 1000)
                break

    # ---- writers (guarded, reversible) ----------------------------------- #
    def mem_offset_scale(self):
        """NVML mem-offset units per 1 unit of the value the mem slider shows.
        NVML mem units are DDR-doubled: reported clock delta = NVML/2. For a known
        GDDR type the slider is in TRUE memory MHz (reported = true*div), so
        NVML = true * 2 * div. For an unknown type the slider stays in the raw
        reported ('effective') scale, NVML = eff * 2. That is to say, for GDDR5, 5X, and 6, 
        the adjustment is numerically faithful to what you see in GPU-Z"""
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
        # Range-checking is not enough: the driver accepts any in-range value,
        # reports success at the value asked for, records it verbatim in its
        # own lock table - and then runs the next ENUMERATED clock UP. Measured:
        # a 1234 request (between the valid 1230 and 1245) ran at 1245, with
        # both the API and the lock record still claiming 1234. That is clock
        # nobody asked for, reported as if it were the request. So snap DOWN to
        # a member of the table first, the same rule the core offset follows.
        rows = self.lockable_clocks_by_mem()
        table = rows[-1][1] if rows else []
        snapped = []
        for want in (mn_mhz, mx_mhz):
            below = [c for c in table if c <= want]
            snapped.append(max(below) if below else want)
        sn_mn, sn_mx = snapped
        if table and (sn_mn, sn_mx) != (mn_mhz, mx_mhz):
            note = (f" (snapped down from [{mn_mhz}..{mx_mhz}]: the driver "
                    f"would have rounded UP to a clock you did not ask for)")
        else:
            note = ""
        st = nv.dll.nvmlDeviceSetGpuLockedClocks(nv.dev, u32(sn_mn), u32(sn_mx))
        if st == 0:
            return True, f"GPU clock locked to [{sn_mn}..{sn_mx}] MHz{note}"
        return False, f"lock failed: {nv.errstr(st)} (needs admin)"

    def reset_gpu_clocks(self):
        nv = self.nvml
        if not nv.ok or not nv.has("nvmlDeviceResetGpuLockedClocks"):
            return False, "ResetGpuLockedClocks not available"
        st = nv.dll.nvmlDeviceResetGpuLockedClocks(nv.dev)
        if st == 0:
            return True, "GPU clock lock released"
        return False, f"reset failed: {nv.errstr(st)}"

    # ---- per-domain V/F point lock ---------------------------------------- #
    # A SECOND, entirely separate lock mechanism from the NVML locked clocks
    # above. They are not two views of one thing and neither call reads or
    # clears the other, so anything holding both must release both:
    #
    #   nvmlDeviceSetGpuLockedClocks  pins a FREQUENCY range. On this card at
    #       idle it leaves the memory clock in the low state (mem 810).
    #   the V/F point lock              pins a V/F POINT by voltage. Measured
    #       here it holds TRUE P0 - pstate 0, mem 7000 - with the card at ~5%
    #       utilisation, which is strictly better for holding a tune steady.
    #
    # Both are volatile: a reboot clears them.
    def _vf_lock_available(self):
        """Both ends of the pair must resolve. The getter alone is a reader;
        without the setter there is no write path, and half a pair must never
        look like a working one."""
        a = self.nvapi
        return bool(a.ok and a.BoostLock and a.VfLockSet)

    def _vf_lock_read_raw(self):
        """The driver's OWN 780-byte lock buffer, or None if the getter did not
        answer. Every write path in this section starts here. Handing back a
        buffer the driver produced - rather than one we assembled from a struct
        definition - is what makes this setter safe, and it is how it was
        validated. Nothing below ever constructs a _ClockLock to write."""
        a = self.nvapi
        if not (a.ok and a.BoostLock):
            return None
        cl = _ClockLock(version=a.ver(_ClockLock, VF_LOCK_VERSION))
        if a.BoostLock(a.gpu, ctypes.byref(cl)) != 0:
            return None
        return cl

    @staticmethod
    def _vf_lock_entries(cl):
        """The entries the driver says are real - count, not the 32 the struct
        reserves. count is 7 here; reading past it would report stale slots as
        lockable domains."""
        return [cl.locks[k] for k in range(min(cl.count, 32))]

    def read_vf_lock(self):
        """The V/F point lock the card is holding NOW, or None when nothing is
        locked (or the getter did not answer - same as read_voltage_boost).

            {domain, lockMode, volt_uV, volt_mv, count}

        volt_mv is the voltage the lock was REQUESTED at, not the point the
        hardware resolved to - the driver stores the request verbatim (see the
        VF_LOCK_* block). Pass it through resolve_vf_point() to name the point
        actually held. What this call is authoritative about is WHETHER a lock
        is in force and WHOSE number is in it, which is how a concurrent tuner
        re-asserting its own value gets caught."""
        cl = self._vf_lock_read_raw()
        if cl is None:
            return None
        for e in self._vf_lock_entries(cl):
            # mode 3 ONLY - a mode-2 entry in this table is the NVML frequency
            # lock and its field is kHz, so reporting it here would hand the
            # caller 1350.00 "mV" for a 1350 MHz clock lock
            if e.lockMode == VF_LOCK_MODE_POINT:
                return {"domain": e.domain, "lockMode": e.lockMode,
                        "volt_uV": e.volt_uV, "volt_mv": e.volt_uV / 1000.0,
                        "count": cl.count}
        return None

    def read_clk_lock(self):
        """The NVML frequency lock, read back out of the SAME table as
        (min_mhz, max_mhz), or None when it is not set.

        Worth having because NVML itself cannot answer this on this card -
        nvmlDeviceGetGpuLockedClocks is absent from the DLL, which is why a
        lock left behind by an earlier run used to be invisible to the next
        one. The mode-2 entries make it readable after all."""
        cl = self._vf_lock_read_raw()
        if cl is None:
            return None
        by_dom = {e.domain: e.volt_uV for e in self._vf_lock_entries(cl)
                  if e.lockMode == VF_LOCK_MODE_FREQ}
        if not by_dom:
            return None
        hi = by_dom.get(CLK_LOCK_DOMAIN_MAX)
        lo = by_dom.get(CLK_LOCK_DOMAIN_MIN, hi)
        if hi is None:
            return None
        return (lo // 1000, hi // 1000)

    def set_vf_lock(self, volt_uv, domain=None):
        """Lock the curve to the highest V/F point AT OR BELOW volt_uv.

        READ-MODIFY-WRITE, never a fresh struct: the buffer written is the one
        the getter just produced, with lockMode and volt_uV changed on ONE
        entry and every other byte - flags, count, the three unknown dwords per
        entry, the six other domains - left exactly as the driver wrote them.

        `domain` defaults to whichever entry is already locked, so a second
        call MOVES the lock instead of adding a second one; with nothing locked
        it falls back to VF_LOCK_DOMAIN.

        The read-back afterwards is not there to learn what the hardware
        resolved to - the struct only ever echoes the request - but to catch a
        concurrent tuner that took the lock straight back. Callers that need to
        name the point really held must resolve the request against the curve
        (resolve_vf_point) or read the vcore rail."""
        a = self.nvapi
        if not self._vf_lock_available():
            return False, ("V/F point lock unavailable: 0xE440B867 / 0x39442CFB "
                           "did not both resolve")
        volt_uv = int(volt_uv)
        if not (VF_LOCK_MIN_UV <= volt_uv <= VF_LOCK_MAX_UV):
            return False, (f"V/F lock: {volt_uv} uV outside the sanity envelope "
                           f"[{VF_LOCK_MIN_UV}..{VF_LOCK_MAX_UV}] uV "
                           f"- the argument is MICROvolts")
        cl = self._vf_lock_read_raw()
        if cl is None:
            return False, "V/F lock: getter failed, refusing to write blind"
        entries = self._vf_lock_entries(cl)
        if domain is None:
            # mode 3 only: an NVML frequency lock puts mode-2 entries on
            # domains 0 and 1, and re-targeting one of those would convert the
            # other mechanism's lock into a voltage lock on the wrong domain
            held = [e for e in entries if e.lockMode == VF_LOCK_MODE_POINT]
            domain = held[0].domain if held else VF_LOCK_DOMAIN
        target = next((e for e in entries if e.domain == domain), None)
        if target is None:
            return False, (f"V/F lock: domain {domain} is not in the driver's "
                           f"lock table (it lists {[e.domain for e in entries]})")
        target.lockMode = VF_LOCK_MODE_POINT
        target.volt_uV = volt_uv
        cl.version = a.ver(_ClockLock, VF_LOCK_VERSION)  # re-stamp; keep the rest
        st = a.VfLockSet(a.gpu, ctypes.byref(cl))
        if st != 0:
            return False, f"V/F lock write failed (status {st}) - needs admin"
        got = self.read_vf_lock()
        if got is None:
            return False, ("V/F lock write returned OK but the card reports no "
                           "lock - another tool may have taken it straight back")
        if got["volt_uV"] != volt_uv:
            return False, (f"V/F lock: wrote {volt_uv / 1000.0:.2f} mV but the card "
                           f"reports {got['volt_mv']:.2f} mV on domain "
                           f"{got['domain']} - another tool holds this lock")
        return True, (f"V/F point lock set on domain {got['domain']}, requested "
                      f"{volt_uv / 1000.0:.2f} mV - the hardware holds the highest "
                      f"V/F point at or below that")

    def clear_vf_lock(self):
        """Release the V/F point lock: lockMode 0 on every locked entry, by the
        same read-modify-write.

        volt_uV is deliberately left as the driver has it. Mode 0 is what an
        unlocked entry reads back as anyway, and a later session cannot know
        what that field held before somebody locked it - inventing a value
        would be exactly the from-scratch write this section refuses to make.

        Mode-2 entries are left strictly alone. They are the NVML frequency
        lock sharing this table, and reset_gpu_clocks() owns those; clearing
        them from here would mean "release the V/F lock" quietly released the
        other mechanism too."""
        a = self.nvapi
        if not self._vf_lock_available():
            return False, ("V/F point lock unavailable: 0xE440B867 / 0x39442CFB "
                           "did not both resolve")
        cl = self._vf_lock_read_raw()
        if cl is None:
            return False, "V/F lock: getter failed, refusing to write blind"
        held = [e for e in self._vf_lock_entries(cl)
                if e.lockMode == VF_LOCK_MODE_POINT]
        if not held:
            return True, "no V/F point lock was set"
        doms = [e.domain for e in held]
        for e in held:
            e.lockMode = VF_LOCK_MODE_OFF
        cl.version = a.ver(_ClockLock, VF_LOCK_VERSION)
        st = a.VfLockSet(a.gpu, ctypes.byref(cl))
        if st != 0:
            return False, f"V/F lock release failed (status {st}) - needs admin"
        # read back: the release is the one call whose failure would leave the
        # card pinned with nothing on screen saying so
        if self.read_vf_lock() is not None:
            return False, ("V/F lock release returned OK but the card still "
                           "reports a lock - another tool is re-asserting it")
        return True, (f"V/F point lock released "
                      f"(domain{'s' if len(doms) > 1 else ''} "
                      f"{', '.join(str(x) for x in doms)})")

    def vf_lock_self_test(self):
        """Hand the driver back the exact bytes its getter just produced.

        A verified no-op: NVAPI_OK, nothing moves. That makes it the cheap
        proof, ON THIS MACHINE, that both ids resolved and that the 780-byte
        layout is the one this driver expects - without touching a knob. It is
        the middle rung of the ladder in the module docstring, and the reason
        the read-modify-write above was safe to attempt at all."""
        a = self.nvapi
        if not self._vf_lock_available():
            return False, ("V/F lock self-test: 0xE440B867 / 0x39442CFB did not "
                           "both resolve")
        cl = self._vf_lock_read_raw()
        if cl is None:
            return False, "V/F lock self-test: getter 0xE440B867 did not answer"
        before = ctypes.string_at(ctypes.addressof(cl), ctypes.sizeof(cl))
        st = a.VfLockSet(a.gpu, ctypes.byref(cl))
        if st != 0:
            return False, (f"V/F lock self-test: the driver REFUSED an identity "
                           f"write (status {st}) - do not use the V/F lock here")
        after = self._vf_lock_read_raw()
        if after is None:
            return False, ("V/F lock self-test: identity write was accepted but "
                           "the getter stopped answering")
        same = ctypes.string_at(ctypes.addressof(after),
                                ctypes.sizeof(after)) == before
        n = min(cl.count, 32)
        held = [f"{cl.locks[k].domain}:mode{cl.locks[k].lockMode}"
                for k in range(n) if cl.locks[k].lockMode != VF_LOCK_MODE_OFF]
        if not same:
            # not necessarily a fault - a concurrent tuner (Afterburner) moving
            # its own lock between the two reads looks identical from here - but
            # a self-test that cannot prove "changed nothing" has not passed
            return False, (f"V/F lock self-test: identity write accepted but the "
                           f"state CHANGED - either the layout is wrong or "
                           f"another tool wrote between the reads")
        return True, (f"V/F lock self-test passed: identity write accepted, "
                      f"state unchanged, {n} entries, locked "
                      f"{held if held else 'none'}")

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

    @staticmethod
    def resolve_vf_point(points, volt_mv):
        """The point the card will ACTUALLY sit on for a lockMode-3 request of
        `volt_mv`, or None when the whole curve sits above it. `points` is
        read_vf_curve() shape.

        TWO stages, and skipping the second one gets the voltage wrong:
          1. the lock resolves the request DOWN to the highest point at or
             below it - that fixes the FREQUENCY;
          2. the boost arbiter then runs that frequency at the LOWEST voltage
             any point maps it to, the same flat rule peak_info() describes.

        Measured on the rail, both stages at once: requesting 900.00 mV (curve
        idx 72, 1740 MHz) held 1740 MHz but at 893.75 mV, because idx 71 is the
        other half of a 1740 MHz flat. Requesting 950.00 mV held 950.00 mV /
        1830 MHz, idx 80 being the lowest member of its own flat. A request
        above the whole curve clamps to the top point: this card was found
        holding a 1137.50 mV lock and running the 1087.50 mV / 1950 MHz point.

        This has to be derived because the lock struct cannot answer it -
        volt_uV echoes the request back verbatim (see the VF_LOCK_* block).
        below_cap() does stage 1 so this and every other "at or below the cap"
        readout in the app share one definition of the boundary."""
        under = [p for p in points if below_cap(p["volt_mv"], volt_mv)]
        if not under:
            return None
        cap = max(under, key=lambda p: p["volt_mv"])
        flat = [p for p in points if p["freq_mhz"] == cap["freq_mhz"]]
        return min(flat, key=lambda p: p["volt_mv"])

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

    MAX_ABS_DELTA_KHZ = 1_000_000  # Mouse-slip-pepega guard only: |delta| never exceeds 1 GHz

    def apply_vf_deltas(self, new_deltas):
        """Read-modify-write the whole delta table (AB-style). new_deltas maps
        idx -> absolute delta_khz; only differing rows are touched. Bounds only
        against accidental user mouse slip or other sorts of garbage (|delta| <= 1 GHz), so legitimate de-flatten compounding
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
                               f"More than a whole gigahertz of delta? Lol, bro thinks he's Seby. Caught you with a mouse-slip and saved you a total driver crash this time. Don't do it next time. :copege:")
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
        # The V/F point lock is a DIFFERENT mechanism: reset_gpu_clocks does not
        # touch it, so a reset that stopped at the step above would report a
        # clean card while this one still pinned it. Appended only when one is
        # actually held, so the ordinary reset does not grow a step that always
        # says "nothing was locked".
        if self._vf_lock_available() and self.read_vf_lock() is not None:
            steps.append(ResetStep(self.VF_LOCK_STEP, self.clear_vf_lock()))
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
    mv = d.get("vf_lock_mv")
    ck = d.get("clk_lock_mhz")
    out.append(f"  energy {d.get('energy_j','?')} J   "
               f"vf-locked domains {d.get('vf_locked_domains','?')}"
               + (f" @ {mv:.2f} mV requested" if mv else "")
               + (f"   clk-locked [{ck[0]}..{ck[1]}] MHz" if ck else ""))
    out.append(f"  vf lock self-test: {g.vf_lock_self_test()[1]}")
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
           "read_vf_lock", "read_clk_lock", "set_vf_lock", "clear_vf_lock",
           "vf_lock_self_test",
           "set_voltage_boost", "read_voltage_boost", "reset_all"):
    setattr(GPU, _m, _synchronized(getattr(GPU, _m)))
