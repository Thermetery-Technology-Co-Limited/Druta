# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Disable-only contracts for independently understood media controllers."""

from __future__ import annotations

import copy
import hashlib
import re
import struct
from collections.abc import Mapping

from . import grpg_runtime as gr
from . import lpwr_runtime as lp
from . import vbios_fields
from .registry_flags import _same_state

KEY = re.compile(r"lpwr\.(nvenc|ofa|nvjpg)\.(0|[1-9][0-9]*)\.enabled\Z")
# pointer, object, controller, unsupported alternate, role, LPWR-index byte, enable, role flag
_PROFILES = {
    "nvenc": (0x120, 0xB5, 13, 18, 5, 14, 1, 8),
    "ofa": (0x124, 0xB6, 14, 19, 7, 15, 1, 8),
    "nvjpg": (0x128, 0xB7, 15, None, 6, 16, 2, 4),
}


class MediaLpwrError(RuntimeError):
    """An exact Media LPWR projection cannot be established."""


def selected(changes):
    if not isinstance(changes, Mapping) or any(not isinstance(k, str) for k in changes):
        raise MediaLpwrError("Media LPWR changes require a field mapping")
    result = {}
    for key, value in changes.items():
        if not any(key.startswith("lpwr." + family + ".") for family in _PROFILES):
            continue
        match = KEY.fullmatch(key)
        if not match or int(match[2]) >= 5 or value is not False:
            raise MediaLpwrError("Only understood enabled Media LPWR rows may be disabled")
        result[key] = value
    if result and (len(result) != len(changes) or len({KEY.fullmatch(k)[1] for k in result}) != 1):
        raise MediaLpwrError("Apply Media LPWR disables separately")
    return result


def read(gpu):
    return gr.read(gpu, main=True)


def _tables(image, family):
    parsed = vbios_fields._Image(image)
    tables = {}
    for name, ptr, owner, hs, stride in (
        ("index", 0x74, 0x24, 6, 18),
        (family, _PROFILES[family][0], _PROFILES[family][1], 10, 2),
    ):
        table = parsed.table("lpwr_" + name, "P", ptr, ((0x10, hs, stride),), owner)
        if table["object_id"] != owner or not 0 < table["count"] < 6:
            raise MediaLpwrError("Media LPWR table ownership or row count is not understood")
        tables[name] = {
            "offset": table["offset"],
            "owner": owner,
            "raw_hex": parsed.span(table["offset"], table["size"]).hex(),
        }
    at = parsed.pointer("P", 0)
    header = parsed.span(at, 14)
    if header[:2] != b"\x70\x0e" or not header[2] or not header[3]:
        raise MediaLpwrError("Media LPWR requires the populated PERF v70 aggregation path")
    stride = header[2] + header[4] * header[5]
    size = 14 + stride * header[3]
    if parsed.own(at, size, 0x0E) != 0x0E:
        raise MediaLpwrError("PERF owner is not understood")
    raw = parsed.span(at, size)
    tables["perf"] = {
        "offset": at,
        "owner": 0x0E,
        "header_hex": header.hex(),
        "row_first_hex": bytes(raw[14 + i * stride] for i in range(header[3])).hex(),
        "table_sha256": hashlib.sha256(raw).hexdigest(),
    }
    _table_data(tables, family)
    return tables


def _table_data(tables, family):
    if not isinstance(tables, dict) or set(tables) != {"index", family, "perf"}:
        raise MediaLpwrError("Missing Media LPWR table evidence")
    data, spans = {}, []
    for name, owner, hs, stride in (("index", 0x24, 6, 18), (family, _PROFILES[family][1], 10, 2)):
        item = tables[name]
        if not isinstance(item, dict) or set(item) != {"offset", "owner", "raw_hex"} or item["owner"] != owner:
            raise MediaLpwrError("Malformed Media LPWR table evidence")
        at = lp._int(item["offset"], 0, 16 * 1024 * 1024)
        text = item["raw_hex"]
        if not isinstance(text, str) or not 2 * hs <= len(text) <= 2 * (hs + 5 * stride):
            raise MediaLpwrError("Malformed Media LPWR table bytes")
        try:
            raw = lp._hex(text, len(text) // 2)
        except (ValueError, lp.LpwrError) as exc:
            raise MediaLpwrError("Malformed Media LPWR table bytes") from exc
        if (
            len(raw) < hs
            or raw[:3] != bytes((0x10, hs, stride))
            or not 0 < raw[3] < 6
            or len(raw) != hs + stride * raw[3]
        ):
            raise MediaLpwrError("Media LPWR table geometry differs")
        data[name] = raw
        spans.append((at, at + len(raw)))
    perf = tables["perf"]
    if (
        not isinstance(perf, dict)
        or set(perf) != {"offset", "owner", "header_hex", "row_first_hex", "table_sha256"}
        or perf["owner"] != 0x0E
    ):
        raise MediaLpwrError("Malformed PERF table evidence")
    at = lp._int(perf["offset"], 0, 16 * 1024 * 1024)
    header = lp._hex(perf["header_hex"], 14)
    if header[:2] != b"\x70\x0e" or not header[2] or not header[3]:
        raise MediaLpwrError("Unsupported PERF table")
    first = lp._hex(perf["row_first_hex"], header[3])
    if (
        all(v == 255 for v in first)
        or not isinstance(perf["table_sha256"], str)
        or not lp._HEX256.fullmatch(perf["table_sha256"])
    ):
        raise MediaLpwrError("Missing populated PERF evidence")
    spans.append((at, at + 14 + (header[2] + header[4] * header[5]) * header[3]))
    spans.sort()
    if any(end > 16 * 1024 * 1024 for _, end in spans) or any(
        a[1] > b[0] for a, b in zip(spans, spans[1:], strict=False)
    ):
        raise MediaLpwrError("Media LPWR table spans overlap")
    return data


def _derive(changes, before, tables):
    requested = selected(changes)
    family = KEY.fullmatch(next(iter(requested)))[1]
    _, _, controller, alternate, role, route_byte, enable, role_flag = _PROFILES[family]
    data = _table_data(tables, family)
    core = gr._observation(before, allow_missing_aux_feature=True)
    if not {"main", "bit1", "auxiliary"} <= core.keys():
        raise MediaLpwrError("Media LPWR needs complete independent readback")
    ctr = core["controllers"]
    selected_controller = ctr[str(controller)]
    if (
        not selected_controller["available"]
        or selected_controller["supported"] is not True
        or not selected_controller["states_available"]
    ):
        raise MediaLpwrError("Media controller support and configured mask are required")
    if alternate is not None and (
        not ctr[str(alternate)]["available"] or ctr[str(alternate)]["supported"] is not False
    ):
        raise MediaLpwrError("Alternate media controller must be independently unsupported")
    if not core["main"]["features"][str(controller)]["available"]:
        raise MediaLpwrError("Media controller feature readback is unavailable")
    raw, index = data[family], data["index"]
    flags = [struct.unpack_from("<H", raw, 10 + 2 * i)[0] for i in range(raw[3])]

    def contributes(f):
        return bool(f & enable) if family == "nvjpg" else f & 3 == 3

    bindings, disabled = {}, set()
    for key in requested:
        i = int(KEY.fullmatch(key)[2])
        if i >= len(flags) or not contributes(flags[i]):
            raise MediaLpwrError("Selected row is not an enabled media contributor")
        disabled.add(i)
        bindings[key] = {
            "index": i,
            "offset": tables[family]["offset"] + 10 + 2 * i,
            "before": flags[i],
            "after": flags[i] & ~enable,
        }
    if not disabled:
        raise MediaLpwrError("No media row selected")
    # Unknown flags are preserved; partial removal is qualified only if their
    # aggregate among enabled rows is unchanged.
    old_union = new_union = 0
    for i, f in enumerate(flags):
        if f & enable:
            old_union |= f
            if i not in disabled:
                new_union |= f
    shutdown = not any(f & enable for i, f in enumerate(flags) if i not in disabled)
    if not shutdown and old_union != new_union:
        raise MediaLpwrError("Partial media disable changes unresolved aggregate flags")
    roles = core["bit1"]["roles"]
    role_enabled = any(contributes(f) and f & role_flag for f in flags)
    if roles[str(role)] != {"available": bool(role_enabled), "supported": True if role_enabled else None}:
        raise MediaLpwrError("Media role support disagrees with loaded row flags")
    before_mask = after_mask = 0
    for row in core["pstates"]["rows"]:
        p = row["lpwr_index"]
        slot = 5
        if p < index[3]:
            slot = index[6 + p * 18 + route_byte]
            if slot >= 6:
                slot = 5
        if slot < len(flags) and contributes(flags[slot]):
            before_mask |= row["pstate_id_mask"]
            if slot not in disabled:
                after_mask |= row["pstate_id_mask"]
    if selected_controller["configured_pstate_mask"] != before_mask:
        raise MediaLpwrError("Media configured mask disagrees with all routes")
    after = {k: copy.deepcopy(core[k]) for k in ("controllers", "auxiliary", "bit1", "main")}
    after["controllers"][str(controller)]["configured_pstate_mask"] = after_mask
    if shutdown:
        if role_enabled:
            try:
                after["bit1"] = gr.project_type4_role(core["bit1"], role, allow_no_peer=True)
            except gr.GrpgError as exc:
                raise MediaLpwrError(str(exc)) from exc
        after["controllers"][str(controller)] = {
            "available": True,
            "supported": False,
            "states_available": False,
            "configured_pstate_mask": None,
        }
        after["main"]["features"][str(controller)] = {"available": False, "value": None}
    if all(_same_state(after[k], core[k]) for k in ("controllers", "auxiliary", "bit1", "main")):
        raise MediaLpwrError("Media disable has no independently observable configured-state effect")
    return bindings, after


def contracts(gpu, image, changes):
    if not selected(changes):
        return {}
    family = KEY.fullmatch(next(iter(selected(changes))))[1]
    tables = _tables(image, family)
    before, error = read(gpu)
    if error or before is None:
        raise MediaLpwrError("Media LPWR configuration is unavailable: " + str(error))
    bindings, after = _derive(changes, before, tables)
    result = {
        "schema": 1,
        "kind": "lpwr_media_disable",
        "family": family,
        "before_image_sha256": hashlib.sha256(image).hexdigest(),
        "tables": tables,
        "before": copy.deepcopy(before),
        "selected": bindings,
        "after": after,
    }
    validate_contracts(changes, result)
    return result


def validate_contracts(changes, saved):
    request = selected(changes)
    if not request and saved == {}:
        return
    if (
        not request
        or not isinstance(saved, dict)
        or set(saved) != {"schema", "kind", "family", "before_image_sha256", "tables", "before", "selected", "after"}
        or type(saved["schema"]) is not int
        or saved["schema"] != 1
        or saved["kind"] != "lpwr_media_disable"
        or saved["family"] != KEY.fullmatch(next(iter(request)))[1]
        or not isinstance(saved["before_image_sha256"], str)
        or not lp._HEX256.fullmatch(saved["before_image_sha256"])
    ):
        raise MediaLpwrError("Malformed Media LPWR contract")
    bindings, after = _derive(request, saved["before"], saved["tables"])
    if not _same_state(bindings, saved["selected"]) or not _same_state(after, saved["after"]):
        raise MediaLpwrError("Saved Media LPWR projection differs from its inputs")


def verify(observation, saved, restore=False):
    if saved == {}:
        return []
    if not isinstance(saved, dict) or not isinstance(saved.get("selected"), dict):
        raise MediaLpwrError("Malformed Media LPWR contract")
    changes = dict.fromkeys(saved["selected"], False)
    validate_contracts(changes, saved)
    before = gr._observation(saved["before"], allow_missing_aux_feature=True)
    actual = gr._observation(observation, allow_missing_aux_feature=True)
    if not _same_state(actual["identity"], before["identity"]) or not _same_state(actual["pstates"], before["pstates"]):
        raise MediaLpwrError("Media LPWR target or Pstate routes changed")
    expected = before if restore else saved["after"]
    if any(not _same_state(actual[k], expected[k]) for k in ("controllers", "auxiliary", "bit1", "main")):
        raise MediaLpwrError("Media LPWR dependency readback differs from the qualified projection")
    return sorted(changes)
