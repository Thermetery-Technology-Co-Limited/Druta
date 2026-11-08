# Druta - a monitor and tuner for NVIDIA GPUs.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The pure current-limit tables of the native core (drutacore/power_layouts.h).

``generation_policies(arch)`` returns what ``nvbackend.GPU._current_limit_generation_policies``
returns for that architecture, and ``transport_layouts(command, packet_bytes, parameter_bytes)``
returns the ``(layout, *fallbacks)`` tuple ``_current_limit_transport_layouts`` builds from that
key, or ``()`` for an unknown key (where the Python raises CurrentLimitUnsupported instead). The
dicts are shaped exactly like the Python ones, so the two compare equal with ``==``.
"""
from __future__ import annotations

import ctypes
import threading

from . import native
from .native import DC_OK, NativeUnavailable

DC_POWER_GET_COUNT = 4
DC_POWER_LAYOUTS_MAX = 4
DC_GENERATION_POLICIES_MAX = 8
POWER_GET_COMMANDS = (0x2080A618, 0x2080A619, 0x2080A61A, 0x2080E61B)


class _PowerLayout(ctypes.Structure):  # dc_power_layout
    _fields_ = [("size", ctypes.c_uint32), ("sizes", ctypes.c_uint32 * DC_POWER_GET_COUNT),
                ("has_commands", ctypes.c_int32), ("commands", ctypes.c_uint32 * DC_POWER_GET_COUNT),
                ("info_base", ctypes.c_uint32), ("info_stride", ctypes.c_uint32),
                ("status_base", ctypes.c_uint32), ("status_stride", ctypes.c_uint32),
                ("control_base", ctypes.c_uint32), ("control_stride", ctypes.c_uint32),
                ("has_status_mask_word", ctypes.c_int32), ("status_mask_word", ctypes.c_uint32),
                ("has_control_mask_word", ctypes.c_int32), ("control_mask_word", ctypes.c_uint32)]


class _GenerationPolicy(ctypes.Structure):  # dc_generation_policy
    _fields_ = [("policy", ctypes.c_uint32), ("label", ctypes.c_char * 32), ("type", ctypes.c_uint32),
                ("channel", ctypes.c_uint32), ("normal_maximum_ma", ctypes.c_uint32)]


_P = ctypes.POINTER
_SIGNATURES = {
    "dc_power_layouts_lookup": (ctypes.c_int32, [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                                                 _P(_PowerLayout), ctypes.c_uint32, _P(ctypes.c_uint32)]),
    "dc_generation_policies": (ctypes.c_int32, [ctypes.c_int32, ctypes.c_uint32, _P(_GenerationPolicy),
                                                ctypes.c_uint32, _P(ctypes.c_uint32)]),
}

_lock = threading.Lock()
_bound = None


def load():
    """The core library with the table lookups bound; raises NativeUnavailable."""
    global _bound
    library = native.load()
    with _lock:
        if _bound is library:
            return library
        try:
            for name, (restype, argtypes) in _SIGNATURES.items():
                function = getattr(library, name)
                function.restype = restype
                function.argtypes = argtypes
        except AttributeError as exc:
            raise NativeUnavailable(f"native core has no current-limit tables: {exc}") from None
        _bound = library
        return library


def available():
    try:
        load()
    except NativeUnavailable:
        return False
    return True


def _u32(value):
    """A key component the C ABI can carry; anything else cannot be in the table."""
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 0xFFFFFFFF


def _layout_dict(row):
    layout = {"sizes": dict(zip(POWER_GET_COMMANDS, row.sizes))}
    if row.has_commands:
        layout["commands"] = dict(zip(POWER_GET_COMMANDS, row.commands))
    layout["info"] = (row.info_base, row.info_stride)
    layout["status"] = (row.status_base, row.status_stride)
    layout["control"] = (row.control_base, row.control_stride)
    if row.has_status_mask_word:
        layout["status_mask_word"] = row.status_mask_word
    if row.has_control_mask_word:
        layout["control_mask_word"] = row.control_mask_word
    return layout


def transport_layouts(command, packet_bytes, parameter_bytes):
    """(layout, *fallbacks) for the power-GET key, or () for an unknown key."""
    library = load()
    if not all(_u32(v) for v in (command, packet_bytes, parameter_bytes)):
        return ()
    rows = (_PowerLayout * DC_POWER_LAYOUTS_MAX)()
    for row in rows:
        row.size = ctypes.sizeof(_PowerLayout)
    count = ctypes.c_uint32()
    status = library.dc_power_layouts_lookup(command, packet_bytes, parameter_bytes, rows,
                                             DC_POWER_LAYOUTS_MAX, ctypes.byref(count))
    if status != DC_OK or count.value > DC_POWER_LAYOUTS_MAX:
        raise RuntimeError(f"dc_power_layouts_lookup: status {status}, count {count.value}")
    return tuple(_layout_dict(rows[i]) for i in range(count.value))


def generation_policies(arch):
    """CURRENT_LIMIT_GENERATION_POLICIES.get(arch, {}) as a fresh dict."""
    library = load()
    if arch is None:
        has_arch, value = 0, 0
    elif _u32(arch):
        has_arch, value = 1, arch
    else:
        return {}
    rows = (_GenerationPolicy * DC_GENERATION_POLICIES_MAX)()
    count = ctypes.c_uint32()
    status = library.dc_generation_policies(has_arch, value, rows, DC_GENERATION_POLICIES_MAX,
                                            ctypes.byref(count))
    if status != DC_OK or count.value > DC_GENERATION_POLICIES_MAX:
        raise RuntimeError(f"dc_generation_policies: status {status}, count {count.value}")
    return {row.policy: {"label": row.label.decode("utf-8"), "type": row.type, "channel": row.channel,
                         "normal_maximum_ma": row.normal_maximum_ma}
            for row in rows[:count.value]}
