# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Loaded constant voltage equations and independently reported rail limits.

ALT is a constant added to a linked REL equation; OV is a terminal constant.
The complete loaded equation packets and retained requests must match exactly.
Firmware-derived effective limits are observations, not a host-side min formula
or evidence of measured regulator voltage or enforcement under load.
"""

import copy
import ctypes
import math
import re
import struct
from collections.abc import Mapping

from . import clock_bios_runtime as clock
from . import vfe_equation_runtime as equations
from . import voltage_bios_runtime as voltage
from . import voltage_rail_control_runtime as rail_control
from .registry_flags import _same_state

KEY = re.compile(r"voltage\.([01])\.(ALT\.addend|OV\.constant)\Z")
_BINDING = {
    "rail",
    "role",
    "equation",
    "equation_type",
    "next_equation",
    "variable_index",
    "output_type",
    "shared",
    "sharing_graph_complete",
    "offsets",
    "current",
    "units",
    "capability",
}


class VoltageCeilingError(RuntimeError):
    pass


def _selected(changes):
    if not isinstance(changes, Mapping) or any(not isinstance(key, str) for key in changes):
        raise VoltageCeilingError("Voltage changes require a field/value mapping")
    selected = {key: value for key, value in changes.items() if KEY.fullmatch(key)}
    if selected and set(selected) != set(changes):
        raise VoltageCeilingError("Apply constant voltage ceilings separately from other BIOS fields")
    return selected


def _whole(value, *, zero=False):
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or value != int(value)
        or not (0 if zero else 1) <= value <= 0x7FFFFFFF
    ):
        raise VoltageCeilingError("Voltage constants require whole microvolts in the understood positive range")
    if struct.unpack("<f", struct.pack("<f", value))[0] != value:
        raise VoltageCeilingError("Voltage constant is not represented exactly by its BIOS float")
    return int(value)


def _boost(gpu):
    """Guard the 40-byte boost GET; its only output is the byte at +4."""
    getter = getattr(gpu.nvapi, "VoltCtrlGet", None)
    if getter is None:
        return None
    reader = clock.PerfDiagnosticsReader(gpu)
    identity = clock._binding(reader)
    storage, data = clock._guarded(40)
    struct.pack_into("<I", data, 0, 0x10028)
    data[4] = 255
    expected = bytes(data)
    status = getter(gpu.nvapi.gpu, ctypes.byref(data))
    clock._guards(storage)
    if type(status) is not int or clock._binding(reader) != identity:
        raise VoltageCeilingError("Voltage boost GET failed or changed adapter")
    if bytes(data[:4]) != expected[:4] or bytes(data[5:]) != expected[5:]:
        raise VoltageCeilingError("Voltage boost GET changed its caller-owned layout")
    # NVIDIA's public NvAPI_Status enum: NO_IMPLEMENTATION, incompatible layout, NOT_SUPPORTED.
    # An optional unsupported boost getter must not erase OV capability.
    if status in (-3, -9, -104):
        return None
    if status != 0 or data[4] > 100:
        raise VoltageCeilingError("Voltage boost GET failed or did not return a valid request byte")
    return int(data[4])


def validate_observation(observation):
    if not isinstance(observation, dict) or set(observation) != {
        "equations",
        "policy",
        "boost_percent",
        "rail_control",
    }:
        raise VoltageCeilingError("Malformed constant voltage observation")
    result = copy.deepcopy(observation)
    result["equations"] = equations.validate_observation(result["equations"])
    result["policy"] = voltage.validate_policy(result["policy"])
    result["rail_control"] = rail_control.validate_observation(result["rail_control"], result["policy"])
    if not _same_state(result["equations"]["identity"], result["policy"]["identity"]):
        raise VoltageCeilingError("Equation and voltage policy adapters differ")
    if result["boost_percent"] is not None:
        if type(result["boost_percent"]) is not int or not 0 <= result["boost_percent"] <= 100:
            raise VoltageCeilingError("Malformed captured voltage boost request byte")
    return result


def _once(gpu):
    first_boost = _boost(gpu)
    native, error = equations.read(gpu)
    if error:
        raise VoltageCeilingError(error)
    policy, error = voltage.read(gpu)
    if error:
        raise VoltageCeilingError(error)
    controls, error = rail_control.read(gpu, policy)
    if error:
        raise VoltageCeilingError(error)
    last_boost = _boost(gpu)
    if first_boost != last_boost:
        raise VoltageCeilingError("Voltage boost request changed during the observation")
    result = validate_observation(
        {"equations": native, "policy": policy, "boost_percent": last_boost, "rail_control": controls}
    )
    if clock._binding(clock.PerfDiagnosticsReader(gpu)) != native["identity"]:
        raise VoltageCeilingError("Voltage target changed during combined GET")
    return result


def read(gpu):
    last = None
    for _ in range(2):
        try:
            with gpu._lock:
                first, second = _once(gpu), _once(gpu)
            if not _same_state(first, second):
                raise VoltageCeilingError("Voltage equations, requests or reported limits changed between reads")
            return second, None
        except Exception as exc:
            last = str(exc)
    return None, last or "Constant voltage readback unavailable"


def require_restartable_request(observation):
    """Refuse an apply known to lose its requested boost during GPU restart.

    Rail-group construction resets the percentage byte to zero. There is no
    validated request-replay path in registry transactions. Keep this apply
    precondition separate from receipt validation, so existing undo records
    remain usable. Unsupported GET remains independent of OV capability.
    """
    observation = validate_observation(observation)
    if observation["boost_percent"] not in (None, 0):
        raise VoltageCeilingError(
            "GPU restart resets the nonzero voltage boost request; set boost to 0% before applying BIOS voltage changes"
        )


def _binding(field, key):
    match = KEY.fullmatch(key)
    index, role = match.group(1), match.group(2).split(".")[0]
    if not isinstance(field, dict) or not _BINDING <= set(field):
        raise VoltageCeilingError(key + ": incomplete BIOS constant binding")
    value = {name: copy.deepcopy(field[name]) for name in _BINDING}
    if (
        value["rail"] != voltage._RAILS[int(index)][0]
        or value["role"] != role
        or type(value["equation_type"]) is not int
        or value["equation_type"] != 1
        or value["shared"] is not False
        or value["sharing_graph_complete"] is not True
        or value["output_type"] != 2
        or type(value["output_type"]) is not int
        or value["units"] != "uV"
        or value["capability"] != "editable"
    ):
        raise VoltageCeilingError(key + ": no independent constant equation binding")
    for name, maximum in (("equation", 8191), ("next_equation", 65535), ("variable_index", 255)):
        if type(value[name]) is not int or not 0 <= value[name] <= maximum:
            raise VoltageCeilingError(key + ": malformed equation index or variable")
    if role == "OV" and value["next_equation"] != 65535:
        raise VoltageCeilingError("OV must be a terminal constant equation")
    if role == "ALT" and not 0 <= value["next_equation"] < 8192:
        raise VoltageCeilingError("ALT must add a constant to a linked REL equation")
    offsets = value["offsets"]
    if (
        not isinstance(offsets, list)
        or len(offsets) != 3
        or any(type(x) is not int or not 3 <= x <= 16 * 1024 * 1024 - 4 for x in offsets)
        or offsets != [offsets[0], offsets[0] + 4, offsets[0] + 8]
    ):
        raise VoltageCeilingError("Constant equation must bind its three exact BIOS float words")
    value["current"] = _whole(value["current"], zero=role == "ALT")
    return value


def _fields(inspection, changes):
    fields = {field["key"]: field for field in (inspection or {}).get("fields", [])}
    result = {}
    for key in _selected(changes):
        binding = _binding(fields.get(key), key)
        if binding["role"] == "ALT":
            rel = fields.get("voltage." + key.split(".")[1] + ".REL.maximum", {})
            if (
                rel.get("equation") != binding["next_equation"]
                or rel.get("role") != "REL"
                or rel.get("rail") != binding["rail"]
            ):
                raise VoltageCeilingError("ALT is not linked to the selected rail's REL equation")
        result[key] = binding
    return result


def _derive(before, bindings, changes):
    before = validate_observation(before)
    after = copy.deepcopy(before)
    selections = {}
    for key, wanted in _selected(changes).items():
        binding = _binding(bindings[key], key)
        role, index = binding["role"], key.split(".")[1]
        row = before["policy"]["rails"].get(index)
        if not row:
            raise VoltageCeilingError(key + ": selected rail has no independent limit readback")
        wanted = _whole(wanted, zero=role == "ALT")
        if role == "ALT" and before["boost_percent"] is None:
            raise VoltageCeilingError("ALT needs the voltage boost request GET to preserve the shared request")
        selections[key] = {
            "index": binding["equation"],
            "before_uv": binding["current"],
            "after_uv": wanted,
            "next_index": binding["next_equation"],
            "variable": binding["variable_index"],
            "output_type": 2,
        }
        if role == "OV":
            offset = row["offsets_uv"]["overvoltage"]
            if binding["current"] + offset != row["policy_uv"]["overvoltage"]:
                raise VoltageCeilingError("BIOS OV plus retained offset differs from the reported OV limit")
            desired = wanted + offset
            name = "overvoltage"
        else:
            desired = row["policy_uv"]["alt_reliability"] + wanted - binding["current"]
            name = "alt_reliability"
        if not 0 < desired <= 0xFFFFFFFF:
            raise VoltageCeilingError("Requested constant produces an invalid reported rail limit")
        after["policy"]["rails"][index]["policy_uv"][name] = desired
    native = equations.contracts(before["equations"], selections)
    after["equations"] = copy.deepcopy(native["after"])
    return after, native["selected"]


def contracts(gpu, inspection, changes):
    selected = _selected(changes)
    if not selected:
        return {}
    bindings = _fields(inspection, changes)
    before, error = read(gpu)
    if error:
        raise VoltageCeilingError(error)
    after, native = _derive(before, bindings, changes)
    saved = {"schema": 1, "selected": bindings, "before": before, "after": after, "equation_selections": native}
    validate_contracts(changes, saved)
    return saved


def validate_contracts(changes, saved):
    selected = _selected(changes)
    if saved == {}:
        if selected:
            raise VoltageCeilingError("Constant voltage changes have no saved runtime contract")
        return
    if (
        not isinstance(saved, dict)
        or set(saved) != {"schema", "selected", "before", "after", "equation_selections"}
        or type(saved["schema"]) is not int
        or saved["schema"] != 1
        or not isinstance(saved["selected"], dict)
        or not selected
        or set(selected) != set(saved["selected"])
    ):
        raise VoltageCeilingError("Malformed constant voltage contract")
    for key, value in saved["selected"].items():
        if not isinstance(value, dict) or set(value) != _BINDING:
            raise VoltageCeilingError("Malformed saved constant BIOS binding")
        _binding(value, key)
    after, native = _derive(saved["before"], saved["selected"], selected)
    if not _same_state(after, saved["after"]) or not _same_state(native, saved["equation_selections"]):
        raise VoltageCeilingError("Constant voltage contract does not preserve loaded equations and peer limits")


def validate_inspection(inspection, changes, saved):
    validate_contracts(changes, saved)
    if saved and not _same_state(_fields(inspection, changes), saved["selected"]):
        raise VoltageCeilingError("Constant voltage BIOS binding changed after preview")


def _reported_contract(policy, selected):
    result = copy.deepcopy(policy)
    alt = any(key.endswith(".ALT.addend") for key in selected)
    affected = {key.split(".")[1] for key in selected}
    for index, row in result["rails"].items():
        # These are firmware-computed outputs. Exact user requests, loaded
        # equations, offsets and all independently reported bounds remain
        # compared. Do not claim an unproven host-side effective-limit formula.
        if alt or index in affected:
            row["policy_uv"].pop("effective")
        if alt:
            row.pop("boost_uv")
            row["policy_uv"].pop("reliability")
    return result


def verify(observation, saved, *, restore=False):
    if not saved:
        return []
    actual = validate_observation(observation)
    expected = saved["before" if restore else "after"]
    equations.verify(
        actual["equations"],
        {
            "schema": 1,
            "selected": saved["equation_selections"],
            "before": saved["before"]["equations"],
            "after": saved["after"]["equations"],
        },
        restore=restore,
    )
    if not _same_state(actual["rail_control"], expected["rail_control"]):
        raise VoltageCeilingError("Retained rail control requests or opaque returned fields changed")
    if actual["boost_percent"] != expected["boost_percent"]:
        raise VoltageCeilingError("Voltage boost request changed")
    if restore:
        matches = _same_state(actual["policy"], expected["policy"])
    else:
        changed = {key for key, row in saved["equation_selections"].items() if row["before_uv"] != row["after_uv"]}
        matches = _same_state(
            _reported_contract(actual["policy"], changed),
            _reported_contract(expected["policy"], changed),
        )
    if not matches:
        raise VoltageCeilingError("Reported constant voltage limit, retained offsets or peer limits differ")
    return list(saved["selected"])
