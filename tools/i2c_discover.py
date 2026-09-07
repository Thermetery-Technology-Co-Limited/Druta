"""Read-only survey of a card's I2C buses, for authoring a rail profile.

WRITES NOTHING. Every access here is a read. The tool has no code path that
issues an I2C write, which is deliberate: discovery on an unknown bus is
exactly when a stray write is least recoverable.

What it does: walks the NVAPI I2C ports, records which 7-bit addresses answer,
and for each responder reads the standard PMBus identification and telemetry
commands. It prints a Markdown block to paste into an issue or PR, so a
reviewer sees the same bytes you did.

WHAT IT CANNOT DO, and you should not pretend otherwise in a profile:

  * An address that answers is NOT a voltage regulator. Fans, temperature
    sensors, EEPROMs and display logic live on these buses too.
  * A part that answers is not necessarily YOUR rail. A card can carry several
    regulators; which one is the core rail is something you establish by
    reading its output voltage and comparing it against the GPU's own, not by
    position in this list.
  * Many boards leave the regulator's bus DISCONNECTED from anything the GPU
    can reach. Finding nothing is a normal and common result. It means the
    part is not reachable as shipped, not that the card lacks one.

Usage:
    python tools/i2c_discover.py                 # ports 0-7, common addresses
    python tools/i2c_discover.py --ports 0,1,2   # narrow the sweep
    python tools/i2c_discover.py --full          # every 7-bit address, slower

Run it under load if you want meaningful voltage numbers. At idle a multiphase
controller sheds phases and reads like a different part.
"""
import argparse
import ctypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import nvbackend as nb                                        # noqa: E402

u8, u32 = ctypes.c_uint8, ctypes.c_uint32
PTR, P8 = ctypes.c_void_p, ctypes.POINTER(u8)

# I2CReadEx reaches the card's INTERNAL buses. The documented NvAPI_I2CRead is
# DDC-only and cannot see a regulator, which is why that one is not used here.
I2C_READ_EX, VER3 = 0x4D7B0709, 0x00030040

# Addresses worth trying first. PMBus parts cluster in a small range, so the
# default sweep is quick; --full covers everything.
COMMON_ADDR = list(range(0x10, 0x40)) + [0x40, 0x41, 0x42, 0x43,
                                         0x44, 0x45, 0x46, 0x47,
                                         0x4C, 0x4D, 0x4E, 0x4F,
                                         0x60, 0x61, 0x62, 0x63,
                                         0x67, 0x68, 0x69, 0x6A]

# Standard PMBus identification and telemetry. Read-only commands only.
# A part is allowed to not implement any of these - a bus error here is
# information, not a failure, and several real parts answer almost none of them.
PROBES = [
    (0x99, 1, "MFR_ID"),
    (0x9A, 2, "MFR_MODEL"),
    (0x9B, 1, "MFR_REVISION"),
    (0xBE, 1, "MFR_PMBUS_ADDR (MPS)"),
    (0x20, 1, "VOUT_MODE"),
    (0x8B, 2, "READ_VOUT"),
    (0x8C, 2, "READ_IOUT"),
    (0x8D, 2, "READ_TEMPERATURE_1"),
    (0x98, 1, "PMBUS_REVISION"),
    (0x24, 2, "VOUT_MAX"),
]


def responds(read, port, addr7):
    """Presence via read-only fallbacks; PAGE is absent on real NCP4206 parts.

    Any answered command qualifies for the survey, never for voltage writes.
    """
    return any(read(port, addr7, cmd, width) is not None
               for cmd, width in ((0x00, 1), (0x99, 1), (0x8B, 2)))


class _V3(ctypes.Structure):
    _fields_ = [("version", u32), ("displayMask", u32), ("bIsDDCPort", u8),
                ("i2cDevAddress", u8), ("pbI2cRegAddress", P8),
                ("regAddrSize", u32), ("pbData", P8), ("cbSize", u32),
                ("i2cSpeed", u32), ("i2cSpeedKhz", u32), ("portId", u8),
                ("bIsPortIdSet", u32)]


def _read_notes(got):
    """Flag response patterns that mean "this is not a PMBus part".

    Saying so here is worth more than a clean-looking table, because both
    patterns below produce plausible numbers that decode into believable
    voltages. Someone who does not know to look for them can write an entirely
    self-consistent profile for a device that is not a regulator.
    """
    notes = []
    vals = [v for v in got.values() if v is not None]
    if vals and all(set(v) == {0xFF} for v in vals):
        notes.append("Every register reads `FF`. That is an address that ACKs "
                     "without a device behind it answering meaningfully - "
                     "treat as **not a regulator**.")
    if vals and all(set(v) == {0x00} for v in vals):
        notes.append("Every register reads `00`. Same conclusion as all-`FF`: "
                     "nothing here is answering with content.")
    # A part that ignores the command byte returns a sliding window of one
    # buffer, so consecutive commands come back shifted by a byte.
    seq = [got.get(c) for c, _n, _l in PROBES[:3]]
    if all(s is not None and len(s) >= 8 and len(set(s)) > 1 for s in seq):
        # len(set(s)) > 1 keeps a constant buffer out: all-FF trivially equals
        # itself shifted, and reporting that as a sliding window on top of the
        # all-FF note says the same thing twice from weaker evidence.
        if seq[0][1:] == seq[1][:-1] and seq[1][1:] == seq[2][:-1]:
            notes.append("Consecutive commands return the SAME bytes shifted "
                         "by one. The device is ignoring the command byte and "
                         "streaming a buffer - it is **not PMBus**, and any "
                         "value decoded from it is meaningless.")
    return notes


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ports", default="0,1,2,3,4,5,6,7")
    ap.add_argument("--full", action="store_true",
                    help="sweep every 7-bit address, not just the usual ones")
    ap.add_argument("--slot", default=None,
                    help="PCI slot, when more than one card is present")
    args = ap.parse_args()

    ports = [int(p) for p in args.ports.split(",") if p.strip() != ""]
    addrs = list(range(0x08, 0x78)) if args.full else COMMON_ADDR

    gpu = nb.GPU(args.slot) if args.slot else nb.GPU()
    fn = gpu.nvapi._i(I2C_READ_EX, PTR, PTR, PTR)
    if fn is None:
        sys.exit("NvAPI I2CReadEx is not available on this driver - "
                 "cannot survey the bus.")

    def rd(port, addr7, cmd, n):
        """One read. Returns bytes, or None if the device did not answer.

        Zero the struct and set only the fields that matter: leaving
        i2cSpeedKhz set alongside the i2cSpeed sentinel makes the call fail on
        this driver, and the trailing out-parameter is TWO dwords, not one.
        Both were found the hard way - with either wrong, every address on
        every port reports "nothing there".
        """
        buf = (u8 * n)()
        reg = (u8 * 1)(cmd)
        s = _V3()
        ctypes.memset(ctypes.byref(s), 0, ctypes.sizeof(s))
        s.version = VER3
        s.i2cDevAddress = addr7 << 1               # EXTDEV wants the 8-bit form
        s.pbI2cRegAddress = ctypes.cast(reg, P8)
        s.regAddrSize = 1
        s.pbData = ctypes.cast(buf, P8)
        s.cbSize = n
        s.i2cSpeed = 0xFFFF
        s.portId = port
        s.bIsPortIdSet = 1
        unk = (u32 * 2)()
        st = fn(gpu.nvapi.gpu, ctypes.byref(s), ctypes.byref(unk))
        _keep = (reg, buf, unk)                                  # noqa: F841
        return None if st != 0 else bytes(buf)

    # Straight from NvAPI: these are not in GPU.static, and the [match] section
    # of a profile needs both. Reported even when the sweep finds nothing,
    # because they are what a reviewer needs to place the board.
    dev = sub = None
    if gpu.nvapi.GetPCIIds:
        did, sid, ext, iid = (u32() for _ in range(4))
        if gpu.nvapi.GetPCIIds(gpu.nvapi.gpu, ctypes.byref(did),
                               ctypes.byref(sid), ctypes.byref(ext),
                               ctypes.byref(iid)) == 0:
            # NvAPI packs (device << 16) | vendor. The low half is always
            # 0x10DE, so masking it off yields the vendor id and a profile
            # [match] block that matches every NVIDIA card ever made.
            dev = f"0x{(did.value >> 16) & 0xFFFF:04X}"
            sub = f"0x{sid.value:08X}"
    name = gpu.static.get("name", "unknown")
    print(f"## I2C survey - {name}\n")
    print(f"- PCI device: `{dev}`   subsystem: `{sub}`")
    print(f"- driver: `{gpu.static.get('driver')}`   "
          f"VBIOS: `{gpu.static.get('vbios')}`")
    print(f"- vcore at scan time: `{gpu.read_vcore_mv()} mV` "
          f"(run under load for a meaningful number)")
    print(f"- sweep: ports {ports}, "
          f"{len(addrs)} addresses{' (full)' if args.full else ''}")
    print("\nRead-only survey. This tool issues no I2C writes.\n")

    found = []
    for port in ports:
        for a in addrs:
            # No one command is universal. NCP4206 lacks PAGE, while other
            # responders lack MFR_ID or READ_VOUT, so try each before omission.
            if not responds(rd, port, a):
                continue
            found.append((port, a))

    if not found:
        print("No device answered on any port swept.\n")
        print("That is a normal result: on many boards the regulator's bus is "
              "not connected to anything the GPU can reach. It does not mean "
              "the card has no regulator, and it is not something a profile "
              "can work around.")
        return

    print(f"### Responders: {len(found)}\n")
    for port, a in found:
        print(f"#### port {port}, address `0x{a:02X}`\n")
        print("| reg | name | bytes read |")
        print("| --- | --- | --- |")
        got_all = {}
        for cmd, n, label in PROBES:
            got = rd(port, a, cmd, n)
            got_all[cmd] = got
            if got is None:
                print(f"| `0x{cmd:02X}` | {label} | *not implemented* |")
                continue
            hexs = " ".join(f"{b:02X}" for b in got)
            ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in got)
            extra = f" (`{ascii_}`)" if any(32 <= b < 127 for b in got) else ""
            print(f"| `0x{cmd:02X}` | {label} | `{hexs}`{extra} |")
        for line in _read_notes(got_all):
            print(f"\n> {line}")
        print()

    print("### What to do with this\n")
    print("Identifying the part is YOUR step and it needs a datasheet. Match "
          "the bytes above against candidate parts, then copy "
          "`i2c/TEMPLATE.toml` and fill it in with page citations. If nothing "
          "above names a part, say so in the issue rather than guessing - a "
          "profile built on a guessed identity is worse than no profile, "
          "because it will be trusted.")


if __name__ == "__main__":
    main()
