#!/usr/bin/env python3
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Capture GA10x framebuffer capacity readbacks for one PCI-selected GPU."""

import argparse
import ctypes
import json
import struct
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from druta import clock_bios_runtime as clock  # noqa: E402
from druta import lpwr_runtime as lpwr  # noqa: E402
from druta.block_clock_gating_live import _read_registers  # noqa: E402
from druta.nvbackend import GPU, format_slot, parse_slot  # noqa: E402
from druta.perf_diagnostics import PerfDiagnosticError, PerfDiagnosticsReader  # noqa: E402

SOURCE = "https://github.com/NVIDIA/open-gpu-kernel-modules/blob/615.78.08/"
FB_INFO = {
    "RAM_CFG": (0x0C, "implementation-dependent value"),
    "RAM_SIZE": (0x07, "KiB"),
    "TOTAL_RAM_SIZE": (0x08, "KiB"),
    "USABLE_RAM_SIZE": (0x20, "KiB"),
    "BUS_WIDTH": (0x0B, "raw driver value (RM header omits unit)"),
    "PARTITION_MASK": (0x14, "active partition bitmask, low 32 bits"),
    "PARTITION_COUNT": (0x04, "FBPA count"),
    "FBP_COUNT": (0x19, "FBP count"),
    "RAM_TYPE": (0x0D, "enum"),
    "MEMORYINFO_VENDOR_ID": (0x1C, "enum"),
}
FB_INFO_COMMAND, FB_INFO_SIZE = 0x20801303, 4 + 128 * 8
USABLE_FB_REGISTER = 0x001183A4
NOUVEAU_STRAP_REGISTER = 0x00101000


class NvmlMemory(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in ("total", "free", "used")]


def _stable(read):
    samples = []
    try:
        for seed in (0xA5A5A5A5, 0x5A5A5A5A):
            samples.append(read(seed))
    except (RuntimeError, ValueError, OSError) as exc:
        result = {"status": getattr(exc, "kind", "error"), "detail": str(exc), "samples": samples}
        if getattr(exc, "status", None) is not None:
            result["driver_status"] = hex(exc.status)
        return result
    if samples[0] != samples[1]:
        return {"status": "unstable", "samples": samples}
    return {"status": "ok", "value": samples[0], "samples": samples}


def _fb_value(get, index, seed):
    request = struct.pack("<3I", 1, index, seed).ljust(FB_INFO_SIZE, b"\0")
    result = get(FB_INFO_COMMAND, FB_INFO_SIZE, request)
    if len(result) != FB_INFO_SIZE or struct.unpack_from("<2I", result) != (1, index):
        raise PerfDiagnosticError("malformed", "FB GET_INFO_V2 changed count, index, or buffer size")
    return struct.unpack_from("<I", result, 8)[0]


def _nvml_total(nvml):
    if not nvml.has("nvmlDeviceGetMemoryInfo"):
        raise PerfDiagnosticError("skipped", "NVML memory-info export is unavailable")
    memory = NvmlMemory()
    status = nvml.dll.nvmlDeviceGetMemoryInfo(nvml.dev, ctypes.byref(memory))
    if status:
        raise PerfDiagnosticError("error", f"NVML memory-info status {status}: {nvml.errstr(status)}", status)
    return memory.total


def _target(gpu, slot):
    nvml = gpu.nvml
    selected = nvml.selected or {}
    if gpu.slot() != slot or selected.get("slot") != slot or not selected.get("uuid") or nvml.dev != selected["dev"]:
        raise PerfDiagnosticError("identity", "The requested PCI slot has no matching NVML handle and UUID")
    return {"slot": slot, **{key: selected.get(key) for key in ("uuid", "devid", "subsys")}}


def collect(gpu, slot):
    identity = _target(gpu, slot)
    reader = PerfDiagnosticsReader(gpu)

    def get(command, size, request):
        binding = clock._binding(reader)
        if binding["slot"] != slot or _target(gpu, slot) != identity:
            raise PerfDiagnosticError("identity", "GPU binding changed before GET")
        return lpwr._get(reader, binding, command, size, request)

    def native_chip(_seed):
        raw = get(0x20801701, 16, bytes(16))
        if len(raw) != 16:
            raise PerfDiagnosticError("malformed", "Native architecture read changed its buffer size")
        words = struct.unpack("<4I", raw)
        return dict(zip(("architecture", "implementation", "revision", "raw_auxiliary"), words, strict=True))

    def read_register(register):
        if _target(gpu, slot) != identity:
            raise PerfDiagnosticError("identity", "GPU binding changed before register read")
        return _read_registers(reader, clock._binding(reader), {register: 0})[hex(register)]

    chip = _stable(native_chip)
    skipped = {"status": "skipped", "detail": "Stable native GA10x architecture and implementation required"}
    scratch, strap = skipped, skipped
    if chip["status"] == "ok":
        arch = chip["value"]
        if arch["architecture"] == 0x170 and arch["implementation"] in (2, 3, 4, 6, 7):
            scratch = _stable(lambda _: read_register(USABLE_FB_REGISTER))
            strap = _stable(lambda _: read_register(NOUVEAU_STRAP_REGISTER))

    fields = {
        name: {"index": hex(index), "unit": unit, **_stable(lambda seed, index=index: _fb_value(get, index, seed))}
        for name, (index, unit) in FB_INFO.items()
    }
    total = _stable(lambda _: _nvml_total(gpu.nvml))
    if _target(gpu, slot) != identity:
        raise PerfDiagnosticError("identity", "GPU binding changed during capture")
    return {
        "identity": {**identity, **{key: gpu.static.get(key) for key in ("name", "driver", "vbios")}},
        "native_chip": chip,
        "devinit_usable_size": {
            "register": hex(USABLE_FB_REGISTER),
            "symbol": "NV_USABLE_FB_SIZE_IN_MB",
            "unit": "MiB",
            **scratch,
        },
        "nouveau_strap_readback": {
            "register": hex(NOUVEAU_STRAP_REGISTER),
            "unit": "raw DWORD",
            **strap,
            "strap_bits_5_2": (strap["value"] & 0x3C) >> 2 if strap["status"] == "ok" else None,
            "note": "Nouveau's strap extraction. The NVIDIA firmware interpretation and active profile are unverified.",
        },
        "fb_get_info_v2": {"command": hex(FB_INFO_COMMAND), "parameter_bytes": FB_INFO_SIZE, "fields": fields},
        "nvml_memory_total": {"api": "nvmlDeviceGetMemoryInfo", "unit": "bytes", **total},
        "ram_cfg_note": "Implementation-dependent RM value. Its relationship to the physical strap is unverified.",
        "sources": {
            "rm_fields": SOURCE + "src/common/sdk/nvidia/inc/ctrl/ctrl2080/ctrl2080fb.h",
            "devinit_handoff": SOURCE + "src/nvidia/src/kernel/gpu/mem_sys/arch/ampere/kern_mem_sys_ga102.c#L34-L47",
            "devinit_chip_dispatch": SOURCE + "src/nvidia/generated/g_kern_mem_sys_nvoc.c#L323-L332",
            "devinit_register": SOURCE + "src/common/inc/swref/published/ampere/ga102/dev_gc6_island.h#L32",
            "devinit_register_alias": SOURCE
            + "src/common/inc/swref/published/ampere/ga102/dev_gc6_island_addendum.h#L31-L33",
            "nouveau_strap_extraction": "https://github.com/torvalds/linux/blob/v6.18/drivers/gpu/drm/nouveau/nvkm/subdev/bios/ramcfg.c#L27-L31",
            "nvml_total": "https://docs.nvidia.com/deploy/nvml-api/api/structnvmlMemory__t.html",
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gpu", required=True, help="Canonical PCI slot, for example 0000:01:00.0")
    parser.add_argument("--out", required=True, type=Path, help="New JSON report path")
    args = parser.parse_args()
    address = parse_slot(args.gpu)
    if address is None or address[0] != 0 or address[2] > 31 or address[3] > 7 or args.gpu != format_slot(*address):
        parser.error("--gpu must be a canonical domain-0000 PCI slot")

    try:
        output = args.out.open("x", encoding="utf-8", newline="\n")
    except OSError as exc:
        parser.error(str(exc))
    with output:
        report = {
            "schema": "ga10x-capacity-handoff-v1",
            "at_utc": datetime.now(UTC).isoformat(),
            "requested_slot": args.gpu,
        }
        try:
            report.update(collect(GPU(args.gpu), args.gpu))
        except (RuntimeError, ValueError, OSError) as exc:
            report["capture_error"] = str(exc)
        json.dump(report, output, indent=2)
        output.write("\n")
    return int("capture_error" in report)


if __name__ == "__main__":
    raise SystemExit(main())
