# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only MP2888A-compatible register discovery on the selected GPU.

The address register is not a model ID. These checks establish a candidate
register layout, not a unique controller identity; the UI must still Verify
its bounded voltage response before enabling Apply.

MPS MP2888A Rev.1.11: pp.30/86 address, p.43 offset/limit, pp.71-72
telemetry. Linux drivers/hwmon/pmbus/mp2888.c documents 0x44 bit3 for
current resolution. No PAGE selection, writes, or vendor/device-ID gates.
"""
from copy import deepcopy
from statistics import median

from .railctl import Profile, Rail, _signed

DISCOVERY_PORTS = tuple(range(8))
DISCOVERY_ADDRESSES = (0x20,) + tuple(a for a in range(0x08, 0x78) if a != 0x20)
FINGERPRINT_VERSION = 1


def _bound_profile(profile, port, addr7):
    """Copy the register recipe; keep the legacy saved-profile identity at home."""
    data = deepcopy(profile.src)
    original = (port == profile.port and addr7 == profile.addr7)
    if not original:
        data['bus'] = {'port': port, 'addr7': addr7}
        data['match'] = {}
        data['identity'] = [{'reg': 0xBE, 'bytes': 1, 'mask': 0x7F,
                             'value': addr7, 'fingerprint': True}]
        data['discovery'] = {'controller': 'MP2888A candidate',
                             'fingerprint_version': FINGERPRINT_VERSION}
    p = Profile(data, profile.path)
    p.profile_name = profile.name
    p.name = f'MP2888A candidate - {p.rail} (port {port}, 0x{addr7:02X})'
    p.port, p.addr7, p.addrs = port, addr7, [addr7]
    p.pci_device = p.pci_subsys = set()
    p.weak_id = True
    # Runtime presentation/decoding can evolve without invalidating old saved
    # offsets on the original connection. present() never trusts that old ID.
    p.telemetry_specs = deepcopy(p.telemetry_specs)
    for spec in p.telemetry_specs:
        if spec.get('key') == 'iout_a':
            spec.update(encoding='uint', bits='11:0', scale=0.25)
    return p


class MP2888Candidate(Rail):
    """A location-bound candidate; every write rechecks the read-only layout."""

    requires_verification = True

    def __init__(self, profile, nvapi, port, addr7):
        if port not in DISCOVERY_PORTS or addr7 not in DISCOVERY_ADDRESSES:
            raise ValueError('MP2888A discovery requires port 0..7 and unicast 0x08..0x77')
        super().__init__(_bound_profile(profile, port, addr7), nvapi, addr7)
        self.discovery_telemetry = {}
        self.discovery_diagnostics = {}

    def _fingerprint_sample(self):
        # Test address first so absent devices cost just one read.
        addr = self.read(0xBE, 1)
        if addr is None or addr & 0x7F != self.addr7:
            return None
        offset, limit = self.read(0x23, 2), self.read(0x24, 2)
        if (offset is None or limit is None or offset & 0xFF00 or limit & 0xFE00
                or not -111 <= _signed(offset, 8) <= 112
                or not 300 <= limit * 6.25 <= 3193.75):
            return None
        vout = self.read(0x8B, 2)
        current = self.read(0x8C, 2)
        temp = self.read(0x8D, 2)
        config = self.read(0x44, 2)
        if any(value is None for value in (vout, current, temp, config)):
            return None
        if (vout & 0xF000 or current & 0xF000 != 0xE000 or temp & 0xF800
                or not 300 <= vout <= 2000 or not 0 <= temp <= 1500):
            return None
        scale = 0.5 if config & 8 else 0.25
        return {'vout_mv': float(vout), 'iout_a': (current & 0xFFF) * scale,
                'vrm_temp_c': temp * 0.1, 'vout_max_mv': limit * 6.25,
                'offset_raw': offset, 'offset_mv': _signed(offset, 8) * 6.25,
                'current_lsb_a': scale, 'address_register': addr}

    def present(self):
        self.discovery_telemetry = {}
        if self.nvapi is None or not getattr(self.nvapi, 'ok', False):
            return False
        try:
            first = self._fingerprint_sample()
            if first is None:
                return False
            samples = [first, self._fingerprint_sample()]
        except Exception:
            return False
        if any(s is None for s in samples):
            return False
        first, last = samples
        # Config/offset must be repeatable. VOUT can move with the governor;
        # allow ordinary idle/load transitions, while rejecting erratic reads.
        if (any(first[k] != last[k] for k in
                ('offset_raw', 'vout_max_mv', 'current_lsb_a', 'address_register'))
                or abs(first['vout_mv'] - last['vout_mv']) > 500
                or abs(first['vrm_temp_c'] - last['vrm_temp_c']) > 10):
            return False
        self.discovery_telemetry = {k: median(s[k] for s in samples)
                                    for k in first}
        self.discovery_telemetry['offset_raw'] = last['offset_raw']
        return True

    def telemetry(self):
        if not self.present():
            return {}
        # Avoid the old profile's LINEAR11 assumption. The current register is
        # unsigned low12 with a fixed 0xE high nibble and a config-selected LSB.
        return dict(self.discovery_telemetry)


def discover(nvapi, profile, log=None):
    """Return every candidate on this GPU, with no writes or arbitrary choice."""
    if nvapi is None or not getattr(nvapi, 'ok', False):
        return []
    candidates = []
    # Preferred address on every port first; then the other unicast addresses.
    for addr7 in DISCOVERY_ADDRESSES:
        for port in DISCOVERY_PORTS:
            candidate = MP2888Candidate(profile, nvapi, port, addr7)
            if not candidate.present():
                continue
            # These IDs are user-programmable, so diagnostics cannot gate
            # discovery on the datasheet's example/default values (25h/88h).
            for key, reg in (('vendor_id_user', 0x27), ('product_id_user', 0x28),
                             ('product_rev_user', 0x29)):
                try:
                    candidate.discovery_diagnostics[key] = candidate.read(reg, 1)
                except Exception:
                    candidate.discovery_diagnostics[key] = None
            candidates.append(candidate)
            if log:
                t = candidate.discovery_telemetry
                log(f'{candidate.p.name}: read-only fingerprint passed; '
                    f'{t["vout_mv"]:.0f} mV, {t["iout_a"]:.2f} A, '
                    f'{t["vrm_temp_c"]:.1f} C. Verify required before Apply.', True)
    return candidates
