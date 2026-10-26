# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Exact loaded REL bounds with separately observed firmware limits.

The loaded clamp is an equation definition, not a requested rail voltage. A
polynomial below its clamp can leave REL unchanged. Chaining, offsets and VID
quantization also prevent deriving the reported REL from the clamp alone.
Fixing both root bounds preserves its polynomial and next-chain definitions;
it does not promise an exact final chained limit or physical rail voltage.
"""

import copy
import re
from collections.abc import Mapping

from . import vfe_equation_runtime as equations
from . import voltage_ceiling_runtime as ceiling
from .registry_flags import _same_state

KEY = re.compile(r"voltage\.([01])\.REL\.(maximum|value)\Z")
_BINDING = {
    "rail",
    "role",
    "equation",
    "equation_type",
    "equation_offset",
    "sharing_graph_complete",
    "offsets",
    "current",
    "units",
    "capability",
    "upper_clamp_binding",
    "affected_rail_roots",
    "affected_clock_roots",
}
_ROOT = {"rail_index", "rail_identity", "role", "selector_offset", "equation", "dependency_indices"}


class VoltageRelError(RuntimeError):
    pass


read = ceiling.read
validate_observation = ceiling.validate_observation


def _selected(changes):
    if not isinstance(changes, Mapping) or any(not isinstance(key, str) for key in changes):
        raise VoltageRelError("REL changes require a field/value mapping")
    selected = {key: value for key, value in changes.items() if KEY.fullmatch(key)}
    if selected and set(selected) != set(changes):
        raise VoltageRelError("Apply REL equation bounds separately from other BIOS fields")
    rails = [KEY.fullmatch(key).group(1) for key in selected]
    if len(set(rails)) != len(rails):
        raise VoltageRelError("Select only one REL operation for each equation")
    return selected


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise VoltageRelError("Malformed REL equation or dependency binding")
    return value


def _binding(field, key):
    match = KEY.fullmatch(key)
    if match is None or not isinstance(field, dict) or not _BINDING <= set(field):
        raise VoltageRelError("Incomplete REL equation-bound BIOS binding")
    value = {name: copy.deepcopy(field[name]) for name in _BINDING}
    rail = int(match.group(1))
    if (
        value["rail"] != ceiling.voltage._RAILS[rail][0]
        or value["role"] != "REL"
        or type(value["equation_type"]) is not int
        or value["equation_type"] != 7
        or value["sharing_graph_complete"] is not True
        or value["units"] != "uV"
        or value["capability"] != "editable"
    ):
        raise VoltageRelError("REL needs an understood multivariate voltage equation and complete dependency graph")
    index = _integer(value["equation"], 0, 8191)
    offset = _integer(value["equation_offset"], 0, 16 * 1024 * 1024 - 25)
    offsets = [offset + 3, offset + 7] if match.group(2) == "value" else [offset + 7]
    if not _same_state(value["offsets"], offsets):
        raise VoltageRelError("REL must bind exactly its selected BIOS bound floats")
    native = value["upper_clamp_binding"]
    native_fields = {
        "index",
        "lower_uv",
        "before_uv",
        "next_index",
        "variable",
        "output_type",
        "variables",
        "coefficients",
    }
    if not isinstance(native, dict) or set(native) != native_fields:
        raise VoltageRelError("Incomplete REL native definition binding")
    if not _same_state(native["index"], index) or not _same_state(native["before_uv"], value["current"]):
        raise VoltageRelError("REL BIOS field and native definition binding disagree")
    if type(value["affected_clock_roots"]) is not list or value["affected_clock_roots"]:
        raise VoltageRelError("REL has an affected clock root without an understood policy contract")
    roots = value["affected_rail_roots"]
    if not isinstance(roots, list) or not roots:
        raise VoltageRelError("REL has no complete affected rail-root manifest")
    seen, own = set(), False
    for root in roots:
        if not isinstance(root, dict) or set(root) != _ROOT:
            raise VoltageRelError("Malformed affected REL rail-root record")
        ri = _integer(root["rail_index"], 0, 1)
        if (
            type(root["rail_identity"]) is not int
            or root["rail_identity"] != ri + 1
            or root["role"] not in ("REL", "ALT")
        ):
            raise VoltageRelError("REL affects a rail role without an understood policy contract")
        token = (ri, root["role"])
        if token in seen:
            raise VoltageRelError("Duplicate affected REL rail root")
        seen.add(token)
        _integer(root["selector_offset"], 0, 16 * 1024 * 1024 - 2)
        eq = _integer(root["equation"], 0, 8191)
        deps = root["dependency_indices"]
        if not isinstance(deps, list) or any(type(i) is not int or not 0 <= i < 8192 for i in deps):
            raise VoltageRelError("Malformed affected REL root dependencies")
        if deps != sorted(set(deps)) or index not in deps or eq not in deps:
            raise VoltageRelError("Affected root does not depend on the selected REL equation")
        own |= token == (rail, "REL") and eq == index
    if not own:
        raise VoltageRelError("REL manifest omits its own selected rail root")
    return value


def _fields(inspection, changes):
    fields = {field["key"]: field for field in (inspection or {}).get("fields", [])}
    bindings = {key: _binding(fields.get(key), key) for key in _selected(changes)}
    _unique_equations(bindings)
    return bindings


def _unique_equations(bindings):
    indices = [binding["equation"] for binding in bindings.values()]
    if len(set(indices)) != len(indices):
        raise VoltageRelError("Selected REL controls alias the same equation")


def _derive(before, bindings, changes):
    before = validate_observation(before)
    if before["boost_percent"] is None:
        raise VoltageRelError("REL needs the voltage boost request GET")
    selected = _selected(changes)
    use_bounds = any(key.endswith(".value") for key in selected)
    checked = {key: _binding(bindings[key], key) for key in selected}
    _unique_equations(checked)
    selections = {}
    for key, wanted in selected.items():
        binding = checked[key]
        rail = key.split(".")[1]
        if rail not in before["policy"]["rails"]:
            raise VoltageRelError("Selected REL rail has no independent policy readback")
        if any(str(root["rail_index"]) not in before["policy"]["rails"] for root in binding["affected_rail_roots"]):
            raise VoltageRelError("An affected REL rail has no independent policy and control readback")
        selections[key] = {**binding["upper_clamp_binding"], "after_uv": ceiling._whole(wanted)}
        if use_bounds:
            selections[key]["after_lower_uv"] = (
                selections[key]["after_uv"] if key.endswith(".value") else selections[key]["lower_uv"]
            )
    make_native = equations.voltage_bounds_contracts if use_bounds else equations.clamp_contracts
    native = make_native(before["equations"], selections)
    after = copy.deepcopy(before)
    after["equations"] = copy.deepcopy(native["after"])
    return after, native


def contracts(gpu, inspection, changes):
    selected = _selected(changes)
    if not selected:
        return {}
    bindings = _fields(inspection, changes)
    before, error = read(gpu)
    if error:
        raise VoltageRelError(error)
    after, native = _derive(before, bindings, selected)
    saved = {"schema": 1, "selected": bindings, "before": before, "after": after, "native": _native_reference(native)}
    validate_contracts(selected, saved)
    return saved


def _native_reference(native):
    # Full INFO/CONTROL packets are already stored once in each observation.
    # Duplicating them here exceeds the bounded durable receipt size.
    return {name: copy.deepcopy(native[name]) for name in ("schema", "kind", "selected")}


def validate_contracts(changes, saved):
    selected = _selected(changes)
    if isinstance(saved, dict) and not saved and not selected:
        return
    if (
        not isinstance(saved, dict)
        or set(saved) != {"schema", "selected", "before", "after", "native"}
        or type(saved["schema"]) is not int
        or saved["schema"] != 1
        or not isinstance(saved["selected"], dict)
        or not selected
        or set(saved["selected"]) != set(selected)
    ):
        raise VoltageRelError("Missing or malformed REL equation-bound contract")
    for field in saved["selected"].values():
        if not isinstance(field, dict) or set(field) != _BINDING:
            raise VoltageRelError("Malformed saved REL BIOS binding")
    after, native = _derive(saved["before"], saved["selected"], selected)
    if not _same_state(after, saved["after"]) or not _same_state(_native_reference(native), saved["native"]):
        raise VoltageRelError("REL contract differs from its exact native projection and preserved state")


def validate_inspection(inspection, changes, saved):
    validate_contracts(changes, saved)
    if saved and not _same_state(_fields(inspection, changes), saved["selected"]):
        raise VoltageRelError("REL BIOS binding changed after preview")


def _policy_contract(policy, saved):
    result = copy.deepcopy(policy)
    changed = {
        key
        for key, row in saved["native"]["selected"].items()
        if row["before_uv"] != row["after_uv"] or row.get("after_lower_uv", row["lower_uv"]) != row["lower_uv"]
    }
    if not changed:
        return result
    affected = {
        role: {
            str(root["rail_index"])
            for key in changed
            for root in saved["selected"][key]["affected_rail_roots"]
            if root["role"] == role
        }
        for role in ("REL", "ALT")
    }
    shared_boost = saved["before"]["boost_percent"] != 0
    for index, row in result["rails"].items():
        # REL/ALT monitor refresh also recomputes shared boost across rails.
        # The loaded definitions and request bytes are verified independently.
        if shared_boost:
            row.pop("boost_uv")
        if shared_boost or index in affected["REL"]:
            row["policy_uv"].pop("reliability")
        if shared_boost or index in affected["REL"] | affected["ALT"]:
            row["policy_uv"].pop("effective")
        if index in affected["ALT"]:
            row["policy_uv"].pop("alt_reliability")
    return result


def verify(observation, saved, *, restore=False):
    if not saved:
        return []
    changes = {key: row["after_uv"] for key, row in saved["native"]["selected"].items()}
    validate_contracts(changes, saved)
    actual = validate_observation(observation)
    expected = saved["before" if restore else "after"]
    verify_native = (
        equations.verify_voltage_bounds if saved["native"]["kind"] == "voltage_bounds" else equations.verify_clamps
    )
    verify_native(
        actual["equations"],
        {**saved["native"], "before": saved["before"]["equations"], "after": saved["after"]["equations"]},
        restore=restore,
    )
    if actual["boost_percent"] != expected["boost_percent"]:
        raise VoltageRelError("Voltage boost request changed during REL activation")
    if not _same_state(actual["rail_control"], expected["rail_control"]):
        raise VoltageRelError("REL activation changed retained rail controls or opaque fields")
    if restore:
        matches = _same_state(actual["policy"], expected["policy"])
    else:
        matches = _same_state(_policy_contract(actual["policy"], saved), _policy_contract(expected["policy"], saved))
    if not matches:
        raise VoltageRelError("REL activation changed preserved rail limits, offsets or identity")
    return list(saved["selected"])
