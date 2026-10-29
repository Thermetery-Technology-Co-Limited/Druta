# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Session-only REL upper bounds, with observed policy and exact compensation.

The stored ROM supplies graph and term identity; the loaded upper bound may
differ after an earlier live edit. Evaluated REL/ALT values and the V/F curve
are observations, not a formula for physical regulator voltage.
"""

from __future__ import annotations

import copy
import ctypes
import hashlib
import time

from . import clock_bios_runtime as clock
from . import vbios_fields
from . import vfe_equation_runtime as equations
from . import voltage_rel_runtime as rel
from .nvbackend import _BoostTable, _set_point_masks, _VfpCurve
from .perf_diagnostics import PerfDiagnosticsReader
from .registry_flags import _same_state


class VoltageRelLiveError(RuntimeError):
    def __init__(self, kind, message, *, attempted=False, rollback=None):
        super().__init__(message)
        self.kind, self.attempted, self.rollback = kind, attempted, rollback


def _loaded_binding(field, observation):
    """Bind current bounds while preserving the ROM graph and terms."""
    key = field["key"]
    original = rel._binding(field, key)
    row = equations.upper_clamp_row(observation["equations"], original["equation"])
    expected = copy.deepcopy(original["upper_clamp_binding"])
    stored_upper = expected.pop("before_uv")
    expected["upper_uv"] = row["upper_uv"]
    expected["lower_uv"] = row["lower_uv"]
    if not _same_state(row, expected):
        raise rel.VoltageRelError("Loaded REL terms or graph identity differ from the stored definition")
    loaded = copy.deepcopy(original)
    loaded["current"] = row["upper_uv"]
    loaded["upper_clamp_binding"]["before_uv"] = row["upper_uv"]
    loaded["upper_clamp_binding"]["lower_uv"] = row["lower_uv"]
    return loaded, stored_upper


def _clocks(gpu):
    """Preserve readable clock requests and retain the computed curve separately."""
    result = {"pstates": None, "domains": None, "boost": None, "curve": None}
    state, error = gpu._read_pstates20()
    if state is not None:
        result["pstates"] = bytes(state).hex()
    elif getattr(gpu.nvapi, "Pstates20Get", None) is not None:
        raise VoltageRelLiveError("transient", error or "P-state request readback failed")
    layout, domains = gpu.clkdom_layout(), gpu.clkdom_domains()
    if layout is not None and domains:
        mask = sum(1 << domain for domain in domains)
        status, control = gpu._clkdom_get(mask)
        if status or not gpu._clkdom_valid_block(control, layout, mask):
            raise VoltageRelLiveError("transient", "Clock request readback failed")
        result["domains"] = bytes(control).hex()
    vf = gpu.vfp_layout()
    if vf is None and gpu.vf_curve_applicable() and getattr(gpu.nvapi, "VfpCurve", None) is not None:
        raise VoltageRelLiveError("transient", "V/F getter is present but its current layout could not be read")
    if vf is not None:
        for name, cls, getter in (
            ("boost", _BoostTable, gpu.nvapi.BoostTableGet),
            ("curve", _VfpCurve, gpu.nvapi.VfpCurve),
        ):
            if getter is None:
                continue
            storage, data = equations._guarded(ctypes.sizeof(cls))
            packet = cls.from_buffer(data)
            packet.version = gpu.nvapi.ver(cls, 1)
            version = packet.version
            _set_point_masks(packet, vf.n_entries)
            try:
                status = getter(gpu.nvapi.gpu, ctypes.byref(packet))
            finally:
                equations._guards(storage)
            if status or packet.version != version:
                raise VoltageRelLiveError("transient", f"V/F {name} request readback failed")
            if name == "curve":
                error = gpu._vfp_curve_error(packet, vf.n_entries)
                if error:
                    raise VoltageRelLiveError("transient", error)
            result[name] = bytes(packet).hex()
    return result


def _requests(clocks):
    result = dict(clocks)
    result.pop("curve")
    return result


def _verify_restored_bounds(observation, saved, native, *, restore=False):
    actual = rel.validate_observation(observation)
    expected = saved["before" if restore else "after"]
    equations.verify_voltage_bounds(actual["equations"], native, restore=restore)
    if actual["boost_percent"] != expected["boost_percent"] or not _same_state(
        actual["rail_control"], expected["rail_control"]
    ):
        raise rel.VoltageRelError("REL activation changed retained requests")
    if restore:
        matches = _same_state(actual["policy"], expected["policy"])
    else:
        matches = _same_state(rel._policy_contract(actual["policy"], saved), rel._policy_contract(expected["policy"], saved))
    if not matches:
        raise rel.VoltageRelError("REL activation changed preserved rail policy")


class VoltageRelController:
    def __init__(self, gpu):
        self.gpu = gpu
        self._reader = PerfDiagnosticsReader(gpu)
        self._identity = None

    def _binding(self):
        try:
            identity = clock._binding(self._reader)
        except Exception as exc:
            raise VoltageRelLiveError(getattr(exc, "kind", "identity"), str(exc)) from exc
        if self._identity is not None and identity != self._identity:
            raise VoltageRelLiveError("identity", "Live REL adapter changed; read the selected adapter again")
        self._identity = identity
        return identity

    def _capture(self):
        from .registry_overrides import read_vbios

        identity = self._binding()
        observation, error = rel.read(self.gpu)
        if error or observation is None:
            raise VoltageRelLiveError("transient", error or "REL readback unavailable")
        if observation["equations"]["identity"] != identity:
            raise VoltageRelLiveError("identity", "REL equation and selected adapter identities differ")
        image = read_vbios(self.gpu)
        result = {
            "voltage": observation,
            "clocks": _clocks(self.gpu),
            "image_sha256": hashlib.sha256(image).hexdigest(),
            "inspection": vbios_fields.inspect(image),
        }
        self._binding()
        return result

    def _capture_retry(self):
        for attempt in range(2):
            try:
                return self._capture()
            except VoltageRelLiveError as exc:
                if exc.kind != "transient" or attempt == 1:
                    raise
        raise AssertionError("Bounded REL read did not complete")

    @staticmethod
    def _summary(state):
        observation = state["voltage"]
        fields = {field["key"]: field for field in state["inspection"].get("fields", [])}
        rows = []
        for rail, label in enumerate(("NVVDD", "MSVDD")):
            key = f"voltage.{rail}.REL.maximum"
            item = {"rail": rail, "label": label, "status": "unsupported", "issues": []}
            try:
                binding, stored = _loaded_binding(fields[key], observation)
                policy = observation["policy"]["rails"][str(rail)]["policy_uv"]
                if observation["boost_percent"] is None:
                    raise rel.VoltageRelError("Voltage boost request readback unavailable")
                item.update(
                    status="ready",
                    equation=binding["equation"],
                    lower_uv=binding["upper_clamp_binding"]["lower_uv"],
                    upper_uv=binding["current"],
                    stored_upper_uv=stored,
                    stored_lower_uv=fields[key]["upper_clamp_binding"]["lower_uv"],
                    policy_uv=copy.deepcopy(policy),
                )
            except (KeyError, TypeError, ValueError, RuntimeError) as exc:
                item["issues"].append(str(exc))
            fixed_key = f"voltage.{rail}.REL.value"
            item["fixed_status"], item["fixed_issues"] = "unsupported", []
            try:
                fixed_binding, _ = _loaded_binding(fields[fixed_key], observation)
                if observation["boost_percent"] is None:
                    raise rel.VoltageRelError("Voltage boost request readback unavailable")
                if any(str(root["rail_index"]) not in observation["policy"]["rails"] for root in fixed_binding["affected_rail_roots"]):
                    raise rel.VoltageRelError("Selected REL rail has no independent policy readback")
                item["fixed_status"] = "ready"
            except (KeyError, TypeError, ValueError, RuntimeError) as exc:
                item["fixed_issues"].append(str(exc))
            rows.append(item)
        return {
            "identity": copy.deepcopy(observation["equations"]["identity"]),
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
                except VoltageRelLiveError as exc:
                    if exc.kind == "identity":
                        raise
                    last = exc
                except Exception as exc:
                    last = VoltageRelLiveError("transient", str(exc))
            raise last

    def set(self, rail, wanted_uv):
        """Change the loaded upper bound."""
        return self._set_bounds(rail, wanted_uv, mode="upper")

    def set_fixed(self, rail, wanted_uv):
        """Set both loaded bounds to the requested whole-microvolt value."""
        return self._set_bounds(rail, wanted_uv, mode="fixed")

    def restore_bounds(self, rail):
        """Restore both loaded bounds to the current ROM definition."""
        return self._set_bounds(rail, None, mode="restore")

    def _set_bounds(self, rail, wanted_uv, *, mode):
        """Write once, verify requests, and compensate once after dispatched errors."""
        if type(rail) is not int or rail not in (0, 1):
            raise VoltageRelLiveError("malformed", "Select an understood REL rail")
        if mode != "restore":
            try:
                equations._clamp_uv(wanted_uv, positive=True)
            except (TypeError, ValueError, RuntimeError) as exc:
                raise VoltageRelLiveError("malformed", str(exc)) from exc
        attempted = False

        def entered():
            nonlocal attempted
            attempted = True

        with self.gpu._lock:
            try:
                before = self._capture_retry()
                key = f"voltage.{rail}.REL." + ("maximum" if mode == "upper" else "value")
                field = next(field for field in before["inspection"]["fields"] if field["key"] == key)
                binding, stored_upper = _loaded_binding(field, before["voltage"])
                current = binding["upper_clamp_binding"]
                if mode == "restore":
                    wanted_uv = stored_upper
                    wanted_lower = field["upper_clamp_binding"]["lower_uv"]
                    # Apply the same policy and affected-root prerequisites as a fixed edit.
                    rel._derive(before["voltage"], {key: binding}, {key: wanted_uv})
                    selections = {key: {**current, "after_uv": wanted_uv, "after_lower_uv": wanted_lower}}
                    native = equations.voltage_bounds_contracts(before["voltage"]["equations"], selections)
                    after = copy.deepcopy(before["voltage"])
                    after["equations"] = copy.deepcopy(native["after"])
                else:
                    after, native = rel._derive(before["voltage"], {key: binding}, {key: wanted_uv})
                    wanted_lower = current["lower_uv"] if mode == "upper" else wanted_uv
                saved = {
                    "schema": 1,
                    "selected": {key: binding},
                    "before": before["voltage"],
                    "after": after,
                    "native": rel._native_reference(native),
                }
                if mode != "restore":
                    rel.validate_contracts({key: wanted_uv}, saved)
                if wanted_uv == binding["current"] and wanted_lower == current["lower_uv"]:
                    return self._summary(before)
                writer = equations.write_upper_clamps if mode == "upper" else equations.write_voltage_bounds
                writer(self.gpu, native, on_attempt=entered)
                active = self._capture_retry()
                if mode == "restore":
                    _verify_restored_bounds(active["voltage"], saved, native)
                else:
                    rel.verify(active["voltage"], saved)
                if active["image_sha256"] != before["image_sha256"] or not _same_state(
                    _requests(active["clocks"]), _requests(before["clocks"])
                ):
                    raise VoltageRelLiveError("verify", "Live REL changed retained clock requests or loaded image")
                return self._summary(active)
            except BaseException as exc:
                if not attempted:
                    if not isinstance(exc, Exception):
                        raise
                    if isinstance(exc, VoltageRelLiveError):
                        raise
                    raise VoltageRelLiveError(getattr(exc, "kind", "malformed"), str(exc)) from exc
                rollback = {"verified": False, "readback": None, "errors": []}
                try:
                    writer(self.gpu, native, restore=True)
                except BaseException as restore_error:
                    rollback["errors"].append(str(restore_error))
                for number in range(8):
                    try:
                        final = self._capture()
                        if mode == "restore":
                            _verify_restored_bounds(final["voltage"], saved, native, restore=True)
                        else:
                            rel.verify(final["voltage"], saved, restore=True)
                        if final["image_sha256"] != before["image_sha256"] or not _same_state(
                            final["clocks"], before["clocks"]
                        ):
                            raise VoltageRelLiveError(
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
                    exc.add_note("REL recovery verified" if rollback["verified"] else "REL recovery requires attention")
                    raise
                raise VoltageRelLiveError(
                    getattr(exc, "kind", "verify"), str(exc), attempted=True, rollback=rollback
                ) from exc
