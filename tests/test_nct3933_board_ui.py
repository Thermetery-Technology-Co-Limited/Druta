"""NCT3933U board-mapping GUI behavior without a GPU or I2C transport."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import dearpygui.dearpygui as dpg

from druta import druta, nct3933_board
from druta.druta import Druta
from tests.test_arch_ui_regressions import FakeUiTest


def fake_dac(*, configuration=0, currents=(-10, -10, -10)):
    profile = SimpleNamespace(
        regulator="Nuvoton NCT3933U", read_only=False,
        name="NCT3933U test controller", rail="OUT1/OUT2/OUT3",
        port=1, addr7=0x15,
        src={"controller": "NCT3933U", "contract": 1,
             "units": "nominal_microamps", "port": 1, "addr7": 0x15})
    control = {"outputs": [1, 1, 1], "configuration": configuration}
    return SimpleNamespace(
        current_dac=True, p=profile, addr7=0x15,
        telemetry=Mock(return_value={"outputs": [1, 1, 1],
                                     "configuration": configuration,
                                     "currents_ua": list(currents)}),
        capture_control=Mock(return_value=control), validate_control=Mock(),
        set_current_ua=Mock(return_value=(True, "stored")),
        zero_outputs=Mock(return_value=(True, "zeroed")),
    )


class Nct3933BoardRenderTests(unittest.TestCase):
    def setUp(self):
        dpg.create_context()
        self.app = Druta.__new__(Druta)
        self.app.gpu = SimpleNamespace(static={"uuid": "GPU-test"})
        self.app.rail = fake_dac()
        self.app.log = Mock()
        self.app.s = lambda value: value
        self.app._ctl_widgets = []
        self.app._i2c_busy = False
        self.app._nct_display_context = (id(self.app.gpu), id(self.app.rail))
        self.app._nct_display_mode = nct3933_board.RAW_MODE

    def tearDown(self):
        dpg.destroy_context()

    def build(self):
        with dpg.window():
            self.app.build_current_dac_controls()

    def test_mapped_render_uses_gpu_memory_pll_order_and_normal_doubled_grids(self):
        self.build()
        table = dpg.get_item_parent(dpg.get_item_parent("nct_label_3"))
        self.assertEqual(
            [dpg.get_item_alias(dpg.get_item_children(row, 1)[0])
             for row in dpg.get_item_children(table, 1)],
            ["nct_label_3", "nct_label_1", "nct_label_2"])
        with patch.object(nct3933_board, "save_mode", return_value=True):
            callback = dpg.get_item_configuration("nct_mapping")["callback"]
            callback(None, nct3933_board.VOLTAGE_MODE, None)

        self.assertEqual(dpg.get_value("nct_label_3"), "GPU offset (OUT3)")
        self.assertEqual(dpg.get_value("nct_label_1"), "Memory offset (OUT1)")
        self.assertEqual(dpg.get_value("nct_label_2"), "PEX-PLL offset (OUT2)")
        for channel, expected in ((3, 10), (1, 10), (2, 66)):
            self.assertEqual(dpg.get_value(f"nct_current_{channel}"), expected)
            self.assertEqual(dpg.get_value(f"nct_unit_{channel}"), "mV")
            self.assertIn(f"{expected} mV step", dpg.get_value(f"nct_range_{channel}"))

        # CR05 x2 flags 0/2/4 double GPU, memory, and PEX/PLL independently.
        self.app._show_current_dac_settings([-20, -20, -20], [2, 2, 2], 0x15)
        for channel, expected in ((3, 20), (1, 20), (2, 132)):
            self.assertEqual(dpg.get_value(f"nct_current_{channel}"), expected)
            self.assertIn(f"{expected} mV step", dpg.get_value(f"nct_range_{channel}"))

    def test_mapping_callback_saves_only_after_readback_and_discards_old_unit_jobs(self):
        self.build()
        initial_generation = 37
        self.app._ui_gen = initial_generation
        late_apply = Mock()
        callback = dpg.get_item_configuration("nct_mapping")["callback"]
        jobs = [[callback, "nct_mapping", nct3933_board.VOLTAGE_MODE, None],
                [lambda: late_apply(), "nct_apply_3", None, None]]
        with patch.object(nct3933_board, "save_mode", return_value=True) as save, \
                patch.object(dpg, "get_callback_queue", return_value=jobs), \
                patch.object(dpg, "is_dearpygui_running", return_value=True):
            dpg.set_value("nct_mapping", nct3933_board.VOLTAGE_MODE)
            self.app.dispatch_callbacks()
        save.assert_called_once_with(self.app.gpu, self.app.rail, nct3933_board.VOLTAGE_MODE)
        late_apply.assert_not_called()
        self.assertEqual(self.app._ui_gen, initial_generation + 1)
        self.assertEqual(dpg.get_value("nct_mapping"), nct3933_board.VOLTAGE_MODE)
        self.assertEqual(dpg.get_value("nct_unit_3"), "mV")

    def test_mapping_telemetry_failure_preserves_old_mode_and_typed_units(self):
        self.build()
        dpg.set_value("nct_current_3", 123)
        self.app.rail.telemetry.side_effect = ValueError("bus lost")
        with patch.object(nct3933_board, "save_mode") as save:
            dpg.set_value("nct_mapping", nct3933_board.VOLTAGE_MODE)
            dpg.get_item_configuration("nct_mapping")["callback"](
                None, nct3933_board.VOLTAGE_MODE, None)
        save.assert_not_called()
        self.assertEqual(self.app._nct_display_mode, nct3933_board.RAW_MODE)
        self.assertEqual(dpg.get_value("nct_mapping"), nct3933_board.RAW_MODE)
        self.assertEqual(dpg.get_value("nct_current_3"), 123)
        self.assertEqual(dpg.get_value("nct_unit_3"), "µA")

    def test_mapping_callback_refuses_while_busy_without_read_or_save(self):
        self.build()
        self.app._i2c_busy = True
        self.app._ui_gen = 4
        with patch.object(nct3933_board, "save_mode") as save:
            dpg.set_value("nct_mapping", nct3933_board.VOLTAGE_MODE)
            dpg.get_item_configuration("nct_mapping")["callback"](
                None, nct3933_board.VOLTAGE_MODE, None)
        self.app.rail.telemetry.assert_called_once_with()  # initial build only
        save.assert_not_called()
        self.assertEqual(self.app._ui_gen, 4)
        self.assertEqual(dpg.get_value("nct_mapping"), nct3933_board.RAW_MODE)
        self.assertIn("wait for I2C", self.app.log.call_args.args[0])


class Nct3933BoardActionTests(FakeUiTest):
    def setUp(self):
        super().setUp()
        self.app.gpu = SimpleNamespace(static={"uuid": "GPU-test"})
        self.app.rail = fake_dac()
        self.app.guard = Mock(return_value=True)
        self.app.i2c_gate = Mock(return_value=(True, ""))
        self.app.autosave_before = Mock()
        self.app.report = Mock()
        self.app.refresh_current_dac_settings = Mock(return_value=True)
        self.app._nct_display_context = (id(self.app.gpu), id(self.app.rail))
        self.app._nct_display_mode = nct3933_board.VOLTAGE_MODE

    def assert_apply(self, channel, request, native):
        self.app.apply_current_dac(channel, request)
        expected = {"outputs": [1, 1, 1], "configuration": 0}
        self.app.rail.set_current_ua.assert_called_once_with(
            channel, native, acknowledged=True, expected=expected)
        self.app.autosave_before.assert_called_once_with(f"i2c-current-dac-out{channel}")

    def test_mapped_positive_and_negative_offsets_convert_to_reversed_native_current(self):
        self.assert_apply(3, 10, -10)
        self.app.rail.set_current_ua.reset_mock()
        self.app.autosave_before.reset_mock()
        self.assert_apply(3, -10, 10)

        self.app.rail.set_current_ua.reset_mock()
        self.app.autosave_before.reset_mock()
        self.assert_apply(2, 66, -10)
        self.app.rail.set_current_ua.reset_mock()
        self.app.autosave_before.reset_mock()
        self.assert_apply(2, -66, 10)

    def test_off_grid_mapped_values_refuse_before_autosave_or_controller_write(self):
        for channel, request in ((3, 5), (2, 10)):
            with self.subTest(channel=channel, request=request):
                self.app.apply_current_dac(channel, request)
                self.app.autosave_before.assert_not_called()
                self.app.rail.set_current_ua.assert_not_called()
                self.assertIn("multiple", self.app.log.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
