# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise the registered NCT button with DearPyGui's real dispatcher."""

import unittest
from types import SimpleNamespace

import dearpygui.dearpygui as dpg

from druta.druta import Druta


class FakeCurrentDac:
    current_dac = True

    def __init__(self):
        self.p = SimpleNamespace(regulator="Nuvoton NCT3933U",
                                 port=1, addr7=0x15)
        self.telemetry_calls = 0
        self.fail = False
        self.outputs = [0x00, 0x00, 0x00]
        self.currents_ua = [0, 0, 0]

    def telemetry(self):
        self.telemetry_calls += 1
        if self.fail:
            raise OSError("I2C unavailable")
        return {"outputs": list(self.outputs), "configuration": 0x00,
                "currents_ua": list(self.currents_ua)}


class CurrentDacCallbackTests(unittest.TestCase):
    def make_app(self, log):
        app = Druta.__new__(Druta)  # no app startup, GPU, or native window
        app.rail = FakeCurrentDac()
        app._i2c_busy = False
        app._ctl_widgets = []
        app.s = lambda value: value
        app.log = log
        return app

    def test_registered_read_settings_button_dispatches_with_three_dpg_arguments(self):
        app = self.make_app(lambda *_args: None)

        dpg.create_context()
        try:
            with dpg.window(tag="test_nct_window", show=False):
                app.build_current_dac_controls()
            self.assertEqual(app.rail.telemetry_calls, 1)  # initial draw
            callback = dpg.get_item_configuration("nct_read_settings")["callback"]
            # This is the callback that the real button registered, delivered
            # through DPG's own argument-counting runner as a button click job.
            dpg.run_callbacks([[callback, "nct_read_settings", None, None]])
            self.assertEqual(app.rail.telemetry_calls, 2)
            self.assertEqual(dpg.get_value("nct_live_1"), "+0 µA  raw 0x00")
        finally:
            dpg.destroy_context()

    def test_failed_read_is_visible_and_next_click_recovers_without_losing_old_values(self):
        logs = []
        app = self.make_app(lambda message, ok=None: logs.append((message, ok)))

        dpg.create_context()
        try:
            with dpg.window(tag="test_nct_window", show=False):
                app.build_current_dac_controls()
            callback = dpg.get_item_configuration("nct_read_settings")["callback"]
            previous = dpg.get_value("nct_live_1")

            app.rail.fail = True
            dpg.run_callbacks([[callback, "nct_read_settings", None, None]])
            self.assertEqual(dpg.get_value("nct_live_1"), previous)
            self.assertIn("I2C unavailable", dpg.get_value("nct_status"))
            self.assertTrue(any("I2C unavailable" in message and ok is False
                                for message, ok in logs))

            app.rail.fail = False
            app.rail.outputs[0] = 0x81
            app.rail.currents_ua[0] = 10
            dpg.run_callbacks([[callback, "nct_read_settings", None, None]])
            self.assertEqual(app.rail.telemetry_calls, 3)
            self.assertEqual(dpg.get_value("nct_live_1"), "+10 µA  raw 0x81")
            self.assertNotIn("failed", dpg.get_value("nct_status").lower())
            self.assertIn("Register readback", dpg.get_value("nct_status"))
        finally:
            dpg.destroy_context()


if __name__ == "__main__":
    unittest.main()
