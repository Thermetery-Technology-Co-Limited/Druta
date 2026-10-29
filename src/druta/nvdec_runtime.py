# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Qualified disable-only NVDEC configured-state contracts."""

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

KEY = re.compile(r"lpwr\.nvdec\.(0|[1-9][0-9]*)\.enabled\Z")


class NvdecError(RuntimeError):
    """An exact NVDEC projection cannot be established."""


def selected(changes):
    if not isinstance(changes, Mapping) or any(not isinstance(k, str) for k in changes):
        raise NvdecError("NVDEC changes require a field mapping")
    result = {}
    for key, value in changes.items():
        if not key.startswith("lpwr.nvdec."):
            continue
        match = KEY.fullmatch(key)
        if not match or int(match[1]) >= 5 or value is not False:
            raise NvdecError("Only understood enabled NVDEC rows may be disabled")
        result[key] = value
    if result and len(result) != len(changes):
        raise NvdecError("Apply NVDEC disables separately")
    return result


def read(gpu):
    return gr.read(gpu, main=True)


def _tables(image):
    parsed = vbios_fields._Image(image)
    tables = {}
    for name, ptr, owner, hs, stride in (("index", 0x74, 0x24, 6, 18), ("nvdec", 0x11C, 0xB4, 10, 2)):
        table = parsed.table("lpwr_" + name, "P", ptr, ((0x10, hs, stride),), owner)
        if table["object_id"] != owner or not 0 < table["count"] < 6:
            raise NvdecError("NVDEC table ownership or row count is not understood")
        tables[name] = {
            "offset": table["offset"],
            "owner": owner,
            "raw_hex": parsed.span(table["offset"], table["size"]).hex(),
        }
    at = parsed.pointer("P", 0)
    header = parsed.span(at, 14)
    if header[:2] != b"\x70\x0e" or not header[2] or not header[3]:
        raise NvdecError("NVDEC requires the populated PERF v70 aggregation path")
    stride = header[2] + header[4] * header[5]
    size = 14 + stride * header[3]
    if parsed.own(at, size, 0x0E) != 0x0E:
        raise NvdecError("PERF owner is not understood")
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
    if not isinstance(tables, dict) or set(tables) != {"index", "nvdec", "perf"}:
        raise NvdecError("Missing NVDEC table evidence")
    data, spans = {}, []
    for name, owner, hs, stride in (("index", 0x24, 6, 18), ("nvdec", 0xB4, 10, 2)):
        item = tables[name]
        if not isinstance(item, dict) or set(item) != {"offset", "owner", "raw_hex"} or item["owner"] != owner:
            raise NvdecError("Malformed NVDEC table evidence")
        at = lp._int(item["offset"], 0, 16 * 1024 * 1024)
        text = item["raw_hex"]
        if not isinstance(text, str) or not 2 * hs <= len(text) <= 2 * (hs + 5 * stride):
            raise NvdecError("Malformed NVDEC table bytes")
        try:
            raw = lp._hex(text, len(text) // 2)
        except (ValueError, lp.LpwrError) as exc:
            raise NvdecError("Malformed NVDEC table bytes") from exc
        if (
            len(raw) < hs
            or raw[:3] != bytes((0x10, hs, stride))
            or not 0 < raw[3] < 6
            or len(raw) != hs + stride * raw[3]
        ):
            raise NvdecError("NVDEC table geometry differs")
        data[name] = raw
        spans.append((at, at + len(raw)))
    perf = tables["perf"]
    if (
        not isinstance(perf, dict)
        or set(perf) != {"offset", "owner", "header_hex", "row_first_hex", "table_sha256"}
        or perf["owner"] != 0x0E
    ):
        raise NvdecError("Malformed PERF table evidence")
    at = lp._int(perf["offset"], 0, 16 * 1024 * 1024)
    header = lp._hex(perf["header_hex"], 14)
    if header[:2] != b"\x70\x0e" or not header[2] or not header[3]:
        raise NvdecError("Unsupported PERF table")
    first = lp._hex(perf["row_first_hex"], header[3])
    if (
        all(v == 255 for v in first)
        or not isinstance(perf["table_sha256"], str)
        or not lp._HEX256.fullmatch(perf["table_sha256"])
    ):
        raise NvdecError("Missing populated PERF evidence")
    spans.append((at, at + 14 + (header[2] + header[4] * header[5]) * header[3]))
    spans.sort()
    if any(end > 16 * 1024 * 1024 for _, end in spans) or any(
        a[1] > b[0] for a, b in zip(spans, spans[1:], strict=False)
    ):
        raise NvdecError("NVDEC table spans overlap")
    return data


def _derive(changes, before, tables):
    requested = selected(changes)
    data = _table_data(tables)
    core = gr._observation(before, allow_missing_aux_feature=True)
    if not {"main", "bit1", "auxiliary"} <= core.keys():
        raise NvdecError("NVDEC needs complete independent readback")
    ctr = core["controllers"]
    if not ctr["12"]["available"] or ctr["12"]["supported"] is not True or not ctr["12"]["states_available"]:
        raise NvdecError("NVDEC needs supported controller12 with an available mask")
    if not ctr["17"]["available"] or ctr["17"]["supported"] is not False:
        raise NvdecError("NVDEC requires independently unsupported controller17")
    if not core["main"]["features"]["12"]["available"]:
        raise NvdecError("NVDEC controller12 feature readback is unavailable")
    raw, index = data["nvdec"], data["index"]
    flags = [struct.unpack_from("<H", raw, 10 + 2 * i)[0] for i in range(raw[3])]
    bindings, disabled = {}, set()
    for key in requested:
        i = int(KEY.fullmatch(key)[1])
        if i >= len(flags) or flags[i] & 3 != 3:
            raise NvdecError("Selected NVDEC row must be an enabled controller12 contributor")
        disabled.add(i)
        bindings[key] = {
            "index": i,
            "offset": tables["nvdec"]["offset"] + 10 + 2 * i,
            "before": flags[i],
            "after": flags[i] & ~1,
        }
    if not disabled:
        raise NvdecError("No NVDEC row selected")
    remaining = [x for i, x in enumerate(flags) if i not in disabled and x & 1]
    old_union = new_union = 0
    for f in flags:
        if f & 1:
            old_union |= f
    for f in remaining:
        new_union |= f
    shutdown = not remaining
    if not shutdown and old_union != new_union:
        raise NvdecError("Partial NVDEC disable changes unresolved aggregate flags")
    roles = core["bit1"]["roles"]
    role4_enabled = any(f & 0x0B == 0x0B for f in flags)
    if roles["4"] != {"available": role4_enabled, "supported": True if role4_enabled else None}:
        raise NvdecError("NVDEC role4 support disagrees with the loaded row flags")
    expected = projected_mask = 0
    for row in core["pstates"]["rows"]:
        p = row["lpwr_index"]
        slot = 5
        if p < index[3]:
            slot = index[6 + p * 18 + 13]
            if slot >= 6:
                slot = 5
        ids = row["pstate_id_mask"]
        if slot < len(flags) and flags[slot] & 3 == 3:
            expected |= ids
            if slot not in disabled:
                projected_mask |= ids
    before_mask = ctr["12"]["configured_pstate_mask"]
    if before_mask != expected:
        raise NvdecError("NVDEC loaded mask disagrees with all routes")
    after = {k: copy.deepcopy(core[k]) for k in ("controllers", "auxiliary", "bit1", "main")}
    after["controllers"]["12"]["configured_pstate_mask"] = projected_mask
    if shutdown:
        if role4_enabled and not any(
            role != "4" and value == {"available": True, "supported": True} for role, value in roles.items()
        ):
            raise NvdecError("NVDEC type4 peer support is not independently witnessed")
        after["controllers"]["12"] = {
            "available": True,
            "supported": False,
            "states_available": False,
            "configured_pstate_mask": None,
        }
        after["main"]["features"]["12"] = {"available": False, "value": None}
        after["bit1"]["roles"]["4"] = {"available": False, "supported": None}
    if all(_same_state(after[k], core[k]) for k in ("controllers", "auxiliary", "bit1", "main")):
        raise NvdecError("NVDEC disable has no independently observable configured-state effect")
    return bindings, after


def contracts(gpu, image, changes):
    if not selected(changes):
        return {}
    tables = _tables(image)
    before, error = read(gpu)
    if error or before is None:
        raise NvdecError("NVDEC configuration is unavailable: " + str(error))
    bindings, after = _derive(changes, before, tables)
    result = {
        "schema": 1,
        "kind": "lpwr_nvdec_disable",
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
        or saved["kind"] != "lpwr_nvdec_disable"
        or not isinstance(saved["before_image_sha256"], str)
        or not lp._HEX256.fullmatch(saved["before_image_sha256"])
    ):
        raise NvdecError("Malformed NVDEC contract")
    bindings, after = _derive(request, saved["before"], saved["tables"])
    if not _same_state(bindings, saved["selected"]) or not _same_state(after, saved["after"]):
        raise NvdecError("Saved NVDEC projection differs from its inputs")


def verify(observation, saved, restore=False):
    if saved == {}:
        return []
    if not isinstance(saved, dict) or not isinstance(saved.get("selected"), dict):
        raise NvdecError("Malformed NVDEC contract")
    changes = dict.fromkeys(saved["selected"], False)
    validate_contracts(changes, saved)
    before = gr._observation(saved["before"], allow_missing_aux_feature=True)
    actual = gr._observation(observation, allow_missing_aux_feature=True)
    if not _same_state(actual["identity"], before["identity"]) or not _same_state(actual["pstates"], before["pstates"]):
        raise NvdecError("NVDEC target or Pstate routes changed")
    expected = before if restore else saved["after"]
    if any(not _same_state(actual[k], expected[k]) for k in ("controllers", "auxiliary", "bit1", "main")):
        raise NvdecError("NVDEC dependency readback differs from the qualified projection")
    return sorted(changes)
