# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Finite readback candidates for losing the main GRPG controller.

The constructor has conditional edges to controllers 2 and 4. This does not
assert that the final dependency graph is known: verification must reject every
readback outside these candidates, including any other peer change.
Input is an already validated typed observation; this module performs no I/O.
"""

from __future__ import annotations

import copy
import itertools


class MainProjectionError(RuntimeError):
    """No safely determined main-loss readback candidate is available."""


_UNAVAILABLE = {"available": False, "value": None}


def _any(rows):
    if any(row["available"] and row["value"] == 1 for row in rows):
        return 1
    if all(row["available"] and row["value"] == 0 for row in rows):
        return 0
    raise MainProjectionError("Remaining group support is unknown")


def _project(core, cleared, *, allow_no_peer=False):
    controllers = copy.deepcopy(core["controllers"])
    auxiliary = copy.deepcopy(core["auxiliary"])
    bit1 = copy.deepcopy(core["bit1"])
    main = copy.deepcopy(core["main"])
    for index in cleared:
        controllers[str(index)] = {
            "available": True,
            "supported": False,
            "states_available": False,
            "configured_pstate_mask": None,
        }
        main["features"][str(index)] = dict(_UNAVAILABLE)
    auxiliary["feature"] = dict(_UNAVAILABLE)

    # Controller 9's feature filter depends on class-0 providers 0, 1, 2, 20.
    feature9 = main["features"]["9"]
    if feature9["available"] and feature9["value"] & 8:
        providers = []
        for index in (1, 2, 20):
            controller = controllers[str(index)]
            feature = main["features"][str(index)]
            if controller["available"] and controller["supported"] is False:
                providers.append(False)
            elif controller["available"] and controller["supported"] is True and feature["available"]:
                providers.append(bool(feature["value"] & 0x200000))
            else:
                providers.append(None)
        if True not in providers:
            if None in providers:
                raise MainProjectionError("Controller 9 feature provider is unknown")
            feature9["value"] &= ~8

    roles = bit1["roles"]
    affected = {0} | ({3} if 2 in cleared else set()) | ({2} if 4 in cleared else set())
    had_affected = any(roles[str(i)]["supported"] is True for i in affected)
    for index in affected:
        roles[str(index)] = {"available": False, "supported": None}
    if had_affected and not allow_no_peer and not any(row["available"] and row["supported"] is True for row in roles.values()):
        raise MainProjectionError("Bit1 group has no remaining supported query witness")

    group = auxiliary["group"]
    aux_affected = {0}
    if 4 in cleared:
        role1 = group["800:1"]
        controller6 = controllers["6"]
        if not controller6["available"] and (not role1["available"] or role1["value"] == 1):
            raise MainProjectionError("Controller 6 support is unknown")
        if controller6["available"] and controller6["supported"] is False:
            aux_affected.add(1)
    for index in aux_affected:
        for attr in ("800", "806"):
            key = f"{attr}:{index}"
            if group[key]["available"]:
                group[key] = {"available": True, "value": 0}
    if group["1:0"]["available"]:
        group["1:0"]["value"] = _any([group[f"800:{i}"] for i in range(4)])

    group4 = main["bit4_group"]
    if not group4["1:0"]["available"] or group4["1:0"]["value"] != 1:
        raise MainProjectionError("Type7 group support is not available and active")
    for index in {0, 1} if 4 in cleared else {0}:
        key = f"801:{index}"
        if not group4[key]["available"]:
            raise MainProjectionError("Affected type7 support is unknown")
        group4[key] = {"available": True, "value": 0}
    alive = _any([group4[f"801:{i}"] for i in range(3)])
    if alive:
        group4["1:0"] = {"available": True, "value": 1}
        if group4["805:0"]["available"]:
            group4["805:0"]["value"] = 0
    else:
        for key in group4:
            group4[key] = dict(_UNAVAILABLE)
    return {"cleared": sorted(cleared), "controllers": controllers, "auxiliary": auxiliary, "bit1": bit1, "main": main}


def profiles(core, *, allow_no_peer=False):
    """Return deterministic, qualified post-main-loss candidates."""
    controller0 = core["controllers"]["0"]
    if not controller0["available"] or controller0["supported"] is not True:
        raise MainProjectionError("Main controller support is not available and active")
    optional = [
        i
        for i in (2, 4)
        if core["controllers"][str(i)]["available"] and core["controllers"][str(i)]["supported"] is True
    ]
    result = []
    for count in range(len(optional) + 1):
        for subset in itertools.combinations(optional, count):
            try:
                candidate = _project(core, {0, *subset}, allow_no_peer=allow_no_peer)
            except MainProjectionError:
                continue
            if candidate not in result:
                result.append(candidate)
    if not result:
        raise MainProjectionError("No qualified main-loss projection")
    return result
