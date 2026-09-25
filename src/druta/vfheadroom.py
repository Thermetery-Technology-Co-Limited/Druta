# Druta - Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Persisted setting for V/F hold headroom (see GPU.apply_hold_headroom).

Default ON with a 25 mV margin. The margin is an estimate from one TU102
(TITAN RTX): holds on the alt-reliability/overvoltage ceiling lost 30-37 MHz,
and 25 mV of headroom removed the loss there. Other cards and generations have
not been measured, so the value is the user's to change and is labelled as an
estimate wherever it is shown.
"""
import json
import math

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
# what Druta itself wrote, and is never replayed: the only actions it offers
# are a PnP restart of the device or dismissal.

def active_path():
    return state_dir() / "vf-headroom-active.json"


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
    card `uuid`. Returns (ok, message). No UUID means no marker: an unbound
    marker could be matched to the wrong card."""
    if not isinstance(uuid, str) or not uuid.strip():
        return False, "no GPU UUID; the headroom raise cannot be tracked across a crash"
    if not _valid_record(record):
        return False, "headroom record has an unexpected shape; not tracked"
    path = path or active_path()
    data = _read_active(path)
    data[uuid] = {k: record[k] for k in ("rail", "hold_mv", "margin_mv", "written_uv", "prior_uv")
                  if k in record}
    try:
        atomic_json(path, data)
        return True, "tracked"
    except OSError as exc:
        return False, f"could not record the headroom raise: {exc}"


def clear_active(uuid, path=None):
    """Drop the marker for `uuid`. Returns (ok, message)."""
    path = path or active_path()
    data = _read_active(path)
    if uuid not in data:
        return True, "nothing to clear"
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
    reset, a reboot that did clear them, another tool - means the marker is
    stale and says nothing about this card any more."""
    try:
        row = (limits or {})[record["rail"]]
        return all(int(round(row[k] * 1000)) == v for k, v in record["written_uv"].items())
    except (KeyError, TypeError, ValueError):
        return False
