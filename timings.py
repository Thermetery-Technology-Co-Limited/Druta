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
Read-only decode of this card's FBPA memory-timing registers, via `nvtune`.

THE ONE THING THIS MODULE EXISTS TO SAY: a timing register holds a CYCLE
COUNT, and a cycle count without the clock it was counted against is not a
number. The same registers, read on this card at three memory states, decode
to (NVML mem figure -> GDDR6 true clock = NVML/4):

    NVML  405 (true  101 MHz)   RC=6   RFC=13   RAS=4   RP=2   CL=9   RD_RCD=2
    NVML  810 (true  203 MHz)   RC=11  RFC=25   RAS=7   RP=4   CL=9   RD_RCD=4
    NVML 7428 (true 1857 MHz)   RC=78  RFC=210  RAS=52  RP=26  CL=24  RD_RCD=26

At P0 those convert to RC 42.0 ns, RAS 28.0 ns, RP 14.0 ns, CL 12.9 ns - real
GDDR6 numbers. The same registers at idle look like garbage, and that is what
made the first two dumps of this feature's investigation unreadable. So every
snapshot here brackets the nvtune call with a memory-clock read and REFUSES to
vouch for its own nanosecond column if the clock moved in between.

TWO FIELDS DO NOT CONVERT and are flagged ns_unreliable so no caller can print
a nanosecond figure for them: RFC (210 cyc = 113 ns against a 240-350 ns spec)
and WL (5 cyc = 2.7 ns, where GDDR6 write latency is expressed relative to CL).

AND AN IDLE CAPTURE IS WORTHLESS, while a P2 one is not. Timings are selected
per CLOCK BAND, not per p-state. MEASURED on this card: the registers are
BIT-IDENTICAL at 7228 (P2, CUDA load) and 7428 (P0, 3D load) - all of
CONFIG0..CONFIG5 and TIMING22 - because 50 MHz of true clock does not cross a
VBIOS band boundary. But 405 and 810 program genuinely slacker values. So
.perf_band (in the top band, worth reading) is the axis the loud warning fires
on, and .at_p0 is kept only as an accurate LABEL - it would be the thing that
revealed a divergence if the memory offset ever grew enough to push P2 and P0
into different bands.

That identity is scoped to READING. A throughput benchmark would still have to
run in the state it claims to describe.

The p-state cannot be FORCED here - only INDUCED, by running work and letting
the driver respond (see gpuload.py). nvmlDeviceSetMemoryLockedClocks returns
NVML_ERROR_NOT_SUPPORTED on this card and `nvidia-smi -lmc` fails identically.
An induced state is the driver's to withdraw at any moment, which is exactly
why snapshot() brackets the register read with a clock sample on each side.

SAFETY: this module is READ-ONLY BY CONSTRUCTION. `nvtune` can write memory
controller registers, which can hang the machine and corrupt VRAM. _run()
accepts only the subcommands in READ_ONLY_SUBCOMMANDS and rejects any argument
in FORBIDDEN_TOKENS, and it is the ONLY place in Druta that spawns nvtune.
There is deliberately no code path here - not behind a flag, not disabled, not
dead - that can build an argv able to write a timing register.

No Dear PyGui import, and no nvbackend import either: `gpu` is duck-typed
(anything with .read() and .static), so this module can be unit-tested with a
stub in place of the card.
"""
import ctypes
import itertools
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field as _dc_field

# For slot parsing only. nvbackend loads no driver library at import time - the
# DLLs open in NvAPI/Nvml constructors - so this stays a pure-Python import and
# does not make merely importing timings.py touch the hardware.
import nvbackend

# ---- where the tool lives -------------------------------------------------- #
NVTUNE_EXE = "nvtune.exe"
# nvtune's upstream. A SEPARATE program by Sebastian Marrufo, GPL-3.0-or-later
# - the same licence Druta uses - but a separate work, not a component.
#
# Deliberately not bundled, and NOT for licensing reasons: GPL permits
# redistribution. nvtunedrv.sys is signed with a SELF-SIGNED TEST certificate,
# so loading it costs Secure Boot, Memory Integrity, driver-signature
# enforcement, and a third-party root certificate in LocalMachine\Root. That is
# a machine-wide security decision about nvtune and belongs to its author's
# install instructions, not to a GPU monitor's installer.
NVTUNE_HOME = "https://github.com/sebastianmarrufo/nvtune"
DRIVER_SERVICE = "nvtunedrv"
ENV_OVERRIDE = "DRUTA_NVTUNE"      # full path to nvtune.exe, or its folder
# The pre-rename name, still honoured. Dropping it would have been worse than
# losing a setting: the override is documented as EXCLUSIVE, so a host with the
# old variable set would have stopped pinning and quietly fallen through to the
# derived search - i.e. run a DIFFERENT nvtune, whose register offsets are the
# whole reason the pin exists. Silently, with nothing on screen.
LEGACY_ENV_OVERRIDE = "TITANTUNE_NVTUNE"

# WHERE NVTUNE LIVES IS DISCOVERED, NEVER WRITTEN DOWN.
#
# This used to hold `CONTRACTOR_DIR = r"C:\Users\Administrator\Desktop\nvtune"`,
# which worked on exactly one machine and one account. Everywhere else the
# Timings tab went dark for a reason nothing on screen explained, and the
# username shipped in the repo.
#
# nvtune is a SEPARATE tool that this build deliberately does not ship. Not for
# licensing reasons - it is GPL-3.0-or-later, the same licence as Druta, and
# redistribution is permitted (see NVTUNE_HOME). It is that its driver is
# SELF-SIGNED with a test certificate, so using it costs Secure Boot, Memory
# Integrity, driver-signature enforcement and a third-party root certificate.
# That decision belongs to the operator and to nvtune's own install
# instructions. So it is treated as an external dependency to be located - by
# the operator once, or by derivation from the environment - in this order:
#
#   1. DRUTA_NVTUNE, exclusive: an explicit override never silently falls
#      back to another copy, because every register offset this feature decodes
#      comes out of the binary that gets run.
#   2. the path the operator registered through the UI (config_path()).
#   3. derived locations - beside Druta first, then the usual install roots.
#   4. PATH, last, being the least predictable.
# Vendor\Product, the Windows convention, and the house mark is the vendor.
CONFIG_DIR_NAME = os.path.join("Thermetery", "Druta")
CONFIG_NAME = "nvtune.json"
# Where this lived before the rename. Read once, to migrate, then never again.
LEGACY_CONFIG_DIR_NAME = "TitanTune"


def config_path():
    """Where the registered nvtune location is remembered. LOCALAPPDATA rather
    than beside the app: an EXE in Program Files cannot write next to itself."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, CONFIG_DIR_NAME, CONFIG_NAME)


def legacy_config_path():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, LEGACY_CONFIG_DIR_NAME, CONFIG_NAME)


def _write_json_atomic(path, data):
    """Write JSON such that a crash cannot leave a half-file behind.

    `open(path, "w")` TRUNCATES before a single byte is written, so anything
    that interrupts the dump - kill, power loss, full disk - leaves a zero-byte
    file where a good config used to be. For the migration that is worse than
    losing the write: an empty destination then looks 'already migrated' to any
    existence check and shadows the perfectly good legacy copy forever.

    Temp file in the same directory (so os.replace is a same-volume rename and
    therefore atomic), fsync before the swap, and the temp is removed on any
    failure path."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".nvtune-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _config_is_readable(path):
    """Does `path` hold a config we could actually use?

    The migration guard asks THIS rather than os.path.exists, so a truncated or
    corrupt destination retries the migration instead of permanently shadowing
    a good legacy file."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            return isinstance(json.load(f), dict)
    except (OSError, ValueError):
        return False


# Last migration outcome worth telling the operator about. Surfaced through
# available(); a migration that fails silently is indistinguishable from the
# feature having been dropped by the upgrade, and the user re-hunts a binary
# they had already registered.
_migration_note = ""


def migration_note():
    return _migration_note


def migrate_legacy_config():
    """Carry a pre-rename registration across, once. (moved, message).

    A rename that silently orphans the one setting the operator had to go and
    find is a bad trade for tidiness - the symptom would be the Timings tab
    going dark after an upgrade, with the old file still sitting there looking
    correct. Copies rather than moves, so a downgrade still works.

    The guard is 'is there a USABLE config at the destination', not 'is there a
    file there', for the reason in _write_json_atomic."""
    global _migration_note
    new, old = config_path(), legacy_config_path()
    if _config_is_readable(new) or not os.path.exists(old):
        return False, ""
    try:
        with open(old, encoding="utf-8-sig") as f:   # BOM-tolerant, see above
            data = json.load(f)
        _write_json_atomic(new, data)
    except (OSError, ValueError) as e:
        _migration_note = (
            f"a pre-rename nvtune registration exists at {old} but could not be "
            f"carried over: {e}. Re-register it with Device -> Locate nvtune...")
        return False, _migration_note
    _migration_note = ""
    return True, f"carried the registered nvtune location over from {old}"


def _as_exe(p):
    """A path naming either nvtune.exe or the folder holding it -> the exe."""
    p = os.path.expandvars(os.path.expanduser((p or "").strip().strip('"')))
    if not p:
        return None
    return p if p.lower().endswith(".exe") else os.path.join(p, NVTUNE_EXE)


def configured_exe():
    """The location the operator registered, or None.

    Migration is attempted here rather than at startup so it also covers a
    fresh process that never opens the Timings tab, and it is a no-op the
    moment a current config exists."""
    migrate_legacy_config()
    try:
        # utf-8-sig, not utf-8: this file is meant to be hand-editable, and
        # Notepad (and PowerShell's Set-Content) write a BOM. Reading it as
        # plain utf-8 raises on the BOM and the registration silently vanishes,
        # which looks exactly like never having set it.
        with open(config_path(), encoding="utf-8-sig") as f:
            return _as_exe((json.load(f) or {}).get("nvtune_exe"))
    except (OSError, ValueError, AttributeError):
        return None


def set_configured_exe(path):
    """Register where nvtune lives, or clear it with None/''. (ok, message).

    Verifies the file is actually there before recording it: a registration
    that silently points at nothing would turn the one explicit mechanism into
    another way to be confused about why the tab is empty."""
    exe = _as_exe(path)
    if exe and not os.path.isfile(exe):
        return False, f"no nvtune.exe at {exe}"
    try:
        # atomic for the same reason the migration is: this truncates the live
        # config, and a half-written one reads as no registration at all
        _write_json_atomic(config_path(), {"nvtune_exe": exe or ""})
    except OSError as e:
        return False, f"could not write {config_path()}: {e}"
    return True, (f"nvtune registered at {exe}" if exe
                  else "registered nvtune location cleared")


def _candidate_dirs():
    """Plausible homes for an unshipped nvtune, DERIVED from the environment.

    Beside Druta comes first so a portable copy always wins. The rest are
    the ordinary install roots expanded for whoever is actually logged in -
    which is how the Desktop location this was originally hardcoded to keeps
    working, without the account name being in the source."""
    dirs = [_app_dir(), os.path.dirname(os.path.abspath(__file__))]
    home = os.path.expanduser("~")
    for base in (os.environ.get("LOCALAPPDATA"),
                 os.environ.get("PROGRAMFILES"),
                 os.environ.get("PROGRAMFILES(X86)"),
                 os.path.join(home, "Desktop"),
                 os.path.join(home, "Downloads"),
                 home):
        if base:
            dirs.append(os.path.join(base, "nvtune"))
    return dirs

# ---- THE SAFETY BOUNDARY, IN CODE ------------------------------------------ #
# Every read-only subcommand nvtune has. Anything not in this set cannot be
# spawned by this process at all.
READ_ONLY_SUBCOMMANDS = frozenset({
    "list", "fields", "dump", "get", "save", "probe", "vbios"})
# Belt and braces on top of that whitelist: even a read-only subcommand may not
# carry these. `set`/`restore`/`apply`/`daemon` are the writing subcommands,
# --commit is what turns nvtune's dry run into a hardware write, --force
# defeats its range checks, and -i/--input only feeds `restore`.
FORBIDDEN_TOKENS = frozenset({
    "set", "restore", "apply", "daemon", "--commit", "--force",
    "-i", "--input"})

# Fields whose cycle count is real but whose NANOSECOND conversion is not, with
# the reason each one is refused. Keyed by field name because the reason is a
# property of the DRAM encoding, not of the register layout nvtune reports.
NS_UNRELIABLE = {
    "RFC": "210 cyc = 113 ns against a 240-350 ns GDDR6 tRFC - so this is a "
           "multiplier, or its range splits with TIMING22.RFCSBA/RFCSBR",
    "WL":  "5 cyc = 2.7 ns - GDDR6 write latency is expressed RELATIVE to CL, "
           "not as an absolute delay",
}

# CREATE_NO_WINDOW: without it every capture flashes a console over the UI.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class TimingsError(RuntimeError):
    """nvtune is missing, refused, or said something we cannot parse."""


# ============================================================================ #
#  discovery                                                                   #
# ============================================================================ #
def _app_dir():
    """Where 'next to Druta' means. Under PyInstaller the module lives in
    a temp extraction dir, so the frozen build has to look beside the EXE."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def pinned_exe(override=None):
    """The path an argument or the environment variable pinned us to, or None.
    An override may name nvtune.exe itself or the folder holding it.

    LEGACY_ENV_OVERRIDE is honoured after the current name so an operator who
    set the pre-rename variable keeps their pin - see its comment for why
    losing it would be a wrong answer rather than an inconvenience."""
    for cand in (override, os.environ.get(ENV_OVERRIDE),
                 os.environ.get(LEGACY_ENV_OVERRIDE)):
        if cand and cand.strip():
            cand = os.path.expandvars(os.path.expanduser(cand.strip().strip('"')))
            return (cand if cand.lower().endswith(".exe")
                    else os.path.join(cand, NVTUNE_EXE))
    return None


def search_path(override=None):
    """Every location find_exe() will try, in order. Public so the UI can say
    WHERE it looked instead of just 'not found'."""
    pin = pinned_exe(override)
    if pin:
        # An explicit override is EXCLUSIVE, not merely first: silently falling
        # back to some other copy would mean the tool being run is not the one
        # the operator named, and every register offset in this feature comes
        # out of that binary.
        return [pin]
    out, seen = [], set()
    cfg = configured_exe()
    cands = ([cfg] if cfg else []) + [os.path.join(d, NVTUNE_EXE)
                                      for d in _candidate_dirs() if d]
    for p in cands:
        if p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out


def find_exe(override=None):
    """First nvtune.exe that exists, or None. PATH is the last resort - an exe
    found there is whatever the shell would run, which is the least predictable
    of the candidates, so it never wins over a local copy, and an explicit
    override skips it entirely."""
    for p in search_path(override):
        if os.path.isfile(p):
            return p
    if pinned_exe(override):
        return None
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d.strip('"'), NVTUNE_EXE)
        if d and os.path.isfile(p):
            return p
    return None


# ---- the kernel driver ----------------------------------------------------- #
_SERVICE_STATES = {1: "STOPPED", 2: "START_PENDING", 3: "STOP_PENDING",
                   4: "RUNNING", 5: "CONTINUE_PENDING", 6: "PAUSE_PENDING",
                   7: "PAUSED"}
_SC_MANAGER_CONNECT = 0x0001
_SERVICE_QUERY_STATUS = 0x0004


class _ServiceStatus(ctypes.Structure):
    _fields_ = [("dwServiceType", ctypes.c_uint32),
                ("dwCurrentState", ctypes.c_uint32),
                ("dwControlsAccepted", ctypes.c_uint32),
                ("dwWin32ExitCode", ctypes.c_uint32),
                ("dwServiceSpecificExitCode", ctypes.c_uint32),
                ("dwCheckPoint", ctypes.c_uint32),
                ("dwWaitHint", ctypes.c_uint32)]


def driver_state(name=DRIVER_SERVICE):
    """(state_text, running) for the nvtune BAR0 accessor.

    Queried through the SCM directly rather than by parsing `sc query`: it
    needs no elevation (SERVICE_QUERY_STATUS is granted to authenticated
    users), spawns no console, and cannot be confused by localisation - `sc`
    prints its state names in the system language, this returns a number."""
    try:
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        adv.OpenSCManagerW.restype = ctypes.c_void_p
        adv.OpenServiceW.restype = ctypes.c_void_p
        adv.OpenServiceW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                     ctypes.c_uint32]
        adv.CloseServiceHandle.argtypes = [ctypes.c_void_p]
        scm = adv.OpenSCManagerW(None, None, _SC_MANAGER_CONNECT)
        if not scm:
            return f"service manager unavailable (err {ctypes.get_last_error()})", False
        try:
            svc = adv.OpenServiceW(scm, name, _SERVICE_QUERY_STATUS)
            if not svc:
                err = ctypes.get_last_error()
                # 1060 = ERROR_SERVICE_DOES_NOT_EXIST: not installed at all,
                # which is a different problem from installed-but-stopped.
                return ("not installed" if err == 1060
                        else f"cannot query (err {err})"), False
            try:
                st = _ServiceStatus()
                if not adv.QueryServiceStatus(ctypes.c_void_p(svc),
                                              ctypes.byref(st)):
                    return f"cannot query (err {ctypes.get_last_error()})", False
                s = st.dwCurrentState
                return (_SERVICE_STATES.get(s, f"state {s}"), s == 4)
            finally:
                adv.CloseServiceHandle(ctypes.c_void_p(svc))
        finally:
            adv.CloseServiceHandle(ctypes.c_void_p(scm))
    except Exception as e:
        return f"unknown ({e})", False


@dataclass
class Availability:
    exe: str = None
    driver: str = "unknown"
    driver_running: bool = False
    reason: str = ""

    @property
    def ok(self):
        return bool(self.exe) and self.driver_running


def available(override=None):
    """Can we take a snapshot, and if not, WHICH half is missing. The two
    failures need different fixes - a missing exe is a deployment problem, a
    stopped driver is one `sc start` - so they are never merged into one
    'unavailable'."""
    exe = find_exe(override)
    state, running = driver_state()
    av = Availability(exe=exe, driver=state, driver_running=running)
    if exe and running:
        return av
    if not exe:
        pin = pinned_exe(override)
        if pin:
            av.reason = (f"{NVTUNE_EXE} was pinned to a path that does not "
                         f"exist:\n    {pin}\nAn override is exclusive - "
                         f"Druta will not quietly run a different copy - "
                         f"so fix the path or clear the {ENV_OVERRIDE} "
                         f"environment variable.")
        else:
            looked = "\n".join("    " + p for p in search_path(override))
            av.reason = (
                f"{NVTUNE_EXE} not found. Druta does not ship it - it is a "
                f"separate tool whose kernel driver is self-signed, so "
                f"installing it is your decision to make, not ours to make "
                f"for you - and it has to be located.\n"
                # Worth saying out loud: before this the message told the
                # operator to go find a binary without telling them where one
                # comes from. nvtune is free software and has a public home.
                f"Get it from {NVTUNE_HOME}\n"
                f"Looked in:\n{looked}\n    (then PATH)\n"
                f"Use 'Device -> Locate nvtune...' to register where it is "
                f"(remembered in {config_path()}), or set the {ENV_OVERRIDE} "
                f"environment variable to its full path.")
            # A failed carry-over is the one cause the operator cannot deduce
            # from the list above: their registration still exists, just not
            # where this build looks. Without this they conclude the upgrade
            # dropped the feature and go re-hunt a binary they already found.
            if migration_note():
                av.reason += f"\n\nNOTE: {migration_note()}"
        if not running:
            av.reason += (f"\n\nIts kernel driver service '{DRIVER_SERVICE}' "
                          f"is also not running (state: {state}).")
        return av
    av.reason = (f"{NVTUNE_EXE} is present ({exe}) but its kernel driver "
                 f"service '{DRIVER_SERVICE}' is not running (state: "
                 f"{state}). It maps the GPU's BAR0 FBPA aperture; without it "
                 f"there is nothing to read. Start it from an elevated "
                 f"prompt:  sc start {DRIVER_SERVICE}")
    return av


# ============================================================================ #
#  running it                                                                  #
# ============================================================================ #
def _check_argv(subcmd, args):
    """The write-refusal, enforced rather than documented. Raises before any
    process is created."""
    if subcmd not in READ_ONLY_SUBCOMMANDS:
        raise TimingsError(
            f"refused: '{subcmd}' is not one of the read-only nvtune "
            f"subcommands {sorted(READ_ONLY_SUBCOMMANDS)}")
    for a in args:
        if str(a).strip().lower() in FORBIDDEN_TOKENS:
            raise TimingsError(f"refused: argument '{a}' can write hardware")


def _run(exe, subcmd, args=(), timeout=20.0, slot=None):
    """Spawn a read-only nvtune subcommand against ONE card.

    `slot` is not optional in spirit. nvtune's `-d` defaults to "all NVIDIA
    GPUs", not to the first one, so an un-targeted call on a two-card host does
    something different from what every caller here assumes. Measured:

        nvtune get FAW RRD   -> two lines, one per card
        nvtune save -o P     -> card 1 writes P, card 2 fails "cannot replace",
                                and the file is card 1's registers regardless of
                                which card the caller meant
        nvtune set FAW=13    -> plans an op on BOTH cards

    Passing slot=None is still allowed, because `fields` is genuinely
    card-independent (verified: byte-identical output on TU102 and GP102), but
    anything decoding registers must name a card."""
    args = [str(a) for a in args]
    _check_argv(subcmd, args)
    argv = [exe, subcmd]
    if slot:
        argv += ["-d", str(slot)]
    argv += args
    try:
        return subprocess.run(argv, capture_output=True,
                              text=True, timeout=timeout,
                              creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise TimingsError(f"nvtune {subcmd} did not answer within "
                           f"{timeout:.0f}s")
    except OSError as e:
        raise TimingsError(f"could not run nvtune: {e}")


# ============================================================================ #
#  `nvtune fields` -> the decode table                                         #
# ============================================================================ #
# Parsed at runtime, never hardcoded: the bit ranges are the tool's own, so a
# nvtune that learns a new field or moves one cannot silently disagree with the
# decode this app prints.
_FIELD_RE = re.compile(
    r"^(?P<name>[A-Za-z][\w]*)\s+(?P<reg>[A-Za-z][\w]*)\s+"
    r"\[(?P<hi>\d+):(?P<lo>\d+)\]\s+(?P<max>\d+)\s+(?P<desc>\S.*?)\s*$")
# a row with no field name, annotating a whole register, e.g.
#   "    TIMING22  INFERRED: Offset inferred from TIMINGn = ..."
_REGNOTE_RE = re.compile(
    r"^\s+(?P<reg>[A-Za-z][\w]*)\s+(?P<kind>[A-Z][A-Z_]{2,}):\s*"
    r"(?P<text>.*?)\s*$")
_ALIAS_RE = re.compile(r"(\w+)\s*->\s*(\w+)")
_STRUCTURAL_RE = re.compile(r"\s*\[structural\]\s*")


@dataclass(frozen=True)
class Field:
    name: str
    register: str
    lo: int
    hi: int
    max_value: int
    description: str            # with the [structural] marker lifted out
    raw_description: str
    structural: bool = False
    inferred: bool = False      # its REGISTER's offset is inferred, not known
    ns_unreliable: str = ""     # empty, or why ns is refused for this field
    split_with: tuple = ()      # sibling fields holding the rest of this value

    @property
    def width(self):
        return self.hi - self.lo + 1

    @property
    def bits(self):
        return f"[{self.hi}:{self.lo}]"

    @property
    def width_consistent(self):
        """MAX should be exactly what the bit range can hold. If it is not,
        one of the two was misparsed and the caller should not trust either."""
        return self.max_value == (1 << self.width) - 1

    @property
    def ns_ok(self):
        """Structural and split fields are excluded from ns as firmly as
        RFC/WL are: REFRESH_LO, DELAY0(+_MSB/_HI), OFFSET0..2 and ADR_MIN are
        FRAGMENTS of a value spread over several bit ranges, so a fragment's
        'nanoseconds' would be arithmetic on part of a number."""
        return not (self.ns_unreliable or self.structural or self.split_with)

    @property
    def ns_refusal(self):
        if self.ns_unreliable:
            return self.ns_unreliable
        if self.structural:
            return ("structural - a fragment of a value split across bit "
                    "ranges, not a delay of its own")
        if self.split_with:
            # e.g. REFRESH carries only bits above REFRESH_LO. nvtune marks the
            # fragment structural but not its head, and 12 bits of a 15-bit
            # interval converted to ns is a confident wrong answer.
            return (f"only part of the value - {', '.join(self.split_with)} "
                    f"hold(s) the rest, and the assembly rule is not verified")
        return ""

    def extract(self, regval):
        if regval is None:
            return None
        return (regval >> self.lo) & ((1 << self.width) - 1)


@dataclass
class FieldTable:
    fields: list = _dc_field(default_factory=list)
    reg_notes: dict = _dc_field(default_factory=dict)   # reg -> [(kind, text)]
    aliases: dict = _dc_field(default_factory=dict)     # t0 -> FAW
    notes: list = _dc_field(default_factory=list)       # trailing prose
    raw: str = ""

    def by_name(self, name):
        for f in self.fields:
            if f.name == name:
                return f
        return None

    @property
    def registers(self):
        """Register names in the order the tool listed their first field."""
        out = []
        for f in self.fields:
            if f.register not in out:
                out.append(f.register)
        for r in self.reg_notes:
            if r not in out:
                out.append(r)
        return out

    def inferred_registers(self):
        return [r for r, ns in self.reg_notes.items()
                if any(k == "INFERRED" for k, _ in ns)]

    def note_text(self, register):
        return "  ".join(f"{k}: {t}" for k, t in self.reg_notes.get(register, ()))


# Suffixes nvtune uses when one value is spread over several bit ranges:
# REFRESH + REFRESH_LO, DELAY0 + DELAY0_MSB + DELAY0_HI. Derived from the
# PARSED names rather than a hardcoded list of the split fields, so a tool that
# splits a different field in a later build still gets caught. Deliberately
# narrow - "WR" and "WR_RCD" are two unrelated timings, not one split value.
_SPLIT_SUFFIXES = ("_LO", "_HI", "_MSB", "_LSB", "_MID")


def _split_siblings(name, fields):
    names = {f.name for f in fields}
    out = [name + sfx for sfx in _SPLIT_SUFFIXES if name + sfx in names]
    for sfx in _SPLIT_SUFFIXES:
        if name.endswith(sfx):
            head = name[:-len(sfx)]
            out += [n for n in sorted(names)
                    if n != name and (n == head or n.startswith(head + "_"))]
            break
    return tuple(dict.fromkeys(out))


def parse_fields(text):
    """`nvtune fields` -> FieldTable. Everything after the last table row is
    kept verbatim in .notes, so a tool that grows a new trailing paragraph
    still carries it to the UI instead of dropping it on the floor."""
    fields, reg_notes, aliases, tail = [], {}, {}, []
    seen_row = False
    for line in text.splitlines():
        if not line.strip() or set(line.strip()) <= {"-", "="}:
            continue
        m = _FIELD_RE.match(line)
        if m and m.group("name") != "FIELD":
            seen_row = True
            raw_desc = m.group("desc")
            desc = _STRUCTURAL_RE.sub(" ", raw_desc).strip()
            fields.append(Field(
                name=m.group("name"), register=m.group("reg"),
                hi=int(m.group("hi")), lo=int(m.group("lo")),
                max_value=int(m.group("max")),
                description=desc, raw_description=raw_desc,
                structural="[structural]" in raw_desc,
                ns_unreliable=NS_UNRELIABLE.get(m.group("name"), "")))
            continue
        m = _REGNOTE_RE.match(line)
        if m:
            seen_row = True
            reg_notes.setdefault(m.group("reg"), []).append(
                (m.group("kind"), m.group("text")))
            continue
        if line.lstrip().lower().startswith("aliases"):
            aliases.update({a: b for a, b in _ALIAS_RE.findall(line)})
        if seen_row:
            tail.append(line.rstrip())
    if not fields:
        raise TimingsError("could not parse any field row out of "
                           "`nvtune fields` - the tool's output format has "
                           "changed and the decode would be a guess")
    # An inferred register taints every field in it, so the flag rides on the
    # field the UI actually draws rather than sitting in a footnote.
    inferred = {r for r, ns in reg_notes.items()
                if any(k == "INFERRED" for k, _ in ns)}
    fields = [Field(**{**f.__dict__, "inferred": f.register in inferred,
                       "split_with": _split_siblings(f.name, fields)})
              for f in fields]
    return FieldTable(fields=fields, reg_notes=reg_notes, aliases=aliases,
                      notes=tail, raw=text)


_FT_CACHE = {}
_FT_LOCK = threading.Lock()


def field_table(override=None, refresh=False, timeout=20.0, exe=None):
    """Parsed `nvtune fields`, cached per exe path. Cached because it is static
    for a given tool build and every snapshot needs it.

    Keyed by exe and NOT by card, which on a multi-card host is a claim worth
    checking rather than assuming. Checked: `nvtune fields` is byte-identical
    (same md5, 45 lines) for `-d` TU102, `-d` GP102, and no `-d` at all. The
    table describes the TOOL's field definitions, not the silicon, so one entry
    per build is right and the cache cannot leak a decode across generations."""
    exe = exe or find_exe(override)
    if not exe:
        raise TimingsError(available(override).reason)
    with _FT_LOCK:
        if not refresh and exe in _FT_CACHE:
            return _FT_CACHE[exe]
    r = _run(exe, "fields", timeout=timeout)
    if r.returncode != 0 and not r.stdout.strip():
        raise TimingsError(f"nvtune fields failed (exit {r.returncode}): "
                           f"{(r.stderr or '').strip()[:400]}")
    ft = parse_fields(r.stdout)
    with _FT_LOCK:
        _FT_CACHE[exe] = ft
    return ft


# ============================================================================ #
#  cycles -> nanoseconds                                                       #
# ============================================================================ #
def true_mhz(nvml_mem, divisor):
    """The clock the registers are actually counted against. NVAPI/NVML report
    GDDR at a multiple of the true clock; the divisor is the memory TYPE's, and
    it comes from nvbackend's MEM_TYPES table via gpu.static['mem_div'] - never
    from a constant here, because a wrong divisor turns every ns in this module
    into a plausible lie."""
    if not nvml_mem or not divisor:
        return None
    v = float(nvml_mem) / float(divisor)
    return v if v > 0 else None


def to_ns(cycles, nvml_mem, divisor):
    """Cycle count -> nanoseconds at that memory state. None when any input is
    missing: no clock means no nanoseconds, which is this module's whole
    point."""
    if cycles is None:
        return None
    mhz = true_mhz(nvml_mem, divisor)
    if not mhz:
        return None
    return cycles * 1000.0 / mhz


# ============================================================================ #
#  `nvtune save` -> a decoded snapshot                                         #
# ============================================================================ #
_OUT_DIR = None
_OUT_LOCK = threading.Lock()
_SEQ = itertools.count(1)


def output_dir():
    """Where the raw JSON lands: a per-process temp dir, NOT the repo - a
    capture is machine state, not source, and the repo is a git working tree.

    Nothing here deletes it. The JSON is the evidence the decode was made
    from, it is ~1.5 KB per capture, and the OS owns the directory."""
    global _OUT_DIR
    with _OUT_LOCK:
        if _OUT_DIR is None:
            _OUT_DIR = tempfile.mkdtemp(prefix="Druta-timings-")
        return _OUT_DIR


@dataclass
class Reading:
    """One decoded field at one memory state."""
    field: Field
    cycles: int = None
    ns: float = None
    ns_refusal: str = ""

    @property
    def name(self):
        return self.field.name


@dataclass
class Snapshot:
    ok: bool = False
    error: str = ""
    # identity, straight out of the JSON
    fmt: str = ""
    taken: str = ""
    slot: str = ""
    boot0: str = ""
    chipset: str = ""
    codename: str = ""
    pci_id: str = ""
    aperture: str = ""
    # scope -> {register name -> int}
    registers: dict = _dc_field(default_factory=dict)
    readings: list = _dc_field(default_factory=list)     # broadcast decode
    scopes: list = _dc_field(default_factory=list)       # fbpa0..fbpaN
    divergence: list = _dc_field(default_factory=list)   # per-partition diffs
    # THE clock this snapshot's nanoseconds are counted against
    mem_before: int = None
    mem_after: int = None
    mem_div: int = None
    mem_type: str = ""
    # every memory clock the driver enumerates, for the P0 classification
    mem_states: list = _dc_field(default_factory=list)
    # The NVML performance state either side of the capture. AUTHORITATIVE for
    # the P0 question - see at_p0 for why the memory clock alone is not.
    pstate_before: int = None
    pstate_after: int = None
    # applied memory offset in REPORTED units, so a reading can be traced back
    # to the enumerated state it sits on (7228 - 427 = 6801)
    mem_offset: float = 0.0
    warnings: list = _dc_field(default_factory=list)
    json_path: str = ""
    wall: float = 0.0            # time.time() of the capture
    elapsed: float = 0.0         # how long the nvtune call took

    @property
    def mem_stable(self):
        """Did the memory clock hold still across the capture? A False here
        makes the whole ns column meaningless, which is why it is a property of
        the snapshot and not an afterthought in the UI."""
        return (self.mem_before is not None
                and self.mem_before == self.mem_after)

    @property
    def mem_nvml(self):
        """The reported figure the ns column was computed from: the reading
        taken BEFORE the registers, so it is never a clock the card moved to
        afterwards."""
        return self.mem_before

    @property
    def mem_true_mhz(self):
        return true_mhz(self.mem_nvml, self.mem_div)

    @property
    def ns_trustworthy(self):
        """May the caller PRINT the ns column at all. Measured on this card:
        a capture that straddled an 810 -> 7428 reclock decoded RC=78 (the P0
        cycle count) against the 810 clock read a moment earlier and produced
        42 ns of truth as 385 ns of nonsense. .ns is still computed and kept -
        nothing is thrown away - but a UI that shows a number here is showing
        arithmetic against a clock the card had already left."""
        return self.mem_stable and self.mem_true_mhz is not None

    @property
    def key(self):
        """Captures are keyed by their memory state - that is the axis the
        comparison view compares along."""
        return self.mem_nvml

    # ---- is this the state anyone tunes for? -------------------------------- #
    @property
    def mem_top(self):
        """The highest memory clock the driver ENUMERATES (7001 on this card).
        Not the highest one reachable - see at_p0."""
        return max(self.mem_states) if self.mem_states else None

    @property
    def pstate(self):
        """The worst (highest-numbered = lowest-performance) p-state seen
        across the capture, or None."""
        ps = [p for p in (self.pstate_before, self.pstate_after)
              if p is not None]
        return max(ps) if ps else None

    @property
    def at_p0(self):
        """True / False / None(cannot tell).

        THE P-STATE IS AUTHORITATIVE, and the memory clock alone is not.
        Measured on this card: a CUDA memcpy load holds memory at 7228, which
        is ABOVE the top clock the driver enumerates (7001) and yet is still
        P-STATE 2 - the compute p-state cap. A clock-only test calls that P0
        and it is not; it is one state down (6801 enumerated + the 427 memory
        offset). The proof-of-concept this was built from made exactly that
        mistake and reported 'reached P0' for a 7228/P2 load.

        The clock still has to agree: 'P0' here means the top state with the
        memory actually up there, so both the p-state and the clock must
        qualify, on BOTH bracketing reads. A capture that started at 810 and
        ended at 7428 straddled a reclock and is a P0 reading of nothing."""
        top = self.mem_top
        seen = [v for v in (self.mem_before, self.mem_after) if v is not None]
        ps = self.pstate
        if not seen or not top:
            return None
        clock_ok = min(seen) >= top
        if ps is None:
            # no p-state to check against: fall back to the clock, and
            # state_headline says the classification is unverified
            return clock_ok
        return clock_ok and ps == 0

    @property
    def matched_state(self):
        """Which ENUMERATED memory state this reading sits on, once the applied
        offset is taken back off: 7228 - 427 = 6801, 7428 - 427 = 7001. None
        when it cannot be matched confidently.

        Tolerant by a few units on purpose - the offset is carried in
        DDR-doubled NVML units (856, i.e. 428 reported) while the delta that
        actually lands is 427, so an equality test would match nothing."""
        if not self.mem_states or self.mem_nvml is None:
            return None
        base = self.mem_nvml - (self.mem_offset or 0)
        best = min(self.mem_states, key=lambda s: abs(s - base))
        return best if abs(best - base) <= 5 else None

    @property
    def band_floor(self):
        """The bottom of the TOP CLOCK BAND - the second-highest enumerated
        state. Captures at or above this read the same timing registers."""
        if len(self.mem_states) > 1:
            return self.mem_states[-2]
        return self.mem_top

    @property
    def perf_band(self):
        """True / False / None. Is this capture worth reading AT ALL?

        THE REAL AXIS, and it is not the p-state. MEASURED on this card: the
        timing registers are BIT-IDENTICAL at 7228 (P2, CUDA load) and 7428
        (P0, 3D load) - all of CONFIG0..CONFIG5 and TIMING22, every decoded
        field. Timings are selected per CLOCK BAND, not per p-state, and the
        50 MHz of true clock between P2 and P0 does not cross a VBIOS band
        boundary.

        So a P2 capture is NOT second-rate: it is the same data. What is
        useless is an IDLE capture - 405 and 810 genuinely program different,
        far slacker values. That is the distinction this property draws, and
        it is the one the loud warning fires on.

        Scoped to READING. If a write phase ever happens, a bandwidth
        benchmark has to run in the state being reasoned about - measuring
        throughput at P2 and reporting it as a P0 result is a different error
        that this identity does not excuse."""
        floor = self.band_floor
        seen = [v for v in (self.mem_before, self.mem_after) if v is not None]
        if not floor or not seen:
            return None
        return min(seen) >= floor

    @property
    def above_enumerated(self):
        """How far past the enumerated top this capture sat, or 0. Non-zero is
        normal and expected here - it is the memory offset, not an error."""
        if self.mem_top and self.mem_nvml:
            return max(0, self.mem_nvml - self.mem_top)
        return 0

    @property
    def state_headline(self):
        """The sentence the tab leads with.

        The loud case is an IDLE capture, not a non-P0 one. An idle capture is
        the same class of error this whole module exists to prevent - a number
        that looks authoritative but was measured in a state nobody runs work
        in - so it is said in full, at the top. A P2 capture is not that error:
        its registers are bit-identical to P0's (see perf_band)."""
        if self.perf_band is None:
            return ("MEMORY STATE UNKNOWN: the driver did not enumerate its "
                    "supported memory clocks, so this capture cannot be "
                    "placed in a clock band. Treat it as unverified.")
        ps = self.pstate
        where = (f"NVML {self.mem_nvml}"
                 + (f" (enumerated state {self.matched_state} + "
                    f"{self.mem_offset:.0f} memory offset)"
                    if self.matched_state else "")
                 + (f", P-STATE {ps}" if ps is not None else ""))
        moved = ("" if self.mem_stable else
                 f", and it moved to {self.mem_after} mid-capture")
        if not self.perf_band:
            return (f"CAPTURED AT {where}{moved}: these are IDLE-state "
                    f"timings and say NOTHING about performance - the card "
                    f"programs genuinely slacker values down here. Run a GPU "
                    f"load - 'Induce P-state' below is enough - and capture "
                    f"again in the top clock band (≥{self.band_floor}).")
        if self.at_p0:
            # the band does not need the p-state, but the P0 LABEL does
            unverified = ("" if ps is not None else
                          " (p-state unreadable, so the P0 label rests on the "
                          "clock alone; the band does not depend on it)")
            return (f"P0, top clock band: captured at {where}{moved}"
                    f"{unverified}. These are the timings the card runs work "
                    f"at.")
        # P2 is not a lesser reading, and must not be described as one.
        return (f"TOP CLOCK BAND: captured at {where}{moved}. This is the "
                f"same timing data as P0 - measured on this card, all of "
                f"CONFIG0..CONFIG5 and TIMING22 are BIT-IDENTICAL at 7228 "
                f"(P2) and 7428 (P0), because timings are selected per clock "
                f"BAND, not per p-state. Valid for reading; no 3D load or "
                f"compute-cap change needed.")

    @property
    def state_tag(self):
        """Short label for a table heading. The band matters more than the
        p-state number, so an in-band non-P0 capture reads as 'P2 band', not
        as a failure."""
        if self.perf_band is None:
            return "?"
        ps = self.pstate
        if self.at_p0:
            return "P0"
        if self.perf_band:
            return f"P{ps} band" if ps is not None else "band"
        return "idle"

    def by_name(self, name):
        for r in self.readings:
            if r.field.name == name:
                return r
        return None


def _state_now(gpu):
    """(reported memory clock, p-state), either may be None. Deliberately one
    full gpu.read(): it is the same call the telemetry thread makes, so this
    cannot drift from what the Monitor tab shows, and the clock and the p-state
    come from the SAME instant - reading them separately would be two different
    moments of a card that reclocks on its own."""
    if gpu is None:
        return None, None
    try:
        d = gpu.read()
        return d.get("mem"), d.get("pstate")
    except Exception:
        return None, None


def _scope_order(registers):
    """broadcast first, then fbpa0..fbpaN numerically (not '10' before '2')."""
    def k(name):
        m = re.match(r"fbpa(\d+)$", name)
        return (1, int(m.group(1))) if m else (0, 0)
    return sorted((s for s in registers if s != "broadcast"), key=k)


def _slot_of(gpu):
    """The PCI slot to target, taken from the GPU object the caller passed.

    Deliberately derived from `gpu` rather than accepted as a separate argument:
    the whole failure this guards against is a snapshot describing one card
    while the mem_div, memory type and clock used to decode it come from
    another, and those all come from `gpu`. Tying both to one object makes the
    two impossible to pass in disagreeing."""
    try:
        s = gpu.slot() if gpu is not None else ""
    except Exception:
        s = ""
    return s or ""


def snapshot(gpu=None, override=None, timeout=20.0):
    """One `nvtune save`, decoded, with the memory clock captured ATOMICALLY
    around it.

    The clock is read immediately before AND immediately after the nvtune call
    and both are kept. If they disagree the registers were sampled while the
    card was reclocking, and the nanosecond column is arithmetic against a
    clock that no longer applies - the snapshot says so rather than printing
    it. This is the exact failure that made the first two dumps of this
    investigation look like garbage.

    Never raises: returns a Snapshot with ok=False and a reason, because the
    caller is a UI tick."""
    snap = Snapshot(wall=time.time())
    try:
        av = available(override)
        if not av.ok:
            snap.error = av.reason
            return snap
        ft = field_table(override=override, timeout=timeout, exe=av.exe)

        if gpu is not None:
            snap.mem_div = gpu.static.get("mem_div")
            snap.mem_type = gpu.static.get("mem_type", "unknown")
            snap.mem_states = sorted(gpu.static.get("mem_clocks") or [])
            try:
                # NVML mem-offset units are DDR-doubled, so the delta that
                # lands on the REPORTED clock is half of it (856 -> 428)
                off = gpu.read().get("mem_off") or 0
                snap.mem_offset = off / 2.0
            except Exception:
                snap.mem_offset = 0.0
        path = os.path.join(output_dir(),
                            f"snapshot-{next(_SEQ):03d}-"
                            f"{time.strftime('%H%M%S')}.json")

        # --- the atomic bracket ------------------------------------------- #
        # An INDUCED state is not a held one: the driver can drop out of it at
        # any moment, which is exactly why both ends are sampled rather than
        # trusting one reading to describe the whole capture.
        slot = _slot_of(gpu)
        if gpu is not None and not slot:
            snap.error = ("the GPU object could not name its PCI slot, and an "
                          "un-targeted nvtune save reads whichever card the "
                          "tool picks - refusing rather than decoding registers "
                          "that may belong to another card")
            return snap

        snap.mem_before, snap.pstate_before = _state_now(gpu)
        t0 = time.perf_counter()
        r = _run(av.exe, "save", ["-o", path], timeout=timeout, slot=slot)
        snap.elapsed = time.perf_counter() - t0
        snap.mem_after, snap.pstate_after = _state_now(gpu)
        # ------------------------------------------------------------------ #

        if not os.path.isfile(path):
            snap.error = (f"nvtune save wrote no file (exit {r.returncode}): "
                          f"{(r.stderr or r.stdout or '').strip()[:400]}")
            return snap
        snap.json_path = path
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            js = json.load(fh)

        snap.fmt = js.get("_format", "")
        snap.taken = js.get("_taken", "")
        snap.slot = js.get("slot", "")

        # The capture names its own card, so the targeting is CHECKED rather
        # than trusted. Without this, a stale file left at `path` - or an nvtune
        # that ignored -d - would be decoded against gpu.static's mem_div and
        # memory type, turning every nanosecond in the table into a confident
        # lie about the wrong silicon.
        if slot and snap.slot and not nvbackend.same_slot(snap.slot, slot):
            snap.error = (f"nvtune was asked for {slot} but the snapshot it "
                          f"wrote is from {snap.slot} - refusing to decode one "
                          f"card's registers against another card's memory "
                          f"clock")
            return snap
        snap.boot0 = js.get("boot0", "")
        snap.chipset = js.get("chipset", "")
        snap.codename = js.get("codename", "")
        snap.pci_id = js.get("pci_id", "")
        snap.aperture = js.get("aperture_broadcast", "")

        regs = js.get("registers") or {}
        for scope, vals in regs.items():
            snap.registers[scope] = {}
            for name, hexs in (vals or {}).items():
                try:
                    snap.registers[scope][name] = int(str(hexs), 16)
                except (TypeError, ValueError):
                    snap.warnings.append(
                        f"{scope}.{name} is not a hex word: {hexs!r}")
        if "broadcast" not in snap.registers:
            snap.error = "snapshot JSON has no 'broadcast' register set"
            return snap
        snap.scopes = _scope_order(snap.registers)

        bc = snap.registers["broadcast"]
        for f in ft.fields:
            if f.register not in bc:
                # a field whose register this build did not dump: shown with
                # no value rather than silently dropped from the table
                snap.readings.append(Reading(field=f, ns_refusal="register "
                                             f"{f.register} not in snapshot"))
                continue
            if not f.width_consistent:
                snap.warnings.append(
                    f"{f.name}: MAX {f.max_value} does not match {f.bits} - "
                    f"the fields table was misparsed, value not trusted")
            cyc = f.extract(bc[f.register])
            if f.ns_ok:
                ns = to_ns(cyc, snap.mem_nvml, snap.mem_div)
                refusal = "" if ns is not None else _why_no_ns(snap)
            else:
                # the field itself refuses, whatever the clock did
                ns, refusal = None, f.ns_refusal
            snap.readings.append(
                Reading(field=f, cycles=cyc, ns=ns, ns_refusal=refusal))

        # per-partition divergence, at FIELD level: the six partitions are
        # identical on this card, so the normal answer is an empty list and the
        # UI stays quiet instead of drawing six copies of the same table
        for scope in snap.scopes:
            sv = snap.registers[scope]
            for f in ft.fields:
                if f.register not in sv or f.register not in bc:
                    continue
                a, b = f.extract(bc[f.register]), f.extract(sv[f.register])
                if a != b:
                    snap.divergence.append(
                        {"scope": scope, "field": f.name,
                         "register": f.register, "broadcast": a, "value": b})

        if snap.mem_before is None:
            snap.warnings.append(
                "no memory clock was readable around this capture - every "
                "cycle count below is unanchored and the ns column is empty")
        elif not snap.mem_stable:
            snap.warnings.append(
                f"THE MEMORY CLOCK MOVED DURING CAPTURE: {snap.mem_before} -> "
                f"{snap.mem_after} (reported). These registers were read while "
                f"the card was reclocking, so the ns column is arithmetic "
                f"against a clock that no longer applies - treat the cycle "
                f"counts as unanchored and take the capture again once the "
                f"clock settles.")
        if snap.mem_div is None and snap.mem_before is not None:
            snap.warnings.append(
                f"memory type '{snap.mem_type or 'unknown'}' has no known "
                f"true-clock divisor, so no cycle count here can be converted "
                f"to nanoseconds")
        snap.ok = True
        return snap
    except TimingsError as e:
        snap.error = str(e)
        return snap
    except Exception as e:                       # never take the UI down
        snap.error = f"{type(e).__name__}: {e}"
        return snap


def _why_no_ns(snap):
    if snap.mem_nvml is None:
        return "no memory clock captured"
    if not snap.mem_div:
        return "memory type has no known true-clock divisor"
    return ""


# ============================================================================ #
#  comparing captures - the actual payload                                     #
# ============================================================================ #
# How close a measured ratio has to sit to the clock ratio before it counts as
# proof, ON TOP OF the rounding slack computed per pair below. "flat" is a
# separate verdict rather than a failure: a field that does not move with the
# clock is usually a mode or bus-turnaround value counted in cycles by design.
RATIO_TOL = 0.10
FLAT_TOL = 0.10


def cycles_agree(c_base, c_other, clock_ratio):
    """Is `c_other` where a FIXED PHYSICAL TIME would have put it, given the
    clock ran `clock_ratio` faster?

    Judged in CYCLES, not in ratios. A register holds whole cycles, so each
    count carries up to ±1 of rounding - and on the baseline, which is the
    small number, that ±1 is worth ±clock_ratio cycles once scaled up. Doing
    this in ratio space instead was measurably wrong: at a 2-cycle baseline the
    rounding band swamps everything and a field that never moved at all came
    out looking like it had scaled by 18x.

    Measured, the case this has to get right: RFC is 13 cycles at NVML 405 and
    210 at 7428. A fixed time predicts 13 x 18.34 = 238; it reads 210, and the
    28-cycle gap fits inside the rounding of that 13. Same field, same decode,
    correctly called agreement."""
    pred = c_base * clock_ratio
    # ±1 cycle on the baseline (worth clock_ratio once scaled) + ±1 on the fast
    # count, then a proportional band for genuine per-p-state VBIOS tweaks.
    tol = clock_ratio + 1.0 + RATIO_TOL * abs(pred)
    return abs(c_other - pred) <= tol


@dataclass
class Comparison:
    """One field across every capture, with its measured scaling next to the
    memory-clock scaling it has to match if the decode is right."""
    name: str
    register: str
    bits: str
    cycles: list = _dc_field(default_factory=list)   # one per capture
    ratios: list = _dc_field(default_factory=list)   # vs the baseline capture
    verdict: str = ""
    note: str = ""


def compare(snapshots):
    """(baseline, clock_ratios, rows). `snapshots` is any iterable of ok
    Snapshots; they are ordered by memory clock and the SLOWEST is the
    baseline, so every ratio is >= 1 and reads the same way down the table.

    Two ratios agreeing - what the field's cycle count did, next to what the
    clock did - is what proves the decode. Everything else in this module is
    plumbing for that one comparison."""
    snaps = sorted([s for s in snapshots if s.ok and s.mem_nvml],
                   key=lambda s: s.mem_nvml)
    if len(snaps) < 2:
        return (snaps[0] if snaps else None), [], []
    base = snaps[0]
    clock_ratios = [s.mem_nvml / float(base.mem_nvml) for s in snaps]
    rows = []
    for r0 in base.readings:
        f = r0.field
        cyc, ratios = [], []
        for s in snaps:
            r = s.by_name(f.name)
            cyc.append(r.cycles if r else None)
        b = cyc[0]
        for v in cyc:
            ratios.append((v / float(b)) if (b and v is not None) else None)
        row = Comparison(name=f.name, register=f.register, bits=f.bits,
                         cycles=cyc, ratios=ratios)
        row.verdict, row.note = _verdict(cyc, ratios, clock_ratios, f)
        rows.append(row)
    return base, clock_ratios, rows


def _verdict(cycles, ratios, clock_ratios, f):
    """tracks / flat / partial / -- , and why."""
    pairs = [(m, c, cycles[0], cycles[i + 1])
             for i, (m, c) in enumerate(zip(ratios[1:], clock_ratios[1:]))
             if m is not None and c]
    # Only pairs where the CLOCK actually moved can prove anything - two
    # captures at the same clock make every field look like it agrees.
    pairs = [p for p in pairs if abs(p[1] - 1.0) > FLAT_TOL]
    if not pairs:
        return "--", ("no second memory state to compare against - these "
                      "captures are at effectively the same clock")
    # FLAT IS TESTED FIRST, and that order is load-bearing: a field that did
    # not move cannot be evidence that it scales with the clock, however
    # generous the rounding band around a 2-cycle count gets.
    if all(abs(m - 1.0) <= FLAT_TOL for m, _c, _b, _o in pairs):
        return "flat", ("unchanged across the clock states - a mode or "
                        "bus-turnaround field, counted in cycles by design")
    if all(cycles_agree(b, o, c) for _m, c, b, o in pairs):
        return "tracks", ("moved by the memory-clock ratio - this field IS a "
                          "cycle count of a fixed time, which is the decode "
                          "being right")
    worst = max(pairs, key=lambda p: abs(p[0] - p[1]) / p[1])
    off = (worst[0] - worst[1]) / worst[1] * 100.0
    note = f"moved, but {off:+.0f}% off the clock ratio"
    if f.structural:
        note += " - expected: this is a fragment of a split field"
    else:
        # NOT presented as a failure. The VBIOS programs each p-state
        # separately and RELAXES timings at low clocks - measured: RC is 42 ns
        # at P0 and 59 ns at idle - so a field can be a perfectly good cycle
        # count and still not scale by the clock ratio alone.
        note += (" - the VBIOS also relaxes timings at low clocks, so this is "
                 "not necessarily a bad decode; read the raw counts")
    return "partial", note


# ============================================================================ #
#  standalone check:  python timings.py                                        #
# ============================================================================ #
if __name__ == "__main__":
    av = available()
    print(f"nvtune : {av.exe or '(not found)'}")
    print(f"driver : {DRIVER_SERVICE} {av.driver}")
    if not av.ok:
        print("\n" + av.reason)
        raise SystemExit(1)
    ft = field_table()
    print(f"fields : {len(ft.fields)} over {', '.join(ft.registers)}"
          f"   inferred: {ft.inferred_registers() or 'none'}"
          f"   aliases: {ft.aliases}")
    try:
        from nvbackend import GPU, slot_from_argv
        g = GPU(slot_from_argv())
    except Exception as e:
        print(f"(no GPU backend: {e})")
        g = None
    s = snapshot(g)
    if not s.ok:
        print("\nsnapshot failed:\n" + s.error)
        raise SystemExit(1)
    print(f"\n{s.codename}  {s.pci_id}  aperture {s.aperture}  "
          f"mem {s.mem_nvml} reported"
          + (f" / {s.mem_true_mhz:.0f} MHz true" if s.mem_true_mhz else "")
          + ("" if s.mem_stable else f"  ** MOVED to {s.mem_after} **"))
    print(f"states : {s.mem_states}  top {s.mem_top}  at_p0={s.at_p0}")
    print(f"\n{s.state_headline}\n")
    print(f"json   : {s.json_path}")
    for w in s.warnings:
        print("  ! " + w)
    print(f"\n{'FIELD':<12}{'REG':<10}{'BITS':<9}{'CYC':>6}   ns")
    for r in s.readings:
        ns = f"{r.ns:8.2f}" if r.ns is not None else f"  {r.ns_refusal[:44]}"
        print(f"{r.name:<12}{r.field.register:<10}{r.field.bits:<9}"
              f"{'--' if r.cycles is None else r.cycles:>6}   {ns}")
    print(f"\ndivergent partitions: {s.divergence or 'none'}")
