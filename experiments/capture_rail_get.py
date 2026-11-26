# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Record two ordinary NVVDD control GETs on an explicitly selected adapter.

Run this standalone, outside the GUI. Only transport metadata is exported.
"""
import argparse
import ctypes
import json
from pathlib import Path
import struct
import sys
import sysconfig

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from druta import rail_transport


MASK = 1  # Public NVVDD selector passed by the normal getter.


def packet_shape(size, header, params):
    # A short or unrelated escape can occur inside a getter. Exclude it from
    # semantic decoding and never export arbitrary header words or payloads.
    if (not isinstance(header, list) or len(header) != 17
            or not isinstance(params, list) or size != 4 * (len(header) + len(params))
            or header[0] != 0x4E564441 or header[3] != 0x4E562A2A
            or header[2] != size):
        return None
    words = header + params
    payload = struct.pack(f"<{len(words)}I", *words)
    recognized = rail_transport.recognize_packet(payload, MASK)
    known_mask = None
    for layout in rail_transport.KNOWN_LAYOUTS:
        if (size, header[14], header[15]) == (layout.packet_size, layout.get_command, layout.params_size):
            known_mask = words[layout.mask_word]
            break
    return {"command": f"0x{header[14]:08X}", "parameter_bytes": header[15],
            "completion": header[11], "rm_status": header[16],
            "recognized": recognized is not None, "mask": known_mask}


def summarize_get(raw):
    """Allowlist the diagnostic fields. Both halves are evaluated independently."""
    escapes = []
    for call in raw["captures"]:
        size = call["escape_size"]
        escapes.append({"packet_bytes": size, "ntstatus": call["escape_ntstatus"],
                        "input": packet_shape(size, call.get("escape_header"), call.get("escape_input_params")),
                        "returned": packet_shape(size, call.get("escape_header_after"), call.get("escape_output_params"))})
    return {"getter_status": raw["nvapi_status"], "escapes": escapes}


def capture(slot):
    if sysconfig.get_platform() != "win-amd64":
        raise RuntimeError("Live capture requires an x64 Windows Python interpreter")
    from druta.nvbackend import GPU, parse_slot
    from druta.tools.probe_volt_rails import rm_call
    from experiments.native_live_smoke_3070ti import paired

    gpu = GPU(slot)
    target = paired(gpu, slot, parse_slot)
    controls = gpu._read_volt_rail_deltas()
    state = gpu.read_volt_rail_state() or {}
    getter = getattr(gpu, "_volt_rail_getter_diagnostics", {}).get("VoltRailsCtlGet", {}).get(0, {})
    if 0 not in controls or not gpu._valid_volt_rail_state(0, state.get(0, {})) or not getter.get("valid"):
        raise RuntimeError("The selected adapter did not return validated NVVDD control and absolute status")
    version = int(getter["version"], 16)
    if version not in (0x00010AC8, 0x00020AC8):
        raise RuntimeError("Unsupported rail getter version")
    try:
        boost = gpu.read_voltage_boost()
    except Exception:
        boost = None
    samples = []
    for _ in range(2):
        # No replacement payload or alternate callable: the probe forwards the
        # normal singleton NVVDD getter unchanged. Keep raw bytes in-process.
        public = (ctypes.c_uint32 * ((version & 0xffff) // 4))()
        public[0], public[1] = version, MASK
        raw = rm_call(gpu, control_version=version, capture_multiple=True,
                      invoke=lambda: gpu.nvapi.VoltRailsCtlGet(gpu.nvapi.gpu, ctypes.byref(public)))
        if paired(gpu, slot, parse_slot) != target:
            raise RuntimeError("Adapter binding changed during the rail GET")
        summary = summarize_get(raw)
        if version == 0x00010AC8 and raw["nvapi_status"] == 0:
            matches = [c for c in raw["captures"] if c.get("escape_size") == 972
                       and c.get("escape_ntstatus") == 0
                       and c.get("escape_header_after", [0] * 17)[14:17] == [0x20803213, 904, 0]
                       and len(c.get("escape_output_params", [])) == 226]
            if len(matches) == 1:
                params = matches[0]["escape_output_params"]
                signed = lambda x: ctypes.c_int32(x).value
                native = [params[2] & 0xff] + [signed(x) for x in params[3:7]]
                public_record = [public[18]] + [signed(x) for x in public[19:23]]
                summary["candidate_972"] = {"input_mask": matches[0]["escape_input_params"][0],
                    "native_type_and_fields": native, "public_type_and_fields": public_record,
                    "record_matches_public": native == public_record,
                    "native_boost_byte": params[1] & 0xff,
                    "boost_matches_independent": (params[1] & 0xff) == boost}
        samples.append(summary)
    return {"schema": 1, "operation": "NVVDD control GET", "rail": 0,
            "requested_mask": MASK, "getter_version": getter["version"],
            "boost_pct_before": boost if type(boost) is int and 0 <= boost <= 100 else None,
            "adapter": {"slot": gpu.slot(), **{key: gpu.static.get(key) for key in ("name", "driver", "vbios")}},
            "samples": samples}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gpu", required=True, metavar="PCI_SLOT")
    parser.add_argument("--out", required=True, type=Path, metavar="NEW_JSON")
    args = parser.parse_args()
    if args.out.exists():
        parser.error("choose a new output path")
    report = capture(args.gpu)
    with args.out.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    print(args.out)


if __name__ == "__main__":
    main()
