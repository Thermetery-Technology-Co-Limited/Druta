# Druta - Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Persisted setting for V/F hold headroom (see GPU.apply_hold_headroom).

Default ON with a 25 mV margin: a fixed safe margin for every card, not tuned
per card or generation, and the user's to change. Holds on the effective voltage
ceiling (reliability plus its boost contribution, alt-reliability, overvoltage)
ran below the clock they showed on one TITAN RTX (TU102, 27-37 MHz) and one RTX
3070 Ti (GA104, one 15 MHz step), and 25 mV of headroom removed the loss on both
(experiments/hold-headroom-*.md). Those runs show that the loss exists and that
25 mV clears it; they are not a procedure. Nobody is expected to sweep margins
(6.25, 12.5, ...) per card or generation, and a change elsewhere does not need
a margin measurement to be accepted.
"""
import json
import math
import os
import re
import subprocess
import sys

from .startup import atomic_json, read_json, state_dir

DEFAULT_ENABLED = True
DEFAULT_MARGIN_MV = 25.0
# Bounds on what the box accepts. The floor is one TU102 V/F point (6.25 mV);
# the ceiling keeps a typo from asking for a large raise.
MIN_MARGIN_MV = 6.25
MAX_MARGIN_MV = 100.0


def config_path():
    return state_dir() / "vf-headroom.json"


def clamp_margin(value):
    """A finite margin inside the accepted band, or the default."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return DEFAULT_MARGIN_MV
    if not math.isfinite(value):
        return DEFAULT_MARGIN_MV
    return min(MAX_MARGIN_MV, max(MIN_MARGIN_MV, value))


def load(path=None):
    """(enabled, margin_mv). A missing, unreadable or malformed file gives the
    defaults: a broken settings file must not silently turn the protection off."""
    try:
        data = read_json(path or config_path(), default={}) or {}
    except (OSError, ValueError, RecursionError):
        data = {}
    enabled = data.get("enabled", DEFAULT_ENABLED)
    if not isinstance(enabled, bool):
        enabled = DEFAULT_ENABLED
    return enabled, clamp_margin(data.get("margin_mv", DEFAULT_MARGIN_MV))


def save(enabled, margin_mv, path=None):
    """Persist the setting. Returns (ok, message)."""
    try:
        atomic_json(path or config_path(),
                    {"version": 1, "enabled": bool(enabled),
                     "margin_mv": clamp_margin(margin_mv)})
        return True, "saved"
    except OSError as exc:
        return False, f"could not save the headroom setting: {exc}"


# ---- active-raise marker -------------------------------------------------- #
# Written while a headroom raise is in the driver (write-ahead: just before the
# SET), removed when it is restored. Rail limits outlive the process (a crash,
# a kill, a power cut), and the next session would read them as its first-read
# values. The marker is the evidence that lets that session say so. It holds
# only what Druta itself wrote and is never replayed by this module.
#
# One file per GPU (named from its NVML UUID), so markers for different cards
# never share a read-modify-write. Each record names the process that owns it
# (PID + process creation time). Two Druta windows can drive the same card
# ("Open a second window on"), and a marker whose owner is still running is a
# LIVE raise, not a crash: it must not produce a dialog, and it must not be
# cleared or overwritten by another window. A file that exists but cannot be
# read is evidence of unknown content and is never overwritten or deleted.

MARKER_VERSION = 2
# Versions this build can judge. Version 1 had no owner (it reads as dead).
# A marker of any other version is from a build that may name its fields
# differently: it is kept as evidence of unknown content, like an unreadable one.
KNOWN_MARKER_VERSIONS = (1, MARKER_VERSION)
# GPU.VOLT_LIMIT_FIELDS: the only field names a marker may carry (pinned by a
# test, so this module need not import the backend).
LIMIT_FIELDS = ("reliability", "alt_reliability", "overvoltage", "vmin")
_STILL_ACTIVE = 259


def active_dir():
    return state_dir() / "vf-headroom-active"


def marker_path(uuid, root=None):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", uuid)
    return (root or active_dir()) / f"{safe}.json"


def _kernel():
    import ctypes
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
    kernel.GetExitCodeProcess.restype = w.BOOL
    kernel.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4
    kernel.GetProcessTimes.restype = w.BOOL
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL
    kernel.GetTickCount64.argtypes = []
    kernel.GetTickCount64.restype = ctypes.c_ulonglong
    kernel.GetSystemTimeAsFileTime.argtypes = [ctypes.POINTER(w.FILETIME)]
    kernel.GetSystemTimeAsFileTime.restype = None
    return ctypes, w, kernel


def boot_filetime():
    """This boot's start as Windows FILETIME ticks (100 ns), or None."""
    if os.name != "nt":
        return None
    ctypes, w, kernel = _kernel()
    now = w.FILETIME()
    kernel.GetSystemTimeAsFileTime(ctypes.byref(now))
    now_ticks = (now.dwHighDateTime << 32) | now.dwLowDateTime
    return now_ticks - int(kernel.GetTickCount64()) * 10_000


def process_created(pid):
    """Creation time (Windows FILETIME ticks) of a RUNNING process `pid`.

    Returns an int for a running process, None for no such running process,
    and "unknown" when the process exists but cannot be queried (access
    denied): unknown is treated as alive, so a live raise is never mistaken
    for a crash."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    if os.name != "nt":
        return 0 if pid == os.getpid() else None
    ctypes, w, kernel = _kernel()
    handle = kernel.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        error = ctypes.get_last_error()
        return "unknown" if error == 5 else None      # 5 = ERROR_ACCESS_DENIED
    try:
        code = w.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            return "unknown"
        if code.value != _STILL_ACTIVE:
            return None
        created, ended, kernel_t, user_t = (w.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(ended),
                                      ctypes.byref(kernel_t), ctypes.byref(user_t)):
            return "unknown"
        return (created.dwHighDateTime << 32) | created.dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


def current_owner():
    return {"pid": os.getpid(), "created": process_created(os.getpid())}


def owner_state(owner):
    """'self', 'alive' (another running Druta process) or 'dead'.

    A missing or malformed owner cannot be tied to a running window: dead. A
    PID that now belongs to a different process (creation time differs) is
    dead. An owner created before this boot is dead whatever access the
    current holder of its PID allows, so an access-denied PID reuse cannot keep
    a crashed session's marker "alive" forever.

    The process is asked FIRST: a running process whose creation time can be
    read is decisive either way, whatever the wall clock says - a clock stepped
    forward after the owner started would otherwise put its creation "before
    this boot" and read a live window's raise as a crash. The boot comparison
    only decides when the process exists but cannot be queried.

    KNOWN LIMIT: "this boot" comes from GetTickCount64, which a Windows Fast
    Startup shutdown does not reset. After one, a crashed session's owner can
    still read as newer than the boot, and if its PID has been reused by a
    process this user cannot query, the marker reads as alive. The effect is a
    missing dialog, never lost evidence: an alive owner's marker is not
    cleared, and a restart or cold boot resolves it."""
    if not isinstance(owner, dict) or type(owner.get("pid")) is not int:
        return "dead"
    me = current_owner()
    if owner["pid"] == me["pid"] and owner.get("created") == me["created"]:
        return "self"
    stored = owner.get("created")
    created = process_created(owner["pid"])
    if created is None:
        return "dead"
    if type(created) is int and type(stored) is int:
        return "alive" if created == stored else "dead"
    boot = boot_filetime()
    if type(stored) is int and boot is not None and stored < boot:
        return "dead"
    return "alive"


def _valid_record(record):
    if not isinstance(record, dict):
        return False
    written, prior = record.get("written_uv"), record.get("prior_uv")
    return (type(record.get("rail")) is int and isinstance(written, dict)
            and isinstance(prior, dict) and written and set(written) == set(prior)
            and set(written) <= set(LIMIT_FIELDS)
            and all(type(v) is int for v in list(written.values()) + list(prior.values())))


def marker_state(uuid, root=None):
    """('missing' | 'ok' | 'unreadable', record_or_None) for `uuid`'s file.
    'unreadable' also covers a marker of a version this build cannot judge."""
    path = marker_path(uuid, root)
    if not path.exists():
        return "missing", None
    try:
        data = read_json(path, default=None)
    except (OSError, ValueError, RecursionError):
        return "unreadable", None
    if not _valid_record(data) or data.get("version", 1) not in KNOWN_MARKER_VERSIONS:
        return "unreadable", None
    return "ok", data


def _no_uuid(uuid):
    return not isinstance(uuid, str) or not uuid.strip()


def mark_active(uuid, record, root=None):
    """Record that `record` (GPU.hold_headroom_record) is, or is about to be,
    in the driver for the card `uuid`, owned by this process. Returns (ok,
    message). No UUID means no marker: an unbound marker could be matched to
    the wrong card. Any existing marker this process does not own is left
    alone - a live window's, a crashed session's (still evidence), or an
    unreadable one."""
    if _no_uuid(uuid):
        return False, "no GPU UUID; the headroom raise cannot be tracked across a crash"
    if not _valid_record(record):
        return False, "headroom record has an unexpected shape; not tracked"
    state, existing = marker_state(uuid, root)
    if state == "unreadable":
        return False, "an unreadable headroom marker exists for this card; it was left in place"
    if state == "ok" and owner_state(existing.get("owner")) != "self":
        return False, ("another headroom marker (a running window's, or an earlier "
                       "session's) exists for this card; it was left in place")
    entry = {k: record[k] for k in ("rail", "hold_mv", "margin_mv", "ceiling_mv",
                                    "written_uv", "prior_uv") if k in record}
    entry.update(version=MARKER_VERSION, owner=current_owner())
    path = marker_path(uuid, root)
    if state == "ok":
        # our own marker: replaced in one step
        try:
            atomic_json(path, entry)
            return True, "tracked"
        except (OSError, ValueError) as exc:
            return False, f"could not record the headroom raise: {exc}"
    # A NEW marker is created exclusively: two windows that both found none a
    # moment ago must not both think they own it. os.rename fails on Windows
    # when the target exists, where os.replace would overwrite it.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.new")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(entry, indent=2, sort_keys=True, allow_nan=False),
                       encoding="utf-8")
        os.rename(tmp, path)
        return True, "tracked"
    except FileExistsError:
        result = (False, "another headroom marker appeared for this card at the same moment; "
                         "it was left in place")
    except (OSError, ValueError) as exc:
        result = (False, f"could not record the headroom raise: {exc}")
    try:
        tmp.unlink()
    except OSError:
        pass
    return result


def clear_active(uuid, root=None, force=False):
    """Drop the marker for `uuid`. Returns (ok, message).

    Without `force` only a marker THIS process owns is removed. `force` is for
    a caller that has already established the owner is gone (the startup check
    and its dialog); even then a running owner's marker, or an unreadable one,
    is kept."""
    if _no_uuid(uuid):
        return True, "nothing to clear"
    state, existing = marker_state(uuid, root)
    if state == "missing":
        return True, "nothing to clear"
    if state == "unreadable":
        return False, "the headroom marker for this card cannot be read; left in place"
    owner = owner_state(existing.get("owner"))
    if owner == "alive" or (owner != "self" and not force):
        return True, "marker belongs to another Druta window; left in place"
    try:
        marker_path(uuid, root).unlink()
        return True, "cleared"
    except FileNotFoundError:
        return True, "nothing to clear"
    except OSError as exc:
        return False, f"could not clear the headroom marker: {exc}"


def active_record(uuid, root=None):
    """The marker left for `uuid`, or None when there is none or it cannot be
    read (see marker_state to tell those apart)."""
    if _no_uuid(uuid):
        return None
    state, record = marker_state(uuid, root)
    return record if state == "ok" else None


def still_raised(record, limits):
    """Whether the card's current raw limits still carry what the marker says
    Druta wrote: True, False, or None when that cannot be told (the recorded
    rail or a recorded field is missing from this read). None must never be
    taken as False - a partial read is not evidence the raise is gone."""
    try:
        row = (limits or {})[record["rail"]]
    except (KeyError, TypeError):
        return None
    try:
        return all(int(round(row[k] * 1000)) == v for k, v in record["written_uv"].items())
    except (KeyError, TypeError, ValueError):
        return None


def other_instances():
    """How many OTHER processes run this same Druta executable, or None when
    that cannot be told (a source checkout runs as python.exe, shared with
    unrelated programs). Used only to warn before offering a PnP restart."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return None
    image = os.path.basename(sys.executable)
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, errors="replace", timeout=5,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    pids = set()
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) > 1 and parts[1].strip('"').isdigit():
            pids.add(int(parts[1].strip('"')))
    pids.discard(os.getpid())
    return len(pids)
