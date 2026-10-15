"""Manual MP voltage cannot escape its session through profiles or undo."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from druta import profiles
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


if __name__ == '__main__':
    unittest.main()
