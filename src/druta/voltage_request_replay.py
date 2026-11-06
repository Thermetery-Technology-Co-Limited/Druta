# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Captured rail requests, guarded public replay, and independent readback.

The caller journals before replay and owns recovery. A single SET can partially
apply even when it returns an error. This module never retries a write, guesses
defaults, restarts a device, or predicts firmware policy. Evidence is recorded
in experiments/registry-overrides/vbios-fields/rail-replay-contract.json.
"""

from __future__ import annotations

import copy
import ctypes
import struct
from contextlib import nullcontext

from . import clock_bios_runtime as clock
from . import voltage_bios_runtime as voltage
from . import voltage_ceiling_runtime as ceiling
from . import voltage_rail_control_runtime as controls
from .perf_diagnostics import PerfDiagnosticError, PerfDiagnosticsReader

SET_EXPORT = 0x87C55C8A
INFO_COMMAND, INFO_SIZE, INFO_STRIDE = 0x2080B201, 0x98C, 76
CONTROL_COMMAND, CONTROL_SIZE, CONTROL_STRIDE = 0x2080B213, 0x40C, 32
HEADER = 12
_CONTROL_TYPES = {0: 2, 2: 4, 3: 5}
_KEYS = {
    "schema",
    "identity",
    "info_hex",
    "native_hex",
    "public",
    "rail_identity",
    "boost_percent",
    "complete",
    "missing_indices",
    "policy",
}


class ReplayError(RuntimeError):
    def __init__(self, kind, message, *, attempted=False, result=None):
        super().__init__(message)
        self.kind, self.attempted, self.result = kind, attempted, result


def _fail(message, kind="malformed"):
    raise ReplayError(kind, message)


def _raw(value, size):
    if not isinstance(value, str) or len(value) != size * 2:
        _fail("Rail replay packet geometry is not understood")
    try:
        raw = bytes.fromhex(value)
    except ValueError as exc:
        raise ReplayError("malformed", "Rail replay packet is not hexadecimal") from exc
    if len(raw) != size or raw.hex() != value:
        _fail("Rail replay packet needs canonical bounded hexadecimal")
    return raw


def _mask(raw):
    return struct.unpack_from("<I", raw, 4)[0]


def _indices(mask):
    return {i for i in range(32) if mask & (1 << i)}


def _six(raw, offset):
    return list(struct.unpack_from("<6i", raw, offset + 4))


def validate(saved):
    """Pure bounded validation; retained policy values are historical context."""
    if not isinstance(saved, dict) or set(saved) != _KEYS or type(saved["schema"]) is not int or saved["schema"] != 1:
        _fail("Malformed captured rail replay observation")
    try:
        policy = voltage.validate_policy(saved["policy"], request=True)
        identity = clock._identity(saved["identity"])
    except (ValueError, RuntimeError, TypeError) as exc:
        raise ReplayError("malformed", str(exc)) from exc
    if identity != policy["identity"]:
        _fail("Rail request and policy identities disagree")
    info, native = _raw(saved["info_hex"], INFO_SIZE), _raw(saved["native_hex"], CONTROL_SIZE)
    active = _indices(_mask(info))
    if not active:
        _fail("Rail INFO has no independently present inventory", "unsupported")
    if not isinstance(saved["public"], dict) or not isinstance(saved["rail_identity"], dict):
        _fail("Malformed rail request inventory")
    if not saved["public"] or set(saved["public"]) != set(saved["rail_identity"]):
        _fail("Rail request identity and packet inventories disagree")
    boost = saved["boost_percent"]
    if type(boost) is not int or not 0 <= boost <= 100 or native[8] != boost:
        _fail("Rail replay boost must match its native request byte")
    selected = set()
    for index, text in saved["public"].items():
        if type(index) is not str or index not in ("0", "1") or int(index) not in active:
            _fail("Rail replay contains an unpaired or absent rail")
        row = saved["rail_identity"][index]
        expected = policy["rails"].get(index)
        if not expected or not isinstance(row, dict) or set(row) != {"name", "policy_type", "control_type"}:
            _fail("Rail replay lacks independent rail identity")
        if (
            row["name"] != expected["name"]
            or type(row["policy_type"]) is not int
            or row["policy_type"] != expected["type"]
            or row["policy_type"] != info[HEADER + int(index) * INFO_STRIDE + 0x33]
            or type(row["control_type"]) is not int
            or row["control_type"] not in _CONTROL_TYPES
            or expected["control_type"] != row["control_type"]
        ):
            _fail("Rail replay requires an independently paired control type")
        public_type = row["control_type"]
        native_type = _CONTROL_TYPES[public_type]
        raw = _raw(text, controls.SIZE)
        at, nat = controls.ROW_OFFSET + int(index) * controls.ROW_STRIDE, HEADER + int(index) * CONTROL_STRIDE
        if struct.unpack_from("<II", raw) != (controls.V2, 1 << int(index)):
            _fail("Rail replay requires exact V2 singleton control packets")
        if (
            struct.unpack_from("<I", raw, at)[0] != public_type
            or info[HEADER + int(index) * INFO_STRIDE] != native_type
            or native[nat] != native_type
        ):
            _fail("Rail replay control/INFO native types disagree")
        if raw[8] != boost or _six(raw, at) != _six(native, nat):
            _fail("Rail replay public/native six-offset requests disagree")
        selected.add(int(index))
    if _mask(native) != sum(1 << i for i in selected):
        _fail("Native CONTROL requested mask differs from captured rails")
    missing = sorted(active - selected)
    if (
        not isinstance(saved["missing_indices"], list)
        or any(type(i) is not int for i in saved["missing_indices"])
        or saved["missing_indices"] != missing
        or type(saved["complete"]) is not bool
        or saved["complete"] is not (not missing)
    ):
        _fail("Rail replay completeness disagrees with full INFO inventory")
    return copy.deepcopy(saved)


def project(saved, *, offsets_uv=None, boost_percent=None):
    """Pure request projection; policy status is retained as historical context."""
    state = validate(saved)
    values = {} if offsets_uv is None else offsets_uv
    if not isinstance(values, dict) or any(type(k) is not str or k not in state["public"] for k in values):
        _fail("Replay projection selects an absent captured rail")
    boost = state["boost_percent"] if boost_percent is None else boost_percent
    if type(boost) is not int or not 0 <= boost <= 100:
        _fail("Replay boost must be a whole percentage from0 through100")
    native = bytearray.fromhex(state["native_hex"])
    native[8] = boost
    for index, text in state["public"].items():
        raw = bytearray.fromhex(text)
        at, nat = controls.ROW_OFFSET + int(index) * controls.ROW_STRIDE, HEADER + int(index) * CONTROL_STRIDE
        row = values.get(index, _six(raw, at))
        if (
            not isinstance(row, (list, tuple))
            or len(row) != 6
            or any(type(v) is not int or not -(2**31) <= v < 2**31 for v in row)
        ):
            _fail("Replay requires exactly six signed int32 request words")
        struct.pack_into("<6i", raw, at + 4, *row)
        struct.pack_into("<6i", native, nat + 4, *row)
        raw[8] = boost
        state["public"][index] = raw.hex()
    state["native_hex"], state["boost_percent"] = native.hex(), boost
    return validate(state)


def _native_get(gpu, reader, command, size, mask=None):
    if (command, size) not in ((INFO_COMMAND, INFO_SIZE), (CONTROL_COMMAND, CONTROL_SIZE)):
        _fail("Only the understood read-only rail INFO/CONTROL commands are allowed")
    identity = clock._binding(reader)
    envelope, fields = reader._transport()
    if any(envelope[i] != value for i, value in clock._ENVELOPE.items()) or any(
        type(v) is not int or not 0 <= v <= 0xFFFFFFFF for v in fields.values()
    ):
        raise PerfDiagnosticError("malformed", "Rail replay needs the understood paired RM envelope")
    storage, data = clock._guarded(68 + size)
    packet = (ctypes.c_uint32 * ((68 + size) // 4)).from_buffer(data)
    for i, word in enumerate(envelope):
        packet[i] = word
    packet[2], packet[14], packet[15], packet[16] = 68 + size, command, size, 0
    packet[5] = packet[11] = 0
    expected = tuple(packet[:16])
    if mask is not None:
        struct.pack_into("<I", data, 72, mask)
    try:
        status = gpu._legacy_clk_escape(packet, dict(fields))
    finally:
        clock._guards(storage)
    if clock._binding(reader) != identity:
        raise PerfDiagnosticError("identity", "Rail replay GET changed adapter")
    if any(packet[i] != value for i, value in enumerate(expected) if i not in (5, 11)) or packet[5] not in (0, 1):
        raise PerfDiagnosticError("malformed", "Rail replay GET changed its envelope")
    if type(status) is not int or status or packet[11] != 1:
        raise PerfDiagnosticError("transient", f"Rail replay GET transport failed: {status}")
    if packet[16]:
        raise PerfDiagnosticError(
            "unsupported" if packet[16] in (0x1F, 0x56) else "transient", f"Rail replay GET RM status {packet[16]}"
        )
    raw = bytes(data)[68:]
    if mask is not None and _mask(raw) != mask:
        raise PerfDiagnosticError("malformed", "Native CONTROL changed its requested inventory")
    return raw


def _once(gpu, reader):
    identity = clock._binding(reader)
    info = _native_get(gpu, reader, INFO_COMMAND, INFO_SIZE)
    identities = {i: info[HEADER + i * INFO_STRIDE + 0x33] for i in (0, 1) if i in _indices(_mask(info))}
    policy, error = voltage.read(gpu, request_identities=identities)
    if error:
        raise PerfDiagnosticError("transient", error)
    if policy["identity"] != identity:
        raise PerfDiagnosticError("identity", "Rail replay policy identifies another adapter")
    public, identities = {}, {}
    for index, row in policy["rails"].items():
        if int(index) not in _indices(_mask(info)):
            raise PerfDiagnosticError("malformed", "Policy rail is absent from native INFO")
        native_type = _CONTROL_TYPES.get(row["control_type"])
        if native_type is None or info[HEADER + int(index) * INFO_STRIDE] != native_type:
            continue
        try:
            entry = controls._get(gpu, reader, index, controls.V2, policy)
        except controls.VoltageRailControlError as exc:
            if str(exc) == "Rail CONTROL offsets differ from the captured policy":
                raise PerfDiagnosticError("transient", "Rail requests changed between policy and CONTROL GET") from exc
            raise
        if entry is None:
            continue
        public[index] = entry["payload_hex"]
        identities[index] = {"name": row["name"], "policy_type": row["type"], "control_type": row["control_type"]}
    if not public:
        raise PerfDiagnosticError("unsupported", "No independently paired rail requests")
    native = _native_get(gpu, reader, CONTROL_COMMAND, CONTROL_SIZE, sum(1 << int(i) for i in public))
    boost = ceiling._boost(gpu)
    if boost is None:
        raise PerfDiagnosticError("unsupported", "Independent boost request GET is unavailable")
    if native[8] != boost:
        raise PerfDiagnosticError("transient", "Boost request changed between native and independent GET")
    for index, text in public.items():
        raw = bytes.fromhex(text)
        at, nat = controls.ROW_OFFSET + int(index) * controls.ROW_STRIDE, HEADER + int(index) * CONTROL_STRIDE
        if raw[8] != boost or _six(raw, at) != _six(native, nat):
            raise PerfDiagnosticError("transient", "Rail requests changed between public and native GET")
    if clock._binding(reader) != identity:
        raise PerfDiagnosticError("identity", "Rail replay target changed during capture")
    missing = sorted(_indices(_mask(info)) - {int(i) for i in public})
    return validate(
        {
            "schema": 1,
            "identity": identity,
            "info_hex": info.hex(),
            "native_hex": native.hex(),
            "public": public,
            "rail_identity": identities,
            "boost_percent": boost,
            "complete": not missing,
            "missing_indices": missing,
            "policy": policy,
        }
    )


def _requested(state):
    """Exclude dynamic firmware policy; retain every captured request/opaque byte."""
    return {k: v for k, v in state.items() if k != "policy"}


def _read(gpu, reader):
    last = None
    for _ in range(2):
        try:
            first, second = _once(gpu, reader), _once(gpu, reader)
            if _requested(first) != _requested(second):
                raise PerfDiagnosticError("transient", "Rail requests changed between fresh reads")
            return second
        except PerfDiagnosticError as exc:
            last = exc
            if exc.kind != "transient":
                break
        except ReplayError:
            raise
        except (voltage.VoltageBiosError, controls.VoltageRailControlError) as exc:
            raise ReplayError("malformed", str(exc)) from exc
        except Exception as exc:
            last = PerfDiagnosticError("transient", f"Rail replay capture failed ({type(exc).__name__}): {exc}")
    raise ReplayError(getattr(last, "kind", "transient"), str(last)) from last


def read_checked(gpu) -> dict:
    """Read under the GPU lock, preserving typed capability/failure errors.

    Partial observations remain readable but are never replayable. Ordinary
    capture failures raise ReplayError with the original error category.
    """
    try:
        with getattr(gpu, "_lock", nullcontext()):
            return _read(gpu, PerfDiagnosticsReader(gpu))
    except PerfDiagnosticError as exc:
        raise ReplayError(exc.kind, str(exc)) from exc


def read(gpu):
    """Compatibility wrapper returning observation/error instead of raising."""
    try:
        return read_checked(gpu), None
    except ReplayError as exc:
        return None, str(exc)


def _opaque(state):
    result = project(state, offsets_uv={i: [0] * 6 for i in state["public"]}, boost_percent=0)
    result.pop("policy")
    return result


def _readback_difference(before, desired, after, status):
    """Describe request mismatches without dropping independent metadata failures."""
    # NVIDIA's legacy VOLT_RAIL_CONTROL ABI names all six voltDeltauV entries.
    names = ("reliability", "alternate_reliability", "overvoltage", "minimum_voltage", "vcrit_low", "vcrit_high")
    differences = []
    for index in sorted(set(desired["public"]) & set(after["public"]), key=int):
        at = controls.ROW_OFFSET + int(index) * controls.ROW_STRIDE
        wanted = _six(bytes.fromhex(desired["public"][index]), at)
        actual = _six(bytes.fromhex(after["public"][index]), at)
        prior = _six(bytes.fromhex(before["public"][index]), at) if index in before["public"] else None
        rail = desired["rail_identity"][index]["name"]
        for position, (requested, observed) in enumerate(zip(wanted, actual, strict=True)):
            if requested != observed:
                unchanged = type(status) is int and status == 0 and prior is not None and observed == prior[position]
                suffix = " (SET returned 0; unchanged from before)" if unchanged else ""
                differences.append(
                    f"{rail} {names[position]}: requested {requested} uV, observed {observed} uV{suffix}"
                )
    if desired["boost_percent"] != after["boost_percent"]:
        suffix = (
            " (SET returned 0; unchanged from before)"
            if type(status) is int and status == 0 and after["boost_percent"] == before["boost_percent"]
            else ""
        )
        differences.append(f"boost: requested {desired['boost_percent']}%, observed {after['boost_percent']}%{suffix}")
    if _opaque(after) != _opaque(desired):
        differences.append("routing, INFO, inventory or opaque metadata differs")
    return "Rail replay readback differs: " + "; ".join(differences or ["request metadata differs"])


def _packet(state):
    raw = bytearray(controls.SIZE)
    struct.pack_into("<II", raw, 0, controls.V2, sum(1 << int(i) for i in state["public"]))
    raw[8] = state["boost_percent"]
    for index, text in state["public"].items():
        at = controls.ROW_OFFSET + int(index) * controls.ROW_STRIDE
        raw[at] = state["rail_identity"][index]["control_type"]  # Canonical zero padding.
        raw[at + 4 : at + 28] = bytes.fromhex(text)[at + 4 : at + 28]
    return bytes(raw)


def replay(gpu, saved):
    """Attempt one public SET; caller must journal and own any recovery.

    Returns detailed captures. ReplayError.result is retained for partial or
    uncertain outcomes, including successful SET followed by failed readback.
    The independently captured policy is reported, never predicted or asserted.
    """
    saved = validate(saved)
    if not saved["complete"]:
        _fail("Complete replay needs every active INFO rail independently captured", "unsupported")
    with getattr(gpu, "_lock", nullcontext()):
        reader = PerfDiagnosticsReader(gpu)
        if clock._binding(reader) != saved["identity"]:
            _fail("Saved rail requests identify another adapter", "identity")
        setter = gpu.nvapi._i(SET_EXPORT, ctypes.c_void_p, ctypes.c_void_p)
        if setter is None:
            _fail("Public rail CONTROL SET export is unavailable", "unsupported")
        before = _read(gpu, reader)
        if not before["complete"] or _opaque(before) != _opaque(saved):
            _fail("Current rail inventory, types or opaque state differs from saved requests", "identity")
        offsets = {
            i: _six(bytes.fromhex(raw), controls.ROW_OFFSET + int(i) * controls.ROW_STRIDE)
            for i, raw in saved["public"].items()
        }
        desired = project(before, offsets_uv=offsets, boost_percent=saved["boost_percent"])
        raw = _packet(desired)
        storage, data = clock._guarded(controls.SIZE)
        ctypes.memmove(ctypes.addressof(data), raw, len(raw))
        result = {
            "attempted": False,
            "status": None,
            "before": before,
            "desired": desired,
            "after": None,
            "write_error": None,
            "readback_error": None,
            "verified": False,
        }
        interrupted = None
        if clock._binding(reader) != saved["identity"]:
            _fail("Rail replay target changed before SET", "identity")
        result["attempted"] = True
        try:
            try:
                result["status"] = setter(gpu.nvapi.gpu, ctypes.byref(data))
            finally:
                clock._guards(storage)
            if bytes(data) != raw:
                _fail("Rail CONTROL SET modified its caller-owned request")
            if clock._binding(reader) != saved["identity"]:
                _fail("Rail replay target changed during SET", "identity")
            if type(result["status"]) is not int or result["status"] != 0:
                _fail(f"Rail CONTROL SET returned {result['status']}; changes may be partial", "write")
        except BaseException as exc:
            result["write_error"] = f"{type(exc).__name__}: {exc}"
            if not isinstance(exc, Exception):
                interrupted = exc
        try:
            if clock._binding(reader) != saved["identity"]:
                _fail("Rail replay readback target changed", "identity")
            result["after"] = _read(gpu, reader)
            if _requested(result["after"]) != _requested(desired):
                _fail(_readback_difference(before, desired, result["after"], result["status"]), "readback")
            result["verified"] = not result["write_error"]
        except Exception as exc:
            result["readback_error"] = f"{type(exc).__name__}: {exc}"
        if interrupted is not None:
            interrupted.attempted = True
            interrupted.result = result
            raise interrupted
        if not result["verified"]:
            raise ReplayError(
                "partial",
                "; ".join(error for error in (result["write_error"], result["readback_error"]) if error),
                attempted=True,
                result=result,
            )
        return result
