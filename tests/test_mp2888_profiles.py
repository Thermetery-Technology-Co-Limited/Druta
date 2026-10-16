"""Manual MP voltage cannot escape its session through profiles or undo."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from druta import profiles, railctl
from druta.controllers.mp2888 import MP2888Candidate
from tests.controllers.test_mp2888_pmbus import PMBus
from tests.test_tune_profiles import hardware


class SessionRail:
    session_only_voltage = True
    absolute_voltage = True

    def __init__(self, enabled=False):
        self.p = SimpleNamespace(src={'kind': 'mp2888-pmbus-v1'},
                                 name='MP2888A candidate', regulator='MPS MP2888A',
                                 rail='Controller output', port=1, read_only=False)
        self.addr7 = 0x20
        self.capture_control = Mock(return_value={'enabled': enabled})
        self.restore_control = Mock(side_effect=AssertionError('session voltage must not replay'))
        self.set_offset_mv = Mock(side_effect=AssertionError('legacy offset must not replay'))


class MP2888ProfileTests(unittest.TestCase):
    def real_adapters(self, offset=8):
        bus = PMBus()
        bus.regs[0x23] = offset
        recipe = next(p for p in railctl.load_profiles() if p.regulator == 'MPS MP2888A')
        legacy = MP2888Candidate(recipe, bus, 1, 0x20)
        self.assertTrue(legacy.present())
        return bus, legacy, legacy.for_control_method('pmbus')

    def test_capture_omits_session_control_and_reports_that_limitation(self):
        g, _, _ = hardware()
        rail = SessionRail(True)
        state = profiles.capture(g, rail)
        self.assertIsNone(state['i2c'])
        self.assertTrue(any('session-only' in reason for reason in profiles.incomplete(state)))
        self.assertIn('session-only', profiles.summarize(state))
        rail.capture_control.assert_called_once()

    def test_auto_does_not_make_an_ordinary_gpu_profile_incomplete(self):
        g, _, _ = hardware()
        without_rail = profiles.capture(g)
        state = profiles.capture(g, SessionRail(False))
        self.assertIsNone(state['i2c'])
        self.assertEqual(profiles.incomplete(state), profiles.incomplete(without_rail))

    def test_legacy_offsets_and_forged_manual_state_stop_before_any_write(self):
        for value in ({'offset_mv': 43.75}, {'control': {'enabled': True, 'command': 176}}):
            with self.subTest(value=value):
                g, _, writes = hardware()
                rail = SessionRail()
                state = {'schema': 2, 'i2c': dict(profiles.rail_identity(rail), **value)}
                result = profiles.restore(g, state, apply_curve=False, rail=rail, i2c_verified=True)
                self.assertFalse(result[0][0])
                self.assertIn('session-only', result[0][1])
                self.assertEqual(writes, [])
                rail.restore_control.assert_not_called()
                rail.set_offset_mv.assert_not_called()

    def test_clock_only_profile_requires_auto_even_without_i2c_payload(self):
        g, _, writes = hardware()
        rail = SessionRail(True)
        state = {'schema': 2, 'core_off_mhz': 100}
        result = profiles.restore(g, state, apply_curve=False, rail=rail)
        self.assertFalse(result[0][0])
        self.assertIn('Auto', result[0][1])
        self.assertEqual(writes, [])
        rail.capture_control.return_value = {'enabled': False}
        self.assertIsNone(profiles.preflight(g, state, rail, apply_curve=False))

    def test_unknown_manual_state_cannot_allow_a_clock_profile(self):
        g, _, writes = hardware()
        rail = SessionRail()
        rail.capture_control.side_effect = ValueError('controller context unreadable')
        result = profiles.restore(g, {'schema': 2, 'core_off_mhz': 100}, apply_curve=False, rail=rail)
        self.assertFalse(result[0][0])
        self.assertIn('unreadable', result[0][1])
        self.assertEqual(writes, [])

    def test_manual_recipe_selection_in_auto_preserves_existing_legacy_profile_and_envelope(self):
        g, _, _ = hardware()
        bus, legacy, manual = self.real_adapters()
        expected = profiles.capture(g, legacy)
        captured = profiles.capture(g, manual)
        self.assertEqual(captured['i2c'], expected['i2c'])
        self.assertEqual(captured['i2c']['offset_mv'], 50)
        self.assertEqual(captured['xoc'], expected['xoc'])
        self.assertEqual(profiles.incomplete(captured), profiles.incomplete(expected))
        self.assertEqual(bus.writes, [])

    def test_legacy_profile_replay_restores_offset_without_manual_mode_commands(self):
        g, _, writes = hardware()
        bus, legacy, _ = self.real_adapters()
        captured = profiles.capture(g, legacy)
        state = {'schema': 2, 'device': captured['device'], 'i2c': captured['i2c']}
        bus.regs[0x23] = 0
        result = profiles.restore(g, state, apply_curve=False, rail=legacy, i2c_verified=True)
        self.assertTrue(all(ok for ok, _ in result), result)
        self.assertEqual(bus.regs[0x23], 8)
        self.assertEqual({reg for reg, _, _ in bus.writes}, {0x23})
        self.assertEqual(writes, [])

    def test_canonical_adapter_does_not_hide_an_owned_manual_request_from_profiles(self):
        g, _, writes = hardware()
        bus, legacy, manual = self.real_adapters(offset=0)
        entry = manual.capture_control()
        self.assertTrue(manual.set_voltage_mv(1100, acknowledged=True, expected=entry)[0])
        bus.writes.clear()
        captured = profiles.capture(g, legacy)
        self.assertIsNone(captured['i2c'])
        self.assertTrue(any('session-only' in item for item in profiles.incomplete(captured)))
        result = profiles.restore(g, {'schema': 2, 'core_off_mhz': 100},
                                  apply_curve=False, rail=legacy)
        self.assertFalse(result[0][0])
        self.assertIn('manual PMBus', result[0][1])
        self.assertEqual(bus.writes, [])
        self.assertEqual(writes, [])
        self.assertTrue(manual.restore_control(entry, recovery=True,
                                               expected=manual.last_written_control)[0])
        self.assertIsNone(profiles.preflight(g, {'schema': 2, 'core_off_mhz': 100},
                                              legacy, apply_curve=False))


if __name__ == '__main__':
    unittest.main()
