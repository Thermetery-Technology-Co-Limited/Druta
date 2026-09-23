# Druta - Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""One positive, UUID-bound I2C route hint per selected GPU.

The file is a hint to narrow a fresh read-only probe. It never stores a live
controller, write verification, or output state, and never authorizes replay.
"""
import copy
import math
from pathlib import Path

from . import profiles, railctl, startup


VERSION = 1
FILENAME = "i2c-routes.json"
IDENTITY_KEYS = {"profile", "sha256", "port", "addr7", "rail"}


def _path(path):
    return Path(path) if path is not None else startup.state_dir() / FILENAME


def _uuid(gpu):
    static = getattr(gpu, "static", None)
    value = static.get("uuid") if isinstance(static, dict) else None
    return value if isinstance(value, str) and value.strip() else None


def _route(value):
    if not isinstance(value, dict) or set(value) != {"uuid", "controller", "identity"}:
        raise ValueError("invalid cached I2C route")
    uuid, controller, identity = (value[key] for key in ("uuid", "controller", "identity"))
    if (not isinstance(uuid, str) or not uuid.strip()
            or not isinstance(controller, str) or not controller.strip()
            or not isinstance(identity, dict) or set(identity) != IDENTITY_KEYS):
        raise ValueError("invalid cached I2C route identity")
    if (any(not isinstance(identity[key], str) or not identity[key]
            for key in ("profile", "rail"))
            or not isinstance(identity["sha256"], str)
            or len(identity["sha256"]) != 64
            or any(ch not in "0123456789abcdef" for ch in identity["sha256"])
            or type(identity["port"]) is not int or not 0 <= identity["port"] <= 255
            or type(identity["addr7"]) is not int or not 0 <= identity["addr7"] <= 127):
        raise ValueError("invalid cached I2C route identity")
    return value


def _document(value):
    if (not isinstance(value, dict) or set(value) != {"version", "routes"}
            or type(value["version"]) is not int or value["version"] != VERSION
            or not isinstance(value["routes"], dict)):
        raise ValueError("invalid I2C route cache")
    for key, route in value["routes"].items():
        if not isinstance(key, str) or _route(route)["uuid"] != key:
            raise ValueError("invalid I2C route cache key")
    return value


def load_route(gpu, *, path=None):
    """Return this GPU's last selected route, or None; corrupt files raise."""
    uuid = _uuid(gpu)
    if uuid is None:
        return None
    document = startup.read_json(_path(path), default=None)
    if document is None:
        return None
    return copy.deepcopy(_document(document)["routes"].get(uuid))


def remember(gpu, rail, *, path=None):
    """Persist only the selected route and recipe fingerprint for this UUID."""
    uuid = _uuid(gpu)
    if uuid is None:
        return False
    profile = getattr(rail, "p", None)
    controller = getattr(profile, "regulator", None)
    route = _route({"uuid": uuid, "controller": controller,
                    "identity": profiles.rail_identity(rail)})
    target = _path(path)
    existing = startup.read_json(target, default=None)
    document = ({"version": VERSION, "routes": {}} if existing is None
                else _document(existing))
    document["routes"][uuid] = route
    startup.atomic_json(target, document)
    return True


def _finite_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _readable(candidate):
    """Check live control state; read-only recipes need any known telemetry."""
    capture = getattr(candidate, "capture_control", None)
    if callable(capture):
        control = capture()
        return isinstance(control, dict) and bool(control)
    telemetry = getattr(candidate, "telemetry", None)
    values = telemetry() if callable(telemetry) else None
    if not isinstance(values, dict):
        return False
    if not getattr(candidate.p, "read_only", True):
        return _finite_number(values.get("offset_mv"))
    return any(_finite_number(value) for key, value in values.items()
               if key not in ("offset_mv", "offset_raw"))


def reconnect(gpu, route, *, controller=None, progress=None, cancelled=None):
    """Probe one route on the current GPU and return only fresh matching hits.

    A failed/stale hint returns [] so the caller can do its normal scoped scan.
    It is deliberately not deleted: a transient I2C read is not negative cache.
    """
    try:
        saved = _route(route)
        uuid = _uuid(gpu)
        if (uuid is None or saved["uuid"] != uuid
                or (controller is not None and controller != saved["controller"])
                or (cancelled is not None and cancelled())):
            return []
        nvapi = getattr(gpu, "nvapi", None)
        if nvapi is None:
            return []
        architecture = gpu.arch()
        identity = saved["identity"]
        candidates = railctl.discover(
            nvapi, architecture=architecture, controller=saved["controller"],
            routes=[(identity["port"], identity["addr7"])],
            progress=progress, cancelled=cancelled)
        matches = []
        for candidate in candidates or []:
            if cancelled is not None and cancelled():
                return []
            try:
                if (candidate.p.regulator != saved["controller"]
                        or profiles.rail_identity(candidate) != identity
                        or not candidate.present()
                        or not _readable(candidate)):
                    continue
            except Exception:
                continue
            matches.append(candidate)
        return [] if cancelled is not None and cancelled() else matches
    except Exception:
        # A stale hint or transient probe/read error belongs to the normal
        # scoped-scan fallback, not to a negative cache or a startup failure.
        return []
