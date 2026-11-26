# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded read-only power INFO diagnostics. No SET, registry or firmware operations."""
import argparse
import ctypes
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from druta.nvbackend import GPU, u32
from druta.power_layout_catalog import CURRENT_LIMIT_LAYOUTS, CURRENT_LIMIT_LAYOUT_FALLBACKS


def capture(slot):
    gpu = GPU(slot)
    result = {'scope': 'read-only GETs; process-local pins are not other processes pins',
              'identity': {k: gpu.static.get(k) for k in ('name', 'driver', 'vbios', 'slot')},
              'nvml_configured_mw': gpu._read_power_limit_nvml(),
              'process_local_pins': dict(getattr(gpu, '_power_policy_pins', None) or {}),
              'info_probes': [], 'errors': {}}
    nv = gpu.nvml
    for label, export, count in [('nvml_bounds_mw', 'nvmlDeviceGetPowerManagementLimitConstraints', 2),
                                  ('nvml_default_mw', 'nvmlDeviceGetPowerManagementDefaultLimit', 1)]:
        if nv.ok and nv.has(export):
            values = [u32() for _ in range(count)]
            status = getattr(nv.dll, export)(nv.dev, *(ctypes.byref(v) for v in values))
            result[label] = {'status': status, 'values': [v.value for v in values] if status == 0 else None}
    try:
        header, fields = gpu._rm_power_transport()
        key = gpu._current_limit_key(header)
        result['transport'] = {'command': f'0x{key[0]:08X}', 'packet_bytes': key[1], 'parameter_bytes': key[2]}
        # Exact catalog INFO sizes for the captured wire family. These are GETs only.
        family = key[0]
        variants = []
        for catalog_key, layout in CURRENT_LIMIT_LAYOUTS.items():
            if catalog_key[0] != family:
                continue
            variants.append(layout)
            variants.extend(CURRENT_LIMIT_LAYOUT_FALLBACKS.get(catalog_key, ()))
        probes = sorted({(layout.get('commands', {}).get(0x2080A618, 0x2080A618),
                          layout['sizes'][0x2080A618]) for layout in variants})
        for command, size in probes:
            packet = (u32 * (17 + size // 4))(*header, *([0] * (size // 4)))
            packet[2], packet[14], packet[15], packet[16] = ctypes.sizeof(packet), command, size, 0
            nt = gpu._legacy_clk_escape(packet, fields)
            item = {'command': f'0x{command:08X}', 'size': size, 'ntstatus': int(nt), 'rm_status': int(packet[16])}
            if nt == 0 and packet[16] == 0:
                data = bytes(packet)[68:]
                item.update({'sha256': hashlib.sha256(data).hexdigest(), 'mask': f'0x{packet[18]:08X}',
                             'response_hex': data.hex()})
            result['info_probes'].append(item)
    except Exception as exc:
        result['errors']['transport_or_probe'] = repr(exc)
    try:
        value, error = gpu.read_live_power_request()
        result['live_board_request'] = value
        result['errors']['live_board_request'] = error
    except Exception as exc:
        result['errors']['live_board_request'] = repr(exc)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--slot', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    result = capture(args.slot)
    with args.output.open('x', encoding='utf-8') as out:
        json.dump(result, out, indent=2, default=str)
        out.write('\n')


if __name__ == '__main__':
    main()
