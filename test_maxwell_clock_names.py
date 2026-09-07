# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""GM107 telemetry names follow state correlations, not idle clock equality."""
import unittest

from nvbackend import (
    GPU, PRIV_CONFIRMED, PRIV_FREQ, PRIV_LIKELY, PRIV_UNPOPULATED,
    classify_domain_names,
)


def clock_rows(values):
    return [dict(domain=domain, kind=PRIV_FREQ, name='', grade='unnamed',
                 prog_khz=round(mhz * 1000), meas_khz=round(mhz * 1000),
                 prog_mhz=mhz, meas_mhz=mhz, delta_mhz=0,
                 flags=1 if mhz else 0, srcid=32, scale=1)
            for domain, mhz in values]


class MaxwellClockNamesTests(unittest.TestCase):
    # GTX 745 DDR3 / GM107, R472.12. Held P0 separates the ROM's XBAR
    # and SYS targets; idle equality alone does not establish their order.
    IDLE = [(4, 405), (6, 405), (15, 270), (16, 810), (17, 810),
            (18, 648), (20, 324), (21, 0), (25, 0)]
    HELD_P0 = [(4, 900.001), (6, 648), (15, 1079.578),
               (16, 1164.375), (17, 1118.571), (18, 1080), (20, 324)]
    BOOST = [(4, 900.001), (6, 648), (15, 2064.55),
             (16, 1899.31), (17, 1858.235), (18, 1080), (20, 324)]
    INFERRED = {6: ('DISP', 1), 16: ('XBAR2CLK', 2),
                17: ('SYS2CLK', 2), 18: ('HUB', 1), 20: ('PWR', 1)}

    def classify(self, values, core, memory, architecture=GPU.ARCH_MAXWELL):
        return {row['domain']: row for row in classify_domain_names(
            clock_rows(values), core, memory, architecture=architecture)}

    def assert_maxwell_identities(self, rows):
        for domain, (name, scale) in self.INFERRED.items():
            self.assertEqual(rows[domain]['name'], name)
            self.assertEqual(rows[domain]['grade'], PRIV_LIKELY)
            self.assertEqual(rows[domain]['scale'], scale)
        for domain, name, scale in ((4, 'MEM', 1), (15, 'GPC2CLK', 2)):
            self.assertEqual(rows[domain]['name'], name)
            self.assertEqual(rows[domain]['grade'], PRIV_CONFIRMED)
            self.assertEqual(rows[domain]['scale'], scale)

    def test_live_states_keep_names_independent_of_row_order(self):
        for values, core, memory in ((self.IDLE, 135, 405),
                                    (self.HELD_P0, 539, 900),
                                    (self.BOOST, 1032, 900)):
            for ordered in (values, values[::-1]):
                with self.subTest(core=core, reversed=ordered != values):
                    self.assert_maxwell_identities(
                        self.classify(ordered, core, memory))

    def test_synthetic_core_boost_does_not_rename_other_domains(self):
        # Deliberately make core and memory equal: labels must not depend on
        # whichever equal-frequency row happens to be visited first.
        values = [(domain, 1800 if domain == 15 else mhz)
                  for domain, mhz in self.HELD_P0]
        self.assert_maxwell_identities(self.classify(values, 900, 900))

    def test_missing_msd_and_l2c_do_not_gain_rom_only_names(self):
        rows = self.classify(self.IDLE, 135, 405)
        for domain in (21, 25):
            self.assertEqual(rows[domain]['name'], '')
            self.assertEqual(rows[domain]['grade'], PRIV_UNPOPULATED)
            self.assertEqual(rows[domain]['scale'], 1)

    def test_other_unknown_slots_remain_unnamed(self):
        rows = self.classify([(5, 277.8), (8, 27), (9, 27), (22, 108)],
                             135, 405)
        for row in rows.values():
            self.assertEqual(row['name'], '')

    def test_unverified_populated_msd_and_l2c_slots_remain_unnamed(self):
        rows = self.classify([(21, 540), (25, 1130)], None, None)
        for row in rows.values():
            self.assertEqual(row['name'], '')
            self.assertNotEqual(row['grade'], PRIV_UNPOPULATED)
            self.assertEqual(row['scale'], 1)

    def test_zero_inferred_slots_remain_unpopulated(self):
        rows = self.classify([(domain, 0) for domain in self.INFERRED],
                             None, None)
        for row in rows.values():
            self.assertEqual(row['name'], '')
            self.assertEqual(row['grade'], PRIV_UNPOPULATED)

    def test_shared_xbar_sys_names_do_not_import_kepler_l2c_to_maxwell(self):
        values = self.HELD_P0 + [(25, 1113.75)]
        for architecture in (GPU.ARCH_KEPLER, GPU.ARCH_MAXWELL):
            rows = self.classify(values, 539, 900, architecture=architecture)
            for domain, name in ((16, 'XBAR2CLK'), (17, 'SYS2CLK')):
                self.assertEqual(rows[domain]['name'], name)
                self.assertEqual(rows[domain]['grade'], PRIV_LIKELY)
                self.assertEqual(rows[domain]['scale'], 2)
            if architecture == GPU.ARCH_KEPLER:
                self.assertEqual(rows[25]['name'], 'L2C2CLK')
                self.assertEqual(rows[25]['grade'], PRIV_LIKELY)
                self.assertEqual(rows[25]['scale'], 2)
            else:
                self.assertEqual(rows[25]['name'], '')
                self.assertEqual(rows[25]['scale'], 1)

    def test_maxwell_extra_names_do_not_leak_to_pascal_or_turing(self):
        for architecture in (GPU.ARCH_PASCAL, 6):
            with self.subTest(architecture=architecture):
                rows = self.classify(self.HELD_P0, 539, 900, architecture)
                for domain in (6, 17, 18, 20):
                    self.assertEqual(rows[domain]['name'], '')

    def test_switching_kepler_rows_to_maxwell_clears_l2c_and_msd(self):
        rows = list(self.classify(
            self.HELD_P0 + [(21, 540), (25, 1113.75)], 539, 900,
            architecture=GPU.ARCH_KEPLER).values())
        classify_domain_names(rows, 539, 900, architecture=GPU.ARCH_MAXWELL)
        by_domain = {row['domain']: row for row in rows}
        for domain in (21, 25):
            self.assertEqual(by_domain[domain]['name'], '')
            self.assertEqual(by_domain[domain]['scale'], 1)
        self.assert_maxwell_identities(by_domain)

    def test_reclassification_clears_names_when_architecture_changes(self):
        rows = list(self.classify(self.HELD_P0, 539, 900).values())
        classify_domain_names(rows, 539, 900, architecture=6)
        by_domain = {row['domain']: row for row in rows}
        for domain in self.INFERRED:
            self.assertEqual(by_domain[domain]['name'], '')
            self.assertEqual(by_domain[domain]['scale'], 1)


if __name__ == '__main__':
    unittest.main()
