# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit, scoped iCX presets. Loading and describing data never access hardware.

A preset stores controller requests, not electrical measurements or factory
defaults. An application may retain a captured connection preset for reset,
but applying it must use the same preview and deliberate apply path.
"""
import json
import time
from contextlib import nullcontext
from pathlib import Path

from .controllers.icx import (
    fbvdd_payload,
    loadline_payload,
    nvvdd_payload,
    ocp_payload,
    switching_frequency_payload,
)

SCHEMA = "druta.icx.preset.v1"
MAX_JSON_BYTES = 64 * 1024
DEFAULT_ORIGIN = "Druta controller requested-state readback"
SETTINGS_KEYS = (
    "nvvdd_mode", "nvvdd_request_mv", "fbvdd_request_mv",
    "nvvdd_frequency_khz", "fbvdd_frequency_khz",
    "nvvdd_ocp_enabled", "fbvdd_ocp_enabled", "loadline_level",
)
_REQUIRED_FLAGS = (
    "firmware_ok", "state_available", "frequency_state_available",
    "ocp_state_available", "loadline_state_available",
)
READBACK_DELAYS = (0.0, 0.10, 0.20)


def _settings(settings, *, xoc):
    if type(xoc) is not bool:
        raise ValueError("XOC selection must be a Boolean")
    if not isinstance(settings, dict) or set(settings) != set(SETTINGS_KEYS):
        raise ValueError("The iCX preset must contain exactly the eight supported setting fields")
    result = {key: settings[key] for key in SETTINGS_KEYS}
    if type(result["nvvdd_mode"]) is not str or result["nvvdd_mode"] not in ("auto", "manual"):
        raise ValueError("NVVDD mode must be auto or manual")
    if result["nvvdd_mode"] == "auto":
        if result["nvvdd_request_mv"] is not None:
            raise ValueError("NVVDD Auto must have a null manual request")
    else:
        if type(result["nvvdd_request_mv"]) is not int:
            raise ValueError("Manual NVVDD must be an integer number of millivolts")
        nvvdd_payload(result["nvvdd_request_mv"], xoc=xoc)
    for key in ("fbvdd_request_mv", "nvvdd_frequency_khz", "fbvdd_frequency_khz", "loadline_level"):
        if type(result[key]) is not int:
            raise ValueError(f"{key} must be an integer")
    fbvdd_payload(result["fbvdd_request_mv"])
    switching_frequency_payload(result["nvvdd_frequency_khz"])
    switching_frequency_payload(result["fbvdd_frequency_khz"])
    ocp_payload(result["nvvdd_ocp_enabled"])
    ocp_payload(result["fbvdd_ocp_enabled"])
    loadline_payload(result["loadline_level"])
    return result


def validate_preset(doc, *, xoc=True):
    """Return copied settings after complete validation, without hardware access."""
    if not isinstance(doc, dict) or set(doc) != {"schema", "settings", "origin"}:
        raise ValueError("The iCX preset must contain exactly schema, settings and origin")
    if type(doc["schema"]) is not str or doc["schema"] != SCHEMA:
        raise ValueError("Unsupported iCX preset schema")
    origin = doc["origin"]
    if type(origin) is not str or not origin.strip() or len(origin) > 512:
        raise ValueError("Preset origin must be a nonempty diagnostic string of at most 512 characters")
    return _settings(doc["settings"], xoc=xoc)


def _from_telemetry(tel):
    if not isinstance(tel, dict):
        raise ValueError("Current iCX controller readback is unavailable")
    missing = [flag for flag in _REQUIRED_FLAGS if tel.get(flag) is not True]
    if missing:
        raise ValueError("Complete current iCX readback is required; unavailable: " + ", ".join(missing))
    return _settings({key: tel.get(key) for key in SETTINGS_KEYS}, xoc=True)


def make_preset(tel, *, origin=DEFAULT_ORIGIN):
    """Freeze fully decoded current readback; incomplete data cannot become a preset."""
    doc = {"schema": SCHEMA, "settings": _from_telemetry(tel), "origin": origin}
    validate_preset(doc)
    return doc


def describe_settings(doc_or_settings):
    """A plain-text preview. This never applies a preset or reads a controller."""
    if isinstance(doc_or_settings, dict) and "schema" in doc_or_settings:
        values = validate_preset(doc_or_settings)
    else:
        values = _settings(doc_or_settings, xoc=True)
    nv = ("Auto" if values["nvvdd_mode"] == "auto"
          else f"manual {values['nvvdd_request_mv']} mV")
    ocp_nv = "enabled" if values["nvvdd_ocp_enabled"] else "disabled"
    ocp_fb = "enabled" if values["fbvdd_ocp_enabled"] else "disabled"
    return (
        f"NVVDD {nv}; FBVDD {values['fbvdd_request_mv']} mV; "
        f"switching frequency NVVDD {values['nvvdd_frequency_khz']} kHz / "
        f"FBVDD {values['fbvdd_frequency_khz']} kHz; "
        f"OCP NVVDD {ocp_nv} / FBVDD {ocp_fb}; loadline Level {values['loadline_level']}."
    )


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f"Non-finite JSON number is not allowed: {value}")


def read_preset(path):
    """Read and validate a bounded JSON file; loading does not apply any setting."""
    with Path(path).open("rb") as source:
        data = source.read(MAX_JSON_BYTES + 1)
    if len(data) > MAX_JSON_BYTES:
        raise ValueError("The iCX preset exceeds the 64 KiB file limit")
    try:
        doc = json.loads(data.decode("utf-8-sig"), object_pairs_hook=_unique_object,
                         parse_constant=_reject_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(f"Invalid iCX preset JSON: {exc}") from exc
    validate_preset(doc)
    return doc


def write_preset(path, doc):
    """Save an already captured, valid preset. This never reads or writes a GPU."""
    settings = validate_preset(doc)
    normalized = {"schema": SCHEMA, "settings": settings, "origin": doc["origin"]}
    text = json.dumps(normalized, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    if len(text.encode("utf-8")) > MAX_JSON_BYTES:
        raise ValueError("The iCX preset exceeds the 64 KiB file limit")
    Path(path).write_text(text, encoding="utf-8", newline="\n")


def _read_complete(rail, expected=None):
    last_error = "current readback is unavailable"
    for delay in READBACK_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            current = _from_telemetry(rail.telemetry())
        except Exception as exc:
            last_error = str(exc)
            continue
        if expected is None or current == expected:
            return current
        changed = [key for key in SETTINGS_KEYS if current[key] != expected[key]]
        last_error = "readback differs for " + ", ".join(changed)
    raise ValueError(last_error)


def _changes(current, requested, *, xoc):
    """Ordered operations for changed fields only; no register-level commands."""
    operations = []

    def add(label, method, *args, **kwargs):
        operations.append((label, method, args, {"acknowledged": True, **kwargs}))

    if requested["nvvdd_mode"] == "auto" and current["nvvdd_mode"] != "auto":
        add("NVVDD Auto", "set_nvvdd_auto")
    for rail, key in (("NVVDD", "nvvdd_ocp_enabled"), ("FBVDD", "fbvdd_ocp_enabled")):
        if requested[key] and not current[key]:
            add(f"{rail} OCP enable", "set_ocp_enabled", rail, True)
    if requested["fbvdd_request_mv"] != current["fbvdd_request_mv"]:
        add("FBVDD voltage", "set_fbvdd_mv", requested["fbvdd_request_mv"])
    for rail, key in (("NVVDD", "nvvdd_frequency_khz"), ("FBVDD", "fbvdd_frequency_khz")):
        if requested[key] != current[key]:
            add(f"{rail} switching frequency", "set_switching_frequency_khz", rail, requested[key])
    if requested["loadline_level"] != current["loadline_level"]:
        add("loadline", "set_loadline", requested["loadline_level"])
    if requested["nvvdd_mode"] == "manual" and (
            current["nvvdd_mode"] != "manual"
            or requested["nvvdd_request_mv"] != current["nvvdd_request_mv"]):
        add("manual NVVDD", "set_nvvdd_mv", requested["nvvdd_request_mv"], xoc=xoc)
    for rail, key in (("NVVDD", "nvvdd_ocp_enabled"), ("FBVDD", "fbvdd_ocp_enabled")):
        if not requested[key] and current[key]:
            add(f"{rail} OCP disable", "set_ocp_enabled", rail, False)
    return operations


def apply_preset(rail, doc, *, acknowledged=False, xoc=False):
    """Deliberately apply changed settings, stop on failure, then verify all fields.

    Callers perform their normal UI unlock, identity and clock-floor checks.
    Each underlying controller setter retains its own binding, identity, B2,
    packet and readback checks. Loading a file never calls this function.
    """
    if acknowledged is not True:
        return False, "Applying an iCX preset requires acknowledgment; nothing was written"
    try:
        requested = validate_preset(doc, xoc=xoc)
    except ValueError as exc:
        return False, f"Invalid iCX preset: {exc}; nothing was written"
    if not callable(getattr(rail, "telemetry", None)):
        return False, "Current iCX controller readback is unavailable; nothing was written"
    verified = []
    # Real EVGAICx instances use an RLock, also acquired by each setter.
    lock = getattr(rail, "_mutex", None)
    with lock if lock is not None else nullcontext():
        try:
            current = _read_complete(rail)
        except ValueError as exc:
            return False, f"Cannot apply iCX preset without complete current readback: {exc}; nothing was written"
        operations = _changes(current, requested, xoc=xoc)
        for label, method, _args, _kwargs in operations:
            if not callable(getattr(rail, method, None)):
                return False, f"The controller cannot apply {label}; nothing was written"
        for label, method, args, kwargs in operations:
            try:
                result = getattr(rail, method)(*args, **kwargs)
                if not isinstance(result, tuple) or len(result) != 2:
                    raise ValueError("The setter did not return a verification result")
                ok, message = result
            except Exception as exc:
                ok, message = False, str(exc)
            if ok is not True:
                return False, (
                    f"iCX preset stopped after {len(verified)} verified change(s): "
                    f"{label} failed or was not verified: {message}. "
                    "Earlier changes remain applied; the failed operation may also have applied. "
                    "No automatic retry or rollback was performed."
                )
            verified.append(label)
        try:
            _read_complete(rail, expected=requested)
        except ValueError as exc:
            return False, (
                f"iCX preset has {len(verified)} verified change(s), but complete final readback "
                f"was not verified: {exc}. No automatic retry or rollback was performed."
            )
    if not verified:
        return True, "The iCX preset already matches all controller settings; no setting writes were needed"
    return True, (
        f"iCX preset applied: {len(verified)} changed setting(s), with complete controller "
        "readback verified. These requests are not physical measurements."
    )
