# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Qualified, disable-only MS configured-state contracts."""

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

KEY = re.compile(r"lpwr\.ms\.(0|[1-9][0-9]*)\.enabled\Z")


class MsError(RuntimeError):
    """An exact MS projection cannot be established."""


def selected(changes):
    if not isinstance(changes, Mapping) or any(not isinstance(k, str) for k in changes):
        raise MsError("MS changes require a field mapping")
    result = {}
    for key, value in changes.items():
        if not key.startswith("lpwr.ms."):
            continue
        match = KEY.fullmatch(key)
        if not match or int(match[1]) >= 5 or value is not False:
            raise MsError("Only understood enabled MS rows may be disabled")
        result[key] = value
    if result and len(result) != len(changes):
        raise MsError("Apply MS disables separately")
    return result


def read(gpu):
    return gr.read(gpu, main=True)


def _tables(image):
    parsed = vbios_fields._Image(image)
    tables = {}
    for name, ptr, owner, hs, stride in (("index", 0x74, 0x24, 6, 18), ("ms", 0x84, 0x3A, 7, 16)):
        table = parsed.table("lpwr_" + name, "P", ptr, ((0x10, hs, stride),), owner)
        if table["object_id"] != owner or not 0 < table["count"] < 6:
            raise MsError("MS table ownership or row count is not understood")
        tables[name] = {
            "offset": table["offset"],
            "owner": owner,
            "raw_hex": parsed.span(table["offset"], table["size"]).hex(),
        }
    at = parsed.pointer("P", 0)
    header = parsed.span(at, 14)
    if header[:2] != b"\x70\x0e" or not header[2] or not header[3]:
        raise MsError("MS requires the populated PERF v70 aggregation path")
    stride = header[2] + header[4] * header[5]
    size = 14 + stride * header[3]
    if parsed.own(at, size, 0x0E) != 0x0E:
        raise MsError("PERF owner is not understood")
    raw = parsed.span(at, size)
    tables["perf"] = {
        "offset": at,
        "owner": 0x0E,
        "header_hex": header.hex(),
        "row_first_hex": bytes(raw[14 + i * stride] for i in range(header[3])).hex(),
        "table_sha256": hashlib.sha256(raw).hexdigest(),
    }
    _table_data(tables)
    return tables


def _table_data(tables):
    if not isinstance(tables, dict) or set(tables) != {"index", "ms", "perf"}:
        raise MsError("Missing MS table evidence")
    data, spans = {}, []
    for name, owner, hs, stride in (("index", 0x24, 6, 18), ("ms", 0x3A, 7, 16)):
        item = tables[name]
        if not isinstance(item, dict) or set(item) != {"offset", "owner", "raw_hex"} or item["owner"] != owner:
            raise MsError("Malformed MS table evidence")
        at = lp._int(item["offset"], 0, 16 * 1024 * 1024)
        text = item["raw_hex"]
        if not isinstance(text, str) or not 2 * hs <= len(text) <= 2 * (hs + 5 * stride):
            raise MsError("Malformed MS table bytes")
        try:
            raw = lp._hex(text, len(text) // 2)
        except (ValueError, lp.LpwrError) as exc:
            raise MsError("Malformed MS table bytes") from exc
        if (
            len(raw) < hs
            or raw[:3] != bytes((0x10, hs, stride))
            or not 0 < raw[3] < 6
            or len(raw) != hs + stride * raw[3]
        ):
            raise MsError("MS table geometry differs")
        data[name] = raw
        spans.append((at, at + len(raw)))
    perf = tables["perf"]
    if (
        not isinstance(perf, dict)
        or set(perf) != {"offset", "owner", "header_hex", "row_first_hex", "table_sha256"}
        or perf["owner"] != 0x0E
    ):
        raise MsError("Malformed PERF table evidence")
    at = lp._int(perf["offset"], 0, 16 * 1024 * 1024)
    header = lp._hex(perf["header_hex"], 14)
    if header[:2] != b"\x70\x0e" or not header[2] or not header[3]:
        raise MsError("Unsupported PERF table")
    first = lp._hex(perf["row_first_hex"], header[3])
    if (
        all(v == 255 for v in first)
        or not isinstance(perf["table_sha256"], str)
        or not lp._HEX256.fullmatch(perf["table_sha256"])
    ):
        raise MsError("Missing populated PERF evidence")
    spans.append((at, at + 14 + (header[2] + header[4] * header[5]) * header[3]))
    spans.sort()
    if any(end > 16 * 1024 * 1024 for _, end in spans) or any(
        a[1] > b[0] for a, b in zip(spans, spans[1:], strict=False)
    ):
        raise MsError("MS table spans overlap")
    return data


def _any(rows):
    if any(x["available"] and x["value"] == 1 for x in rows):
        return 1
    if all(x["available"] and x["value"] == 0 for x in rows):
        return 0
    raise MsError("Remaining auxiliary support is unknown")


def _derive(changes, before, tables):
    requested = selected(changes)
    data = _table_data(tables)
    core = gr._observation(before, allow_missing_aux_feature=True)
    if not {"main", "bit1", "auxiliary"} <= core.keys():
        raise MsError("MS needs complete independent readback")
    ctr = core["controllers"]
    for i in (6, 16):
        if not ctr[str(i)]["available"] or ctr[str(i)]["supported"] is not False:
            raise MsError("MS requires independently unsupported controllers 6 and 16")
    if not ctr["4"]["available"] or ctr["4"]["supported"] is not True or not ctr["4"]["states_available"]:
        raise MsError("MS controller 4 support and mask are required")
    raw, index = data["ms"], data["index"]
    flags = [struct.unpack_from("<I", raw, 7 + 16 * i)[0] for i in range(raw[3])]
    bindings, disabled = {}, set()
    for key in requested:
        i = int(KEY.fullmatch(key)[1])
        if i >= len(flags) or not flags[i] & 1:
            raise MsError("Selected MS row is not enabled")
        disabled.add(i)
        bindings[key] = {
            "index": i,
            "offset": tables["ms"]["offset"] + 7 + 16 * i,
            "before": flags[i],
            "after": flags[i] & ~1,
        }
    if not disabled:
        raise MsError("No MS row selected")
    remaining = [x for i, x in enumerate(flags) if i not in disabled and x & 1]
    old_union = 0
    new_union = 0
    for f in flags:
        if f & 1:
            old_union |= f
    for f in remaining:
        new_union |= f
    shutdown = not remaining
    if not shutdown and old_union != new_union:
        raise MsError("Partial MS disable changes unresolved aggregate flags")
    routes = []
    for row in core["pstates"]["rows"]:
        p = row["lpwr_index"]
        slot = 5
        if p < 6:
            if p >= index[3]:
                raise MsError("Pstate selects an unloaded LPWR index")
            slot = index[6 + p * 18 + 2]
            if slot >= 6:
                slot = 5
        routes.append((slot, row["pstate_id_mask"]))

    def project_mask(before_mask, bit):
        selected_ids = other_ids = known_ids = expected = 0
        for slot, ids in routes:
            if slot < len(flags):
                known_ids |= ids
                if flags[slot] & 1 and (bit is None or flags[slot] & bit):
                    expected |= ids
                    if slot in disabled:
                        selected_ids |= ids
                    else:
                        other_ids |= ids
            else:
                other_ids |= ids
        if before_mask & known_ids != expected:
            raise MsError("MS loaded mask disagrees with known routes")
        return before_mask & ~(selected_ids & ~other_ids)

    after = {k: copy.deepcopy(core[k]) for k in ("controllers", "auxiliary", "bit1", "main")}
    for i in ("1", "4"):
        c = ctr[i]
        if c["available"] and c["supported"] is True:
            if not c["states_available"]:
                raise MsError("MS controller mask is unavailable")
            after["controllers"][i]["configured_pstate_mask"] = project_mask(c["configured_pstate_mask"], None)
    group = core["auxiliary"]["group"]
    seven = core["main"]["bit4_group"]
    for src, key, bit, dst in (
        (group, "806:1", 0x10, after["auxiliary"]["group"]),
        (seven, "805:1", 0x20, after["main"]["bit4_group"]),
    ):
        if src[key]["available"]:
            dst[key]["value"] = project_mask(src[key]["value"], bit)
    if shutdown:
        after["controllers"]["4"] = {
            "available": True,
            "supported": False,
            "states_available": False,
            "configured_pstate_mask": None,
        }
        after["main"]["features"]["4"] = {"available": False, "value": None}
        if ctr["1"]["supported"] is True:
            after["controllers"]["1"]["configured_pstate_mask"] = 0
        if after["bit1"]["roles"]["2"]["available"]:
            after["bit1"]["roles"]["2"] = {"available": False, "supported": None}
        if group["800:1"]["available"]:
            after["auxiliary"]["group"]["800:1"]["value"] = 0
        if group["806:1"]["available"]:
            after["auxiliary"]["group"]["806:1"]["value"] = 0
        if group["1:0"]["available"]:
            after["auxiliary"]["group"]["1:0"]["value"] = _any(
                [after["auxiliary"]["group"][f"800:{i}"] for i in range(4)]
            )
        out = after["main"]["bit4_group"]
        if not seven["801:1"]["available"]:
            raise MsError("MS type7 role support is unknown")
        out["801:1"]["value"] = 0
        alive = _any([out[f"801:{i}"] for i in range(3)])
        if alive:
            if out["805:1"]["available"]:
                out["805:1"]["value"] = 0
            if out["1:0"]["available"]:
                out["1:0"]["value"] = 1
        else:
            for key in out:
                out[key] = {"available": False, "value": None}
    if all(_same_state(after[k], core[k]) for k in ("controllers", "auxiliary", "bit1", "main")):
        raise MsError("MS disable has no independently observable configured-state effect")
    return bindings, after


def contracts(gpu, image, changes):
    if not selected(changes):
        return {}
    tables = _tables(image)
    before, error = read(gpu)
    if error or before is None:
        raise MsError("MS configuration is unavailable: " + str(error))
    bindings, after = _derive(changes, before, tables)
    result = {
        "schema": 1,
        "kind": "lpwr_ms_disable",
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
        or set(saved) != {"schema", "kind", "before_image_sha256", "tables", "before", "selected", "after"}
        or type(saved["schema"]) is not int
        or saved["schema"] != 1
        or saved["kind"] != "lpwr_ms_disable"
        or not isinstance(saved["before_image_sha256"], str)
        or not lp._HEX256.fullmatch(saved["before_image_sha256"])
    ):
        raise MsError("Malformed MS contract")
    bindings, after = _derive(request, saved["before"], saved["tables"])
    if not _same_state(bindings, saved["selected"]) or not _same_state(after, saved["after"]):
        raise MsError("Saved MS projection differs from its inputs")


def verify(observation, saved, restore=False):
    if saved == {}:
        return []
    if not isinstance(saved, dict) or not isinstance(saved.get("selected"), dict):
        raise MsError("Malformed MS contract")
    changes = dict.fromkeys(saved["selected"], False)
    validate_contracts(changes, saved)
    before = gr._observation(saved["before"], allow_missing_aux_feature=True)
    actual = gr._observation(observation, allow_missing_aux_feature=True)
    if not _same_state(actual["identity"], before["identity"]) or not _same_state(actual["pstates"], before["pstates"]):
        raise MsError("MS target or Pstate routes changed")
    expected = before if restore else saved["after"]
    if any(not _same_state(actual[k], expected[k]) for k in ("controllers", "auxiliary", "bit1", "main")):
        raise MsError("MS dependency readback differs from the qualified projection")
    return sorted(changes)
