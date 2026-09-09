# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only MP29816 discovery, bound to the currently selected PAGE.

Identity/offset: ElmorLabs EVC2 MP29816.xml Detect and Loop 1/2 VID Offset.
VOUT scaling: Linux drivers/hwmon/pmbus/mp2869.c (mp29816 support).
We never select PAGE or change scaling. Device identity does not identify
which physical GPU rail a board connects to either output.
"""
from copy import deepcopy
from types import SimpleNamespace

from .railctl import Profile, Rail

DISCOVERY_PORTS = tuple(range(8))
DISCOVERY_ADDRESSES = (0x30,) + tuple(a for a in range(0x08, 0x78) if a != 0x30)
DEVICE_ID = 0x0002A81604  # SMBus count 4 followed by EVC2's 0x0002A816 payload
VOUT_SCALES_MV = (6.25, 5.0, 2.5, 2.0, 1.0, 1 / 256, 1 / 512, 1 / 1024)


def _bound_profile(profile, port, addr7, page, selector):
    data = deepcopy(profile.src)
    original = ((port, addr7, page, selector) ==
                (profile.port, profile.addr7, 0, 1))
    # Keep the original recipe bytes for old saved-profile hashes. Runtime
    # checks are supplied below and never trust its board/OEM fingerprints.
    if not original:
        data['bus'] = {'port': port, 'addr7': addr7}
        data['match'] = {}
        data['identity'] = [{'reg': 0xAD, 'bytes': 5, 'equals': DEVICE_ID},
                            {'reg': 0, 'bytes': 1, 'equals': page},
                            {'reg': 0x29, 'bytes': 2, 'mask': 0x1C00,
                             'value': selector << 10}]
        data['discovery'] = {'controller': 'MP29816', 'version': 1,
                             'page': page, 'vout_scale_selector': selector}
        data['profile']['rail'] = f'PAGE {page} output'
        for spec in data['telemetry']:
            if spec.get('key') == 'vout_mv':
                spec['scale'] = VOUT_SCALES_MV[selector]
        # EVC2 supplies offset semantics only for the 5 mV mode on both pages.
        if selector != 1:
            data.pop('write', None)
    p = Profile(data, profile.path)
    p.profile_name = profile.name if original else f'MP29816 PAGE {page} output'
    if original:
        p.profile_rail = profile.rail
    p.rail = f'PAGE {page} output'
    mode = 'Verify required' if not p.read_only else 'telemetry only; offset encoding unknown'
    p.name = (f'MP29816 PAGE {page} (port {port}, 0x{addr7:02X}; '
              f'physical rail unassigned; {mode})')
    p.port, p.addr7, p.addrs = port, addr7, [addr7]
    p.pci_device = p.pci_subsys = set()
    p.runtime_checks = True
    return p


class MP29816(Rail):
    requires_verification = True

    def __init__(self, profile, nvapi, port, addr7, page, selector):
        if (type(port) is not int or port not in DISCOVERY_PORTS
                or type(addr7) is not int or addr7 not in DISCOVERY_ADDRESSES
                or type(page) is not int or page not in (0, 1)
                or type(selector) is not int or selector not in range(8)):
            raise ValueError('MP29816 requires a unicast route, PAGE 0/1 and a valid scale selector')
        super().__init__(_bound_profile(profile, port, addr7, page, selector), nvapi, addr7)
        self.page, self.scale_selector = page, selector
        self.discovery_diagnostics = {}
        self.discovery_telemetry = {}

    def present(self):
        if not getattr(self.nvapi, 'ok', False):
            return False
        try:
            if self.read(0xAD, 5) != DEVICE_ID or self.read(0, 1) != self.page:
                return False
            scale = self.read(0x29, 2)
            return (type(scale) is int and 0 <= scale <= 0xFFFF
                    and (scale >> 10) & 7 == self.scale_selector)
        except Exception:
            return False


def discover(nvapi, profile, log=None):
    """Read model ID on all routes; return every stable PAGE-bound output."""
    if not getattr(nvapi, 'ok', False):
        return []
    hits = []
    for addr7 in DISCOVERY_ADDRESSES:
        for port in DISCOVERY_PORTS:
            probe = Rail(profile, nvapi, addr7)
            # Transport only needs p.port; never mutate the shared TOML recipe.
            probe.p = SimpleNamespace(port=port)
            try:
                if probe.read(0xAD, 5) != DEVICE_ID:
                    continue
                page, scale = probe.read(0, 1), probe.read(0x29, 2)
                if page not in (0, 1) or type(scale) is not int or not 0 <= scale <= 0xFFFF:
                    if log:
                        log(f'MP29816 at port {port}/0x{addr7:02X}: PAGE/scaling unavailable '
                            'or unsupported; no page-selection write attempted.', False)
                    continue
                rail = MP29816(profile, nvapi, port, addr7, page, (scale >> 10) & 7)
                if not rail.present():
                    continue
                # These board-observed strings/revisions are useful evidence,
                # but the source-backed 0xAD model ID is the identity gate.
                for key, reg, width in (('manufacturer_block', 0x99, 4),
                                        ('model_block', 0x9A, 7),
                                        ('revision_block', 0x9B, 3)):
                    try:
                        rail.discovery_diagnostics[key] = rail.read(reg, width)
                    except Exception:
                        rail.discovery_diagnostics[key] = None
                rail.discovery_telemetry = rail.telemetry()
                if not rail.present():
                    continue
                hits.append(rail)
                if log:
                    log(f'{rail.p.name}; {VOUT_SCALES_MV[rail.scale_selector]:g} mV/LSB.', True)
            except Exception:
                # A failed route must not prevent discovering other ports.
                continue
    return hits
