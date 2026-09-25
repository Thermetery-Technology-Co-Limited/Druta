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
