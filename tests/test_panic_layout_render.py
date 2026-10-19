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

            def page(label, tab_tag, page_tag, plot_tag, end_tag):
                def build():
                    with dpg.tab(label=label, tag=tab_tag):
                        with dpg.child_window(tag=page_tag, width=-1, height=-1, border=False):
                            dpg.add_plot(tag=plot_tag, label=label + " plot", width=-1, height=180)
                            for number in range(100):
                                dpg.add_text(f"{label} synthetic content {number}")
                            dpg.add_text(label + " END", tag=end_tag)
                return build

            app.build_control = page("Control", "tab_control", "page_control",
                                     "render_plot_control", "render_end_control")
            app.build_monitor = page("Monitor", "tab_monitor", "page_monitor",
                                     "render_plot_monitor", "render_end_monitor")
            app.build_timings = page("Timings", "tab_tim", "page_timings",
                                     "render_plot_timings", "render_end_timings")

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
                pages = (("tab_control", "page_control", "render_plot_control", "render_end_control"),
                         ("tab_monitor", "page_monitor", "render_plot_monitor", "render_end_monitor"),
                         ("tab_tim", "page_timings", "render_plot_timings", "render_end_timings"))
                for width, height in ((1180, 900), (900, 620), (1400, 1000)):
                    dpg.set_viewport_width(width)
                    dpg.set_viewport_height(height)
                    frames()
                    app.layout_shared_header(dpg.get_viewport_client_width())
                    frames()
                    client_height = dpg.get_viewport_client_height()
                    client_width = dpg.get_viewport_client_width()
                    if dpg.get_y_scroll_max("root") != 0:
                        raise AssertionError(("root must not scroll", width, height,
                                              dpg.get_y_scroll_max("root")))
                    panic_before = dpg.get_item_state("panic_pnp_reset")["pos"]
                    unlock_before = dpg.get_item_pos("unlock")
                    selector_before = dpg.get_item_state("tab_control")["rect_min"]
                    button = dpg.get_item_state("panic_pnp_reset")
                    if (button["rect_min"][0] <= client_width / 2
                            or not 0 <= client_width - button["rect_max"][0] <= 20):
                        raise AssertionError(("panic button must occupy the upper-right column", button))
                    if not (unlock_before[1] < selector_before[1]):
                        raise AssertionError(("unlock strip must sit above the tab selector",
                                              unlock_before, selector_before))
                    for tab, page, plot_tag, end_tag in pages:
                        dpg.set_value("tabs", tab)
                        dpg.set_y_scroll(page, 0)
                        frames()
                        page_pos = dpg.get_item_pos(page)
                        page_size = dpg.get_item_rect_size(page)
                        bottom_gap = client_height - (page_pos[1] + page_size[1])
                        if not 0 <= bottom_gap <= 16:
                            raise AssertionError(("page does not fill under the pinned selector",
                                                  width, height, tab, page_pos, page_size, bottom_gap))
                        if page_pos[1] <= selector_before[1]:
                            raise AssertionError(("page overlaps the tab selector", tab, page_pos,
                                                  selector_before))
                        if not dpg.get_item_state(plot_tag)["visible"]:
                            raise AssertionError(("plot is unreachable at tab top", tab))
                        maximum = dpg.get_y_scroll_max(page)
                        if maximum <= 0:
                            raise AssertionError(("long tab did not become scrollable", tab))
                        dpg.set_y_scroll(page, maximum)
                        frames()
                        end_state = dpg.get_item_state(end_tag)
                        if not end_state["visible"] or end_state["rect_max"][1] > page_pos[1] + page_size[1] + 2:
                            raise AssertionError(("end content is unreachable after scroll", tab,
                                                  maximum, end_state, page_pos, page_size))
                        panic_after = dpg.get_item_state("panic_pnp_reset")["pos"]
                        unlock_after = dpg.get_item_pos("unlock")
                        selector_after = dpg.get_item_state(tab)["rect_min"]
                        if (panic_after != panic_before or unlock_after != unlock_before
                                or selector_after != dpg.get_item_state(tab)["rect_min"]
                                or dpg.get_item_pos("unlock")[1] != unlock_before[1]
                                or dpg.get_y_scroll_max("root") != 0):
                            raise AssertionError(("pinned chrome moved with the page",
                                                  panic_before, panic_after, unlock_before, unlock_after))
                        if dpg.get_item_state("tab_control")["rect_min"] != selector_before:
                            raise AssertionError(("tab selector moved", selector_before,
                                                  dpg.get_item_state("tab_control")["rect_min"]))
                        rows.append({"viewport": [width, height], "tab": dpg.get_item_label(tab),
                                     "page_size": page_size, "page_scroll_max": maximum,
                                     "panic_pos": panic_after, "unlock_pos": unlock_after})
                if not app.tear_page("tab_monitor"):
                    raise AssertionError("monitor tab did not tear out")
                frames()
                parent = dpg.get_item_info("page_monitor")["parent"]
                alias = parent if isinstance(parent, str) else dpg.get_item_alias(parent)
                if alias != "win_page_monitor":
                    raise AssertionError(("torn page is not in the float window", parent, alias))
                if dpg.is_item_shown("tab_monitor"):
                    raise AssertionError("torn tab is still on the selector")
                unlock_torn = dpg.get_item_pos("unlock")
                if unlock_torn != unlock_before or dpg.get_y_scroll_max("root") != 0:
                    raise AssertionError(("unlock strip moved when a tab was torn out",
                                          unlock_before, unlock_torn,
                                          dpg.get_y_scroll_max("root"),
                                          dpg.get_item_state("tab_control").get("rect_min")))
                if not app.dock_page("tab_monitor"):
                    raise AssertionError("monitor tab did not dock back")
                frames()
                app.layout_torn_pages()
                frames()
                parent = dpg.get_item_info("page_monitor")["parent"]
                alias = parent if isinstance(parent, str) else dpg.get_item_alias(parent)
                if alias != "tab_monitor":
                    raise AssertionError(("docked page did not return to its tab", parent, alias))
                if not dpg.is_item_shown("tab_monitor"):
                    raise AssertionError("docked tab is hidden")
                dpg.set_value("tabs", "tab_monitor")
                frames()
                page_pos = dpg.get_item_pos("page_monitor")
                page_size = dpg.get_item_rect_size("page_monitor")
                gap = dpg.get_viewport_client_height() - (page_pos[1] + page_size[1])
                if not 0 <= gap <= 16:
                    raise AssertionError(("docked page does not fill under the selector",
                                          page_pos, page_size, gap))
                # Closing the float must reparent the page before Dear PyGui
                # deletes the window. Call the window's own on_close.
                if not app.tear_page("tab_tim"):
                    raise AssertionError("timings tab did not tear out")
                frames()
                cfg = dpg.get_item_configuration("win_page_timings")
                close = cfg.get("on_close")
                if close is None:
                    raise AssertionError(("float window has no on_close", sorted(cfg)))
                close("win_page_timings", None, dpg.get_item_user_data("win_page_timings"))
                frames()
                app.layout_torn_pages()
                frames()
                parent = dpg.get_item_info("page_timings")["parent"]
                alias = parent if isinstance(parent, str) else dpg.get_item_alias(parent)
                if alias != "tab_tim" or not dpg.does_item_exist("page_timings"):
                    raise AssertionError(("closed float did not return the page", parent, alias))
                if not dpg.is_item_shown("tab_tim"):
                    raise AssertionError("closed tab stayed hidden")
                if (dpg.get_item_pos("unlock") != unlock_before
                        or dpg.get_y_scroll_max("root") != 0
                        or dpg.get_item_state("tab_control")["rect_min"] != selector_before):
                    raise AssertionError(("pinned chrome moved when the float closed",
                                          dpg.get_item_pos("unlock"),
                                          dpg.get_y_scroll_max("root")))
                print(json.dumps({"rows": rows, "torn_parent": "win_page_monitor",
                                  "docked_parent": "tab_monitor",
                                  "closed_parent": "tab_tim"}))
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
        payload = json.loads(result.stdout)
        rows = payload["rows"]
        self.assertEqual(payload["torn_parent"], "win_page_monitor")
        self.assertEqual(payload["docked_parent"], "tab_monitor")
        self.assertEqual(payload["closed_parent"], "tab_tim")
        self.assertEqual(len(rows), 9)
        self.assertGreater(len({row["page_size"][1] for row in rows}), 1)
        self.assertTrue(all(row["page_scroll_max"] > 0 for row in rows))
        self.assertEqual(len({tuple(row["unlock_pos"]) for row in rows
                              if row["viewport"] == rows[0]["viewport"]}), 1)


if __name__ == "__main__":
    unittest.main()
