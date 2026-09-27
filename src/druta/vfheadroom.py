# Druta - Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Persisted setting for V/F hold headroom (see GPU.apply_hold_headroom).

Default ON with a 25 mV margin. The margin is an estimate from one TU102
(TITAN RTX): holds on the effective voltage ceiling (reliability plus its boost
contribution, alt-reliability, overvoltage) lost 19-37 MHz, and 25 mV of
headroom removed the loss there. Other cards and generations have not been
measured, so the value is the user's to change and is labelled as an estimate
wherever it is shown.
"""
import json
import math
import os

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
    except (OSError, ValueError, json.JSONDecodeError):
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
# Written while a headroom raise is in the driver, removed when it is restored.
# Rail limits outlive the process (a crash, a kill, a power cut), and the next
# session would read them as its first-read values. The marker is the evidence
# that lets that session say so. It is bound to the GPU's NVML UUID, holds only
# what Druta itself wrote, and is never replayed by this module.
#
# Each record names the process that owns it (PID + process creation time).
# Two Druta windows can drive the same card ("Open a second window on"), and a
# marker whose owner is still running is a LIVE raise, not a crash: it must not
# produce a dialog, and it must not be cleared or overwritten by another window.

MARKER_VERSION = 2
_STILL_ACTIVE = 259


def active_path():
    return state_dir() / "vf-headroom-active.json"


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

    A missing or malformed owner (a version-1 marker) is dead: it cannot be
    tied to a running window. A PID that now belongs to a different process
    (creation time differs) is dead too."""
    if not isinstance(owner, dict) or not isinstance(owner.get("pid"), int):
        return "dead"
    me = current_owner()
    if owner["pid"] == me["pid"] and owner.get("created") == me["created"]:
        return "self"
    created = process_created(owner["pid"])
    if created is None:
        return "dead"
    if created == "unknown" or owner.get("created") in (None, "unknown"):
        return "alive"
    return "alive" if created == owner.get("created") else "dead"


def _valid_record(record):
    if not isinstance(record, dict):
        return False
    written, prior = record.get("written_uv"), record.get("prior_uv")
    return (isinstance(record.get("rail"), int) and isinstance(written, dict)
            and isinstance(prior, dict) and written and set(written) == set(prior)
            and all(type(v) is int for v in list(written.values()) + list(prior.values())))


def _read_active(path):
    try:
        data = read_json(path, default={}) or {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def mark_active(uuid, record, path=None):
    """Record that `record` (GPU.hold_headroom_record) is in the driver for the
    card `uuid`, owned by this process. Returns (ok, message). No UUID means no
    marker: an unbound marker could be matched to the wrong card. A marker
    owned by another RUNNING Druta process is never overwritten."""
    if not isinstance(uuid, str) or not uuid.strip():
        return False, "no GPU UUID; the headroom raise cannot be tracked across a crash"
    if not _valid_record(record):
        return False, "headroom record has an unexpected shape; not tracked"
    path = path or active_path()
    data = _read_active(path)
    existing = data.get(uuid)
    if isinstance(existing, dict) and owner_state(existing.get("owner")) == "alive":
        return False, ("another Druta window is tracking a headroom raise on this "
                       "card; this window's raise is not tracked across a crash")
    entry = {k: record[k] for k in ("rail", "hold_mv", "margin_mv", "written_uv", "prior_uv")
             if k in record}
    entry.update(version=MARKER_VERSION, owner=current_owner())
    data[uuid] = entry
    try:
        atomic_json(path, data)
        return True, "tracked"
    except OSError as exc:
        return False, f"could not record the headroom raise: {exc}"


def clear_active(uuid, path=None, force=False):
    """Drop the marker for `uuid`. Returns (ok, message).

    Without `force` only a marker THIS process owns is removed. `force` is for
    a caller that has already established the owner is gone (the startup check
    and its dialog); even then a marker owned by a running process is kept."""
    path = path or active_path()
    data = _read_active(path)
    entry = data.get(uuid)
    if entry is None:
        return True, "nothing to clear"
    state = owner_state(entry.get("owner") if isinstance(entry, dict) else None)
    if state == "alive" or (state != "self" and not force):
        return True, "marker belongs to another Druta window; left in place"
    data.pop(uuid)
    try:
        atomic_json(path, data)
        return True, "cleared"
    except OSError as exc:
        return False, f"could not clear the headroom marker: {exc}"


def active_record(uuid, path=None):
    """The marker left for `uuid`, or None when there is none or it is malformed."""
    if not isinstance(uuid, str) or not uuid.strip():
        return None
    record = _read_active(path or active_path()).get(uuid)
    return record if _valid_record(record) else None


def still_raised(record, limits):
    """Whether the card's current raw limits still carry what the marker says
    Druta wrote. `limits` is GPU.read_volt_rail_limits(). Anything else - a
    reset, a restart that did clear them, another tool - means the marker is
    stale and says nothing about this card any more."""
    try:
        row = (limits or {})[record["rail"]]
        return all(int(round(row[k] * 1000)) == v for k, v in record["written_uv"].items())
    except (KeyError, TypeError, ValueError):
        return False
