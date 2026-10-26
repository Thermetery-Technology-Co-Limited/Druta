# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure effective model-input contracts; no registry or hardware operations.

IDDQ values remain raw model integers. These contracts require exact evaluated
readback as well as the stored override; they do not assign physical units or
assume where substitution occurs in firmware.
"""

from __future__ import annotations

import copy
import math
import re
from collections.abc import Mapping
from dataclasses import fields

from .registry_flags import _same_state
from .vfe_inputs import _FUSE_SELECTORS, MODERN_SENSED_FUSE, PROBE_ARCHITECTURES, VfeInput, VfeVariable

SPEEDO_NAMES = frozenset(name for name, _, kind in _FUSE_SELECTORS.values() if kind == "speedo")
IDDQ_NAMES = frozenset(name for name, _, kind in _FUSE_SELECTORS.values() if kind == "iddq")
NAMES = SPEEDO_NAMES | IDDQ_NAMES
_ROW_FIELDS = {field.name for field in fields(VfeInput)}
_VARIABLE_FIELDS = {field.name for field in fields(VfeVariable)}
_BOOL_FIELDS = {"signed", "raw_hardware_signed", "default_signed", "override_present", "cache_ready"}
_STATE_FIELDS = {"slot", "architecture", "inputs", "variables", "control_sha256", "model_static_sha256"}
_FLOAT32_MAX = float.fromhex("0x1.fffffep127")


class ModelRegistryError(ValueError):
    pass


def _fail(message):
    raise ModelRegistryError(message)


def _integer(value, minimum, maximum):
    return type(value) is int and minimum <= value <= maximum


def _range_number(value):
    # Comparing a huge Python int is bounded and does not convert it to float.
    # math.isfinite alone would raise OverflowError on a malformed receipt.
    return type(value) in (int, float) and -_FLOAT32_MAX <= value <= _FLOAT32_MAX and math.isfinite(value)


def _variable(row):
    if not isinstance(row, dict) or set(row) != _VARIABLE_FIELDS:
        _fail("Incomplete captured model-variable metadata")
    if (
        not _integer(row["index"], 0, 254)
        or not _integer(row["type"], 0, 0xFFFFFFFF)
        or not _integer(row["control_mode"], 0, 255)
    ):
        _fail("Captured model-variable encoding is malformed")
    if any(value is not None and not _range_number(value) for value in (row["minimum"], row["maximum"])):
        _fail("Captured model-variable range is malformed")
    # Unknown or unusable variables remain descriptive peer metadata. A
    # reversed range on such a row must not suppress a different valid rail;
    # selected typed inputs undergo the stricter _row range checks below.
    words = row["control_words"]
    if (
        not isinstance(words, (list, tuple))
        or len(words) != 2
        or any(not _integer(word, 0, 0xFFFFFFFF) for word in words)
    ):
        _fail("Captured model-variable control words are malformed")
    return dict(copy.deepcopy(row), control_words=list(words))


def _row(row):
    if not isinstance(row, dict) or set(row) != _ROW_FIELDS:
        _fail("Incomplete captured model-input metadata")
    if any(type(row[name]) is not bool for name in _BOOL_FIELDS):
        _fail("Model-input Boolean metadata is malformed")
    if not _integer(row["index"], 0, 254):
        _fail("Model-input index is outside the understood inventory")
    if any(
        not _integer(row[k], 0, 255)
        for k in ("fuse_id", "fuse_version_field_id", "fuse_version", "expected_fuse_version")
    ):
        _fail("Model-input fuse identity is malformed")
    selector = (row["fuse_id"], row["fuse_version_field_id"])
    descriptor = _FUSE_SELECTORS.get(selector)
    if descriptor is None or (row["registry_name"], row["label"], row["kind"]) != descriptor:
        _fail("Model-input identity does not match its typed fuse selector")
    for name, signed in (("active", "signed"), ("raw_hardware", "raw_hardware_signed"), ("default", "default_signed")):
        low, high = (-(1 << 31), (1 << 31) - 1) if row[signed] else (0, 0xFFFFFFFF)
        if not _integer(row[name], low, high):
            _fail("Model-input integer encoding is malformed")
    if any(not _integer(row[k], 0, 0xFFFFFFFF) for k in ("override", "hardware_scale_q12")) or not _integer(
        row["hardware_offset"], -(1 << 31), (1 << 31) - 1
    ):
        _fail("Model-input correction or override encoding is malformed")
    if any(not _range_number(row[k]) for k in ("minimum", "maximum")) or row["minimum"] > row["maximum"]:
        _fail("Model-input range is malformed")
    return copy.deepcopy(row)


def _state(value):
    if not isinstance(value, dict) or set(value) != _STATE_FIELDS:
        _fail("Incomplete captured model-input state")
    from .nvbackend import parse_slot

    address = parse_slot(value["slot"]) if isinstance(value["slot"], str) else None
    if address is None or address[0] != 0 or address[1] > 255 or address[2] > 31 or address[3] > 7:
        _fail("Captured model-input PCI identity is malformed")
    if type(value["architecture"]) is not int or value["architecture"] not in PROBE_ARCHITECTURES:
        _fail("Captured model-input architecture is unsupported")
    for key in ("control_sha256", "model_static_sha256"):
        if not isinstance(value[key], str) or not re.fullmatch(r"[0-9a-f]{64}", value[key]):
            _fail("Captured model-input static/control fingerprint is missing or malformed")
    if not isinstance(value["inputs"], list) or not 1 <= len(value["inputs"]) <= 255:
        _fail("Captured model inputs are unavailable")
    rows = [_row(row) for row in value["inputs"]]
    indices = [row["index"] for row in rows]
    if indices != sorted(set(indices)):
        _fail("Captured model-input indices are duplicated or unordered")
    if len({row["registry_name"] for row in rows}) != len(rows):
        _fail("Captured model-input selectors are duplicated")
    if not isinstance(value["variables"], list) or not 1 <= len(value["variables"]) <= 255:
        _fail("Captured model-variable inventory is unavailable")
    variables = [_variable(row) for row in value["variables"]]
    indices = [row["index"] for row in variables]
    if indices != sorted(set(indices)):
        _fail("Captured model-variable indices are duplicated or unordered")
    by_index = {row["index"]: row for row in variables}
    for row in rows:
        variable = by_index.get(row["index"])
        if (
            variable is None
            or variable["type"] != MODERN_SENSED_FUSE
            or not _same_state(variable["minimum"], row["minimum"])
            or not _same_state(variable["maximum"], row["maximum"])
        ):
            _fail("Typed model input disagrees with its variable inventory")
    return {**copy.deepcopy(value), "inputs": rows, "variables": variables}


def observation(vfe):
    if not isinstance(vfe, Mapping):
        _fail("Fresh model-input readback is unavailable")
    inputs, variables = vfe.get("inputs"), vfe.get("variables")
    if (
        not isinstance(inputs, (list, tuple))
        or not 1 <= len(inputs) <= 255
        or not isinstance(variables, (list, tuple))
        or not 1 <= len(variables) <= 255
    ):
        _fail("Fresh model-input inventory is malformed")
    # Validate indices before sorting: malformed mixed types must raise a
    # domain error, not leak a TypeError from the sorting implementation.
    inputs, variables = [_row(row) for row in inputs], [_variable(row) for row in variables]
    return _state(
        {
            "slot": vfe.get("slot"),
            "architecture": vfe.get("architecture"),
            "inputs": sorted(inputs, key=lambda row: row["index"]),
            "variables": sorted(variables, key=lambda row: row["index"]),
            "control_sha256": vfe.get("control_sha256"),
            "model_static_sha256": vfe.get("model_static_sha256"),
        }
    )


def _changes(changes):
    if not isinstance(changes, Mapping) or any(name not in NAMES for name in changes):
        _fail("Only understood direct model-input substitutions have effective contracts")
    if any(not _integer(value, 1, 0xFFFFFFFF) for value in changes.values()):
        _fail("Model-input requests must be positive unsigned DWORD integers")
    return dict(sorted(changes.items()))


def _after(before, changes):
    changes = _changes(changes)
    if not changes:
        _fail("Only understood direct model-input substitutions have effective contracts")
    result = copy.deepcopy(before)
    for name, value in changes.items():
        matches = [row for row in result["inputs"] if row["registry_name"] == name]
        if (
            len(matches) != 1
            or not matches[0]["cache_ready"]
            or any(matches[0][key] for key in ("signed", "raw_hardware_signed", "default_signed"))
        ):
            _fail(name + ": a unique, ready unsigned model input is required")
        row = matches[0]
        variable = next(item for item in result["variables"] if item["index"] == row["index"])
        if variable["control_mode"] != 0 or variable["control_words"] != [0, 0]:
            _fail(name + ": a runtime control override is already present")
        if not row["minimum"] <= row["active"] <= row["maximum"]:
            _fail(name + ": current active value is outside the reported range")
        if row["override_present"] and (row["override"] == 0 or row["active"] != row["override"]):
            _fail(name + ": stored and evaluated overrides disagree; reconcile pending model settings first")
        if not _integer(value, 1, 0xFFFFFFFF) or not row["minimum"] <= value <= row["maximum"]:
            _fail(name + ": request must be a positive integer within the reported range")
        row.update(active=value, override=value, override_present=True)
    return result


def contracts(vfe, changes):
    changes = _changes(changes)
    if not changes:
        return {}
    before = observation(vfe)
    return {"schema": 1, "selected": changes, "before": before, "after": _after(before, changes)}


def validate_contracts(changes, saved):
    """Validate expected effects before any registry operation, including undo."""
    changes = _changes(changes)
    if not changes and type(saved) is dict and saved == {}:
        return
    if (
        not isinstance(saved, dict)
        or set(saved) != {"schema", "selected", "before", "after"}
        or not _same_state(saved["schema"], 1)
        or not isinstance(saved["selected"], dict)
    ):
        _fail("Malformed effective model-input contract")
    if not changes or not _same_state(changes, saved["selected"]):
        _fail("Saved model-input selections disagree with the requested changes")
    before = _state(saved["before"])
    after = _state(saved["after"])
    if not _same_state(_after(before, changes), after):
        _fail("Saved model-input request changes unselected metadata")


def verify(vfe, saved, *, restore=False):
    if type(restore) is not bool:
        _fail("Malformed model-input verification phase")
    if type(saved) is dict and saved == {}:
        return []
    if not isinstance(saved, dict):
        _fail("Malformed effective model-input contract")
    validate_contracts(saved.get("selected"), saved)
    expected = saved["before" if restore else "after"]
    if not _same_state(observation(vfe), expected):
        _fail("Evaluated model input, stored override or preserved peer metadata differs")
    return list(saved["selected"])
