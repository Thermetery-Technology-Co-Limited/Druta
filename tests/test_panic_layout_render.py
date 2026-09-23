# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Real DearPyGui regression coverage for the fixed header/tab scroll split."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
HAS_WINDOWS_DPG = sys.platform == "win32" and importlib.util.find_spec("dearpygui.dearpygui") is not None


@unittest.skipUnless(HAS_WINDOWS_DPG, "requires the Windows DearPyGui renderer")
class PanicLayoutRenderTests(unittest.TestCase):
    def test_header_stays_fixed_while_each_real_tab_viewport_scrolls(self):
        """Use a fresh process: DearPyGui contexts cannot safely share a test process."""
        script = textwrap.dedent(
            """
            from types import SimpleNamespace
            import json
            from druta import druta
            import dearpygui.dearpygui as dpg

            app = druta.Druta.__new__(druta.Druta)
            app.scale = 1
            app._fonts = {}
            app.gpu_list = [{"slot": "0000:01:00.0", "name": "Synthetic GPU"}]
            app.gpu = SimpleNamespace(
                static={"name": "Synthetic GPU", "driver": "test", "vbios": "test", "admin": True},
                slot=lambda: "0000:01:00.0",
            )
            # This callback is intentionally never invoked. The renderer must
            # exercise only layout code, with no GPU or PnP boundary available.
            app.open_device_restart = lambda *_args: (_ for _ in ()).throw(AssertionError("unexpected callback"))

            def page(label, plot_tag, end_tag):
                def build():
                    with dpg.tab(label=label):
                        dpg.add_plot(tag=plot_tag, label=label + " plot", width=-1, height=180)
                        for number in range(100):
                            dpg.add_text(f"{label} synthetic content {number}")
                        dpg.add_text(label + " END", tag=end_tag)
                return build

            app.build_control = page("Control", "render_plot_control", "render_end_control")
            app.build_monitor = page("Monitor", "render_plot_monitor", "render_end_monitor")
            app.build_timings = page("Timings", "render_plot_timings", "render_end_timings")

            def frames(count=4):
                for _ in range(count):
                    dpg.render_dearpygui_frame()

            dpg.create_context()
            try:
                dpg.create_viewport(title="Druta layout regression", width=1180, height=900)
                with dpg.window(tag="root"):
                    dpg.add_spacer(tag="menu_pad", height=30)
                app.build_body()
                dpg.setup_dearpygui()
                dpg.show_viewport()
                dpg.set_primary_window("root", True)
                rows = []
                targets = (("render_plot_control", "render_end_control"),
                           ("render_plot_monitor", "render_end_monitor"),
                           ("render_plot_timings", "render_end_timings"))
                for width, height in ((1180, 900), (900, 620), (1400, 1000)):
                    dpg.set_viewport_width(width)
                    dpg.set_viewport_height(height)
                    frames()
                    app.layout_shared_header(dpg.get_viewport_client_width())
                    frames()
                    tab_pos = dpg.get_item_pos("tab_content")
                    tab_size = dpg.get_item_rect_size("tab_content")
                    client_height = dpg.get_viewport_client_height()
                    client_width = dpg.get_viewport_client_width()
                    bottom_gap = client_height - (tab_pos[1] + tab_size[1])
                    if not 0 <= bottom_gap <= 16:
                        raise AssertionError(("tab child does not fill remaining root space", width, height,
                                              tab_pos, tab_size, bottom_gap))
                    if dpg.get_y_scroll_max("root") != 0:
                        raise AssertionError(("root must not scroll", width, height,
                                              dpg.get_y_scroll_max("root")))
                    panic_before = dpg.get_item_state("panic_pnp_reset")["pos"]
                    button = dpg.get_item_state("panic_pnp_reset")
                    if (button["rect_min"][0] <= client_width / 2
                            or not 0 <= client_width - button["rect_max"][0] <= 20):
                        raise AssertionError(("panic button must occupy the upper-right column", button))
                    for tab, (plot_tag, end_tag) in zip(dpg.get_item_children("tabs", 1), targets):
                        dpg.set_value("tabs", tab)
                        dpg.set_y_scroll("tab_content", 0)
                        frames()
                        if not dpg.get_item_state(plot_tag)["visible"]:
                            raise AssertionError(("plot is unreachable at tab top", dpg.get_item_label(tab)))
                        maximum = dpg.get_y_scroll_max("tab_content")
                        if maximum <= 0:
                            raise AssertionError(("long tab did not become scrollable", dpg.get_item_label(tab)))
                        dpg.set_y_scroll("tab_content", maximum)
                        frames()
                        end_state = dpg.get_item_state(end_tag)
                        if not end_state["visible"] or end_state["rect_max"][1] > tab_pos[1] + tab_size[1] + 2:
                            raise AssertionError(("end content is unreachable after scroll", dpg.get_item_label(tab),
                                                  maximum, end_state, tab_pos, tab_size))
                        panic_after = dpg.get_item_state("panic_pnp_reset")["pos"]
                        if panic_after != panic_before or dpg.get_y_scroll_max("root") != 0:
                            raise AssertionError(("header moved with tab content", panic_before, panic_after))
                        rows.append({"viewport": [width, height], "tab": dpg.get_item_label(tab),
                                     "tab_size": tab_size, "tab_scroll_max": maximum,
                                     "panic_pos": panic_after})
                print(json.dumps(rows))
            finally:
                dpg.destroy_context()
            """
        )
        environment = dict(os.environ)
        source = str(ROOT / "src")
        environment["PYTHONPATH"] = source + os.pathsep + environment.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=ROOT, env=environment,
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = json.loads(result.stdout)
        self.assertEqual(len(rows), 9)
        self.assertGreater(len({row["tab_size"][1] for row in rows}), 1)
        self.assertTrue(all(row["tab_scroll_max"] > 0 for row in rows))


if __name__ == "__main__":
    unittest.main()
