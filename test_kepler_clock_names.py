# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Clock identities must survive idle frequency collisions and GPU switches."""
import threading
import unittest
from unittest.mock import Mock, patch

from nvbackend import (
    GPU, PRIV_CONFIRMED, PRIV_FREQ, PRIV_LIKELY, PRIV_UNPOPULATED,
    _AllClocksPriv, classify_domain_names,
)


def clock_rows(values):
    return [dict(domain=dom, kind=PRIV_FREQ, name='', grade='unnamed',
                 prog_khz=int(mhz * 1000), meas_khz=int(mhz * 1000),
                 prog_mhz=mhz, meas_mhz=mhz, delta_mhz=0,
                 flags=1 if mhz else 0, srcid=32, scale=1)
            for dom, mhz in values]


def named(rows):
    return {row['domain']: row for row in rows}


class KeplerClockNamesTests(unittest.TestCase):
    def test_screenshot_idle_memory_is_not_mistaken_for_core(self):
        values = [(4, 324), (5, 277.8), (6, 540), (8, 27), (9, 27),
                  (15, 648), (16, 648), (17, 648), (18, 648), (20, 324)]
        for ordered in (values, values[::-1]):
            rows = named(classify_domain_names(
                clock_rows(ordered), 324, 324, architecture=GPU.ARCH_KEPLER))
            self.assertEqual(rows[4]['name'], 'MEM')
            self.assertEqual(rows[4]['scale'], 1)
            self.assertEqual(rows[15]['name'], 'GPC2CLK')
            self.assertEqual(rows[15]['grade'], PRIV_CONFIRMED)
            self.assertEqual(rows[15]['scale'], 2)

    def test_core_identity_survives_held_p0_and_boost(self):
        for core, gpc2 in ((705, 1411), (1201, 2402), (1215, 2430)):
            rows = named(classify_domain_names(
                clock_rows([(4, 3004), (15, gpc2), (16, gpc2),
                            (17, gpc2), (20, 324)]),
                core, 3004, architecture=GPU.ARCH_KEPLER))
            self.assertEqual(rows[15]['name'], 'GPC2CLK')
            self.assertEqual(rows[4]['name'], 'MEM')

    def test_legacy_families_use_their_own_primary_slots(self):
        for architecture in (GPU.ARCH_KEPLER, GPU.ARCH_MAXWELL,
                             GPU.ARCH_PASCAL):
            rows = named(classify_domain_names(
                clock_rows([(0, 0), (4, 324), (15, 648)]),
                324, 324, architecture=architecture))
            self.assertEqual(rows[15]['name'], 'GPC2CLK')
            self.assertEqual(rows[4]['name'], 'MEM')
            self.assertEqual(rows[0]['grade'], PRIV_UNPOPULATED)

    def test_absent_legacy_core_does_not_relabel_memory(self):
        rows = named(classify_domain_names(
            clock_rows([(4, 324), (15, 0), (20, 324)]),
            324, 324, architecture=GPU.ARCH_KEPLER))
        self.assertEqual(rows[4]['name'], 'MEM')
        self.assertFalse(any(row['name'] in ('GPC', 'GPC2CLK')
                             for row in rows.values()))

    def test_failed_core_probe_does_not_import_turing_names(self):
        rows = named(classify_domain_names(
            clock_rows([(0, 500), (1, 500), (2, 500), (4, 324),
                        (15, 648), (21, 500)]),
            None, 324, architecture=GPU.ARCH_KEPLER))
        for dom in (0, 1, 2):
            self.assertEqual(rows[dom]['name'], '')
        self.assertEqual(rows[4]['name'], 'MEM')

    def test_kepler_rom_correlations_are_hedged_and_keep_units(self):
        rows = named(classify_domain_names(
            clock_rows([(4, 3004), (5, 277.8), (6, 540), (8, 27), (9, 27),
                        (15, 1411), (16, 1480.344), (17, 1480.344), (18, 1080),
                        (20, 324), (21, 540), (22, 108), (25, 1411)]),
            705, 3004, architecture=GPU.ARCH_KEPLER))
        for dom, name in ((6, 'DISP'), (18, 'HUB'), (20, 'PWR'),
                          (21, 'MSD'), (25, 'L2C2CLK')):
            self.assertEqual(rows[dom]['name'], name)
            self.assertEqual(rows[dom]['grade'], PRIV_LIKELY)
        for dom, name in ((16, 'XBAR2CLK'), (17, 'SYS2CLK')):
            self.assertEqual(rows[dom]['name'], name)
            self.assertEqual(rows[dom]['grade'], PRIV_LIKELY)
        for dom in (5, 8, 9, 22):
            self.assertEqual(rows[dom]['name'], '')
        self.assertEqual({dom for dom, row in rows.items() if row['scale'] == 2},
                         {15, 16, 17, 25})

    def test_gtx770_held_p0_separates_xbar_sys_and_l2c(self):
        # Two captures reproduce the distinct targets in the supplied ROM:
        # GPC 1080, XBAR 1165, SYS 1134, L2C 1115 MHz (PLL quantized).
        values = [(4, 3505), (15, 1071.29), (16, 1164.375),
                  (17, 1134), (25, 1113.75)]
        for ordered in (values, values[::-1]):
            rows = named(classify_domain_names(
                clock_rows(ordered), 535, 3505,
                architecture=GPU.ARCH_KEPLER))
            for dom, name in ((16, 'XBAR2CLK'), (17, 'SYS2CLK'),
                              (25, 'L2C2CLK')):
                self.assertEqual(rows[dom]['name'], name)
                self.assertEqual(rows[dom]['grade'], PRIV_LIKELY)
                self.assertEqual(rows[dom]['scale'], 2)
            self.assertEqual(rows[15]['name'], 'GPC2CLK')
            self.assertEqual(rows[15]['grade'], PRIV_CONFIRMED)
            self.assertEqual(rows[4]['name'], 'MEM')

    def test_turing_and_blackwell_idle_collision_keeps_core_and_memory(self):
        for kwargs in ({'architecture': 6}, {'blackwell': True}):
            rows = named(classify_domain_names(
                clock_rows([(0, 405), (1, 405), (4, 405), (15, 810)]),
                405, 405, **kwargs))
            self.assertEqual(rows[0]['name'], 'GPC')
            self.assertEqual(rows[4]['name'], 'MEM')

    def test_unknown_architecture_does_not_name_equal_frequency_candidates(self):
        rows = classify_domain_names(
            clock_rows([(4, 324), (15, 648), (16, 648), (20, 324)]),
            324, 324, architecture=None)
        self.assertFalse(any(row['name'] in ('GPC', 'GPC2CLK', 'MEM')
                             for row in rows))

    def test_unknown_architecture_can_still_use_unique_correlations(self):
        rows = named(classify_domain_names(
            clock_rows([(4, 3004), (15, 2402), (20, 324)]),
            1201, 3004, architecture=None))
        self.assertEqual(rows[4]['name'], 'MEM')
        self.assertEqual(rows[15]['name'], 'GPC2CLK')

    def test_backend_supplies_selected_gpu_architecture(self):
        gpu = GPU.__new__(GPU)
        gpu._lock = threading.RLock()
        gpu.arch = Mock(return_value=GPU.ARCH_KEPLER)
        gpu.clkdom_is_blackwell = Mock(return_value=False)
        payload = _AllClocksPriv()
        for domain, mhz in ((4, 324), (15, 648)):
            payload.w[2 * domain] = mhz * 1000
            payload.w[2 * domain + 1] = 1
            payload.w[64 + 7 * domain] = mhz * 1000
            payload.w[64 + 7 * domain + 1] = 32
        rows, error = gpu.read_clock_domains(payload, 324, 324)
        self.assertIsNone(error)
        self.assertEqual(named(rows)[4]['name'], 'MEM')
        self.assertEqual(named(rows)[15]['name'], 'GPC2CLK')

    def test_monitor_uses_explicit_scale_even_when_frequencies_overlap(self):
        from druta import Druta
        app = Druta.__new__(Druta)
        app._dom_name, app._dom_band, app._dom_shown = {}, {}, set()
        app.dom_band = Mock(return_value='ok')
        rows = clock_rows([(4, 324), (15, 648), (20, 324)])
        rows[0].update(name='MEM')
        rows[1].update(name='GPC2CLK', scale=2)
        with patch('druta.dpg'):
            app.refresh_domains({'clk_domains': rows})
        self.assertEqual([call.args[1] for call in app.dom_band.call_args_list],
                         [1, 2, 1])


if __name__ == '__main__':
    unittest.main()
