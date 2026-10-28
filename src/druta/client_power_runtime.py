# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Live client power requests with exact readback and compensating restoration.

E633 changes an existing FE current-arbitration request. INFO limits describe
the ordinary range; they are not this call's admission bounds. Downstream PMU
votes and effective limits can differ from the accepted request. No registry,
persistence, GPU restart or workload operation is performed here.
"""

from __future__ import annotations

import copy
import ctypes
import struct
from dataclasses import dataclass

from . import clock_bios_runtime as clock
from . import power_arbitration
from .perf_diagnostics import PerfDiagnosticError, PerfDiagnosticsReader

INFO, CONTROL, SET = 0x2080A630, 0x2080A632, 0x2080E633
ROLE_INFO, NATIVE_INFO, NATIVE_CONTROL, NATIVE_STATUS = 0x2080A61C, 0x2080A618, 0x2080A61A, 0x2080A619
SIZES = {
    INFO: 0x924,
    CONTROL: 0x328,
    SET: 0x328,
    ROLE_INFO: 0x108,
    NATIVE_INFO: 20512,
    NATIVE_CONTROL: 13876,
    NATIVE_STATUS: 397048,
}
LEGACY_SIZES = {
    INFO: 1160, CONTROL: 392, SET: 392, ROLE_INFO: 232,
    NATIVE_INFO: 8632, NATIVE_CONTROL: 6356, NATIVE_STATUS: 190272,
}
# The smaller client group can accompany either native policy ABI. Select
# with read-only INFO acceptance, never a device or driver version.
TURING_SIZES = {
    **LEGACY_SIZES,
    NATIVE_INFO: 8620, NATIVE_CONTROL: 4432, NATIVE_STATUS: 172080,
}
RM26_SIZES = {
    INFO: 644, CONTROL: 388, SET: 388, ROLE_INFO: 120,
    NATIVE_INFO: 5008, NATIVE_CONTROL: 2108, NATIVE_STATUS: 14148,
}
RM26_EXTENDED_SIZES = {
    INFO: 900, CONTROL: 388, SET: 388, ROLE_INFO: 136,
    NATIVE_INFO: 5520, NATIVE_CONTROL: 2124, NATIVE_STATUS: 69148,
}
_RM26_COMMANDS = dict(zip(
    (INFO, CONTROL, SET, ROLE_INFO, NATIVE_INFO, NATIVE_CONTROL, NATIVE_STATUS),
    (0x20802630, 0x20802632, 0x20802633, 0x2080261C, 0x20802618, 0x2080261A, 0x20802619),
    strict=True,
))
INFO_BASE, INFO_STRIDE, CONTROL_BASE, CONTROL_STRIDE, CAPACITY = 0x24, 0x24, 0x28, 0xC, 64
NATIVE_INFO_BASE, NATIVE_INFO_STRIDE = 0xCC, 0x104
NATIVE_CONTROL_BASE, NATIVE_CONTROL_STRIDE = 0x14, 0xC4
# The understood native interface exposes one u32 occupancy mask. Buffer tails
# are preserved; their size must not be mistaken for extra addressable rows.
NATIVE_CAPACITY = min(
    32,
    (SIZES[NATIVE_INFO] - NATIVE_INFO_BASE) // NATIVE_INFO_STRIDE,
    (SIZES[NATIVE_CONTROL] - NATIVE_CONTROL_BASE) // NATIVE_CONTROL_STRIDE,
    (SIZES[NATIVE_STATUS] - 0x9C) // 0x1730,
)
ROLE, CLIENT, MISSING = 1, 0xFE, 0xFFFFFFFF
_LAYOUT = {
    "info": (NATIVE_INFO_BASE, NATIVE_INFO_STRIDE),
    "control": (NATIVE_CONTROL_BASE, NATIVE_CONTROL_STRIDE),
    "status": (0x9C, 0x1730),
    "sizes": {k: SIZES[k] for k in (NATIVE_INFO, NATIVE_CONTROL, NATIVE_STATUS)},
}
_STATE_KEYS = (
    "schema",
    "stable_reads",
    "identity",
    "binding",
    "requested_mw",
    "info_hex",
    "control_hex",
    "role_info_hex",
    "native_info_hex",
    "native_control_hex",
)


class ClientPowerError(RuntimeError):
    """A typed failure; rollback is None when no SET was dispatched."""

    def __init__(self, kind, message, rollback=None, rm_status=None):
        super().__init__(message)
        self.kind, self.rollback = kind, rollback
        self.rm_status = rm_status


@dataclass
class _Write:
    dispatched: bool = False


def _u32(raw, offset):
    return struct.unpack_from("<I", raw, offset)[0]


def _words(raw):
    return list(struct.unpack(f"<{len(raw) // 4}I", raw))


def _state(observation):
    return {key: observation[key] for key in _STATE_KEYS}


def _small_native_geometry(binding):
    """Native control size/stride selected by the accepted INFO geometry."""
    if binding.get("native_layout") == "policy124":
        return TURING_SIZES[NATIVE_CONTROL], 124
    return LEGACY_SIZES[NATIVE_CONTROL], 184


def _native_geometry(binding):
    """INFO base/stride and CONTROL base/stride/size for a captured binding."""
    if binding.get("layout") == "rm26":
        if binding.get("native_layout") == "rm26_extended":
            return 60, 132, 20, 52, RM26_EXTENDED_SIZES[NATIVE_CONTROL]
        return 52, 116, 4, 52, RM26_SIZES[NATIVE_CONTROL]
    if binding.get("layout") == "legacy32":
        size, stride = _small_native_geometry(binding)
        return 88, 228, 20, stride, size
    return NATIVE_INFO_BASE, NATIVE_INFO_STRIDE, NATIVE_CONTROL_BASE, NATIVE_CONTROL_STRIDE, SIZES[NATIVE_CONTROL]


def _coupled_requests(observation):
    # v30 firmware policies recalculate peer votes in both RM packet families.
    # Their table version is independent of the client/native buffer sizes.
    return bytes.fromhex(observation["role_info_hex"])[4] == 0x30


def _project(before, mw):
    result = copy.deepcopy(_state(before))
    result["requested_mw"] = mw
    legacy = before["binding"].get("layout") == "legacy32"
    _, _, native_base, native_stride, _ = _native_geometry(before["binding"])
    control_base = 4 if before["binding"].get("layout") == "rm26" else 8 if legacy else CONTROL_BASE
    for name, base, stride, index in (
        ("control_hex", control_base, CONTROL_STRIDE, before["binding"]["client_index"]),
        ("native_control_hex", native_base, native_stride, before["binding"]["native_index"]),
    ):
        raw = bytearray.fromhex(before[name])
        struct.pack_into("<I", raw, base + index * stride + 4, mw)
        result[name] = raw.hex()
    return result


def _matches(expected, observed):
    """v30 board writes may recalculate occupied peers' request words."""
    actual = _state(observed)
    if not _coupled_requests(expected):
        return actual == expected
    if any(actual[k] != expected[k] for k in _STATE_KEYS if k != "native_control_hex"):
        return False
    old, new = bytes.fromhex(expected["native_control_hex"]), bytes.fromhex(actual["native_control_hex"])
    info = bytes.fromhex(expected["native_info_hex"])
    info_base, info_stride, control_base, stride, size = _native_geometry(expected["binding"])
    if len(old) != len(new) or len(old) != size:
        return False
    mask = _u32(info, 4)
    selected = expected["binding"]["native_index"]
    allowed = set()
    for i in range(32):
        if not mask >> i & 1 or i == selected:
            continue
        at, row = control_base + i * stride + 4, info_base + i * info_stride
        if old[at:at + 4] == new[at:at + 4]:
            continue
        minimum, rated, maximum = struct.unpack_from("<III", info, row + 8)
        if (info[row] != 0x30 or info[row + 6] not in (0, 1)
                or _u32(old, at - 4) != info[row + 4]
                or not minimum <= rated <= maximum or maximum == 0
                or _u32(old, at) == MISSING or _u32(new, at) == MISSING):
            return False
        allowed.update(range(at, at + 4))
    return all(a == b or i in allowed for i, (a, b) in enumerate(zip(old, new, strict=True)))


class ClientPowerController:
    """One sticky PCI-paired GPU; construct a new controller after restart."""

    def __init__(self, gpu):
        self.gpu = gpu
        self._reader = PerfDiagnosticsReader(gpu, attempts=2)
        self.sizes = SIZES
        self._legacy = False
        self._native_policy124 = False
        self._rm26 = False
        self._rm26_extended = False
        self._transport_key = None

    def _reset_transport(self):
        self.gpu._current_limit_transport = None
        self.gpu._current_limit_layout_choice = None

    def _binding(self):
        try:
            api, slot, architecture = self._reader._binding()
        except PerfDiagnosticError as exc:
            raise ClientPowerError(exc.kind, str(exc)) from exc
        except Exception as exc:
            raise ClientPowerError("transient", f"GPU binding could not be read: {exc}") from exc
        for key, maximum in (("devid", 0xFFFF), ("subsys", 0xFFFFFFFF)):
            if type(api.selected.get(key)) is not int or not 0 <= api.selected[key] <= maximum:
                raise ClientPowerError("identity", "Client power needs a complete paired PCI identity")
        return {
            "slot": slot,
            "architecture": architecture,
            "device_id": api.selected["devid"],
            "subsystem_id": api.selected["subsys"],
        }

    def _transport(self):
        identity = self._binding()
        try:
            self.gpu._current_limit_transport_layouts()
            header, fields = self.gpu._current_limit_transport
            if (not isinstance(header, (tuple, list)) or len(header) != 17
                    or any(type(v) is not int or not 0 <= v <= 0xFFFFFFFF for v in header)
                    or header[2] != 68 + header[15]
                    or not isinstance(fields, dict)
                    or set(fields) != {"hAdapter", "hDevice", "Type", "Flags", "hContext"}):
                raise ClientPowerError("malformed", "Client-power transport geometry is not understood")
            key = header[14], header[2], header[15]
            if self._transport_key is not None and key != self._transport_key:
                raise ClientPowerError("identity", "Client-power transport changed; create a fresh controller")
            if key in ((0x20802612, 2524, 2456), (0x20802612, 3548, 3480)):
                if identity["architecture"] != 6:
                    raise ClientPowerError("unsupported", "This client-power RM26 ABI requires Turing")
                self._rm26_extended = key == (0x20802612, 3548, 3480)
                self.sizes = RM26_EXTENDED_SIZES if self._rm26_extended else RM26_SIZES
                self._rm26 = True
            else:
                if self._rm26:
                    raise ClientPowerError("identity", "Client-power transport changed; create a fresh controller")
                if header[14] != 0x2080A612:
                    raise ClientPowerError("unsupported", "Client-power command family is not understood")
        except ClientPowerError:
            raise
        except PerfDiagnosticError as exc:
            raise ClientPowerError(exc.kind, str(exc)) from exc
        except ValueError as exc:
            from .nvbackend import CurrentLimitUnsupported

            kind = "unsupported" if isinstance(exc, CurrentLimitUnsupported) else "transient"
            raise ClientPowerError(kind, str(exc)) from exc
        except Exception as exc:
            raise ClientPowerError("transient", f"Paired power transport could not be captured: {exc}") from exc
        if self._binding() != identity:
            raise ClientPowerError("identity", "Client-power target changed during transport capture")
        if any(type(v) is not int or not 0 <= v <= 0xFFFFFFFF for v in fields.values()) or any(
            header[i] != value for i, value in clock._ENVELOPE.items()
        ):
            raise ClientPowerError("malformed", "Client-power transport envelope is not understood")
        self._transport_key = key
        return header, fields

    def _exchange(self, command, payload, *, write=None):
        """One exact command, including one dispatch only for any SET."""
        if command not in self.sizes or len(payload) != self.sizes[command] or (command == SET) != isinstance(write, _Write):
            raise ClientPowerError("malformed", "Client-power command or packet geometry is not understood")
        identity = self._binding()
        header, fields = self._transport()
        size = self.sizes[command]
        storage, buffer = clock._guarded(68 + size)
        packet = (ctypes.c_uint32 * ((68 + size) // 4)).from_buffer(buffer)
        for index, value in enumerate(header):
            packet[index] = value
        wire_command = _RM26_COMMANDS[command] if self._rm26 else command
        packet[2], packet[14], packet[15], packet[16] = 68 + size, wire_command, size, 0
        packet[5] = packet[11] = 0
        buffer[68:] = payload
        expected = tuple(packet[:16])
        if write is not None:
            write.dispatched = True
        try:
            status = self.gpu._legacy_clk_escape(packet, dict(fields))
        except Exception as exc:
            raise ClientPowerError("transient", f"Client-power transport failed: {exc}") from exc
        try:
            clock._guards(storage)
        except PerfDiagnosticError as exc:
            raise ClientPowerError("malformed", str(exc)) from exc
        if self._binding() != identity:
            raise ClientPowerError("identity", "Client-power target changed during the operation")
        if any(packet[i] != value for i, value in enumerate(expected) if i not in (5, 11)) or packet[5] not in (0, 1):
            raise ClientPowerError("malformed", "Client-power response changed its transport envelope")
        if type(status) is not int or status != 0 or packet[11] != 1:
            raise ClientPowerError("transient", f"Client-power escape failed (NT {status!r}, completion {packet[11]})")
        if packet[16] != 0:
            kind = "unsupported" if packet[16] in (0x1F, 0x56, 0x40) else "transient"
            raise ClientPowerError(kind, f"Client-power command {wire_command:#x} rejected (RM {packet[16]:#x})", rm_status=packet[16])
        result = bytes(buffer)[68:]
        if command == SET and result != payload:
            raise ClientPowerError("malformed", "Client-power SET changed the requested packet")
        return result

    def _metadata_once(self):
        identity = self._binding()
        self._transport()  # Select the command family before allocating INFO.
        try:
            info = self._exchange(INFO, bytes(self.sizes[INFO]))
        except ClientPowerError as exc:
            if self._legacy or self._rm26 or identity["architecture"] not in (6, 7) or exc.rm_status != 0x1F:
                raise
            self.sizes, self._legacy = LEGACY_SIZES, True
            info = self._exchange(INFO, bytes(self.sizes[INFO]))
        base, info_stride, capacity = (4, 28 if self._rm26_extended else 20, 32) if self._rm26 else (8, INFO_STRIDE, 32) if self._legacy else (INFO_BASE, INFO_STRIDE, CAPACITY)
        native_base, native_stride = (60, 132) if self._rm26_extended else (52, 116) if self._rm26 else (88, 228) if self._legacy else (NATIVE_INFO_BASE, NATIVE_INFO_STRIDE)
        mask = int.from_bytes(info[:4] if self._rm26 else info[4:base], "little")
        if mask >> capacity:
            raise ClientPowerError("malformed", "Client-power inventory exceeds the understood row capacity")
        candidates = [i for i in range(capacity) if mask >> i & 1 and info[base + i * info_stride + 2] == 1]
        if len(candidates) != 1:
            raise ClientPowerError("unsupported", "No unique role1 client-power policy is available")
        index = candidates[0]
        at = base + index * info_stride
        row = info[at : at + info_stride]
        if row[0] != row[1]:
            raise ClientPowerError("malformed", "Selected client INFO type discriminants disagree")
        if row[3] != 0:
            raise ClientPowerError("unsupported", "Selected client-power policy does not report milliwatts")
        limits = struct.unpack_from("<III", row, 4)
        if not limits[0] <= limits[1] <= limits[2]:
            raise ClientPowerError("malformed", "Client-power ordinary limits are inconsistent")
        role_request = bytes([ROLE]) + bytes(self.sizes[ROLE_INFO] - 1)
        role = self._exchange(ROLE_INFO, role_request)
        if role[:4] != role_request[:4]:
            raise ClientPowerError("malformed", "Power-role INFO changed its semantic selector")
        try:
            native_info = self._exchange(NATIVE_INFO, bytes(self.sizes[NATIVE_INFO]))
        except ClientPowerError as exc:
            if not self._legacy or self._native_policy124 or exc.rm_status != 0x1F:
                raise
            self.sizes, self._native_policy124 = TURING_SIZES, True
            native_info = self._exchange(NATIVE_INFO, bytes(self.sizes[NATIVE_INFO]))
        native_mask = _u32(native_info, 4)
        matches = [
            i
            for i in range(NATIVE_CAPACITY)
            if native_mask >> i & 1
            and native_info[native_base + i * native_stride : native_base + (i + 1) * native_stride]
            == role[4:]
        ]
        if len(matches) != 1:
            raise ClientPowerError("malformed", "Role1 INFO does not uniquely bind a current native policy")
        native_index = matches[0]
        source = role[4:]
        # This byte describes the firmware policy table, not the RM packet
        # capacity. A larger native packet can carry a v30 policy unchanged.
        table_versions = (0x30,) if self._legacy or self._rm26 else (0x30, 0x40)
        if source[0] not in table_versions or source[6] != row[3] or struct.unpack_from("<III", source, 8) != limits:
            raise ClientPowerError("malformed", "Client INFO differs from the bound native policy definition")
        if self._binding() != identity:
            raise ClientPowerError("identity", "Client-power target changed during metadata capture")
        return {
            "schema": 1,
            "stable_reads": 2,
            "identity": identity,
            "binding": {
                "client_index": index,
                "native_index": native_index,
                "semantic_role": ROLE,
                "client": CLIENT,
                "units": "mW",
                "minimum_mw": limits[0],
                "rated_mw": limits[1],
                "maximum_mw": limits[2],
                **({"layout": "legacy32"} if self._legacy else {}),
                **({"layout": "rm26"} if self._rm26 else {}),
                **({"native_layout": "rm26_extended"} if self._rm26_extended else {}),
                **({"native_layout": "policy124"} if self._native_policy124 else {}),
            },
            "info_hex": info.hex(),
            "role_info_hex": role.hex(),
            "native_info_hex": native_info.hex(),
        }

    def _once(self):
        metadata = self._metadata_once()
        info = bytes.fromhex(metadata["info_hex"])
        native_info = bytes.fromhex(metadata["native_info_hex"])
        source = bytes.fromhex(metadata["role_info_hex"])[4:]
        index, native_index = metadata["binding"]["client_index"], metadata["binding"]["native_index"]
        base, info_stride, control_base = (4, 28 if self._rm26_extended else 20, 4) if self._rm26 else (8, INFO_STRIDE, 8) if self._legacy else (INFO_BASE, INFO_STRIDE, CONTROL_BASE)
        _, _, native_base, native_stride, _ = _native_geometry(metadata["binding"])
        row = info[base + index * info_stride : base + (index + 1) * info_stride]
        request = bytearray(self.sizes[CONTROL])
        if self._rm26:
            struct.pack_into("<I", request, 0, 1 << index)
        else:
            request[:base] = info[:base]
            request[4:base] = (1 << index).to_bytes(base - 4, "little")
        cp = control_base + index * CONTROL_STRIDE
        request[cp : cp + 2], request[cp + 8] = row[:2], CLIENT
        control = self._exchange(CONTROL, request)
        if ((control[:4] != request[:4]) if self._rm26 else
                (control[0] != info[0] or control[4:base] != request[4:base])):
            raise ClientPowerError("malformed", "Client CONTROL group or selected mask differs")
        if control[cp : cp + 2] != row[:2] or control[cp + 8] != CLIENT:
            raise ClientPowerError("malformed", "Client CONTROL changed the selected row type or client")
        wanted = _u32(control, cp + 4)
        if wanted == MISSING:
            raise ClientPowerError("unsupported", "The selected FE client has no existing restorable request")
        if wanted == 0:
            raise ClientPowerError("unsupported", "A zero FE input has uncharacterized removal semantics")
        native_mask = _u32(native_info, 4)
        request = bytearray(self.sizes[NATIVE_CONTROL])
        short_rm26 = self._rm26 and not self._rm26_extended
        struct.pack_into("<I", request, 0 if short_rm26 else 16, native_mask)
        native_control = self._exchange(NATIVE_CONTROL, request)
        if ((struct.unpack_from("<I", native_control) != (native_mask,)) if short_rm26 else
                (struct.unpack_from("<5I", native_control) != (0, 0, 0, 255, native_mask))):
            raise ClientPowerError("malformed", "Native CONTROL header or inventory differs from INFO")
        np = native_base + native_index * native_stride
        if _u32(native_control, np) != source[4]:
            raise ClientPowerError("malformed", "Bound native policy CONTROL type differs from INFO")
        if _u32(native_control, np + 4) != wanted:
            raise ClientPowerError("unstable", "Client and native current requests disagree")
        if self._binding() != metadata["identity"]:
            raise ClientPowerError("identity", "Client-power target changed during capture")
        return {
            **metadata,
            "requested_mw": wanted,
            "control_hex": control.hex(),
            "native_control_hex": native_control.hex(),
        }

    def _arbitration(self, observation):
        if self._legacy or self._rm26:
            return None, "Power arbitration diagnostics are unavailable for this policy layout"
        info, control = bytes.fromhex(observation["native_info_hex"]), bytes.fromhex(observation["native_control_hex"])
        request = bytearray(SIZES[NATIVE_STATUS])
        request[4:8] = info[4:8]
        error = None
        for attempt in range(2):
            try:
                status = self._exchange(NATIVE_STATUS, request)
                decoded = power_arbitration.decode(_LAYOUT, _words(info), _words(control), _words(status))
                if not decoded["available"]:
                    return None, decoded["reason"]
                return decoded, None
            except ClientPowerError as exc:
                # An identity change invalidates the binding, even when it was
                # discovered during optional telemetry. Other STATUS failures
                # remain diagnostic and cannot remove the established request.
                if exc.kind == "identity":
                    raise
                error = str(exc)
                if exc.kind != "transient":
                    break
                self._reset_transport()
                if attempt == 1:
                    break
        return None, error

    def _stable_locked(self, capture):
        for attempt in range(2):
            try:
                first, second = capture(), capture()
                if first != second:
                    raise ClientPowerError(
                        "unstable", "Client-power definitions or requests changed between fresh reads"
                    )
                return second
            except ClientPowerError as exc:
                if exc.kind != "transient":
                    raise
                self._reset_transport()
                if attempt == 1:
                    raise
        raise AssertionError("Bounded client-power read did not return or fail")

    def _read_locked(self):
        state = self._stable_locked(self._once)
        arbitration, error = self._arbitration(state)
        return {**state, "arbitration": arbitration, "arbitration_error": error}

    def read_metadata(self):
        """Two stable INFO captures, independent of current-request eligibility."""
        with self.gpu._lock:
            return self._stable_locked(self._metadata_once)

    def read(self):
        """Two stable definition/request captures; STATUS is optional."""
        with self.gpu._lock:
            return self._read_locked()

    def _rollback(self, before):
        write = _Write()
        result = {"attempted": False, "verified": False, "error": None, "readback": None}
        errors = []
        try:
            self._exchange(SET, bytes.fromhex(before["control_hex"]), write=write)
        except BaseException as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
        result["attempted"] = write.dispatched
        if write.dispatched:
            try:
                result["readback"] = self._read_locked()
                if _coupled_requests(before) and _matches(_state(before), result["readback"]):
                    # Returning the board request can recalculate peer requests.
                    # Restore only peers that differ from the captured baseline.
                    original = bytes.fromhex(before["native_control_hex"])
                    current = bytes.fromhex(result["readback"]["native_control_hex"])
                    mask = _u32(bytes.fromhex(before["native_info_hex"]), 4)
                    selected = before["binding"]["native_index"]
                    for index in range(32):
                        _, _, base, stride, _ = _native_geometry(before["binding"])
                        at = base + index * stride + 4
                        if index == selected or not mask >> index & 1 or original[at:at + 4] == current[at:at + 4]:
                            continue
                        if self._binding() != before["identity"]:
                            raise ClientPowerError("identity", "GPU binding changed before peer restoration")
                        ok, message = self.gpu._write_policy_limit(index, _u32(original, at))
                        if not ok:
                            raise ClientPowerError("readback", f"Policy {index} restoration failed: {message}")
                    result["readback"] = self._read_locked()
                if _state(result["readback"]) != _state(before):
                    raise ClientPowerError(
                        "readback", "Original client-power definitions and requests were not restored"
                    )
                result["verified"] = not errors
            except BaseException as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
        result["error"] = "; ".join(errors) or None
        return result

    def set_request(self, mw):
        """Write once, verify the exact projection, compensate once on failure."""
        if type(mw) is not int or not 0 < mw < MISSING:
            raise ClientPowerError(
                "invalid", "Power request must be positive whole uint32 milliwatts below the missing-client sentinel"
            )
        with self.gpu._lock:
            before = self._read_locked()
            expected = _project(before, mw)
            write = _Write()
            try:
                self._exchange(SET, bytes.fromhex(expected["control_hex"]), write=write)
                after = self._read_locked()
                if not _matches(expected, after):
                    raise ClientPowerError(
                        "readback", "Client-power request or preserved definitions did not read back exactly"
                    )
                return {"before": before, "after": after}
            except BaseException as exc:
                if not write.dispatched:
                    raise
                self._reset_transport()
                rollback = self._rollback(before)
                detail = (
                    "Original request restored and verified"
                    if rollback["verified"]
                    else "Original request restoration was not verified"
                )
                if not isinstance(exc, Exception):
                    exc.add_note(detail)
                    exc.rollback = rollback
                    raise
                raise ClientPowerError(getattr(exc, "kind", "transient"), f"{exc}. {detail}", rollback) from exc
