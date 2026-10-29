# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Configured GR power-gating masks and bounded disable-only BIOS contracts.

Only the transaction owner changes BIOS bytes or restarts a GPU. These contracts
verify configured controller membership, not physical power-gating residency.
"""

from __future__ import annotations

import copy
import hashlib
import re
import struct
from collections.abc import Mapping

from . import lpwr_runtime as lp
from . import vbios_fields
from .clock_vf_registry import _identity
from .grpg_main_projection import MainProjectionError
from .grpg_main_projection import profiles as main_profiles
from .perf_diagnostics import PerfDiagnosticError, PerfDiagnosticsReader
from .registry_flags import _same_state

KEY = re.compile(r"lpwr\.gr_pg\.(0|[1-9][0-9]*)\.enabled\Z")
CONTROLLERS = tuple(range(22))
_TABLES = (("index", 0x74, 0x24, 6, 18), ("grpg", 0x80, 0x39, 10, 5))
_AUX_KEYS = ((0, 1), *((role, 0x800) for role in range(4)), *((role, 0x806) for role in range(4)))
_BIT1_ROLES = tuple(range(11))
_BIT4_KEYS = ((0, 1), *((role, 0x801) for role in range(3)), (0, 0x805), (1, 0x805))
_SCHEMAS = {0: 1, 8: 2, 2: 3, 10: 4, 1: 5, 3: 5, 9: 5, 11: 5, 16: 6, 18: 7, 24: 7, 26: 7}


class GrpgError(RuntimeError):
    """An exact configured-mask disable projection is unavailable."""


def selected(changes):
    """Return this isolated category, without requiring a GET when absent."""
    if not isinstance(changes, Mapping) or any(not isinstance(key, str) for key in changes):
        raise GrpgError("GRPG changes need a field/value mapping")
    result = {}
    for key, value in changes.items():
        if not key.startswith("lpwr.gr_pg."):
            continue
        match = KEY.fullmatch(key)
        if not match or len(match[1]) > 1 or not 0 <= int(match[1]) < 5:
            raise GrpgError("GRPG field index is outside the understood loaded rows")
        if value is not False:
            raise GrpgError("GRPG edits may only disable a captured enabled entry")
        result[key] = value
    if result and len(result) != len(changes):
        raise GrpgError("Apply GRPG disables separately from other BIOS controls")
    return result


def _controllers(support_raw, states_raw):
    supports = lp._attributes(support_raw, lp.SUPPORT, CONTROLLERS)
    indices = tuple(i for i in CONTROLLERS if supports[str(i)] == (True, 1))
    if states_raw is not None and not indices:
        raise GrpgError("LPWR state reply has no independently supported controller")
    states = lp._attributes(states_raw, lp.STATES, indices) if states_raw is not None else {}
    result = {}
    for index in CONTROLLERS:
        available, value = supports[str(index)]
        state_available, mask = states.get(str(index), (False, None))
        result[str(index)] = {
            "available": available,
            "supported": bool(value) if available else None,
            "states_available": state_available,
            "configured_pstate_mask": mask,
        }
    return result


def _aux_request():
    request = bytearray(lp.ATTRIBUTE_SIZE)
    struct.pack_into("<I", request, 0, len(_AUX_KEYS))
    for ordinal, (role, attribute) in enumerate(_AUX_KEYS):
        struct.pack_into("<HHH", request, 4 + 16 * ordinal, 1, role, attribute)
    return bytes(request)


def _aux_group(raw):
    request, untouched, result = _aux_request(), bytearray(raw), {}
    if len(raw) != lp.ATTRIBUTE_SIZE:
        raise GrpgError("Malformed GRPG auxiliary packet size")
    for ordinal, (role, attribute) in enumerate(_AUX_KEYS):
        at = 4 + 16 * ordinal
        flags, value = struct.unpack_from("<HI", raw, at + 6)
        if flags not in (0, 1) or flags and attribute != 0x806 and value not in (0, 1):
            raise GrpgError("Malformed GRPG auxiliary support response")
        result[f"{attribute:x}:{role}"] = {"available": bool(flags), "value": value if flags else None}
        untouched[at + 6 : at + 12] = request[at + 6 : at + 12]
    if untouched != request:
        raise GrpgError("GRPG auxiliary GET changed routing or opaque bytes")
    return result


def _auxiliary(value):
    if not isinstance(value, dict) or set(value) != {"feature_hex", "feature", "group_hex", "group"}:
        raise GrpgError("Missing GRPG auxiliary readback")
    feature_raw = lp._hex(value["feature_hex"], lp.ATTRIBUTE_SIZE)
    available, number = lp._attributes(feature_raw, 0x38, (0,))["0"]
    feature = {"available": available, "value": number}
    group = _aux_group(lp._hex(value["group_hex"], lp.ATTRIBUTE_SIZE))
    if not _same_state(feature, value["feature"]) or not _same_state(group, value["group"]):
        raise GrpgError("Typed GRPG auxiliary observation differs from its reply bytes")
    return {"feature": feature, "group": group}


def _bit1_request():
    request = bytearray(lp.ATTRIBUTE_SIZE)
    struct.pack_into("<I", request, 0, len(_BIT1_ROLES))
    for ordinal, role in enumerate(_BIT1_ROLES):
        struct.pack_into("<HHH", request, 4 + 16 * ordinal, 4, role, 9)
    return bytes(request)


def _bit1_roles(raw):
    request, untouched, roles = _bit1_request(), bytearray(raw), {}
    if len(raw) != lp.ATTRIBUTE_SIZE:
        raise GrpgError("Malformed GRPG bit1 support packet size")
    for ordinal, role in enumerate(_BIT1_ROLES):
        at = 4 + 16 * ordinal
        flags, value = struct.unpack_from("<HI", raw, at + 6)
        # The selected-role support gate precedes the attribute9 handler.
        # Unsupported roles leave an invalid row; valid false is not defined.
        if flags not in (0, 1) or flags and value != 1:
            raise GrpgError("Malformed GRPG bit1 support response")
        roles[str(role)] = {"available": bool(flags), "supported": True if flags else None}
        untouched[at + 6 : at + 12] = request[at + 6 : at + 12]
    if untouched != request:
        raise GrpgError("GRPG bit1 GET changed routing or opaque bytes")
    return roles


def _bit1(value):
    if not isinstance(value, dict) or set(value) != {"support_hex", "roles"}:
        raise GrpgError("Missing GRPG bit1 support readback")
    roles = _bit1_roles(lp._hex(value["support_hex"], lp.ATTRIBUTE_SIZE))
    if not _same_state(roles, value["roles"]):
        raise GrpgError("Typed GRPG bit1 support differs from its reply bytes")
    return {"roles": roles}


def _bit4_request():
    request = bytearray(lp.ATTRIBUTE_SIZE)
    struct.pack_into("<I", request, 0, len(_BIT4_KEYS))
    for ordinal, (role, attribute) in enumerate(_BIT4_KEYS):
        struct.pack_into("<HHH", request, 4 + 16 * ordinal, 7, role, attribute)
    return bytes(request)


def _bit4_group(raw):
    request, untouched, result = _bit4_request(), bytearray(raw), {}
    if len(raw) != lp.ATTRIBUTE_SIZE:
        raise GrpgError("Malformed GRPG type7 packet size")
    for ordinal, (role, attribute) in enumerate(_BIT4_KEYS):
        at = 4 + 16 * ordinal
        flags, value = struct.unpack_from("<HI", raw, at + 6)
        if flags not in (0, 1) or flags and attribute != 0x805 and value not in (0, 1):
            raise GrpgError("Malformed GRPG type7 support response")
        result[f"{attribute:x}:{role}"] = {"available": bool(flags), "value": value if flags else None}
        untouched[at + 6 : at + 12] = request[at + 6 : at + 12]
    if untouched != request:
        raise GrpgError("GRPG type7 GET changed routing or opaque bytes")
    return result


def _features(raw, controllers):
    indices = tuple(i for i in CONTROLLERS if controllers[str(i)]["supported"] is True)
    if (raw is None) != (not indices):
        raise GrpgError("GRPG features need independently supported controller routing")
    values = lp._attributes(raw, 0x38, indices) if indices else {}
    return {
        str(i): {"available": values.get(str(i), (False, None))[0], "value": values.get(str(i), (False, None))[1]}
        for i in CONTROLLERS
    }


def _main(value, controllers):
    if not isinstance(value, dict) or set(value) != {"features_hex", "features", "bit4_group_hex", "bit4_group"}:
        raise GrpgError("Missing GRPG main dependency readback")
    raw = None if value["features_hex"] is None else lp._hex(value["features_hex"], lp.ATTRIBUTE_SIZE)
    features = _features(raw, controllers)
    group = _bit4_group(lp._hex(value["bit4_group_hex"], lp.ATTRIBUTE_SIZE))
    if not _same_state(features, value["features"]) or not _same_state(group, value["bit4_group"]):
        raise GrpgError("Typed GRPG main dependency observation differs from its reply bytes")
    return {"features": features, "bit4_group": group}


def _observation(value, *, allow_missing_aux_feature=False):
    if not isinstance(value, dict) or set(value) not in (
        lp._OBS_KEYS,
        lp._OBS_KEYS | {"auxiliary"},
        lp._OBS_KEYS | {"bit1"},
        lp._OBS_KEYS | {"auxiliary", "bit1"},
        lp._OBS_KEYS | {"auxiliary", "bit1", "main"},
    ):
        raise GrpgError("Malformed GRPG observation")
    if type(value["schema"]) is not int or value["schema"] != 1:
        raise GrpgError("Unknown GRPG observation schema")
    if type(value["stable_reads"]) is not int or value["stable_reads"] != 2:
        raise GrpgError("Two fresh stable GRPG captures are required")
    identity = _identity(value["identity"])
    pstates = lp._pstates(lp._hex(value["pstate_info_hex"], lp.PSTATE_SIZE))
    support = lp._hex(value["support_hex"], lp.ATTRIBUTE_SIZE)
    states = None if value["states_hex"] is None else lp._hex(value["states_hex"], lp.ATTRIBUTE_SIZE)
    controllers = _controllers(support, states)
    if not _same_state(pstates, value["pstates"]) or not _same_state(controllers, value["controllers"]):
        raise GrpgError("Typed GRPG observation differs from its reply bytes")
    result = {"identity": identity, "pstates": pstates, "controllers": controllers}
    if "auxiliary" in value:
        result["auxiliary"] = _auxiliary(value["auxiliary"])
    if "bit1" in value:
        result["bit1"] = _bit1(value["bit1"])
    if "main" in value:
        result["main"] = _main(value["main"], controllers)
        auxiliary_feature = result["auxiliary"]["feature"]
        if (not allow_missing_aux_feature or auxiliary_feature["available"]) and not _same_state(
            auxiliary_feature, result["main"]["features"]["0"]
        ):
            raise GrpgError("Independent GRPG controller0 feature captures disagree")
    return result


def _once(reader, identity, *, auxiliary=False, bit1=False, main=False):
    auxiliary, bit1 = auxiliary or main, bit1 or main
    info = lp._get(reader, identity, lp.PSTATE_COMMAND, lp.PSTATE_SIZE)
    pstates = lp._pstates(info)
    support = lp._get(reader, identity, lp.ATTRIBUTE_COMMAND, lp.ATTRIBUTE_SIZE, lp._request(lp.SUPPORT, CONTROLLERS))
    values = lp._attributes(support, lp.SUPPORT, CONTROLLERS)
    indices = tuple(i for i in CONTROLLERS if values[str(i)] == (True, 1))
    states = None
    if indices:
        try:
            states = lp._get(reader, identity, lp.ATTRIBUTE_COMMAND, lp.ATTRIBUTE_SIZE, lp._request(lp.STATES, indices))
        except PerfDiagnosticError as exc:
            if exc.kind != "unsupported":
                raise
    result = {
        "schema": 1,
        "stable_reads": 2,
        "identity": identity,
        "pstate_info_hex": info.hex(),
        "pstates": pstates,
        "support_hex": support.hex(),
        "states_hex": states.hex() if states is not None else None,
        "controllers": _controllers(support, states),
    }
    if auxiliary:
        feature = lp._get(reader, identity, lp.ATTRIBUTE_COMMAND, lp.ATTRIBUTE_SIZE, lp._request(0x38, (0,)))
        group = lp._get(reader, identity, lp.ATTRIBUTE_COMMAND, lp.ATTRIBUTE_SIZE, _aux_request())
        available, value = lp._attributes(feature, 0x38, (0,))["0"]
        result["auxiliary"] = {
            "feature_hex": feature.hex(),
            "feature": {"available": available, "value": value},
            "group_hex": group.hex(),
            "group": _aux_group(group),
        }
    if bit1:
        support = lp._get(reader, identity, lp.ATTRIBUTE_COMMAND, lp.ATTRIBUTE_SIZE, _bit1_request())
        result["bit1"] = {"support_hex": support.hex(), "roles": _bit1_roles(support)}
    if main:
        raw = (
            lp._get(reader, identity, lp.ATTRIBUTE_COMMAND, lp.ATTRIBUTE_SIZE, lp._request(0x38, indices))
            if indices
            else None
        )
        group = lp._get(reader, identity, lp.ATTRIBUTE_COMMAND, lp.ATTRIBUTE_SIZE, _bit4_request())
        result["main"] = {
            "features_hex": raw.hex() if raw is not None else None,
            "features": _features(raw, result["controllers"]),
            "bit4_group_hex": group.hex(),
            "bit4_group": _bit4_group(group),
        }
    _observation(result, allow_missing_aux_feature=True)
    return result


def read(gpu, *, auxiliary=False, bit1=False, main=False):
    """Read all known controller support/masks; unavailable peers stay diagnostic."""
    reader = PerfDiagnosticsReader(gpu)
    try:
        with gpu._lock:
            identity = lp.clock._binding(reader)
            for attempt in range(2):
                try:
                    first = _once(reader, identity, auxiliary=auxiliary, bit1=bit1, main=main)
                    second = _once(reader, identity, auxiliary=auxiliary, bit1=bit1, main=main)
                    if not _same_state(_observation(first, allow_missing_aux_feature=True),
                                       _observation(second, allow_missing_aux_feature=True)):
                        raise PerfDiagnosticError(
                            "unstable", "GRPG routes or controller configuration changed between reads"
                        )
                    return second, None
                except PerfDiagnosticError as exc:
                    if exc.kind != "transient" or attempt == 1:
                        raise
                    gpu._current_limit_transport = None
                    gpu._current_limit_layout_choice = None
    except Exception as exc:
        return None, str(exc)
    return None, "GRPG configuration is unavailable"


def _tables(image):
    parsed = vbios_fields._Image(image)
    result = {}
    for name, pointer, owner, hs, stride in _TABLES:
        table = parsed.table("lpwr_" + name, "P", pointer, ((0x10, hs, stride),), owner)
        if table["object_id"] != owner or not 0 < table["count"] < 6:
            raise GrpgError("GRPG model lacks understood bounded object ownership")
        result[name] = {
            "offset": table["offset"],
            "owner": owner,
            "raw_hex": parsed.span(table["offset"], table["size"]).hex(),
        }
    at = parsed.pointer("P", 0)
    header = parsed.span(at, 14)
    if header[:2] != b"\x70\x0e" or not header[2] or not header[3]:
        raise GrpgError("GRPG projection requires the populated PERF v70 aggregation path")
    stride = header[2] + header[4] * header[5]
    size = 14 + stride * header[3]
    if parsed.own(at, size, 0x0E) != 0x0E:
        raise GrpgError("PERF table lacks its bounded object owner")
    raw = parsed.span(at, size)
    result["perf"] = {
        "offset": at,
        "owner": 0x0E,
        "header_hex": header.hex(),
        "row_first_hex": bytes(raw[14 + i * stride] for i in range(header[3])).hex(),
        "table_sha256": hashlib.sha256(raw).hexdigest(),
    }
    _table_data(result)
    return result


def _table_data(tables):
    if not isinstance(tables, dict) or set(tables) != {"index", "grpg", "perf"}:
        raise GrpgError("Missing bounded GRPG model tables")
    decoded, spans = {}, []
    for name, _, owner, hs, stride in _TABLES:
        value = tables[name]
        if not isinstance(value, dict) or set(value) != {"offset", "owner", "raw_hex"}:
            raise GrpgError("Malformed GRPG table evidence")
        at = lp._int(value["offset"], 0, 16 * 1024 * 1024)
        if type(value["owner"]) is not int or value["owner"] != owner:
            raise GrpgError("GRPG table owner differs")
        text = value["raw_hex"]
        if not isinstance(text, str) or not 2 * (hs + stride) <= len(text) <= 2 * (hs + 5 * stride):
            raise GrpgError("GRPG table extent is invalid")
        raw = lp._hex(text, len(text) // 2)
        if raw[:3] != bytes((0x10, hs, stride)) or not 0 < raw[3] < 6 or len(raw) != hs + stride * raw[3]:
            raise GrpgError("GRPG table geometry differs")
        decoded[name] = raw
        spans.append((at, at + len(raw)))
    perf = tables["perf"]
    if not isinstance(perf, dict) or set(perf) != {"offset", "owner", "header_hex", "row_first_hex", "table_sha256"}:
        raise GrpgError("Malformed PERF aggregation evidence")
    at = lp._int(perf["offset"], 0, 16 * 1024 * 1024)
    if type(perf["owner"]) is not int or perf["owner"] != 0x0E:
        raise GrpgError("PERF table owner differs")
    header = lp._hex(perf["header_hex"], 14)
    if header[:2] != b"\x70\x0e" or not header[2] or not header[3]:
        raise GrpgError("PERF v70 composite geometry is not understood")
    first = lp._hex(perf["row_first_hex"], header[3])
    if all(value == 255 for value in first):
        raise GrpgError("PERF is not on the populated aggregation branch")
    if not isinstance(perf["table_sha256"], str) or not lp._HEX256.fullmatch(perf["table_sha256"]):
        raise GrpgError("Missing exact PERF table fingerprint")
    spans.append((at, at + 14 + (header[2] + header[4] * header[5]) * header[3]))
    spans.sort()
    if any(end > 16 * 1024 * 1024 for _, end in spans) or any(
        a[1] > b[0] for a, b in zip(spans, spans[1:], strict=False)
    ):
        raise GrpgError("GRPG model table spans overlap or exceed the bounded image")
    return decoded


def _aggregate(flags):
    result = 0
    for value in flags:
        if value & 1:
            result |= value & 0x1B
    return result


def _aggregate_change(changes, tables):
    grpg = _table_data(tables)["grpg"]
    flags = [grpg[10 + 5 * i] for i in range(grpg[3])]
    disabled = {int(KEY.fullmatch(key)[1]) for key in selected(changes)}
    if any(i >= len(flags) or not flags[i] & 1 for i in disabled):
        raise GrpgError("Selected GRPG entry is absent or already disabled")
    after = [value & ~1 if i in disabled else value for i, value in enumerate(flags)]
    loss = _aggregate(flags) ^ _aggregate(after)
    if loss not in _SCHEMAS:
        raise GrpgError("GRPG disable would remove the last enabled entry or an unqualified auxiliary capability")
    return loss


def _aux_projection(core, flags, routes, removed, *, feature_fallback=None):
    auxiliary = core.get("auxiliary")
    if auxiliary is None:
        raise GrpgError("Last GRPG auxiliary contributor requires independent auxiliary readback")
    feature, group = auxiliary["feature"], auxiliary["group"]
    checked_feature = feature if feature["available"] else feature_fallback
    if (
        not checked_feature or not checked_feature["available"]
        or not checked_feature["value"] & 0x10
        or group["1:0"] != {"available": True, "value": 1}
        or group["800:0"] != {"available": True, "value": 1}
        or not group["806:0"]["available"]
    ):
        raise GrpgError("GRPG auxiliary role, configured mask and controller feature must be available and active")
    mask = group["806:0"]["value"]
    for slot, ids in routes:
        if slot < len(flags):
            expected = ids if flags[slot] & 9 == 9 else 0
            if mask & ids != expected:
                raise GrpgError("Loaded GRPG auxiliary membership differs from a consumed BIOS entry")
    if mask & ~removed:
        raise GrpgError("Untouched or fallback auxiliary membership prevents removing role support")
    survivors = [group[f"800:{i}"] for i in range(1, 4)]
    if any(value == {"available": True, "value": 1} for value in survivors):
        any_supported = 1
    elif all(value["available"] for value in survivors):
        any_supported = 0
    else:
        raise GrpgError("Unavailable auxiliary support prevents determining the remaining group support")
    after = copy.deepcopy(auxiliary)
    if after["feature"]["available"]:
        after["feature"]["value"] &= ~0x10
    after["group"]["800:0"] = {"available": True, "value": 0}
    after["group"]["806:0"] = {"available": True, "value": 0}
    after["group"]["1:0"] = {"available": True, "value": any_supported}
    return after


def _bit1_projection(core, *, allow_no_peer=False):
    before = core.get("bit1")
    if before is None or before["roles"]["0"] != {"available": True, "supported": True}:
        raise GrpgError("Last GRPG bit1 contributor requires independently available role0 support")
    peers = [before["roles"][str(i)] for i in _BIT1_ROLES if i != 0]
    if not any(row == {"available": True, "supported": True} for row in peers):
        if not allow_no_peer or any(row != {"available": False, "supported": None} for row in peers):
            raise GrpgError("GRPG bit1 removal requires another supported role as a query witness")
    after = copy.deepcopy(before)
    after["roles"]["0"] = {"available": False, "supported": None}
    return after


def _derive(changes, before, tables, *, full_scope=False):
    requested = selected(changes)
    loss = _aggregate_change(requested, tables)
    core, data = _observation(before, allow_missing_aux_feature=full_scope or loss in (16, 18, 24, 26)), _table_data(tables)
    controller = core["controllers"]["0"]
    if not controller["available"] or controller["supported"] is not True or not controller["states_available"]:
        raise GrpgError("GRPG needs available support and a configured Pstate mask")
    live_mask = controller["configured_pstate_mask"]
    grpg, index = data["grpg"], data["index"]
    flags = [grpg[10 + 5 * i] for i in range(grpg[3])]
    bindings, disabled = {}, set()
    for key in requested:
        ordinal = int(KEY.fullmatch(key)[1])
        if ordinal >= len(flags):
            raise GrpgError("Selected GRPG entry is absent")
        value = flags[ordinal]
        if not value & 1:
            raise GrpgError("Selected GRPG entry is already disabled")
        bindings[key] = {
            "index": ordinal,
            "offset": tables["grpg"]["offset"] + 10 + 5 * ordinal,
            "before_byte": value,
            "after_byte": value & ~1,
        }
        disabled.add(ordinal)
    routed = dict.fromkeys(disabled, 0)
    untouched, routes = 0, []
    for row in core["pstates"]["rows"]:
        selector = row["lpwr_index"]
        slot = 5
        if selector < 6:
            if selector >= index[3]:
                raise GrpgError("A valid LPWR selector addresses an unloaded index row")
            slot = index[6 + 18 * selector + 1]
            slot = slot if slot < 6 else 5
        routes.append((slot, row["pstate_id_mask"]))
        if slot < len(flags):
            mask = row["pstate_id_mask"]
            expected = mask if flags[slot] & 1 else 0
            if live_mask & mask != expected:
                raise GrpgError("Loaded GRPG membership differs from a consumed BIOS entry")
        if slot in disabled:
            routed[slot] |= row["pstate_id_mask"]
        else:
            untouched |= row["pstate_id_mask"]
    if loss & 1:
        if disabled != {i for i, value in enumerate(flags) if value & 1}:
            raise GrpgError("Main GRPG removal requires every enabled BIOS entry")
        if not {"auxiliary", "bit1", "main"} <= core.keys():
            raise GrpgError("Main GRPG removal requires independent dependency readbacks")
        try:
            after_profiles = main_profiles(core)
        except MainProjectionError as exc:
            raise GrpgError(str(exc)) from exc
        return bindings, after_profiles, None, None
    removed = 0
    for mask in routed.values():
        if not mask or live_mask & mask != mask:
            raise GrpgError("Every selected GRPG entry must have an active configured Pstate route")
        removed |= mask
    if removed & untouched:
        raise GrpgError("Selected GRPG routes overlap untouched Pstate membership")
    after = copy.deepcopy(core["controllers"])
    after["0"]["configured_pstate_mask"] = live_mask & ~removed
    if full_scope and loss in (2, 10):
        if not {"auxiliary", "bit1", "main"} <= core.keys() or not core["main"]["features"]["0"]["available"]:
            raise GrpgError("GRPG bit1 removal requires complete main feature readback")
        projected = {key: copy.deepcopy(core[key]) for key in ("controllers", "auxiliary", "bit1", "main")}
        projected["controllers"] = after
        projected["bit1"] = _bit1_projection(core, allow_no_peer=True)
        feature = core["main"]["features"]["0"]
        if loss & 8:
            projected["auxiliary"] = _aux_projection(core, flags, routes, removed, feature_fallback=feature)
            projected["main"]["features"]["0"]["value"] &= ~0x10
        else:
            group = projected["auxiliary"]["group"]
            removed_bit3 = 0
            for slot, ids in routes:
                if slot in disabled and flags[slot] & 9 == 9:
                    removed_bit3 |= ids
            if removed_bit3:
                if group["800:0"] != {"available": True, "value": 1} or not group["806:0"]["available"]:
                    raise GrpgError("GRPG bit3 routed mask and support must be available")
                mask = group["806:0"]["value"]
                for slot, ids in routes:
                    if slot < len(flags):
                        expected = ids if flags[slot] & 9 == 9 else 0
                        if mask & ids != expected:
                            raise GrpgError("Loaded GRPG bit3 membership differs from the known BIOS routes")
                group["806:0"]["value"] = mask & ~removed_bit3
        return bindings, projected, None, None
    if loss in (16, 18, 24, 26):
        if not {"auxiliary", "bit1", "main"} <= core.keys():
            raise GrpgError("Last GRPG bit4 contributor requires complete independent readback")
        main = core["main"]
        feature, group = main["features"]["0"], main["bit4_group"]
        if (not feature["available"] or not feature["value"] & 8
                or group["1:0"] != {"available": True, "value": 1}
                or group["801:0"] != {"available": True, "value": 1}):
            raise GrpgError("GRPG bit4 removal requires the active role")
        peers = [group[f"801:{i}"] for i in (1, 2)]
        peer_supported = any(row == {"available": True, "value": 1} for row in peers)
        if not peer_supported and any(row != {"available": True, "value": 0} for row in peers):
            raise GrpgError("GRPG bit4 peer support is unknown")
        bit4_mask = 0
        for slot, ids in routes:
            if slot < len(flags) and flags[slot] & 0x11 == 0x11:
                bit4_mask |= ids
        if not bit4_mask or group["805:0"] != {"available": True, "value": bit4_mask}:
            raise GrpgError("GRPG bit4 configured mask differs from the known BIOS routes")
        projected = {key: copy.deepcopy(core[key]) for key in ("controllers", "auxiliary", "bit1", "main")}
        projected["controllers"] = after
        aux_group = projected["auxiliary"]["group"]
        removed_bit3 = 0
        for slot, ids in routes:
            if slot in disabled and flags[slot] & 9 == 9:
                removed_bit3 |= ids
        if loss & 8:
            projected["auxiliary"] = _aux_projection(core, flags, routes, removed, feature_fallback=feature)
        elif removed_bit3:
            if (aux_group["800:0"] != {"available": True, "value": 1}
                    or not aux_group["806:0"]["available"]):
                raise GrpgError("GRPG bit3 routed mask and support must be available")
            mask = aux_group["806:0"]["value"]
            for slot, ids in routes:
                if slot < len(flags):
                    expected = ids if flags[slot] & 9 == 9 else 0
                    if mask & ids != expected:
                        raise GrpgError("Loaded GRPG bit3 membership differs from the known BIOS routes")
            aux_group["806:0"]["value"] = mask & ~removed_bit3
        if loss & 2:
            projected["bit1"] = _bit1_projection(core, allow_no_peer=True)
        if projected["auxiliary"]["feature"]["available"]:
            projected["auxiliary"]["feature"]["value"] &= ~8
        projected["main"]["features"]["0"]["value"] &= ~(8 | (0x10 if loss & 8 else 0))
        if peer_supported:
            projected["main"]["bit4_group"]["801:0"]["value"] = 0
            projected["main"]["bit4_group"]["805:0"]["value"] = 0
        else:
            for key in projected["main"]["bit4_group"]:
                projected["main"]["bit4_group"][key] = {"available": False, "value": None}
        return bindings, projected, None, None
    auxiliary = _aux_projection(core, flags, routes, removed) if loss & 8 else None
    bit1 = _bit1_projection(core) if loss & 2 else None
    return bindings, after, auxiliary, bit1


def contracts(gpu, image_bytes, changes):
    """Capture a qualified GRPG disable; no SET, registry or restart operations."""
    if not selected(changes):
        return {}
    tables = _tables(image_bytes)
    loss = _aggregate_change(changes, tables)
    schema = {2: 8, 10: 9}.get(loss, _SCHEMAS[loss])
    if schema in (5, 6, 7, 8, 9):
        before, error = read(gpu, main=True)
    elif schema == 4:
        before, error = read(gpu, auxiliary=True, bit1=True)
    elif schema == 2:
        before, error = read(gpu, auxiliary=True)
    elif schema == 3:
        before, error = read(gpu, bit1=True)
    else:
        before, error = read(gpu)
    if error or before is None:
        raise GrpgError("GRPG configuration is unavailable: " + str(error))
    bindings, after, after_auxiliary, after_bit1 = _derive(changes, before, tables, full_scope=schema in (8, 9))
    result = {
        "schema": schema,
        "kind": "lpwr_grpg_disable",
        "before_image_sha256": hashlib.sha256(image_bytes).hexdigest(),
        "before": copy.deepcopy(before),
        "tables": tables,
        "selected": bindings,
    }
    result["after_profiles" if schema == 5 else "after"] = after
    if schema in (2, 4):
        result["after_auxiliary"] = after_auxiliary
    if schema in (3, 4):
        result["after_bit1"] = after_bit1
    validate_contracts(changes, result)
    return result


def validate_contracts(changes, saved):
    """Pure validation of the exact saved projection before apply or Undo."""
    requested = selected(changes)
    if not requested and saved == {}:
        return
    if (
        not requested
        or not isinstance(saved, dict)
        or type(saved.get("schema")) is not int
        or saved["schema"] not in (1, 2, 3, 4, 5, 6, 7, 8, 9)
    ):
        raise GrpgError("Missing or malformed GRPG disable contract")
    keys = {"schema", "kind", "before_image_sha256", "before", "tables", "selected"}
    keys.add("after_profiles" if saved["schema"] == 5 else "after")
    if saved["schema"] in (2, 4):
        keys.add("after_auxiliary")
    if saved["schema"] in (3, 4):
        keys.add("after_bit1")
    if set(saved) != keys or saved["kind"] != "lpwr_grpg_disable":
        raise GrpgError("Unknown GRPG disable contract")
    digest = saved["before_image_sha256"]
    if not isinstance(digest, str) or not lp._HEX256.fullmatch(digest):
        raise GrpgError("Invalid GRPG before-image binding")
    loss = _aggregate_change(requested, saved["tables"])
    schema = saved["schema"]
    expected_schema = {8: 2, 9: 10}.get(schema)
    if (
        (expected_schema != loss if schema in (8, 9) else schema != _SCHEMAS[loss])
        or not isinstance(saved["before"], dict)
        or ("auxiliary" in saved["before"]) != (schema in (2, 4, 5, 6, 7, 8, 9))
        or ("bit1" in saved["before"]) != (schema in (3, 4, 5, 6, 7, 8, 9))
        or ("main" in saved["before"]) != (schema in (5, 6, 7, 8, 9))
    ):
        raise GrpgError("GRPG contract schema does not match its auxiliary scope")
    bindings, after, after_auxiliary, after_bit1 = _derive(requested, saved["before"], saved["tables"], full_scope=schema in (8, 9))
    if not _same_state(bindings, saved["selected"]) or not _same_state(
        after, saved["after_profiles" if schema == 5 else "after"]
    ):
        raise GrpgError("Saved GRPG projection differs from selected fields or BIOS model")
    if schema in (2, 4) and not _same_state(after_auxiliary, saved["after_auxiliary"]):
        raise GrpgError("Saved GRPG auxiliary projection differs from its consumed BIOS model")
    if schema in (3, 4) and not _same_state(after_bit1, saved["after_bit1"]):
        raise GrpgError("Saved GRPG bit1 projection differs from its consumed BIOS model")


def verify(observation, saved, restore=False):
    """Verify configured masks and routes; A7 evaluated clock bytes are diagnostic."""
    if saved == {}:
        return []
    if not isinstance(saved, dict) or not isinstance(saved.get("selected"), dict):
        raise GrpgError("Malformed GRPG disable contract")
    changes = dict.fromkeys(saved["selected"], False)
    validate_contracts(changes, saved)
    allow_missing = saved["schema"] in (6, 7, 8, 9)
    before = _observation(saved["before"], allow_missing_aux_feature=allow_missing)
    actual = _observation(observation, allow_missing_aux_feature=allow_missing)
    if not _same_state(actual["identity"], before["identity"]) or not _same_state(actual["pstates"], before["pstates"]):
        raise GrpgError("Loaded GRPG target or Pstate routing changed")
    if saved["schema"] in (5, 6, 7, 8, 9):
        keys = ("controllers", "auxiliary", "bit1", "main")
        outcomes = [before] if restore else (saved["after_profiles"] if saved["schema"] == 5 else [saved["after"]])
        if not any(all(_same_state(actual.get(key), expected[key]) for key in keys) for expected in outcomes):
            message = ("Loaded GRPG main dependency state differs from every qualified projection"
                       if saved["schema"] == 5 else "Loaded GRPG dependency readback differs from the qualified projection")
            raise GrpgError(message)
        return sorted(changes)
    expected = before["controllers"] if restore else saved["after"]
    if not _same_state(actual["controllers"], expected):
        raise GrpgError("Loaded GRPG support or configured masks differ from the expected state")
    if saved["schema"] in (2, 4):
        expected = before["auxiliary"] if restore else saved["after_auxiliary"]
        if not _same_state(actual.get("auxiliary"), expected):
            raise GrpgError("Loaded GRPG auxiliary support, feature or configured masks differ from the expected state")
    if saved["schema"] in (3, 4):
        expected = before["bit1"] if restore else saved["after_bit1"]
        if not _same_state(actual.get("bit1"), expected):
            raise GrpgError("Loaded GRPG bit1 role support or query witnesses differ from the expected state")
    return sorted(changes)
