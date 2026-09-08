# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
import unittest
import threading
from types import SimpleNamespace
from unittest.mock import Mock
from druta.nvbackend import GPU

class KeplerTests(unittest.TestCase):
    def test_kepler_vf_paths_never_touch_nvapi_even_with_cached_layout(self):
        gpu = GPU.__new__(GPU)
        gpu._lock = threading.RLock()
        gpu.arch = Mock(return_value=GPU.ARCH_KEPLER)
        gpu.nvapi = Mock(ok=True)
        gpu._vfp_layout_cache = object()
        self.assertFalse(gpu.vf_curve_applicable())
        self.assertIsNone(gpu.vfp_layout())
        self.assertIsNone(gpu.read_vf_curve()[0])
        self.assertIsNone(gpu.read_vf_lock())
        self.assertFalse(gpu.apply_vf_deltas({0: 15000})[0])
        self.assertFalse(gpu.reset_vf_curve()[0])
        self.assertFalse(gpu.set_vf_lock(1000000)[0])
        self.assertFalse(gpu.clear_vf_lock()[0])
        self.assertEqual(gpu.nvapi.mock_calls, [])

    def test_other_architectures_still_require_runtime_vf_validation(self):
        gpu = GPU.__new__(GPU)
        for arch in (None, 3, 4, 6, 10):
            gpu.arch = Mock(return_value=arch)
            self.assertTrue(gpu.vf_curve_applicable())

    def test_kepler_callbacks_do_not_read_write_or_access_absent_widgets(self):
        from druta.druta import Druta
        app = Druta.__new__(Druta)
        app.gpu = SimpleNamespace(vf_curve_applicable=lambda: False)
        # No DPG context or editor state: a stale callback must return first.
        for name in ('vf_read', 'vf_reset', 'vf_apply', 'vf_deflatten',
                     'vf_ramp', 'vf_hard_deflatten', 'vf_rephase', 'oc_max'):
            getattr(app, name)()
        self.assertFalse(app.hold_for_read())
        self.assertEqual(app.n_vf_rows(), 0)

    def test_kepler_profiles_restore_without_a_curve(self):
        from druta import profiles
        from tests.test_tune_profiles import hardware
        gpu, rail, calls = hardware()
        gpu.vf_curve_applicable = lambda: False
        state = profiles.capture(gpu, rail)
        gpu.read_vf_curve.assert_not_called()
        self.assertFalse(state['vf_applicable'])
        self.assertEqual(profiles.incomplete(state), [])
        results = profiles.restore(gpu, state, rail=rail, i2c_verified=True)
        self.assertTrue(all(ok for ok, _ in results), results)
        gpu.apply_vf_deltas.assert_not_called()
        gpu.set_clock_offset.assert_called()
        rail.set_offset_mv.assert_called_once()

    def test_profile_vf_applicability_mismatch_refuses_before_any_write(self):
        from druta import profiles
        from tests.test_tune_profiles import hardware
        gpu, _, calls = hardware()
        for applicable, state in ((False, {'vf_deltas': {'0': 15000}}),
                                  (True, {'vf_applicable': False})):
            gpu.vf_curve_applicable = lambda: applicable
            results = profiles.restore(gpu, state)
            self.assertFalse(results[0][0])
            self.assertEqual(calls, [])

    def test_max_without_curve_does_not_partially_apply(self):
        from druta.druta import Druta
        app = Druta.__new__(Druta)
        app.guard = Mock(return_value=True)
        app.gpu = SimpleNamespace(static={}, set_fan=Mock(), set_voltage_boost=Mock())
        app.vf_points = []
        app.vf_read = Mock()
        app.log = Mock()
        app.autosave_before = Mock()
        app.oc_max()
        app.gpu.set_fan.assert_not_called()
        app.gpu.set_voltage_boost.assert_not_called()
        app.autosave_before.assert_not_called()

    def test_clock_grid_uses_upper_regime(self):
        gpu = GPU.__new__(GPU)
        # Low divider regime followed by fractional 13 MHz boost bins.
        clocks = list(range(136, 406, 2)) + [round(419 + i * 13.05) for i in range(61)]
        gpu.lockable_clocks_by_mem = Mock(return_value=[(3505, clocks)])
        self.assertAlmostEqual(gpu.clock_step_khz(), 13050, delta=20)

    def test_uniform_pascal_and_turing_grids_are_preserved(self):
        for step in (12657, 15000):
            gpu = GPU.__new__(GPU)
            clocks = [round(139 + i * step / 1000) for i in range(141)]
            gpu.lockable_clocks_by_mem = Mock(return_value=[(1000, clocks)])
            self.assertAlmostEqual(gpu.clock_step_khz(), step, delta=5)

    def test_kepler_zero_getter_cannot_enable_private_writes(self):
        gpu = GPU.__new__(GPU)
        gpu.arch = Mock(return_value=2)
        gpu.nvapi = SimpleNamespace(ok=True, ClkDomCtlGet=Mock(), ClkDomCtlSet=Mock())
        gpu._clkdom_get = Mock()
        self.assertIsNone(gpu.clkdom_layout())
        self.assertIsNone(gpu.read_rail_offset_mv())
        self.assertEqual(gpu.clkdom_controls_for_ui(), [])
        self.assertEqual(gpu.clkdom_pairing([{'domain':15, 'prog_mhz':270}]), {})
        self.assertFalse(gpu.set_rail_offset_mv(12.5)[0])
        self.assertFalse(gpu.set_clk_domain_offset(2,25)[0])
        gpu.nvapi.ClkDomCtlSet.assert_not_called()
        gpu._clkdom_get.assert_not_called()

if __name__ == '__main__':
    unittest.main()
