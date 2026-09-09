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

"""Direct PMBus/SMBus control of a board's voltage regulator.

THE SPLIT, AND WHY IT IS WHERE IT IS. A profile in i2c/*.toml supplies DATA:
which device, which registers, what encoding, what bounds. Everything that
decides whether a write is ALLOWED lives here, in code. A profile chooses which
registers and what limits; it never chooses whether it is checked.

That line is not stylistic. Profiles are meant to be written by users and passed
between them, which makes a profile untrusted input arriving on the one path in
Druta with no firmware underneath it. If the guard structure were data, a file
downloaded from a forum could disable the identity check, whitelist an EEPROM
commit, or raise the typo ceiling - and it would look exactly like a helpful
profile for your card while doing it. So the guards are not reachable from the
file format at all.

WHY THIS PATH IS THE DANGEROUS ONE. Every other write in Druta submits a REQUEST
the GPU's firmware may clamp, and the ~1093.75 mV reliability ceiling is a real
backstop. This module does not go through that path. It talks to the regulator,
so the ceiling is not on this road: measured here, VOUT_OFFSET +150 mV put the
rail at 1178 mV while the GPU still believed 1050 and did not compensate.

REACHED FROM USERMODE - ring 3, via NVAPI. What makes it work is topology, not
privilege: the regulator is a separate IC that obeys whoever holds the bus. On
many stock boards that bus is not routed to the GPU at all, which is why a
profile is inert until its identity read passes on the actual hardware.
"""
import ctypes
import math
import os
import sys
import time
import tomllib
from .paths import app_dir, resource_path

u8, u32 = ctypes.c_uint8, ctypes.c_uint32
PTR, P8 = ctypes.c_void_p, ctypes.POINTER(u8)

I2C_READ_EX = 0x4D7B0709
I2C_WRITE_EX = 0x283AC65A
VER3 = 0x00030040

PROFILE_FORMAT = 1

# ---- guards that no profile can reach ---------------------------------------- #
# NON-VOLATILE COMMANDS. Refused whatever a profile says, because these are the
# ones with no undo: they commit the regulator's current state - including
# whatever a bad write just put there - to EEPROM, or set a one-shot password
# that locks the bus until the card loses 12 V. A profile's own never_write list
# ADDS to this; nothing subtracts from it.
NEVER_WRITE = {
    0x11: "STORE_DEFAULT_ALL - commits to non-volatile memory",
    0x12: "RESTORE_DEFAULT_ALL",
    0x13: "STORE_DEFAULT_CODE",
    0x14: "RESTORE_DEFAULT_CODE",
    0x15: "STORE_USER_ALL - commits the live state to EEPROM",
    0x16: "RESTORE_USER_ALL",
    0x17: "STORE_USER_CODE",
    0x18: "RESTORE_USER_CODE",
    0x04: "MFR_USER_PWD on MPS parts - one-shot PMBus write lockout",
}

# A typo catcher, not an envelope, and the one bound that survives every mode.
# A ring-0 vendor tool in this class accepted 14000 mV on a CPU rail and put it
# straight through; the part did not survive. 14000 is 1400 with a slipped
# digit, and no sub-zero run on any part needs 14 V.
SANITY_MAX_ABS_OFFSET_MV = 2000.0

XOC_CONFIRM = "I ACCEPT PERMANENT HARDWARE DAMAGE"

VERIFY_SAMPLES = 9
VERIFY_SETTLE_S = 0.15
# Observe a full second rather than one short burst of VRM telemetry.
VERIFY_MEASUREMENT_SAMPLES = 25
VERIFY_MEASUREMENT_INTERVAL_S = 0.04


class ProfileError(ValueError):
    pass


class _V3(ctypes.Structure):
    _fields_ = [("version", u32), ("displayMask", u32), ("bIsDDCPort", u8),
                ("i2cDevAddress", u8), ("pbI2cRegAddress", P8),
                ("regAddrSize", u32), ("pbData", P8), ("cbSize", u32),
                ("i2cSpeed", u32), ("i2cSpeedKhz", u32), ("portId", u8),
                ("bIsPortIdSet", u32)]


# ---- encodings ---------------------------------------------------------------- #
def _bitspec(s):
    """'7:0' -> (hi, lo). A field, which is NOT the transaction width.

    Kept separate because conflating the two is the likeliest way to write a
    broken profile, and it has already happened once here: the MP2888A command
    table gives VOUT_OFFSET a 2-byte transaction while only bits 7:0 hold the
    value, so writing it as 16 bits made every NEGATIVE offset fail read-back
    and undervolting was silently impossible.
    """
    if s is None:
        return None
    try:
        hi, lo = (int(x) for x in str(s).split(":"))
    except Exception:
        raise ProfileError(f"bits must look like '7:0', got {s!r}")
    if not (0 <= lo <= hi <= 31):
        raise ProfileError(f"bits {s!r} out of range")
    return hi, lo


def _extract(raw, bits):
    if bits is None:
        return raw
    hi, lo = bits
    return (raw >> lo) & ((1 << (hi - lo + 1)) - 1)


def _signed(v, width):
    return v - (1 << width) if v & (1 << (width - 1)) else v


def _linear11(raw):
    m, e = raw & 0x7FF, (raw >> 11) & 0x1F
    if m >= 1024:
        m -= 2048
    if e >= 16:
        e -= 32
    return m * (2.0 ** e)


def _decode(raw, enc, bits, scale):
    v = _extract(raw, bits)
    if enc == "linear11":
        return _linear11(v)
    if enc == "int":
        v = _signed(v, (bits[0] - bits[1] + 1) if bits else 16)
    return v * scale


# ---- the profile: data only --------------------------------------------------- #
class Profile:
    """A parsed i2c/*.toml. Validated on construction; inert until matched."""

    def __init__(self, d, path=""):
        self.path = path
        self.src = d
        p = d.get("profile") or {}
        if p.get("format") != PROFILE_FORMAT:
            raise ProfileError(f"profile.format must be {PROFILE_FORMAT}, "
                               f"got {p.get('format')!r}")
        self.name = str(p.get("name") or os.path.basename(path))
        self.regulator = str(p.get("regulator") or "unknown regulator")
        self.rail = str(p.get("rail") or "?")
        self.provenance = d.get("provenance") or {}

        bus = d.get("bus") or {}
        self.port = int(bus.get("port", 0))
        self.addrs = ([int(bus["addr7"])] if "addr7" in bus
                      else [int(a) for a in bus.get("addr7_probe", [])])
        if not self.addrs:
            raise ProfileError("bus needs addr7 or addr7_probe")
        self.addr7 = self.addrs[0]

        self.identity = d.get("identity") or []
        if not self.identity:
            raise ProfileError(
                "a profile must declare at least one [[identity]] read. "
                "Address alone does not identify a device, and this is the "
                "check that stops a profile being applied to the wrong board.")
        # A fingerprint is a real check but a weaker claim, and the UI says so
        # rather than presenting it as an ID register.
        self.weak_id = all(bool(i.get("fingerprint")) for i in self.identity)

        self.telemetry_specs = d.get("telemetry") or []
        if not any(t.get("key") == "vout_mv" for t in self.telemetry_specs):
            raise ProfileError(
                "a profile must expose telemetry key 'vout_mv' - it is what "
                "the verifier watches and what the UI shows instead of the "
                "number on the slider.")

        # A profile with NO [[write]] is legal and is READ-ONLY. That is the
        # first rung for a contributor: a board whose regulator answers and
        # whose output voltage decodes is worth shipping as telemetry long
        # before anyone has characterised its offset register. Such a profile
        # cannot write by construction rather than by policy - there is no
        # register for it to write - which makes it the safest thing to accept
        # from someone whose board nobody here owns.
        wr = d.get("write") or []
        self.write = next((w for w in wr if w.get("key") == "offset_mv"), None)
        self.read_only = self.write is None
        # Paged controllers need their PAGE/scaling identity gates around every
        # operation. Read-only profiles already use this; existing unpaged
        # writable profiles keep their established polling cost unless opted in.
        self.runtime_checks = self.read_only or bool(p.get("runtime_checks", False))
        if self.read_only:
            self.wreg = self.wbytes = self.wbits = None
            self.lsb_mv = self.raw_min = self.raw_max = None
        else:
            self.wreg = int(self.write["reg"])
            self.wbytes = int(self.write.get("bytes", 1))
            self.wbits = _bitspec(self.write.get("bits"))
            self.lsb_mv = float(self.write.get("lsb_mv", 6.25))
            self.raw_min = int(self.write["raw_min"])
            self.raw_max = int(self.write["raw_max"])
            if self.raw_min > self.raw_max:
                raise ProfileError("raw_min > raw_max")

        # Whitelist. Only registers named by a [[write]] are ever writable, and
        # the profile's own hazards are merged with the built-in denylist.
        self.writable = {int(w["reg"]) for w in wr}
        self.never = dict(NEVER_WRITE)
        for n in d.get("never_write") or []:
            self.never[int(n["reg"])] = str(n.get("why") or "named by profile")
        clash = self.writable & set(NEVER_WRITE)
        if clash:
            raise ProfileError(
                f"profile whitelists register(s) {[hex(c) for c in clash]} "
                f"that are permanently denied: "
                f"{'; '.join(NEVER_WRITE[c] for c in sorted(clash))}. A "
                f"profile cannot grant itself this.")

        lim = d.get("limits") or {}
        self.env_min = float(lim.get("envelope_min_mv", -200.0))
        self.env_max = float(lim.get("envelope_max_mv", 100.0))
        self.ceiling = float(lim.get("rail_ceiling_mv", 1200.0))
        self.sanity_rail = float(lim.get("sanity_max_rail_mv", 2000.0))
        band = lim.get("plausible_rail_mv") or [400.0, 1300.0]
        self.plausible = (float(band[0]), float(band[1]))

        v = d.get("verify") or {}
        self.rungs = tuple(float(x) for x in
                           v.get("rungs_mv", (6.25, 12.5, 25.0, 50.0, 75.0)))
        # Legacy min_loaded_vout_mv metadata is ignored; voltage is not a load detector.
        self.deadband_mv = v.get("expect_deadband_mv")

        m = d.get("match") or {}
        self.pci_device = {str(x).lower() for x in m.get("pci_device", [])}
        self.pci_subsys = {str(x).lower() for x in m.get("pci_subsys", [])}

    @property
    def hw_min_mv(self):
        return None if self.read_only else self.raw_min * self.lsb_mv

    @property
    def hw_max_mv(self):
        return None if self.read_only else self.raw_max * self.lsb_mv

    def candidate_for(self, dev_id=None, subsys=None):
        """Require known matching PCI ids, then verify identity on the bus."""
        if self.pci_device:
            if dev_id is None or f"0x{dev_id:04x}" not in self.pci_device:
                return False
        if self.pci_subsys:
            if subsys is None or f"0x{subsys:08x}" not in self.pci_subsys:
                return False
        return True


def profile_dirs():
    """Where profiles live, most user-editable first.

    Beside the executable comes FIRST because that is the directory a user can
    actually open - the PyInstaller bundle directory is an implementation
    detail and asking someone to drop a file into _internal is asking them not
    to bother.
    """
    out = []
    if getattr(sys, "frozen", False):
        out.append(str(app_dir() / "i2c"))
    out.append(str(resource_path("i2c")))
    return out


def load_profiles(log=None):
    """Every readable profile. A broken one is skipped and named, never fatal."""
    seen, out = set(), []
    for d in profile_dirs():
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".toml") or fn.lower() in seen:
                continue
            # Authoring aids are not profiles. TEMPLATE.toml is structurally
            # valid on purpose - a contributor needs it to load in an editor
            # and to be checkable - but it describes no board, and a blank
            # identity read at address 0x00 must never reach a real bus.
            if fn.upper().startswith("TEMPLATE") or fn.startswith("_"):
                continue
            seen.add(fn.lower())
            path = os.path.join(d, fn)
            try:
                with open(path, "rb") as f:
                    out.append(Profile(tomllib.load(f), path))
            except Exception as e:                              # noqa: BLE001
                if log:
                    log(f"i2c profile {fn} ignored: "
                        f"{type(e).__name__}: {e}", False)
    return out


# ---- the guarded device ------------------------------------------------------- #
class Rail:
    """One regulator on one board, behind every guard.

    XOC is per-instance rather than module-global: the old module-level flag was
    process-wide mutable state that a second code path could leave set.
    """

    def __init__(self, profile, nvapi, addr7=None):
        self.p = profile
        self.nvapi = nvapi
        self.addr7 = profile.addr7 if addr7 is None else addr7
        self.xoc = False

    # -- transport ------------------------------------------------------------ #
    def _mk(self, cmd, buf, n):
        regb = (u8 * 1)(cmd)
        s = _V3()
        ctypes.memset(ctypes.byref(s), 0, ctypes.sizeof(s))
        s.version = VER3
        s.i2cDevAddress = self.addr7 << 1
        s.pbI2cRegAddress = ctypes.cast(regb, P8)
        s.regAddrSize = 1
        s.pbData = ctypes.cast(buf, P8)
        s.cbSize = n
        s.i2cSpeed = 0xFFFF
        s.portId = self.p.port
        s.bIsPortIdSet = 1
        return s, regb

    def read(self, cmd, n):
        """Raw read. Read-only for any register, so it needs no whitelist."""
        fn = self.nvapi._i(I2C_READ_EX, PTR, PTR, PTR)
        if fn is None:
            return None
        buf = (u8 * n)()
        s, regb = self._mk(cmd, buf, n)
        unk = (u32 * 2)()
        st = fn(self.nvapi.gpu, ctypes.byref(s), ctypes.byref(unk))
        _keep = (regb, buf, unk)                                # noqa: F841
        return int.from_bytes(bytes(buf), "little") if st == 0 else None

    def _raw_write(self, cmd, value, nbytes):
        fn = self.nvapi._i(I2C_WRITE_EX, PTR, PTR, PTR)
        if fn is None:
            return False
        buf = (u8 * nbytes)(*[(value >> (8 * i)) & 0xFF
                              for i in range(nbytes)])
        s, regb = self._mk(cmd, buf, nbytes)
        unk = (u32 * 2)()
        st = fn(self.nvapi.gpu, ctypes.byref(s), ctypes.byref(unk))
        _keep = (regb, buf, unk)                                # noqa: F841
        return st == 0

    # -- identity ------------------------------------------------------------- #
    def present(self):
        """Is the profile's device actually here, by identity and never address?

        Address alone is not evidence. On the authoring board, fitting the mod
        links made a DIFFERENT part start answering on the same port, so
        "something ACKed" and "the thing we expect is there" came apart.
        """
        if self.nvapi is None or not getattr(self.nvapi, "ok", False):
            return False
        for chk in self.p.identity:
            got = self.read(int(chk["reg"]), int(chk.get("bytes", 1)))
            if got is None:
                return False
            if "equals" in chk and got != int(chk["equals"]):
                return False
            if "mask" in chk and (got & int(chk["mask"])) != int(
                    chk.get("value", 0)):
                return False
        return True

    # -- telemetry ------------------------------------------------------------ #
    def telemetry(self):
        if not self.present():
            return {}
        out = {}
        for t in self.p.telemetry_specs:
            raw = self.read(int(t["reg"]), int(t.get("bytes", 2)))
            if raw is None:
                continue
            out[str(t["key"])] = _decode(raw, t.get("encoding", "uint"),
                                         _bitspec(t.get("bits")),
                                         float(t.get("scale", 1.0)))
        # PAGE/scaling can change through another controller client.
        if self.p.runtime_checks and not self.present():
            return {}
        if self.p.read_only:
            # No offset register to report, and reporting 0 would read as
            # "no offset applied" rather than "this profile cannot apply one".
            out["offset_raw"] = out["offset_mv"] = None
            return out
        raw = self.read(self.p.wreg, self.p.wbytes)
        if self.p.runtime_checks and not self.present():
            return {}
        out["offset_raw"] = raw
        out["offset_mv"] = None if raw is None else self._offset_mv(raw)
        return out

    def _offset_mv(self, raw):
        f = _extract(raw, self.p.wbits)
        w = ((self.p.wbits[0] - self.p.wbits[1] + 1) if self.p.wbits
             else self.p.wbytes * 8)
        return _signed(f, w) * self.p.lsb_mv

    # -- XOC ------------------------------------------------------------------ #
    def enable_xoc(self, confirm):
        """Remove the voltage ENVELOPE. Nothing else.

        A typed phrase rather than a boolean on purpose: a caller cannot reach
        this state by passing a truthy value it happened to have lying around,
        and the call site reads as what it is when somebody audits the diff.
        """
        if confirm != XOC_CONFIRM:
            return False, "XOC not enabled: confirmation string did not match"
        self.xoc = True
        return True, ("XOC MODE ON - the voltage envelope is removed. The "
                      "command whitelist, the NVRAM denylist, the identity "
                      "check, the read-back, the dry run, the typo ceiling and "
                      "the register's representable range all remain.")

    def disable_xoc(self):
        self.xoc = False
        return True, "XOC off - the profile's envelope is restored"

    def _envelope(self, xoc=None):
        if (self.xoc if xoc is None else xoc):
            return (float("-inf"), float("inf"), float("inf"),
                    (300.0, self.p.sanity_rail))
        return (self.p.env_min, self.p.env_max, self.p.ceiling,
                self.p.plausible)

    # -- the write ------------------------------------------------------------ #
    def plan(self, mv):
        """Every guard, reporting what WOULD happen. Writes nothing, ever.

        Exists because the absence of one cost a real unintended write during
        bring-up: a check meant to trip the ceiling did not, because at idle the
        base rail was 682 mV and +100 predicted 782 - comfortably legal. The
        guards behaved correctly; the operator's model of them did not. A dry
        run turns an assumed refusal into an observed one.
        """
        return self.set_offset_mv(mv, acknowledged=True, dry_run=True)

    def validate_offset_mv(self, mv, *, xoc=False):
        """Check a saved/requested offset without reading or writing the bus."""
        p = self.p
        if (not isinstance(mv, (int, float)) or isinstance(mv, bool)
                or not math.isfinite(mv)):
            return False, "refused: offset must be a finite number of millivolts"
        if p.read_only:
            return False, f"refused: the {p.name} profile is read-only"
        if p.wreg not in p.writable:
            return False, f"refused: 0x{p.wreg:02X} is not a whitelisted register"
        if p.wreg in p.never:
            return False, f"refused: 0x{p.wreg:02X} is permanently denied"
        if abs(mv) > SANITY_MAX_ABS_OFFSET_MV:
            return False, (f"REFUSED AS A TYPO: {mv:+.2f} mV exceeds the "
                           f"{SANITY_MAX_ABS_OFFSET_MV:.0f} mV absolute offset bound")
        steps = int(round(mv / p.lsb_mv))
        if not p.raw_min <= steps <= p.raw_max:
            return False, (f"refused: {mv:+.2f} mV is raw {steps:+d}, outside "
                           f"the register's representable [{p.raw_min:+d}, "
                           f"{p.raw_max:+d}] range")
        if not xoc and not p.env_min <= mv <= p.env_max:
            return False, (f"refused: {mv:+.2f} mV outside this profile's "
                           f"[{p.env_min:+.0f}, {p.env_max:+.0f}] mV envelope")
        return True, "offset is within the static register and mode bounds"

    def set_offset_mv(self, mv, *, acknowledged=False, dry_run=False, _xoc=None):
        # Verification restores using its entry policy even if an API caller
        # changes XOC meanwhile. This private override never changes live mode;
        # identity, whitelist, representability, telemetry and readback remain.
        xoc = self.xoc if _xoc is None else _xoc
        p = self.p
        if not acknowledged:
            return False, ("refused: the rail offset writes the VRM directly "
                           "and is not bounded by the GPU's voltage ceiling. "
                           "The caller must pass acknowledged=True.")
        if not self.present():
            return False, (f"refused: no {p.regulator} identified at "
                           f"0x{self.addr7:02X}/port {p.port} - the identity "
                           f"read did not match this profile.")

        if p.read_only:
            return False, (f"refused: the {p.name} profile is read-only - it "
                           f"declares no offset register, so there is nothing "
                           f"on this board Druta knows how to write.")
        cmd = p.wreg
        if cmd not in p.writable:
            return False, f"refused: 0x{cmd:02X} is not a whitelisted register"
        if cmd in p.never:
            return False, (f"refused: 0x{cmd:02X} is permanently denied - "
                           f"{p.never[cmd]}")

        # Typo catcher first, before any bus traffic, so an absurd value is
        # named as absurd rather than as merely out of policy - and so XOC never
        # reports it as writable.
        if abs(mv) > SANITY_MAX_ABS_OFFSET_MV:
            return False, (f"REFUSED AS A TYPO: {mv:+.2f} mV. The largest "
                           f"offset Druta will ever build is "
                           f"{SANITY_MAX_ABS_OFFSET_MV:.0f} mV, in any mode, "
                           f"including XOC. Check for a slipped decimal.")

        # Representability, also before any bus traffic, and AHEAD of the
        # envelope because XOC removes the envelope and must not remove this.
        # Outside this range the field wraps through its sign bit and the
        # regulator receives the opposite polarity: on the MP2888A a request
        # for +800 mV becomes raw 128 -> 0x80 -> -128 -> -800 mV delivered.
        steps = int(round(mv / p.lsb_mv))
        if not (p.raw_min <= steps <= p.raw_max):
            return False, (f"refused: {mv:+.2f} mV is raw {steps:+d}, outside "
                           f"the register's representable [{p.raw_min:+d}, "
                           f"{p.raw_max:+d}] ([{p.hw_min_mv:+.2f}, "
                           f"{p.hw_max_mv:+.2f}] mV). Past this the field "
                           f"wraps through its sign bit and the rail would "
                           f"move the WRONG WAY. No mode removes this - it is "
                           f"what the register can hold, not a policy.")

        lo_mv, hi_mv, ceiling, band = self._envelope(xoc)
        tag = "  [XOC - envelope removed]" if xoc else ""
        if not (lo_mv <= mv <= hi_mv):
            return False, (f"refused: {mv:+.2f} mV outside this profile's "
                           f"[{lo_mv:+.0f}, {hi_mv:+.0f}] mV envelope. XOC "
                           f"removes this bound.")

        cur_vout = self.read_vout()
        cur_raw = self.read(cmd, p.wbytes)
        if cur_vout is None or cur_raw is None:
            return False, "refused: could not read the rail before writing"
        blo, bhi = band
        if not (blo <= cur_vout <= bhi):
            return False, (f"refused: rail reads {cur_vout:.0f} mV, outside "
                           f"the plausible {blo:.0f}-{bhi:.0f} mV band - not "
                           f"writing against a reading we do not trust")

        applied = steps * p.lsb_mv
        base = cur_vout - self._offset_mv(cur_raw)
        predicted = base + applied
        if predicted > p.sanity_rail:
            return False, (f"REFUSED AS A TYPO: {applied:+.2f} mV would put "
                           f"the rail near {predicted:.2f} mV. No mode, XOC "
                           f"included, takes the rail past "
                           f"{p.sanity_rail:.0f} mV (base {base:.2f} mV).")
        if predicted > ceiling:
            return False, (f"refused: {applied:+.2f} mV would put the rail "
                           f"near {predicted:.2f} mV, over this profile's "
                           f"{ceiling:.0f} mV ceiling (base {base:.2f} mV). "
                           f"XOC removes this ceiling.")

        if dry_run:
            cap = "none" if xoc else f"{ceiling:.0f} mV"
            return True, (f"WOULD write {applied:+.2f} mV (raw {steps:+d}): "
                          f"base {base:.2f} mV -> predicted {predicted:.2f} "
                          f"mV, ceiling {cap}. Nothing was written.{tag}")

        # Replace only the declared field. Other bits may be factory settings,
        # not reserved zeros. Masking first also prevents sign extension from
        # overwriting those settings for a negative offset.
        width = ((p.wbits[0] - p.wbits[1] + 1) if p.wbits else p.wbytes * 8)
        field = steps & ((1 << width) - 1)
        shift = p.wbits[1] if p.wbits else 0
        mask = ((1 << width) - 1) << shift
        raw = (cur_raw & ~mask) | (field << shift)
        if p.runtime_checks:
            if not self.present():
                return False, "refused: controller identity/PAGE/scaling changed before writing"
            if self.read(cmd, p.wbytes) != cur_raw:
                return False, "refused: the offset word changed before writing; retry with a fresh reading"
            if not self.present():
                return False, "refused: controller identity/PAGE/scaling changed before dispatch"
        failure = None
        try:
            # Even a rejected/exceptional transaction may have reached the
            # controller. Every failure after dispatch attempts exact rollback.
            if not self._raw_write(cmd, raw, p.wbytes):
                failure = "the I2C write was rejected by NVAPI"
            elif p.runtime_checks and not self.present():
                failure = "controller identity/PAGE/scaling changed after writing"
            else:
                back = self.read(cmd, p.wbytes)
                if back != raw:
                    failure = (f"WROTE BUT READ BACK WRONG: sent word 0x{raw:04X}, "
                               f"read 0x{-1 if back is None else back:04X}")
                elif p.runtime_checks and not self.present():
                    failure = "controller identity/PAGE/scaling changed during readback"
                else:
                    now = self.read_vout()
                    if p.runtime_checks and now is None:
                        failure = "controller voltage/configuration could not be verified after writing"
        except Exception as e:                                  # noqa: BLE001
            failure = f"I2C transaction raised {type(e).__name__}: {e}"
        if failure is not None:
            restored, undo = self._restore_word(cur_raw)
            status = "restored original word" if restored else "RESTORE FAILED; rail state unknown"
            return False, f"{failure}. {status}: {undo}"
        return True, (f"{p.rail} offset {applied:+.2f} mV (raw {steps:+d}), "
                      f"rail now {'?' if now is None else f'{now:.0f}'} mV. "
                      f"This does NOT clear on reboot - only 'Reset all to "
                      f"stock' or a power cycle.{tag}")

    def _restore_word(self, original):
        """Restore a captured word, preserving its field and factory bits.

        Restoration is not a new voltage request, so it does not depend on
        today's voltage envelope or telemetry. Device/configuration identity,
        the command whitelist and the NVRAM denylist still apply.
        """
        p = self.p
        if p.read_only or p.wreg not in p.writable or p.wreg in p.never:
            return False, "no permitted offset register to restore"
        try:
            if not self.present():
                return False, "controller identity/PAGE/scaling does not match; restore not dispatched"
            if not isinstance(original, int) or not 0 <= original < 1 << (p.wbytes * 8):
                return False, "invalid captured register word"
            chk = self.read(p.wreg, p.wbytes)
            if p.runtime_checks and not self.present():
                return False, "controller identity/PAGE/scaling changed during restore readback"
            if chk == original:
                return True, f"original 0x{original:04X} already reads back exactly"
            if not self._raw_write(p.wreg, original, p.wbytes):
                return False, "restore write rejected"
            if p.runtime_checks and not self.present():
                return False, "controller identity/PAGE/scaling changed during restore"
            chk = self.read(p.wreg, p.wbytes)
            if p.runtime_checks and not self.present():
                return False, "controller identity/PAGE/scaling changed during restore readback"
            if chk == original:
                return True, f"0x{original:04X} read back exactly"
            return False, f"restore readback mismatch (reads 0x{-1 if chk is None else chk:04X})"
        except Exception as e:                                  # noqa: BLE001
            return False, f"restore raised {e!r}"

    def reset(self):
        """Zero the offset. The only reliable undo; a reboot is not one."""
        return self.set_offset_mv(0.0, acknowledged=True)

    # -- measurement ---------------------------------------------------------- #
    def read_vout(self):
        if self.p.runtime_checks and not self.present():
            return None
        t = next((x for x in self.p.telemetry_specs
                  if x.get("key") == "vout_mv"), None)
        if t is None:
            return None
        raw = self.read(int(t["reg"]), int(t.get("bytes", 2)))
        if raw is None:
            return None
        if self.p.runtime_checks and not self.present():
            return None
        return _decode(raw, t.get("encoding", "uint"),
                       _bitspec(t.get("bits")), float(t.get("scale", 1.0)))

    def _sample(self, n=VERIFY_MEASUREMENT_SAMPLES, ref=None, check=None):
        """Return a complete window's median and peak-to-peak noise.

        Verification uses raw controller VOUT and checks the operating point
        during sampling. The optional subtraction is retained for diagnostic
        callers only: it cannot establish the physical I2C voltage response.
        """
        xs = []
        for _ in range(n):
            if check is not None:
                check()
            v = self.read_vout()
            if v is not None:
                if ref is None:
                    xs.append(float(v))
                else:
                    try:
                        r = ref()
                    except Exception:                           # noqa: BLE001
                        r = None
                    if r is not None:
                        xs.append(float(v) - float(r))
            time.sleep(VERIFY_MEASUREMENT_INTERVAL_S)
        if check is not None:
            check()
        if len(xs) != n or any(not math.isfinite(x) for x in xs):
            return None, None
        s = sorted(xs)
        n2 = len(s)
        med = s[n2 // 2] if n2 % 2 else (s[n2 // 2 - 1] + s[n2 // 2]) / 2.0
        return med, (max(xs) - min(xs))

    def _verification_stability(self, ref=None, operating_point=None, cancelled=None,
                                check_voltage=True):
        """Require complete, repeatable samples before interpreting a response.

        Baseline spread is compared with the controller's offset resolution.
        After a write, spread becomes part of the response detection noise
        threshold; rejecting a noisy small rung would prevent testing a larger
        measurable response. P-state and clock samples
        must agree when the caller provides them. This proves only stability
        over the sampling window, not a particular load or electrical rail ID.
        """
        cancelled = cancelled or (lambda: False)
        volts, references, points = [], [], []
        for _ in range(VERIFY_SAMPLES):
            if cancelled():
                raise ValueError("verification cancelled")
            voltage = self.read_vout()
            if voltage is None or not math.isfinite(float(voltage)):
                raise ValueError("selected rail voltage unreadable during stability check")
            volts.append(float(voltage))
            if ref is not None:
                try:
                    value = ref()
                except Exception:
                    value = None
                references.append(value)
            if operating_point is not None:
                point = operating_point()
                if (not isinstance(point, tuple) or len(point) != 3
                        or any(v is None or not isinstance(v, (int, float))
                               or not math.isfinite(v) for v in point)):
                    raise ValueError("GPU P-state/core/memory clocks unreadable")
                points.append(point)
            time.sleep(0.01)
        resolution = abs(self.p.lsb_mv)
        if not math.isfinite(resolution) or resolution <= 0:
            raise ValueError("controller offset resolution is invalid")
        spread = max(volts) - min(volts)
        if check_voltage and spread > resolution:
            raise ValueError(f"selected rail changed by {spread:.2f} mV during sampling "
                             f"(controller offset resolution {resolution:g} mV)")
        available = [v for v in references if v is not None]
        if available:
            if len(available) != len(references) or any(
                    not isinstance(v, (int, float)) or not math.isfinite(v) for v in available):
                raise ValueError("NVAPI reference was intermittent or invalid")
            spread = max(available) - min(available)
            if check_voltage and spread > resolution:
                raise ValueError(f"NVAPI reference changed by {spread:.2f} mV during sampling "
                                 f"(controller offset resolution {resolution:g} mV)")
        if points and any(point != points[0] for point in points):
            raise ValueError("GPU P-state or core/memory clocks changed during sampling")
        return {"vout_min_mv": min(volts), "vout_max_mv": max(volts),
                "reference_available": bool(available),
                "reference_min_mv": min(available) if available else None,
                "reference_max_mv": max(available) if available else None,
                "operating_point": points[0] if points else None}

    def verify(self, *, acknowledged=False, log=None, ref=None,
               allow_idle=False, cancelled=None, operating_point=None):
        """Climb the smallest offsets that could move the rail until one does.

        WHY A LADDER AND NOT A SINGLE WRITE. This is how a volt mod is proven on
        an EVC: you do not trust a pot because it turned, you turn it the
        smallest amount that could register and look for the rail to report the
        change back. Reaching a large step with a flat rail means the wiper is
        not on the node you think it is.

        It answers what the dry run cannot. plan() proves the GUARDS agree; it
        cannot prove the write ARRIVES. Every failure that matters here - wrong
        device, unfitted link, a register that stores a value the regulator
        ignores - passes the dry run AND the read-back, because the read-back
        only proves the REGISTER took the value, not that the RAIL did.

        Voltage alone does not establish load state. No minimum operating-voltage
        gate is applied. ``allow_idle`` remains accepted for older callers but
        no longer changes behavior. ``ref`` is also retained for compatibility
        but is never sampled: response detection uses controller VOUT directly.
        A driver voltage is not an independent measurement of an I2C offset;
        subtracting it can mask the response or manufacture a false response.
        Success establishes only a measured response
        at the tested operating point, not full-load validation or voltage gain.

        Returns (ok, message, ladder). The entry offset is restored in a
        finally, and the restore is verified.
        """
        self._verification_write_attempted = False
        self._verification_restore_ok = True
        self._verification_restore_error = ""
        ref = None
        cancelled = cancelled or (lambda: False)
        if cancelled():
            return False, "verification cancelled; nothing written", []
        entry_xoc = bool(getattr(self, "xoc", False))
        p = self.p
        if p.read_only:
            return False, "refused: this profile supports telemetry only", []
        if not acknowledged:
            return False, ("refused: verification writes real offsets to the "
                           "VRM. The caller must pass acknowledged=True."), []
        if not self.present():
            return False, ("refused: no device matching this profile - "
                           "nothing to verify against."), []

        entry_raw = self.read(p.wreg, p.wbytes)
        if entry_raw is None:
            return False, "refused: could not read the current offset", []
        entry_mv = self._offset_mv(entry_raw)

        def say(m):
            if log:
                log(m)

        try:
            stable = self._verification_stability(ref, operating_point, cancelled,
                                                 check_voltage=operating_point is None)
        except Exception as exc:
            return False, f"INCONCLUSIVE - {exc}; nothing written", []
        say(f"stable baseline: {stable}")
        def check_sample():
            if cancelled():
                raise ValueError("verification cancelled")
            if operating_point() != stable["operating_point"]:
                raise ValueError("GPU operating point changed during measurement")
        sample_args = {"ref": None}
        if operating_point is not None:
            sample_args["check"] = check_sample
        try:
            base, noise = self._sample(**sample_args)
        except Exception as exc:
            return False, f"INCONCLUSIVE - {exc}; nothing written", []
        if cancelled():
            return False, "verification cancelled; nothing written", []
        if base is None:
            return False, "refused: could not read the rail", []
        noise = max(noise, stable["vout_max_mv"] - stable["vout_min_mv"])
        what = "I2C VOUT"
        say(f"baseline {what} {base:.0f} mV, wander {noise:.0f} mV "
            f"(entry offset {entry_mv:+.2f} mV)")

        ladder, hit = [], None
        failure = None
        restore_errors = []
        try:
            for rung in p.rungs:
                if cancelled():
                    failure = "verification cancelled"
                    break
                if bool(getattr(self, "xoc", False)) != entry_xoc:
                    failure = "verification stopped: XOC mode changed"
                    break
                if operating_point is not None and operating_point() != stable["operating_point"]:
                    failure = "INCONCLUSIVE - GPU operating point changed before the write"
                    break
                self._verification_write_attempted = True
                self._verification_restore_ok = False
                ok, msg = self.set_offset_mv(entry_mv + rung,
                                             acknowledged=True)
                if not ok:
                    # Distinguish "the rail did not move" from "we were not
                    # allowed to try". Reporting a policy refusal as a dead
                    # write path sends somebody looking for a soldering fault
                    # that is not there.
                    ladder.append({"rung_mv": rung, "refused": msg})
                    say(f"  {rung:+6.2f} mV  REFUSED - {msg}")
                    failure = (
                        f"INCONCLUSIVE - verification stopped at "
                        f"{rung:+.2f} mV because the offset request failed. "
                        f"The voltage write path remains unverified. {msg}")
                    break

                time.sleep(VERIFY_SETTLE_S)
                if cancelled():
                    failure = "verification cancelled"
                    break
                now, _pp = self._sample(**sample_args)
                if cancelled():
                    failure = "verification cancelled"
                    break
                if bool(getattr(self, "xoc", False)) != entry_xoc:
                    failure = "verification stopped: XOC mode changed"
                    break
                if now is None:
                    ladder.append({"rung_mv": rung, "read_failed": True})
                    say(f"  {rung:+6.2f} mV  rail read failed")
                    failure = ("INCONCLUSIVE - rail read failed during "
                               "verification; no further offsets attempted.")
                    break

                # Confirm that a transient operating-point change did not
                # masquerade as a voltage response before accepting the rung.
                check = self._verification_stability(ref, operating_point, cancelled,
                                                     check_voltage=False)
                if check["operating_point"] != stable["operating_point"]:
                    failure = "INCONCLUSIVE - GPU operating point changed during the trial"
                    break
                delta = now - base
                # Detect against measured noise and the controller's step,
                # not an assumed fraction of the requested voltage gain.
                response_noise = max(noise, _pp,
                                     check["vout_max_mv"] - check["vout_min_mv"])
                thr = max(abs(p.lsb_mv), response_noise)
                moved = delta >= abs(p.lsb_mv) and delta > response_noise
                ladder.append({"rung_mv": rung, "rail_mv": now,
                               "delta_mv": delta, "threshold_mv": thr,
                               "noise_mv": response_noise,
                               "moved": moved})
                say(f"  {rung:+6.2f} mV  {what} {now:.0f} mV  delta "
                    f"{delta:+.0f} mV (need {thr:.1f})  "
                    f"{'MOVED' if moved else 'flat'}")
                if moved:
                    hit = ladder[-1]
                    break
        except Exception as exc:                                # noqa: BLE001
            failure = f"INCONCLUSIVE - verification raised {exc!r}"
        finally:
            # Do not return from the ladder: even a refusal must wait for the
            # restoration verdict. A measured response cannot authorize Apply
            # while the entry setting is unconfirmed.
            try:
                rok, rmsg = (self._restore_word(entry_raw)
                             if self._verification_write_attempted
                             else (True, "nothing written"))
                if not rok:
                    restore_errors.append(f"restore refused: {rmsg}")
            except Exception as exc:                            # noqa: BLE001
                restore_errors.append(f"restore raised {exc!r}")
            try:
                # Independently confirm the captured complete word. Neighboring
                # fields are controller state, not necessarily reserved zeros.
                back = self.read(p.wreg, p.wbytes)
                if back != entry_raw:
                    seen = "unreadable" if back is None else f"0x{back:04X}"
                    restore_errors.append(
                        f"restore readback {seen}, expected word 0x{entry_raw:X}")
            except Exception as exc:                            # noqa: BLE001
                restore_errors.append(f"restore readback raised {exc!r}")
            self._verification_restore_ok = not restore_errors
            self._verification_restore_error = "; ".join(restore_errors)
            if restore_errors:
                say("RESTORE FAILED: " + "; ".join(restore_errors))
            else:
                say(f"restored to {entry_mv:+.2f} mV")

        if cancelled():
            failure = "verification cancelled" + (f"; {failure}" if failure else "")
        elif bool(getattr(self, "xoc", False)) != entry_xoc:
            failure = "verification stopped: XOC mode changed"
        if restore_errors:
            return False, (
                "RESTORE FAILED - verification is invalid; treat the rail as "
                "unknown; verification cannot unlock Apply. "
                + "; ".join(restore_errors)
                + (f". {failure}" if failure else "")), ladder
        if failure:
            return False, f"{failure} Restored to {entry_mv:+.2f} mV.", ladder

        if hit is None:
            return False, (
                f"INCONCLUSIVE - no positive I2C VOUT response exceeded the "
                f"detection threshold through +{p.rungs[-1]:.2f} mV. "
                f"Register writes/readback succeeded, but this does not prove "
                f"a voltage change or identify the electrical rail. "
                f"Baseline {base:.1f} mV; see the rung measurements in the log. "
                f"Restored to {entry_mv:+.2f} mV. Apply remains unverified."), ladder

        # A register readback proves restoration of the command, not that the
        # observed response was caused by it. Require an A-B-A measurement:
        # controller VOUT must return to its entry baseline while P0 is held.
        try:
            time.sleep(VERIFY_SETTLE_S)
            restored = self._verification_stability(operating_point=operating_point,
                                                     cancelled=cancelled,
                                                     check_voltage=False)
            if restored["operating_point"] != stable["operating_point"]:
                raise ValueError("GPU operating point changed after restoration")
            restored_mv, restored_noise = self._sample(**sample_args)
            if restored_mv is None:
                raise ValueError("restored I2C VOUT is unreadable")
            step = abs(p.lsb_mv)
            telemetry = next((t for t in getattr(p, "telemetry_specs", [])
                              if t.get("key") == "vout_mv"), {})
            quantum = abs(float(telemetry.get("scale", 1)))
            if telemetry.get("encoding", "uint") in ("uint", "int") and quantum > 0:
                # A 6.25 mV step can span seven 1 mV READ_VOUT codes.
                step = math.ceil(step / quantum) * quantum
            tolerance = max(step, noise, restored_noise)
            if abs(restored_mv - base) > tolerance:
                raise ValueError(f"I2C VOUT did not return to baseline "
                                 f"({restored_mv:.1f} vs {base:.1f} mV)")
            reversal = hit["rail_mv"] - restored_mv
            if reversal < abs(p.lsb_mv) or reversal <= max(hit["noise_mv"], restored_noise):
                raise ValueError("I2C VOUT did not show a downward response above noise after restoration")
            if cancelled():
                raise ValueError("verification cancelled")
            hit["restored_vout_mv"] = restored_mv
            hit["reversal_mv"] = reversal
            say(f"I2C VOUT returned within the baseline noise/resolution band: "
                f"{restored_mv:.1f} mV (entry {base:.1f} mV; "
                f"downward response {reversal:.1f} mV)")
        except Exception as exc:
            return False, (f"INCONCLUSIVE - {exc}. Offset word restored to "
                           f"{entry_mv:+.2f} mV; voltage response remains unverified."), ladder

        # A detecting step is not a calibrated gain or a measured deadband.
        # Boards with the same controller may have different loadline settings.
        return True, (
            "WRITE PATH CONFIRMED at the tested operating point. "
            f"First detected response at {hit['rung_mv']:+.2f} mV: "
            f"{hit['delta_mv']:+.0f} mV against a {hit['threshold_mv']:.1f} mV "
            f"threshold. I2C VOUT returned to {restored_mv:.1f} mV after restoration. "
            f"Restored to {entry_mv:+.2f} mV. "
            "This verifies a response, not full-load behavior or a 1:1 voltage gain."
        ), ladder


def discover(nvapi, dev_id=None, subsys=None, log=None, *, architecture=None):
    """Read-only candidate discovery on the selected GPU's actual I2C buses.

    NCP4206 and MP2888A use controller fingerprints, without board-ID gates.
    Other TOML recipes retain their explicit board constraints. Return every
    candidate: a caller must never silently resolve an ambiguous bus map.
    """
    from .ncp4206 import DISCOVERY_PORTS, NCP4206
    from .mp2888 import discover as discover_mp2888
    hits = []
    if architecture == 2 and getattr(nvapi, "ok", False):
        for port in DISCOVERY_PORTS:
            ncp = NCP4206(nvapi, architecture=architecture, port=port)
            if ncp.present():
                hits.append(ncp)
    selected = getattr(nvapi, "selected", None) or {}
    conflict = any(supplied is not None and selected.get(key) is not None
                   and supplied != selected[key]
                   for key, supplied in (("devid", dev_id), ("subsys", subsys)))
    if dev_id is None:
        dev_id = selected.get("devid")
    if subsys is None:
        subsys = selected.get("subsys")
    for p in load_profiles(log=log):
        if getattr(p, "regulator", "").upper() == "MPS MP2888A":
            hits.extend(discover_mp2888(nvapi, p, log=log))
            continue
        if conflict or not p.candidate_for(dev_id, subsys):
            continue
        for a in p.addrs:
            r = Rail(p, nvapi, addr7=a)
            if r.present():
                hits.append(r)
    if conflict and log:
        log("board-specific I2C recipes skipped: supplied PCI IDs do not "
            "match the selected GPU", False)
    if log:
        for r in hits:
            log(f"i2c candidate: {r.p.name} at 0x{r.addr7:02X}/port {r.p.port}", True)
        if len(hits) > 1:
            log(f"{len(hits)} I2C candidates found (ambiguous); select a controller and "
                "Verify it before applying an adjustment", False)
    return hits


def find(nvapi, dev_id=None, subsys=None, log=None, *, architecture=None):
    """Compatibility API: return a rail only when discovery is unambiguous."""
    hits = discover(nvapi, dev_id, subsys, log, architecture=architecture)
    return hits[0] if len(hits) == 1 else None
