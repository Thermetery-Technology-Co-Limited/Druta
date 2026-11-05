# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Session-only power-policy integral bounds, with exact readback."""

import struct
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext

from . import power_arbitration
from .perf_diagnostics import PerfDiagnosticsReader

INFO, STATUS, CONTROL, SET = 0x2080A618, 0x2080A619, 0x2080A61A, 0x2080E61B


class PowerIntegralError(RuntimeError):
    def __init__(self, kind, message, *, rollback=None):
        super().__init__(message)
        self.kind, self.rollback = kind, rollback


def percent_to_raw(text):
    try:
        value = Decimal(str(text))
        if not value.is_finite() or value < 0 or value > 1600:
            raise ValueError
        with localcontext() as context:
            context.prec = max(28, len(value.as_tuple().digits) + 20)
            raw = int((value * Decimal(4096) / Decimal(100)).to_integral_value(rounding=ROUND_HALF_UP))
        if not 0 <= raw <= 65535:
            raise ValueError
        return raw
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise ValueError("Percentage must be finite and fit the unsigned ratio") from exc


def raw_to_percent(raw):
    if type(raw) is not int or not 0 <= raw <= 65535:
        raise ValueError("Ratio must be an unsigned 16-bit integer")
    return format(Decimal(raw) * Decimal(100) / Decimal(4096), "f")


def _bytes(words, size):
    if (
        not isinstance(words, (list, tuple))
        or len(words) != size // 4
        or any(type(x) is not int or not 0 <= x <= 0xFFFFFFFF for x in words)
    ):
        raise PowerIntegralError("malformed", "Policy packet has invalid size or words")
    return struct.pack(f"<{len(words)}I", *words)


class PowerIntegralController:
    def __init__(self, gpu):
        self.gpu = gpu
        self._reader = PerfDiagnosticsReader(gpu)

    def _binding(self):
        try:
            api, slot, arch = self._reader._binding()
            return {
                "slot": slot,
                "architecture": arch,
                "device_id": api.selected.get("devid"),
                "subsystem_id": api.selected.get("subsys"),
            }
        except Exception as exc:
            raise PowerIntegralError(getattr(exc, "kind", "transient"), str(exc)) from exc

    def _capture_once(self):
        identity = self._binding()
        layout = self.gpu._current_limit_abi()
        try:
            power_arbitration._geometry(layout)
            if layout["sizes"].get(SET) != 13876 or layout.get("commands", {}).get(SET, SET) != SET:
                raise ValueError("SET geometry is not understood")
        except (ValueError, KeyError, TypeError) as exc:
            raise PowerIntegralError("unsupported", str(exc)) from exc
        info = self.gpu._current_limit_rm(INFO)
        ib = _bytes(info, 20512)
        mask = info[1]
        control = self.gpu._current_limit_rm(CONTROL, policy_mask=mask) if mask else None
        cb = _bytes(control, 13876) if mask else None
        if mask and control[:5] != [0, 0, 0, 255, mask]:
            raise PowerIntegralError("malformed", "Policy CONTROL header does not match INFO")
        self.gpu._power_policy_mask = mask
        if self._binding() != identity:
            raise PowerIntegralError("identity", "GPU binding changed during policy GET")
        return identity, layout, info, control, ib, cb

    def _capture(self):
        for attempt in range(2):
            try:
                return self._capture_once()
            except Exception as exc:
                kind = getattr(exc, "kind", "transient")
                if kind != "transient" or attempt:
                    if isinstance(exc, PowerIntegralError):
                        raise
                    raise PowerIntegralError(kind, str(exc)) from exc
                self.gpu._current_limit_transport = None
                self.gpu._current_limit_layout_choice = None

    def _report(self, state):
        identity, layout, info, control, ib, cb = state
        mask = info[1]
        decoded = None
        if mask:
            try:
                status = self.gpu._current_limit_rm(STATUS, policy_mask=mask)
                decoded = power_arbitration.decode(layout, info, control, status)
                self._binding()
            except Exception as exc:
                if getattr(exc, "kind", None) == "identity":
                    raise
        diagnostics = {r["policy"]: r for r in (decoded or {}).get("policies", [])}
        rows = []
        for i in range(32):
            if not mask >> i & 1:
                continue
            a, b = 0xCC + i * 0x104, 0x14 + i * 0xC4
            kind, channel, unitcode = ib[a + 4 : a + 7]
            mode, past, nxt = cb[b + 8 : b + 11]
            default_mode, default_past, default_next = ib[a + 0x20], ib[a + 0x22], ib[a + 0x23]
            lower, upper = struct.unpack_from("<HH", cb, b + 12)
            dlow, dup = struct.unpack_from("<HH", ib, a + 0x24)
            minimum, maximum = struct.unpack_from("<I", ib, a + 8)[0], struct.unpack_from("<I", ib, a + 16)[0]
            request = struct.unpack_from("<I", cb, b + 4)[0]
            diag = diagnostics.get(i, {})
            unit = {0: "mW", 1: "mA"}.get(unitcode)
            reason = None
            if ib[a] != 0x40 or struct.unpack_from("<I", cb, b)[0] != kind or (diag and not diag.get("available")):
                reason = "Policy types or V4 format are not understood"
            elif unit is None:
                reason = "Policy unit is not understood"
            elif mode != 2 or not past or not nxt or default_mode != 2 or not default_past or not default_next:
                reason = "Integral mode or sample counts are not understood"
            elif lower > upper or not upper or dlow > dup or not dup:
                reason = "Integral bounds are inconsistent"
            elif not minimum <= request <= maximum or minimum > maximum:
                reason = "Ordinary policy request is outside its reported range"
            pmu = diag.get("pmu", {})
            rows.append(
                {
                    "policy": i,
                    "type": kind,
                    "channel": channel,
                    "unit": unit,
                    "available": reason is None,
                    "reason": reason,
                    "lower_raw": lower,
                    "upper_raw": upper,
                    "default_lower_raw": dlow,
                    "default_upper_raw": dup,
                    "past_samples": past,
                    "next_samples": nxt,
                    "default_past_samples": default_past,
                    "default_next_samples": default_next,
                    "requested": request,
                    "effective": diag.get("effective"),
                    "pmu_output": pmu.get("output") if pmu.get("available") else None,
                }
            )
        return {"identity": identity, "policies": rows}

    def read(self):
        with self.gpu._lock:
            return self._report(self._capture())

    def set_upper(self, policy, raw, *, expected_identity=None):
        return self._set_ratio(policy, raw, "upper", expected_identity)

    def set_lower(self, policy, raw, *, expected_identity=None):
        return self._set_ratio(policy, raw, "lower", expected_identity)

    def _set_ratio(self, policy, raw, field, expected_identity):
        if field not in ("lower", "upper"):
            raise ValueError("Unsupported ratio field")
        if type(policy) is not int or not 0 <= policy < 32 or type(raw) is not int or not 0 <= raw <= 65535:
            raise PowerIntegralError("invalid", "Policy and ratio must be valid unsigned integers")
        if field == "upper" and raw == 0:
            raise PowerIntegralError("invalid", "Upper ratio must be positive")
        key = f"{field}_raw"
        offset = 12 if field == "lower" else 14
        with self.gpu._lock:
            before = self._capture()
            report = self._report(before)
            if expected_identity is not None and report["identity"] != expected_identity:
                raise PowerIntegralError("identity", "Selected GPU identity changed")
            row = next((r for r in report["policies"] if r["policy"] == policy), None)
            if row is None or not row["available"]:
                raise PowerIntegralError("unsupported", row["reason"] if row else "Policy is absent")
            if field == "upper" and raw < row["lower_raw"]:
                raise PowerIntegralError("invalid", "Upper ratio must be at least the active lower ratio")
            if field == "lower" and raw > row["upper_raw"]:
                raise PowerIntegralError("invalid", "Lower ratio must not exceed the active upper ratio")
            if raw == row[key]:
                return report
            projected = bytearray(before[5])
            struct.pack_into("<H", projected, 0x14 + policy * 0xC4 + offset, raw)
            request = list(struct.unpack(f"<{len(projected) // 4}I", projected))
            request[4] = 1 << policy
            if self._binding() != before[0]:
                raise PowerIntegralError("identity", "GPU binding changed before SET")
            try:
                self.gpu._current_limit_rm(SET, request, any_live_policy=True)
                after = self._capture()
                if after[0] != before[0] or after[4] != before[4] or after[5] != bytes(projected):
                    raise PowerIntegralError("readback", "Policy INFO or CONTROL did not read back exactly")
                return self._report(after)
            except BaseException as exc:
                rollback = {"attempted": False, "verified": False, "error": None}
                self.gpu._current_limit_transport = None
                self.gpu._current_limit_layout_choice = None
                try:
                    current = self._capture()
                    if current[0] != before[0] or not current[1] or not current[2][1] >> policy & 1:
                        raise PowerIntegralError("identity", "Original policy is no longer bound")
                    restore = bytearray(current[5])
                    struct.pack_into("<H", restore, 0x14 + policy * 0xC4 + offset, row[key])
                    words = list(struct.unpack(f"<{len(restore) // 4}I", restore))
                    words[4] = 1 << policy
                    rollback["attempted"] = True
                    self.gpu._current_limit_rm(SET, words, any_live_policy=True)
                    back = self._capture()
                    rollback["verified"] = back[0] == current[0] and back[4] == current[4] and back[5] == bytes(restore)
                    if not rollback["verified"]:
                        raise PowerIntegralError("readback", "Restored request did not read back exactly")
                except BaseException as restore_exc:
                    rollback["error"] = str(restore_exc)
                detail = (
                    f"Original {field} restored and verified"
                    if rollback["verified"]
                    else f"Original {field} restoration was not verified"
                )
                if not isinstance(exc, Exception):
                    exc.add_note(detail)
                    raise
                raise PowerIntegralError(
                    getattr(exc, "kind", "transient"), f"{exc}. {detail}", rollback=rollback
                ) from exc
