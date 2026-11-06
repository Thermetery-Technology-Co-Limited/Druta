# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit startup REL adjustment with independent activation/readback checks.

FixBlackScreenIssue DWORD1 forces the startup adjustment; DWORD2 suppresses it.
The affected rails have current native INFO byte+0x33 equal to3. A current
offset does not identify its source, and this module cannot establish whether
arbitrary captured requests can be restored after a restart. The transaction
owner records the original state and reports recovery required if exact
restoration cannot be verified.

No registry writes, SET calls, or restarts occur here. Exact loaded definitions,
requests, and opaque bytes are preserved. Firmware-derived policy is observed,
not predicted by a voltage formula or treated as physical voltage measurement.
The initializer and selector serializer are traced in
experiments/registry-overrides/driver-controls/fix-black-screen-findings-20261025.json.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from contextlib import nullcontext

from . import vfe_equation_runtime as equations
from . import voltage_request_replay as requests
from .registry_flags import _same_state

KEY = "rail_rel_adjustment"
REGISTRY_NAME = "FixBlackScreenIssue"
_DOMAIN_OFFSET = 0x33


class VoltageStartupError(RuntimeError):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def _fail(message, kind="malformed"):
    raise VoltageStartupError(kind, message)


def _selected(changes):
    if not isinstance(changes, Mapping) or any(type(key) is not str for key in changes):
        _fail("Startup voltage changes require a field/value mapping")
    if KEY not in changes:
        return {}
    if set(changes) != {KEY}:
        _fail("Apply the startup REL adjustment separately from other controls")
    registry_dword(changes[KEY])
    return {KEY: changes[KEY]}


def registry_dword(enabled):
    """Return an explicit setting; absence and zero have different semantics."""
    if type(enabled) is not bool:
        _fail("Startup REL adjustment requires an explicit Boolean selection")
    return 1 if enabled else 2


def _requests(value):
    try:
        state = requests.validate(value)
    except requests.ReplayError as exc:
        raise VoltageStartupError(exc.kind, str(exc)) from exc
    # rr.project deliberately retains historical policy. A fresh observation,
    # unlike that projection, must agree with the independent offset getter.
    for index, text in state["public"].items():
        raw = bytes.fromhex(text)
        at = requests.controls.ROW_OFFSET + int(index) * requests.controls.ROW_STRIDE
        values = requests._six(raw, at)
        expected = dict(zip(requests.voltage._OFFSETS, values[:4], strict=True))
        if not _same_state(state["policy"]["rails"][index]["offsets_uv"], expected):
            _fail("Startup policy offsets disagree with independently captured rail requests")
    if state["complete"] and set(state["policy"]["rails"]) != set(state["public"]):
        _fail("Complete startup capture has an unpaired policy rail")
    return state


def validate_observation(observation):
    """Validate fresh readback; partial requests remain readable diagnostics."""
    if (
        not isinstance(observation, dict)
        or set(observation) != {"schema", "requests", "equations"}
        or type(observation["schema"]) is not int
        or observation["schema"] != 1
    ):
        _fail("Malformed startup voltage observation")
    state = _requests(observation["requests"])
    try:
        definitions = equations.validate_observation(observation["equations"])
    except equations.VfeEquationError as exc:
        raise VoltageStartupError("malformed", str(exc)) from exc
    if not _same_state(state["identity"], definitions["identity"]):
        _fail("Startup requests and VFE definitions identify different adapters", "identity")
    return {"schema": 1, "requests": state, "equations": definitions}


def read_checked(gpu):
    """Bracket definitions with stable current requests, with one GET retry."""
    last = None
    with getattr(gpu, "_lock", nullcontext()):
        for attempt in range(2):
            try:
                first = _requests(requests.read_checked(gpu))
                definitions, error = equations.read(gpu)
                if error:
                    _fail("Startup VFE definition readback unavailable: " + str(error), "unavailable")
                second = _requests(requests.read_checked(gpu))
                if not _same_state(first, second):
                    _fail("Startup rail requests or reported policy changed during capture", "transient")
                return validate_observation({"schema": 1, "requests": second, "equations": definitions})
            except requests.ReplayError as exc:
                last = VoltageStartupError(exc.kind, str(exc))
                last.__cause__ = exc
            except VoltageStartupError as exc:
                last = exc
            if last.kind != "transient" or attempt:
                raise last
    raise last  # Defensive: the bounded loop either returns or raises.


def read(gpu):
    try:
        return read_checked(gpu), None
    except VoltageStartupError as exc:
        return None, str(exc)


def _affected(state):
    if not state["complete"]:
        _fail("Startup REL adjustment needs every active rail independently captured", "unsupported")
    info = bytes.fromhex(state["info_hex"])
    active = requests._indices(requests._mask(info))
    affected = [
        str(index)
        for index in sorted(active)
        if info[requests.HEADER + index * requests.INFO_STRIDE + _DOMAIN_OFFSET] == 3
    ]
    if not affected:
        _fail("No current rail reports the understood startup adjustment selector", "unsupported")
    if any(index not in state["public"] for index in affected):
        _fail("An affected startup rail lacks independent request and policy readback", "unsupported")
    return affected


def from_observation(observation, changes):
    """Build a pure request contract; this is not proof of recovery capability."""
    selected = _selected(changes)
    if not selected:
        return {}
    before = validate_observation(observation)
    state = before["requests"]
    affected = _affected(state)
    offsets = {}
    for index in affected:
        at = requests.controls.ROW_OFFSET + int(index) * requests.controls.ROW_STRIDE
        values = requests._six(bytes.fromhex(state["public"][index]), at)
        values[0] = -50000 if selected[KEY] else 0
        offsets[index] = values
    after = requests.project(state, offsets_uv=offsets)
    return {
        "schema": 1,
        "selected": selected,
        "affected_indices": affected,
        "before": before,
        # This policy member is historical context, not a predicted output.
        "after_requests": after,
    }


def contracts(gpu, changes):
    if not _selected(changes):
        return {}
    return from_observation(read_checked(gpu), changes)


def validate_contracts(changes, saved):
    selected = _selected(changes)
    if isinstance(saved, dict) and not saved and not selected:
        return
    if (
        not selected
        or not isinstance(saved, dict)
        or set(saved) != {"schema", "selected", "affected_indices", "before", "after_requests"}
        or type(saved["schema"]) is not int
        or saved["schema"] != 1
        or not _same_state(saved["selected"], selected)
    ):
        _fail("Missing or malformed startup REL adjustment contract")
    expected = from_observation(saved["before"], selected)
    if not _same_state(saved, expected):
        _fail("Startup contract differs from its complete inventory and exact REL-only projection")


def _saved(saved):
    if not isinstance(saved, dict) or "selected" not in saved:
        _fail("Missing startup REL adjustment contract")
    validate_contracts(saved["selected"], saved)


def verify_activation_requests(observation, saved):
    """Check the initializer before SET; repeat inside the replay worker.

    Other offsets and boost may have reset and await the final replay check.
    This narrow proof must never be reported as full transaction verification.
    """
    if saved == {}:
        return []
    _saved(saved)
    actual = _requests(observation)
    expected = saved["after_requests"]
    if not _same_state(requests._opaque(actual), requests._opaque(expected)):
        _fail("Startup activation changed rail identity, complete INFO, or opaque request state")
    for index in saved["affected_indices"]:
        at = requests.controls.ROW_OFFSET + int(index) * requests.controls.ROW_STRIDE
        value = requests._six(bytes.fromhex(actual["public"][index]), at)[0]
        wanted = requests._six(bytes.fromhex(expected["public"][index]), at)[0]
        if value != wanted:
            _fail("Startup REL adjustment did not activate before request replay", "activation")
    return [KEY]


def verify_activation(observation, saved):
    """Prove selected registry effect and unchanged definitions before replay."""
    if saved == {}:
        return []
    _saved(saved)
    actual = validate_observation(observation)
    if not _same_state(actual["equations"], saved["before"]["equations"]):
        _fail("Startup activation changed loaded VFE definitions")
    return verify_activation_requests(actual["requests"], saved)


def _policy(policy, saved):
    result = copy.deepcopy(policy)
    shared_boost = saved["before"]["requests"]["boost_percent"] != 0
    for index, row in result["rails"].items():
        if shared_boost:
            row.pop("boost_uv")
        if shared_boost or index in saved["affected_indices"]:
            row["policy_uv"].pop("reliability")
            row["policy_uv"].pop("effective")
    return result


def verify(observation, saved, *, restore=False):
    """Verify final replayed requests and scoped policy, or exact full undo."""
    if saved == {}:
        return []
    _saved(saved)
    actual = validate_observation(observation)
    before = saved["before"]
    if restore:
        if not _same_state(actual, before):
            _fail("Startup adjustment undo did not restore captured requests, definitions, and policy")
        return [KEY]
    expected = saved["after_requests"]
    if not _same_state(actual["equations"], before["equations"]):
        _fail("Startup adjustment changed loaded VFE definitions")
    if not _same_state(requests._requested(actual["requests"]), requests._requested(expected)):
        _fail("Startup adjustment changed preserved requests, boost, INFO, or opaque bytes")
    expected_policy = copy.deepcopy(before["requests"]["policy"])
    for index in saved["affected_indices"]:
        expected_policy["rails"][index]["offsets_uv"]["reliability"] = -50000 if saved["selected"][KEY] else 0
    if not _same_state(_policy(actual["requests"]["policy"], saved), _policy(expected_policy, saved)):
        _fail("Startup adjustment changed preserved rail limits or policy offsets")
    return [KEY]
