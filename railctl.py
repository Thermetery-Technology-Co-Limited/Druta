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
import os
import sys
import time
import tomllib

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
# READ_VOUT is whole millivolts, so two adjacent codes can differ by 1 mV with
# nothing having happened.
VERIFY_MIN_DETECT_MV = 3.0


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

        wr = d.get("write") or []
        self.write = next((w for w in wr if w.get("key") == "offset_mv"), None)
        if self.write is None:
            raise ProfileError("a profile must declare a [[write]] with "
                               "key = \"offset_mv\"")
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
        self.min_loaded_mv = float(v.get("min_loaded_vout_mv", 800.0))
        self.deadband_mv = v.get("expect_deadband_mv")

        m = d.get("match") or {}
        self.pci_device = {str(x).lower() for x in m.get("pci_device", [])}
        self.pci_subsys = {str(x).lower() for x in m.get("pci_subsys", [])}

    @property
    def hw_min_mv(self):
        return self.raw_min * self.lsb_mv

    @property
    def hw_max_mv(self):
        return self.raw_max * self.lsb_mv

    def candidate_for(self, dev_id=None, subsys=None):
        """PCI ids narrow the candidates. They never decide - identity does."""
        if self.pci_device and dev_id is not None:
            if f"0x{dev_id:04x}" not in self.pci_device:
                return False
        if self.pci_subsys and subsys is not None:
            if f"0x{subsys:08x}" not in self.pci_subsys:
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
        out.append(os.path.join(os.path.dirname(sys.executable), "i2c"))
        mei = getattr(sys, "_MEIPASS", None)
        if mei:
            out.append(os.path.join(mei, "i2c"))
    else:
        out.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "i2c"))
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
        raw = self.read(self.p.wreg, self.p.wbytes)
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

    def _envelope(self):
        if self.xoc:
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

    def set_offset_mv(self, mv, *, acknowledged=False, dry_run=False):
        p = self.p
        if not acknowledged:
            return False, ("refused: the rail offset writes the VRM directly "
                           "and is not bounded by the GPU's voltage ceiling. "
                           "The caller must pass acknowledged=True.")
        if not self.present():
            return False, (f"refused: no {p.regulator} identified at "
                           f"0x{self.addr7:02X}/port {p.port} - the identity "
                           f"read did not match this profile.")

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

        lo_mv, hi_mv, ceiling, band = self._envelope()
        tag = "  [XOC - envelope removed]" if self.xoc else ""
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
            cap = "none" if self.xoc else f"{ceiling:.0f} mV"
            return True, (f"WOULD write {applied:+.2f} mV (raw {steps:+d}): "
                          f"base {base:.2f} mV -> predicted {predicted:.2f} "
                          f"mV, ceiling {cap}. Nothing was written.{tag}")

        # Only the FIELD, placed where the profile says it lives. The reserved
        # bits of a wider transaction are documented to ignore writes and read
        # back as zero, so sending a sign-extended value guarantees a read-back
        # mismatch on every negative offset.
        width = ((p.wbits[0] - p.wbits[1] + 1) if p.wbits else p.wbytes * 8)
        field = steps & ((1 << width) - 1)
        raw = field << (p.wbits[1] if p.wbits else 0)
        if not self._raw_write(cmd, raw, p.wbytes):
            return False, "the I2C write was rejected by NVAPI"

        back = self.read(cmd, p.wbytes)
        if back is None or _extract(back, p.wbits) != field:
            undo = self._panic_zero()
            return False, (f"WROTE BUT READ BACK WRONG: sent field "
                           f"0x{field:0{max(2, width // 4)}X}, read "
                           f"0x{-1 if back is None else back:04X}. Offset "
                           f"auto-reset: {undo}. Treat the rail as unknown "
                           f"until a telemetry read agrees.")
        now = self.read_vout()
        return True, (f"{p.rail} offset {applied:+.2f} mV (raw {steps:+d}), "
                      f"rail now {'?' if now is None else f'{now:.0f}'} mV. "
                      f"This does NOT clear on reboot - only 'Reset all to "
                      f"stock' or a power cycle.{tag}")

    def _panic_zero(self):
        """Do not merely ADVISE a reset after a bad read-back - attempt it.

        The alternative is returning an error while the regulator holds an
        offset neither the caller nor this module can name, which is the worst
        state available on this path.
        """
        try:
            if not self._raw_write(self.p.wreg, 0, self.p.wbytes):
                return "ZERO WRITE REJECTED"
            chk = self.read(self.p.wreg, self.p.wbytes)
            if chk is not None and _extract(chk, self.p.wbits) == 0:
                return "zeroed"
            return f"ZERO FAILED (reads 0x{-1 if chk is None else chk:04X})"
        except Exception as e:                                  # noqa: BLE001
            return f"ZERO RAISED {e!r}"

    def reset(self):
        """Zero the offset. The only reliable undo; a reboot is not one."""
        return self.set_offset_mv(0.0, acknowledged=True)

    # -- measurement ---------------------------------------------------------- #
    def read_vout(self):
        t = next((x for x in self.p.telemetry_specs
                  if x.get("key") == "vout_mv"), None)
        if t is None:
            return None
        raw = self.read(int(t["reg"]), int(t.get("bytes", 2)))
        if raw is None:
            return None
        return _decode(raw, t.get("encoding", "uint"),
                       _bitspec(t.get("bits")), float(t.get("scale", 1.0)))

    def _sample(self, n=VERIFY_SAMPLES, ref=None):
        """(median, peak-to-peak) of the detection quantity.

        WITHOUT `ref` the quantity is the raw rail, and it is only as steady as
        the GPU's governor: at idle the VID request wanders on its own and a
        small offset disappears into that movement. Measured on the authoring
        card at idle - 9 mV of wander, which swallowed the 6.25, 12.50 and
        25.00 mV rungs outright.

        WITH `ref` - a callable returning the GPU's own reported rail in mV -
        the quantity is `rail - ref`, the offset the regulator is ACTUALLY
        adding. That subtracts the governor entirely, because it moves both
        terms. It is the software equivalent of what a volt modder does with a
        meter on the rail and the vendor tool on screen: watch the difference,
        not either number. Same card, under load: 2 mV.

        Peak-to-peak rather than a standard deviation because the question is
        not how the samples are distributed, it is how far the reading wanders
        while nobody is writing to it. Less than that cannot be attributed.
        """
        xs = []
        for _ in range(n):
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
            time.sleep(0.01)
        if not xs:
            return None, None
        s = sorted(xs)
        n2 = len(s)
        med = s[n2 // 2] if n2 % 2 else (s[n2 // 2 - 1] + s[n2 // 2]) / 2.0
        return med, (max(xs) - min(xs))

    def verify(self, *, acknowledged=False, log=None, ref=None,
               allow_idle=False):
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

        THE CARD MUST BE UNDER LOAD, and refusing at idle is not caution. A
        multiphase controller sheds phases and changes loadline under PSI, so an
        idle card is a DIFFERENT REGULATOR from the one that carries an
        overclock. A result measured there is unrepresentative, which is worse
        than noisy. `allow_idle` downgrades to a path-only verdict with no
        magnitude attached rather than letting an idle number stand in.

        Returns (ok, message, ladder). The entry offset is restored in a
        finally, and the restore is verified.
        """
        p = self.p
        if not acknowledged:
            return False, ("refused: verification writes real offsets to the "
                           "VRM. The caller must pass acknowledged=True."), []
        if not self.present():
            return False, ("refused: no device matching this profile - "
                           "nothing to verify against."), []

        vcore = None
        if ref is not None:
            try:
                vcore = ref()
            except Exception:                                   # noqa: BLE001
                vcore = None
        idle = vcore is None or vcore < p.min_loaded_mv
        if idle and not allow_idle:
            seen = "unreadable" if vcore is None else f"{vcore:.2f} mV"
            return False, (
                f"refused: the card is not at a real operating point (rail "
                f"reads {seen}, and this profile wants at least "
                f"{p.min_loaded_mv:.0f} mV). The regulator drops phases and "
                f"changes loadline at idle, so a result measured here would "
                f"describe a configuration nobody runs. Load the card and "
                f"repeat."), []

        entry_raw = self.read(p.wreg, p.wbytes)
        if entry_raw is None:
            return False, "refused: could not read the current offset", []
        entry_mv = self._offset_mv(entry_raw)

        def say(m):
            if log:
                log(m)

        base, noise = self._sample(ref=ref)
        if base is None and ref is not None:
            say("GPU voltage readback unavailable - falling back to the raw "
                "rail, which needs a bigger step to clear idle wander")
            ref = None
            base, noise = self._sample()
        if base is None:
            return False, "refused: could not read the rail", []
        what = "rail-minus-VID" if ref is not None else "rail"
        say(f"baseline {what} {base:.0f} mV, wander {noise:.0f} mV "
            f"(entry offset {entry_mv:+.2f} mV)")

        ladder, hit = [], None
        try:
            for rung in p.rungs:
                ok, msg = self.set_offset_mv(entry_mv + rung,
                                             acknowledged=True)
                if not ok:
                    # Distinguish "the rail did not move" from "we were not
                    # allowed to try". Reporting a policy refusal as a dead
                    # write path sends somebody looking for a soldering fault
                    # that is not there.
                    ladder.append({"rung_mv": rung, "refused": msg})
                    say(f"  {rung:+6.2f} mV  REFUSED - {msg}")
                    return False, (
                        f"INCONCLUSIVE - the ladder ran out of headroom at "
                        f"{rung:+.2f} mV before the rail was seen to move, so "
                        f"this is a refusal and not a verdict on the write "
                        f"path. Refusal was: {msg}"), ladder

                time.sleep(VERIFY_SETTLE_S)
                now, _pp = self._sample(ref=ref)
                if now is None:
                    ladder.append({"rung_mv": rung, "read_failed": True})
                    say(f"  {rung:+6.2f} mV  rail read failed")
                    continue

                delta = now - base
                # Three floors, largest wins. Half the rung stops a rail that
                # drifted up on its own being counted as a response; the wander
                # term stops noise being counted at all.
                thr = max(VERIFY_MIN_DETECT_MV, noise, 0.5 * rung)
                moved = delta >= thr
                ladder.append({"rung_mv": rung, "rail_mv": now,
                               "delta_mv": delta, "threshold_mv": thr,
                               "moved": moved})
                say(f"  {rung:+6.2f} mV  {what} {now:.0f} mV  delta "
                    f"{delta:+.0f} mV (need {thr:.1f})  "
                    f"{'MOVED' if moved else 'flat'}")
                if moved:
                    hit = ladder[-1]
                    break
        finally:
            rok, rmsg = self.set_offset_mv(entry_mv, acknowledged=True)
            if not rok:
                say(f"RESTORE FAILED: {rmsg}")
            else:
                back = self.read(p.wreg, p.wbytes)
                want = int(round(entry_mv / p.lsb_mv)) & (
                    (1 << ((p.wbits[0] - p.wbits[1] + 1) if p.wbits
                           else p.wbytes * 8)) - 1)
                if back is None or _extract(back, p.wbits) != want:
                    say("RESTORE READ BACK WRONG - treat the rail as unknown")
                else:
                    say(f"restored to {entry_mv:+.2f} mV")

        if hit is None:
            return False, (
                f"WRITE PATH NOT WORKING. The rail did not move at any rung up "
                f"to {p.rungs[-1]:.0f} mV. Every write was accepted and read "
                f"back correctly, so the register is taking the value and the "
                f"rail is not following it: the part at 0x{self.addr7:02X} is "
                f"not what drives this rail, or this profile describes a "
                f"different board. Do not use the offset slider."), ladder

        if idle:
            # Deliberately no magnitude. The path was exercised, which is real
            # and useful, but the NUMBER belongs to a phase-shed regulator.
            return True, (
                f"WRITE PATH REACHES THE RAIL - but measured at IDLE, so this "
                f"is a path verdict only. It moved at {hit['rung_mv']:+.2f} "
                f"mV. How much it moves under load is not established here and "
                f"no figure from this run should be quoted. Restored to "
                f"{entry_mv:+.2f} mV."), ladder

        # NOT a single gain figure. delta/rung at the detecting rung reads 0.60
        # on the authoring board, and quoting that as "the response" would be
        # wrong twice over: the incremental gain above the deadband is about
        # 1:1, and the shortfall is a fixed floor rather than a proportional
        # loss. A ratio invites someone to scale it. Report the floor.
        dead = max((r["rung_mv"] for r in ladder if not r.get("moved", True)),
                   default=None)
        floor = ("" if dead is None else
                 f" Nothing up to {dead:+.2f} mV cleared the detection "
                 f"threshold, so the usable floor is between {dead:+.2f} and "
                 f"{hit['rung_mv']:+.2f} mV - below it this knob does much "
                 f"less than it says, and above it roughly 1:1.")
        return True, (
            f"WRITE PATH CONFIRMED under load (GPU rail {vcore:.2f} mV). The "
            f"rail first moved at {hit['rung_mv']:+.2f} mV, by "
            f"{hit['delta_mv']:+.0f} mV against a {hit['threshold_mv']:.1f} mV "
            f"threshold.{floor} Restored to {entry_mv:+.2f} mV. Read the "
            f"measured column from here on, not the number on the slider."
        ), ladder


def find(nvapi, dev_id=None, subsys=None, log=None):
    """The one Rail whose identity passes on this card, or None.

    Candidates are narrowed by PCI id and decided by a read on the actual bus.
    If two profiles both identify, the first wins and the clash is logged -
    silently picking one of two descriptions of the same regulator is how a
    board ends up driven by the wrong bounds.
    """
    hits = []
    for p in load_profiles(log=log):
        if not p.candidate_for(dev_id, subsys):
            continue
        for a in p.addrs:
            r = Rail(p, nvapi, addr7=a)
            if r.present():
                hits.append(r)
                break
    if not hits:
        return None
    if len(hits) > 1 and log:
        log(f"{len(hits)} i2c profiles identify on this card "
            f"({', '.join(h.p.name for h in hits)}) - using the first. Remove "
            f"the ones that do not describe your board.", False)
    r = hits[0]
    if log:
        weak = (" (identified by fingerprint, not an ID register - a weaker "
                "claim)" if r.p.weak_id else "")
        log(f"i2c rail: {r.p.name} - {r.p.regulator} on {r.p.rail} at "
            f"0x{r.addr7:02X}/port {r.p.port}{weak}", True)
    return r
