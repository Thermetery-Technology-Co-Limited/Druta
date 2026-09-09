"""Focused coverage for the shunt-mod parallel-resistance helper."""
import math
import unittest

import dearpygui.dearpygui as dpg

from druta.druta import Druta
from druta import shuntmod


class ParallelResistanceTests(unittest.TestCase):
    def test_two_resistors_match_the_workbench_examples(self):
        self.assertAlmostEqual(shuntmod.parallel_resistance([5.0, 3.0]),
                               1.875)
        self.assertAlmostEqual(shuntmod.parallel_resistance([5.0, 5.0, 5.0]),
                               5.0 / 3.0)

    def test_invalid_or_zero_values_do_not_produce_a_fake_equivalent(self):
        for values in ([], [5.0, 0.0], [5.0, -1.0], [5.0, "bad"],
                       [5.0, math.inf], [5.0, math.nan]):
            self.assertIsNone(shuntmod.parallel_resistance(values))


class ParallelResistanceUiTests(unittest.TestCase):
    def setUp(self):
        dpg.create_context()
        self.app = Druta.__new__(Druta)
        self.app.s = lambda value: value
        self.app.shunt_parallel_resistors = [5.0, 5.0]
        with dpg.window():
            with dpg.table(tag="shunt_parallel_table"):
                dpg.add_table_column()
                dpg.add_table_column()
                dpg.add_table_column()
            dpg.add_text("", tag="shunt_parallel_result")

    def tearDown(self):
        dpg.destroy_context()

    def test_starts_with_two_inputs_and_updates_live(self):
        self.app.draw_shunt_parallel_rows()
        self.assertTrue(dpg.does_item_exist("sh_pr0"))
        self.assertTrue(dpg.does_item_exist("sh_pr1"))
        self.assertEqual(dpg.get_value("shunt_parallel_result"),
                         "5 || 5 mOhm = 2.5 mOhm")

        self.app.shunt_parallel_edit(app_data=3.0, user_data=1)
        self.assertEqual(dpg.get_value("shunt_parallel_result"),
                         "5 || 3 mOhm = 1.875 mOhm")

    def test_adds_more_resistors_and_keeps_the_first_two(self):
        self.app.draw_shunt_parallel_rows()
        self.app.shunt_parallel_add()
        self.assertEqual(self.app.shunt_parallel_resistors, [5.0, 5.0, 5.0])
        self.assertEqual(dpg.get_value("shunt_parallel_result"),
                         "5 || 5 || 5 mOhm = 1.667 mOhm")

        self.app.shunt_parallel_remove(user_data=0)
        self.assertEqual(len(self.app.shunt_parallel_resistors), 3)
        self.app.shunt_parallel_remove(user_data=2)
        self.assertEqual(self.app.shunt_parallel_resistors, [5.0, 5.0])


if __name__ == "__main__":
    unittest.main()
