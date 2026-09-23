"""Profile fan inputs reflect requested levels, independently of motor ramping."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from druta.druta import Druta


class FanSliderRestoreTests(unittest.TestCase):
    def sync(self, fans, captured_manual=True):
        app = Druta.__new__(Druta)
        app.gpu = SimpleNamespace(
            read=Mock(return_value={"fans": [(43, 1300), (48, 1400)]}),
            mem_offset_scale=Mock(return_value=(8, "MHz true")),
            read_voltage_boost=Mock(return_value=None),
            read_fan_control_state=Mock(return_value={"fans": fans} if fans else None))
        app.log = Mock()
        app.sync_knob_boxes = Mock()
        with patch("druta.druta.dpg.does_item_exist", side_effect=lambda tag: tag == "sl_fan"), \
                patch("druta.druta.dpg.set_value") as setter:
            app.sync_sliders_from_gpu({"fan_manual": captured_manual, "fan_pct": 99})
        app.sync_knob_boxes.assert_called_once()
        return app, setter

    def test_same_manual_targets_ignore_measured_and_saved_duty(self):
        app, setter = self.sync([{"level": 65, "manual": True}] * 2)
        setter.assert_called_once_with("sl_fan", 65)
        app.log.assert_not_called()

    def test_fresh_state_wins_when_profile_auto_restore_was_refused(self):
        _app, setter = self.sync([{"level": 80, "manual": True}], captured_manual=False)
        setter.assert_called_once_with("sl_fan", 80)

    def test_different_targets_or_policies_do_not_invent_one_shared_target(self):
        for fans in ([{"level": 50, "manual": True}, {"level": 65, "manual": True}],
                     [{"level": 50, "manual": True}, {"level": 0, "manual": False}]):
            with self.subTest(fans=fans):
                app, setter = self.sync(fans)
                setter.assert_not_called()
                self.assertIn("different targets or policies", app.log.call_args.args[0])

    def test_auto_and_unreadable_targets_do_not_pin_a_measured_speed(self):
        for fans in (None, [{"level": 23, "manual": False}]):
            with self.subTest(fans=fans):
                _app, setter = self.sync(fans)
                setter.assert_not_called()


if __name__ == "__main__":
    unittest.main()
