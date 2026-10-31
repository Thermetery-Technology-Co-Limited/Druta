# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure range tests and an isolated, hidden native DearPyGui render check."""

import ctypes
import importlib.util
import json
import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

from druta.power_slider_style import extended_segments, outside_original

ROOT = Path(__file__).resolve().parents[1]
HAS_WINDOWS_DPG = sys.platform == "win32" and importlib.util.find_spec("dearpygui.dearpygui") is not None


class PowerSliderGeometryTests(unittest.TestCase):
    def test_upper_extension_uses_current_and_original_bounds(self):
        self.assertEqual(extended_segments((50, 250), (50, 150), 600), ((300, 600),))
        self.assertEqual(extended_segments((10, 50), (10, 40), 200), ((150, 200),))

    def test_both_sides_and_negative_bounds(self):
        self.assertEqual(extended_segments((-50, 150), (0, 100), 400), ((0, 100), (300, 400)))

    def test_original_range_can_be_wider_than_current_range(self):
        self.assertEqual(extended_segments((100, 200), (0, 300), 200), ())
        self.assertEqual(extended_segments((100, 200), (0, 150), 200), ((100, 200),))

    def test_nonoverlapping_ranges_are_clipped(self):
        self.assertEqual(extended_segments((100, 200), (250, 300), 200), ((0, 200),))
        self.assertEqual(extended_segments((100, 200), (0, 50), 200), ((0, 200),))

    def test_unknown_or_invalid_ranges_do_not_claim_an_extension(self):
        for original in (None, (200, 100), (0, float("inf")), (float("nan"), 10), (), (1, 2, 3)):
            with self.subTest(original=original):
                self.assertEqual(extended_segments((100, 200), original, 200), ())
                self.assertFalse(outside_original(300, original))

    def test_degenerate_slider_or_width(self):
        for bounds, width in (((1, 1), 200), ((2, 1), 200), ((0, 2), 0), ((0, 2), -1)):
            self.assertEqual(extended_segments(bounds, (0, 1), width), ())

    def test_input_boundaries_are_inclusive_on_the_request_grid(self):
        for value in (123.25, 150, 287.75):
            self.assertFalse(outside_original(value, (123.25, 287.75)))
        for value in (123.249, 287.751):
            self.assertTrue(outside_original(value, (123.25, 287.75)))
        for value in (None, "", float("nan"), float("inf")):
            self.assertFalse(outside_original(value, (123.25, 287.75)))

    def test_c_float_fractional_endpoints_use_the_power_request_milliwatt_grid(self):
        bounds = (180.001, 450.001)
        for value in bounds:
            self.assertFalse(outside_original(ctypes.c_float(value).value, bounds))
        for value in (180.000, 450.002):
            self.assertTrue(outside_original(ctypes.c_float(value).value, bounds))


@unittest.skipUnless(HAS_WINDOWS_DPG, "requires the Windows DearPyGui renderer")
class PowerSliderRenderTests(unittest.TestCase):
    def test_native_clipping_geometry_text_and_lifecycle(self):
        # The offscreen viewport is hidden immediately; no other process,
        # window, cursor or adapter is accessed. Each DPG context gets a process.
        script = textwrap.dedent(
            """
            import ctypes
            import json
            import os
            import dearpygui.dearpygui as dpg
            from druta.power_slider_style import PowerSliderStyle

            dpg.create_context()
            try:
                title = f"Druta hidden power slider test {os.getpid()}"
                dpg.create_viewport(title=title, width=800, height=420, x_pos=-32000, y_pos=-32000)
                called = []
                def callback(*args):
                    called.append(args)
                with dpg.theme() as previous_theme:
                    with dpg.theme_component(dpg.mvAll):
                        dpg.add_theme_color(dpg.mvThemeCol_Border, (20, 40, 60))
                def build():
                    with dpg.window(tag="root"):
                        dpg.add_text("Unrelated header", tag="header")
                        with dpg.child_window(tag="panel", height=180):
                            with dpg.table(header_row=False):
                                dpg.add_table_column(init_width_or_weight=3)
                                dpg.add_table_column(init_width_or_weight=1)
                                with dpg.table_row():
                                    with dpg.group(tag="slider_group"):
                                        dpg.add_slider_float(tag="slider", min_value=50, max_value=250,
                                            default_value=125, width=-1, format=" ", callback=callback)
                                    dpg.add_input_float(tag="input", default_value=125, width=-1, step=0,
                                                        callback=callback)
                                with dpg.table_row():
                                    dpg.add_text("Next row", tag="next_row")
                                    dpg.add_text("Unaffected", tag="peer")
                            for i in range(20):
                                dpg.add_text(f"filler {i}")
                    dpg.bind_item_theme("input", previous_theme)
                    return PowerSliderStyle("slider", "input", original_bounds=(50, 150))
                style = build()
                dpg.setup_dearpygui()
                dpg.show_viewport()
                user32 = ctypes.windll.user32
                user32.FindWindowW.restype = ctypes.c_void_p
                handle = user32.FindWindowW(None, title)
                pid = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(ctypes.c_void_p(handle), ctypes.byref(pid))
                assert pid.value == os.getpid()
                user32.ShowWindow(ctypes.c_void_p(handle), 0)
                dpg.set_primary_window("root", True)
                def frames(count=4):
                    for _ in range(count):
                        style.refresh()
                        dpg.render_dearpygui_frame()
                frames()
                rows = []
                for width, scale in ((800, 1), (520, 1), (1100, 1.5), (800, 1)):
                    dpg.set_viewport_width(width)
                    dpg.set_global_font_scale(scale)
                    frames()
                    sw, sh = dpg.get_item_rect_size("slider")
                    rect = dpg.get_item_configuration(style._rectangles[0])
                    rows.append({"width": sw, "height": sh,
                        "aligned": dpg.get_item_rect_min("slider") == dpg.get_item_rect_min(style._drawlist),
                        "size": dpg.get_item_rect_size(style._drawlist),
                        "boundary": rect["pmin"][0],
                        "overlay_disabled": not dpg.get_item_configuration(style._overlay)["enabled"]})
                themes = []
                for value in (50, 150, 49.5, 150.5, 125):
                    dpg.focus_item("slider")
                    frames()
                    dpg.set_value("input", value)
                    dpg.focus_item("input")
                    frames()
                    themes.append({"value": value, "actual_value": dpg.get_value("input"),
                                   "theme": dpg.get_item_theme("input"), "expected": style._themes[1],
                                   "amber": dpg.get_item_theme("input") == style._themes[1],
                                   "focused": dpg.is_item_focused("input")})
                dpg.focus_item("slider")
                frames()
                focused = dpg.is_item_focused("slider") and not dpg.is_item_focused(style._drawlist)
                dpg.configure_item("slider", enabled=False)
                frames()
                disabled_alpha = dpg.get_item_configuration(style._rectangles[0])["fill"][3]
                dpg.configure_item("slider", enabled=True)
                dpg.set_y_scroll("panel", 110)
                frames()
                scrolled = {
                    "aligned": dpg.get_item_rect_min("slider") == dpg.get_item_rect_min(style._drawlist),
                    "slider_visible": dpg.is_item_visible("slider"),
                    "draw_visible": dpg.is_item_visible(style._drawlist),
                    "parent": dpg.get_item_parent(style._overlay) in ("slider_group", dpg.get_alias_id("slider_group")),
                }
                style.refresh(original_bounds=None)
                frames()
                unknown = not dpg.get_item_configuration(style._overlay)["show"]
                unknown = unknown and dpg.get_item_theme("input") == style._themes[0]
                dpg.set_y_scroll("panel", 0)
                style.refresh(original_bounds=(50, 250))
                frames()
                no_extension = not dpg.get_item_configuration(style._overlay)["show"]
                style.refresh(original_bounds=(100, 200))
                frames()
                both = all(dpg.get_item_configuration(r)["show"] for r in style._rectangles)
                config = dpg.get_item_configuration("slider")
                native_preserved = config["min_value"] == 50 and config["max_value"] == 250
                native_preserved = native_preserved and config["callback"] is callback and config["format"] == " "
                native_preserved = native_preserved and dpg.get_value("slider") == 125
                owned = [style._overlay, style._themes[1]]
                style.close()
                cleaned = all(not dpg.does_item_exist(item) for item in owned)
                cleaned = cleaned and dpg.get_item_theme("input") == previous_theme
                style.close()
                dpg.delete_item("root")
                style = build()
                dpg.set_primary_window("root", True)
                frames()
                rebuilt = dpg.get_item_configuration(style._overlay)["show"]
                dpg.delete_item("root")
                gone = not style.refresh() and not dpg.does_item_exist(style._themes[1])
                print(json.dumps(dict(rows=rows, themes=themes, focused=focused, disabled_alpha=disabled_alpha,
                    scrolled=scrolled, unknown=unknown, no_extension=no_extension, both=both,
                    native_preserved=native_preserved, cleaned=cleaned, rebuilt=rebuilt, gone=gone)))
            finally:
                dpg.destroy_context()
            """
        )
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + environment.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=ROOT, env=environment, capture_output=True, text=True, timeout=60
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads(result.stdout.strip().splitlines()[-1])
        for row in data["rows"]:
            self.assertTrue(row["aligned"], row)
            self.assertTrue(row["overlay_disabled"], row)
            self.assertEqual(row["size"], [row["width"], row["height"]])
            self.assertAlmostEqual(row["boundary"], row["width"] / 2)
        self.assertGreater(len({row["width"] for row in data["rows"]}), 2)
        self.assertEqual([entry["amber"] for entry in data["themes"]], [False, False, True, True, False], data)
        self.assertTrue(all(entry["focused"] for entry in data["themes"]))
        self.assertTrue(data["focused"])
        self.assertLess(data["disabled_alpha"], 0.2)
        self.assertEqual(
            data["scrolled"], {"aligned": True, "slider_visible": False, "draw_visible": False, "parent": True}
        )
        for key in ("unknown", "no_extension", "both", "native_preserved", "cleaned", "rebuilt", "gone"):
            self.assertTrue(data[key], (key, data))


if __name__ == "__main__":
    unittest.main()
