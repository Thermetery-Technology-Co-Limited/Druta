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
PRIV_UNPOPULATED = "unpopulated"   # reads zero on a card that is demonstrably
#                                    running - not a slow clock, an empty slot

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
#
# EVERY NAME ABOVE WAS EARNED ON TU102 AND ONLY ON TU102, and the table is a
# map from DOMAIN NUMBER to name - which is precisely the thing that moves
# between architectures. Applied blind to GP102 it labels four dead rows:
# domains 0, 1, 2 and 21 all read 0.0 MHz there while the card runs 1898 MHz,
# so "GPC" and "XBAR" and "VIDEO" appear in CONFIRMED styling on empty slots
# while the real GPU clock sits unnamed in domain 15 at 3796 MHz (2x core -
# Pascal publishes GPC2CLK here, not GPC).
#
# That is the exact failure this block's own header warns about: a wrong name
# is worse than a bare index. So the table is no longer applied by domain
# number alone - classify_domain_names() below has to earn it against values
# the driver reports independently, on the card in front of us.


def classify_domain_names(rows, core_mhz=None, mem_nvml=None):
    """Name clock domains by CORRELATION against the driver's own figures.

    `rows` is mutated in place and returned.

    The only two names anybody can be sure of without a per-architecture map
    are the two the driver will tell us independently: whichever domain carries
    the GPU clock, and whichever carries the memory clock. Everything else is
    either a TU102 name that has to prove the card looks like TU102 first, or
    an index.

    Three rules:

    1. A domain matching the core clock at 1x is GPC; at 2x it is GPC2CLK, and
       it is named for what it actually holds rather than being silently halved
       - the core tile already shows the graphics clock.
    2. A domain reading zero while the card is demonstrably running is marked
       PRIV_UNPOPULATED and loses its name. An empty slot is not a slow clock.
    3. The TU102 table is applied only when this card presents the TU102
       signature - GPC correlating to domain 0. On anything else the extra
       names are not ours to hand out.

    If the correlation fails outright (no core figure to check against), the
    table is still applied so a working panel is never blanked by a failed
    probe, but every CONFIRMED drops to LIKELY: without ground truth the names
    are inherited assumptions, and should read as such."""
    def close(a, b, tol=0.005):
        return bool(b) and abs(a - b) <= max(0.5, abs(b) * tol)

    gpc_dom, gpc_scale, mem_dom = None, 1, None
    for r in rows:
        if r.get("kind") != PRIV_FREQ:
            continue
        p = r.get("prog_mhz") or 0.0
        if not p:
            continue
        if gpc_dom is None and core_mhz:
            if close(p, core_mhz):
                gpc_dom, gpc_scale = r["domain"], 1
            elif close(p, 2.0 * core_mhz):
                gpc_dom, gpc_scale = r["domain"], 2
        if mem_dom is None and close(p, mem_nvml):
            mem_dom = r["domain"]

    # Domain 0 carrying the GPU clock is the TU102 shape. GP102 puts it at 15.
    turing_like = (gpc_dom == 0)
    blind = (gpc_dom is None)

    # GP102's own earned name, gated on the GP102 signature exactly as the
    # TU102 table is gated on the TU102 one. Domain 16 was identified by a
    # 10-stop V/F-lock sweep: it holds 0.962-0.970 of GPC2CLK across the whole
    # top half of the curve, and a +60 MHz core offset moved it by twice the
    # core's move while changing that ratio by 0.0006 - so it is a 2x domain
    # riding the core clock.
    #
    # LIKELY, not CONFIRMED, and deliberately: what is measured is "a 2x domain
    # slaved to GPC at ~0.966". Calling that XBAR is an analogy with TU102,
    # where the domain in the same relationship (~0.95 of GPC) is XBAR. The
    # behaviour is established; the word is not.
    pascal_like = (gpc_dom == 15)
    PASCAL_NAMES = {16: ("XBAR2CLK", PRIV_LIKELY)}

    for r in rows:
        dom = r["domain"]
        if (r.get("kind") == PRIV_FREQ and core_mhz
                and not (r.get("prog_khz") or r.get("meas_khz"))):
            r["name"], r["grade"] = "", PRIV_UNPOPULATED
            continue
        if dom == gpc_dom:
            r["name"] = "GPC2CLK" if gpc_scale == 2 else "GPC"
            r["grade"] = PRIV_CONFIRMED
            r["scale"] = gpc_scale
        elif dom == mem_dom:
            r["name"], r["grade"] = "MEM", PRIV_CONFIRMED
        elif pascal_like and dom in PASCAL_NAMES:
            r["name"], r["grade"] = PASCAL_NAMES[dom]
        elif dom in PRIV_DOMAIN_ID and (turing_like or blind
                                        or r.get("kind") == PRIV_PCIE_GEN):
            name, grade, _kind = PRIV_DOMAIN_ID[dom]
            r["name"] = name
            r["grade"] = PRIV_LIKELY if (blind and grade == PRIV_CONFIRMED) \
                else grade
        else:
            r["name"], r["grade"] = "", PRIV_UNNAMED
    return rows


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
# and this card was found holding a 1137500 uV lock that the rail cannot deliver
# (it stops near 1093.75). Reading the getter answers "what was asked for", never
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


# VF structs: Turing's GPU points are CONTIGUOUS (the 80+23 split in older
# community layouts is a Pascal artefact). Total sizes must equal the original
# community structs (7208 / 9248 bytes) because the driver validates version
# = sizeof | ver<<16.
# 128, not 103. The mask is 4 u32 = 128 bits and the driver returns a point for
# every bit set; asking for 103 returned exactly 103 and looked like the whole
# table. It is not: all 128 bits yield 128 points spanning 450.00-1243.75 mV
# with a 2010 MHz peak, against the 450.00-1087.50 / 1965 MHz that 103 showed.
# The point the user could reach in Afterburner but not here - 1093.75 mV - is
# idx 103, the first one past the old window. Asking for 8 or 16 mask words is
# rejected, so the mask really is 4 words and `unk[12]` is something else.
VFP_POINTS = 128
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
# ^ TU102's grid, and now only the FALLBACK. It is not universal: GP102 steps
# ~12.657 MHz (the driver's own lockable table is 141 values from 139 to 1911,
# so 1772/140). Snapping a Pascal core offset to 15 MHz is itself the
# de-phasing the snap exists to prevent - measured, a "+60 MHz" request moved
# the core +51. GPU.clock_step_khz() derives it per card; every planner takes
# it as an argument so nothing silently reaches for this constant.

# THE SHAPE LAW. The delta table takes whatever you write - every delta reads
# back verbatim - but the curve the driver EVALUATES from it is not free-form.
# Measured on this card (see GPU.evaluate_curve_law for the two experiments and
# the 22 points they reproduce exactly), the evaluated curve always satisfies
#
#     0 <= f[i] - f[i-1] <= 45 MHz          (points in voltage order)
#
# and the driver repairs a violation by RAISING the lower of the pair. Both
# halves bite in practice and neither is visible in the delta table:
#   * the lower bound means a point written BELOW the one under it is silently
#     raised to it - a run of them becomes one flat, which is exactly the thing
#     a ramp exists to remove;
#   * the upper bound means a point written far ABOVE the one under it drags
#     that one up too, so an edit can move points OUTSIDE the range it wrote.
# 45000 is 3 * VF_STEP_KHZ, and the fit is exact rather than approximate.
#
# MEASURED ON TU102 ONLY, and the scaling to other cards is an ASSUMPTION we
# are making explicit rather than hiding: GPU.max_rise_khz() returns 3 * the
# card's grid step, i.e. it treats the law as "three clock bins" rather than
# "45 megahertz". Those are the same number on Turing and different everywhere
# else, and which one the hardware actually implements has not been tested.
# On a non-Turing card the reshape PREDICTION is therefore unverified - it
# affects what the plan banner promises, not whether a write is safe.
VF_MAX_RISE_KHZ = 45000
VF_MAX_RISE_BINS = 3


class _VfpEntry(ctypes.Structure):
    _fields_ = [("u0", u32), ("freq_kHz", u32), ("volt_uV", u32),
                ("u3", u32), ("u4", u32), ("u5", u32), ("u6", u32)]


class _VfpCurve(ctypes.Structure):
    _fields_ = [("version", u32), ("masks", u32 * 4), ("unk", u32 * 12),
                # tail shrinks as `entries` grows: the driver validates the
                # struct SIZE through the version word, so 7208 is fixed and
                # only the split between entries and tail may move.
                ("entries", _VfpEntry * VFP_POINTS), ("tail", u32 * 889)]


class _BoostRow(ctypes.Structure):
    _fields_ = [("w", i32 * 9)]   # w[5] = freqDelta_kHz


class _BoostTable(ctypes.Structure):
    _fields_ = [("version", u32), ("masks", u32 * 4), ("unk", u32 * 12),
                ("rows", _BoostRow * VFP_POINTS), ("tail", u32 * 1143)]


assert ctypes.sizeof(_VfpCurve) == 7208
assert ctypes.sizeof(_BoostTable) == 9248


def below_cap(volt_mv, cap_mv):
    """Single definition of 'at or below the voltage cap' (shared by the planner
    and every readout, so the number in a dialog is the number that was planned)."""
    return volt_mv <= cap_mv + 0.01


def above_floor(volt_mv, lo_mv):
    """The other end of a band, and it resolves the OPPOSITE way to below_cap:
    the cap is the highest point AT OR BELOW the number, the floor is the lowest
    point AT OR ABOVE it. Both round the band INWARDS, so a bound can never
    quietly acquire a point on the far side of the value that was typed - and
    for the floor that is a safety property, not a nicety: everything below it
    is left alone, and the points just under a ramp's floor are the idle rungs
    pinned at minimum clock (see compute_ramp)."""
    return volt_mv >= lo_mv - 0.01


def _set_point_masks(obj, nbits=VFP_POINTS):
    """Ask for exactly `nbits` points.

    The mask is 4 u32 = 128 bits and the driver returns one entry per bit set -
    but only up to the number of entries THIS card's table actually has. Ask for
    one more than that and the whole call fails, with NVAPI -1 (the GENERIC
    error) rather than -9 INCOMPATIBLE_STRUCT_VERSION, so it reads like the call
    is unsupported instead of like the request being too wide.

    That is exactly how a Turing-shaped request made a Pascal card look as
    though it could not read its own curve, while Afterburner read it fine.

    Measured: TU102 accepts 128. GP102 accepts 84 and refuses 85.

    Nothing here caps at a hardcoded per-architecture number - GPU.vfp_layout()
    probes for it, because a fixed constant is what was wrong both times (103
    on Turing, then 128 on Pascal)."""
    nbits = max(0, min(int(nbits), VFP_POINTS))
    for i in range(4):
        lo = i * 32
        if nbits >= lo + 32:
            obj.masks[i] = 0xFFFFFFFF
        elif nbits > lo:
            obj.masks[i] = (1 << (nbits - lo)) - 1
        else:
            obj.masks[i] = 0


class VfpLayout:
    """What this card's VF table actually IS, probed rather than assumed.

    Every field here was once a hardcoded Turing constant, and every one of them
    was wrong on Pascal in a different way:

        n_entries   TU102 128          GP102 84
        gpu_idx     all of them        the first 80
        other_idx   none               80..83, the MEMORY points - their
                                       frequencies are the driver's own
                                       mem_clocks list, and they are the reason
                                       the table does not end where the GPU
                                       points do
        freq_div    1                  2 - the GPU rows carry GPC2CLK, the 2x
                                       clock, so the graphics MHz is half

    A card is classified from what the driver answers, never from its device id:
    a PCI table needs a new row per SKU forever and would not have caught either
    mistake. These three checks would have caught both."""

    __slots__ = ("n_entries", "gpu_idx", "other_idx", "freq_div", "notes")

    def __init__(self, n_entries, gpu_idx, other_idx, freq_div, notes):
        self.n_entries = n_entries
        self.gpu_idx = gpu_idx
        self.other_idx = other_idx
        self.freq_div = freq_div
        self.notes = notes

    @property
    def n_gpu(self):
        return len(self.gpu_idx)

    def __repr__(self):
        return (f"VfpLayout({self.n_entries} entries, {self.n_gpu} GPU, "
                f"{len(self.other_idx)} other, freq/{self.freq_div})")


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
            # core/mem are already in `d` from _read_clocks above, and they come
            # from the PUBLIC clock getter whose domain ids are arch-stable.
            # Handing them over is what lets the domain names be earned against
            # this card instead of inherited from TU102.
            d["clk_domains"], d["clk_domains_err"] = self.read_clock_domains(
                pc, core_mhz=d.get("core"), mem_nvml=d.get("mem"))
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

    def read_clock_domains(self, pc=None, core_mhz=None, mem_nvml=None):
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
            # `kind` is structural and stays table-driven: domain 31 is a link
            # generation rather than a frequency on every card we have seen,
            # and rendering it as MHz would be a units error, not a naming one.
            # The NAME and GRADE are decided afterwards, by correlation.
            _n, _g, kind = PRIV_DOMAIN_ID.get(
                dom, ("", PRIV_UNNAMED, PRIV_FREQ))
            row = {"domain": dom, "name": "", "grade": PRIV_UNNAMED,
                   "kind": kind, "prog_khz": prog, "meas_khz": meas,
                   "flags": flags, "srcid": srcid, "scale": 1,
                   "prog_mhz": None, "meas_mhz": None, "delta_mhz": None}
            if kind == PRIV_FREQ:
                row["prog_mhz"] = prog / 1000.0
                row["meas_mhz"] = meas / 1000.0
                if prog and meas:
                    row["delta_mhz"] = (meas - prog) / 1000.0
            rows.append(row)
        classify_domain_names(rows, core_mhz, mem_nvml)
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

    def clock_step_khz(self):
        """This card's core-clock grid in kHz, derived from the driver.

        The lockable-clock table IS the enumeration of legal core clocks, so
        the step is just its span divided by its gaps - no per-architecture
        constant, and it would have caught this the first time. Checked:
            TU102   360..2160 over 121 entries -> 1800/120 = 15.000 MHz
            GP102   139..1911 over 141 entries -> 1772/140 = 12.657 MHz

        Span-over-gaps rather than the median difference on purpose: GP102's
        consecutive differences alternate 12 and 13 because the true step is
        not an integer, so a median returns 13 and accumulates error across the
        table. The endpoints do not.

        Falls back to VF_STEP_KHZ when the table is too short to measure or the
        answer is implausible - a wrong step is worse than a stale one, because
        every planner multiplies it."""
        cached = getattr(self, "_clock_step_cache", None)
        if cached:
            return cached
        step = None
        try:
            table = self.lockable_clocks_by_mem() or []
            best = max((cl for _mem, cl in table), key=len, default=[])
            if len(best) >= 8:
                span = max(best) - min(best)
                if span > 0:
                    step = int(round(span * 1000.0 / (len(best) - 1)))
        except Exception:                                       # noqa: BLE001
            step = None
        # 5-30 MHz brackets every NVIDIA grid we know of; outside it, something
        # about the table is not what we think and the constant is safer.
        if not step or not (5000 <= step <= 30000):
            step, measured = VF_STEP_KHZ, False
        else:
            measured = True
        self._clock_step_cache = step
        self._clock_step_measured = measured
        return step

    def max_rise_khz(self):
        """The shape law's upper bound for this card - see VF_MAX_RISE_KHZ.

        Three grid bins. On Turing that reproduces the measured 45 MHz exactly;
        elsewhere the multiplier is inherited, not measured."""
        return VF_MAX_RISE_BINS * self.clock_step_khz()

    def step_is_measured(self):
        """Did the grid come from the card, or is it the fallback constant?

        The UI says which, because a planner quoting 15 MHz bins on a card whose
        bins are 12.657 is exactly the failure this change is about - and a
        card that legitimately measures 15.000 must not be reported as a
        fallback, so this is a separate flag rather than a comparison against
        VF_STEP_KHZ."""
        self.clock_step_khz()
        return bool(getattr(self, "_clock_step_measured", False))

    def set_clock_offset(self, ctype, mhz):
        """ctype 0=GRAPHICS (mhz in MHz, snapped to THIS CARD's clock grid),
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
            # THIS CARD's grid, not 15 MHz. On GP102 the grid is 12.657, so
            # the old snap rounded a request onto a lattice the hardware does
            # not have - measured, "+60 MHz" moved the core +51.
            #
            # And the value is rounded UP to the next whole MHz, which is not
            # tidiness. MEASURED on GP102, three requests, all consistent: the
            # driver FLOORS the offset to a whole number of bins. Rounding the
            # snapped value to nearest put it at 4.98 / 1.98 / 7.98 bins - just
            # under each boundary - so every request came back one bin short
            # (asked 60, sent 63, moved 51). Ceiling lands just above the
            # boundary instead, so the floor divides to the bin we intended.
            #
            # On an integer grid (TU102's 15) the ceiling is a no-op, so this
            # changes nothing there.
            step_mhz = self.clock_step_khz() / 1000.0
            want = round(mhz / step_mhz) * step_mhz
            mhz = int(want) + (1 if want > int(want) else 0)
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
        lay = self.vfp_layout()
        if lay is None:
            return None, "could not determine this card's VF table layout"
        cv = _VfpCurve(version=a.ver(_VfpCurve, 1))
        _set_point_masks(cv, lay.n_entries)
        st = a.VfpCurve(a.gpu, ctypes.byref(cv))
        if st != 0:
            return None, f"curve read failed (status {st})"
        bt = _BoostTable(version=a.ver(_BoostTable, 1))
        _set_point_masks(bt, lay.n_entries)
        st = a.BoostTableGet(a.gpu, ctypes.byref(bt))
        if st != 0:
            return None, f"delta-table read failed (status {st})"
        # GPU rows only. The trailing rows on Pascal are the MEMORY points, and
        # handing those to the curve editor would put a 5705 "MHz" dot at
        # 756.25 mV and let a de-flatten write a delta to a memory V/F point.
        points = []
        for i in lay.gpu_idx:
            e = cv.entries[i]
            if e.freq_kHz == 0:
                continue
            points.append({"idx": i,
                           "volt_mv": e.volt_uV / 1000.0,
                           "freq_mhz": e.freq_kHz / (1000.0 * lay.freq_div),
                           "delta_khz": bt.rows[i].w[5]})
        if not points:
            return None, "curve read returned no points"
        return points, None

    def read_vfp_other_rows(self):
        """The non-GPU rows of the VF table, or [] - on Pascal these are the
        four MEMORY V/F points, which is what makes that table 84 entries long
        rather than 80. Kept separate from read_vf_curve() on purpose: they are
        real data, but they are not points the curve editor may touch."""
        a = self.nvapi
        lay = self.vfp_layout()
        if lay is None or not lay.other_idx or not (a.ok and a.VfpCurve):
            return []
        cv = _VfpCurve(version=a.ver(_VfpCurve, 1))
        _set_point_masks(cv, lay.n_entries)
        if a.VfpCurve(a.gpu, ctypes.byref(cv)) != 0:
            return []
        mem = set(self.static.get("mem_clocks") or ())
        out = []
        for i in lay.other_idx:
            e = cv.entries[i]
            raw = e.freq_kHz / 1000.0
            out.append({"idx": i, "volt_mv": e.volt_uV / 1000.0,
                        "value": raw,
                        "kind": "memory" if round(raw) in mem else "unknown"})
        return out

    # ---- layout probing --------------------------------------------------- #
    def _probe_vfp_entry_count(self):
        """How many entries will this driver return for this GPU?

        Acceptance is monotonic - every width at or below the table's size is
        accepted and every width above it fails - so this binary-searches in
        about seven calls instead of walking 128. All reads."""
        a = self.nvapi

        def accepted(n):
            cv = _VfpCurve(version=a.ver(_VfpCurve, 1))
            _set_point_masks(cv, n)
            return a.VfpCurve(a.gpu, ctypes.byref(cv)) == 0

        if accepted(VFP_POINTS):
            return VFP_POINTS
        lo, hi = 0, VFP_POINTS          # accepted(lo), not accepted(hi)
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if accepted(mid):
                lo = mid
            else:
                hi = mid
        return lo

    def vfp_layout(self, force=False):
        """Probe this card's VF table shape. Cached; pass force=True to redo.

        Three checks, each against something the driver itself reports, because
        a hardcoded constant has now been wrong twice (103 on Turing, then the
        corrected 128 on Pascal):

        1. HOW MANY ENTRIES - widen the mask until the call fails.
        2. WHICH ARE GPU POINTS - the GPU block is the leading run of
           non-decreasing voltage. On GP102 idx 79 is 1243.75 mV and idx 80 is
           550.00, and that collapse is the boundary. Matching frequencies
           against mem_clocks instead would misfire the moment a GPU point
           happens to sit at 405 or 810 MHz, which is entirely possible.
        3. WHAT SCALE THE FREQUENCY IS IN - compare the top GPU row against the
           driver's own gfx_max. GP102 reports GPC2CLK, twice the graphics
           clock: raw, 60 of its 80 points claim to be above the card's maximum,
           which cannot be true. Halved, none are.

        Returns None if the curve APIs are unavailable or nothing answers."""
        cached = getattr(self, "_vfp_layout_cache", None)
        if cached is not None and not force:
            return cached
        a = self.nvapi
        if not (a.ok and a.VfpCurve):
            return None
        n = self._probe_vfp_entry_count()
        if not n:
            return None
        cv = _VfpCurve(version=a.ver(_VfpCurve, 1))
        _set_point_masks(cv, n)
        if a.VfpCurve(a.gpu, ctypes.byref(cv)) != 0:
            return None

        rows = [(i, cv.entries[i].volt_uV / 1000.0, cv.entries[i].freq_kHz)
                for i in range(n) if cv.entries[i].freq_kHz]
        gpu_idx, other_idx, prev, broken = [], [], None, False
        for i, mv, _f in rows:
            if not broken and (prev is None or mv >= prev - 0.01):
                gpu_idx.append(i)
                prev = mv
            else:
                broken = True
                other_idx.append(i)

        freq_div, gfx_max = 1, self.static.get("gfx_max")
        gset = set(gpu_idx)
        top_raw = max((f for i, _mv, f in rows if i in gset), default=0) / 1000.0
        if gfx_max and top_raw > gfx_max * 1.5:
            freq_div = 2

        mem = set(self.static.get("mem_clocks") or ())
        n_mem = sum(1 for i, _mv, f in rows
                    if i in set(other_idx) and round(f / 1000.0) in mem)
        notes = (f"{n} entries; {len(gpu_idx)} GPU points"
                 + (f"; {len(other_idx)} trailing rows"
                    f" ({n_mem} match mem_clocks)" if other_idx else "")
                 + f"; GPU frequency is {'GPC2CLK (halved)' if freq_div == 2 else 'direct'}"
                 + (f"; top raw {top_raw:.0f} vs gfx_max {gfx_max}"
                    if gfx_max else ""))
        lay = VfpLayout(n, gpu_idx, other_idx, freq_div, notes)
        self._vfp_layout_cache = lay
        return lay

    @staticmethod
    def peak_info(points, cap_mv=None):
        """(peak_mhz, park_idx, park_mv, n_at_peak) for the REACHABLE curve.

        FLATTENING-AWARE: when several voltages carry the peak frequency the
        arbiter runs the LOWEST of them, so the park point is the bottom of
        that flat run and not the top of the curve. `n_at_peak` is the run's
        length, which is the number de-flatten exists to drive to 1 - at 1 the
        peak is already unique and there is nothing to do.

        CAP-AWARE, and it has to be: the table on this card runs to 1243.75 mV
        while the rail stops at the VBIOS cap near 1.093 V, so every point
        above the cap describes a frequency the card can never request.
        Uncapped, this reported a 2010 MHz peak parked at 1175.00 mV while the
        card was demonstrably sitting at 1050.00 mV / 1965 MHz. That is not a
        rounding error, it is the wrong operating point - so pass the same cap
        the planner uses. Omitting cap_mv keeps the old whole-table behaviour,
        which is only correct when the caller has already filtered.

        (The bug was invisible until VFP_POINTS went 103 -> 128: the truncated
        read stopped below the cap, so an uncapped max happened to be right.)"""
        if not points:
            return 0.0, None, 0.0, 0
        usable = ([p for p in points if below_cap(p["volt_mv"], cap_mv)]
                  if cap_mv is not None else list(points))
        if not usable:
            return 0.0, None, 0.0, 0
        peak = max(p["freq_mhz"] for p in usable)
        # sort explicitly rather than trusting index order to be voltage order
        at = sorted((p for p in usable if p["freq_mhz"] == peak),
                    key=lambda p: p["volt_mv"])
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
        1830 MHz, idx 80 being the lowest member of its own flat.

        A request above the whole curve clamps to the top point - but note that
        "the whole curve" moved when VFP_POINTS went 103 -> 128. The 1137.50 mV
        lock this card was found holding was read as a clamp to the top of the
        103-point window; it is not one, because the real table runs to 1243.75
        and 1137.50 is a point in it (idx 110). Any resolution recorded against
        a 1087.50 mV ceiling predates that fix.

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

    # 0, not 1. Aiming one point PAST the cap was a sensible margin while the
    # table appeared to end at 1087.50 - the cap was then an approximation of
    # the ceiling. With the full 128-point table the cap snaps to a real grid
    # point and IS the reachable ceiling, so +1 lands above the rail: it makes
    # unique a point the card cannot reach and leaves the flat run that
    # actually pins it untouched. Measured on the stock curve at cap 1093.75:
    # extra=0 moves the park 96@1050.00 -> 103@1093.75 (run 8 -> 1); extra=1
    # and extra=2 leave it at 96@1050.00 with the run still 8 deep.
    EXTRA_POINTS_ABOVE_CAP = 0

    @staticmethod
    def compute_deflatten(points, vcap_mv, max_khz=None, extra_points_above=None,
                          step_khz=None):
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

        CAVEAT on "left untouched", and it applies to every planner in this
        file: that is a statement about the DELTA TABLE, which is the only thing
        written. The curve the driver EVALUATES is reshaped afterwards
        (VF_MAX_RISE_KHZ, evaluate_curve_law) and a point raised here can drag
        the points below it up with it, 45 MHz at a time, without a delta being
        written to any of them. De-flatten's +1 bin is far too small for that to
        bite - a 15 MHz raise can never open a 45 MHz gap that was not already
        there - but the claim is only safe because the move is small, not
        because writing no delta means writing no change. compute_hard_deflatten
        is where the same rule turns into a 16-point cascade.
        Returns (changes, ceil_before_mhz, ceil_after_mhz, meta)."""
        if extra_points_above is None:
            extra_points_above = GPU.EXTRA_POINTS_ABOVE_CAP
        step = int(step_khz or VF_STEP_KHZ)
        n = len(points)
        khz = [int(round(p["freq_mhz"] * 1000)) for p in points]
        below = [i for i in range(n) if below_cap(points[i]["volt_mv"], vcap_mv)]
        if n == 0 or not below:
            return [], 0.0, 0.0, {"clamped": False, "boundary_idx": None,
                                  "unique": False}
        B = min(max(below) + max(0, extra_points_above), n - 1)
        ceil_before = khz[max(below)]
        peak_below = max(khz[:B]) if B > 0 else -1
        target = max(khz[B], peak_below + step)
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

        # RAISING IS NOT THE ONLY WAY TO MAKE THE BOUNDARY UNIQUE, and on some
        # cards it is not an available way at all. When the cap point already
        # holds the hardware maximum - GP102 stock peaks at 1911 = gfx_max, with
        # the point below it at 1911 too - there is no headroom above to raise
        # into, and this used to give up with "a point below it already holds
        # the hardware max clock".
        #
        # But the arbiter rule only cares that the boundary is the LOWEST
        # voltage carrying the peak. Lowering whatever shadows it achieves that
        # exactly as well as raising it would, and costs no clock at the point
        # the card actually parks on - the shadowing points are ones the card
        # could never occupy anyway, because they held the same frequency at a
        # higher voltage.
        #
        # Walk DOWN from the boundary giving each shadowing point one bin less
        # than the one above it, and stop at the first point already low enough.
        # A stock curve is non-decreasing in voltage order, so everything below
        # that point is below it too - one pass is sufficient.
        # Only the points that actually SHADOW the boundary, and no further. A
        # naive descent that steps down a bin per point never terminates early
        # on this hardware, because the stock curve descends at almost exactly
        # one bin per point too - it cost 24 points reaching down to 850 mV to
        # fix a two-point flat. A point is done as soon as it sits below the one
        # above it; everything under that is below it too.
        lowered = 0
        if target <= peak_below and B > 0:
            above_val = target
            for i in range(B - 1, -1, -1):
                if khz[i] < above_val:
                    break
                want = above_val - step
                if want <= 0:
                    break
                new[i] = want
                above_val = want
                lowered += 1

        changes = [(points[i]["idx"], points[i]["volt_mv"], khz[i] / 1000.0,
                    new[i] / 1000.0,
                    points[i]["delta_khz"] + (new[i] - khz[i]))
                   for i in sorted(new)]
        return (changes, ceil_before / 1000.0, target / 1000.0,
                {"clamped": clamped, "boundary_idx": points[B]["idx"],
                 "unique": target > peak_below or lowered > 0,
                 "lowered": lowered,
                 "lowered_by_mhz": (lowered * step) / 1000.0})

    @staticmethod
    def evaluate_curve_law(khz, max_rise_khz=None):
        """Given a whole curve's frequencies in kHz, IN VOLTAGE ORDER, return
        what the driver will actually evaluate from it. See VF_MAX_RISE_KHZ.

        `max_rise_khz` defaults to the TU102-measured 45 MHz. Callers with a GPU
        should pass GPU.max_rise_khz() so the bound follows the card's grid -
        the 45 MHz below is three TU102 bins, and on a card whose bins are
        12.657 MHz the two readings of the law disagree.

        The delta table is not the curve. Deltas read back exactly as written -
        verified, 14 rows, zero mismatches - while the frequencies attached to
        those points come back reshaped, and nothing in the write path reports
        it. So the planner predicts the reshape instead of being surprised by it.

        Two passes, and one of each is enough: the forward pass can only raise a
        point to its left neighbour (which cannot break the backward rule for the
        pair it just fixed), and the backward pass only ever raises a point
        towards its right neighbour, which leaves it still at or above its own
        left neighbour.

        MEASURED, both experiments on the stock curve of this card, and this
        function reproduces every point of both:

          A. idx 60 (825.00 mV, 1605) written +150 -> 1755. Read back, the four
             points BELOW it had moved with it - 1575 / 1620 / 1665 / 1710 at idx
             56-59, each exactly 45 MHz under the next, stopping the moment idx
             55's untouched 1530 was within 45 of idx 56. Points above collapsed
             onto 1755 up to idx 72, which already held it.
          B. the bottom 15 rungs of an 800 mV ramp written for real (1425..1635
             at idx 56-70, against an untouched idx 55 at 1530). Eight rungs -
             everything asked for below 1530 - came back AS 1530: one flat run
             where eight distinct operating points had been planned. At the other
             end idx 69/70 were pulled UP to 1650/1695 by idx 71's untouched
             1740, the same 45 MHz rule from the other side."""
        rise = int(max_rise_khz or VF_MAX_RISE_KHZ)
        out = [int(v) for v in khz]
        for i in range(1, len(out)):          # non-decreasing
            if out[i] < out[i - 1]:
                out[i] = out[i - 1]
        for i in range(len(out) - 2, -1, -1):  # at most `rise` per point
            if out[i] < out[i + 1] - rise:
                out[i] = out[i + 1] - rise
        return out

    @staticmethod
    def predict_curve(points, khz, new, max_rise_khz=None):
        """(real, pos) for a staged plan: the frequencies the driver will
        actually evaluate (evaluate_curve_law), and an idx -> voltage-position
        map to read them with. `new` is idx -> planned kHz for the points the
        plan moves; everything else keeps `khz`.

        One definition, because every planner in this file needs the same
        answer and a second copy would drift from the measurements."""
        order = sorted(range(len(points)), key=lambda i: points[i]["volt_mv"])
        pos = {i: k for k, i in enumerate(order)}
        return GPU.evaluate_curve_law([new.get(i, khz[i]) for i in order],
                                      max_rise_khz), pos

    @staticmethod
    def cascade_meta(points, khz, real, pos, idxs):
        """How far the shape law reaches into points the plan did NOT write:
        how many move, the lowest voltage that moves, and the worst rise. This
        is the difference between "we wrote no delta there" and "the card is not
        asked for more clock there", and only the second one is a safety claim."""
        lifted = [i for i in idxs if real[pos[i]] != khz[i]]
        return {"lifted_below": len(lifted),
                "lift_max_mhz": (max(real[pos[i]] - khz[i] for i in lifted)
                                 / 1000.0 if lifted else 0.0),
                "lift_lowest_mv": (min(points[i]["volt_mv"] for i in lifted)
                                   if lifted else None)}

    # The floor of HARD DE-FLATTEN. A hardware-modification number, not a taste:
    # with `refin_adj` deactivated on the PCB and the core rail driven by an
    # external mod, the GPU still BELIEVES it is at 800 mV and computes its power
    # from that belief, so it stops throttling - while the real rail, now
    # unreadable to any GPU software including this app, is driven higher from
    # outside. Without that mod the card really is at 800 mV, the flat top
    # demands clocks it cannot hold, and it crashes. Every caller has to say so.
    HARD_FLOOR_MV = 800.00

    @staticmethod
    def compute_hard_deflatten(points, floor_mv, target_khz, step_khz=None):
        """Set EVERY point at or above `floor_mv` to ONE frequency - deliberately
        make the curve completely flat above the floor - so the boost arbiter
        parks AT the floor. Returns (changes, floor_before_mhz, target_mhz, meta),
        the same shape as compute_deflatten and compute_ramp.

        THIS IS THE OPPOSITE OF compute_ramp, and on purpose. The ramp removes
        flats so a throttling card has fine-grained operating points to descend
        through. This one builds the largest flat it can, because the arbiter
        runs the LOWEST voltage of any peak-frequency flat run: make 72 points
        share one frequency and the card requests that frequency at the bottom
        of the run. The ramp is for throttling that is going to happen anyway;
        this is for throttling that should not happen at all.

        WHAT IT IS ACTUALLY FOR: DECEIVING THE POWER ESTIMATOR. The GPU believes
        it is running at `floor_mv` - 800.00 by default - and computes its power
        from that belief, so it stops throttling. The real rail is driven
        externally by a hard mod and is invisible to all GPU software, this app
        included. The target must be high enough to keep the card in P0, which is
        why it defaults to the curve's own peak rather than to a round number.

        IT REQUIRES THE MOD, and the caller must gate on an explicit
        acknowledgement, not a tooltip. Without a functional external voltage mod
        - `refin_adj`, or the equivalent circuit on that board, rendered
        completely nonoperational - the card really is at 800 mV, the flat top
        demands clocks it cannot hold there, the cascade below demands high
        clocks all the way down to 700 mV, and the driver crashes.

        THE CASCADE IS THE THING TO SHOW. The shape law (VF_MAX_RISE_KHZ) lets no
        two neighbouring points differ by more than 45 MHz, and repairs a
        violation by RAISING the lower one. A flat top at 2010 from 800.00 mV
        therefore drags 16 points BELOW the floor up with it - idx 40 (700.00 mV)
        through idx 55 (793.75 mV), worst case 1530 -> 1965 MHz at a nominal
        793.75 mV - without a delta being written to any of them. "Points below
        the floor are left untouched" is true of what this app WRITES and false
        of what the driver EVALUATES, so meta carries the predicted cascade
        (`lifted_below`, `lift_lowest_mv`, `lift_max_mhz`) and the caller shows
        it before the click.

        The park point in meta is derived from the PREDICTED curve, not from the
        plan: if the target is low enough that untouched points below the floor
        still hold it, the arbiter parks on one of those instead and the whole
        exercise misses. `parks_at_floor` says which happened."""
        n = len(points)
        khz = [int(round(p["freq_mhz"] * 1000)) for p in points]
        # DOWN onto the 15 MHz grid, like every other frequency in this app: a
        # mid-bin target floors on evaluation and the "one frequency" the whole
        # mechanism depends on would silently become two.
        grid = int(step_khz or VF_STEP_KHZ)
        target = max(0, int(target_khz) // grid) * grid
        band = sorted((i for i in range(n)
                       if above_floor(points[i]["volt_mv"], floor_mv)),
                      key=lambda i: points[i]["volt_mv"])
        meta = {"floor_idx": None, "floor_mv": None, "target_mhz": target / 1000.0,
                "n_flat": 0, "park_idx": None, "park_mv": None,
                "park_mhz": 0.0, "parks_at_floor": False,
                "lifted_below": 0, "lift_max_mhz": 0.0, "lift_lowest_mv": None}
        if not band:
            return [], 0.0, 0.0, meta
        F = band[0]
        new = {i: target for i in band if khz[i] != target}
        real, pos = GPU.predict_curve(points, khz, new,
                                      VF_MAX_RISE_BINS * grid)
        # the arbiter's rule, applied to the curve the DRIVER will have: highest
        # frequency anywhere, then the lowest voltage carrying it
        peak = max(real)
        park = min((i for i in range(n) if real[pos[i]] == peak),
                   key=lambda i: points[i]["volt_mv"])
        under = [i for i in range(n)
                 if points[i]["volt_mv"] < points[F]["volt_mv"] - 0.01]
        meta.update({
            "floor_idx": points[F]["idx"], "floor_mv": points[F]["volt_mv"],
            "target_mhz": target / 1000.0, "n_flat": len(band),
            "park_idx": points[park]["idx"], "park_mv": points[park]["volt_mv"],
            "park_mhz": peak / 1000.0,
            "parks_at_floor": park == F,
        })
        meta.update(GPU.cascade_meta(points, khz, real, pos, under))
        changes = [(points[i]["idx"], points[i]["volt_mv"], khz[i] / 1000.0,
                    new[i] / 1000.0,
                    points[i]["delta_khz"] + (new[i] - khz[i]))
                   for i in sorted(new)]
        return changes, khz[F] / 1000.0, target / 1000.0, meta

    @staticmethod
    def compute_ramp(points, lo_mv, cap_mv, max_khz=None, step_khz=None):
        """Rebuild the band [lo_mv, cap_mv] as a STRICTLY INCREASING ramp on the
        15 MHz grid - one distinct frequency per voltage point, no ties anywhere
        in the band. Returns (changes, ceil_before_mhz, ceil_after_mhz, meta),
        the same shape as compute_deflatten.

        WHY THIS EXISTS, and it is not de-flatten's reason. De-flatten makes ONE
        point unique (the boundary) and levels everything above it. That fixes
        the steady-state park point and nothing else. A power- or thermally
        throttling card does not sit at the park point: it walks LEFT through the
        V/F curve until it is under budget, and from there the GRANULARITY of the
        available operating points decides the performance. The arbiter can only
        occupy, for each distinct frequency, the LOWEST voltage carrying it, so a
        flat run is a voltage band the card cannot sit in at all.

        Measured on this card's stock curve, the usable operating points:

            below 1050 mV: uniform 12.50 mV / 15 MHz steps
            1175.00/2010 -> 1137.50/1995   drops 37.50 mV in one step
            1137.50/1995 -> 1106.25/1980   drops 31.25 mV
            1106.25/1980 -> 1050.00/1965   drops 56.25 mV

        Between 1050 and 1175 mV there are 21 voltage points and only 4 are
        usable; 17 are shadowed. Power goes roughly as f*V^2, so shedding 56 mV
        to give up 15 MHz dumps far more power than the budget ever asked for and
        the card undershoots badly - up to 7% of a benchmark, measured, with an
        imperfect power-limit bypass (shunt mods, where the GPU's own
        current-sensing heuristics still throttle).

        ANCHOR AT THE TOP AND DESCEND. The cap point takes the highest allowed
        frequency and every point below it is exactly one 15 MHz bin lower, down
        to the floor. The alternative - ascend from the floor - CLIPS: from
        800 mV the unclipped top is 2250 MHz against this card's 2130 max, so the
        top eight points get clipped onto 2130 and a nine-point flat run reappears
        exactly where it hurts most. Descending cannot clip, by construction.

        The ceiling itself is min(max_khz, the unclipped ascending top), which is
        what keeps a low cap honest: anchoring unconditionally at the hardware
        max would demand 2130 MHz at whatever voltage the cap happens to name.

        WHAT IT COSTS. For the regular band the price is zero: descending 15
        rungs from 2130 lands on exactly 1905 at 1000.00 mV, which is what stock
        already has there. For a 48-point band from 800 mV the clip costs 120 MHz
        at the floor (1425 against stock's 1545) - that is the honest price of
        monotonicity over a wide span, so meta carries it and every caller
        reports it rather than hiding it.

        AND WHAT THE DRIVER THEN DOES TO IT. The delta table takes the plan
        verbatim; the evaluated curve does not (VF_MAX_RISE_KHZ,
        evaluate_curve_law). A clipped floor lands below the untouched point
        under the band and the driver raises those rungs onto it - measured, the
        800 mV band's bottom eight rungs all come back as the 1530 MHz of the
        point below, one flat run where eight operating points were planned. So
        meta reports `delivered`, the number of DISTINCT operating points the
        band will really have, next to `rungs`, the number that were asked for;
        the first is the number this feature is actually judged on. A band can
        never deliver more than (top - the point below it)/15 + 1 rungs, however
        many points it spans. `lifted_below` is the other half of the same law:
        a floor placed more than 45 MHz above the point beneath it drags that
        point up, so "nothing below the floor is touched" is a promise about the
        delta table and this is the promise about the rail.

        Points BELOW lo_mv are left untouched, for the reason compute_deflatten
        gives: the low-voltage floor is many points pinned at the minimum clock,
        and ramping them means demanding high clocks at tiny voltages.

        Points ABOVE the cap are levelled onto the top rung. They are unreachable
        on this card (the rail stops near 1.093 V), and levelling keeps the cap
        point the LOWEST-voltage member of the top flat, which is where the
        arbiter then parks - the same trick de-flatten ends on.

        The granularity and the overclock are the SAME edit: every rung demands
        more clock at its voltage than stock did, so every rung has to be
        stable in its own right."""
        grid = int(step_khz or VF_STEP_KHZ)
        n = len(points)
        khz = [int(round(p["freq_mhz"] * 1000)) for p in points]
        # sorted by VOLTAGE, not by position: the descent assigns one bin per
        # step down the band, so it has to walk the band in the order the rail
        # does. peak_info() declines to trust index order for the same reason.
        band = sorted((i for i in range(n)
                       if above_floor(points[i]["volt_mv"], lo_mv)
                       and below_cap(points[i]["volt_mv"], cap_mv)),
                      key=lambda i: points[i]["volt_mv"])
        above = [i for i in range(n)
                 if not below_cap(points[i]["volt_mv"], cap_mv)]
        meta = {"clamped": False, "boundary_idx": None, "unique": False,
                "lo_idx": None, "cap_idx": None, "lo_mv": None, "cap_mv": None,
                "rungs": 0, "top_mhz": 0.0, "floor_before_mhz": 0.0,
                "floor_after_mhz": 0.0, "floor_cost_mhz": 0.0,
                "leveled_above": 0, "under_band_mhz": None,
                "shadowed": 0, "delivered": 0, "lifted_below": 0,
                "lift_max_mhz": 0.0, "lift_lowest_mv": None,
                "dropped_rungs": 0, "dropped_reason": ""}
        if not band:
            return [], 0.0, 0.0, meta
        L, B = band[0], band[-1]
        rungs = len(band)
        # the ascending top is what the band would reach if the floor kept its
        # current frequency and every point above it gained one bin
        asc_top = khz[L] + (rungs - 1) * grid
        top = asc_top
        if max_khz is not None and top > max_khz:
            top, meta["clamped"] = int(max_khz), True

        # SHRINK THE BAND TO WHAT THE HEADROOM ACTUALLY ALLOWS.
        #
        # A clipped ramp keeps its rung count and slides the whole descent down,
        # which puts the bottom rungs UNDER the untouched point below the band -
        # and the shape law's non-decreasing pass then raises them all back onto
        # that point as ONE FLAT. That is the exact pathology a ramp exists to
        # remove, so emitting a plan that causes it is worse than emitting a
        # smaller plan. Measured on GP102: a 10-rung band from 1000 mV clipped
        # at gfx_max delivered 8 distinct frequencies, with idx 55/56/57 all
        # collapsed onto the neighbour's 1822.5.
        #
        # So drop rungs from the BOTTOM until the floor clears the point below
        # it. The dropped points keep their stock values, which are already
        # increasing - leaving them alone beats flattening them. Shrinking from
        # the bottom rather than the top because the top is the end that is
        # pinned: the cap point has to stay the highest, or the arbiter parks
        # somewhere else entirely.
        dropped = 0
        while len(band) > 1:
            below_band = [i for i in range(n)
                          if points[i]["volt_mv"] < points[band[0]]["volt_mv"] - 0.01]
            if not below_band:
                break
            un = max(below_band, key=lambda i: points[i]["volt_mv"])
            if top - (len(band) - 1) * grid >= khz[un]:
                break
            band = band[1:]
            dropped += 1
        if dropped:
            L, rungs = band[0], len(band)
            meta["dropped_rungs"] = dropped
            meta["dropped_reason"] = (
                "the band's lower rungs had no headroom: their targets landed "
                "under the untouched point below the band, where the shape law "
                "would have raised them all onto it as one flat")

        new = {}
        for step, i in enumerate(band):
            want = top - (rungs - 1 - step) * grid
            if khz[i] != want:
                new[i] = want
        for i in above:                    # flat top; park = the cap point
            if khz[i] != top:
                new[i] = top
        floor_after = top - (rungs - 1) * grid
        # The point immediately UNDER the band keeps whatever it had, so a
        # clipped ramp can land its floor below its own neighbour. On paper that
        # is a step down at the band edge; in hardware it never becomes one,
        # because the shape law raises the offending rungs back onto the
        # neighbour instead - which is the far worse outcome and the reason the
        # next block exists.
        under = [i for i in range(n)
                 if points[i]["volt_mv"] < points[L]["volt_mv"] - 0.01]
        u = max(under, key=lambda i: points[i]["volt_mv"]) if under else None
        # WHAT THE CARD WILL ACTUALLY RUN, which is not what was just planned:
        # the driver reshapes the evaluated curve (VF_MAX_RISE_KHZ). At the floor
        # a clipped ramp's bottom rungs get raised onto the untouched point below
        # and collapse into one flat - the exact pathology a ramp is for - and a
        # floor sitting far ABOVE that point drags it, and its own neighbours,
        # up. Both are measured; both are invisible in the delta table; so both
        # are counted here rather than discovered after the write.
        real, pos = GPU.predict_curve(points, khz, new,
                                      VF_MAX_RISE_BINS * grid)
        in_band = [pos[i] for i in band]
        shadowed = sum(1 for i in band
                       if real[pos[i]] != new.get(i, khz[i]))
        meta.update({
            "boundary_idx": points[B]["idx"],
            # the band itself cannot tie - every rung is one bin apart - so the
            # only thing that can steal the park point is an UNTOUCHED point
            # below the floor still holding the top frequency. Monotone curves
            # never do; a clipped one-rung band could, and that is worth saying
            # rather than asserting True and being wrong once.
            "unique": u is None or khz[u] < top,
            "lo_idx": points[L]["idx"], "cap_idx": points[B]["idx"],
            "lo_mv": points[L]["volt_mv"], "cap_mv": points[B]["volt_mv"],
            "rungs": rungs, "top_mhz": top / 1000.0,
            "floor_before_mhz": khz[L] / 1000.0,
            "floor_after_mhz": floor_after / 1000.0,
            "floor_cost_mhz": (khz[L] - floor_after) / 1000.0,
            "leveled_above": sum(1 for i in above if i in new),
            "under_band_mhz": (khz[u] / 1000.0) if u is not None else None,
            # rungs the driver will refuse to place where they were planned...
            "shadowed": shadowed,
            # ...leaving this many distinct operating points across the band,
            # which is the number the whole feature is judged on
            "delivered": len({real[k] for k in in_band}),
        })
        # points BELOW the floor the driver will move anyway - shared with
        # compute_hard_deflatten, where the same law reaches 16 points deep
        meta.update(GPU.cascade_meta(points, khz, real, pos, under))
        changes = [(points[i]["idx"], points[i]["volt_mv"], khz[i] / 1000.0,
                    new[i] / 1000.0,
                    points[i]["delta_khz"] + (new[i] - khz[i]))
                   for i in sorted(new)]
        return changes, khz[B] / 1000.0, top / 1000.0, meta

    @staticmethod
    def compute_rephase(deltas, step_khz):
        """Pure phase math: {idx: delta_khz} -> {idx: corrected_delta_khz}.

        Returns (changes, phase). `changes` holds only the points that move.

        Split out of rephase_deltas so it can be run against a STAGED plan
        rather than only against what the hardware currently holds. Re-phasing
        the hardware while an edit is staged answers a question nobody asked:
        the staged deltas are the ones about to be written, so they are the ones
        whose phases have to agree. Doing it before the write also means ONE
        write instead of a write followed by a corrective second one."""
        if not deltas:
            return {}, 0
        grid = int(step_khz)
        counts = {}
        for d in deltas.values():
            r = int(d) % grid
            counts[r] = counts.get(r, 0) + 1
        phase = max(counts, key=lambda r: counts[r])
        changes = {i: int(d) - ((int(d) % grid - phase) % grid)
                   for i, d in deltas.items() if int(d) % grid != phase}
        return changes, phase

    def rephase_deltas(self):
        """Force every delta onto ONE grid phase. Uniform offsets (the core
        slider, or any whole-curve move) only stay grid-exact if all deltas share
        a remainder mod the grid step; a point left on another phase crosses bin
        boundaries at different offsets and silently re-creates a flat. Off-phase
        deltas are rounded DOWN to the common phase, so a point can only lose a
        bin, never gain one unasked.

        Uses this card's step, not 15 MHz: rephasing a Pascal curve onto a
        Turing lattice would BE the de-phasing this exists to remove."""
        pts, err = self.read_vf_curve()
        if err:
            return False, err
        grid = self.clock_step_khz()
        new, _phase = GPU.compute_rephase(
            {p["idx"]: p["delta_khz"] for p in pts}, grid)
        gm = grid / 1000.0
        if not new:
            return True, (f"all {len(pts)} deltas already share one "
                          f"{gm:.4g} MHz phase")
        ok, m = self.apply_vf_deltas(new)
        if ok:
            return True, (f"re-phased {len(new)} off-phase point(s) "
                          f"(idx {sorted(new)}) onto the common {gm:.4g} MHz "
                          f"grid")
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
        lay = self.vfp_layout()
        if lay is None:
            return False, "could not determine this card's VF table layout"
        writable = set(lay.gpu_idx)
        bt = _BoostTable(version=a.ver(_BoostTable, 1))
        _set_point_masks(bt, lay.n_entries)
        st = a.BoostTableGet(a.gpu, ctypes.byref(bt))
        if st != 0:
            return False, f"pre-write table read failed (status {st})"
        nchg = 0
        for idx, delta in new_deltas.items():
            idx, delta = int(idx), int(delta)
            # GPU rows only: on Pascal the trailing rows are MEMORY V/F points,
            # and a delta written there is not a core overclock at all.
            if idx not in writable:
                return False, (f"point index {idx} is not a GPU V/F point on "
                               f"this card ({lay.n_gpu} GPU points of "
                               f"{lay.n_entries} entries)")
            if abs(delta) > self.MAX_ABS_DELTA_KHZ:
                return False, (f"refusing point {idx}: delta {delta // 1000} MHz "
                               f"More than a whole gigahertz of delta? Lol, bro thinks he's Seby. Caught you with a mouse-slip and saved you a total driver crash this time. Don't do it next time. :copege:")
            if bt.rows[idx].w[5] != delta:
                bt.rows[idx].w[5] = delta
                nchg += 1
        if nchg == 0:
            return True, "no delta changes to apply"
        bt.version = a.ver(_BoostTable, 1)
        _set_point_masks(bt, lay.n_entries)
        st = a.BoostTableSet(a.gpu, ctypes.byref(bt))
        if st == 0:
            return True, f"VF delta table written ({nchg} points changed)"
        return False, f"VF table write failed (status {st})"

    def reset_vf_curve(self):
        """Zero every GPU point's delta = factory VF curve. Removes all offsets
        and any de-flatten/editor edits. Stock deltas are 0 on both Turing and
        Pascal, so this is the unambiguous 'back to stock' with no persisted
        baseline to be poisoned.

        Scoped to the GPU rows for the same reason apply_vf_deltas is - zeroing
        'every index' would have written to Pascal's memory V/F rows."""
        lay = self.vfp_layout()
        if lay is None:
            return False, "could not determine this card's VF table layout"
        return self.apply_vf_deltas({i: 0 for i in lay.gpu_idx})

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
