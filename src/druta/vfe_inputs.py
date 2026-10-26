"""Read-only NVIDIA VFE sensed-fuse inputs.

The native getter contract is independently traced in
experiments/registry-overrides/speedo-iddq/findings.json. These are private
interfaces: the exact version echo, mask, typed rows and target are checked on
every read. No driver or board identity is used as a compatibility gate.

Speedo and IDDQ are model inputs, not measured power or OCP thresholds. IDDQ
physical units remain unresolved. Direct evaluated substitution was measured
on one configuration; callers must verify the exact active request on each
adapter rather than assume firmware correction order.
"""

from __future__ import annotations

import ctypes
import hashlib
import math
import struct
import time
from contextlib import nullcontext
from dataclasses import dataclass

INFO_ID = 0xB9DA41D6
CONTROL_ID = 0x5D387298
INFO_VERSION = 0x2F9568
INFO_SIZE = 0x2B9568  # Size cannot be recovered from the low 16 version bits.
CONTROL_VERSION = 0x29FC8
CONTROL_SIZE = 0x9FC8
INFO_STRIDE = 0x2BC0
CONTROL_STRIDE = 0xA0
CAPACITY = 255
MODERN_SENSED_FUSE = 13

# VFE exists in NVIDIA's Pascal-era PMU sources. Architecture permits the probe;
# an accepted packet and a matching typed variable establish actual capability.
PROBE_ARCHITECTURES = frozenset((4, 5, 6, 7, 8, 9, 10))
_FUSE_SELECTORS = {
    (0x00, 0x01): ("RMHwSpeedoOverride", "Speedo", "speedo"),
    (0x10, 0x01): ("RMHwSpeedo1Override", "Speedo 1", "speedo"),
    (0x1A, 0x01): ("RMHwSpeedo2Override", "Speedo 2", "speedo"),
    (0x18, 0x03): ("RmPmgrIddqNvvddOverride", "NVVDD IDDQ", "iddq"),
    (0x19, 0x03): ("RmPmgrIddqMsvddOverride", "MSVDD IDDQ", "iddq"),
}


class VfeInputError(RuntimeError):
    def __init__(self, kind: str, message: str, status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status


@dataclass(frozen=True)
class VfeIssue:
    index: int
    reason: str
    registry_name: str | None = None


@dataclass(frozen=True)
class VfeVariable:
    index: int
    type: int
    minimum: float | None
    maximum: float | None
    control_mode: int
    control_words: tuple[int, int]


@dataclass(frozen=True)
class VfeInput:
    index: int
    registry_name: str
    label: str
    kind: str
    fuse_id: int
    fuse_version_field_id: int
    active: int
    raw_hardware: int
    default: int
    signed: bool
    raw_hardware_signed: bool
    default_signed: bool
    override: int
    override_present: bool
    cache_ready: bool
    fuse_version: int
    expected_fuse_version: int
    hardware_scale_q12: int
    hardware_offset: int
    minimum: float
    maximum: float

    @property
    def readback_ready(self) -> bool:
        """Driver readiness guard for the active/raw pair.

        NVIDIA names the underlying flag bVersionCheckDone (once per driver
        load). The traced active-value accessor also requires this flag.
        """
        return self.cache_ready


@dataclass(frozen=True)
class VfeSnapshot:
    slot: str
    architecture: int | None
    captured_at: float
    variables: tuple[VfeVariable, ...]
    inputs: tuple[VfeInput, ...]
    issues: tuple[VfeIssue, ...]
    info_sha256: str
    control_sha256: str
    # Full INFO with only understood known-input active/override fields masked.
    # Empty retains compatibility with old snapshots, but cannot authorize a
    # comprehensive model-input contract.
    model_static_sha256: str = ""

    def input_for(self, registry_name: str) -> VfeInput | None:
        """Return a unique usable decoded input, never guess among duplicates."""
        matches = [item for item in self.inputs if item.registry_name == registry_name]
        if len(matches) > 1:
            raise VfeInputError("malformed", f"Multiple VFE inputs match {registry_name}")
        return matches[0] if matches else None


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _validate_info(info: bytes) -> None:
    if len(info) != INFO_SIZE or _u32(info, 0) != INFO_VERSION:
        raise VfeInputError("malformed", "Unrecognized VFE info packet geometry or version echo")
    if info[35] & 0x80:
        raise VfeInputError("malformed", "VFE info mask exceeds the 255-row capacity")


def _boolean(info: bytes, offset: int, name: str) -> bool:
    value = info[offset]
    if value not in (0, 1):
        raise ValueError(f"Unrecognized {name} flag {value}")
    return bool(value)


def _integer(info: bytes, offset: int) -> tuple[int, bool]:
    signed = _boolean(info, offset, "integer signedness")
    value = _u32(info, offset + 4)
    if signed and value >= 1 << 31:
        value -= 1 << 32
    return value, signed


def decode_packets(info: bytes, control: bytes, *, slot: str = "", architecture: int | None = None) -> VfeSnapshot:
    """Decode understood rows; one unsupported or malformed row is isolated.

    Global geometry/mask errors reject the packet pair. Unknown row types and
    selectors stay visible as variable metadata and issues. No fixed row index
    or local board-specific value participates in matching a known input.
    """
    _validate_info(info)
    if len(control) != CONTROL_SIZE or _u32(control, 0) != CONTROL_VERSION:
        raise VfeInputError("malformed", "Unrecognized VFE control packet geometry or version echo")
    if info[4:36] != control[4:36]:
        raise VfeInputError("malformed", "VFE info and control variable masks disagree")
    variables: list[VfeVariable] = []
    inputs: list[VfeInput] = []
    issues: list[VfeIssue] = []
    model_static = bytearray(info)
    mask = int.from_bytes(info[4:36], "little")
    for index in range(CAPACITY):
        if not mask & (1 << index):
            continue
        base, ctl = index * INFO_STRIDE, index * CONTROL_STRIDE
        typ = _u32(info, base + 0x128)
        if _u32(control, ctl + 0x68) != typ:
            issues.append(VfeIssue(index, "Info and control row types disagree"))
            continue
        minimum, maximum = struct.unpack_from("<ff", control, ctl + 0x6C)
        variables.append(
            VfeVariable(
                index,
                typ,
                minimum if math.isfinite(minimum) else None,
                maximum if math.isfinite(maximum) else None,
                control[ctl + 0x94],
                struct.unpack_from("<II", control, ctl + 0x98),
            )
        )
        if typ != MODERN_SENSED_FUSE:
            continue
        selector = (info[base + 0x77C], info[base + 0x77D])
        identity = _FUSE_SELECTORS.get(selector)
        if identity is None:
            issues.append(VfeIssue(index, f"Unknown sensed-fuse selector {selector}"))
            continue
        name, label, kind = identity
        try:
            if not math.isfinite(minimum) or not math.isfinite(maximum) or minimum > maximum:
                raise ValueError("Invalid reported model-input range")
            active, signed = _integer(info, base + 0x240)
            raw, raw_signed = _integer(info, base + 0x248)
            default, default_signed = _integer(info, base + 0x250)
            # CA8550 publishes the same active/raw cached values both as
            # typed unions and legacy scalar fields. NVAPI275BA0 preserves
            # those mirrors at 264/268; compare bit patterns for signed rows.
            if _u32(info, base + 0x264) != active & 0xFFFFFFFF or _u32(info, base + 0x268) != raw & 0xFFFFFFFF:
                raise ValueError("Active/raw fuse scalar mirrors disagree with typed values")
            present = _boolean(info, base + 0x25C, "registry override presence")
            ready = _boolean(info, base + 0x260, "cache readiness")
            inputs.append(
                VfeInput(
                    index=index,
                    registry_name=name,
                    label=label,
                    kind=kind,
                    fuse_id=selector[0],
                    fuse_version_field_id=selector[1],
                    active=active,
                    raw_hardware=raw,
                    default=default,
                    signed=signed,
                    raw_hardware_signed=raw_signed,
                    default_signed=default_signed,
                    override=_u32(info, base + 0x258),
                    override_present=present,
                    cache_ready=ready,
                    fuse_version=info[base + 0x23C],
                    expected_fuse_version=info[base + 0x26C],
                    hardware_scale_q12=_u32(info, base + 0x374),
                    hardware_offset=struct.unpack_from("<i", info, base + 0x378)[0],
                    minimum=minimum,
                    maximum=maximum,
                )
            )
            # Unknown/malformed rows are never masked. Other known-input
            # fields and every unselected/unknown INFO byte remain covered.
            for offset, length in ((0x244, 4), (0x258, 4), (0x25C, 1), (0x264, 4)):
                model_static[base + offset : base + offset + length] = bytes(length)
        except ValueError as exc:
            issues.append(VfeIssue(index, str(exc), name))
    for name in {item.registry_name for item in inputs}:
        matches = [item for item in inputs if item.registry_name == name]
        if len(matches) > 1:
            issues.extend(VfeIssue(item.index, "Ambiguous duplicate sensed-fuse selector", name) for item in matches)
    return VfeSnapshot(
        slot,
        architecture,
        time.time(),
        tuple(variables),
        tuple(inputs),
        tuple(issues),
        hashlib.sha256(info).hexdigest(),
        hashlib.sha256(control).hexdigest(),
        hashlib.sha256(model_static).hexdigest(),
    )


class VfeInputReader:
    """Fresh GETs on an already paired nvbackend.GPU, with bounded recovery.

    last_valid is retained as historical state after a failed read. It is never
    silently returned by read(). Callers must mark historical displays stale.
    After a device restart create a fresh GPU and reader, as for other NVAPI
    handles. This class never restarts or rebinds a device.
    """

    def __init__(self, gpu, *, attempts: int = 2):
        if not isinstance(attempts, int) or not 1 <= attempts <= 3:
            raise ValueError("VFE read attempts must be between 1 and 3")
        self.gpu = gpu
        self.attempts = attempts
        self.last_valid: VfeSnapshot | None = None
        self.last_error: VfeInputError | None = None

    def _binding(self):
        # Reuse backend PCI parsing; importing lazily keeps packet decoding pure.
        from .nvbackend import parse_slot, same_slot

        gpu = self.gpu
        if getattr(gpu, "pairing_error", ""):
            raise VfeInputError("identity", gpu.pairing_error)
        api = getattr(gpu, "nvapi", None)
        if api is None or not api.ok or not api.gpu or not api.selected:
            raise VfeInputError("transient", "A live PCI-paired NVAPI handle is unavailable")
        slot = gpu.slot()
        parsed = parse_slot(slot)
        if parsed is None or not same_slot(slot, api.selected.get("slot")):
            raise VfeInputError("identity", "Selected NVAPI handle does not match the GPU PCI slot")
        if parsed[0] != 0:
            raise VfeInputError("identity", "NVAPI does not identify the PCI segment for this adapter")
        selected_handle = api.selected.get("handle")

        def handle_value(value):
            return getattr(value, "value", value)

        if handle_value(selected_handle) != handle_value(api.gpu):
            raise VfeInputError("identity", "NVAPI handle changed without an updated target binding")
        architecture = gpu.arch()
        if architecture is None:
            raise VfeInputError("transient", "GPU architecture could not be read")
        if architecture not in PROBE_ARCHITECTURES:
            raise VfeInputError(
                "unsupported", "VFE sensed-fuse probing requires a supported Pascal-or-later architecture"
            )
        return api, slot, architecture

    @staticmethod
    def _get(getter, handle, size: int, version: int, mask: bytes | None = None) -> bytes:
        packet = (ctypes.c_ubyte * size)()
        struct.pack_into("<I", packet, 0, version)
        if mask is not None:
            packet[4:36] = mask
        try:
            status = int(getter(handle, ctypes.byref(packet)))
        except (OSError, ctypes.ArgumentError) as exc:
            raise VfeInputError("transient", f"VFE getter failed: {exc}") from exc
        if status != 0:
            kind = "unsupported" if status in (-3, -9, -104) else "transient"
            raise VfeInputError(kind, f"VFE getter returned NVAPI status {status}", status)
        return bytes(packet)

    def read(self) -> VfeSnapshot:
        for attempt in range(self.attempts):
            try:
                with getattr(self.gpu, "_lock", nullcontext()):
                    api, slot, architecture = self._binding()
                    info_getter = api._i(INFO_ID, ctypes.c_void_p, ctypes.c_void_p)
                    control_getter = api._i(CONTROL_ID, ctypes.c_void_p, ctypes.c_void_p)
                    if info_getter is None or control_getter is None:
                        raise VfeInputError("unsupported", "VFE variable getter exports are unavailable")
                    info = self._get(info_getter, api.gpu, INFO_SIZE, INFO_VERSION)
                    _validate_info(info)
                    control = self._get(control_getter, api.gpu, CONTROL_SIZE, CONTROL_VERSION, info[4:36])
                    snapshot = decode_packets(info, control, slot=slot, architecture=architecture)
                self.last_valid = snapshot
                self.last_error = None
                return snapshot
            except VfeInputError as exc:
                self.last_error = exc
                if exc.kind != "transient" or attempt + 1 == self.attempts:
                    raise
        raise AssertionError("Unreachable bounded VFE read loop")
