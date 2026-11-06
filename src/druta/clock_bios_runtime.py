# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Typed BIOS clock-range readback, with exact user-request preservation.

Only GET calls occur here. INFO ordinal binding follows the traced v50 BIOS
builder. The understood NVAPI CONTROL wrapper preserves that native ordinal;
type16 becomes public type15 and its signed kHz request is at row+0x114.
"""

from __future__ import annotations

import copy
import ctypes
import math
import re
import struct
from collections.abc import Mapping

from .perf_diagnostics import PROBE_ARCHITECTURES, PerfDiagnosticError, PerfDiagnosticsReader
from .registry_flags import _same_state

COMMAND, EXPORT = 0x20809019, 0x57B5A5DF
SIZE, HEADER, STRIDE, CAPACITY = 0x3030, 0x30, 0x180, 32
_CONTROL_VERSION, _CONTROL_SIZE = 0x261A4, 0x61A4
_CONTROL_LAYOUT = {
    "version": _CONTROL_VERSION,
    "parameter_bytes": _CONTROL_SIZE,
    "header_bytes": 0x124,
    "row_bytes": 0x304,
    "capacity": 32,
}
_GUARD, _SENTINEL = 64, 0xA5
_ENVELOPE = {0: 0x4E564441, 1: 0x10002, 3: 0x4E562A2A, 4: 0x0500002B}
_LAYOUT = {
    "command": "0x20809019",
    "parameter_bytes": SIZE,
    "header_bytes": HEADER,
    "row_bytes": STRIDE,
    "capacity": CAPACITY,
}
_KEY = re.compile(r"clock\.(0|[1-9]\d*)\.(minimum|maximum)\Z")
_OFFSETS = {"minimum": 0x26, "maximum": 0x28}
# NVIDIA primary API-domain constants, not ordinal/device assumptions.
_DOMAINS = {
    "GPC": 1,
    "XBAR": 2,
    "SYS": 4,
    "HUB": 8,
    "DRAM": 16,
    "HOST": 32,
    "DISP": 64,
    "PWR": 0x80000,
    "VID": 0x100000,
    "PCIEGEN": 0x200000,
}
_BINDING_KEYS = {
    "schema",
    "table_version",
    "bios_row_type",
    "bios_index",
    "bios_hal_selector",
    "table_offset",
    "header_size",
    "entry_size",
    "entry_count",
    "subentry_count",
    "subentry_size",
}


class ClockBiosError(RuntimeError):
    pass


def _integer(value, low, high, description):
    if type(value) is not int or not low <= value <= high:
        raise ClockBiosError(description)
    return value


def _mhz(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not float(value).is_integer():
        raise ClockBiosError("Clock range requires a whole MHz value")
    return _integer(int(value), -32768, 32767, "Clock range exceeds signed 16-bit MHz")


def _identity(value):
    from .nvbackend import parse_slot

    if not isinstance(value, dict) or set(value) != {"slot", "architecture", "device_id", "subsystem_id"}:
        raise ClockBiosError("Clock range needs an exact paired adapter identity")
    address = parse_slot(value["slot"]) if isinstance(value["slot"], str) else None
    if address is None or address[0] != 0 or address[1] > 255 or address[2] > 31 or address[3] > 7:
        raise ClockBiosError("Clock range has an invalid PCI identity")
    for key, maximum in (("architecture", 0xFFFFFFFF), ("device_id", 0xFFFF), ("subsystem_id", 0xFFFFFFFF)):
        _integer(value[key], 0, maximum, "Clock range has an invalid adapter identity")
    if value["architecture"] not in PROBE_ARCHITECTURES:
        raise ClockBiosError("Clock INFO needs a supported modern architecture")
    return dict(value)


def _hex(value, length):
    if not isinstance(value, str) or len(value) != length * 2:
        raise ClockBiosError("Clock readback has invalid packet geometry")
    try:
        raw = bytes.fromhex(value)
    except ValueError as exc:
        raise ClockBiosError("Clock readback is not hexadecimal") from exc
    if len(raw) != length or raw.hex() != value:
        raise ClockBiosError("Clock readback needs canonical bounded hexadecimal")
    return raw


def _decode(raw):
    if len(raw) != SIZE:
        raise ClockBiosError("Clock INFO geometry is not understood")
    mask = struct.unpack_from("<I", raw, 4)[0]
    rows = {}
    for index in range(CAPACITY):
        if not mask & (1 << index):
            continue
        pos = HEADER + index * STRIDE
        row_type, base_type = raw[pos : pos + 2]
        domain = struct.unpack_from("<I", raw, pos + 4)[0]
        typed = row_type == base_type == 16
        lo, hi = struct.unpack_from("<hh", raw, pos + 0x26) if typed else (None, None)
        rows[str(index)] = {
            "type": row_type,
            "base_type": base_type,
            "api_domain_mask": domain,
            "minimum_mhz": lo,
            "maximum_mhz": hi,
        }
    return rows


def _control_shape(value, identity):
    if not isinstance(value, dict) or set(value) != {*_CONTROL_LAYOUT, "mask", "payload_hex"}:
        raise ClockBiosError("Complete user-clock requests are unavailable")
    if any(type(value[k]) is not type(v) or value[k] != v for k, v in _CONTROL_LAYOUT.items()):
        raise ClockBiosError("User-clock request layout is not understood")
    mask = _integer(value["mask"], 1, 0xFFFFFFFF, "User-clock request inventory is invalid")
    raw = _hex(value["payload_hex"], _CONTROL_SIZE)
    if struct.unpack_from("<I", raw, 0)[0] != _CONTROL_VERSION or struct.unpack_from("<I", raw, 8)[0] != mask:
        raise ClockBiosError("User-clock requests changed version or inventory")
    requests = {}
    for index in range(32):
        if not mask & (1 << index):
            continue
        pos = 0x124 + index * 0x304
        # NVAPI20DF75..20E0FA preserves native ordinal. Type16 is mapped
        # by15DC30 to public15;20E076 rejects non-kHz native mode then
        # copies native CONTROL row+C to public row+114 without scaling.
        if struct.unpack_from("<I", raw, pos)[0] == 15:
            requests[index] = struct.unpack_from("<i", raw, pos + 0x114)[0]
    return copy.deepcopy(value), requests


def _canonical(value, *, require_control=True):
    if not isinstance(value, dict) or set(value) != {"identity", "info_hex", "user_control"}:
        raise ClockBiosError("Malformed saved clock observation")
    identity = _identity(value["identity"])
    _decode(_hex(value["info_hex"], SIZE))
    if value["user_control"] is None and not require_control:
        return {"identity": identity, "info_hex": value["info_hex"], "user_control": None}
    control, _ = _control_shape(value["user_control"], identity)
    return {"identity": identity, "info_hex": value["info_hex"], "user_control": control}


def _shape(observation, *, writable=False):
    if not isinstance(observation, Mapping):
        raise ClockBiosError("Clock INFO observation is unavailable")
    if any(type(observation.get(k)) is not type(v) or observation[k] != v for k, v in _LAYOUT.items()):
        raise ClockBiosError("Clock INFO layout is not understood")
    if type(observation.get("stable_reads")) is not int or observation["stable_reads"] != 2:
        raise ClockBiosError("Two stable fresh clock INFO reads are required")
    state = _canonical(
        {key: observation.get(key) for key in ("identity", "info_hex", "user_control")}, require_control=writable
    )
    if not _same_state(observation.get("rows"), _decode(bytes.fromhex(state["info_hex"]))):
        raise ClockBiosError("Decoded clock ranges disagree with their raw packet")
    return state


def _binding(reader):
    api, slot, architecture = reader._binding()
    return _identity(
        {
            "slot": slot,
            "architecture": architecture,
            "device_id": api.selected.get("devid"),
            "subsystem_id": api.selected.get("subsys"),
        }
    )


def _guarded(size):
    storage = (ctypes.c_ubyte * (size + 2 * _GUARD))()
    ctypes.memset(ctypes.addressof(storage), _SENTINEL, len(storage))
    data = (ctypes.c_ubyte * size).from_buffer(storage, _GUARD)
    ctypes.memset(ctypes.addressof(data), 0, size)
    return storage, data


def _guards(storage):
    if any(v != _SENTINEL for v in storage[:_GUARD]) or any(v != _SENTINEL for v in storage[-_GUARD:]):
        raise PerfDiagnosticError("malformed", "Clock GET exceeded the guarded allocation")


def _info_once(gpu, reader):
    identity = _binding(reader)
    if gpu.nvapi._i(EXPORT, ctypes.c_void_p, ctypes.c_void_p) is None:
        raise PerfDiagnosticError("unsupported", "Clock INFO GET export is unavailable")
    header, fields = reader._transport()
    if any(header[i] != value for i, value in _ENVELOPE.items()) or any(
        type(v) is not int or not 0 <= v <= 0xFFFFFFFF for v in fields.values()
    ):
        raise PerfDiagnosticError("malformed", "Clock INFO needs the understood paired envelope")
    storage, buffer = _guarded(68 + SIZE)
    packet = (ctypes.c_uint32 * ((68 + SIZE) // 4)).from_buffer(buffer)
    for i, word in enumerate(header):
        packet[i] = word
    packet[2], packet[14], packet[15], packet[16] = 68 + SIZE, COMMAND, SIZE, 0
    packet[5] = packet[11] = 0
    expected = tuple(packet[:16])
    status = gpu._legacy_clk_escape(packet, dict(fields))
    if _binding(reader) != identity:
        raise PerfDiagnosticError("identity", "Clock INFO target changed during GET")
    _guards(storage)
    if type(status) is not int or status != 0:
        raise PerfDiagnosticError("transient", f"Clock INFO transport failed: {status}")
    if any(packet[i] != v for i, v in enumerate(expected) if i not in (5, 11)) or packet[5] not in (0, 1):
        raise PerfDiagnosticError("malformed", "Clock INFO changed its envelope or geometry")
    if packet[11] != 1:
        raise PerfDiagnosticError("transient", "Clock INFO completion is unavailable")
    if packet[16]:
        raise PerfDiagnosticError(
            "unsupported" if packet[16] in (0x1F, 0x56) else "transient", f"Clock INFO RM status {packet[16]}"
        )
    return identity, bytes(buffer)[68:]


def _control_once(gpu, reader, identity):
    getter = getattr(gpu.nvapi, "ClkDomCtlGet", None)
    if getter is None:
        raise ClockBiosError("User-clock CONTROL GET is unavailable")
    domains = gpu.clkdom_domains()
    if not isinstance(domains, (list, tuple)) or not domains or len(set(domains)) != len(domains):
        raise ClockBiosError("User-clock request layout or inventory is unavailable")
    if any(type(d) is not int or not 0 <= d < 32 for d in domains):
        raise ClockBiosError("User-clock inventory is invalid")
    mask = sum(1 << d for d in domains)
    storage, packet = _guarded(_CONTROL_SIZE)
    struct.pack_into("<I", packet, 0, _CONTROL_VERSION)
    struct.pack_into("<I", packet, 8, mask)
    status = getter(gpu.nvapi.gpu, ctypes.byref(packet))
    _guards(storage)
    if _binding(reader) != identity:
        raise PerfDiagnosticError("identity", "User-clock request target changed")
    if type(status) is not int or status != 0:
        raise ClockBiosError(f"User-clock CONTROL GET failed: {status}")
    value = {**_CONTROL_LAYOUT, "mask": mask, "payload_hex": bytes(packet).hex()}
    return _control_shape(value, identity)[0]


def read(gpu):
    """Read stable INFO and requests independently, with bounded fresh retries."""
    reader, last, partial = PerfDiagnosticsReader(gpu), None, None
    for attempt in range(2):
        try:
            with gpu._lock:
                samples = []
                for _ in range(2):
                    identity, info = _info_once(gpu, reader)
                    try:
                        control, error = _control_once(gpu, reader, identity), None
                    except PerfDiagnosticError:
                        raise
                    except Exception as exc:
                        control, error = None, str(exc)
                    samples.append(({"identity": identity, "info_hex": info.hex(), "user_control": control}, error))
                first, second = samples[0][0], samples[1][0]
                if first["identity"] != second["identity"] or first["info_hex"] != second["info_hex"]:
                    raise PerfDiagnosticError("transient", "Clock INFO changed between fresh reads")
                control_stable = first["user_control"] is not None and _same_state(
                    first["user_control"], second["user_control"]
                )
                if not control_stable:
                    second["user_control"] = None
                observation = {
                    **_LAYOUT,
                    **second,
                    "rows": _decode(bytes.fromhex(second["info_hex"])),
                    "stable_reads": 2,
                    "user_control_error": None
                    if control_stable
                    else samples[1][1] or "User-clock requests were not stable",
                }
                _shape(observation)
                if control_stable:
                    return observation, None
                partial = observation
        except PerfDiagnosticError as exc:
            last = str(exc)
            if exc.kind != "transient":
                return None, last
        except Exception as exc:
            last = str(exc)
        if attempt == 0:
            gpu._current_limit_transport = None
            gpu._current_limit_layout_choice = None
    return (partial, None) if partial is not None else (None, last or "Clock INFO is unavailable")


def _bios_binding(value, index):
    if not isinstance(value, dict) or set(value) != _BINDING_KEYS:
        raise ClockBiosError("Clock field lacks the exact BIOS builder binding")
    for key in value:
        _integer(value[key], 0, 16 * 1024 * 1024, "Clock BIOS binding contains an invalid integer")
    if (
        value["schema"] != 1
        or value["table_version"] != 0x50
        or value["bios_row_type"] > 255
        or value["bios_row_type"] & 31 != 2
    ):
        raise ClockBiosError("Clock field is not a traced v50 programmable entry")
    if not 0 <= index < value["entry_count"] <= 32 or value["bios_index"] != index:
        raise ClockBiosError("Clock BIOS ordinal is invalid")
    if (
        not 13 <= value["header_size"] <= 255
        or not 15 <= value["entry_size"] <= 255
        or not 3 <= value["subentry_count"] <= 255
        or not 0 <= value["subentry_size"] <= 7
        or value["bios_hal_selector"] > 255
    ):
        raise ClockBiosError("Clock BIOS geometry is not understood")
    end = (
        value["table_offset"]
        + value["header_size"]
        + value["entry_count"] * (value["entry_size"] + value["subentry_count"] * value["subentry_size"])
    )
    if end > 16 * 1024 * 1024:
        raise ClockBiosError("Clock BIOS table exceeds the image parser bound")
    return dict(value)


def _selected_row(state, index, domain_label, binding):
    raw = _hex(state["info_hex"], SIZE)
    rows = _decode(raw)
    row = rows.get(str(index))
    if raw[8] != binding["table_version"] or row is None or row["type"] != 16 or row["base_type"] != 16:
        raise ClockBiosError("Selected runtime row does not match the v50 programmable builder")
    mask = row["api_domain_mask"]
    if mask == 0 or mask & (mask - 1) or sum(r["api_domain_mask"] == mask for r in rows.values()) != 1:
        raise ClockBiosError("Selected runtime API domain is not unique and one-bit")
    if not isinstance(domain_label, str) or (
        domain_label not in _DOMAINS and re.fullmatch(r"domain\d+", domain_label) is None
    ):
        raise ClockBiosError("Clock field has an unrecognized domain label")
    if domain_label in _DOMAINS and _DOMAINS[domain_label] != mask:
        raise ClockBiosError("Clock field name disagrees with the primary API domain identifier")
    if row["minimum_mhz"] > row["maximum_mhz"]:
        raise ClockBiosError("Clock runtime bounds are inverted")
    return row


def contracts(gpu, inspection, changes):
    """Bind all selected v50 ranges; legacy public GPC/DRAM remains separate."""
    if not isinstance(changes, Mapping):
        raise ClockBiosError("Clock changes must be a mapping")
    chosen = {key: value for key, value in changes.items() if isinstance(key, str) and _KEY.fullmatch(key)}
    if not chosen:
        return {}
    fields = {f["key"]: f for f in (inspection or {}).get("fields", [])}
    selected = {}
    for key, value in chosen.items():
        field = fields.get(key)
        if field is None:
            raise ClockBiosError("Selected clock field is missing from BIOS inspection")
        if field.get("clock_table_version") == 0x40 and field.get("domain") in ("GPC", "DRAM"):
            continue
        selected[key] = value
    if not selected:
        return {}
    if any(key.startswith("factory.") for key in changes):
        raise ClockBiosError("Apply BIOS ranges and factory clock deltas in separate transactions")
    observation, error = read(gpu)
    if error:
        raise ClockBiosError(error)
    before = _shape(observation, writable=True)
    entries = {}
    for key, wanted in selected.items():
        match = _KEY.fullmatch(key)
        index, role = int(match[1]), match[2]
        field = fields[key]
        binding = _bios_binding(field.get("clock_binding"), index)
        row = _selected_row(before, index, field.get("domain"), binding)
        start = (
            binding["table_offset"]
            + binding["header_size"]
            + index * (binding["entry_size"] + binding["subentry_count"] * binding["subentry_size"])
        )
        for peer_role, offset in (("minimum", 3), ("maximum", 5)):
            peer = fields.get(f"clock.{index}.{peer_role}")
            if (
                peer is None
                or not _same_state(peer.get("clock_binding"), binding)
                or peer.get("domain") != field.get("domain")
                or peer.get("units") != "MHz"
                or peer.get("encoding") != "h"
                or peer.get("width") != 2
                or peer.get("scale") != 1
                or peer.get("mask") is not None
                or peer.get("offsets") != [start + offset]
                or _mhz(peer.get("current")) != row[peer_role + "_mhz"]
            ):
                raise ClockBiosError("BIOS clock peer or raw field differs from the active bound")
        if field.get("capability") != "editable":
            raise ClockBiosError("Selected BIOS range is not editable")
        entries[key] = {
            "binding": binding,
            "domain": field["domain"],
            "api_domain_mask": row["api_domain_mask"],
            "before_mhz": row[role + "_mhz"],
            "after_mhz": _mhz(wanted),
        }
    saved = {"schema": 1, "selected": entries, "before": before, "after": copy.deepcopy(before)}
    raw = bytearray.fromhex(before["info_hex"])
    for key, entry in entries.items():
        match = _KEY.fullmatch(key)
        struct.pack_into("<h", raw, HEADER + int(match[1]) * STRIDE + _OFFSETS[match[2]], entry["after_mhz"])
    saved["after"]["info_hex"] = raw.hex()
    validate_contracts(selected, saved)
    return saved


def validate_contracts(changes, saved):
    """Pure exact binding of the owner's v50 subset, also before recovery."""
    if not isinstance(changes, Mapping) or any(not isinstance(k, str) or _KEY.fullmatch(k) is None for k in changes):
        raise ClockBiosError("Clock contract changes need exact range keys")
    if not changes and saved == {}:
        return
    if (
        not isinstance(saved, dict)
        or set(saved) != {"schema", "selected", "before", "after"}
        or type(saved["schema"]) is not int
        or saved["schema"] != 1
        or not isinstance(saved["selected"], dict)
        or not changes
        or set(changes) != set(saved["selected"])
    ):
        raise ClockBiosError("Clock changes and saved contracts disagree")
    before, after = _canonical(saved["before"]), _canonical(saved["after"])
    raw = bytearray.fromhex(before["info_hex"])
    selected_indices = set()
    row_bindings, table_binding = {}, None
    for key, wanted in changes.items():
        match = _KEY.fullmatch(key)
        index, role = int(match[1]), match[2]
        entry = saved["selected"][key]
        if not isinstance(entry, dict) or set(entry) != {
            "binding",
            "domain",
            "api_domain_mask",
            "before_mhz",
            "after_mhz",
        }:
            raise ClockBiosError("Malformed selected clock contract")
        binding = _bios_binding(entry["binding"], index)
        table = {k: v for k, v in binding.items() if k not in ("bios_index", "bios_row_type")}
        if table_binding is not None and table != table_binding:
            raise ClockBiosError("Selected clock fields disagree about their BIOS table")
        if index in row_bindings and row_bindings[index] != binding:
            raise ClockBiosError("Selected clock bounds disagree about their BIOS row")
        table_binding, row_bindings[index] = table, binding
        row = _selected_row(before, index, entry["domain"], binding)
        if (
            type(entry["api_domain_mask"]) is not int
            or entry["api_domain_mask"] != row["api_domain_mask"]
            or type(entry["before_mhz"]) is not int
            or entry["before_mhz"] != row[role + "_mhz"]
            or type(entry["after_mhz"]) is not int
            or _mhz(wanted) != entry["after_mhz"]
        ):
            raise ClockBiosError("Selected clock range disagrees with its saved raw baseline or request")
        struct.pack_into("<h", raw, HEADER + index * STRIDE + _OFFSETS[role], entry["after_mhz"])
        selected_indices.add(index)
    expected = copy.deepcopy(before)
    expected["info_hex"] = raw.hex()
    if not _same_state(after, expected):
        raise ClockBiosError("Clock contract changes an unselected header, peer or user request")
    old, new = _decode(bytes.fromhex(before["info_hex"])), _decode(bytes(raw))
    _, requests = _control_shape(before["user_control"], before["identity"])
    for index in selected_indices:
        prior, proposed = old[str(index)], new[str(index)]
        lo, hi = proposed["minimum_mhz"], proposed["maximum_mhz"]
        if lo > hi:
            raise ClockBiosError("Selected clock range would become inverted")
        tightening = lo > prior["minimum_mhz"] or hi < prior["maximum_mhz"]
        if tightening and index not in requests:
            raise ClockBiosError("Tightening needs the selected type15 user-clock request")
        if tightening and not lo * 1000 <= requests[index] <= hi * 1000:
            raise ClockBiosError("The selected retained user-clock request is outside the proposed range")


def verify(observation, saved, *, restore=False):
    if saved == {}:
        return []
    try:
        changes = {key: value["after_mhz"] for key, value in saved["selected"].items()}
    except (KeyError, TypeError, AttributeError) as exc:
        raise ClockBiosError("Malformed saved clock contract") from exc
    validate_contracts(changes, saved)
    actual = _shape(observation, writable=True)
    if not _same_state(actual, saved["before" if restore else "after"]):
        raise ClockBiosError("Clock range, full INFO peers or retained user requests differ from the exact expectation")
    return list(saved["selected"])
