# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only Fan2X metadata and independent BIOS activation contracts.

INFO supplies the client-role map; CONTROL GET supplies raw duty and RPM
bounds. INFO also reports loaded PWM frequency, independently of those bounds.
ClientFanCoolers STATUS can normalize bounds to 30--100 and is not a
substitute. Evidence: experiments/registry-overrides/driver-controls and
experiments/registry-overrides/vbios-fields/fan-pwm-readback-contract.json. This
module contains no setter, registry access, restart, or driver/board allowlist.
"""

from __future__ import annotations

import copy
import ctypes
import math
import re
import struct
from collections.abc import Mapping
from contextlib import nullcontext

from .bios_runtime import BiosRuntimeError
from .perf_diagnostics import PROBE_ARCHITECTURES, PerfDiagnosticError, PerfDiagnosticsReader

INFO_COMMAND, INFO_BYTES = 0x2080852E, 0x598
CONTROL_COMMAND, CONTROL_BYTES = 0x20808530, 0x3D4
CAPACITY = 16
INFO_OFFSET, INFO_STRIDE = 0x18, 0x58
CONTROL_OFFSET, CONTROL_STRIDE = 0x14, 0x3C
_GETS = frozenset(((INFO_COMMAND, INFO_BYTES), (CONTROL_COMMAND, CONTROL_BYTES)))
_ROLES = {
    "level_minimum": ("duty_minimum_q16", "%", "q16_fraction"),
    "level_maximum": ("duty_maximum_q16", "%", "q16_fraction"),
    "rpm_minimum": ("rpm_minimum", "RPM", "RPM"),
    "rpm_maximum": ("rpm_maximum", "RPM", "RPM"),
}
_FIELDS = {**_ROLES, "pwm_hz": ("pwm_frequency_hz", "Hz", "Hz")}
_KEY = re.compile(r"fan\.(0|[1-9]\d*)\.(level_minimum|level_maximum|rpm_minimum|rpm_maximum|pwm_hz)\Z")
_LAYOUT = {
    "group": "fan2x",
    "capacity": CAPACITY,
    "info_command": f"0x{INFO_COMMAND:08X}",
    "control_command": f"0x{CONTROL_COMMAND:08X}",
    "info_bytes": INFO_BYTES,
    "control_bytes": CONTROL_BYTES,
    "info_offset": INFO_OFFSET,
    "info_stride": INFO_STRIDE,
    "control_offset": CONTROL_OFFSET,
    "control_stride": CONTROL_STRIDE,
}


class FanRuntimeError(BiosRuntimeError):
    pass


def _fail(message):
    raise FanRuntimeError(message)


def _same(a, b):
    """Receipt equality must not treat True, 1 and 1.0 as interchangeable."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_same(a[key], b[key]) for key in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_same(left, right) for left, right in zip(a, b, strict=True))
    return a == b


def _mask(text):
    try:
        if not isinstance(text, str) or len(text) != 8:
            raise ValueError
        raw = bytes.fromhex(text)
        bits = int.from_bytes(raw, "little")
        if len(raw) != 4 or bits >> CAPACITY:
            raise ValueError
        return bits
    except (TypeError, ValueError):
        _fail("Fan2X inventory mask has an unrecognized geometry")


def _mapping(value, mask):
    if not isinstance(value, Mapping) or set(value) != {"1", "2", "3", "4"}:
        _fail("Fan2X needs the four client-role mapping entries")
    targets = []
    for index in value.values():
        if index is None:
            continue
        if type(index) is not int or not 0 <= index < CAPACITY or not mask & (1 << index):
            _fail("Fan2X client-role mapping is outside the inventory")
        targets.append(index)
    if len(targets) != len(set(targets)):
        _fail("Fan2X client-role mapping has duplicate targets")
    return dict(value)


def _bounds(row):
    """Unknown or invalid peers are diagnostic; selected rows need all bounds."""
    if row.get("type") not in (2, 3) or row.get("type") != row.get("control_type"):
        return None
    result = {role: row.get(field) for role, (field, _, _) in _ROLES.items()}
    if any(type(value) is not int for value in result.values()):
        return None
    if not 0 <= result["level_minimum"] < result["level_maximum"] <= 65536:
        return None
    if not 0 <= result["rpm_minimum"] <= result["rpm_maximum"] <= 65535:
        return None
    return result


def _pwm_shape(observation):
    """Optional INFO-only metadata; do not change legacy bounds identities.

    Type 2 and 3 use INFO serializer CA0C30. The routing values are retained
    without assigning connector names. Neither a malformed CONTROL bound nor
    an unknown peer makes an independently understood PWM field unavailable.
    """
    bindings, values = {}, {}
    for row in observation["entries"]:
        if row["type"] not in (2, 3):
            continue
        value = row.get("pwm_frequency_hz")
        if type(value) is not int or not 1 <= value <= 0xFFFFFF:
            continue
        binding = {
            "type": row["type"],
            "control_selector_raw": row.get("info_control_selector_raw"),
            "gpio_function_raw": row.get("info_gpio_function_raw"),
            "pwm_function_raw": row.get("info_pwm_function_raw"),
        }
        if any(type(v) is not int or not 0 <= v <= 0xFFFFFFFF for v in binding.values()):
            continue
        if binding["control_selector_raw"] > 255:
            continue
        index = row["index"]
        bindings[str(index)] = binding
        values[f"fan.{index}.pwm_hz"] = value
    return {"bindings": bindings, "values": values}


def decode(info, control):
    """Decode the two exact getter payloads without using any GPU APIs."""
    if len(info) != INFO_BYTES or len(control) != CONTROL_BYTES:
        _fail("Unexpected Fan2X parameter geometry")
    mask = _mask(info[4:8].hex())
    if info[4:8] != control[4:8] or info[0] != control[0]:
        _fail("Fan2X INFO and CONTROL group identifiers or masks disagree")
    client_mask = _mask(info[0xC:0x10].hex())
    if client_mask & ~mask:
        _fail("Fan2X client mask is outside the inventory")
    roles = _mapping({str(i + 1): None if n == 255 else n for i, n in enumerate(info[0x10:0x14])}, mask)
    entries = []
    for index in range(CAPACITY):
        if not mask & (1 << index):
            continue
        raw = info[INFO_OFFSET + index * INFO_STRIDE : INFO_OFFSET + (index + 1) * INFO_STRIDE]
        ctl = control[CONTROL_OFFSET + index * CONTROL_STRIDE : CONTROL_OFFSET + (index + 1) * CONTROL_STRIDE]
        row = {
            "index": index,
            "type": raw[0],
            "control_type": ctl[0],
            "client_ids": [int(role) for role, target in roles.items() if target == index],
            "client_visible": bool(client_mask & (1 << index)),
            "info_hex": raw.hex(),
            "control_hex": ctl.hex(),
            "issues": [],
        }
        if raw[0] in (2, 3):
            row.update(
                pwm_frequency_hz=struct.unpack_from("<I", raw, 0x18)[0],
                info_gpio_function_raw=struct.unpack_from("<I", raw, 8)[0],
                info_pwm_function_raw=struct.unpack_from("<I", raw, 0x14)[0],
                info_control_selector_raw=raw[0xE],
            )
            if not 1 <= row["pwm_frequency_hz"] <= 0xFFFFFF:
                row["issues"].append("PWM frequency is outside its nonzero unsigned BIOS-24-bit range")
        if raw[0] != ctl[0]:
            row["issues"].append("INFO and CONTROL row types disagree")
        elif raw[0] in (2, 3):
            rpm_minimum, rpm_maximum = struct.unpack_from("<II", ctl, 8)
            duty_minimum, duty_maximum = struct.unpack_from("<II", ctl, 0x18)
            row.update(
                rpm_minimum=rpm_minimum,
                rpm_maximum=rpm_maximum,
                duty_minimum_q16=duty_minimum,
                duty_maximum_q16=duty_maximum,
                info_control_selector_raw=raw[0xE],
                control_override_flag_raw=ctl[0x10],
                control_request_raw=struct.unpack_from("<I", ctl, 0x14)[0],
            )
            if _bounds(row) is None:
                row["issues"].append("Bounds are outside the traced duty Q16 / unsigned BIOS-word ranges")
        else:
            row["issues"].append("Row type has no established semantic decoder")
        entries.append(row)
    return {
        **_LAYOUT,
        "group_id": info[0],
        "mask_hex": info[4:8].hex(),
        "client_mask_hex": info[0xC:0x10].hex(),
        "client_id_to_index": roles,
        "entries": entries,
    }


def _shape(observation, *, stable=False):
    if not isinstance(observation, Mapping):
        _fail("Fan2X observation is unavailable")
    if any(not _same(observation.get(key), value) for key, value in _LAYOUT.items()):
        _fail("Fan2X observation has an unrecognized layout")
    identity = observation.get("identity")
    if not isinstance(identity, dict) or set(identity) != {"slot", "architecture", "device_id", "subsystem_id"}:
        _fail("Fan2X observation has no exact adapter identity")
    if not _same(identity["slot"], observation.get("slot")) or not _same(
        identity["architecture"], observation.get("architecture")
    ):
        _fail("Fan2X observation disagrees with its adapter identity")
    from .nvbackend import parse_slot

    address = parse_slot(identity["slot"]) if isinstance(identity["slot"], str) else None
    if address is None or address[0] != 0 or address[1] > 255 or address[2] > 31 or address[3] > 7:
        _fail("Fan2X observation has an invalid PCI address")
    for name, maximum in (("architecture", 0xFFFFFFFF), ("device_id", 0xFFFF), ("subsystem_id", 0xFFFFFFFF)):
        if type(identity[name]) is not int or not 0 <= identity[name] <= maximum:
            _fail("Fan2X observation has an invalid adapter identity")
    if identity["architecture"] not in PROBE_ARCHITECTURES:
        _fail("Fan2X probing requires a Pascal-or-later architecture")
    if stable and not _same(observation.get("stable_reads"), 2):
        _fail("Fan2X needs two stable fresh reads")
    mask, clients = _mask(observation.get("mask_hex")), _mask(observation.get("client_mask_hex"))
    if clients & ~mask:
        _fail("Fan2X client mask is outside the inventory")
    roles = _mapping(observation.get("client_id_to_index"), mask)
    group_id = observation.get("group_id")
    if type(group_id) is not int or not 0 <= group_id <= 255:
        _fail("Fan2X group identity is invalid")
    rows = observation.get("entries")
    if not isinstance(rows, list):
        _fail("Fan2X typed inventory is unavailable")
    row_types, bounds = [], {}
    for row in rows:
        if not isinstance(row, Mapping) or any(
            type(row.get(key)) is not int for key in ("index", "type", "control_type")
        ):
            _fail("Fan2X inventory contains an invalid row")
        index = row["index"]
        if not 0 <= index < CAPACITY or any(not 0 <= row[key] <= 255 for key in ("type", "control_type")):
            _fail("Fan2X inventory contains an invalid row")
        if not _same(row.get("client_visible"), bool(clients & (1 << index))) or not _same(
            row.get("client_ids"), [int(role) for role, target in roles.items() if target == index]
        ):
            _fail("Fan2X row disagrees with its client-role mapping")
        selector = row.get("info_control_selector_raw")
        if selector is not None and (type(selector) is not int or not 0 <= selector <= 255):
            _fail("Fan2X row has an invalid control selector")
        # Old receipts only captured this selector for a matched, understood
        # INFO/CONTROL row. PWM's INFO-only binding retains the selector even
        # when CONTROL disagrees, without changing that legacy identity slot.
        legacy_selector = selector if row["type"] in (2, 3) and row["type"] == row["control_type"] else None
        row_types.append([index, row["type"], row["control_type"], legacy_selector])
        understood = _bounds(row)
        if understood is not None:
            bounds.update({f"fan.{index}.{role}": value for role, value in understood.items()})
    row_types.sort()
    if [row[0] for row in row_types] != [index for index in range(CAPACITY) if mask & (1 << index)]:
        _fail("Fan2X typed inventory differs from its mask")
    return {
        "identity": copy.deepcopy(identity),
        "layout": dict(_LAYOUT),
        "group_id": group_id,
        "mask_hex": mask.to_bytes(4, "little").hex(),
        "client_mask_hex": clients.to_bytes(4, "little").hex(),
        "client_id_to_index": roles,
        "row_types": row_types,
        "bound_indices": sorted({int(_KEY.fullmatch(key)[1]) for key in bounds}),
    }, bounds


def _get(reader, command, size, transport, mask=None):
    if (command, size) not in _GETS:
        _fail("Only the two traced Fan2X GET commands are allowed")
    if command == CONTROL_COMMAND:
        if not isinstance(mask, bytes) or len(mask) != 4:
            _fail("Fan2X CONTROL GET requires the exact INFO mask")
    elif mask is not None:
        _fail("Fan2X INFO GET does not accept a mask")
    reader._binding()
    header, fields = transport
    packet = (ctypes.c_uint32 * (17 + size // 4))()
    for index, value in enumerate(header):
        packet[index] = value
    packet[2], packet[14], packet[15], packet[16] = ctypes.sizeof(packet), command, size, 0
    if mask is not None:
        packet[18] = int.from_bytes(mask, "little")
    status = reader.gpu._legacy_clk_escape(packet, fields)
    reader._binding()
    if status != 0:
        raise PerfDiagnosticError("transient", f"Fan2X GET transport failed (NTSTATUS {status})")
    if packet[16] != 0:
        kind = "unsupported" if packet[16] in (0x1F, 0x56) else "transient"
        raise PerfDiagnosticError(kind, f"Fan2X GET rejected (RM 0x{packet[16]:X})")
    if (packet[2], packet[14], packet[15]) != (ctypes.sizeof(packet), command, size):
        _fail("Fan2X response changed its command or parameter geometry")
    return bytes(packet)[68:]


def _capture(reader):
    gpu = reader.gpu
    for attempt in range(2):
        try:
            with getattr(gpu, "_lock", nullcontext()):
                api, slot, architecture = reader._binding()
                for export in (0x35AED5E8, 0x814B209F):
                    if api._i(export, ctypes.c_void_p, ctypes.c_void_p) is None:
                        raise PerfDiagnosticError("unsupported", "ClientFanCoolers getter export is unavailable")
                transport = reader._transport()
                info = _get(reader, INFO_COMMAND, INFO_BYTES, transport)
                _mask(info[4:8].hex())
                control = _get(reader, CONTROL_COMMAND, CONTROL_BYTES, transport, info[4:8])
                result = decode(info, control)
                result.update(
                    slot=slot,
                    architecture=architecture,
                    identity={
                        "slot": slot,
                        "architecture": architecture,
                        "device_id": api.selected.get("devid"),
                        "subsystem_id": api.selected.get("subsys"),
                    },
                )
                _shape(result)
                return result
        except FanRuntimeError:
            raise
        except PerfDiagnosticError as exc:
            if exc.kind != "transient":
                raise
            gpu._current_limit_transport = None
            gpu._current_limit_layout_choice = None
            if attempt:
                raise
        except Exception:
            gpu._current_limit_transport = None
            gpu._current_limit_layout_choice = None
            if attempt:
                raise
        gpu._current_limit_transport = None
        gpu._current_limit_layout_choice = None


def read(gpu):
    """Return (fresh observation, error); never substitute historical data."""
    try:
        reader = PerfDiagnosticsReader(gpu)
        first, second = _capture(reader), _capture(reader)
        if (
            not _same(_shape(first)[0], _shape(second)[0])
            or not _same(_shape(first)[1], _shape(second)[1])
            or not _same(_pwm_shape(first), _pwm_shape(second))
        ):
            _fail("Fan2X identity, inventory, bounds or PWM metadata changed between reads; retry")
        second["stable_reads"] = 2
        return second, None
    except Exception as exc:
        return None, str(exc)


def _value(role, value):
    limit = 0xFFFFFF if role == "pwm_hz" else 100 if role.startswith("level_") else 65535
    minimum = 1 if role == "pwm_hz" else 0
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or not float(value).is_integer()
        or not minimum <= value <= limit
    ):
        _fail(f"Fan2X {role} needs a whole value in {minimum}..{limit}")
    value = int(value)
    if role.startswith("level_"):
        quotient, remainder = divmod(value * 65536, 100)
        return quotient + int(remainder > 50)
    return value


def contracts(gpu, inspection, changes):
    """Bind selected BIOS metadata to a fresh paired Fan2X inventory."""
    if not isinstance(changes, Mapping):
        _fail("Fan changes must be a mapping")
    selected = sorted(
        key for key in changes if isinstance(key, str) and key.startswith("fan.") and key != "fan.zero_rpm"
    )
    if not selected:
        return {}
    if any(_KEY.fullmatch(key) is None for key in selected):
        _fail("Selected fan field has no established runtime semantics")
    fields = {}
    for field in (inspection or {}).get("fields", []):
        key = field.get("key")
        if key in fields:
            _fail("BIOS inspection contains duplicate fields")
        fields[key] = field
    observation, error = read(gpu)
    if error or observation is None:
        _fail("Fan2X metadata is unavailable: " + str(error))
    state, bounds = _shape(observation, stable=True)
    pwm = _pwm_shape(observation)
    values = {**bounds, **pwm["values"]}
    rows = {row["index"]: row for row in observation["entries"]}
    after = dict(values)
    for key in selected:
        match = _KEY.fullmatch(key)
        index, role = int(match[1]), match[2]
        row = rows.get(index)
        if row is None or key not in values or not row["client_visible"] or not row["client_ids"]:
            _fail(f"{key}: selected Fan2X row has no understood client-visible metadata")
        metadata_roles = {role: _FIELDS[role]} if role == "pwm_hz" else _ROLES
        for peer_role, (_, units, _) in metadata_roles.items():
            peer_key = f"fan.{index}.{peer_role}"
            field = fields.get(peer_key)
            if not isinstance(field, Mapping) or field.get("units") != units:
                _fail(f"{key}: BIOS fan metadata or units are unavailable")
            mapping = _mapping(field.get("client_id_to_index"), _mask(state["mask_hex"]))
            if not _same(mapping, state["client_id_to_index"]):
                _fail(f"{key}: BIOS and runtime client-role mappings disagree")
            if _value(peer_role, field.get("current")) != values[peer_key]:
                _fail(f"{key}: driver fan metadata differs from the decoded BIOS value")
        after[key] = _value(role, changes[key])
    for index in {int(_KEY.fullmatch(key)[1]) for key in selected if _KEY.fullmatch(key)[2] in _ROLES}:
        prefix = f"fan.{index}."
        if (
            after[prefix + "level_minimum"] >= after[prefix + "level_maximum"]
            or after[prefix + "rpm_minimum"] > after[prefix + "rpm_maximum"]
        ):
            _fail("Selected Fan2X minimum exceeds its maximum")
    unchanged = {
        "state": state,
        "bounds": {key: value for key, value in bounds.items() if key not in selected},
        # Optional in old receipts. All new contracts retain independently
        # understood PWM peers, including when only a bound is edited.
        "pwm": {
            "bindings": pwm["bindings"],
            "values": {key: value for key, value in pwm["values"].items() if key not in selected},
        },
    }
    result = {}
    for key in selected:
        match = _KEY.fullmatch(key)
        index, role = int(match[1]), match[2]
        result[key] = {
            "schema": 1,
            "kind": "fan2x_pwm" if role == "pwm_hz" else "fan2x_bound",
            "index": index,
            "role": role,
            "unit": _FIELDS[role][2],
            "before": values[key],
            "after": after[key],
            "selected_fields": list(selected),
            "unchanged": copy.deepcopy(unchanged),
        }
    return result


def verify(snapshot, saved_contracts, *, restore=False):
    """Verify a full transaction snapshot's fan_bounds against its receipt."""
    if not isinstance(saved_contracts, Mapping):
        _fail("Fan2X contracts must be a mapping")
    if not saved_contracts:
        return []
    if not isinstance(snapshot, Mapping):
        _fail("Fan2X snapshot is unavailable")
    state, bounds = _shape(snapshot.get("fan_bounds"), stable=True)
    pwm = _pwm_shape(snapshot["fan_bounds"])
    values = {**bounds, **pwm["values"]}
    if any(not isinstance(key, str) for key in saved_contracts):
        _fail("Unknown Fan2X contract key")
    selected = sorted(saved_contracts)
    expected_keys = {"schema", "kind", "index", "role", "unit", "before", "after", "selected_fields", "unchanged"}
    reference = None
    for key in selected:
        match = _KEY.fullmatch(key) if isinstance(key, str) else None
        receipt = saved_contracts[key]
        if match is None or not isinstance(receipt, Mapping) or set(receipt) != expected_keys:
            _fail("Unknown or malformed Fan2X contract")
        index, role = int(match[1]), match[2]
        metadata = {
            "schema": 1,
            "kind": "fan2x_pwm" if role == "pwm_hz" else "fan2x_bound",
            "index": index,
            "role": role,
            "unit": _FIELDS[role][2],
            "selected_fields": selected,
        }
        if any(not _same(receipt[name], value) for name, value in metadata.items()):
            _fail("Malformed Fan2X contract identity or selected fields")
        maximum = 0xFFFFFF if role == "pwm_hz" else 65536 if role.startswith("level_") else 65535
        minimum = 1 if role == "pwm_hz" else 0
        if any(
            type(receipt[name]) is not int or not minimum <= receipt[name] <= maximum for name in ("before", "after")
        ):
            _fail("Malformed Fan2X contract bounds")
        unchanged = receipt["unchanged"]
        if (
            not isinstance(unchanged, dict)
            or set(unchanged) not in ({"state", "bounds"}, {"state", "bounds", "pwm"})
            or not isinstance(unchanged["bounds"], dict)
        ):
            _fail("Malformed Fan2X unchanged-state contract")
        if reference is None:
            reference = unchanged
        elif not _same(reference, unchanged):
            _fail("Fan2X contracts disagree about their unchanged peers")
        if not _same(unchanged["state"], state):
            _fail("Fan2X target, inventory or role mapping changed")
        expected_peers = {f"fan.{peer}.{name}" for peer in state["bound_indices"] for name in _ROLES} - set(selected)
        if set(unchanged["bounds"]) != expected_peers:
            _fail("Fan2X contract omits understood peer bounds")
        if index not in state["client_id_to_index"].values() or not _mask(state["client_mask_hex"]) & (1 << index):
            _fail("Fan2X contract selects an unmapped or hidden row")
        for peer, wanted in unchanged["bounds"].items():
            if peer in selected or type(wanted) is not int or not _same(bounds.get(peer), wanted):
                _fail("Fan2X untouched peer bounds changed or are unavailable")
        pwm_receipt = unchanged.get("pwm")
        if role == "pwm_hz" and pwm_receipt is None:
            _fail("Fan2X PWM contract omits its routing and peer metadata")
        if "pwm" in unchanged:
            if (
                not isinstance(pwm_receipt, dict)
                or set(pwm_receipt) != {"bindings", "values"}
                or not isinstance(pwm_receipt["bindings"], dict)
                or not isinstance(pwm_receipt["values"], dict)
            ):
                _fail("Malformed Fan2X PWM peer contract")
            if not _same(pwm_receipt["bindings"], pwm["bindings"]):
                _fail("Fan2X PWM routing identity or availability changed")
            if set(pwm_receipt["values"]) != set(pwm["values"]) - set(selected):
                _fail("Fan2X PWM contract omits understood peer frequencies")
            for peer, wanted in pwm_receipt["values"].items():
                if type(wanted) is not int or not _same(pwm["values"].get(peer), wanted):
                    _fail("Fan2X untouched peer PWM frequency changed or is unavailable")
        wanted = receipt["before" if restore else "after"]
        if not _same(values.get(key), wanted):
            _fail(f"{key}: Fan2X metadata differs from the expected active value")
    return selected
