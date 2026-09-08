# Druta - a monitor and tuner for NVIDIA GPUs.
# Copyright (C) 2026 Thermetery Technology Co Limited
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Druta - GPU backend (NVAPI + NVML), read + guarded write.

Built for the Titan RTX (TU102, DEV_1E02) on a 2080 Ti PCB, but
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
import json
import math
import ntpath
import os
import re
import statistics
import struct
import sys
import threading
import time
from dataclasses import dataclass

u8, u32, i32 = ctypes.c_uint8, ctypes.c_uint32, ctypes.c_int32
u64, i64 = ctypes.c_uint64, ctypes.c_int64
PTR = ctypes.c_void_p

# --------------------------------------------------------------------------- #
#  PCI slot: the one identity all three interfaces agree on                     #
# --------------------------------------------------------------------------- #
# NVAPI handle order and NVML index order are NOT the same order. Measured on a
# two-card rig (Titan RTX at bus 1, Titan Xp at bus 2):
#
#     NvAPI_EnumPhysicalGPUs -> [0] = Titan Xp (bus 2), [1] = Titan RTX (bus 1)
#     nvmlDeviceGetHandleByIndex -> [0] = Titan RTX,    [1] = Titan Xp
#
# They are exactly reversed. Pairing the two halves of one GPU object by index
# would therefore have spliced one card's V/F curve onto the other card's name,
# memory type and clock table - silently, and only on a multi-card host. Every
# pairing in this module goes through the PCI slot instead, which is also the
# identity nvtune's `-d` takes, so all three interfaces are keyed the same way.
def format_slot(domain, bus, device, function=0):
    """The canonical spelling, byte-identical to what nvtune prints and
    accepts: '0000:01:00.0'."""
    return f"{domain:04x}:{bus:02x}:{device:02x}.{function}"


def parse_slot(s):
    """'0000:01:00.0' -> (domain, bus, device, function), or None.

    Also eats NVML's 8-digit domain ('00000000:01:00.0'), because
    nvmlDeviceGetPciInfo spells the same slot differently from nvtune."""
    if not s:
        return None
    m = _SLOT_RE.match(str(s).strip())
    if not m:
        return None
    return tuple(int(g, 16) for g in m.groups())


def same_slot(a, b):
    """Do two slot strings name the same card?

    Compared on (bus, device) rather than the whole tuple: NVAPI exposes no PCI
    DOMAIN, so slots built from it always read 0000, and full-string equality
    would fail against NVML on any host where the domain is not zero. Bus and
    device are what actually distinguish cards on the hosts this runs on, and
    the devid/subsys cross-check in GPU._pair() catches the rest."""
    pa, pb = parse_slot(a), parse_slot(b)
    if pa is None or pb is None:
        return False
    return pa[1:3] == pb[1:3]


_SLOT_RE = re.compile(
    r"^([0-9a-fA-F]{4,8}):([0-9a-fA-F]{2}):([0-9a-fA-F]{2})\.([0-9a-fA-F])$")


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# --------------------------------------------------------------------------- #
#  NVAPI                                                                       #
# --------------------------------------------------------------------------- #
class NvAPI:
    def __init__(self, slot=None):
        self.ok = False
        self.gpu = None
        self.gpus = []
        self.selected = None
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
        # Both are PUBLIC SDK entry points, not reverse-engineered ids, and both
        # returned status 0 on every handle of the two-card rig. They are what
        # makes a handle addressable as a slot instead of as an index.
        self.GetBusId = self._i(0x1BE0B8E5, PTR, ctypes.POINTER(u32))
        self.GetBusSlotId = self._i(0x2A0A350F, PTR, ctypes.POINTER(u32))

        # readers
        self.ThermalSettings = self._i(0xE3640A56, PTR, u32, PTR)
        self.ThermalSensors = self._i(0x65FE3AAD, PTR, PTR)
        # Legacy fan path used by Pascal/R470 when NVML has no fan setters.
        self.CoolerSettings = self._i(0xDA141340, PTR, u32, PTR)
        self.CoolerLevelsSet = self._i(0x891FA0AE, PTR, u32, PTR, u32)
        self.CoolerRestore = self._i(0x8F6ED0FB, PTR, PTR, u32)
        self.TachReading = self._i(0x5F608315, PTR, ctypes.POINTER(u32))
        self.FanCoolersControl = self._i(0x814B209F, PTR, PTR)
        self.FanCoolersStatus = self._i(0x35AED5E8, PTR, PTR)
        self.FanCoolersSetControl = self._i(0xA58971A5, PTR, PTR)
        self.VoltRailsStatus = self._i(0x465F9BCF, PTR, PTR)
        self.PowerTopo = self._i(0x0EDCF624E, PTR, PTR)
        self.PowerPolInfo = self._i(0x34206D86, PTR, PTR)
        self.PowerPolStatus = self._i(0x70916171, PTR, PTR)
        self.PerfDecrease = self._i(0x7F7F4600, PTR, ctypes.POINTER(u32))
        self.DynPstates = self._i(0x60DED2ED, PTR, PTR)
        self.CurrentPstate = self._i(0x927DA4F6, PTR, ctypes.POINTER(u32))
        self.ForcePstate = self._i(0x025BFB10, PTR, u32, u32)
        self.Pstates20Get = self._i(0x6FF81213, PTR, PTR)
        self.Pstates20Set = self._i(0x0F4DAE6B, PTR, PTR)
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
        # Per-rail voltage LIMITS, read-only. There is no matching setter: see
        # GPU.read_volt_rail_limits for what was searched and what was found.
        self.VoltRailsCtlGet = self._i(0xA3070DB0, PTR, PTR)
        # The same rails as ABSOLUTE microvolts, including a LIVE per-rail
        # voltage. See GPU.read_volt_rail_state: this is the block that makes a
        # limit write checkable against a number the card produced rather than
        # against an echo of the delta we sent.
        self.VoltRailsAbs = self._i(0x5D0634EE, PTR, PTR)
        # Per-domain clock offsets - the ONLY path to XBAR. Neither NVML's
        # clock offsets nor NVAPI's Pstates20 can reach it: both enumerate
        # exactly two domains on this card, GRAPHICS and MEMORY, and
        # NV_GPU_PUBLIC_CLOCK_ID has no crossbar member at all. Measured here:
        # GetAllClockFrequencies reports bIsPresent for slots 0 and 4 only.
        # These two wrap the RM CLK_DOMAINS controls (NV2080_CTRL_CMD_CLK_
        # CLK_DOMAINS_{GET,SET}_CONTROL). Direction was established by
        # MEASUREMENT, not by trusting a label: the applied XBAR offset was
        # +90 MHz before and after a Get call, 24/24 samples each way.
        self.ClkDomCtlGet = self._i(0xF58938F5, PTR, PTR)
        self.ClkDomCtlSet = self._i(0xD14B69CF, PTR, PTR)
        # Read-only physical clock counter used by the Blackwell probe.  It
        # lets the probe distinguish a real clock movement from array-A's
        # programmed-target echo, which is not sufficient evidence for a
        # working mapping.
        self.ClkMeasureFreq = self._i(0x527FC458, PTR, PTR)

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
        for i in range(cnt.value):
            did, sub, rev, ext = u32(0), u32(0), u32(0), u32(0)
            self.GetPCIIds(handles[i], ctypes.byref(did), ctypes.byref(sub),
                           ctypes.byref(rev), ctypes.byref(ext))
            bus, dev = u32(0), u32(0)
            has_bus = (self.GetBusId is not None
                       and self.GetBusId(handles[i], ctypes.byref(bus)) == 0
                       and self.GetBusSlotId is not None
                       and self.GetBusSlotId(handles[i],
                                             ctypes.byref(dev)) == 0)
            self.gpus.append({
                "handle": handles[i],
                "enum_index": i,
                "devid": did.value >> 16,
                "subsys": sub.value,
                # NVAPI exposes bus and device but no PCI domain, so the slot it
                # yields always reads domain 0000. see same_slot().
                "slot": format_slot(0, bus.value, dev.value) if has_bus else "",
            })
        self._select(slot)

    def _select(self, slot):
        """Pick the handle this instance speaks for.

        With no slot asked for, the choice is the LOWEST PCI slot rather than
        enumeration index 0 - so that "the default card" is a property of the
        machine and not of the order a driver happened to hand back. Asking for
        a slot that is not present fails rather than falling back to some other
        card: a caller that named a card wants that card."""
        if not self.gpus:
            self.err_detail = self.err_detail or "NvAPI enumerated no GPUs"
            return
        if slot:
            hit = [g for g in self.gpus if same_slot(g["slot"], slot)]
            if not hit:
                seen = ", ".join(g["slot"] or "?" for g in self.gpus) or "none"
                self.err_detail = (f"no NVAPI GPU at slot {slot} "
                                   f"(NVAPI sees: {seen})")
                return
            self.selected = hit[0]
        else:
            ordered = sorted(self.gpus,
                             key=lambda g: (parse_slot(g["slot"]) or
                                            (0xFFFF, 0xFF, 0xFF, g["enum_index"])))
            self.selected = ordered[0]
        self.gpu = self.selected["handle"]
        self.ok = True

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


class _CoolerSetting(ctypes.Structure):
    _fields_ = [(name, u32) for name in (
        "type", "controller", "default_min", "default_max", "current_min",
        "current_max", "current_level", "default_policy", "current_policy",
        "target", "control_type", "active")]


class _CoolerSettings(ctypes.Structure):
    _fields_ = [("version", u32), ("count", u32),
                ("entries", _CoolerSetting * 20)]


class _CoolerLevel(ctypes.Structure):
    _fields_ = [("level", u32), ("policy", u32)]


class _CoolerLevels(ctypes.Structure):
    _fields_ = [("version", u32), ("entries", _CoolerLevel * 20)]


class _FanCoolerControlEntry(ctypes.Structure):
    _fields_ = [("id", u32), ("level", u32), ("mode", u32),
                ("reserved", u32 * 8)]


class _FanCoolersControl(ctypes.Structure):
    _fields_ = [("version", u32), ("unknown", u32), ("count", u32),
                ("reserved", u32 * 8), ("entries", _FanCoolerControlEntry * 32)]


class _FanCoolerStatusEntry(ctypes.Structure):
    _fields_ = [("id", u32), ("rpm", u32), ("min", u32), ("max", u32),
                ("level", u32), ("reserved", u32 * 8)]


class _FanCoolersStatus(ctypes.Structure):
    _fields_ = [("version", u32), ("count", u32), ("reserved", u32 * 8),
                ("entries", _FanCoolerStatusEntry * 32)]


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
# On TU102 these are distinct observations. A is the PROGRAMMED target: always
# exactly on the 15 MHz grid, and bit-identical across samples for a fixed
# domain. B is a MEASURED counter: it jitters 1-3 Hz and never lands on the
# grid. GK104/GM107 return identical A/B values in tested states; it has not
# established an independent measured counter. Keep that scope explicit.
#
# HOW FAR APART THEY ACTUALLY RUN, measured on TU102 under ~99% GPU load,
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
# WHAT B ACTUALLY IS, measured rather than assumed: a periodically-refreshed
# hardware frequency measurement. Hammered at 705 calls/s for 6 s, B held ONE
# value across ~700 consecutive reads and then stepped - median gap between
# changes 1020 ms, i.e. a ~1 Hz refresh, not a per-call computation. It landed
# on the 15 MHz programming grid 0 times out of 4229 while A landed on it every
# time. Observed quantum 8 kHz, jitter ~0.25 MHz at 1.95 GHz (0.013%). The
# RM-level mechanism is very likely NV2080_CTRL_CMD_CLK_MEASURE_FREQ
# (0x20809006), which is documented as querying a domain's PHYSICAL frequency -
# that last part is inference, the rest is measured.
#
# B IS THE ONLY MEASURED CLOCK IN THIS APP. nvmlDeviceGetClockInfo(CURRENT)
# returns exactly A - 1950 against A's 1950 and B's 1949.339 - so NVML, the
# tiles and every readout built on them report the driver's TARGET. Anything
# verifying that a write reached the hardware must read B; array A moves
# whether or not the silicon obeys, which is exactly how GP102's inert clock
# offsets passed a verification pass.
#
# SRCID IS THE PARENT DOMAIN INDEX, not an opaque tag. Every ratio slave
# carries the domain number of the row it is slaved to: on TU102 XBAR, SYSCLK,
# LTCCLK and VIDEO all read 0, and GPC is domain 0; on GP102 domains 16 and 17
# read 15, and GPC2CLK is domain 15. 32 is outside the 0-31 domain space and
# marks a domain with no parent - GPC, MEM and the fixed clocks all carry it.
# That column is the clock topology.
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


def classify_domain_names(rows, core_mhz=None, mem_nvml=None,
                          blackwell=False, architecture=None):
    """Name telemetry using architecture-specific identities, then correlation.

    Equal frequencies do not identify a domain: Kepler's idle MEM and graphics
    clocks both read 324 MHz. Known families use their established primary
    slots; unknown families require a unique independent clock correlation.
    Extra legacy names are GK104/GM107 ROM/live-state inferences and remain LIKELY.
    These telemetry IDs never authorize private offset-control writes.
    """
    def close(a, b, tol=0.005):
        return bool(b) and abs(a - b) <= max(0.5, abs(b) * tol)

    populated = {r["domain"]: r for r in rows
                 if r.get("kind") == PRIV_FREQ and r.get("prog_mhz")}
    legacy = architecture in (2, 3, 4)
    modern = architecture == 6 or blackwell or architecture == 10
    gpc_dom, gpc_scale, mem_dom = None, 1, None
    if legacy or modern:
        slot, gpc_scale = (15, 2) if legacy else (0, 1)
        gpc_dom = slot if slot in populated else None
        mem_dom = 4 if 4 in populated else None
    else:
        candidates = [(dom, scale) for dom, r in populated.items()
                      for scale in (1, 2)
                      if core_mhz and close(r["prog_mhz"], scale * core_mhz)]
        memory = [dom for dom, r in populated.items()
                  if close(r["prog_mhz"], mem_nvml)]
        if len(candidates) == 1:
            gpc_dom, gpc_scale = candidates[0]
        if len(memory) == 1:
            mem_dom = memory[0]
        if gpc_dom is not None and gpc_dom == mem_dom:
            gpc_dom = mem_dom = None

    turing_like = architecture == 6 or (architecture is None and gpc_dom == 0)
    blind = architecture is None and not core_mhz

    # GTX 690 and GTX 770 ROM/live-state comparisons, R472.12.
    # GTX 770's distinct held-P0 targets resolve 16=XBAR, 17=SYS, 25=L2C;
    # apply the Kepler map by architecture even when GTX 690 values coincide.
    # These remain ROM-correlated identities, not independent engine counters.
    # See experiments/kepler-gtx770-clock-crosscheck.md.
    KEPLER_NAMES = {
        6: ("DISP", 1),
        16: ("XBAR2CLK", 2), 17: ("SYS2CLK", 2),
        18: ("HUB", 1), 20: ("PWR", 1), 21: ("MSD", 1),
        25: ("L2C2CLK", 2),
    }

    # GM107's deliberately distinct P0 ROM values resolve the pair that was
    # ambiguous on GK104: 16 tracks XBAR 1165 MHz, 17 tracks SYS 1120 MHz.
    # The 1130 MHz L2C and 540 MHz MSD entries have no populated private row
    # in these captures. Do not transfer Kepler's 25/21 labels to empty slots.
    # See experiments/maxwell-gtx745-clock-domains.md.
    MAXWELL_NAMES = {
        6: ("DISP", 1), 16: ("XBAR2CLK", 2), 17: ("SYS2CLK", 2),
        18: ("HUB", 1), 20: ("PWR", 1),
    }

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
    pascal_like = architecture == 4 or (architecture is None and gpc_dom == 15)
    PASCAL_NAMES = {16: ("XBAR2CLK", PRIV_LIKELY)}

    # BLACKWELL PUTS GPC AT DOMAIN 0 TOO, so `turing_like` is true there and
    # the TU102 table would be applied wholesale to a card it was never earned
    # on. Measured on RTX 5080 / 580.97, two of those names are simply wrong:
    #   domain 5  is TU102's LTCCLK, and here reads 0.0 with flags 0x00 - an
    #             empty slot, not a slow clock
    # Domain 21 keeps its name: it was checked against the MEASURED column
    # first and wrongly cleared, because the video engine idles at ~250-310 MHz
    # there. The PROGRAMMED column is the one to read, and it tracks exactly -
    # 2317 / 2407 / 2647 for offsets of +0 / +100 / +335, equal to NVML's own
    # video clock at every step.
    # Domain 20 is active and scales at roughly 0.80 of GPC, which is where an
    # L2 clock usually sits, but nothing here has earned it a name.
    #
    # So Blackwell gets its own table and everything absent from it stays a
    # bare index. GPC and MEM are not listed because the branches above name
    # them by correlation against the driver's own figures, which is stronger
    # than any table.
    BLACKWELL_NAMES = {
        # Earned on Blackwell, not inherited: ctl 0 moves domain 0, and the
        # physical counter for index 0 matched NVML's graphics clock to 1 MHz
        # under load. Listed because the correlation branches above cannot fire
        # at idle - the GPC counter reads a gated ~32 MHz there while NVML
        # still reports its nominal, so nothing correlates and the row would
        # otherwise go nameless on an idle card that plainly has a GPC.
        0: ("GPC", PRIV_CONFIRMED),
        # ctl 2 moves domain 4, and counter index 4 matched NVML's memory
        # clock to 0.2% under load.
        4: ("MEM", PRIV_CONFIRMED),
        # ctl 1 moves this domain 1:1 (+149.6 for +150) and nothing else.
        1: ("XBAR", PRIV_CONFIRMED),
        # ctl 3 moves this domain 1:1. The BEHAVIOUR is confirmed; the word
        # SYSCLK is inherited by elimination, so it stays hedged.
        2: ("SYSCLK", PRIV_LIKELY),
        # Programmed column tracks the VIDEO control 1:1 and equals NVML's
        # video clock exactly. Its MEASURED column reads a few hundred MHz
        # whenever nothing is encoding or decoding, which is an idle engine
        # and not a wrong row.
        21: ("VIDEO", PRIV_CONFIRMED),
        31: ("PCIe link gen", PRIV_LIKELY),
    }

    for r in rows:
        dom = r["domain"]
        r["scale"] = 1
        if (r.get("kind") == PRIV_FREQ
                and not (r.get("prog_khz") or r.get("meas_khz"))):
            r["name"], r["grade"] = "", PRIV_UNPOPULATED
            continue
        if dom == gpc_dom:
            r["name"] = "GPC2CLK" if gpc_scale == 2 else "GPC"
            r["grade"] = PRIV_CONFIRMED
            r["scale"] = gpc_scale
        elif dom == mem_dom:
            r["name"], r["grade"] = "MEM", PRIV_CONFIRMED
        elif architecture == 2 and dom in KEPLER_NAMES:
            r["name"], r["scale"] = KEPLER_NAMES[dom]
            r["grade"] = PRIV_LIKELY
        elif architecture == 3 and dom in MAXWELL_NAMES:
            r["name"], r["scale"] = MAXWELL_NAMES[dom]
            r["grade"] = PRIV_LIKELY
        elif pascal_like and dom in PASCAL_NAMES:
            r["name"], r["grade"] = PASCAL_NAMES[dom]
            r["scale"] = 2
        elif blackwell:
            r["name"], r["grade"] = BLACKWELL_NAMES.get(
                dom, ("", PRIV_UNNAMED))
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
# On R580 the NVML frequency lock lives in this same table, and the mode is
# what tells them apart. nvmlDeviceSetGpuLockedClocks(lo, hi) writes TWO
# mode-2 entries whose "volt_uV" field is a FREQUENCY IN kHz, not a voltage:
# domain 0 takes hi, domain 1 takes lo (measured with an asymmetric lock -
# 1350..1800 produced domain 0 = 1800000 and domain 1 = 1350000). They coexist
# with the mode-3 entry; reset_gpu_clocks clears the mode-2 pair and leaves
# mode 3 alone. So every lookup here matches on MODE, never on "lockMode != 0":
# reading a mode-2 entry as a voltage yields a confident 1350.00 mV, and
# clearing one from this side would silently release the other mechanism's
# lock - the exact confusion the two-mechanism split exists to prevent.
# R472 keeps the frequency lock in separate RM performance-limit records;
# read_clk_lock handles that measured legacy layout without changing this one.
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


# ---- per-domain clock-offset control block ------------------------------- #
# Deliberately NOT a ctypes.Structure. We know four fields out of a 772-byte
# entry; declaring the other 768 bytes as named members would be inventing a
# layout we have not measured. A byte buffer plus offsets says exactly as much
# as we actually know, and the read-modify-write below never has to reconstruct
# the parts we do not understand.
#
# The geometry is not a guess - it SELF-CHECKS. The driver declares the struct
# as 24996 bytes through the version word, and
#       CLKDOM_HDR + 32 * CLKDOM_STRIDE  ==  0x124 + 32*0x304  ==  24996
# exactly. A wrong header or stride would not divide the declared size evenly.
# Measured by sweeping the domain mask one bit at a time and watching where the
# populated dword moved: domain d landed at 0x124 + d*0x304 for d = 0..9.
@dataclass(frozen=True)
class ClkDomLayout:
    """The small part of the private CLK_DOMAINS layout that we use.

    This is intentionally data rather than a ctypes.Structure.  The driver
    owns the remaining bytes and the setter must hand those bytes back exactly
    as GET returned them.  Blackwell kept the version/header/stride but moved
    the fields used by the frequency and MSVDD controls on the validated
    Windows implementation, so those offsets cannot remain global constants.
    """

    name: str
    version: int
    size: int
    header: int
    stride: int
    mask_dword: int
    mode: int
    freq_khz: int
    nvvdd_uv: int | None
    msvdd_uv: int | None


CLKDOM_LAYOUT_TURING = ClkDomLayout(
    name="turing",
    version=0x000261A4,
    size=0x61A4,
    header=0x124,
    stride=0x304,
    mask_dword=2,
    mode=0x000,
    freq_khz=0x10C,
    nvvdd_uv=0x110,
    msvdd_uv=0x114,
)

# Candidate Blackwell layout from the public Windows reverse-engineering notes
# cited in README.md.  The version/header/stride agree with Druta's Windows
# block, while the frequency/MSVDD fields are shifted by one/two dwords.  The
# runtime probe below still checks the version echo and the accepted domain
# mask before this layout can be used.  It deliberately does not enable the
# Blackwell path for a non-RTX-50 card.
CLKDOM_LAYOUT_BLACKWELL = ClkDomLayout(
    name="blackwell",
    version=0x000261A4,
    size=0x61A4,
    header=0x124,
    stride=0x304,
    mask_dword=2,
    mode=0x000,
    freq_khz=0x114,
    nvvdd_uv=0x118,
    msvdd_uv=0x11C,
)

# Kept as compatibility aliases for probes and callers outside this module.
# New driver-facing code should use GPU.clkdom_layout() instead.
CLKDOM_VERSION = CLKDOM_LAYOUT_TURING.version  # ver 2, 24996 bytes
CLKDOM_SIZE = CLKDOM_LAYOUT_TURING.size
CLKDOM_HDR = CLKDOM_LAYOUT_TURING.header
CLKDOM_STRIDE = CLKDOM_LAYOUT_TURING.stride
CLKDOM_MASK_DW = CLKDOM_LAYOUT_TURING.mask_dword  # header dword 2 is a DOMAIN BITMASK, not a
#                                    count: bit d selects domain d. Corroborated
#                                    by nvgpu's CTRL_CLK_DOMAIN_XBARCLK = 0x2,
#                                    which is BIT(1) for domain index 1.
CLKDOM_SLOTS = 32
CLKDOM_MODE = CLKDOM_LAYOUT_TURING.mode  # 8 / 9 / 2 - master / slave / fixed shaped
CLKDOM_FREQ_KHZ = CLKDOM_LAYOUT_TURING.freq_khz  # signed kHz
# The voltage fields are an ARRAY of rails, not one value. Probing every dword
# from +0x100 to +0x140 found exactly three consecutive REFUSED slots -
# 0x114, 0x118, 0x11C - on every domain, which is the signature of a rail array
# whose upper members this silicon does not have. Aligned against the frequency
# field, the array starts at +0x110 = RAIL 0, and rail 0 is the one that works
# here. Third-party notes point at rail 1 because that is MSVDD on Blackwell;
# TU102 refuses it.
# Rail 0 is NVVDD - the rail vcore reports, and the proper schematic name for
# it. Confirmed by effect, not by naming: +50 mV here moves vcore exactly
# +50 mV with the core clock pinned.
CLKDOM_NVVDD_UV = CLKDOM_LAYOUT_TURING.nvvdd_uv  # signed microvolts - WRITABLE, 1:1 on TU102
CLKDOM_RAIL0_UV = CLKDOM_NVVDD_UV  # older name, kept so callers do not break
# Rail 1 is MSVDD. Refused on every domain that DOES anything, and accepted
# only on domain 6 - which stores frequency offsets it never applies either, so
# its acceptance means "nothing validates this", not "this rail is here". Not
# reachable on TU102 through this interface. Read, never written.
CLKDOM_MSVDD_UV = CLKDOM_LAYOUT_TURING.msvdd_uv

_RAIL_NAME = {0: "core rail (NVVDD)", 1: "MSVDD"}

# WHAT IS AND IS NOT ESTABLISHED ABOUT RAIL 1, measured on RTX 5080 / 580.97.
#
# ESTABLISHED, and it is a change from Turing: the write is ACCEPTED. Turing
# refused it on every domain that does anything. Blackwell accepts it on all
# nine, which sounds like progress and is not - it accepts on domains 5 and 7
# too, and those move nothing whatsoever. Acceptance on a provably inert domain
# means "nothing validates this", not "the rail is here". Exactly the reading
# already recorded for TU102 domain 6.
#
# NOT ESTABLISHED, and this is why the write is off by default:
#   - NO LONGER TRUE, kept because the conclusion below still stands: this
#     said the card exposes no MSVDD readback, because VoltRailsStatus returns
#     one rail (NVVDD) and every other version word on it returns -9. A
#     different id does report the rail - see read_volt_rail_state - so a
#     verifying read IS available now, and anyone reviving this feature should
#     use it rather than repeat the search. What has NOT changed is the point
#     below: no effect was ever measured, and that is why the write stays off.
#   - no measurable effect was found. Against NVVDD as a positive control at
#     the same magnitude - which moved vcore +10 mV for +25 and +35 mV for
#     +50, frequency-locked - rail 1 moved neither vcore nor board power at
#     +-25 or +-50 mV. Board power is a blunt instrument at this operating
#     point (NVVDD's own signature was +1.2 W against +-0.3 W of noise), so
#     that is a failure to detect and NOT a demonstration of no effect.
#   - the OFFSET ITSELF is unverified HERE. 0x118 was confirmed as NVVDD by
#     watching vcore follow it; 0x11C has had no equivalent check, because the
#     check needs a readback this card does not provide. That is a gap in OUR
#     evidence, not a doubt about where the offset came from - it is
#     contributed work from a source this project trusts. Unverified and
#     unreliable are different words and only the first one applies.
#
# Left switchable rather than deleted because cross-checking it against another
# tool's behaviour on the same card is legitimate black-box measurement, and
# that is how the identity gets settled. Nothing here is derived from any other
# tool's code or data.
CLKDOM_XBAR = 1

# MEASURED, not inherited. This block does NOT use the private clock getter's
# domain numbering, and assuming it did produced a table that was wrong for
# every entry except GPC and XBAR. The tell is structural: the getter puts
# VIDEO at 21 while this block refuses every bit above 9, so the two cannot be
# the same numbering.
#
# Each row below was established by writing +45 MHz to the control index with
# the clock pinned and recording which private-getter domain moved:
#   0 -> GPC, and every ratio slave with it (XBAR, SYS, LTC, VIDEO all shift)
#   1 -> XBAR      2 -> MEM      3 -> SYS      5 -> VIDEO      9 -> LTC
# 4, 6, 7 and 8 accept a write, store it, and move nothing - they stay
# unnamed rather than being given a plausible label.
CLKDOM_NAMES = {0: "GPC", 1: "XBAR", 2: "MEM", 3: "SYS", 5: "VIDEO", 9: "LTC"}
CLKDOM_SYS = 3
CLKDOM_VIDEO = 5
CLKDOM_LTC = 9

# Which PRIVATE clock domain each CONTROL index actually moves - and it is NOT
# the same on every architecture, which is exactly the trap the domain-name
# tables above are gated against. The CONTROL indices are stable (control 1 is
# XBAR on both cards measured); the PRIVATE domain they land on is not.
#
#   TU102: control 1/3/5/9 -> private 1 (XBAR), 2 (SYS), 21 (VIDEO), 5 (LTC)
#   GP102: control 1 -> private 16 (the 2x domain named XBAR2CLK), 3 -> 17
#
# Measured, by moving one knob and watching every domain. GP102's VIDEO and LTC
# pairings are NOT known - absent here rather than guessed, so a readout for
# them reads "--" instead of quoting an unrelated clock.
#
# VERIFY WITH ARRAY B, NEVER ARRAY A. A is the target the driver RECORDED - an
# echo of the request, which moves whether or not the hardware does. B is the
# free-running measured counter. Every pair below was confirmed by moving the
# offset and watching BOTH: a working control moves A and B together.
CLKDOM_PAIR_TURING = {1: 1, 3: 2, 5: 21, 9: 5}    # A and B both +45, verified
#
# GP102 ACCEPTS THE WRITES AND THE HARDWARE IGNORES THEM. Measured with the
# V/F point pinned: control 1 moved A 3442->3493 (+51) while B sat at 3290.3;
# control 3 moved A 2986->3037 (+51) while B sat at 2885.4. The whole offset
# turns into programmed-vs-measured divergence, which is why the Monitor's
# delta column goes red in proportion to the slider.
#
# The correspondence itself is real - control 1 does drive domain 16's target,
# control 3 domain 17's - so it is recorded here rather than deleted, to save
# anyone re-deriving it. But the SHIPPED map is empty: no knob is built for a
# control that cannot move a clock.
CLKDOM_PAIR_PASCAL_TARGET_ONLY = {1: 16, 3: 17}
CLKDOM_PAIR_PASCAL = {}

assert CLKDOM_HDR + CLKDOM_SLOTS * CLKDOM_STRIDE == CLKDOM_SIZE

# Blackwell's control-domain indices are the RM clock-domain controls, not the
# private getter's domain numbering.  The XBAR/SYSCLK names are retained from
# the documented control layout, while VIDEO was re-identified on RTX 5080
# +610.88 by a repeatable physical VIDEO response at control 4.
# Control 2 is MEM, and it is here because it was MEASURED, not because
# the name lines up. On RTX 5080 / 580.97 a +100 request moved the memory
# clock 15001 -> 15101 and +300 moved it to 15301, exactly 1:1, restoring
# to 15001 on zero. That matters more than usual for this one: it is the
# only route to a memory offset that is not checked against the VBIOS
# delta range. It is NOT, however, a way past that range: measured with NVML
# at zero, this delta moves the memory clock 1:1 up to exactly +3000 MHz
# effective and then stops - +3500 and +4500 store their full value and leave
# the clock at 18001, the same 18001 the declared maximum reaches. The clamp
# lives downstream of every path we have. Unvalidated is not the same as
# unbounded, and this block is the former.
CLKDOM_BLACKWELL_CONTROLS = {1: "XBAR", 2: "MEM", 3: "SYSCLK", 4: "VIDEO"}
# Keep the logical-to-wire signs explicit for each Blackwell control. The
# follow-up end-to-end RTX 5080 test showed that XBAR must be written with the
# same sign selected in the UI. The earlier raw-probe direction was one layer
# too early to use as the UI polarity and caused +200 to apply as -200 in the
# built application.
CLKDOM_BLACKWELL_CONTROL_POLARITY = {1: 1, 3: 1, 4: 1}
# When a driver accepts the documented controls but one has no physical effect,
# scan the other non-core/non-memory indices before touching controls 0 or 2.
# Controls 0 and 2 are kept out of the default scan because they may be the
# GPC and memory paths on a different driver branch.
CLKDOM_BLACKWELL_SAFE_SCAN_CONTROLS = (1, 3, 4, 5, 6, 7, 8, 9)
CLKDOM_BLACKWELL_RISKY_SCAN_CONTROLS = (0, 2)
# Frequency-field candidates only.  The field probe deliberately excludes the
# neighbouring voltage/rail dwords: discovering a frequency layout must never
# require experimenting with NVVDD or MSVDD.
CLKDOM_BLACKWELL_FREQ_CANDIDATES = (0x10C, 0x114)

# NvAPI_GPU_ClockCtrl_ClkMeasureFreq, used only for read-only validation.
CLKMEASURE_VERSION = 0x0001000C
CLKMEASURE_SIZE = 0x000C
CLKMEASURE_MASK = 0x004
CLKMEASURE_FREQ = 0x008
# THESE ARE DOMAIN INDICES, NOT A BITMASK, and the distinction is the whole
# reason this table used to be wrong by one place. Written as bits it looks
# like 1/2/4/16 selects GPC/XBAR/SYS/MEM; measured, the value is an index into
# the same private domain numbering read_clock_domains() reports, where domain
# 4 is MEM. 0x10 is not "memory", it is domain 16, and the driver rejects it
# with -104.
#
# Anchored two independent ways on RTX 5080 / driver 580.97, both under load:
#   index 0 reads 2915.5 MHz against NVML graphics 2917  (1 MHz apart)
#   index 4 reads 14778 MHz against NVML memory 14801    (0.2% apart)
#
# Getting this wrong does not fail loudly - it silently reports a neighbouring
# domain's clock under the name of the one you asked for, which is exactly how
# a 1:1 XBAR response gets reported as a 0.7 ratio.
CLKMEASURE_MASKS = {
    "gpc": 0x00000000,
    "xbar": 0x00000001,
    "sys": 0x00000002,
    "memory": 0x00000004,
}

# The Blackwell physical counters can move while the driver changes P-state.
# A single before/after sample therefore cannot prove that a control write did
# anything.  The probe uses medians and only treats a domain as evidence when
# both windows are reasonably settled.
CLKDOM_PROBE_SAMPLES = 7
CLKDOM_PROBE_INTERVAL_S = 0.05
CLKDOM_PROBE_MAX_RANGE_KHZ = 2_000
CLKDOM_PROBE_EFFECT_KHZ = 2_000
# The diagnostic is intentionally allowed to use the public project's
# +200 MHz starting-point scale.  This is still temporary and is restored
# after each item; it is not a default UI tuning range.
CLKDOM_PROBE_MAX_DELTA_MHZ = 200
# GPC is the core/master clock and can drift by a few MHz while a Blackwell
# control request is being applied. Keep it in the observation windows for
# operating-point diagnostics, but do not let that drift prove an XBAR/SYS/
# VIDEO mapping. The latter four are the direct domain observations.
CLKDOM_PROBE_DIRECT_OBSERVATIONS = (
    "xbar", "sys", "memory", "video")


def clkdom_entry(domain, field, layout=CLKDOM_LAYOUT_TURING):
    """Byte offset of `field` within `domain`'s entry."""
    return layout.header + domain * layout.stride + field


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
    # GTX 745 DDR3: RAM type 7; 900 MHz reported = 1.8 Gbps data rate.
    # A +20-unit Pstates20 request moved the physical clock by +10 MHz.
    7:  ("DDR3", 1),
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


# Public NVAPI Pstates20 layout (NVIDIA/nvapi nvapi.h). The legacy setter
# uses a sparse P0 request: one clock domain, one signed-kHz delta, and no
# voltage entries. This provides the ordinary offsets on drivers predating
# NVML's offset APIs without touching the separate clock-domain controls.
class _Pstate20Delta(ctypes.Structure):
    _fields_ = [("value", i32), ("minimum", i32), ("maximum", i32)]


class _Pstate20Clock(ctypes.Structure):
    _fields_ = [("domain", u32), ("kind", u32), ("flags", u32),
                ("delta", _Pstate20Delta), ("data", u32 * 5)]


class _Pstate20Voltage(ctypes.Structure):
    _fields_ = [("domain", u32), ("flags", u32), ("voltage_uv", u32),
                ("delta", _Pstate20Delta)]


class _Pstate20Entry(ctypes.Structure):
    _fields_ = [("pstate", u32), ("flags", u32),
                ("clocks", _Pstate20Clock * 8),
                ("voltages", _Pstate20Voltage * 4)]


class _Pstates20V1(ctypes.Structure):
    _fields_ = [("version", u32), ("flags", u32), ("num_pstates", u32),
                ("num_clocks", u32), ("num_voltages", u32),
                ("pstates", _Pstate20Entry * 16)]


class _Pstates20V2(ctypes.Structure):
    _fields_ = _Pstates20V1._fields_ + [
        ("num_ov_voltages", u32), ("ov_voltages", _Pstate20Voltage * 4)]


assert ctypes.sizeof(_Pstate20Clock) == 44
assert ctypes.sizeof(_Pstates20V1) == 7316
assert ctypes.sizeof(_Pstates20V2) == 7416


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


class _NvmlPciInfo(ctypes.Structure):
    _fields_ = [("busIdLegacy", ctypes.c_char * 16), ("domain", u32),
                ("bus", u32), ("device", u32), ("pciDeviceId", u32),
                ("pciSubSystemId", u32), ("busId", ctypes.c_char * 32)]


def _windows_system_directory():
    """Resolve the running process's system directory without assuming C:."""
    kernel32 = ctypes.WinDLL("kernel32.dll", winmode=0x800,
                            use_last_error=True)
    get_dir = kernel32.GetSystemDirectoryW
    get_dir.argtypes = [ctypes.c_wchar_p, u32]
    get_dir.restype = u32
    buf = ctypes.create_unicode_buffer(32768)
    length = get_dir(buf, len(buf))
    if not length:
        raise ctypes.WinError(ctypes.get_last_error())
    if length >= len(buf):
        raise OSError("GetSystemDirectoryW returned an oversized path")
    return buf.value


def _windows_program_files_directory():
    """Use the OS folder for this process architecture, including custom drives.

    CSIDL_PROGRAM_FILES resolves to native Program Files in our 64-bit build,
    and Program Files (x86) for a 32-bit process on 64-bit Windows. Do not try
    to load a native 64-bit driver DLL into a 32-bit Python process.
    """
    shell32 = ctypes.WinDLL("shell32.dll", winmode=0x800)
    get_dir = shell32.SHGetFolderPathW
    get_dir.argtypes = [PTR, ctypes.c_int, PTR, u32, ctypes.c_wchar_p]
    get_dir.restype = ctypes.c_long
    buf = ctypes.create_unicode_buffer(260)  # SHGetFolderPathW takes MAX_PATH.
    status = get_dir(None, 0x26, None, 0, buf)  # CSIDL_PROGRAM_FILES, CURRENT
    if status != 0:
        raise OSError(f"SHGetFolderPathW status 0x{status & 0xFFFFFFFF:08X}")
    return buf.value


def _nvml_driver_paths():
    """Only driver installation directories, never PATH or the working folder.

    NVIDIA's R470 reference documents both layouts: DCH in System32, Standard
    in Program Files/NVIDIA Corporation/NVSMI. The latter matters for 472.12.
    https://docs.nvidia.com/deploy/archive/R470/nvml-api/nvml-api-reference.html
    """
    paths, errors = [], []
    for label, resolve, suffix in (
        ("Windows system directory", _windows_system_directory, "nvml.dll"),
        ("Program Files directory", _windows_program_files_directory,
         r"NVIDIA Corporation\NVSMI\nvml.dll"),
    ):
        try:
            folder = ntpath.normpath(resolve())
            drive, tail = ntpath.splitdrive(folder)
            if not drive or not tail.startswith("\\"):
                raise OSError(f"Windows returned a non-absolute path: {folder!r}")
            paths.append(ntpath.join(folder, suffix))
        except Exception as exc:
            errors.append(f"{label}: {exc}")
    return paths, errors


def _load_nvml_library():
    paths, errors = _nvml_driver_paths()
    for path in paths:
        try:
            # PyInstaller otherwise substitutes a bundled basename when an
            # absolute path is missing. NVML must come from the driver install.
            if not os.path.isfile(path):
                raise FileNotFoundError("file not found")
            # LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32:
            # dependencies may live beside the driver DLL or in the system dir.
            dll = ctypes.CDLL(path, winmode=0x100 | 0x800)
            return dll, path
        except Exception as exc:
            detail = str(exc)
            # Frozen builds wrap the original Windows loader error. Preserve it
            # so missing dependencies and wrong architecture remain diagnosable.
            if exc.__cause__ is not None:
                detail += f" (cause: {exc.__cause__})"
            errors.append(f"{path}: {detail}")
    bits = ctypes.sizeof(PTR) * 8
    raise OSError(f"{bits}-bit process; " + "; ".join(errors))


class Nvml:
    def __init__(self, slot=None):
        self.ok = False
        self.dev = None
        self.gpus = []
        self.selected = None
        self.err_detail = ""
        self.dll_path = ""
        try:
            self.dll, self.dll_path = _load_nvml_library()
        except Exception as e:
            self.err_detail = f"nvml.dll not loadable: {e}"
            return
        self.dll.nvmlErrorString.restype = ctypes.c_char_p
        st = self.dll.nvmlInit_v2()
        if st != 0:
            self.err_detail = f"nvmlInit_v2 status {st}"
            return
        cnt = u32(0)
        st = self.dll.nvmlDeviceGetCount_v2(ctypes.byref(cnt))
        if st != 0:
            self.err_detail = f"nvmlDeviceGetCount_v2 status {st}"
            return
        for i in range(cnt.value):
            dev = PTR()
            if self.dll.nvmlDeviceGetHandleByIndex_v2(i,
                                                      ctypes.byref(dev)) != 0:
                continue
            pci = _NvmlPciInfo()
            entry = {"dev": dev, "nvml_index": i, "slot": "",
                     "devid": None, "subsys": None, "name": "", "uuid": ""}
            if self.dll.nvmlDeviceGetPciInfo_v3(dev, ctypes.byref(pci)) == 0:
                entry["slot"] = format_slot(pci.domain, pci.bus, pci.device)
                entry["devid"] = pci.pciDeviceId >> 16
                entry["subsys"] = pci.pciSubSystemId
            buf = ctypes.create_string_buffer(96)
            if self.dll.nvmlDeviceGetName(dev, buf, 96) == 0:
                entry["name"] = buf.value.decode(errors="replace")
            # The only identity that survives two IDENTICAL cards in one host,
            # where name and VBIOS are equal by construction. Profiles lean on
            # it (see profiles.device_mismatch).
            if self.has("nvmlDeviceGetUUID"):
                ubuf = ctypes.create_string_buffer(96)
                if self.dll.nvmlDeviceGetUUID(dev, ubuf, 96) == 0:
                    entry["uuid"] = ubuf.value.decode(errors="replace")
            self.gpus.append(entry)
        self._select(slot)

    def _select(self, slot):
        """Same rule as NvAPI._select: lowest slot by default, exact match or
        failure when a slot is named."""
        if not self.gpus:
            self.err_detail = "NVML enumerated no GPUs"
            return
        if slot:
            hit = [g for g in self.gpus if same_slot(g["slot"], slot)]
            if not hit:
                seen = ", ".join(g["slot"] or "?" for g in self.gpus) or "none"
                self.err_detail = (f"no NVML device at slot {slot} "
                                   f"(NVML sees: {seen})")
                return
            self.selected = hit[0]
        else:
            ordered = sorted(self.gpus,
                             key=lambda g: (parse_slot(g["slot"]) or
                                            (0xFFFF, 0xFF, 0xFF,
                                             g["nvml_index"])))
            self.selected = ordered[0]
        self.dev = self.selected["dev"]
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


def enumerate_gpus():
    """Every NVIDIA GPU on the host, ordered by PCI slot.

    Ordered by SLOT, not by either library's index, so the list the user picks
    from is stable across driver restarts and reboots and matches the order
    `nvtune list` prints. Each entry is what is needed to name a card on screen
    and to construct a GPU for it; nothing here touches clocks or voltage.

    Returns [] when no driver interface answers, which the caller must treat as
    "no cards" rather than "one default card"."""
    nvml, nvapi = Nvml(), NvAPI()
    out = []
    for g in nvml.gpus:
        paired = [a for a in nvapi.gpus if same_slot(a["slot"], g["slot"])]
        out.append({
            "slot": g["slot"],
            "name": g["name"] or "GPU",
            "uuid": g["uuid"],
            "devid": g["devid"],
            "subsys": g["subsys"],
            "nvml_index": g["nvml_index"],
            "has_nvapi": bool(paired),
        })
    # A card NVAPI can see but NVML cannot is still a card, and hiding it would
    # be the more confusing failure - it is the shape a half-attached GPU takes.
    for a in nvapi.gpus:
        if not any(same_slot(a["slot"], o["slot"]) for o in out):
            out.append({
                "slot": a["slot"], "name": "GPU (NVML did not enumerate it)",
                "uuid": "", "devid": a["devid"], "subsys": a["subsys"],
                "nvml_index": None, "has_nvapi": True,
            })
    out.sort(key=lambda o: parse_slot(o["slot"]) or (0xFFFF, 0xFF, 0xFF, 0))
    return out


def slot_from_argv(argv=None):
    """The slot named by `--gpu SLOT` (or `-d SLOT`) on a command line, else "".

    Shared by every entry point in the tree - the shipped UI, the Tk build and
    the self-test mains - so that "which card?" is spelled one way everywhere,
    in nvtune's own slot syntax."""
    argv = list(sys.argv[1:] if argv is None else argv)
    for i, a in enumerate(argv):
        if a in ("--gpu", "-d") and i + 1 < len(argv):
            return argv[i + 1]
    return ""


class GPU:
    # names for the reset_all steps a caller has to single out (see ResetStep).
    # There are TWO lock mechanisms and a caller clearing its on-screen record
    # has to know which one the reset actually released.
    LOCK_STEP = "clock lock"
    VF_LOCK_STEP = "v/f point lock"
    P0_LOCK_STEP = "legacy P0 hold"

    def __init__(self, slot=None):
        self._lock = threading.RLock()
        self._legacy_p0_owned = False
        self._vf_lock_recovery = None
        self._last_clock_lock_request = None
        # The card this object speaks for, fixed at construction. Nothing
        # re-targets a live GPU: switching cards builds a NEW GPU, which is what
        # keeps the probed per-instance caches (_vfp_layout_cache at 128 entries
        # on TU102 vs 84 on GP102, _clock_step_cache at 15 MHz vs 12.657) from
        # ever describing a card they were not measured on.
        self.requested_slot = slot or ""
        self.nvapi = NvAPI(slot)
        self.nvml = Nvml(slot)
        self.pairing_error = self._pair()
        self._pcie_baseline = None
        self.static = self._read_static()
        # Per-card layout selection is intentionally cached only after the
        # getter has echoed the expected version.  A failed probe is cached as
        # False so a bad driver cannot make the UI repeatedly retry an
        # unverified write path on every refresh tick.
        self._clkdom_layout_cache = None
        self._volt_rail_masks_cache = {}
        self.msvdd_write_enabled = False
        self.volt_limits_write_enabled = False
        self.voltage_xoc_enabled = False
        rail_profile = self._volt_rail_profile()
        if rail_profile is not None:
            self.VOLT_LIMIT_MIN_MV = rail_profile["min_mv"]
            self.VOLT_LIMIT_MAX_MV = rail_profile["max_mv"]
            self.VOLT_LIMIT_POWERON = rail_profile["poweron"]

    def _pair(self):
        """Refuse to be half one card and half another.

        NVAPI and NVML enumerate in different orders, so the two handles this
        object holds are only the same silicon if something says so. Both were
        selected by slot, and this re-checks that decision against the PCI
        device and subsystem ids the two libraries report INDEPENDENTLY. On a
        mismatch the NVAPI half is dropped rather than used: the app degrades to
        NVML-only telemetry, which is visibly reduced, instead of writing a V/F
        curve to whichever card NVAPI happened to be holding."""
        a, n = self.nvapi.selected, self.nvml.selected
        if not a or not n:
            return ""
        if n["devid"] is None:
            return ""
        if a["devid"] == n["devid"] and a["subsys"] == n["subsys"]:
            return ""
        msg = (f"NVAPI and NVML disagree about the card at "
               f"{self.requested_slot or a['slot'] or n['slot']}: NVAPI says "
               f"devid {a['devid']:04X}/subsys {a['subsys']:08X}, NVML says "
               f"devid {n['devid']:04X}/subsys {n['subsys']:08X}. "
               f"NVAPI disabled rather than risk writing the wrong card.")
        self.nvapi.ok = False
        self.nvapi.gpu = None
        self.nvapi.err_detail = msg
        return msg

    # ---- helpers ---------------------------------------------------------- #
    def available(self):
        return self.nvapi.ok or self.nvml.ok

    def slot(self):
        """The PCI slot of the card this object drives, in nvtune's spelling.

        NVML's is preferred because it carries a real PCI domain; NVAPI's always
        reads 0000. Empty when neither library could be reached, and every
        nvtune call refuses to run rather than defaulting to all cards."""
        for src in (self.nvml.selected, self.nvapi.selected):
            if src and src.get("slot"):
                return src["slot"]
        return self.requested_slot or ""

    def status_line(self):
        a = "ok" if self.nvapi.ok else f"FAIL ({self.nvapi.err_detail})"
        n = "ok" if self.nvml.ok else f"FAIL ({self.nvml.err_detail})"
        slot = self.slot()
        where = f"   |   slot {slot}" if slot else ""
        return f"NVAPI: {a}   |   NVML: {n}{where}"

    # ---- static identity -------------------------------------------------- #
    def _read_static(self):
        s = {"name": "GPU", "driver": "?", "vbios": "?", "admin": is_admin(),
             "slot": self.slot(), "uuid": ""}
        if self.nvml.selected:
            s["uuid"] = self.nvml.selected.get("uuid", "")
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
                memclks = self._supported_nvml_clocks("nvmlDeviceGetSupportedMemoryClocks")
                if memclks:
                    s["mem_clocks"] = memclks
                    g = self._supported_nvml_clocks(
                        "nvmlDeviceGetSupportedGraphicsClocks", u32(memclks[-1]))
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
        # Clock-offset editable ranges (NVML, or NVAPI on older drivers).
        s["core_off_range"] = self._offset_range(0)
        s["mem_off_range"] = self._offset_range(2)
        if "fan_min" not in s or "fan_max" not in s:
            native = self._native_fan_data()
            if native:
                s["fan_min"] = max(f["min"] for f in native[0]["fans"])
                s["fan_max"] = min(f["max"] for f in native[0]["fans"])
        return s

    def _offset_range(self, ctype):
        nv = self.nvml
        if ctype not in (0, 2):
            return None
        if nv.ok and nv.has("nvmlDeviceGetClockOffsets"):
            co = _ClockOffset(version=nv.ver(_ClockOffset, 1), type=ctype, pstate=0)
            if nv.dll.nvmlDeviceGetClockOffsets(nv.dev, ctypes.byref(co)) == 0:
                return (co.mn, co.mx, co.off)
        info, _err = self._read_pstates20()
        clock = self._pstate20_clock(info, ctype)
        if clock is not None and clock.flags & 1:
            delta = clock.delta
            if delta.minimum <= delta.maximum:
                # Round bounds inward. Match NVML's integer-MHz telemetry
                # contract; full raw V/F deltas are captured separately.
                # NVML doubles memory offsets, while NVAPI uses the reported
                # memory clock's kHz. Measured on both driver interfaces:
                # e.g. Xp NVAPI +/-1000 MHz is NVML +/-2000 API MHz.
                scale = 2 if ctype == 2 else 1
                low = -(-(delta.minimum * scale) // 1000)
                high = delta.maximum * scale // 1000
                if low <= high:
                    return (low, high, int(delta.value * scale / 1000))
        return None

    def _read_pstates20(self):
        """Read and validate the public P-state table; never change clocks."""
        a = self.nvapi
        getter = getattr(a, "Pstates20Get", None)
        if not a.ok or getter is None:
            return None, "NVAPI Pstates20 is unavailable"
        for typ, version in ((_Pstates20V2, 3), (_Pstates20V2, 2),
                             (_Pstates20V1, 1)):
            expected = a.ver(typ, version)
            info = typ(version=expected)
            status = getter(a.gpu, ctypes.byref(info))
            if status == -9:  # NVAPI_INCOMPATIBLE_STRUCT_VERSION
                continue
            if status != 0:
                return None, f"NVAPI Pstates20 read failed: status {status}"
            if (info.version != expected or not 1 <= info.num_pstates <= 16
                    or not 1 <= info.num_clocks <= 8
                    or info.num_voltages > 4
                    or getattr(info, "num_ov_voltages", 0) > 4):
                return None, "NVAPI Pstates20 returned an invalid table"
            return info, None
        return None, "NVAPI Pstates20 structure version is unsupported"

    @staticmethod
    def _pstate20_clock(info, ctype):
        if info is None or ctype not in (0, 2):
            return None
        states = [p for p in info.pstates[:info.num_pstates] if p.pstate == 0]
        if len(states) != 1:
            return None
        domain = 0 if ctype == 0 else 4
        clocks = [c for c in states[0].clocks[:info.num_clocks]
                  if c.domain == domain]
        return clocks[0] if len(clocks) == 1 else None

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
        # Blackwell's private getter domain numbering is not assumed to match
        # the CLK_DOMAINS control indices.  Keep the requested offsets beside
        # the snapshot so the UI can show the value it will write without
        # quoting an unrelated private-clock row.
        if self.clkdom_is_blackwell():
            d["clkdom_offsets"], d["clkdom_offsets_err"] = \
                self.read_clk_domain_offsets()
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
            prog_khz    array-A frequency; programmed target on TU102
            meas_khz    array-B frequency; measured counter on TU102
            prog_mhz / meas_mhz / delta_mhz
                        the same in MHz, None when the row is not a frequency
            flags       array-A's odd dword, the per-domain capability field
                        (constant across every sample of a given domain)
            srcid       array-B's second dword

        Delta is B minus A. The physical-counter interpretation was measured
        on TU102; GK104/GM107 returned identical A/B words in all tested states.

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
        classify_domain_names(rows, core_mhz, mem_nvml,
                              blackwell=self.clkdom_is_blackwell(),
                              architecture=self.arch())
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
        # Applied offsets in the same units on both APIs. Pstates20 is the
        # fallback on R472, before NVML exposed any offset getter/setter.
        for ctype, key in ((0, "core_off"), (2, "mem_off")):
            offset = self._offset_range(ctype)
            if offset is not None:
                d[key] = offset[2]

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
            requested = self.read_power_limit_mw()
            if requested is not None:
                d["pl_requested_mw"] = requested
            v = u32(0)
            if nv.has("nvmlDeviceGetPowerUsage") and \
                    nv.dll.nvmlDeviceGetPowerUsage(nv.dev, ctypes.byref(v)) == 0:
                d["power_w"] = v.value / 1000.0
            if nv.has("nvmlDeviceGetEnforcedPowerLimit") and \
                    nv.dll.nvmlDeviceGetEnforcedPowerLimit(
                        nv.dev, ctypes.byref(v)) == 0:
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
        native = self._native_fan_data()
        nf = u32(0)
        counted = (nv.ok and nv.has("nvmlDeviceGetNumFans") and
                   nv.dll.nvmlDeviceGetNumFans(nv.dev, ctypes.byref(nf)) == 0)
        if not counted and native:
            nf.value = len(native[0]["fans"])
            counted = True
        if counted:
            d["num_fans"] = nf.value
        fans = []
        # R470 has GetFanSpeed[_v2], but no GetNumFans. Read its first fan
        # without claiming the adapter has exactly one. A confirmed zero means
        # a fanless adapter, not an invitation to read an invented fan zero.
        for f in range(nf.value if counted else 1):
            duty = None
            rpm = None
            speed = u32(0)
            if nv.ok and nv.has("nvmlDeviceGetFanSpeed_v2"):
                if nv.dll.nvmlDeviceGetFanSpeed_v2(
                        nv.dev, f, ctypes.byref(speed)) == 0:
                    duty = speed.value
            if duty is None and f == 0 and nv.ok and nv.has("nvmlDeviceGetFanSpeed"):
                if nv.dll.nvmlDeviceGetFanSpeed(
                        nv.dev, ctypes.byref(speed)) == 0:
                    duty = speed.value
            if nv.ok and nv.has("nvmlDeviceGetFanSpeedRPM"):
                fi = _FanSpeedInfo(version=nv.ver(_FanSpeedInfo, 1), fan=f)
                if nv.dll.nvmlDeviceGetFanSpeedRPM(nv.dev, ctypes.byref(fi)) == 0:
                    rpm = fi.speed
            if counted or duty is not None or rpm is not None:
                fans.append((duty, rpm))
        d["fans"] = fans
        if native:
            state, _ = native
            d["num_fans"] = len(state["fans"])
            merged = []
            for index, fan in enumerate(state["fans"]):
                duty, rpm = fans[index] if index < len(fans) else (None, None)
                merged.append((fan["level"] if duty is None else duty,
                               fan.get("rpm") if rpm is None else rpm))
            d["fans"] = merged

    def _native_fan_data(self):
        """Read legacy fan controls; no setters or policy changes occur here.

        Pascal exposes classic CoolerSettings; Turing exposes ClientFanCoolers.
        Layouts correspond to nvapi-sys and NvAPIWrapper's published bindings.
        Classic policy 1 is manual; other known policies are automatic. The
        client interface uses mode 0/1 and IDs that need not start at zero.
        """
        a = self.nvapi
        if not a.ok:
            return None
        getter = getattr(a, "CoolerSettings", None)
        if getter:
            buf = _CoolerSettings(version=a.ver(_CoolerSettings, 1))
            if getter(a.gpu, 7, ctypes.byref(buf)) == 0 and 0 < buf.count <= 20:
                fans = []
                for index in range(buf.count):
                    row = buf.entries[index]
                    if not (0 <= row.current_min <= row.current_max <= 100
                            and row.current_level <= 100
                            and row.current_policy in (1, 2, 4, 8, 16)
                            and row.default_policy in (2, 4, 8, 16)):
                        return None
                    fans.append({"id": index, "level": row.current_level,
                                 "policy": row.current_policy,
                                 "default_policy": row.default_policy,
                                 "manual": row.current_policy == 1,
                                 "min": row.current_min, "max": row.current_max})
                tach = getattr(a, "TachReading", None)
                if len(fans) == 1 and tach:
                    rpm = u32(0)
                    if tach(a.gpu, ctypes.byref(rpm)) == 0:
                        fans[0]["rpm"] = rpm.value
                return {"source": "nvapi_cooler", "fans": fans}, buf
        control_get = getattr(a, "FanCoolersControl", None)
        status_get = getattr(a, "FanCoolersStatus", None)
        if not (control_get and status_get):
            return None
        ctl = _FanCoolersControl(version=a.ver(_FanCoolersControl, 1))
        status = _FanCoolersStatus(version=a.ver(_FanCoolersStatus, 1))
        if control_get(a.gpu, ctypes.byref(ctl)) != 0 or \
                status_get(a.gpu, ctypes.byref(status)) != 0 or \
                not (0 < ctl.count <= 32 and ctl.count == status.count):
            return None
        by_id = {status.entries[i].id: status.entries[i] for i in range(status.count)}
        if len(by_id) != status.count or len({ctl.entries[i].id for i in range(ctl.count)}) != ctl.count:
            return None
        fans = []
        for index in range(ctl.count):
            row = ctl.entries[index]
            live = by_id.get(row.id)
            if live is None or row.mode not in (0, 1) or row.level > 100 or \
                    not (0 <= live.min <= live.max <= 100):
                return None
            fans.append({"id": row.id, "level": row.level, "policy": row.mode,
                         "default_policy": 0, "manual": row.mode == 1,
                         "min": live.min, "max": live.max, "rpm": live.rpm})
        return {"source": "nvapi_client", "fans": fans}, ctl

    def read_fan_control_state(self):
        """Serializable requested levels/policies, separate from spinning RPM.

        A fan ramps after a request. Snapshot its requested level, rather than
        the intermediate speed, so Undo restores what was requested exactly.
        """
        nv = self.nvml
        if nv.ok and all(nv.has(name) for name in (
                "nvmlDeviceGetNumFans", "nvmlDeviceGetFanControlPolicy_v2",
                "nvmlDeviceGetTargetFanSpeed")):
            count = u32(0)
            if nv.dll.nvmlDeviceGetNumFans(nv.dev, ctypes.byref(count)) == 0 and \
                    0 < count.value <= 32:
                fans = []
                for index in range(count.value):
                    policy, level = u32(0), u32(0)
                    if nv.dll.nvmlDeviceGetFanControlPolicy_v2(
                            nv.dev, index, ctypes.byref(policy)) != 0 or \
                            nv.dll.nvmlDeviceGetTargetFanSpeed(
                                nv.dev, index, ctypes.byref(level)) != 0 or \
                            policy.value not in (0, 1) or level.value > 100:
                        break
                    fans.append({"id": index, "level": level.value,
                                 "policy": policy.value, "default_policy": 0,
                                 "manual": policy.value == 1,
                                 "min": self.static.get("fan_min", 30),
                                 "max": self.static.get("fan_max", 100)})
                if len(fans) == count.value:
                    return {"source": "nvml", "fans": fans}
        native = self._native_fan_data()
        return native[0] if native else None

    def read_fan_manual(self):
        """True/False when every fan shares that policy; None for unknown/mixed."""
        state = self.read_fan_control_state()
        if state:
            modes = {fan["manual"] for fan in state["fans"]}
            return modes.pop() if len(modes) == 1 else None
        nv = self.nvml
        if nv.ok and nv.has("nvmlDeviceGetFanControlPolicy_v2"):
            policy = u32(0)
            if nv.dll.nvmlDeviceGetFanControlPolicy_v2(
                    nv.dev, 0, ctypes.byref(policy)) == 0 and policy.value in (0, 1):
                return policy.value == 1
        return None

    def fan_capabilities(self):
        """Capabilities shared by UI, profiles, and the writer entry points."""
        nv = self.nvml
        count = u32(0)
        has_count = (nv.ok and nv.has("nvmlDeviceGetNumFans") and
                     nv.dll.nvmlDeviceGetNumFans(nv.dev, ctypes.byref(count)) == 0
                     and 0 < count.value <= 32)
        manual = bool(has_count and nv.has("nvmlDeviceSetFanSpeed_v2"))
        auto = bool(has_count and nv.has("nvmlDeviceSetDefaultFanSpeed_v2"))
        result = {"manual": manual, "auto": auto, "source": "nvml",
                  "min": self.static.get("fan_min", 30),
                  "max": self.static.get("fan_max", 100)}
        native = self._native_fan_data()
        if native:
            state, _ = native
            a = self.nvapi
            if state["source"] == "nvapi_cooler":
                fallback_manual = bool(getattr(a, "CoolerLevelsSet", None))
                fallback_auto = bool(getattr(a, "CoolerRestore", None))
            else:
                fallback_manual = fallback_auto = bool(getattr(a, "FanCoolersSetControl", None))
            result.update(manual=manual or fallback_manual, auto=auto or fallback_auto,
                          min=max(f["min"] for f in state["fans"]),
                          max=min(f["max"] for f in state["fans"]))
            if not (manual and auto):
                result["source"] = state["source"]
        return result

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
        if nv.ok:
            # EventReasons renamed the older ThrottleReasons API. Both expose
            # the same 64-bit mask; use the legacy name on R470/472.12.
            for name in ("nvmlDeviceGetCurrentClocksEventReasons",
                         "nvmlDeviceGetCurrentClocksThrottleReasons"):
                if nv.has(name):
                    v = u64(0)
                    if getattr(nv.dll, name)(nv.dev, ctypes.byref(v)) == 0:
                        d["event_mask"] = v.value
                        break
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
        if "core_p0max" not in d or "mem_p0max" not in d:
            info, _err = self._read_pstates20()
            for ctype, key in ((0, "core_p0max"), (2, "mem_p0max")):
                clock = self._pstate20_clock(info, ctype)
                if key in d or clock is None or clock.kind not in (0, 1):
                    continue
                # Pstates20 supplies the same P0 frequency range in kHz;
                # SINGLE has one value, RANGE has min then max. It reports
                # the applied frequency range, so do not add the delta again.
                maximum = clock.data[1] if clock.kind == 1 else clock.data[0]
                if maximum:
                    d[key] = maximum // 1000
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
                # split by MODE. On R580 both mechanisms live in this table; their
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
        if "clk_lock_mhz" not in d:
            legacy_lock = self._read_legacy_clk_lock()
            if legacy_lock is not None:
                d["clk_lock_mhz"] = legacy_lock

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

    def memory_offset_step_units(self):
        """Measured request granularity, in driver offset units; no driver calls.

        This RTX 5080/580.97 combination truncates odd NVML memory-offset
        requests to even units. Its exact PCI/VBIOS identity establishes the
        Blackwell scope without querying architecture during profile preflight.
        Other adapters and the Pstates20 transport keep the existing unit grid.
        """
        nv = getattr(self, "nvml", None)
        if not (nv and nv.ok and nv.has("nvmlDeviceSetClockOffsets")):
            return 1
        selected = getattr(nv, "selected", None) or {}
        static = getattr(self, "static", {})
        key = (selected.get("devid"), selected.get("subsys"),
               str(static.get("vbios", "")).lower(), static.get("driver"))
        return 2 if key == (0x2C02, 2313031747, "98.03.3b.c0.6f", "580.97") else 1

    def clock_step_khz(self):
        """This card's core-clock grid in kHz, derived from the driver.

        Use span divided by gaps within the upper, contiguous boost regime.
        Older GPUs can mix divider regimes within one clock table. Checked:
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
            best = sorted(set(max((cl for _mem, cl in table), key=len, default=[])))
            # Kepler mixes divider regimes: the GTX 770 list starts with
            # ~2 MHz gaps and ends with ~13 MHz boost bins. Averaging those
            # regimes invents a 5.523 MHz grid. Measure the contiguous upper
            # regime, allowing integer rounding of a fractional clock bin.
            if len(best) >= 8:
                top_gap = best[-1] - best[-2]
                start = len(best) - 2
                while start > 0 and abs((best[start] - best[start - 1]) - top_gap) <= 1:
                    start -= 1
                best = best[start:]
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
        if type(ctype) is not int or ctype not in (0, 2):
            return False, "clock offset type must be 0 (core) or 2 (memory)"
        if type(mhz) not in (int, float):
            return False, "clock offset must be a finite number, not a bool or string"
        try:
            finite = math.isfinite(mhz)
        except OverflowError:
            finite = False
        if not finite:
            return False, "clock offset must be a finite number"
        if not -(1 << 31) <= mhz < (1 << 31):
            return False, "clock offset exceeds the driver's signed 32-bit range"
        nv = self.nvml
        modern = nv.ok and nv.has("nvmlDeviceSetClockOffsets")
        if not modern and not (self.nvapi.ok
                               and getattr(self.nvapi, "Pstates20Set", None)):
            return False, "clock offset setter is unavailable"
        dom = "core" if ctype == 0 else "mem"
        if ctype == 0:
            mhz = int(mhz)
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
            if (type(scale) not in (int, float) or not 0 < scale < (1 << 31)):
                return False, "memory offset scale is invalid"
        scaled = mhz * scale
        if not -(1 << 31) <= scaled < (1 << 31):
            return False, f"{dom} offset exceeds the driver's signed 32-bit range"
        units = int(scaled)
        step = self.memory_offset_step_units() if ctype == 2 else 1
        if units != scaled or units % step:
            return False, (f"{dom} offset {mhz:+g} {unit} is not representable; "
                           f"use multiples of {step / scale:g} {unit}")
        rng = self._offset_range(ctype)
        if modern and ctype == 2 and rng is None:
            return False, "memory offset range/readback is unavailable; no write issued"
        lo, hi = rng[:2] if rng else ((-1000, 1000) if ctype == 0 else (-2000, 6000))
        if not (lo <= units <= hi):
            elo, ehi = lo / scale, hi / scale
            return False, f"{dom} offset {mhz:+g} {unit} out of range [{elo:g}..{ehi:g}]"
        if not modern:
            return self._set_pstate20_offset(ctype, units, mhz, unit)
        co = _ClockOffset(version=nv.ver(_ClockOffset, 1), type=ctype,
                          pstate=0, off=units)
        st = nv.dll.nvmlDeviceSetClockOffsets(nv.dev, ctypes.byref(co))
        if st == 0:
            if ctype == 2:
                readback = self._offset_range(ctype)
                if readback is None:
                    return False, "memory offset was sent, but readback failed"
                if readback[2] != units:
                    return False, (f"mem offset requested {mhz:+g} {unit}, but the driver "
                                   f"reported {readback[2] / scale:+g} {unit}")
            return True, f"{dom} offset set to {mhz:+g} {unit}"
        return False, f"{dom} offset failed: {nv.errstr(st)}"

    def _set_pstate20_offset(self, ctype, units, mhz, unit):
        """Set one ordinary P0 clock offset, with no voltage/other-domain rows."""
        a = self.nvapi
        dom = "core" if ctype == 0 else "mem"
        info, err = self._read_pstates20()
        clock = self._pstate20_clock(info, ctype)
        if clock is None:
            return False, err or f"no unique P0 {dom} clock entry"
        if not clock.flags & 1:
            return False, f"P0 {dom} clock offset is not editable"
        wire_scale = 2 if ctype == 2 else 1
        delta_khz = units * 1000 // wire_scale
        if not clock.delta.minimum <= delta_khz <= clock.delta.maximum:
            return False, f"{dom} offset exceeds the current P0 range"
        request = type(info)(version=info.version, num_pstates=1, num_clocks=1)
        request.pstates[0].pstate = 0
        request.pstates[0].clocks[0].domain = clock.domain
        request.pstates[0].clocks[0].delta.value = delta_khz
        status = a.Pstates20Set(a.gpu, ctypes.byref(request))
        if status != 0:
            return False, f"{dom} offset failed: NVAPI status {status}"
        readback, err = self._read_pstates20()
        applied = self._pstate20_clock(readback, ctype)
        if applied is None:
            return False, f"{dom} offset was sent, but readback failed: {err or 'missing P0 entry'}"
        if applied.delta.value != delta_khz:
            return False, (f"{dom} offset requested {mhz:+g} {unit}, but the driver "
                           f"reported {applied.delta.value * wire_scale / 1000:g} API MHz")
        return True, f"{dom} offset set to {mhz:+g} {unit} (NVAPI Pstates20)"

    # ---- per-domain clock offsets (XBAR and friends) ---------------------- #
    # A SECOND, entirely separate offset mechanism from set_clock_offset above.
    # That one goes through NVML and reaches GRAPHICS and MEMORY only; this one
    # reaches the domains NVIDIA does not expose publicly at all. They are not
    # two views of one thing: the core offset lands in the V/F delta table, this
    # lands in a per-domain control block, and neither reads or clears the other.
    _CLKDOM_BUF = 65536            # far larger than the 24996 declared

    # Compatibility default; __init__ owns a separate gate on every GPU.
    # See _RAIL_NAME above for the evidence about this offset mechanism.
    msvdd_write_enabled = False

    # NVML's own architecture enum. Kepler 2, Maxwell 3, Pascal 4, Volta 5,
    # Turing 6, Ampere 7, Ada 8, Hopper 9, Blackwell 10.
    ARCH_KEPLER = 2
    ARCH_MAXWELL = 3
    ARCH_PASCAL = 4
    ARCH_TURING = 6
    ARCH_NAMES = {2: "Kepler", 3: "Maxwell", 4: "Pascal", 5: "Volta",
                  6: "Turing", 7: "Ampere", 8: "Ada", 9: "Hopper",
                  10: "Blackwell"}

    def arch(self):
        """This card's architecture as NVML's enum, or ``None``."""
        nv = getattr(self, "nvml", None)
        if not (nv and nv.ok and nv.has("nvmlDeviceGetArchitecture")):
            return None
        a = u32(0)
        if nv.dll.nvmlDeviceGetArchitecture(nv.dev, ctypes.byref(a)) != 0:
            return None
        return a.value

    def arch_name(self):
        return self.ARCH_NAMES.get(self.arch() or -1)

    def is_gtx745(self):
        """The GM107 board tested on R472; not a blanket Maxwell capability."""
        api = getattr(self, "nvapi", None)
        return (self.arch() == self.ARCH_MAXWELL
                and (getattr(api, "selected", None) or {}).get("devid") == 0x1382)

    def vf_curve_applicable(self):
        """Kepler and the GTX 745 use ordinary clock offsets, not this V/F table.

        Other architectures retain their existing runtime layout validation;
        an unavailable read must not be mistaken for an inapplicable curve.
        """
        return self.arch() != self.ARCH_KEPLER and not self.is_gtx745()

    # WHAT WAS ACTUALLY MEASURED, per (architecture, control domain). Absent
    # means nobody has looked, and absent must not be read as either answer.
    #
    #   Pascal / MEM   GP102, Titan Xp. APPLIES, and it is the only path found
    #                  anywhere that goes PAST the declared ceiling: with the
    #                  ordinary memory slider already maxed at its declared
    #                  +250 real (GPU-Z 1426 -> 1676), the per-domain delta
    #                  carried the clock beyond 1676 to 1700+. That is the
    #                  behaviour this knob was built hoping to find, and it
    #                  exists on the OLD card and not the new one.
    #   Pascal / XBAR  GP102. Stored and ignored - the original measurement,
    #                  and the one that was wrongly generalised to every domain.
    #   Turing / MEM   TU102, Titan RTX. APPLIES, but NOT 1:1 and not on any
    #                  ratio yet identified: a +100 request moved the clock to
    #                  1772.7 and +750 landed at 1937, which two points do not
    #                  fit one straight line. Recorded as measured rather than
    #                  modelled. Whether it can pass the declared ceiling is
    #                  UNTESTABLE on that card - its memory gives out around
    #                  2150-2200, below the 2500 the ceiling allows, so the
    #                  silicon runs out before the limit does.
    #   Blackwell/MEM  GB203. APPLIES 1:1 and is CLAMPED at exactly the
    #                  declared maximum: +3000 reaches it, +3500 and +4500
    #                  store in full and move nothing further.
    #
    # So the generations disagree, and the disagreement is the finding: the
    # declared range is enforced downstream on Blackwell and is not on Pascal.
    CLKDOM_DELTA_APPLIES = {
        (ARCH_PASCAL, 2): True,
        (ARCH_PASCAL, 1): False,
        (ARCH_TURING, 2): True,
        (10, 2): True,
    }

    # Where the delta is known to reach past the range the card declares.
    CLKDOM_DELTA_CLEARS_CEILING = {
        (ARCH_PASCAL, 2): True,
        (10, 2): False,
    }

    def clkdom_delta_inert(self, domain=None):
        """True only for a measured inert delta; False for a measured response.

        Unknown domains/architectures return None. Pascal XBAR was stored but
        ignored, while Pascal MEM responded; neither result establishes the
        behavior of Kepler or Maxwell's unvalidated private control layouts.
        """
        applies = self.CLKDOM_DELTA_APPLIES.get((self.arch(), domain))
        return None if applies is None else not applies

    def clkdom_delta_clears_ceiling(self, domain):
        """Does this delta reach past the card's DECLARED range? Tri-state."""
        return self.CLKDOM_DELTA_CLEARS_CEILING.get((self.arch(), domain))

    def clkdom_is_blackwell(self):
        """Whether this is an RTX 50-series card.

        Device ids are not used as the primary discriminator because NVIDIA
        has already shipped multiple board variants for the same GB20x GPU.
        NVML's model string is the stable user-visible identity and also
        covers desktop and laptop RTX 50-series names.
        """
        name = str(getattr(self, "static", {}).get("name", ""))
        return bool(re.search(r"\bRTX\s*50\d{2}\b", name, re.IGNORECASE))

    def clkdom_layout(self):
        """Return the validated layout for this card, or ``None``.

        Druta's original offsets were measured on TU102.  Blackwell uses the
        same private NvAPI ids and version word but the frequency/MSVDD fields
        are at different offsets in the Windows control block.  Select the
        candidate by architecture, then require a successful one-domain GET
        and an exact version echo before any read or write can use it.
        """
        # GK104 accepts this getter and echoes zero-filled Turing-shaped
        # records. Neither the field meanings nor voltage response have been
        # established on Kepler; successful GET alone cannot authorize SET.
        # GM107 GTX 745 exhibits the same unvalidated Turing-shaped response.
        if self.arch() == self.ARCH_KEPLER or self.is_gtx745():
            return None
        cached = getattr(self, "_clkdom_layout_cache", None)
        if cached is not None:
            return None if cached is False else cached
        if not self.clkdom_ok():
            self._clkdom_layout_cache = False
            return None

        layout = (CLKDOM_LAYOUT_BLACKWELL if self.clkdom_is_blackwell()
                  else CLKDOM_LAYOUT_TURING)
        if layout.header + CLKDOM_SLOTS * layout.stride != layout.size:
            self._clkdom_layout_cache = False
            return None

        # Use an accepted one-hot mask rather than assuming domain 0 exists on
        # every architecture.  This is a read-only validation call.
        domains = self.clkdom_domains()
        if not domains:
            self._clkdom_layout_cache = False
            return None
        st, buf = self._clkdom_get(1 << domains[0])
        if st != 0:
            self._clkdom_layout_cache = False
            return None
        echoed = ctypes.cast(buf, ctypes.POINTER(u32))[0]
        if echoed != layout.version:
            self._clkdom_layout_cache = False
            return None
        self._clkdom_layout_cache = layout
        return layout

    def clkdom_controls_for_ui(self, rows=None):
        """Controls safe to show in the UI for this card.

        Turing keeps the original measured control→private-domain pairing.
        Blackwell's private getter numbering is not assumed to match the
        control block, so its controls are gated by the accepted mask and the
        validated control-block layout instead.  Their live values are shown
        as requested offsets, not mislabelled private-clock readings.
        """
        if self.clkdom_layout() is None:
            return []
        if self.clkdom_is_blackwell():
            accepted = set(self.clkdom_domains())
            return [d for d in CLKDOM_BLACKWELL_CONTROLS if d in accepted]
        return sorted(self.clkdom_pairing(rows))

    def clkdom_control_polarity(self, control):
        """Logical-to-wire sign for a Blackwell control request."""
        if not self.clkdom_is_blackwell():
            return 1
        return CLKDOM_BLACKWELL_CONTROL_POLARITY.get(int(control), 1)

    def clkdom_control_label(self, control):
        """Human-readable Blackwell control name, with an honest fallback."""
        if self.clkdom_is_blackwell():
            return CLKDOM_BLACKWELL_CONTROLS.get(
                control, f"control {control}")
        return CLKDOM_NAMES.get(control, f"domain {control}")

    def clkdom_step_mhz(self):
        """Requested per-domain slider granularity for this architecture."""
        # Blackwell's private control block accepts signed kHz requests.  The
        # driver may quantise the applied clock internally, so the UI keeps
        # one-MHz request precision instead of incorrectly borrowing the
        # graphics V/F table's step.
        return 1 if self.clkdom_is_blackwell() else None

    def _clk_measure_freq(self, mask):
        """Read one physical clock counter, or ``None`` if unavailable."""
        a = self.nvapi
        if not (a.ok and a.ClkMeasureFreq):
            return None
        buf = (ctypes.c_ubyte * CLKMEASURE_SIZE)()
        ctypes.memset(buf, 0, CLKMEASURE_SIZE)
        p = ctypes.cast(buf, ctypes.POINTER(u32))
        p[0] = CLKMEASURE_VERSION
        p[CLKMEASURE_MASK // 4] = int(mask)
        st = a.ClkMeasureFreq(a.gpu, ctypes.byref(buf))
        if st != 0:
            return None
        value = int(p[CLKMEASURE_FREQ // 4])
        # Reject garbage before it becomes mapping evidence - but the ceiling
        # has to clear the MEMORY counter, and on GDDR7 that is not a graphics
        # clock. Measured 14,778 MHz on RTX 5080 against NVML's 14,801, so the
        # old 10 GHz cap threw the memory domain away as nonsense on every
        # sample and made it permanently unobservable. 40 GHz keeps the filter
        # useful against a wild read while leaving headroom above the fastest
        # memory this is likely to meet.
        return value if 1_000 <= value <= 40_000_000 else None

    def clkdom_measurements(self):
        """Read-only Blackwell physical clock measurements in kHz."""
        if not self.clkdom_is_blackwell():
            return {}
        return {name: self._clk_measure_freq(mask)
                for name, mask in CLKMEASURE_MASKS.items()}

    def _clkdom_probe_observation(self):
        """Collect physical and private clock observations for the probe."""
        d = self.read()
        obs = dict(self.clkdom_measurements())
        # An offset can change a ceiling without changing an idle clock.  Keep
        # the operating point beside the frequency counters so a report can
        # prove whether the before/after windows were actually comparable.
        if d.get("pstate") is not None:
            obs["pstate"] = int(d["pstate"])
        for key in ("util_gpu", "util_fb", "util_vid", "util_bus"):
            if d.get(key) is not None:
                obs[key] = int(d[key])
        for key in ("core", "mem"):
            if d.get(key) is not None:
                obs[key] = int(d[key]) * 1000
        video = d.get("video")
        obs["video"] = int(video * 1000) if video is not None else None
        for row in d.get("clk_domains") or []:
            dom = row.get("domain")
            if dom is not None:
                obs[f"private_{dom}_meas_khz"] = row.get("meas_khz")
        return obs

    def _clkdom_probe_samples(self):
        """Summarise a short read-only observation window.

        The range is reported along with the median so a mapping result cannot
        hide a P-state transition behind one conveniently timed sample.
        """
        samples = []
        for i in range(CLKDOM_PROBE_SAMPLES):
            samples.append(self._clkdom_probe_observation())
            if i + 1 < CLKDOM_PROBE_SAMPLES:
                time.sleep(CLKDOM_PROBE_INTERVAL_S)
        out = {}
        keys = sorted({key for sample in samples for key in sample})
        for key in keys:
            values = [sample[key] for sample in samples
                      if sample.get(key) is not None]
            if values:
                out[key] = {
                    "median": int(statistics.median(values)),
                    "min": int(min(values)),
                    "max": int(max(values)),
                    "range": int(max(values) - min(values)),
                }
        return out

    def clkdom_mapping_probe(self, delta_mhz=5, confirm=False,
                             freq_fields=None, controls=None):
        """Temporarily test Blackwell XBAR/SYS/VIDEO control indices.

        This is an explicit, administrator-only diagnostic.  It changes one
        frequency-request dword at a time, samples physical counters, and
        restores the complete GET buffer in a ``finally`` block.  When
        ``freq_fields`` contains both candidates, it is a field-layout probe;
        it never experiments with the neighbouring voltage fields.  It is
        never called by the UI automatically.  The output is intended to be
        pasted into a PR or issue so the control mapping can be reviewed from
        actual hardware instead of guessed from domain numbering.
        """
        fields = tuple(freq_fields or ())
        result = {
            "gpu": self.static.get("name", "GPU"),
            "driver": self.static.get("driver", "?"),
            "vbios": self.static.get("vbios", "?"),
            "layout": None,
            "delta_mhz": (int(delta_mhz)
                           if isinstance(delta_mhz, int) else delta_mhz),
            "controls": {},
        }
        if fields and any(field not in CLKDOM_BLACKWELL_FREQ_CANDIDATES
                          for field in fields):
            result["error"] = "unsupported frequency-field candidate"
            return result
        if controls is not None:
            controls = tuple(controls)
            if (not controls
                    or any(not isinstance(control, int)
                           or not 0 <= control < CLKDOM_SLOTS
                           for control in controls)):
                result["error"] = "controls must contain valid clock-domain indices"
                return result
        if not confirm:
            result["error"] = "pass confirm=True to enable the temporary write probe"
            return result
        if not self.clkdom_is_blackwell():
            result["error"] = "this probe is restricted to RTX 50-series cards"
            return result
        if (not isinstance(delta_mhz, int)
                or not 1 <= abs(delta_mhz) <= CLKDOM_PROBE_MAX_DELTA_MHZ):
            result["error"] = (
                "delta_mhz must be an integer in "
                f"[-{CLKDOM_PROBE_MAX_DELTA_MHZ}..-1] or "
                f"[1..{CLKDOM_PROBE_MAX_DELTA_MHZ}]")
            return result
        if not is_admin():
            result["error"] = "administrator privileges are required"
            return result
        layout = self.clkdom_layout()
        if layout is None or not self.nvapi.ClkDomCtlSet:
            result["error"] = "Blackwell clock-domain layout/setter is unavailable"
            return result
        result["layout"] = {
            "header": layout.header,
            "stride": layout.stride,
            "freq_khz": layout.freq_khz,
            "msvdd_uv": layout.msvdd_uv,
        }
        fields = fields or (layout.freq_khz,)
        if len(fields) > 1:
            result["fields"] = {}
        accepted = set(self.clkdom_domains())
        controls = (tuple(CLKDOM_BLACKWELL_CONTROLS)
                    if controls is None else controls)
        result["controls_requested"] = list(controls)
        change_khz = int(delta_mhz) * 1000
        for field in fields:
            field_result = result["controls"]
            if len(fields) > 1:
                field_result = {}
                result["fields"][f"+0x{field:03X}"] = field_result
            for control in controls:
                label = CLKDOM_BLACKWELL_CONTROLS.get(
                    control, f"control {control}")
                item = {"label": label, "accepted": control in accepted,
                        "freq_field": f"+0x{field:03X}"}
                field_result[str(control)] = item
                if control not in accepted:
                    continue
                mask = 1 << control
                st, buf = self._clkdom_get(mask)
                if st != 0:
                    item["get_status"] = int(st)
                    continue
                original = bytes(buf)
                field_offset = (layout.header + control * layout.stride + field)
                dw = field_offset // 4
                old_khz = self._clkdom_word(buf, field_offset, signed=True)
                item["old_freq_khz"] = old_khz
                if old_khz is None:
                    item["error"] = "frequency field is outside the returned buffer"
                    continue
                before = self._clkdom_probe_samples()
                item["before"] = before
                changed = False
                try:
                    ctypes.cast(buf, ctypes.POINTER(i32))[dw] = old_khz + change_khz
                    st2, ref = self._clkdom_get(mask)
                    if st2 != 0:
                        item["verify_status"] = int(st2)
                        continue
                    pb = ctypes.cast(buf, ctypes.POINTER(u32))
                    pr = ctypes.cast(ref, ctypes.POINTER(u32))
                    diffs = [i for i in range(layout.size // 4)
                             if pb[i] != pr[i]]
                    if diffs != [dw]:
                        item["error"] = ("refusing probe write: unexpected changed "
                                          f"dwords {diffs[:8]}")
                        continue
                    st3 = self.nvapi.ClkDomCtlSet(self.nvapi.gpu,
                                                   ctypes.byref(buf))
                    item["set_status"] = int(st3)
                    if st3 != 0:
                        continue
                    changed = True
                    item["requested_freq_khz"] = old_khz + change_khz
                    st4, readback = self._clkdom_get(mask)
                    item["readback_status"] = int(st4)
                    if st4 == 0:
                        item["readback_freq_khz"] = self._clkdom_word(
                            readback, field_offset, signed=True)
                        item["readback_mode"] = self._clkdom_word(
                            readback,
                            layout.header + control * layout.stride + layout.mode)
                        item["readback_matches_request"] = (
                            item["readback_freq_khz"] == item["requested_freq_khz"])
                        if not item["readback_matches_request"]:
                            item["readback_error"] = (
                                "SET_CONTROL returned success but GET did not retain "
                                f"the requested frequency ({item['readback_freq_khz']!r}; "
                                f"expected {item['requested_freq_khz']!r})")
                    time.sleep(CLKDOM_PROBE_INTERVAL_S)
                    item["after"] = self._clkdom_probe_samples()
                    # The XBAR counter has a wider measurement spread under
                    # load than the private getter.  A large, repeatable
                    # request should not be discarded merely because its
                    # window is wider than the 2 MHz small-signal threshold.
                    # Cap the relaxed window so a genuine P-state jump still
                    # cannot be silently accepted as a clock mapping.
                    settled_range_khz = max(
                        CLKDOM_PROBE_MAX_RANGE_KHZ,
                        min(50_000, abs(change_khz) // 4))
                    item["settled_range_limit_khz"] = settled_range_khz
                    # A ±200 MHz diagnostic is deliberately large enough to
                    # expose a real domain response, but the ordinary 2 MHz
                    # effect threshold would classify normal counter jitter
                    # as a mapping. Scale the threshold with the request so
                    # small-signal probes stay sensitive while large probes
                    # require a materially larger response.
                    effect_threshold_khz = max(
                        CLKDOM_PROBE_EFFECT_KHZ,
                        min(25_000, abs(change_khz) // 10))
                    item["effect_threshold_khz"] = effect_threshold_khz
                    stable = {
                        key: (before[key]["range"] <= settled_range_khz
                              and item["after"].get(key, {}).get("range", 0)
                              <= settled_range_khz)
                        for key in before
                        if key in item["after"]
                    }
                    item["stable_observations"] = {
                        key: value for key, value in stable.items() if value
                    }
                    item["changed_observations"] = {
                        key: {"before": before[key]["median"],
                              "after": item["after"][key]["median"],
                              "delta": (item["after"][key]["median"]
                                        - before[key]["median"])}
                        for key in before
                        if stable.get(key)
                        and abs(item["after"][key]["median"]
                                - before[key]["median"]) >= effect_threshold_khz
                    }
                    # Preserve a useful lead even when a physical counter's
                    # after-window is too wide to be accepted as settled.
                    # These entries are diagnostic candidates only; mapping
                    # verdicts continue to use changed_observations below.
                    item["median_shift_candidates"] = {
                        key: {"before": before[key]["median"],
                              "after": item["after"][key]["median"],
                              "delta": (item["after"][key]["median"]
                                        - before[key]["median"]),
                              "stable": bool(stable.get(key))}
                        for key in before
                        if key in item["after"]
                        and abs(item["after"][key]["median"]
                                - before[key]["median"]) >= effect_threshold_khz
                    }
                    item["direct_changed_observations"] = {
                        key: value for key, value in
                        item["changed_observations"].items()
                        if key in CLKDOM_PROBE_DIRECT_OBSERVATIONS
                    }
                    expected_sign = 1 if change_khz > 0 else -1
                    item["directional_observations"] = {
                        key: value for key, value in
                        item["direct_changed_observations"].items()
                        if value["delta"] * expected_sign > 0
                    }
                    item["reverse_directional_observations"] = {
                        key: value for key, value in
                        item["direct_changed_observations"].items()
                        if value["delta"] * expected_sign < 0
                    }
                    item["physical_effect"] = bool(
                        item["directional_observations"])
                    if not item["physical_effect"]:
                        if item["changed_observations"]:
                            item["error"] = (
                                "settled observations moved, but none followed the "
                                "requested frequency direction; repeat the A/B test")
                        else:
                            item["error"] = (
                                "write was accepted but no settled physical clock "
                                "observation moved by >=2 MHz; lock the GPU clock "
                                "or repeat under a steady load")
                except Exception as exc:
                    item["error"] = f"observation failed after write: {exc}"
                finally:
                    # Restore the complete original GET buffer, not a newly
                    # synthesised struct.  This preserves fields Druta does not
                    # understand and runs even if sampling raises.
                    if changed:
                        restore = (ctypes.c_ubyte * self._CLKDOM_BUF)()
                        ctypes.memmove(restore, original, len(original))
                        rst = self.nvapi.ClkDomCtlSet(self.nvapi.gpu,
                                                      ctypes.byref(restore))
                        item["restore_status"] = int(rst)
                        if rst == 0:
                            time.sleep(CLKDOM_PROBE_INTERVAL_S)
                            item["restored"] = self._clkdom_probe_samples()
                            rows, restore_err = self.read_clk_domain_offsets()
                            restored_freq = ((rows or {}).get(control) or
                                             {}).get("freq_khz")
                            item["restored_freq_khz"] = restored_freq
                            if restore_err or restored_freq != old_khz:
                                item["error"] = (
                                    "restore status was successful but the original "
                                    f"frequency request was not read back ({restored_freq!r}; "
                                    f"expected {old_khz!r})")
        return result

    @staticmethod
    def _clkdom_word(buf, offset, signed=False):
        if offset < 0 or offset + 4 > len(buf) or offset % 4:
            return None
        typ = i32 if signed else u32
        return int(ctypes.cast(buf, ctypes.POINTER(typ))[offset // 4])

    def clkdom_debug_report(self):
        """Return a JSON-safe read-only report for a new GPU/driver.

        The report intentionally includes both candidate field locations.  A
        maintainer can run ``python nvbackend.py --clkdom-debug --json`` on a
        Blackwell card and compare the live values before authorising a write
        mapping.  This method never calls ClkDomCtlSet.
        """
        report = {
            "gpu": dict(getattr(self, "static", {})),
            "nvapi": {
                "ok": bool(self.nvapi.ok),
                "clkdom_get": bool(self.nvapi.ClkDomCtlGet),
                "clkdom_set": bool(self.nvapi.ClkDomCtlSet),
                "clk_measure": bool(self.nvapi.ClkMeasureFreq),
            },
            "blackwell_name_match": self.clkdom_is_blackwell(),
            "accepted_domains": [],
            "layout": None,
            "getter_status": None,
            "version_echo": None,
            "entries": {},
            "physical_measurements_khz": {},
            "private_clock_domains": None,
            "private_clock_domains_error": None,
        }
        if not self.clkdom_ok():
            report["error"] = "0xF58938F5 is not available"
            return report

        domains = self.clkdom_domains()
        report["accepted_domains"] = list(domains)
        layout = self.clkdom_layout()
        if layout is None:
            # A suppressed write path must not suppress the evidence needed to
            # decode it. Keep unknown fields as raw offsets, without applying
            # Turing's record layout or interpreting accepted masks as support.
            report["error"] = "offset controls suppressed: field mapping/write response unverified"
            report["raw_queries"] = []
            for mask in [0] + [1 << d for d in domains]:
                status, raw = self._clkdom_get(mask)
                initial = bytearray(len(raw))
                struct.pack_into("<I", initial, 0, CLKDOM_VERSION)
                struct.pack_into("<I", initial, CLKDOM_MASK_DW * 4, mask)
                returned = bytes(raw)
                changes = {}
                for offset in range(0, len(returned), 4):
                    if returned[offset:offset + 4] != initial[offset:offset + 4]:
                        changes[f"0x{offset:X}"] = struct.unpack_from("<I", returned, offset)[0]
                report["raw_queries"].append({
                    "mask": mask, "status": int(status),
                    "version_echo": self._clkdom_word(raw, 0),
                    "changed_dwords": changes,
                })
            try:
                rows, err = self.read_clock_domains()
                report["private_clock_domains"] = rows
                report["private_clock_domains_error"] = err
            except Exception as exc:
                report["private_clock_domains_error"] = str(exc)
            return report
        report["layout"] = {
            "name": layout.name,
            "version": f"0x{layout.version:08X}",
            "size": f"0x{layout.size:X}",
            "header": f"0x{layout.header:X}",
            "stride": f"0x{layout.stride:X}",
            "mode": f"0x{layout.mode:X}",
            "freq_khz": f"0x{layout.freq_khz:X}",
            "nvvdd_uv": (f"0x{layout.nvvdd_uv:X}"
                         if layout.nvvdd_uv is not None else None),
            "msvdd_uv": (f"0x{layout.msvdd_uv:X}"
                         if layout.msvdd_uv is not None else None),
        }

        mask = 0
        for d in domains:
            mask |= 1 << d
        st, buf = self._clkdom_get(mask)
        report["getter_status"] = int(st)
        if st != 0:
            report["error"] = f"GET failed with status {st}"
            return report
        report["version_echo"] = f"0x{self._clkdom_word(buf, 0):08X}"

        # Include the mode and a window around both known frequency/MSVDD
        # candidates.  The buffer's unknown bytes are never reconstructed.
        fields = (0x000, 0x100, 0x104, 0x108, 0x10C, 0x110, 0x114,
                  0x118, 0x11C, 0x120, 0x124)
        for d in domains:
            base = layout.header + d * layout.stride
            words = {}
            for off in fields:
                value = self._clkdom_word(buf, base + off)
                if value is not None:
                    words[f"+0x{off:03X}"] = {
                        "u32": value,
                        "i32": self._clkdom_word(buf, base + off, True),
                    }
            report["entries"][str(d)] = words

        report["physical_measurements_khz"] = self.clkdom_measurements()
        try:
            rows, err = self.read_clock_domains()
            report["private_clock_domains"] = rows
            report["private_clock_domains_error"] = err
        except Exception as exc:
            report["private_clock_domains_error"] = str(exc)
        return report

    def clkdom_ok(self):
        a = self.nvapi
        return bool(a.ok and a.ClkDomCtlGet)

    def _clkdom_get(self, mask):
        """(status, buffer). The buffer is the driver's own bytes, untouched."""
        a = self.nvapi
        buf = (ctypes.c_ubyte * self._CLKDOM_BUF)()
        ctypes.memset(buf, 0, self._CLKDOM_BUF)
        p = ctypes.cast(buf, ctypes.POINTER(u32))
        p[0] = CLKDOM_VERSION
        p[CLKDOM_MASK_DW] = mask
        return a.ClkDomCtlGet(a.gpu, ctypes.byref(buf)), buf

    def clkdom_domains(self):
        """Domains this card accepts, probed once and cached.

        Not a constant: the mask rejects the WHOLE call with -1 if any bit is
        invalid, so a hardcoded mask that is right on TU102 would break every
        card with a different domain count. Probe one bit at a time instead."""
        if getattr(self, "_clkdom_valid", None) is None:
            found = []
            if self.clkdom_ok():
                for d in range(CLKDOM_SLOTS):
                    st, _ = self._clkdom_get(1 << d)
                    if st == 0:
                        found.append(d)
            self._clkdom_valid = found
        return self._clkdom_valid

    def clkdom_pairing(self, rows=None):
        """{control index: private domain} for THIS card, or {} if unknown.

        Gated on the same signature the name tables use - GPC at domain 0 is
        the TU102 shape, at 15 the GP102 one - because a control->private pair
        measured on one card is precisely the thing that does not transfer. An
        unknown architecture returns EMPTY rather than the Turing table: a
        readout of '--' is honest, and a readout quoting an unrelated domain's
        clock beside a knob is the failure this whole app is built to avoid.

        `rows` may be supplied by a caller that already has a snapshot; pass
        the rows from GPU.read(), NOT a bare read_clock_domains(). Without
        core_mhz the naming runs BLIND: it cannot apply the unpopulated check,
        so it names a dead domain 0 'GPC' and reports every card as Turing.
        That is how an earlier build mislabelled a GP102."""
        if self.arch() == 2:
            return {}
        if getattr(self, "_clkdom_pair", None):
            return self._clkdom_pair
        if rows is None:
            rows = (self.read() or {}).get("clk_domains")
        # Signature by WHICH DOMAINS ARE POPULATED, not by which one the namer
        # decided is GPC. That naming correlates a domain against the core
        # clock, and at deep idle several domains correlate equally well - a
        # GP102 sitting at 278 MHz had domain 5 (571 MHz) identified as the 2x
        # core just as readily as domain 15. Populated-or-not does not move
        # with the operating point.
        live = {r["domain"] for r in (rows or []) if r.get("prog_mhz")}
        if 0 in live:               # TU102 runs its GPU clock at domain 0
            pair = dict(CLKDOM_PAIR_TURING)
        elif 15 in live:            # GP102 puts it at 15, doubled
            pair = dict(CLKDOM_PAIR_PASCAL)
        else:
            # Nothing recognised. Return empty WITHOUT caching: a card read
            # mid-transition would otherwise be stuck knob-less for the life of
            # the process, and an empty answer is only ever a "not yet".
            return {}
        self._clkdom_pair = pair
        return pair

    def read_clk_domain_offsets(self):
        """({domain: {mode, freq_khz, msvdd_uv}}, err)."""
        if not self.clkdom_ok():
            return None, "per-domain clock control unavailable (0xF58938F5)"
        layout = self.clkdom_layout()
        if layout is None:
            return None, "clock-domain layout is not validated for this GPU/driver"
        doms = self.clkdom_domains()
        if not doms:
            return None, "no clock domain accepted by this driver"
        mask = 0
        for d in doms:
            mask |= 1 << d
        st, buf = self._clkdom_get(mask)
        if st != 0:
            return None, f"clock-domain read failed (status {st})"
        p = ctypes.cast(buf, ctypes.POINTER(i32))
        if ctypes.cast(buf, ctypes.POINTER(u32))[0] != layout.version:
            return None, "clock-domain block did not echo its version"
        out = {}
        for d in doms:
            base = layout.header + d * layout.stride
            raw_freq_khz = p[(base + layout.freq_khz) // 4]
            out[d] = {
                "mode": p[(base + layout.mode) // 4],
                # Expose the logical UI sign. The diagnostic probe below
                # deliberately writes raw signed values so it can discover
                # this polarity rather than hiding it.
                "freq_khz": (raw_freq_khz
                             * self.clkdom_control_polarity(d)),
                "msvdd_uv": (p[(base + layout.msvdd_uv) // 4]
                             if layout.msvdd_uv is not None else None),
            }
        return out, None

    def read_xbar_offset_mhz(self):
        """The REQUESTED XBAR offset in MHz, or None.

        Requested, not applied. The driver floors a request to whole clock
        bins, so a +100 request runs as +90 - both numbers are real and they
        are not the same number. The applied figure comes from the private
        clock getter (domain 1), never from here."""
        rows, err = self.read_clk_domain_offsets()
        if err or CLKDOM_XBAR not in (rows or {}):
            return None
        return rows[CLKDOM_XBAR]["freq_khz"] / 1000.0

    def read_rail_offset_mv(self, domain=0):
        """The rail-0 voltage offset in mV, or None."""
        layout = self.clkdom_layout()
        if layout is None or layout.nvvdd_uv is None:
            return None
        st, buf = self._clkdom_get(1 << domain)
        if st != 0:
            return None
        dw = (layout.header + domain * layout.stride + layout.nvvdd_uv) // 4
        return ctypes.cast(buf, ctypes.POINTER(i32))[dw] / 1000.0

    def set_rail_offset_mv(self, mv, domain=0, rail=0):
        """Offset the core rail by `mv` millivolts.

        MEASURED 1:1 on TU102 with the core clock pinned by the NVML frequency
        lock: 6.25 / 12.5 / 25 / 50 / 100 mV requested moved vcore by exactly
        6.25 / 12.5 / 25 / 50 / 100 mV, quantised to the card's 6.25 mV grid.
        The clock held at 1500.0 MHz throughout, so the movement had nowhere
        else to come from.

        Measuring this with the V/F POINT lock instead gives a much smaller and
        wrong answer - that lock holds 'the highest point at or below a
        voltage', so shifting the rail changes which point is held and the
        reading conflates the offset with a change of operating point. If this
        ever needs re-measuring, pin the FREQUENCY, not the voltage.

        This is a SECOND mechanism on the rail that set_voltage_boost already
        raises a ceiling on. They are not the same knob and nothing here reads
        or clears the other."""
        a = self.nvapi
        if not (self.clkdom_ok() and a.ClkDomCtlSet):
            return False, "per-domain clock control unavailable"
        layout = self.clkdom_layout()
        if layout is None or layout.nvvdd_uv is None:
            return False, "NVVDD layout is not validated for this GPU/driver"
        field = layout.nvvdd_uv if rail == 0 else layout.msvdd_uv
        if field is None:
            return False, f"rail {rail} has no field in the {layout.name} layout"
        if rail != 0 and not self.msvdd_write_enabled:
            # Off by default, and not something a slider can turn on by itself.
            # See the class attribute for what is and is not established about
            # this field.
            return False, ("rail 1 writes are disabled: this offset has "
                           "never been shown to move anything, and NVVDD as a "
                           "positive control did move at the same magnitude. "
                           "The rail itself IS readable now via "
                           "read_volt_rail_state, so the check is available. "
                           "Set this GPU's msvdd_write_enabled to allow it.")
        st, buf = self._clkdom_get(1 << domain)
        if st != 0:
            return False, f"read failed (status {st})"
        dw = (layout.header + domain * layout.stride + field) // 4
        was = ctypes.cast(buf, ctypes.POINTER(i32))[dw]
        if rail == 0:
            lower = -500.0 if self.voltage_xoc_enabled else -100.0
            upper = (self.RAIL_OFFSET_XOC_MAX_MV if self.voltage_xoc_enabled
                     else self.RAIL_OFFSET_MAX_MV)
            lower, upper = min(lower, was / 1000), max(upper, was / 1000)
            if not (lower <= mv <= upper):
                return False, (f"NVVDD offset {mv:g} mV is outside Druta's "
                               f"{lower:g}..{upper:g} mV bound")
        uv = int(round(mv * 1000))
        if was == uv:
            # The diff guard below demands exactly one changed dword, so a
            # no-op write would be REFUSED rather than silently doing nothing.
            # Say so plainly instead of reporting a failure for a request that
            # is already satisfied.
            return True, (f"{_RAIL_NAME[rail]} offset already "
                          f"{uv/1000:+.2f} mV")
        ctypes.cast(buf, ctypes.POINTER(i32))[dw] = uv
        st2, ref = self._clkdom_get(1 << domain)
        if st2 != 0:
            return False, f"verification read failed (status {st2})"
        pb = ctypes.cast(buf, ctypes.POINTER(u32))
        pr = ctypes.cast(ref, ctypes.POINTER(u32))
        diffs = [i for i in range(layout.size // 4) if pb[i] != pr[i]]
        if diffs != [dw]:
            return False, (f"refusing to write: {len(diffs)} dwords differ, "
                           f"expected only {dw}")
        sst = a.ClkDomCtlSet(a.gpu, ctypes.byref(buf))
        if sst != 0:
            return False, (f"{_RAIL_NAME[rail]} write failed "
                           f"(status {sst})")
        note = ("" if rail == 0 else
                " - UNVERIFIABLE: nothing on this card reads this rail "
                "back, so this is a request that was accepted, not a "
                "change that was observed")
        return True, (f"{_RAIL_NAME[rail]} offset {was/1000:+.2f} -> "
                      f"{uv/1000:+.2f} mV{note}")

    def set_clk_domain_offset(self, domain, mhz):
        """Read-modify-write exactly one dword of the control block.

        The buffer written is the one the getter just produced, with the single
        frequency-delta dword changed and every other byte - the mode field,
        the MSVDD rail, and all 31 other domain entries - passed back exactly
        as the driver wrote it. Same doctrine as set_vf_lock: never a fresh
        struct, because 768 of the 772 bytes in an entry are fields we have not
        identified and cannot responsibly synthesise.

        The diff guard below is not decoration. It re-reads the block and
        refuses to write if anything other than the intended dword differs, so
        a layout change in a future driver turns into a refusal rather than a
        write into whatever now occupies that offset."""
        if type(domain) is not int or not 0 <= domain < CLKDOM_SLOTS:
            return False, "clock domain must be an integer control index"
        if type(mhz) not in (int, float):
            return False, "clock-domain offset must be a finite number"
        try:
            finite = math.isfinite(mhz)
        except OverflowError:
            finite = False
        if not finite or not -(1 << 31) <= mhz * 1000 < (1 << 31):
            return False, "clock-domain offset exceeds the signed 32-bit kHz range"
        a = self.nvapi
        if not (self.clkdom_ok() and a.ClkDomCtlSet):
            return False, "per-domain clock control unavailable"
        layout = self.clkdom_layout()
        if layout is None:
            return False, "clock-domain layout is not validated for this GPU/driver"
        if domain not in self.clkdom_domains():
            return False, (f"domain {domain} is not one this driver accepts "
                           f"({self.clkdom_domains()})")
        khz = int(round(mhz)) * 1000
        if not -(1 << 31) <= khz < (1 << 31):
            return False, "rounded clock-domain offset exceeds the signed 32-bit kHz range"
        wire_khz = khz * self.clkdom_control_polarity(domain)
        mask = 1 << domain
        st, buf = self._clkdom_get(mask)
        if st != 0:
            return False, f"clock-domain read failed (status {st})"
        def valid_read(block):
            if ctypes.sizeof(block) < layout.size:
                return False
            words = ctypes.cast(block, ctypes.POINTER(u32))
            return words[0] == layout.version and words[layout.mask_dword] == mask

        if not valid_read(buf):
            return False, "clock-domain block did not echo its version and requested mask"
        original = ctypes.string_at(buf, layout.size)
        dw = (layout.header + domain * layout.stride + layout.freq_khz) // 4
        was_wire = ctypes.cast(buf, ctypes.POINTER(i32))[dw]
        was = was_wire * self.clkdom_control_polarity(domain)

        st2, ref = self._clkdom_get(mask)
        if st2 != 0:
            return False, f"verification read failed (status {st2})"
        if not valid_read(ref):
            return False, "verification block did not echo its version and requested mask"
        if ctypes.string_at(ref, layout.size) != original:
            return False, "clock-domain block changed between reads; no write issued"
        if was_wire == wire_khz:
            return True, (f"{self.clkdom_control_label(domain)} offset already "
                          f"{khz/1000:+.0f} MHz; no write needed")
        ctypes.cast(buf, ctypes.POINTER(i32))[dw] = wire_khz
        pb = ctypes.cast(buf, ctypes.POINTER(u32))
        pr = ctypes.cast(ref, ctypes.POINTER(u32))
        diffs = [i for i in range(layout.size // 4) if pb[i] != pr[i]]
        if diffs != [dw]:
            return False, (f"refusing to write: {len(diffs)} dwords differ "
                           f"{diffs[:8]}, expected only {dw}. The block layout "
                           f"is not what this build measured.")

        sst = a.ClkDomCtlSet(a.gpu, ctypes.byref(buf))
        if sst != 0:
            return False, f"clock-domain write failed (status {sst})"
        name = self.clkdom_control_label(domain)
        return True, (f"{name} offset {was/1000:+.0f} -> {khz/1000:+.0f} MHz "
                      f"(requested; the driver floors to whole clock bins)")

    def read_power_limit_mw(self):
        """Configured power request in mW, or None when its getter is unreadable.

        The enforced limit is a different, potentially delayed constraint and
        must not stand in for the user's setting in profiles or write readback.
        """
        nv = self.nvml
        if not nv.ok or not nv.has("nvmlDeviceGetPowerManagementLimit"):
            return None
        try:
            value = u32(0)
            status = nv.dll.nvmlDeviceGetPowerManagementLimit(nv.dev, ctypes.byref(value))
            return value.value if status == 0 and value.value > 0 else None
        except Exception:
            return None

    def set_power_limit_mw(self, mw):
        nv = self.nvml
        if not nv.ok or not nv.has("nvmlDeviceSetPowerManagementLimit"):
            return False, "SetPowerManagementLimit not available"
        if type(mw) not in (int, float):
            return False, "power limit must be a finite number in whole milliwatts"
        try:
            valid = math.isfinite(mw) and 0 < mw < (1 << 32) and int(mw) == mw
        except OverflowError:
            valid = False
        if not valid:
            return False, "power limit must be a finite number in whole milliwatts"
        mw = int(mw)
        # driver constraints if known, else a conservative sanity envelope
        mn = self.static.get("pl_min_mw", 50000)
        mx = self.static.get("pl_max_mw", 400000)
        if not (mn <= mw <= mx):
            return False, f"limit {mw/1000:g} W out of [{mn/1000:g}..{mx/1000:g}] W"
        if self.read_power_limit_mw() is None:
            return False, "configured power-limit readback is unavailable; no write issued"
        st = nv.dll.nvmlDeviceSetPowerManagementLimit(nv.dev, u32(mw))
        if st == 0:
            got = self.read_power_limit_mw()
            if got is None:
                return False, "power limit was sent, but configured-limit readback failed"
            if got != mw:
                return False, (f"power limit requested {mw/1000:g} W, but the driver "
                               f"reports a configured limit of {got/1000:g} W")
            return True, f"power limit configured to {got/1000:g} W"
        return False, f"power limit failed: {nv.errstr(st)}"

    def _supported_nvml_clocks(self, function, *selectors):
        """Read a complete variable-length NVML clock list, or return no list.

        RTX 5080 exposes 389 graphics clocks per performance memory row, more
        than the former 256-entry buffer. Query the required count first and
        allow bounded growth retries. Never use a truncated result or trust a
        returned count beyond the allocated buffer.
        """
        nv = self.nvml
        if not (nv.ok and nv.has(function)):
            return []
        maximum = 4096
        try:
            getter = getattr(nv.dll, function)
            count = u32(0)
            status = getter(nv.dev, *selectors, ctypes.byref(count), None)
            if status not in (0, 7):  # NVML_SUCCESS / NVML_ERROR_INSUFFICIENT_SIZE
                return []
            capacity = count.value
            for _ in range(3):
                if not 0 < capacity <= maximum:
                    return []
                values = (u32 * capacity)()
                count = u32(capacity)
                status = getter(nv.dev, *selectors, ctypes.byref(count), values)
                if status == 7:
                    if count.value <= capacity:
                        return []
                    capacity = count.value
                    continue
                if status != 0 or not 0 < count.value <= capacity:
                    return []
                clocks = list(values[:count.value])
                return sorted(set(clocks)) if all(clocks) else []
        except (AttributeError, OSError, TypeError, ValueError, OverflowError):
            return []
        return []

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
            g = self._supported_nvml_clocks("nvmlDeviceGetSupportedGraphicsClocks", u32(m))
            if g:
                out.append((m, g))
        return out

    def last_clock_lock_request(self):
        """The exact last successful frequency-lock command from this instance.

        This is the snapped range sent to the driver, not a physical-clock
        observation. A failed new request or successful release clears it.
        """
        return getattr(self, "_last_clock_lock_request", None)

    def lock_gpu_clocks(self, mn_mhz, mx_mhz):
        self._last_clock_lock_request = None
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
            self._last_clock_lock_request = (sn_mn, sn_mx)
            return True, f"GPU clock locked to [{sn_mn}..{sn_mx}] MHz{note}"
        return False, f"lock failed: {nv.errstr(st)} (needs admin)"

    def reset_gpu_clocks(self):
        return self._reset_gpu_clocks()

    # Exact board/firmware/driver combinations with force AND release measured.
    # These hold the top memory band, but reduce core clocks under load.
    LEGACY_P0_PROFILES = {
        (ARCH_MAXWELL, 0x1382, 0x6893103C, "472.12", "82.07.32.00.6a"):
            {"name": "GTX 745", "driver": "472.12",
             "held_core_mhz": 540, "boost_core_mhz": 1072},
        (ARCH_KEPLER, 0x1188, 0x84061043, "472.12", "80.04.1e.00.18"):
            {"name": "GTX 690", "driver": "472.12",
             "held_core_mhz": 705, "boost_core_mhz": 1201},
    }

    def legacy_p0_profile(self):
        """Measured behavior for this exact GPU/driver, or None if unverified."""
        api = getattr(self, "nvapi", None)
        if not (api and api.ok and getattr(api, "ForcePstate", None)
                and not getattr(self, "pairing_error", None)):
            return None
        card = getattr(api, "selected", None) or {}
        static = getattr(self, "static", {})
        key = (self.arch(), card.get("devid"), card.get("subsys"),
               static.get("driver"), str(static.get("vbios") or "").lower())
        profile = self.LEGACY_P0_PROFILES.get(key)
        return dict(profile) if profile is not None else None

    def legacy_p0_supported(self):
        """Only a measured board/firmware/driver combination may force P0."""
        return self.legacy_p0_profile() is not None

    def legacy_p0_owned(self):
        """Session ownership, not a claim to read another tuner's force state."""
        return getattr(self, "_legacy_p0_owned", False)

    def hold_legacy_p0(self):
        if not self.legacy_p0_supported():
            return False, "legacy P0 hold is not verified on this GPU/driver"
        try:
            status = self.nvapi.ForcePstate(self.nvapi.gpu, u32(0), u32(2))
        except Exception as exc:
            return False, f"P0 request failed: {exc}"
        if status != 0:
            return False, f"P0 request failed (NVAPI status {status})"
        # Track a successful request immediately, even if verification fails.
        # A failed rollback must remain releasable from the UI and on exit.
        self._legacy_p0_owned = True
        reason = "P0 and its top memory band were not observed"
        consecutive = 0
        try:
            for _ in range(20):
                data = self.read()
                top = data.get("mem_p0max")
                mem = data.get("mem")
                good = (data.get("pstate") == 0 and top is not None and top > 0
                        and mem is not None and mem >= top * 0.97)
                consecutive = consecutive + 1 if good else 0
                if consecutive >= 3:
                    return True, (f"P0 held; core {data.get('core', '?')} MHz, "
                                  f"memory {mem} MHz (not a maximum-core lock)")
                time.sleep(0.1)
        except Exception as exc:
            reason = f"P0 verification failed: {exc}"
        ok, message = self.release_legacy_p0()
        return False, reason + "; " + (message if ok else "RELEASE FAILED: " + message)

    def release_legacy_p0(self):
        if not self.legacy_p0_owned():
            return True, "this session owns no legacy P0 hold"
        try:
            status = self.nvapi.ForcePstate(self.nvapi.gpu, u32(16), u32(2))
        except Exception as exc:
            return False, f"P0 release failed: {exc}"
        if status != 0:
            return False, f"P0 release failed (NVAPI status {status})"
        # Automatic behavior may still be P0 under load. Waiting for P8 would
        # incorrectly report a failed release while a game is running.
        self._legacy_p0_owned = False
        return True, "P0 request released; automatic performance states restored"

    def _reset_gpu_clocks(self, allow_pascal_noop=False):
        nv = self.nvml
        if not nv.ok or not nv.has("nvmlDeviceResetGpuLockedClocks"):
            return False, "ResetGpuLockedClocks not available"
        st = nv.dll.nvmlDeviceResetGpuLockedClocks(nv.dev)
        if st == 0:
            self._last_clock_lock_request = None
            return True, "GPU clock lock released"
        # NVML documents this pair for Volta or newer. A stock reset on a
        # positively identified Pascal card has no such lock to release.
        # Direct Release keeps reporting the driver's failure, and every
        # other error (including permission/device loss) remains a failure.
        if st == 3 and allow_pascal_noop and self.arch() == self.ARCH_PASCAL:
            self._last_clock_lock_request = None
            return True, "NVML frequency lock is not applicable to Pascal; nothing to reset"
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
        if not self.vf_curve_applicable():
            return False
        a = self.nvapi
        return bool(a.ok and a.BoostLock and a.VfLockSet)

    def _vf_lock_read_raw(self):
        """The driver's OWN 780-byte lock buffer, or None if the getter did not
        answer. Every write path in this section starts here. Handing back a
        buffer the driver produced - rather than one we assembled from a struct
        definition - is what makes this setter safe, and it is how it was
        validated. Nothing below ever constructs a _ClockLock to write."""
        if not self.vf_curve_applicable():
            return None
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

    def read_vf_lock_status(self, domain=None):
        """Return (point lock, error), distinguishing unlocked from unreadable.

        An explicit domain checks only that target, so a second tuner's lock
        earlier in the table cannot hide this session's own request.

            {domain, lockMode, volt_uV, volt_mv, count}

        volt_mv is the voltage the lock was REQUESTED at, not the point the
        hardware resolved to - the driver stores the request verbatim (see the
        VF_LOCK_* block). Pass it through resolve_vf_point() to name the point
        actually held. What this call is authoritative about is WHETHER a lock
        is in force and WHOSE number is in it, which is how a concurrent tuner
        re-asserting its own value gets caught."""
        cl = self._vf_lock_read_raw()
        if cl is None:
            return None, "V/F lock getter failed; the current lock is unknown"
        entries = self._vf_lock_entries(cl)
        if domain is not None:
            entries = [e for e in entries if e.domain == domain]
            if not entries:
                return None, f"V/F lock domain {domain} is missing; its current state is unknown"
        for e in entries:
            # mode 3 ONLY - a mode-2 entry in this table is the NVML frequency
            # lock and its field is kHz, so reporting it here would hand the
            # caller 1350.00 "mV" for a 1350 MHz clock lock
            if e.lockMode == VF_LOCK_MODE_POINT:
                return {"domain": e.domain, "lockMode": e.lockMode,
                        "volt_uV": e.volt_uV, "volt_mv": e.volt_uV / 1000.0,
                        "count": cl.count}, None
        return None, None

    def read_vf_lock(self):
        """Current point lock, or None for unlocked/unreadable (legacy reader API).

        Ownership and cleanup decisions must use read_vf_lock_status(), because
        a failed getter cannot establish that an accepted write was released.
        """
        return self.read_vf_lock_status()[0]

    def read_clk_lock(self):
        """The driver's current NVML frequency lock as (min_mhz, max_mhz).

        R580 exposes mode-2 BoostLock records. R472 keeps this independent
        control in RM's performance-limit records instead. Neither path uses
        a remembered setter argument: another process's lock is visible too.
        None means no readable frequency lock, as with read_vf_lock.
        """
        cl = self._vf_lock_read_raw()
        if cl is None:
            return self._read_legacy_clk_lock()
        by_dom = {e.domain: e.volt_uV for e in self._vf_lock_entries(cl)
                  if e.lockMode == VF_LOCK_MODE_FREQ}
        if not by_dom:
            return self._read_legacy_clk_lock()
        hi = by_dom.get(CLK_LOCK_DOMAIN_MAX)
        lo = by_dom.get(CLK_LOCK_DOMAIN_MIN, hi)
        if hi is None:
            return self._read_legacy_clk_lock()
        return (lo // 1000, hi // 1000)

    def _read_legacy_clk_lock(self):
        # This private ABI is measured on the TITAN RTX with 472.12. Pascal's
        # NVML frequency-lock setter is unsupported on both tested drivers.
        if (self.static.get("driver") != "472.12"
                or not self.nvapi.ok
                or self.nvapi.selected.get("devid") != 0x1E02):
            return None
        records = self._legacy_clk_limit_records()
        if records is None or len(records) != 2:
            return None
        values = []
        for record, ident in zip(records, (0x4C, 0x4B)):
            # R472 GET returns 82 dwords per record. Mode 2 is an explicit
            # clock in kHz (unit 1); the final three words are the effective
            # value and may differ from this client's requested constraint.
            if (len(record) != 82 or record[0] != ident or record[1] != 2
                    or record[4] != 1 or not (0 < record[3] <= 10_000_000)):
                return None
            values.append(record[3] // 1000)
        return tuple(values) if values[0] <= values[1] else None

    def _legacy_clk_limit_records(self):
        """Read RM GET 0x20802077; never submit its paired SET command.

        The transport belongs to this NvAPI GPU/client and is captured once
        from its read-only voltage getter. Later polls call D3DKMTEscape
        directly, with fresh count/pointer/record buffers. A rejected cached
        transport is discarded. No DLL offsets or copied driver code ship.
        """
        transport = getattr(self, "_legacy_clk_transport", None)
        if transport is None:
            transport = self._capture_legacy_clk_transport()
            if transport is None:
                return None
            self._legacy_clk_transport = transport
        header, fields = transport
        records = ((u32 * 82) * 2)()
        records[0][0], records[1][0] = 0x4C, 0x4B
        address = ctypes.addressof(records)
        packet = (u32 * 21)(*header, 2, 0, address & 0xFFFFFFFF,
                            address >> 32)
        packet[2], packet[14], packet[15], packet[16] = 84, 0x20802077, 16, 0
        status = self._legacy_clk_escape(packet, fields)
        if status != 0 or packet[16] != 0 or packet[17] != 2:
            self._legacy_clk_transport = None
            return None
        return [list(record) for record in records]

    @staticmethod
    def _legacy_clk_gdi():
        # Optional on older Windows (notably EnumAdapters2 on Windows 7).
        # Check the complete readback path before installing a capture hook;
        # missing readback must not interrupt ordinary GPU monitoring.
        try:
            gdi = ctypes.WinDLL("gdi32.dll")
            functions = [getattr(gdi, name) for name in (
                "D3DKMTEnumAdapters2", "D3DKMTQueryAdapterInfo",
                "D3DKMTCloseAdapter", "D3DKMTEscape")]
        except (AttributeError, OSError):
            return None
        for function in functions:
            function.argtypes, function.restype = [ctypes.c_void_p], ctypes.c_long
        return gdi

    def _legacy_clk_escape(self, packet, fields):
        # NvAPI closes its borrowed adapter handle after the captured call.
        # Obtain our own handle by PCI address and close every enumerated
        # adapter in finally. Rechecking identity also handles device removal.
        class Adapter(ctypes.Structure):
            _fields_ = [("handle", u32), ("luid", u32 * 2),
                        ("sources", u32), ("preferred", i32)]

        class Adapters(ctypes.Structure):
            _fields_ = [("count", u32), ("items", ctypes.POINTER(Adapter))]

        class Query(ctypes.Structure):
            _fields_ = [("handle", u32), ("kind", u32),
                        ("data", ctypes.c_void_p), ("size", u32)]

        slot = self.nvapi.selected.get("slot", "")
        match = re.fullmatch(r"0+:([0-9a-fA-F]{2}):([0-9a-fA-F]{2})\.([0-7])", slot)
        if not match:
            return None
        location = tuple(int(value, 16) for value in match.groups())
        gdi = self._legacy_clk_gdi()
        if gdi is None:
            return None
        items = (Adapter * 16)()
        adapters = Adapters(16, items)
        if gdi.D3DKMTEnumAdapters2(ctypes.byref(adapters)) != 0:
            return None
        try:
            matches = []
            for item in items[:min(adapters.count, 16)]:
                address = (u32 * 3)()
                query = Query(item.handle, 6, ctypes.addressof(address), 12)
                if (gdi.D3DKMTQueryAdapterInfo(ctypes.byref(query)) == 0
                        and tuple(address) == location):
                    matches.append(item.handle)
            if len(matches) != 1:
                return None
            current = dict(fields, hAdapter=matches[0])
            escape = self._Escape(**current,
                                  pPrivateDriverData=ctypes.addressof(packet),
                                  PrivateDriverDataSize=ctypes.sizeof(packet))
            return gdi.D3DKMTEscape(ctypes.byref(escape))
        finally:
            for item in items[:min(adapters.count, 16)]:
                gdi.D3DKMTCloseAdapter(ctypes.byref(u32(item.handle)))

    def _capture_legacy_clk_transport(self):
        a = self.nvapi
        if not (a.ok and a.VoltRailsCtlGet):
            return None
        with GPU._RAIL_HOOK_LOCK:
            gdi = self._legacy_clk_gdi()
            if gdi is None:
                return None
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.VirtualProtect.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                                             u32, ctypes.POINTER(u32)]
            kernel.GetCurrentThreadId.restype = u32
            kernel.GetCurrentProcess.restype = ctypes.c_void_p
            kernel.FlushInstructionCache.argtypes = [ctypes.c_void_p,
                                                      ctypes.c_void_p,
                                                      ctypes.c_size_t]
            owner = kernel.GetCurrentThreadId()
            process = kernel.GetCurrentProcess()
            address = ctypes.cast(gdi.D3DKMTEscape, ctypes.c_void_p).value
            original = ctypes.string_at(address, 14)
            proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)
            found = []

            def restore():
                ctypes.memmove(address, original, 14)
                kernel.FlushInstructionCache(process, address, 14)

            def capture(ptr):
                restore()
                if ptr and kernel.GetCurrentThreadId() == owner:
                    escape = self._Escape.from_address(ptr)
                    if escape.pPrivateDriverData and escape.PrivateDriverDataSize == 716:
                        words = ctypes.cast(escape.pPrivateDriverData,
                                            ctypes.POINTER(u32))
                        if (words[2] == 716 and words[14] == 0x20803213
                                and words[15] == 648 and words[17] == 1):
                            found.append((list(words[:17]), {
                                key: getattr(escape, key) for key in
                                ("hAdapter", "hDevice", "Type", "Flags", "hContext")}))
                return proto(address)(ptr)

            callback = proto(capture)
            old = u32()
            if not kernel.VirtualProtect(address, 14, 0x40, ctypes.byref(old)):
                return None
            try:
                patch = (b"\xff\x25\0\0\0\0" + struct.pack(
                    "<Q", ctypes.cast(callback, ctypes.c_void_p).value))
                ctypes.memmove(address, patch, 14)
                kernel.FlushInstructionCache(process, address, 14)
                block = (u32 * (0xAC8 // 4))(0x10AC8, 1)
                status = a.VoltRailsCtlGet(a.gpu, ctypes.byref(block))
            finally:
                restore()
                previous = u32()
                kernel.VirtualProtect(address, 14, old.value,
                                      ctypes.byref(previous))
            return found[0] if status == 0 and len(found) == 1 else None

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
        if self.vf_lock_recovery_pending():
            return False, "V/F lock: the previous change needs recovery before another hold"
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
        previous = (int(target.lockMode), int(target.volt_uV))
        target.lockMode = VF_LOCK_MODE_POINT
        target.volt_uV = volt_uv
        cl.version = a.ver(_ClockLock, VF_LOCK_VERSION)  # re-stamp; keep the rest
        # Record cleanup before calling the setter: even a failed verification
        # must not turn an accepted write into a hold nobody owns on exit.
        self._vf_lock_recovery = {"domain": domain, "previous": previous,
                                  "requested": (VF_LOCK_MODE_POINT, volt_uv)}
        try:
            st = a.VfLockSet(a.gpu, ctypes.byref(cl))
            if st != 0:
                reason = f"V/F lock write failed (status {st}) - needs admin"
            else:
                back = self._vf_lock_read_raw()
                got = next((e for e in self._vf_lock_entries(back)
                            if e.domain == domain), None) if back is not None else None
                if got is not None and (got.lockMode, got.volt_uV) == (VF_LOCK_MODE_POINT, volt_uv):
                    self._vf_lock_recovery = None
                    return True, (f"V/F point lock set on domain {domain}, requested "
                      f"{volt_uv / 1000.0:.2f} mV - the hardware holds the highest "
                      f"V/F point at or below that")
                reason = ("V/F lock write returned OK but its verification read failed"
                          if back is None else
                          "V/F lock write returned OK but the requested lock was not read back")
        except Exception as exc:
            reason = f"V/F lock write/verification failed: {exc}"
        restored, message = self.recover_vf_lock()
        return False, reason + "; " + (message if restored else "RECOVERY REQUIRED: " + message)

    def vf_lock_recovery_pending(self):
        """Whether an unsuccessful hold still needs its prior state restored."""
        return getattr(self, "_vf_lock_recovery", None) is not None

    def recover_vf_lock(self):
        """Retry a failed hold's rollback without replaying an old whole buffer.

        Only restore this request's target mode/value. All other bytes come
        from a fresh GET, so an independent frequency lock or newer driver
        fields survive. A different target request belongs to another writer;
        refuse to overwrite it and retain the recovery record for the caller.
        """
        pending = getattr(self, "_vf_lock_recovery", None)
        if pending is None:
            return True, "no V/F lock recovery is pending"
        if not self._vf_lock_available():
            return False, "V/F lock recovery is unavailable"
        try:
            cl = self._vf_lock_read_raw()
            if cl is None:
                return False, "V/F lock recovery getter failed; refusing to write blind"
            target = next((e for e in self._vf_lock_entries(cl)
                           if e.domain == pending["domain"]), None)
            if target is None:
                return False, "V/F lock recovery target is missing from the current table"
            current = (target.lockMode, target.volt_uV)
            if current == pending["previous"]:
                self._vf_lock_recovery = None
                return True, "previous V/F lock state is confirmed restored"
            if current != pending["requested"]:
                return False, "V/F lock target changed concurrently; leaving its current request untouched"
            target.lockMode, target.volt_uV = pending["previous"]
            a = self.nvapi
            cl.version = a.ver(_ClockLock, VF_LOCK_VERSION)
            st = a.VfLockSet(a.gpu, ctypes.byref(cl))
            if st != 0:
                return False, f"V/F lock recovery write failed (status {st})"
            back = self._vf_lock_read_raw()
            got = next((e for e in self._vf_lock_entries(back)
                        if e.domain == pending["domain"]), None) if back is not None else None
            if got is None or (got.lockMode, got.volt_uV) != pending["previous"]:
                return False, "V/F lock recovery was sent but the previous state could not be verified"
            self._vf_lock_recovery = None
            return True, "previous V/F lock state restored and verified"
        except Exception as exc:
            return False, f"V/F lock recovery failed: {exc}"

    def clear_vf_lock(self, domain=None, expected_uv=None):
        """Release the V/F point lock: lockMode 0 on every locked entry, by the
        same read-modify-write.

        volt_uV is deliberately left as the driver has it. Mode 0 is what an
        unlocked entry reads back as anyway, and a later session cannot know
        what that field held before somebody locked it - inventing a value
        would be exactly the from-scratch write this section refuses to make.

        Mode-2 entries are left strictly alone. They are the NVML frequency
        lock sharing this table, and reset_gpu_clocks() owns those; clearing
        them from here would mean "release the V/F lock" quietly released the
        other mechanism too. An explicit domain/expected request releases only
        the hold the caller owns; Reset all deliberately leaves both unset.
        """
        a = self.nvapi
        if not self._vf_lock_available():
            return False, ("V/F point lock unavailable: 0xE440B867 / 0x39442CFB "
                           "did not both resolve")
        cl = self._vf_lock_read_raw()
        if cl is None:
            return False, "V/F lock: getter failed, refusing to write blind"
        entries = self._vf_lock_entries(cl)
        if domain is not None:
            entries = [e for e in entries if e.domain == domain]
            if not entries:
                return False, f"V/F lock domain {domain} is missing; refusing to assume it was released"
        held = [e for e in entries
                if e.lockMode == VF_LOCK_MODE_POINT]
        if domain is not None and expected_uv is not None and held and held[0].volt_uV != expected_uv:
            return True, "this V/F request is no longer held; the current target request was left untouched"
        if not held:
            if domain is None:
                self._vf_lock_recovery = None
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
        back = self._vf_lock_read_raw()
        if back is None:
            return False, "V/F lock release was sent, but the verification read failed"
        after = self._vf_lock_entries(back)
        if domain is not None:
            after = [e for e in after if e.domain == domain]
            if not after:
                return False, "V/F lock release target is missing from the verification read"
        if any(e.lockMode == VF_LOCK_MODE_POINT for e in after):
            return False, ("V/F lock release returned OK but the card still "
                           "reports a lock - another tool is re-asserting it")
        if domain is None:
            self._vf_lock_recovery = None
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

    def restore_fan_control_state(self, state):
        """Restore each fan's requested duty and policy using current bindings.

        Classic SetCoolerLevels always engages manual mode on the tested Xp,
        even if its policy field carries the original automatic policy. Auto
        must use RestoreCoolerSettings. ClientFanCoolers instead shares its
        complete getter/setter buffer, so retain all IDs and reserved fields.
        """
        current = self.read_fan_control_state()
        if not current or not isinstance(state, dict):
            return False, "fan control state unavailable"
        wanted = state.get("fans")
        # A partially exposed modern API must not hide a complete native
        # fallback. Map the same card's fan order across the two interfaces;
        # NVML indices are zero-based, while client cooler IDs need not be.
        if current["source"] == "nvml" and isinstance(wanted, list) and any(
                isinstance(row, dict) and not self.nvml.has(
                    "nvmlDeviceSetFanSpeed_v2" if row.get("manual") else
                    "nvmlDeviceSetDefaultFanSpeed_v2") for row in wanted):
            native = self._native_fan_data()
            if native:
                current = native[0]
        rows = current["fans"]
        if not isinstance(wanted, list) or len(wanted) != len(rows) or not rows:
            return False, "fan count changed; refusing to restore another layout"
        if state.get("source") not in ("nvml", "nvapi_cooler", "nvapi_client"):
            return False, "fan snapshot has an unknown control source"
        same_source = state.get("source") == current["source"]
        plan = []
        for old, now in zip(wanted, rows):
            if not isinstance(old, dict) or not isinstance(old.get("manual"), bool):
                return False, "fan snapshot has an unknown policy"
            if same_source and old.get("id") != now["id"]:
                return False, "fan IDs changed; refusing to restore another layout"
            manual, level = old["manual"], old.get("level")
            if isinstance(level, bool) or not isinstance(level, (int, float)) or \
                    not float(level).is_integer() or not (0 <= level <= 100):
                return False, "fan snapshot has an invalid requested level"
            if manual and not (now["min"] <= level <= now["max"]):
                return False, f"fan {now['id']} request outside [{now['min']}..{now['max']}]%"
            policy = (old.get("policy") if same_source else
                      (1 if manual else now["default_policy"]))
            if not manual and policy != now["default_policy"]:
                return False, "non-default automatic fan policy cannot be restored safely"
            plan.append((now["id"], int(level), bool(manual), policy))
        a, nv = self.nvapi, self.nvml
        errors = []
        if current["source"] == "nvml":
            for fan_id, level, manual, policy in plan:
                name = "nvmlDeviceSetFanSpeed_v2" if manual else "nvmlDeviceSetDefaultFanSpeed_v2"
                if not nv.has(name):
                    return False, f"{name} not available"
            for fan_id, level, manual, policy in plan:
                st = (nv.dll.nvmlDeviceSetFanSpeed_v2(nv.dev, fan_id, level) if manual else
                      nv.dll.nvmlDeviceSetDefaultFanSpeed_v2(nv.dev, fan_id))
                if st != 0:
                    errors.append(f"fan{fan_id}:{nv.errstr(st)}")
        elif current["source"] == "nvapi_cooler":
            if any(not getattr(a, "CoolerLevelsSet" if manual else "CoolerRestore", None)
                   for _, _, manual, _ in plan):
                return False, "NVAPI cooler controls unavailable"
            for fan_id, level, manual, policy in plan:
                if manual:
                    buf = _CoolerLevels(version=a.ver(_CoolerLevels, 1))
                    buf.entries[0].level, buf.entries[0].policy = level, 1
                    st = a.CoolerLevelsSet(a.gpu, fan_id, ctypes.byref(buf), 1)
                else:
                    indexes = (u32 * 1)(fan_id)
                    st = a.CoolerRestore(a.gpu, indexes, 1)
                if st != 0:
                    errors.append(f"fan{fan_id}:NVAPI status {st}")
        else:
            if not getattr(a, "FanCoolersSetControl", None):
                return False, "NVAPI client fan controls unavailable"
            native = self._native_fan_data()
            if not native or native[0]["source"] != "nvapi_client" or \
                    [r["id"] for r in native[0]["fans"]] != [r["id"] for r in rows]:
                return False, "fan controls changed during preparation"
            buf = native[1]
            for index, (fan_id, level, manual, policy) in enumerate(plan):
                buf.entries[index].level = level
                buf.entries[index].mode = 1 if manual else 0
            st = a.FanCoolersSetControl(a.gpu, ctypes.byref(buf))
            if st != 0:
                errors.append(f"NVAPI client fan status {st}")
        if errors:
            return False, "; ".join(errors)
        # Automatic requested levels can update asynchronously with temperature.
        # Manual requests and every policy must agree once the driver settles.
        # GTX 690 / R472 updates the requested cooler state on roughly a
        # one-second cadence. Keep checking the policy and level (not RPM),
        # allowing two seconds before declaring an accepted write unverified.
        for attempt in range(21):
            if current["source"] == "nvml":
                after = self.read_fan_control_state()
            else:
                native = self._native_fan_data()
                after = native[0] if native else None
            if after and after["source"] == current["source"] and \
                    len(after["fans"]) == len(plan) and all(
                    row["id"] == fan_id and row["manual"] == manual and
                    (row["level"] == level if manual else row["policy"] == policy)
                    for row, (fan_id, level, manual, policy) in zip(after["fans"], plan)):
                return True, "fan control state restored"
            if attempt < 20:
                time.sleep(0.1)
        return False, "fan write accepted but requested policy/level did not read back"

    def _set_nvapi_fans(self, pct=None):
        native = self._native_fan_data()
        if not native:
            return False, "NVAPI fan controls not available"
        state, _ = native
        for fan in state["fans"]:
            fan["manual"] = pct is not None
            fan["policy"] = 1 if pct is not None else fan["default_policy"]
            if pct is not None:
                fan["level"] = int(pct)
            elif state["source"] == "nvapi_client":
                fan["level"] = 0
        ok, msg = self.restore_fan_control_state(state)
        return (True, "fans returned to automatic" if pct is None else
                f"fans set to manual {int(pct)}%") if ok else (ok, msg)

    def set_fan(self, pct):
        nv = self.nvml
        if not nv.ok or not nv.has("nvmlDeviceSetFanSpeed_v2") or \
                not nv.has("nvmlDeviceGetNumFans"):
            return self._set_nvapi_fans(pct)
        pct = int(pct)
        floor = self.static.get("fan_min", 30)
        if pct < floor:
            return False, (f"fan {pct}% below hardware minimum {floor}% "
                           f"- use Auto for the zero-RPM idle curve")
        pct = max(0, min(100, pct))
        nf = u32(0)
        if not nv.has("nvmlDeviceGetNumFans") or \
                nv.dll.nvmlDeviceGetNumFans(nv.dev, ctypes.byref(nf)) != 0:
            if self._native_fan_data():
                return self._set_nvapi_fans(pct)
            return False, "fan count unavailable; cannot select fans to control"
        if not nf.value:
            return False, "no controllable fans reported"
        errs = []
        for f in range(nf.value):
            st = nv.dll.nvmlDeviceSetFanSpeed_v2(nv.dev, u32(f), u32(pct))
            if st != 0:
                errs.append(f"fan{f}:{nv.errstr(st)}")
        if not errs:
            return True, f"fans set to manual {pct}%"
        return False, "; ".join(errs) + " (needs admin)"

    def reset_fan(self):
        nv = self.nvml
        if not nv.ok or not nv.has("nvmlDeviceSetDefaultFanSpeed_v2") or \
                not nv.has("nvmlDeviceGetNumFans"):
            return self._set_nvapi_fans()
        nf = u32(0)
        if not nv.has("nvmlDeviceGetNumFans") or \
                nv.dll.nvmlDeviceGetNumFans(nv.dev, ctypes.byref(nf)) != 0:
            if self._native_fan_data():
                return self._set_nvapi_fans()
            return False, "fan count unavailable; cannot select fans to control"
        if not nf.value:
            return False, "no controllable fans reported"
        errs = []
        for f in range(nf.value):
            st = nv.dll.nvmlDeviceSetDefaultFanSpeed_v2(nv.dev, u32(f))
            if st != 0:
                errs.append(f"fan{f}:{nv.errstr(st)}")
        if not errs:
            return True, "fans returned to automatic"
        return False, "; ".join(errs)

    # ---- over-voltage % (AB "Core Voltage" slider) ------------------------ #
    def read_vcore_mv(self):
        """Just the NVVDD rail, as ONE NVAPI call.

        read() returns this too, but reaches NVML and a dozen other entry
        points to do it. This exists for the I2C offset verifier, which needs
        the GPU's own view of the rail sampled tens of times in a row and
        tightly interleaved with a PMBus read: the whole point of that
        measurement is that both numbers describe the same instant, and a
        full telemetry sweep between them defeats it.
        """
        a = self.nvapi
        if not (a.ok and a.VoltRailsStatus):
            return None
        vs = _VoltStatus(version=a.ver(_VoltStatus, 1))
        if a.VoltRailsStatus(a.gpu, ctypes.byref(vs)) == 0 and vs.value_uV:
            return vs.value_uV / 1000.0
        return None

    # ---- per-rail voltage limits ----------------------------------------- #
    # The rail limit block. Readable here; written by set_volt_rail_limits,
    # which does NOT go through NvAPI - see the end of this comment for why.
    #
    # LAYOUT, established here by probing, not from any third-party header:
    #   id 0xA3070DB0, version word 0x00020AC8 (v2, 2760 bytes)
    #   dw1 is an INPUT rail mask - 0x1 fills record 0, 0x2 record 1, 0x3 both.
    #   Records start at byte 0x48, stride 0x54, five known dwords each:
    #     +0x00 type   +0x04 reliability  +0x08 alt_reliability
    #     +0x0C overvoltage  +0x10 vmin
    #
    # The four limits are SIGNED MICROVOLT DELTAS from a fixed 1040 mV base,
    # not absolute ceilings. Confirmed by reconstruction: with the card held at
    # NVVDD 900/1150 and MSVDD 750/950, this block read NVVDD reliability +110
    # / vmin +100 and MSVDD reliability -90 / vmin -50, and 1040+110, 800+100,
    # 1040-90, 800-50 give back all four numbers exactly. NVVDD
    # alt_reliability read +90 => 1060, precisely the ceiling measured on this
    # card before the id was known.
    #
    # THE FACTORY STATE IS NOT ALL ZERO. On this GB203 the card powers up with
    # NVVDD at 0 and MSVDD reliability at -50000, i.e. NVVDD capped at the full
    # 1040 mV and MSVDD 50 mV lower at 990. Verified stable and identical
    # across two independent PnP device restarts, so a caller must NOT treat a
    # non-zero delta as evidence that something else has been writing.
    #
    # THE BLOCK IS VOLATILE DRIVER STATE. It is not in the adapter's registry
    # key and nothing re-applies it at boot, but it also does NOT clear on the
    # display-stack reset (Win+Ctrl+Shift+B). A PnP restart of the adapter
    # returns it to the factory values above.
    #
    # NO NVAPI EXPORT WRITES THIS BLOCK, and that was searched exhaustively
    # rather than assumed - which is why set_volt_rail_limits goes to RM
    # directly instead. The rails RM commands GET_CONTROL/SET_CONTROL
    # (0x2080B203 and 0x2080B204) do not occur anywhere in nvapi64.dll as
    # immediates. The unified command 0x2080F214 has exactly three owning
    # exports - 0x9C4BB8D0 (info), 0x2C73AFDC (status) and 0xA3070DB0 (this
    # one) - and every one of them reads. 0x5D0634EE, which sits between them
    # in the id table and accepts the same 2760-byte struct, also returns data
    # when called, so it is a fourth getter and not the setter its position
    # suggests: writes through it are accepted and applied nowhere, with a full
    # sweep of the header dwords and of every unused dword in a record, as
    # candidate "valid" masks, moving nothing. The vendor's published
    # ctrl2080volt.h carries no commands or structs at all.
    #
    # 0x5D0634EE being "only a getter" turned out to undersell it badly. What
    # it gets is the absolute, live rail state - see read_volt_rail_state.
    # Being uninteresting as a setter is not the same as being uninteresting.
    #
    # "No export" is not "no write path", and conflating the two is what kept
    # this read-only for longer than it needed to be.
    # The TITAN profiles are deliberately board/VBIOS/driver-specific. All
    # four fields were independently changed and restored on these adapters;
    # an accepted getter or an all-zero record alone proves no write support.
    # Their native RM getter reports type 2, but the type-5 F214 writes below
    # do apply. Both boards also held 1112.5 mV after raising the ceilings past
    # 1093.75 mV, in two A/B repetitions with a distinct V/F point at 1112.5.
    # See VOLTAGE-RAILS-TITAN.md and docs/rail-probes for the measurements and
    # restoration checks. Pascal vmin applies at idle but can be bypassed by
    # P2/V/F-point operation; it is not an unconditional live-voltage floor.
    # Reliability bases here are at ZERO boost. The absolute status header's
    # dw2 is the current boost contribution, not a fixed headroom value, and
    # the absolute reliability field includes that contribution.
    _TITAN_VOLT_RAIL_PROFILES = {
        (0x1E02, 312676574, "90.02.1e.00.02", "472.12"): {
            "bases": {"reliability": 1068.75, "alt_reliability": 1093.75,
                      "overvoltage": 1125.0, "vmin": 650.0},
            "headroom_mv": 25.0,
            "control_version": 0x00010AC8,
        },
        (0x1B02, 299831518, "86.02.3d.00.01", "472.12"): {
            # R470 measures a different zero-boost base/headroom from R580.
            "bases": {"reliability": 1068.75, "alt_reliability": 1093.75,
                      "overvoltage": 1200.0, "vmin": 650.0},
            "headroom_mv": 25.0,
            "control_version": 0x00010AC8,
            "settle_vmin": True,
        },
        (0x1E02, 312676574, "90.02.1e.00.02", "580.97"): {
            "bases": {"reliability": 1068.75, "alt_reliability": 1093.75,
                      "overvoltage": 1125.0, "vmin": 650.0},
            "headroom_mv": 25.0,
        },
        (0x1B02, 299831518, "86.02.3d.00.01", "580.97"): {
            "bases": {"reliability": 1062.5, "alt_reliability": 1093.75,
                      "overvoltage": 1200.0, "vmin": 650.0},
            "headroom_mv": 31.25,
        },
    }

    def _volt_rail_profile(self):
        """Known conversion/defaults for this exact adapter, or ``None``."""
        static = getattr(self, "static", {})
        selected = getattr(getattr(self, "nvapi", None), "selected", None) or {}
        key = (selected.get("devid"), selected.get("subsys"),
               str(static.get("vbios", "")).lower(), static.get("driver"))
        titan = self._TITAN_VOLT_RAIL_PROFILES.get(key)
        if titan is not None:
            return {"bases": {0: titan["bases"]},
                    "headroom_mv": {0: titan["headroom_mv"]},
                    "poweron": {0: (0, 0, 0, 0)},
                    "fields": {0: self.VOLT_LIMIT_FIELDS},
                    "control_version": titan.get("control_version", 0x00020AC8),
                    "settle_vmin": titan.get("settle_vmin", False),
                    "min_mv": 650.0,
                    "max_mv": self.VOLT_LIMIT_MAX_MV}
        if self.clkdom_is_blackwell():
            return {"bases": {r: dict(GPU.VOLT_LIMIT_BASE_MV) for r in (0, 1)},
                    "headroom_mv": {0: 20.0, 1: 20.0},
                    "poweron": {0: (0, 0, 0, 0), 1: (-50000, 0, 0, 0)},
                    "fields": {r: self.VOLT_LIMIT_FIELDS for r in (0, 1)},
                    "min_mv": 700.0, "max_mv": self.VOLT_LIMIT_MAX_MV}
        return None

    def _read_volt_rail_blocks(self, function, version):
        """Read present rails with singleton masks, cached per GPU/getter.

        Both TITANs reject mask 3 while accepting mask 1. Their supported
        control record is entirely zero at stock, including its discriminator,
        so record contents must never be used to infer an absent rail.
        """
        a = self.nvapi
        fn = getattr(a, function, None)
        if not (a.ok and fn):
            return {}
        cache = getattr(self, "_volt_rail_masks_cache", None)
        if cache is None:
            cache = self._volt_rail_masks_cache = {}
        candidates = cache.get(function, (0, 1))
        versions = getattr(self, "_volt_rail_read_versions", None)
        if versions is None:
            versions = self._volt_rail_read_versions = {}
        # R470 exposes the same NVAPI record fields at V1. Retry only after
        # INCOMPATIBLE_STRUCT_VERSION, not after an absent-rail error.
        requested_versions = (versions[function],) if function in versions else (
            (version, 0x00010AC8) if function == "VoltRailsCtlGet"
            and version == 0x00020AC8 else (version,))
        out = {}
        for rail in candidates:
            for candidate_version in requested_versions:
                buf = (ctypes.c_ubyte * 8192)()
                pu = ctypes.cast(buf, ctypes.POINTER(u32))
                pu[0], pu[1] = candidate_version, 1 << rail
                status = fn(a.gpu, ctypes.byref(buf))
                if status == 0 and pu[0] == candidate_version:
                    out[rail] = buf
                    versions[function] = candidate_version
                    break
                if status != -9:
                    break
        if function not in cache:
            cache[function] = tuple(out)
        return out

    def volt_rail_limits_supported(self):
        """Whether this card has a measured limit-write/defaults profile.

        Readability stays independent: a new GPU can report its live rails
        without inheriting another board's writes or reset values.
        """
        profile = self._volt_rail_profile()
        if profile is None:
            return False
        cur = self.read_volt_rail_limits()
        return cur is not None and set(cur) == set(profile["poweron"])

    def volt_rail_limit_fields(self, rail):
        """Limit fields with measured effects on this rail, or no fields."""
        profile = self._volt_rail_profile()
        return tuple((profile or {}).get("fields", {}).get(rail, ()))

    def read_volt_rail_limits(self):
        """Per-rail voltage limits as millivolt deltas, or ``None``.

        Returns ``{rail_index: {"type", "reliability", "alt_reliability",
        "overvoltage", "vmin"}}`` with the four limits in millivolts, signed.
        Each record also carries ``_base_mv`` and ``_headroom_mv`` from this
        card's validated profile. The class conversion helpers consume those
        values; an unknown card's bases remain unknown rather than inheriting
        GB203's. Rail 0 is NVVDD; rail 1, where present, is MSVDD.

        For absolute values, and for the live rail voltage, use
        read_volt_rail_state instead. These deltas are what gets written; those
        absolutes are what the card reports back independently.
        """
        blocks = self._read_volt_rail_blocks("VoltRailsCtlGet", 0x00020AC8)
        profile = self._volt_rail_profile()
        # R470 V1 also returns success for absent masks, with empty records.
        # Its independent absolute getter rejects those masks. Cross-check
        # there rather than applying the newer driver's zero-record rule.
        legacy_present = None
        if any(ctypes.cast(b, ctypes.POINTER(u32))[0] == 0x00010AC8
               for b in blocks.values()):
            legacy_present = set(self.read_volt_rail_state() or {})
        out = {}
        for rail, buf in blocks.items():
            if legacy_present is not None and rail not in legacy_present:
                continue
            pi = ctypes.cast(buf, ctypes.POINTER(i32))
            base = (0x48 + rail * 0x54) // 4
            out[rail] = {
                "type": pi[base],
                "reliability": pi[base + 1] / 1000.0,
                "alt_reliability": pi[base + 2] / 1000.0,
                "overvoltage": pi[base + 3] / 1000.0,
                "vmin": pi[base + 4] / 1000.0,
                "_base_mv": dict((profile or {}).get("bases", {}).get(rail, {})),
                "_headroom_mv": (profile or {}).get("headroom_mv", {}).get(rail),
            }
        return out or None

    # ---- the live rail block --------------------------------------------- #
    # A DIFFERENT id and a different block from the limits above, and the
    # distinction is the whole point of it. 0xA3070DB0 stores signed deltas, so
    # reading it back returns what we wrote and proves storage, never effect.
    # This one reports ABSOLUTE microvolts the card computed for itself, plus a
    # LIVE per-rail voltage, so a write can finally be checked against a number
    # we did not supply.
    #
    # LAYOUT, recovered by probing:
    #   id 0x5D0634EE (RM 0x2080B213), version word 0x00010AC8 (v1, 2760 bytes)
    #   dw1 is the INPUT rail mask, same convention as the limit block
    #   records at byte 0x48, stride 0x54, all fields UNSIGNED ABSOLUTE uV:
    #       +0x00 type   1 = NVVDD, 3 = MSVDD
    #       +0x04 live voltage
    #       +0x08 reliability      +0x0C alt_reliability
    #       +0x10 overvoltage      +0x14 effective     +0x18 vmin
    # A v2 also exists at 0x00021620 (5664 bytes, records 0xA0/0x14C stride
    # 0xAC). v1 carries everything we use, so v1 is what we ask for.
    #
    # WHY THE LIVE FIELD IS A MEASUREMENT AND NOT A CONSTANT THAT LOOKS RIGHT:
    # rail 0 tracked read_vcore_mv exactly across clock locks, 800000 ->
    # 910000 uV, which is the positive control. Then each rail was clamped
    # ALONE: clamping MSVDD moved only rail 1 (to 850.0, then 900.0) while
    # rail 0 held at 910.0, and clamping NVVDD moved only rail 0 while rail 1
    # held at 915.0. Two independent sensors, and at stock they disagree -
    # 910.0 against 915.0 - so rail 1 is not rail 0 wearing another offset.
    #
    # What this does NOT establish is whether the number is ADC-sensed or the
    # commanded setpoint. Both behave identically under a clamp, and no
    # experiment here separates them, so callers should say "live" and not
    # "measured at the rail".
    #
    # A NOTE ON THE OLD NEGATIVE. 0x2C73AFDC was written off as static
    # description data because nothing in it moved under load. It was being
    # called at v1 (0x00010ACC), which populates nothing but the version and a
    # count; a v2 exists (0x0002184C, 6220 bytes) that does fill records. That
    # conclusion was an artifact of the struct version, not a property of the
    # card - which is why version discovery now runs before any such claim.
    LIVE_RAIL_VER = 0x00010AC8
    LIVE_RAIL_BASE = 0x48
    LIVE_RAIL_STRIDE = 0x54
    LIVE_RAIL_FIELDS = ("type", "live", "reliability", "alt_reliability",
                        "overvoltage", "effective", "vmin")

    def read_volt_rail_state(self):
        """Per-rail live voltage and absolute limits, or ``None``.

        Returns ``{rail: {"type", "live", "reliability", "alt_reliability",
        "overvoltage", "effective", "vmin"}}`` in millivolts, ABSOLUTE. Unlike
        read_volt_rail_limits these are not deltas and need no base applied.
        """
        blocks = self._read_volt_rail_blocks("VoltRailsAbs", self.LIVE_RAIL_VER)
        out = {}
        for rail, buf in blocks.items():
            pu = ctypes.cast(buf, ctypes.POINTER(u32))
            base = (self.LIVE_RAIL_BASE + rail * self.LIVE_RAIL_STRIDE) // 4
            rec = {}
            for n, key in enumerate(self.LIVE_RAIL_FIELDS):
                v = pu[base + n]
                rec[key] = v if key == "type" else v / 1000.0
            # THE INDEX IDENTIFIES THE RAIL, not the type field. type is
            # decoded and returned so a caller can check it - on this card it
            # reads 1 for NVVDD and 3 for MSVDD - but it is deliberately not
            # used to key the result. The limit block and the write path both
            # address rails positionally, and a reader that keyed off type
            # while the writer keyed off index could disagree about which rail
            # is which, which is the one disagreement that must never happen
            # here. Positional everywhere, and the discriminator exposed.
            #
            # An all-zero record means the mask selected a rail this card does
            # not have. Reporting 0.0 mV as a live voltage would be worse than
            # reporting nothing.
            if rec["live"] or rec["reliability"]:
                out[rail] = rec
        return out or None

    def read_rail_live_mv(self, rail):
        """One rail's live voltage in millivolts, or ``None``."""
        state = self.read_volt_rail_state()
        return (state or {}).get(rail, {}).get("live")

    # The base every delta above is measured from. Not read from the card -
    # nothing exposes it - but pinned by the reconstruction in the comment on
    # read_volt_rail_limits, where four independent settings all resolved
    # against 1040 mV for the ceilings and 800 mV for the floors.
    # alt_reliability is based at 1060, NOT 1040 like the other ceilings. The
    # 1060 base is pinned by measurement: a +93 delta held 1145 mV, which a
    # 1040 base cannot produce because it would cap at 1133, below the point
    # the card was observed holding.
    # overvoltage is based at 1200, and that was WRONG here as 1040 until the
    # absolute block above made it checkable. Requesting 1000 produced an
    # absolute limit of 1160 mV, 900 -> 1060, 850 -> 1010 - each exactly
    # +160 mV above what a 1040 base predicts, on both rails, which is the gap
    # between 1040 and 1200. The error was invisible for as long as the only
    # readback was the delta we had just written.
    VOLT_LIMIT_BASE_MV = {"reliability": 1040.0, "alt_reliability": 1060.0,
                          "overvoltage": 1200.0, "vmin": 800.0}

    # THE TWO CEILINGS ARE NOT THE SAME KNOB, and treating them as one is a
    # real regression rather than a harmless simplification. Measured under
    # load, boost 0% vs 100%, on this card:
    #
    #     reliability / alt      boost 0     boost 100
    #     1040 / 1060 (stock)    1040 mV     1060 mV
    #     1150 / 1060            1060 mV     1060 mV
    #     1040 / 1150            1040 mV     1060 mV
    #     1150 / 1150            1145 mV     1145 mV
    #
    # All eight points fit one rule:
    #
    #     cap = min(reliability + boost% * headroom, alt_reliability)
    #
    # so reliability is the base the voltage-boost slider climbs FROM, and
    # alt_reliability is a hard clamp over the result. Row 2 is why writing
    # reliability alone does nothing, and row 4 is why writing BOTH to the
    # requested ceiling makes the boost slider inert: 0% and 100% then land on
    # the same volt.
    #
    # The headroom is the VBIOS over-voltage allowance and is exactly the gap
    # between the two bases, so it is derived rather than hardcoded.
    @classmethod
    def volt_boost_headroom_mv(cls, fields=None):
        """What 100% voltage boost is worth, in millivolts."""
        if fields is not None and "_headroom_mv" in fields:
            value = fields["_headroom_mv"]
            return float("nan") if value is None else value
        return (cls.VOLT_LIMIT_BASE_MV["alt_reliability"]
                - cls.VOLT_LIMIT_BASE_MV["reliability"])

    # ---- writing the limits ---------------------------------------------- #
    # NvAPI has no export that writes this block, but RM does implement the
    # write - the two facts are not the same thing, and conflating them cost a
    # long detour. Command 0x2080F214 with POPULATED records is the setter,
    # established by sweeping the VOLT command space and watching which one
    # moved the rails. It is not reachable through NvAPI's own code: NvAPI
    # sends the request under 0x2080B213 (a read) and marshals only the rail
    # mask, so both the command and the rail data have to be supplied here.
    # Everything else in the request is NvAPI's own and correctly versioned;
    # only two fields and the record array are ours.
    #
    # RM's params (1036 bytes = a 3-dword header + 32 records of 8 dwords,
    # which is exactly 0x40C) were recovered by differential reads - asking for
    # rail 0 alone, then rail 1 alone, and seeing which dwords moved:
    #     dw17  valid-rail mask (out)   dw18  requested mask (in)
    #     dw20  first record, stride 8: +0 type(5)  +1 reliability_uV
    #                                   +2..+5 the other limits  +7 flag(1)
    # These offsets are into the ESCAPE payload, not into the NvAPI struct;
    # the two layouts are unrelated and must not be mixed up.
    #
    # NOTHING VALIDATES THE VALUE. A 1500 mV ceiling is accepted and reads
    # straight back, so the bound below is Druta's and the only one there is.
    # User-selected request bounds, not measured hardware maxima. XOC raises
    # the software ceiling; only live readback establishes what a card uses.
    VOLT_LIMIT_MAX_MV = 1200.0
    VOLT_LIMIT_XOC_MAX_MV = 1500.0
    RAIL_OFFSET_MAX_MV = 200.0
    RAIL_OFFSET_XOC_MAX_MV = 500.0
    voltage_xoc_enabled = False
    VOLT_LIMIT_MIN_MV = 700.0
    # Off unless something deliberately turns it on, exactly like the rail-1
    # gate. A slider must not be able to set this by itself.
    volt_limits_write_enabled = False

    _ESC_B213, _ESC_F214 = 0x2080B213, 0x2080F214
    _ESC_REC0, _ESC_STRIDE = 20, 8

    # The R470 layout was captured from a voltage-boost identity write.
    # Tuple: packet/params sizes, commands, mask/boost words, record start,
    # record stride/type, optional valid word. Every offset is in dwords.
    _RAIL_WRITE_LAYOUTS = {
        0x00020AC8: (1104, 1036, 0x2080B213, 0x2080F214, 18, 19, 20, 8, 5, 7),
        0x00010AC8: (716, 648, 0x20803213, 0x20803214, 17, 18, 19, 5, 1, None),
    }
    _RAIL_HOOK_LOCK = threading.RLock()

    class _Escape(ctypes.Structure):
        _fields_ = [("hAdapter", u32), ("hDevice", u32), ("Type", u32),
                    ("Flags", u32), ("pPrivateDriverData", ctypes.c_void_p),
                    ("PrivateDriverDataSize", u32), ("hContext", u32)]

    def _write_rail_records(self, records):
        # The code patch is process-wide even when GPU objects have different
        # per-card locks. Only one installer may own it at a time.
        with GPU._RAIL_HOOK_LOCK:
            profile = self._volt_rail_profile() or {}
            settle = False
            boost = None
            if profile.get("settle_vmin"):
                previous = self.read_volt_rail_limits() or {}
                settle = any(len(values) == 4 and rail in previous
                             and round(previous[rail]["vmin"] * 1000) != values[3]
                             for rail, values in records.items())
                if settle:
                    boost = self.read_voltage_boost()
            result = self._write_rail_records_locked(records)
            if settle and result == (True, 0):
                # GP102/R470 recomputes an idle floor using the preceding
                # stored value. A verified identity re-send makes its live
                # voltage catch up, including when restoring the stock floor.
                # No extra delta is added and every requested field is equal.
                _back, error = self._verify_rail_records(records, boost)
                if error:
                    return False, result[1]
                result = self._write_rail_records_locked(records)
            return result

    def _write_rail_records_locked(self, records):
        """Issue one rails-control WRITE. Returns (ok, RM status).

        The hook is one-shot: it restores the original bytes before calling
        through, so the real function is what runs and there is no trampoline
        to build - which also means no instruction-length decoding and no
        disassembler in the shipped bundle. The caller serializes installers;
        a callback on another native thread restores and forwards the getter
        without substituting a write to that thread's potentially different GPU.
        """
        if not self.volt_rail_limits_supported():
            return False, None
        profile = self._volt_rail_profile()
        control_version = profile.get("control_version", 0x00020AC8)
        transport = self._RAIL_WRITE_LAYOUTS.get(control_version)
        if transport is None:
            return False, None
        (packet_size, params_size, get_command, set_command, mask_word,
         boost_word, record0, record_stride, record_type, valid_word) = transport
        if not records or set(records) != set(profile["poweron"]):
            return False, None
        if any(len(vals) != len(self.VOLT_LIMIT_FIELDS)
               for vals in records.values()):
            return False, None
        boost = self.read_voltage_boost()
        if boost is None:
            return False, None
        rail_mask = sum(1 << rail for rail in records)
        a = self.nvapi
        gdi = ctypes.WinDLL("gdi32.dll")
        addr = ctypes.cast(gdi.D3DKMTEscape, ctypes.c_void_p).value
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.VirtualProtect.argtypes = [ctypes.c_void_p, ctypes.c_size_t, u32,
                                       ctypes.POINTER(u32)]
        k32.GetCurrentThreadId.argtypes = []
        k32.GetCurrentThreadId.restype = u32
        owner_thread = k32.GetCurrentThreadId()
        orig = bytes((ctypes.c_ubyte * 14).from_address(addr))
        proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)
        state = {"status": None, "done": False}

        def restore():
            ctypes.memmove(addr, orig, 14)

        def cb(pesc):
            hit = None
            try:
                if (pesc and not state["done"]
                        and k32.GetCurrentThreadId() == owner_thread):
                    e = GPU._Escape.from_address(pesc)
                    if (e.pPrivateDriverData
                            and e.PrivateDriverDataSize == packet_size):
                        pu = ctypes.cast(e.pPrivateDriverData,
                                         ctypes.POINTER(u32))
                        pi = ctypes.cast(e.pPrivateDriverData,
                                         ctypes.POINTER(i32))
                        if (pu[14] == get_command and pu[15] == params_size
                                and pu[mask_word] == rail_mask):
                            pu[14] = set_command
                            pu[mask_word] = rail_mask
                            # This header field is the voltage boost percent.
                            # Zeroing it during a rail write silently clears
                            # the user's independent Core Voltage setting.
                            pu[boost_word] = int(boost)
                            for r, vals in records.items():
                                b = record0 + r * record_stride
                                pi[b] = record_type
                                for k, v in enumerate(vals):
                                    pi[b + 1 + k] = int(v)
                                if valid_word is not None:
                                    pi[b + valid_word] = 1
                            hit = pu
                            state["done"] = True
            except Exception:
                pass
            restore()                     # real bytes back before calling
            rc = proto(addr)(pesc)
            if hit is not None:
                state["status"] = hit[16]
            return rc

        keep = proto(cb)
        old = u32()
        if not k32.VirtualProtect(ctypes.c_void_p(addr), 14, 0x40,
                                  ctypes.byref(old)):
            return False, None
        patch = (b"\xFF\x25\x00\x00\x00\x00"
                 + struct.pack("<Q", ctypes.cast(keep, ctypes.c_void_p).value))
        ctypes.memmove(addr, patch, 14)
        try:
            buf = (ctypes.c_ubyte * 8192)()
            ctypes.memset(buf, 0, 8192)
            p = ctypes.cast(buf, ctypes.POINTER(u32))
            p[0], p[1] = control_version, rail_mask
            a.VoltRailsCtlGet(a.gpu, ctypes.byref(buf))
        finally:
            restore()
            k32.VirtualProtect(ctypes.c_void_p(addr), 14, old,
                               ctypes.byref(old))
        return state["done"], state["status"]

    # The four limits, in the order they sit in an RM record.
    VOLT_LIMIT_FIELDS = ("reliability", "alt_reliability", "overvoltage",
                         "vmin")

    def _verify_rail_records(self, records, boost):
        """Check every preserved rail/field and the independent boost setting."""
        back = self.read_volt_rail_limits()
        if back is None or set(back) != set(records):
            return None, "write issued but the rail read-back is incomplete"
        for rail, wanted in records.items():
            got = [int(round(back[rail][k] * 1000))
                   for k in self.VOLT_LIMIT_FIELDS]
            if got != wanted:
                return None, (f"rail {rail} read-back disagrees: wanted "
                              f"{wanted}, got {got}")
        if self.read_voltage_boost() != boost:
            return None, "rail write changed the voltage boost setting"
        return back, None

    def set_volt_rail_limits(self, rail, **limits):
        """Set one rail's limits, each in ABSOLUTE millivolts against its base.

        Keywords are the names in VOLT_LIMIT_FIELDS. Every limit is a SEPARATE
        field and is written only if named here - nothing is derived from
        anything else. That is deliberate: `reliability` and `alt_reliability`
        interact (see the measured table above), and a single synthetic
        "ceiling" knob would have to pick one mapping of that interaction and
        hide the rest. Showing both and letting the caller decide keeps the
        hardware legible; a wrapper that wants one number can compose these two
        calls itself and say so.

        Returns (ok, message). Every write is read back and compared before it
        is called a success - but note what that does and does not prove: this
        block stores a request verbatim, so a matching read-back means the
        request was stored, never that the card will honour it. Only
        rail_ceiling_mv, or a rail measurement under load, answers the second.
        """
        if not self.volt_limits_write_enabled:
            return False, ("rail limit writes are disabled: nothing bounds "
                           "this value but Druta, so it is off by default")
        if not self.volt_rail_limits_supported():
            return False, "rail limit writes are not validated for this GPU/driver"
        unknown = set(limits) - set(self.VOLT_LIMIT_FIELDS)
        if unknown:
            return False, f"not a rail limit: {', '.join(sorted(unknown))}"
        cur = self.read_volt_rail_limits()
        if cur is None:
            return False, "cannot read the current limits"
        if rail not in cur:
            return False, f"rail {rail} is not present on this GPU"
        if set(limits) - set(self.volt_rail_limit_fields(rail)):
            return False, f"one or more fields are not validated for rail {rail}"
        boost = self.read_voltage_boost()
        if boost is None:
            return False, "cannot preserve the current voltage boost"
        recs = {r: [int(round(cur[r][k] * 1000))
                    for k in self.VOLT_LIMIT_FIELDS] for r in cur}
        for key, mv in limits.items():
            if mv is None:
                continue
            maximum = (self.VOLT_LIMIT_XOC_MAX_MV if self.voltage_xoc_enabled
                       else self.VOLT_LIMIT_MAX_MV)
            # Leaving XOC keeps existing values and permits lowering them,
            # but cannot raise an already above-normal value any further.
            maximum = max(maximum, self.abs_limit_mv(cur[rail], key))
            if not (self.VOLT_LIMIT_MIN_MV <= mv <= maximum):
                return False, (f"{key} {mv:.0f} mV is outside Druta's "
                               f"{self.VOLT_LIMIT_MIN_MV:.0f}-"
                               f"{maximum:.0f} mV bound")
            recs[rail][self.VOLT_LIMIT_FIELDS.index(key)] = int(round(
                (mv - cur[rail]["_base_mv"][key]) * 1000))
        ok, status = self._write_rail_records(recs)
        if not ok:
            return False, "the rails request was not seen - nothing was written"
        if status:
            return False, f"driver refused the write (NV_STATUS 0x{status:X})"
        back, error = self._verify_rail_records(recs, boost)
        if error:
            return False, error
        # Report the fields that were written AND what they add up to, because
        # the second is not obvious from the first: raising reliability alone
        # can leave the reachable maximum exactly where it was.
        wrote = ", ".join(f"{k} {self.abs_limit_mv(back[rail], k):.0f}"
                          for k in sorted(limits))
        note = ""
        if "vmin" in limits and self.nvapi.selected.get("devid") == 0x1B02:
            note = ("; Pascal vmin was verified at idle; a V/F point lock or "
                    "P2 can hold the live voltage below this floor")
        return True, (f"{_RAIL_NAME[rail]}: {wrote} mV stored; floor "
                      f"{self.rail_floor_mv(back[rail]):.0f}, cap at 100% boost "
                      f"{self.rail_ceiling_mv(back[rail]):.0f} mV{note}")

    @classmethod
    def abs_limit_mv(cls, fields, key):
        """One limit as an absolute voltage. Each has its OWN base."""
        bases = fields.get("_base_mv", cls.VOLT_LIMIT_BASE_MV)
        return fields[key] + bases.get(key, float("nan"))

    @classmethod
    def rail_ceiling_mv(cls, fields):
        """The configured ceiling at 100% boost, not a promised live voltage.

        DERIVED, not a field. Measured under load across four configurations:

            cap = min(reliability + boost% * headroom, alt_reliability)

        so the reachable maximum is reliability plus the whole boost headroom,
        clamped by alt_reliability. Reporting either field on its own is what
        made a 1153 mV write look applied while the card sat at 1060.

        OVERVOLTAGE IS A THIRD CLAMP, added once the absolute block made the
        effective limit readable. The card's own "effective" field equals
        min(reliability, alt_reliability, overvoltage): holding overvoltage at
        1060 left the effective limit at 1040, and dropping it to 1010 pulled
        the effective limit down to 1010 with both ceilings untouched. It sits
        at 1200 mV from the factory on both rails, so it is inert until
        somebody moves it - which is exactly why leaving it out of this
        calculation went unnoticed.

        MSVDD's bases are no longer an extrapolation. The absolute block
        reports both rails directly, and its numbers reconcile: MSVDD's stock
        990 mV reliability is the shared 1040 base plus the -50 mV delta the
        card ships with, and its alt_reliability reads 1060 like NVVDD's.

        `fields` is one rail's dict of millivolt DELTAS, as
        read_volt_rail_limits returns it.
        """
        return min(cls.abs_limit_mv(fields, "reliability")
                   + cls.volt_boost_headroom_mv(fields),
                   cls.abs_limit_mv(fields, "alt_reliability"),
                   cls.abs_limit_mv(fields, "overvoltage"))

    @classmethod
    def rail_floor_mv(cls, fields):
        return cls.abs_limit_mv(fields, "vmin")

    def stock_limit_mv(self, rail, key):
        """Known power-on value, independent of normal/XOC request bounds.

        Stock remains the measured board default even when the user permits
        requests up to 1200 mV normally or 1500 mV in XOC mode.
        """
        profile = self._volt_rail_profile()
        if profile is None or rail not in profile["poweron"]:
            return None
        return (profile["bases"][rail][key]
                + profile["poweron"][rail][self.VOLT_LIMIT_FIELDS.index(key)]
                / 1000.0)

    # This card's power-on deltas, in microvolts, in VOLT_LIMIT_FIELDS order.
    # NOT all zero: MSVDD ships 50 mV below NVVDD, so zeroing both would RAISE
    # the MSVDD ceiling rather than restore it.
    VOLT_LIMIT_POWERON = {0: (0, 0, 0, 0), 1: (-50000, 0, 0, 0)}

    def reset_volt_rail_limits(self, rail=None, fields=None):
        """Put limits back to this card's power-on values.

        `rail` None means both; `fields` None means all four. Both are narrowed
        rather than assumed, because a per-knob Stock button that resets the
        whole block silently discards settings on the OTHER rail - which is
        exactly what it did before this took arguments.

        Deliberately NOT gated on volt_limits_write_enabled. That gate exists to
        stop a slider raising a ceiling by itself; this call only ever returns
        one to the power-on value. Refusing it because "writes are disabled"
        would strand a card on limits the user is trying to clear - the exact
        stickiness that made this feature necessary.
        """
        if not self.volt_rail_limits_supported():
            return False, "rail reset defaults are not validated for this GPU/driver"
        profile = self._volt_rail_profile()
        cur = self.read_volt_rail_limits()
        if cur is None:
            return False, "cannot read the current limits"
        rails = tuple(cur) if rail is None else (rail,)
        if any(r not in cur for r in rails):
            return False, "requested rail is not present on this GPU"
        keys = tuple(fields) if fields else self.VOLT_LIMIT_FIELDS
        bad = set(keys) - set(self.VOLT_LIMIT_FIELDS)
        if bad:
            return False, f"not a rail limit: {', '.join(sorted(bad))}"
        boost = self.read_voltage_boost()
        if boost is None:
            return False, "cannot preserve the current voltage boost"
        recs = {r: [int(round(cur[r][k] * 1000))
                    for k in self.VOLT_LIMIT_FIELDS] for r in cur}
        for r in rails:
            for k in keys:
                i = self.VOLT_LIMIT_FIELDS.index(k)
                recs[r][i] = profile["poweron"][r][i]
        ok, status = self._write_rail_records(recs)
        if not ok or status:
            return False, f"reset refused (NV_STATUS 0x{status or 0:X})"
        _back, error = self._verify_rail_records(recs, boost)
        if error:
            return False, error
        what = ("rail limits" if rail is None and not fields
                else f"{_RAIL_NAME[rails[0]]} "
                     + (", ".join(keys) if fields else "limits"))
        return True, f"{what} back to the power-on values"

    def volt_rail_limits_mv(self):
        """The same limits resolved to absolute millivolts, or ``None``."""
        raw = self.read_volt_rail_limits()
        if raw is None:
            return None
        return {rail: {k: self.abs_limit_mv(fields, k)
                       for k in self.VOLT_LIMIT_FIELDS}
                for rail, fields in raw.items()}

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
        if not self.vf_curve_applicable():
            return None, "V/F curves are not applicable on this GPU"
        a = self.nvapi
        if not (a.ok and a.VfpCurve and a.BoostTableGet):
            return None, "VF curve APIs unavailable"
        lay = self.vfp_layout()
        if lay is None:
            return None, getattr(self, "_vfp_layout_error", "") or (
                "could not determine this card's VF table layout")
        cv = _VfpCurve(version=a.ver(_VfpCurve, 1))
        _set_point_masks(cv, lay.n_entries)
        st = a.VfpCurve(a.gpu, ctypes.byref(cv))
        if st != 0:
            self._vfp_layout_cache = None
            return None, f"curve read failed (status {st})"
        err = self._vfp_curve_error(cv, lay.n_entries)
        if err:
            self._vfp_layout_cache = None
            self._vfp_layout_error = err
            return None, err
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
    @staticmethod
    def _vfp_curve_error(cv, n):
        """Reject successful API calls containing an incomplete curve.

        On GP102, a driver can accept all 84 mask bits but return only a
        450 mV / 278 kHz placeholder. Caching that as a one-point GPU layout
        leaves the editor stuck even if a later driver read recovers.
        All requested rows must carry actual voltage/frequency data before
        their boundary or frequency scale can be inferred.
        """
        valid = sum(1 for e in cv.entries[:n]
                    if e.volt_uV > 0 and e.freq_kHz >= 1000)
        if n < 2 or valid != n:
            return (f"driver returned an incomplete V/F curve "
                    f"({valid} valid rows of {n}); press Read curve to retry")
        return None

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
        3. WHAT SCALE THE FREQUENCY IS IN - the measured Pascal/Turing formats
           use doubled/direct graphics clocks respectively, regardless of a
           tune. For an unmapped architecture, compare the top GPU row against
           the driver's own gfx_max as the original layout probe did.

        Returns None if the curve APIs are unavailable or nothing answers."""
        if not self.vf_curve_applicable():
            return None
        cached = getattr(self, "_vfp_layout_cache", None)
        if cached is not None and not force:
            return cached
        self._vfp_layout_cache = None
        self._vfp_layout_error = ""
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
        err = self._vfp_curve_error(cv, n)
        if err:
            self._vfp_layout_error = err
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

        if len(gpu_idx) < 2:
            self._vfp_layout_error = (
                "driver returned fewer than two GPU V/F points; "
                "press Read curve to retry")
            return None

        gfx_max = self.static.get("gfx_max")
        gset = set(gpu_idx)
        top_raw = max((f for i, _mv, f in rows if i in gset), default=0) / 1000.0
        freq_div = {self.ARCH_PASCAL: 2, self.ARCH_TURING: 1}.get(self.arch())
        if freq_div is None:
            freq_div = 2 if gfx_max and top_raw > gfx_max * 1.5 else 1

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
        """Raise points in [lo_mv, cap_mv] by whole bins on the supplied grid.

        Aim for one distinct frequency per voltage point without lowering any
        existing frequency. Each increment is rounded down relative to that
        point's current frequency; a ceiling can leave ties where no whole bin
        fits. `delivered` reports the predicted distinct operating points.
        Returns (changes, ceil_before_mhz, ceil_after_mhz, meta), the same shape
        as compute_deflatten.

        max_khz is an optional explicit planning ceiling. The regular ramp
        and Max it leave it unset: NVML's supported-clock list maximum is not
        a V/F overclock ceiling and must not constrain this planner.

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

        NEVER BELOW STOCK. Each rung first targets max(its own frequency, one
        grid step above the previous planned rung). Apply the optional ceiling,
        then round the nonnegative increment down to whole bins. Existing
        frequencies above that ceiling are preserved. Rounded Pascal clock
        readings need not share one exact grid phase, so this may retain a
        smaller stock gap rather than propose a fractional-bin adjustment
        that Apply would discard.

        This replaced a uniform descent - every rung one bin below the one above
        it, anchored at khz[floor] + (rungs-1)*grid - which fixed the slope at
        one step per point no matter what stock did. Anywhere stock climbed
        faster, the ramp fell behind and never caught up: a 33-rung band from
        1004 mV on the 7.5 MHz grid topped out at 2932 MHz where stock already
        held 3157. A 225 MHz demotion, from a feature whose whole purpose is to
        ask for more clock.

        WHAT IT COSTS. Nothing, at any rung, by construction - no point is ever
        planned below the frequency it already holds. floor_cost_mhz is retained
        in meta and is now always zero; callers that reported it keep working.
        The price moved to the other side of the ledger: every rung that IS
        lifted asks for more clock at its voltage than stock did, so the
        granularity fix and the overclock remain one edit and each rung still
        has to be stable in its own right.

        AND WHAT THE DRIVER THEN DOES TO IT. The delta table takes the plan
        verbatim; the evaluated curve does not (VF_MAX_RISE_KHZ,
        evaluate_curve_law), so meta still reports `delivered`, the number of
        DISTINCT operating points the band will really have, beside `rungs`, the
        number asked for. The first is what this feature is judged on.

        The clipped-floor pathology that used to dominate this paragraph is
        gone with the descent that caused it: the bottom rung is now stock[L]
        exactly, so it cannot land under the untouched point beneath the band
        and cannot be raised back onto it as one flat. `lifted_below` is kept
        because the law still applies in general - a floor more than 45 MHz
        above the point beneath drags it up - but a floor that equals stock
        cannot open a gap stock did not already have.

        Points BELOW lo_mv are left untouched, for the reason compute_deflatten
        gives: the low-voltage floor is many points pinned at the minimum clock,
        and ramping them means demanding high clocks at tiny voltages.

        Points ABOVE the cap are left untouched. Levelling them onto the top
        rung used to make the cap the park point, but it did so by DEMOTING
        them, and a planner that never places a rung below stock cannot make an
        exception for the points above the band. The cap now bounds the band
        rather than the card: the curve keeps rising past it and the arbiter
        parks at the highest point the rail can actually reach.

        The granularity and the overclock are the SAME edit: each lifted rung
        demands more clock at its voltage than before, so each must be stable
        in its own right."""
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

        # A RUNG IS NEVER PLACED BELOW ITS OWN STOCK FREQUENCY.
        #
        #     new[i] = max(stock[i], new[i-1] + grid)
        #
        # is the initial target, walked upward through the band. The ceiling
        # and per-point whole-bin quantization below can retain ties, but can
        # never demote an existing point.
        #
        # The previous shape was a UNIFORM descent from an anchor: every rung
        # exactly one grid step below the one above it, with the top pinned at
        # khz[floor] + (rungs-1)*grid. That fixed the ramp's slope at one step
        # per point regardless of what stock did, so anywhere stock climbed
        # faster than the grid the ramp fell behind it and stayed behind. On
        # this card's curve, a 33-rung band from 1004 mV on the 7.5 MHz grid
        # topped out at 2932 MHz where stock already held 3157 - a 225 MHz
        # DEMOTION issued by a feature whose entire purpose is to ask for more
        # clock. The flat top was removed and the whole band went backwards.
        #
        # Following stock wherever stock is steeper fixes it: in those regions
        # the rung IS the stock value and the plan costs nothing, and the grid
        # step only does work where stock is flat - which is precisely the
        # region a ramp exists to break up.
        #
        # THE DRIVER'S MAX RISE IS SATISFIED WITHOUT CHECKING IT. A step is
        # either grid (trivially under the limit) or stock[i] - new[i-1], and
        # since new[i-1] >= stock[i-1] that is at most stock[i] - stock[i-1],
        # a gap the stock curve already carries and the driver already accepts.
        #
        # It also retires the whole clipped-floor problem. The bottom rung is
        # stock[L] exactly, so it can no longer land under the untouched point
        # beneath the band, which is what used to make the shape law raise the
        # lower rungs back onto that point as one flat. The band no longer has
        # to be shrunk from the bottom to avoid it, and the floor costs zero.
        plan = {}
        prev = None
        for i in band:
            want = khz[i] if prev is None else max(khz[i], prev + grid)
            if max_khz is not None and want > max_khz:
                want, meta["clamped"] = int(max_khz), True
            # Each delta must advance by whole bins from THIS point's read
            # frequency. Pascal's evaluated clocks have rounded 12.5/13 MHz
            # gaps, while the nominal grid is 12.657 MHz. Subtracting those
            # directly produced 157 kHz edits that Apply's re-phase erased.
            # Quantize here, before the preview/prediction, using the same
            # downward rule. Never lower an existing point to meet a ceiling.
            bins = max(0, (want - khz[i]) // grid)
            want = khz[i] + bins * grid
            plan[i] = want
            prev = want
        top = plan[B]

        new = {}
        for i in band:
            if khz[i] != plan[i]:
                new[i] = plan[i]
        # POINTS ABOVE THE CAP ARE LEFT ALONE. They used to be levelled onto
        # the top rung, which made the cap point the lowest-voltage member of
        # the peak flat and therefore the park point. That levelling was a
        # DEMOTION - up to 210 MHz off a single point on this card's curve -
        # and "never plan a rung below stock" does not get an exception for
        # the points nobody looked at.
        #
        # It also bought nothing here. The cap sits above what the rail can
        # reach - measured, this one saturates near 1150 mV while the cap is
        # set around 1206 - so every point it demoted was unreachable, and
        # demoting an unreachable point cannot move the park point.
        #
        # The trade is real and worth stating: on a card whose rail CAN climb
        # past the cap, levelling was what made the voltage cap bound the CARD
        # rather than just the plan. Without it the cap bounds the band, the
        # curve keeps rising above it, and the arbiter parks at the highest
        # point the rail actually reaches. That is the behaviour asked for.
        floor_after = plan[L]
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
            # The cap can be the park point only when it reaches the predicted
            # whole-curve peak and no lower-voltage point shares that frequency.
            # A ceiling can retain ties inside the band; untouched points above
            # the cap can also remain higher than its planned top.
            "unique": (real[pos[B]] == max(real)
                       and all(real[pos[i]] < real[pos[B]]
                               for i in range(n)
                               if points[i]["volt_mv"] < points[B]["volt_mv"])),
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
        physical_grid = self.clock_step_khz()
        lay = self.vfp_layout()
        if lay is None:
            return False, "could not determine this card's VF delta units"
        grid = physical_grid * lay.freq_div
        new, _phase = GPU.compute_rephase(
            {p["idx"]: p["delta_khz"] for p in pts}, grid)
        gm = physical_grid / 1000.0
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
        if not self.vf_curve_applicable():
            return False, "V/F curves are not applicable on this GPU"
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
        # The per-domain offsets are a THIRD mechanism: neither set_clock_offset
        # above nor the curve reset below touches them, so a reset that skipped
        # this would report a stock card while XBAR was still carrying an
        # offset - and this button's own text promises it zeroes the offsets.
        # Only emitted where the control block answers and the domain is
        # actually carrying something, so the ordinary reset does not grow a
        # step that always says "nothing to do".
        layout = self.clkdom_layout() if self.clkdom_ok() else None
        if layout is not None:
            rows, err = self.read_clk_domain_offsets()
            if err or not rows:
                steps.append(ResetStep("clock-domain offsets", (False,
                    f"clock-domain offsets were not reset: {err or 'current values unreadable'}")))
            else:
                for d in sorted(rows):
                    if rows[d]["freq_khz"]:
                        steps.append(ResetStep(
                            f"{CLKDOM_NAMES.get(d, f'domain {d}')} offset",
                            self.set_clk_domain_offset(d, 0)))
            # The rail offset is a THIRD thing again - not a clock offset and
            # not the voltage boost - so it needs its own step or a reset would
            # leave the card carrying volts nothing on screen accounts for.
            if layout.nvvdd_uv is not None:
                rail = self.read_rail_offset_mv(0)
                if rail is None:
                    steps.append(ResetStep("core rail offset", (False,
                        "core rail offset was not reset: current value unreadable")))
                elif rail:
                    steps.append(ResetStep("core rail offset",
                                           self.set_rail_offset_mv(0, 0)))
        # The rail LIMITS are a fourth mechanism, and they outlive the app: they
        # are driver state that a reboot does not clear and that the display
        # reset does not touch. Leaving them out would make Druta exactly as
        # sticky as the tool whose leftovers we had to clear with a PnP restart,
        # which is the complaint that started this work. Only emitted when they
        # are actually off the power-on values, so a stock card does not carry a
        # pointless step.
        # COMPARED AGAINST VOLT_LIMIT_POWERON, field by field, on both rails.
        # This used to be a hand-written literal for rail 0 plus two spot
        # checks on rail 1 - its vmin and its reliability - which left MSVDD's
        # alt_reliability and overvoltage unexamined. A clamp on either of
        # those produced no reset step at all, so "reset to stock complete"
        # was reported over a rail still carrying it, on driver state that a
        # reboot does not clear. That gap was unreachable from the UI until
        # the overvoltage knobs shipped, and MSVDD overvoltage is precisely
        # what one of them writes.
        #
        # Compare only known card-specific defaults. Readable telemetry on
        # an unvalidated board must never dispatch a Blackwell-default reset.
        profile = self._volt_rail_profile()
        cur = self.read_volt_rail_limits() if profile is not None else None
        poweron = profile["poweron"] if profile is not None else {}
        if profile is not None and (cur is None or set(cur) != set(poweron)):
            steps.append(ResetStep("rail limits", (False,
                "rail limits were not reset: current values unreadable or incomplete")))
            cur = None
        off_stock = False
        for rail, fields in (cur or {}).items():
            want = poweron.get(rail)
            if not want:
                continue
            for n, key in enumerate(self.VOLT_LIMIT_FIELDS):
                if abs(fields[key] - want[n] / 1000.0) > 1e-6:
                    off_stock = True
                    break
        if off_stock:
            steps.append(ResetStep("rail limits",
                                   self.reset_volt_rail_limits()))
        if self.static.get("pl_def_mw"):
            steps.append(ResetStep(
                "power limit", self.set_power_limit_mw(self.static["pl_def_mw"])))
        else:
            steps.append(ResetStep(
                "power limit",
                (False, "power limit: default unknown, left unchanged")))
        steps.append(ResetStep(self.LOCK_STEP,
                               self._reset_gpu_clocks(allow_pascal_noop=True)))
        if self.legacy_p0_owned():
            steps.append(ResetStep(self.P0_LOCK_STEP, self.release_legacy_p0()))
        # The V/F point lock is a DIFFERENT mechanism: reset_gpu_clocks does not
        # touch it, so a reset that stopped at the step above would report a
        # clean card while this one still pinned it. clear_vf_lock distinguishes
        # an unreadable buffer from a validated unlocked one; read_vf_lock
        # alone returns None for both cases.
        if self._vf_lock_available():
            steps.append(ResetStep(self.VF_LOCK_STEP, self.clear_vf_lock()))
        steps.append(ResetStep("fan", self.reset_fan()))
        if self.nvapi.ok and self.nvapi.VoltCtrlGet and self.nvapi.VoltCtrlSet:
            steps.append(ResetStep("voltage boost", self.set_voltage_boost(0)))
        if self.vf_curve_applicable() and self.nvapi.ok and self.nvapi.BoostTableSet:
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
    delta_mhz = 5
    if "--delta" in sys.argv:
        pos = sys.argv.index("--delta")
        if pos + 1 >= len(sys.argv):
            print("--delta requires an integer MHz value")
            sys.exit(2)
        try:
            delta_mhz = int(sys.argv[pos + 1])
        except ValueError:
            print("--delta requires an integer MHz value")
            sys.exit(2)
    if "--clkdom-debug" in sys.argv:
        report = g.clkdom_debug_report()
        if "--json" in sys.argv:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        sys.exit(0 if report.get("layout") else 2)
    if "--clkdom-map-probe" in sys.argv:
        report = g.clkdom_mapping_probe(
            delta_mhz=delta_mhz, confirm=("--confirm" in sys.argv))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        sys.exit(0 if not report.get("error") else 2)
    if "--clkdom-field-probe" in sys.argv:
        report = g.clkdom_mapping_probe(
            delta_mhz=delta_mhz, confirm=("--confirm" in sys.argv),
            freq_fields=CLKDOM_BLACKWELL_FREQ_CANDIDATES)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        sys.exit(0 if not report.get("error") else 2)
    if "--clkdom-control-probe" in sys.argv:
        controls = list(CLKDOM_BLACKWELL_SAFE_SCAN_CONTROLS)
        if "--include-core-memory" in sys.argv:
            controls.extend(CLKDOM_BLACKWELL_RISKY_SCAN_CONTROLS)
        report = g.clkdom_mapping_probe(
            delta_mhz=delta_mhz, confirm=("--confirm" in sys.argv),
            controls=controls)
        report["probe"] = ("all-accepted-controls"
                           if "--include-core-memory" in sys.argv
                           else "safe-controls-excluding-0-and-2")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        sys.exit(0 if not report.get("error") else 2)
    print(_fmt_snapshot(g))


# Serialize every driver-touching entry point (RLock => nested calls are fine).
for _m in ("read", "read_clock_domains", "read_vf_curve", "apply_vf_deltas",
           "reset_vf_curve",
           "rephase_deltas", "set_clock_offset", "set_power_limit_mw", "read_power_limit_mw",
           "lock_gpu_clocks", "reset_gpu_clocks", "set_fan", "reset_fan",
           "hold_legacy_p0", "release_legacy_p0",
           "read_fan_control_state", "restore_fan_control_state",
           "read_fan_manual", "fan_capabilities",
           "read_vf_lock", "read_vf_lock_status", "read_clk_lock", "set_vf_lock", "clear_vf_lock",
           "recover_vf_lock",
           "vf_lock_self_test",
           "set_voltage_boost", "read_voltage_boost", "reset_all",
           "clkdom_debug_report", "clkdom_mapping_probe"):
    setattr(GPU, _m, _synchronized(getattr(GPU, _m)))
