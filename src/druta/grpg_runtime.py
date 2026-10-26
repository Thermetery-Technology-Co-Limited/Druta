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
from collections.abc import Mapping

from . import lpwr_runtime as lp
from . import vbios_fields
from .clock_vf_registry import _identity
from .perf_diagnostics import PerfDiagnosticError, PerfDiagnosticsReader
from .registry_flags import _same_state

KEY = re.compile(r"lpwr\.gr_pg\.(0|[1-9][0-9]*)\.enabled\Z")
CONTROLLERS = tuple(range(22))
_TABLES = (("index", 0x74, 0x24, 6, 18), ("grpg", 0x80, 0x39, 10, 5))


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


def _observation(value):
    if not isinstance(value, dict) or set(value) != lp._OBS_KEYS:
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
    return {"identity": identity, "pstates": pstates, "controllers": controllers}


def _once(reader, identity):
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
    _observation(result)
    return result


def read(gpu):
    """Read all known controller support/masks; unavailable peers stay diagnostic."""
    reader = PerfDiagnosticsReader(gpu)
    try:
        with gpu._lock:
            identity = lp.clock._binding(reader)
            for attempt in range(2):
                try:
                    first, second = _once(reader, identity), _once(reader, identity)
                    if not _same_state(_observation(first), _observation(second)):
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


def _derive(changes, before, tables):
    requested = selected(changes)
    core, data = _observation(before), _table_data(tables)
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
    after_flags = [value & ~1 if i in disabled else value for i, value in enumerate(flags)]
    if _aggregate(flags) != _aggregate(after_flags):
        raise GrpgError("GRPG disable would remove the last enabled entry or an auxiliary capability")
    routed = dict.fromkeys(disabled, 0)
    untouched = 0
    for row in core["pstates"]["rows"]:
        selector = row["lpwr_index"]
        slot = 5
        if selector < 6:
            if selector >= index[3]:
                raise GrpgError("A valid LPWR selector addresses an unloaded index row")
            slot = index[6 + 18 * selector + 1]
            slot = slot if slot < 6 else 5
        if slot < len(flags):
            mask = row["pstate_id_mask"]
            expected = mask if flags[slot] & 1 else 0
            if live_mask & mask != expected:
                raise GrpgError("Loaded GRPG membership differs from a consumed BIOS entry")
        if slot in disabled:
            routed[slot] |= row["pstate_id_mask"]
        else:
            untouched |= row["pstate_id_mask"]
    removed = 0
    for mask in routed.values():
        if not mask or live_mask & mask != mask:
            raise GrpgError("Every selected GRPG entry must have an active configured Pstate route")
        removed |= mask
    if removed & untouched:
        raise GrpgError("Selected GRPG routes overlap untouched Pstate membership")
    after = copy.deepcopy(core["controllers"])
    after["0"]["configured_pstate_mask"] = live_mask & ~removed
    return bindings, after


def contracts(gpu, image_bytes, changes):
    """Capture a qualified GRPG disable; no SET, registry or restart operations."""
    if not selected(changes):
        return {}
    tables = _tables(image_bytes)
    before, error = read(gpu)
    if error or before is None:
        raise GrpgError("GRPG configuration is unavailable: " + str(error))
    bindings, after = _derive(changes, before, tables)
    return {
        "schema": 1,
        "kind": "lpwr_grpg_disable",
        "before_image_sha256": hashlib.sha256(image_bytes).hexdigest(),
        "before": copy.deepcopy(before),
        "tables": tables,
        "selected": bindings,
        "after": after,
    }


def validate_contracts(changes, saved):
    """Pure validation of the exact saved projection before apply or Undo."""
    requested = selected(changes)
    if not requested and saved == {}:
        return
    if (
        not requested
        or not isinstance(saved, dict)
        or set(saved) != {"schema", "kind", "before_image_sha256", "before", "tables", "selected", "after"}
    ):
        raise GrpgError("Missing or malformed GRPG disable contract")
    if type(saved["schema"]) is not int or saved["schema"] != 1 or saved["kind"] != "lpwr_grpg_disable":
        raise GrpgError("Unknown GRPG disable contract")
    digest = saved["before_image_sha256"]
    if not isinstance(digest, str) or not lp._HEX256.fullmatch(digest):
        raise GrpgError("Invalid GRPG before-image binding")
    bindings, after = _derive(requested, saved["before"], saved["tables"])
    if not _same_state(bindings, saved["selected"]) or not _same_state(after, saved["after"]):
        raise GrpgError("Saved GRPG projection differs from selected fields or BIOS model")


def verify(observation, saved, restore=False):
    """Verify configured masks and routes; A7 evaluated clock bytes are diagnostic."""
    if saved == {}:
        return []
    if not isinstance(saved, dict) or not isinstance(saved.get("selected"), dict):
        raise GrpgError("Malformed GRPG disable contract")
    changes = dict.fromkeys(saved["selected"], False)
    validate_contracts(changes, saved)
    before, actual = _observation(saved["before"]), _observation(observation)
    if not _same_state(actual["identity"], before["identity"]) or not _same_state(actual["pstates"], before["pstates"]):
        raise GrpgError("Loaded GRPG target or Pstate routing changed")
    expected = before["controllers"] if restore else saved["after"]
    if not _same_state(actual["controllers"], expected):
        raise GrpgError("Loaded GRPG support or configured masks differ from the expected state")
    return sorted(changes)
