"""An unreadable driver lock table must never establish free ownership."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from druta.nvbackend import GPU


def entry(domain, mode=0, value=0):
    return SimpleNamespace(domain=domain, lockMode=mode, volt_uV=value)


def gpu(entries=None):
    g = GPU.__new__(GPU)
    g._lock = threading.RLock()
    table = None if entries is None else SimpleNamespace(count=len(entries), locks=entries)
    g._vf_lock_read_raw = Mock(return_value=table)
    g._read_legacy_clk_lock = Mock(return_value=None)
    g.arch = Mock(return_value=GPU.ARCH_AMPERE)
    return g


class ClockLockStatusTests(unittest.TestCase):
    def test_known_domains_unlocked_and_frequency_lock_have_distinct_results(self):
        self.assertEqual(gpu([entry(0), entry(1)]).read_clk_lock_status(), (None, None))
        self.assertEqual(gpu([entry(0, 2, 1500000), entry(1, 2, 405000)]).read_clk_lock_status(),
                         ((405, 1500), None))

    def test_getter_failure_is_unknown_and_validated_legacy_lock_is_visible(self):
        g = gpu()
        lock, error = g.read_clk_lock_status()
        self.assertIsNone(lock)
        self.assertIn('unknown', error)
        g._read_legacy_clk_lock.return_value = (600, 900)
        self.assertEqual(g.read_clk_lock_status(), ((600, 900), None))

    def test_modern_unlocked_does_not_hide_an_independent_legacy_lock(self):
        g = gpu([entry(0), entry(1)])
        g._read_legacy_clk_lock.return_value = (600, 900)
        self.assertEqual(g.read_clk_lock_status(), ((600, 900), None))

    def test_turing_without_gdi_adapter_apis_names_the_missing_export(self):
        # Windows 7 and Vista gdi32 have no D3DKMTEnumAdapters2. The refusal
        # must hold and say why, not blame the driver's lock state.
        g = gpu([entry(0), entry(1)])
        g.arch.return_value = GPU.ARCH_TURING
        g._legacy_clk_limit_records = Mock(return_value=None)
        g._legacy_clk_gdi = Mock(return_value=None)
        lock, error = g.read_clk_lock_status()
        self.assertIsNone(lock)
        self.assertIn('D3DKMTEnumAdapters2', error)
        self.assertIn('ownership is unknown', error)
        g._legacy_clk_gdi.return_value = object()  # adapter APIs present
        self.assertEqual(g.read_clk_lock_status(),
                         (None, 'legacy frequency-lock state is unreadable or ambiguous'))
        g.arch.return_value = GPU.ARCH_AMPERE  # Turing-only channel
        g._legacy_clk_gdi.return_value = None
        self.assertEqual(g.read_clk_lock_status(), (None, None))

    def test_turing_unlocked_requires_both_live_channels_to_be_understood(self):
        g = gpu([entry(0), entry(1)])
        g.arch.return_value = GPU.ARCH_TURING
        g._legacy_clk_limit_records = Mock(return_value=None)
        self.assertIsNotNone(g.read_clk_lock_status()[1])
        rows = [[0] * 82 for _ in range(2)]
        for row, ident in zip(rows, (0x4C, 0x4B)):  # zip(strict=True) is 3.10+
            row[0], row[4] = ident, 1
        g._legacy_clk_limit_records.return_value = rows
        self.assertEqual(g.read_clk_lock_status(), (None, None))
        rows[0][1] = 2
        self.assertIsNotNone(g.read_clk_lock_status()[1])

    def test_partial_ambiguous_or_malformed_lock_table_does_not_mean_unlocked(self):
        for entries in ([], [entry(0)], [entry(1)], [entry(0), entry(0)],
                        [entry(0, 2, 405000), entry(1)],
                        [entry(0, 3, 1093750), entry(1)],
                        [entry(0, 2, 405001), entry(1, 2, 405000)],
                        [entry(0, 2, 405000), entry(1, 2, 900000)],
                        [entry(0, 2, 11000000), entry(1, 2, 405000)]):
            with self.subTest(entries=entries):
                lock, error = gpu(entries).read_clk_lock_status()
                self.assertIsNone(lock)
                self.assertIsInstance(error, str)


if __name__ == '__main__':
    unittest.main()
