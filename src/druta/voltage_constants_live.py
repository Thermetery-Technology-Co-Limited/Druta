# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Session-only typed OV and ALT constants with verified compensation."""

from __future__ import annotations

import copy
import hashlib
import time

from . import clock_bios_runtime as clock
from . import vbios_fields
from . import vfe_equation_runtime as equations
from . import voltage_ceiling_runtime as ceiling
from . import voltage_rel_live as rel_live
from .perf_diagnostics import PerfDiagnosticsReader
from .registry_flags import _same_state


class VoltageConstantsLiveError(RuntimeError):
    def __init__(self, kind, message, *, attempted=False, rollback=None):
        super().__init__(message)
        self.kind, self.attempted, self.rollback = kind, attempted, rollback


def _key(rail, role):
    return f"voltage.{rail}." + ("ALT.addend" if role == "ALT" else "OV.constant")


def _loaded_binding(inspection, observation, key):
    fields = {field["key"]: field for field in inspection.get("fields", [])}
    original = ceiling._fields(inspection, {key: fields[key]["current"]})[key]
    row = equations.constant_row(observation["equations"], original["equation"])
    expected = {
        "index": original["equation"],
        "value_uv": row["value_uv"],
        "next_index": original["next_equation"],
        "variable": original["variable_index"],
        "output_type": original["output_type"],
    }
    if not _same_state(row, expected):
        raise ceiling.VoltageCeilingError("Loaded constant identity differs from the stored definition")
    loaded = copy.deepcopy(original)
    loaded["current"] = row["value_uv"]
    return loaded, original["current"]


class VoltageConstantsController:
    def __init__(self, gpu):
        self.gpu = gpu
        self._reader = PerfDiagnosticsReader(gpu)
        self._identity = None

    def _binding(self):
        try:
            identity = clock._binding(self._reader)
        except Exception as exc:
            raise VoltageConstantsLiveError(getattr(exc, "kind", "identity"), str(exc)) from exc
        if self._identity is not None and identity != self._identity:
            raise VoltageConstantsLiveError("identity", "Live voltage adapter changed; read the selected adapter again")
        self._identity = identity
        return identity

    def _capture(self):
        from .registry_overrides import read_vbios

        identity = self._binding()
        observation, error = ceiling.read(self.gpu)
        if error or observation is None:
            raise VoltageConstantsLiveError("transient", error or "Voltage constant readback unavailable")
        if observation["equations"]["identity"] != identity:
            raise VoltageConstantsLiveError("identity", "Voltage equation and selected adapter identities differ")
        try:
            clocks = rel_live._clocks(self.gpu)
        except rel_live.VoltageRelLiveError as exc:
            raise VoltageConstantsLiveError(exc.kind, str(exc)) from exc
        image = read_vbios(self.gpu)
        result = {
            "voltage": observation,
            "clocks": clocks,
            "image_sha256": hashlib.sha256(image).hexdigest(),
            "inspection": vbios_fields.inspect(image),
        }
        self._binding()
        return result

    def _capture_retry(self):
        for attempt in range(2):
            try:
                return self._capture()
            except VoltageConstantsLiveError as exc:
                if exc.kind != "transient" or attempt == 1:
                    raise
            except Exception as exc:
                if attempt == 1:
                    raise VoltageConstantsLiveError("transient", str(exc)) from exc
        raise AssertionError("Bounded voltage read did not complete")

    @staticmethod
    def _summary(state):
        obs = state["voltage"]
        rows = []
        for rail, label in enumerate(("NVVDD", "MSVDD")):
            for role in ("ALT", "OV"):
                key = _key(rail, role)
                item = {
                    "rail": rail,
                    "label": label,
                    "role": role,
                    "status": "unsupported",
                    "issues": [],
                    "equation": None,
                    "value_uv": None,
                    "stored_value_uv": None,
                    "reported_uv": None,
                    "policy_uv": None,
                }
                try:
                    binding, stored = _loaded_binding(state["inspection"], obs, key)
                    policy = obs["policy"]["rails"][str(rail)]["policy_uv"]
                    if role == "ALT" and obs["boost_percent"] is None:
                        raise ceiling.VoltageCeilingError("Voltage boost request readback unavailable")
                    # Check the role-specific current policy and complete native binding.
                    ceiling._derive(obs, {key: binding}, {key: binding["current"]})
                    item.update(
                        status="ready",
                        equation=binding["equation"],
                        value_uv=binding["current"],
                        stored_value_uv=stored,
                        policy_uv=copy.deepcopy(policy),
                        reported_uv=policy["alt_reliability" if role == "ALT" else "overvoltage"],
                    )
                except (KeyError, TypeError, ValueError, RuntimeError) as exc:
                    item["issues"].append(str(exc))
                rows.append(item)
        return {
            "identity": copy.deepcopy(obs["equations"]["identity"]),
            "image_sha256": state["image_sha256"],
            "rows": rows,
            "unavailable_peers": [key for key, value in state["clocks"].items() if value is None],
        }

    def read(self):
        with self.gpu._lock:
            last = None
            for _ in range(2):
                try:
                    return self._summary(self._capture())
                except VoltageConstantsLiveError as exc:
                    if exc.kind == "identity":
                        raise
                    last = exc
                except Exception as exc:
                    last = VoltageConstantsLiveError("transient", str(exc))
            raise last

    def set(self, rail, role, wanted_uv):
        if type(rail) is not int or rail not in (0, 1) or role not in ("ALT", "OV"):
            raise VoltageConstantsLiveError("malformed", "Select an understood voltage rail and constant role")
        try:
            ceiling._whole(wanted_uv, zero=role == "ALT")
        except (TypeError, ValueError, RuntimeError) as exc:
            raise VoltageConstantsLiveError("malformed", str(exc)) from exc
        attempted = False

        def entered():
            nonlocal attempted
            attempted = True

        with self.gpu._lock:
            try:
                before = self._capture_retry()
                key = _key(rail, role)
                binding, _ = _loaded_binding(before["inspection"], before["voltage"], key)
                after, selected = ceiling._derive(before["voltage"], {key: binding}, {key: wanted_uv})
                saved = {
                    "schema": 1,
                    "selected": {key: binding},
                    "before": before["voltage"],
                    "after": after,
                    "equation_selections": selected,
                }
                ceiling.validate_contracts({key: wanted_uv}, saved)
                native = {
                    "schema": 1,
                    "selected": selected,
                    "before": before["voltage"]["equations"],
                    "after": after["equations"],
                }
                if wanted_uv == binding["current"]:
                    return self._summary(before)
                equations.write_constants(self.gpu, native, on_attempt=entered)
                active = self._capture_retry()
                ceiling.verify(active["voltage"], saved)
                if active["image_sha256"] != before["image_sha256"] or not _same_state(
                    rel_live._requests(active["clocks"]), rel_live._requests(before["clocks"])
                ):
                    raise VoltageConstantsLiveError(
                        "verify", "Live constant changed retained clock requests or loaded image"
                    )
                return self._summary(active)
            except BaseException as exc:
                if not attempted:
                    if not isinstance(exc, Exception):
                        raise
                    if isinstance(exc, VoltageConstantsLiveError):
                        raise
                    raise VoltageConstantsLiveError(getattr(exc, "kind", "malformed"), str(exc)) from exc
                rollback = {"verified": False, "readback": None, "errors": []}
                try:
                    equations.write_constants(self.gpu, native, restore=True)
                except BaseException as restore_error:
                    rollback["errors"].append(str(restore_error))
                for number in range(8):
                    try:
                        final = self._capture()
                        ceiling.verify(final["voltage"], saved, restore=True)
                        if final["image_sha256"] != before["image_sha256"] or not _same_state(
                            final["clocks"], before["clocks"]
                        ):
                            raise VoltageConstantsLiveError(
                                "verify", "Original clock requests or computed curve not restored"
                            )
                        rollback["readback"] = self._summary(final)
                        rollback["verified"] = not rollback["errors"]
                        break
                    except Exception as read_error:
                        if number == 7:
                            rollback["errors"].append(str(read_error))
                        else:
                            time.sleep(1)
                if not isinstance(exc, Exception):
                    exc.rollback = rollback
                    exc.add_note(
                        "Voltage constant recovery verified"
                        if rollback["verified"]
                        else "Voltage constant recovery requires attention"
                    )
                    raise
                raise VoltageConstantsLiveError(
                    getattr(exc, "kind", "verify"), str(exc), attempted=True, rollback=rollback
                ) from exc
