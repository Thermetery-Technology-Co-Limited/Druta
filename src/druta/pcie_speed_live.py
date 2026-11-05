# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Manual PCIe generation request with target and link readback."""

import struct
import time

from . import clock_bios_runtime as clock
from . import lpwr_runtime as lpwr
from .block_clock_gating_live import _read_registers
from .perf_diagnostics import PerfDiagnosticError, PerfDiagnosticsReader

INFO, SET, ARCH = 0x20801823, 0x20801805, 0x20801701
REGISTER, TARGET_MASK = 0x85008, 0xF0000
_IMPLEMENTATIONS = frozenset((2, 3, 5, 6, 7))
_SELECTORS = (3, 4, 7, 8)


class PcieSpeedError(RuntimeError):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def _info(reader, identity):
    request = bytearray(420)
    struct.pack_into("<I", request, 0, len(_SELECTORS))
    for index, selector in enumerate(_SELECTORS):
        struct.pack_into("<I", request, 4 + 8 * index, selector)
    raw = lpwr._get(reader, identity, INFO, len(request), bytes(request))
    if len(raw) != len(request):
        raise PcieSpeedError("malformed", "PCIe link INFO size changed")
    clean, values = bytearray(raw), {}
    for index, selector in enumerate(_SELECTORS):
        offset = 8 + 8 * index
        values[selector] = struct.unpack_from("<I", raw, offset)[0]
        clean[offset:offset + 4] = request[offset:offset + 4]
    if clean != request:
        raise PcieSpeedError("malformed", "PCIe link INFO geometry changed")
    return values


class PcieSpeedController:
    def __init__(self, gpu):
        self.gpu = gpu
        self.reader = PerfDiagnosticsReader(gpu)

    def _once(self):
        identity = clock._binding(self.reader)
        native = lpwr._get(self.reader, identity, ARCH, 16, bytes(16))
        if len(native) != 16:
            raise PcieSpeedError("malformed", "PCIe architecture reply has an unexpected size")
        arch, implementation, _, _ = struct.unpack("<4I", native)
        if arch != 0x1B0 or implementation not in _IMPLEMENTATIONS:
            return {"identity": identity, "available": False,
                    "status": "PCIe speed target layout is not supported",
                    "gpu_max_generation": None, "root_max_generation": None,
                    "generation": None, "root_generation": None, "width": None,
                    "root_width": None, "target_generation": None,
                    "target_register": None, "allowed_generations": []}
        values = _info(self.reader, identity)
        registers = _read_registers(self.reader, identity, {REGISTER: 0})
        register = registers.get(hex(REGISTER))
        if type(register) is not int or not 0 <= register <= 0xFFFFFFFF:
            raise PcieSpeedError("malformed", "PCIe target register is unavailable")
        if clock._binding(self.reader) != identity:
            raise PcieSpeedError("identity", "GPU identity changed during PCIe read")
        gpu_max, root_max = values[3] & 15, values[4] & 15
        generation, root_generation = (values[7] >> 16) & 15, (values[8] >> 16) & 15
        width, root_width = (values[7] >> 20) & 63, (values[8] >> 20) & 63
        target = (register & TARGET_MASK) >> 16
        valid = (1 <= gpu_max <= 6 and 1 <= root_max <= 6 and
                 1 <= generation <= 6 and 1 <= root_generation <= 6 and
                 width in (1, 2, 4, 8, 16, 32) and root_width in (1, 2, 4, 8, 16, 32)
                 and 1 <= target <= 5 and not register & 0x1F)
        maximum = min(gpu_max, root_max, 5) if valid else 0
        return {"identity": identity, "available": bool(valid),
                "status": "PCIe target and link read" if valid else "PCIe target or link readback is not understood or is busy",
                "gpu_max_generation": gpu_max, "root_max_generation": root_max,
                "generation": generation, "root_generation": root_generation,
                "width": width, "root_width": root_width,
                "target_generation": target, "target_register": register,
                "allowed_generations": list(range(1, maximum + 1))}

    def _read(self):
        for attempt in range(2):
            try:
                return self._once()
            except PerfDiagnosticError as exc:
                if exc.kind != "transient" or attempt:
                    raise PcieSpeedError(exc.kind, str(exc)) from exc
        raise AssertionError("unreachable")

    def read(self):
        with self.gpu._lock:
            return self._read()

    def set_generation(self, generation, expected_identity=None):
        if type(generation) is not int:
            raise PcieSpeedError("invalid", "PCIe generation must be an integer")
        with self.gpu._lock:
            before = self._read()
            if not before.get("available"):
                raise PcieSpeedError("unsupported", before.get("status", "PCIe speed unavailable"))
            if expected_identity is not None and expected_identity != before["identity"]:
                raise PcieSpeedError("identity", "Selected GPU identity changed")
            if generation not in before["allowed_generations"]:
                raise PcieSpeedError("invalid", "Requested generation exceeds the supported link range")
            if (before["target_generation"] == generation and before["generation"] == generation
                    and before["root_generation"] == generation):
                return {**before, "before": before, "requested_generation": generation,
                        "request_accepted": False, "verification": "already_matches"}
            request = struct.pack("<I", generation)
            try:
                returned = lpwr._get(self.reader, before["identity"], SET, 4, request)
            except PerfDiagnosticError as exc:
                raise PcieSpeedError(exc.kind, f"PCIe speed SET failed: {exc}") from exc
            if returned != request:
                raise PcieSpeedError("malformed", "PCIe speed SET changed its request payload")
            observed, detail = None, "requested target and both link speeds did not read back"
            for attempt in range(3):
                if attempt:
                    time.sleep(0.05 if attempt == 1 else 0.2)
                try:
                    observed = self._read()
                except Exception as exc:
                    observed = None
                    detail = f"post-request read failed: {exc}"
                    continue
                if (observed.get("available") and observed["identity"] == before["identity"]
                        and observed["target_generation"] == generation
                        and observed["generation"] == generation and observed["root_generation"] == generation
                        and observed["width"] == before["width"] and observed["root_width"] == before["root_width"]
                        and observed["gpu_max_generation"] == before["gpu_max_generation"]
                        and observed["root_max_generation"] == before["root_max_generation"]
                        and (observed["target_register"] & ~TARGET_MASK) == (before["target_register"] & ~TARGET_MASK)):
                    return {**observed, "before": before, "requested_generation": generation,
                            "request_accepted": True, "verification": "target_and_link",
                            "status": "Requested target and both link speeds read back"}
            if observed is None:
                observed = {"identity": before["identity"], "available": False,
                            "gpu_max_generation": None, "root_max_generation": None,
                            "generation": None, "root_generation": None, "width": None,
                            "root_width": None, "target_generation": None,
                            "target_register": None, "allowed_generations": []}
            return {**observed, "before": before, "requested_generation": generation,
                    "request_accepted": True, "verification": "unverified", "status": detail}
