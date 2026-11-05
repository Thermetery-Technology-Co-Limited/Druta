# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Ordinary rail actions save the immediately prior selected user requests."""
import copy
import tempfile
from unittest.mock import Mock, patch

from druta import profiles
from tests.test_arch_ui_regressions import FakeUiTest
from tests.test_hold_headroom import AppTestCase
from tests.test_volt_rails import fake_gpu


class RailLimitProfileUiTests(FakeUiTest):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = patch.object(profiles, 'DIR', directory.name)
        store.start()
        self.addCleanup(store.stop)
        self.app.gpu = fake_gpu('blackwell')
        self.app.gpu.volt_limits_write_enabled = True
        self.app.gpu.read_volt_rail_limits()
        self.app._rail_writable = {0, 1}
        self.app._i2c_busy = False
        self.app._pending_load = None
        self.app.rail = None
        self.app.guard = Mock(return_value=True)
        self.app.refresh_volt_limits = Mock()
        self.app.sync_hold_headroom = Mock()
        self.app.refresh_profile_list = Mock()
        self.app.set_pending_load = Mock()
        self.app.profile_failure = Mock()
        self.app._clk_lock = {'kind': 'held'}
        for name in ('_finish_manual_voltage', 'rail_for_profile', 'i2c_verified', 'sync_sliders_from_gpu'):
            setattr(self.app, name, Mock(side_effect=AssertionError('unrelated ' + name)))
        self.values.update(vlim_mode=True, vlim_link=False)

    def latest(self):
        return profiles.load(profiles.list_profiles()[0][0])

    def test_linked_apply_undo_restores_pair_and_preserves_other_fields(self):
        before = copy.deepcopy(self.app.gpu.nvapi.control)
        self.values['vlim_link'] = True
        self.app.apply_vlim(0, reliability=1050.125)
        state = self.latest()
        self.assertEqual(state['rail_limits'], {'0': {'reliability': 0, 'alt_reliability': 0}})
        self.app.undo_last_write()
        self.assertEqual(self.app.gpu.nvapi.control, before)
        self.assertEqual(self.app._clk_lock, {'kind': 'held'})
        self.app.sync_hold_headroom.assert_called_with(replan=True)
        self.app.profile_failure.assert_not_called()

    def test_unchanged_apply_does_not_replace_undo_or_write(self):
        row = self.app.gpu.read_volt_rail_limits()[0]
        self.app.apply_vlim(0, reliability=self.app.gpu.abs_limit_mv(row, 'reliability'))
        self.assertEqual(profiles.list_profiles(), [])
        self.app.gpu._write_rail_records.assert_not_called()

    def test_stock_saves_immediately_previous_value_not_first_read(self):
        self.app.apply_vlim(0, reliability=1050.125)
        changed = copy.deepcopy(self.app.gpu.nvapi.control)
        self.app.stock_knob('vlim_rel')
        self.assertEqual(self.app.gpu.nvapi.control[0][0], 0)
        self.assertEqual(self.latest()['rail_limits'], {'0': {'reliability': changed[0][0]}})
        self.app.undo_last_write()
        self.assertEqual(self.app.gpu.nvapi.control, changed)

    def test_reset_all_saves_both_rails(self):
        self.app.gpu.set_volt_rail_limits(0, reliability=1050.125)
        self.app.gpu.set_volt_rail_limits(1, vmin=725.25)
        changed = copy.deepcopy(self.app.gpu.nvapi.control)
        self.app.apply_vlim_reset()
        state = self.latest()
        self.assertEqual(set(state['rail_limits']), {'0', '1'})
        self.app.undo_last_write()
        self.assertEqual(self.app.gpu.nvapi.control, changed)

    def test_snapshot_failure_blocks_apply_but_explicit_reset_remains_available(self):
        self.app.gpu.set_volt_rail_limits(0, reliability=1050.125)
        self.app.gpu._write_rail_records.reset_mock()
        with patch.object(profiles, 'autosave_state', side_effect=OSError('disk full')):
            self.app.apply_vlim(0, reliability=1040)
            self.app.gpu._write_rail_records.assert_not_called()
            self.app.stock_knob('vlim_rel')
        self.assertEqual(self.app.gpu.nvapi.control[0][0], 0)
        self.assertTrue(any('continuing the explicit first-read reset' in c.args[0]
                            for c in self.app.log.call_args_list))

    def test_named_save_captures_applied_values_and_load_is_scoped(self):
        self.app._profile_save_scope = 'rail_limits'
        self.app.gpu.set_volt_rail_limits(0, reliability=1050.125)
        self.values.update(prof_name='rails', in_vlim_rel=9999)
        self.app.gpu._write_rail_records.reset_mock()
        self.app.save_profile()
        saved = profiles.load('rails')
        self.app.gpu._write_rail_records.assert_not_called()
        self.app.gpu.reset_volt_rail_limits()
        self.app.load_profile('rails')
        self.assertEqual(profiles.capture_rail_limits(self.app.gpu)['rail_limits'], saved['rail_limits'])
        self.app.profile_failure.assert_not_called()

    def test_automatic_load_refused_without_writes(self):
        state = profiles.capture_rail_limits(self.app.gpu)
        self.app.begin_profile_load('rails', state, automatic=True)
        self.app.finish_profile_load('rails', state, automatic=True)
        self.assertEqual(self.app.profile_failure.call_count, 2)
        self.app.gpu._write_rail_records.assert_not_called()


class RailLimitHeldProfileTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.gpu.slot = lambda: '0000:01:00.0'
        self.gpu.volt_rail_limit_fields = lambda rail: self.gpu.VOLT_LIMIT_FIELDS if rail == 0 else ()
        self.gpu.volt_limits_write_enabled = True
        self.gpu.stock_limit_mv = lambda rail, field: None
        self.gpu.read_volt_rail_state = lambda: None
        self.app.rail_limit_diagnostics = lambda: {'available': True, 'rails': {0: {'available': True}}}
        self.app.ov_carryover = Mock()
        self.app.sync_vcap_to_ceiling = Mock()
        self.app._rail_writable = {0}
        self.app.guard = lambda: True
        self.app.gate_is_off = lambda tag: False
        self.app._i2c_busy, self.app._pending_load, self.app.rail = False, None, None
        self.app.refresh_profile_list = Mock()
        self.app.set_pending_load = Mock()
        self.app.profile_failure = Mock()

    def test_apply_undo_reconciles_retained_hold_and_release_keeps_user_baseline(self):
        self.hold(1093.75)
        held = dict(self.app._clk_lock)
        with patch.object(profiles, 'DIR', str(self.root / 'profiles')):
            self.app.apply_vlim(0, reliability=1065)
            state = profiles.load(profiles.list_profiles()[0][0])
            self.assertEqual(state['rail_limits'], {'0': {'reliability': 0}})
            self.assertEqual(self.gpu.user_rail_limits()[0]['reliability'], -3.75)
            self.app.undo_last_write()
        self.app.profile_failure.assert_not_called()
        self.assertEqual(self.app._clk_lock, held)
        self.assertEqual(self.limits(), (1093.75, 1118.75, 1125.0))
        self.app.set_lock_state(None)
        self.assertEqual(self.limits(), (1068.75, 1093.75, 1125.0))

    def test_matching_held_raise_is_saved_and_replayed_as_explicit_user_choice(self):
        self.hold(1093.75)
        held = dict(self.app._clk_lock)
        with patch.object(profiles, 'DIR', str(self.root / 'profiles')):
            self.app.apply_vlim(0, reliability=1093.75)
            self.assertEqual(self.gpu.user_rail_limits()[0]['reliability'], 25.0)
            saved = profiles.capture_rail_limits(self.gpu, {0: ['reliability']})
            profiles.save('chosen', saved)
            self.app.undo_last_write()
            self.assertEqual(self.gpu.user_rail_limits()[0]['reliability'], 0.0)
            self.app.load_profile('chosen')
        self.app.profile_failure.assert_not_called()
        self.assertEqual(self.app._clk_lock, held)
        self.assertEqual(self.gpu.user_rail_limits()[0]['reliability'], 25.0)
        self.app.set_lock_state(None)
        self.assertEqual(self.limits(), (1093.75, 1093.75, 1125.0))
