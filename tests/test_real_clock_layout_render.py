# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Real DearPyGui coverage for the measured line in the Monitor's clock tiles,
with the app's own fonts where they exist: the tiles keep one height through
every state of the line, never overflow either way, and a rebuilt tab (Refresh
capabilities) colours its line again."""
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
class RealClockLayoutRenderTests(unittest.TestCase):
    def test_clock_tiles_hold_their_height_through_every_state(self):
        """A fresh process: DearPyGui contexts cannot safely share a test process."""
        script = textwrap.dedent(
            """
            from types import SimpleNamespace
            from unittest.mock import Mock
            import json
            from druta import druta
            from druta.nvbackend import PRIV_CONFIRMED, PRIV_FREQ
            import dearpygui.dearpygui as dpg

            app = druta.Druta.__new__(druta.Druta)
            app.scale = 1
            app._fonts = {}
            app._bar_themes, app._bar_band = {}, {}
            app.gpu = SimpleNamespace(static={"mem_div": 2, "mem_type": "GDDR6"})
            app._dom_band, app._dom_name, app._dom_shown = {}, {}, set()
            app.step_khz = Mock(return_value=15000)
            app._stale = False

            def row(dom, name, a, b):
                return {"domain": dom, "name": name, "grade": PRIV_CONFIRMED, "kind": PRIV_FREQ,
                        "prog_khz": a, "meas_khz": b, "flags": 0, "srcid": 32, "scale": 1,
                        "prog_mhz": a / 1000.0, "meas_mhz": b / 1000.0,
                        "delta_mhz": (b - a) / 1000.0}

            clock = [0.0]

            def feed(a, b, util=99, stale=False, core=None):
                clock[0] += 1.05
                app._stale = stale
                d = {"core": core if core is not None else a // 1000, "xbar": 2025, "mem": 6801,
                     "util_gpu": util, "pstate": 0,
                     "clk_domains": [row(0, "GPC", a, b), row(1, "XBAR", 2025000, 2011100),
                                     row(4, "MEM", 6801000, 6794200)]}
                app.refresh_real_clocks(d, now=clock[0])

            def frames(count=3):
                for _ in range(count):
                    dpg.render_dearpygui_frame()

            amber = lambda: [feed(2115000, 2087100 + (i % 3) * 8) for i in range(9)]
            states = [("checking", lambda: feed(2115000, 2087100)),
                      ("amber", amber),
                      ("unpaired", lambda: feed(2115000, 2087108, core=2100)),
                      ("light", lambda: feed(2115000, 1300000, util=3)),
                      ("stale", lambda: feed(2115000, 2087100, stale=True)),
                      ("mirror", lambda: [feed(a, a) for a in
                                          (1950000, 1965000, 1950000, 1965000, 1950000, 1965000)])]

            def build():
                with dpg.window(tag="root"):
                    with dpg.tab_bar(tag="tabs"):
                        app.build_monitor()

            dpg.create_context()
            app.load_fonts()
            try:
                dpg.create_viewport(title="Druta measured clock layout", width=1180, height=900)
                build()
                dpg.setup_dearpygui()
                dpg.show_viewport()
                dpg.set_primary_window("root", True)
                out = []
                for width, height in ((1000, 800), (1180, 900), (1544, 1000), (1920, 1080)):
                    dpg.set_viewport_width(width)
                    dpg.set_viewport_height(height)
                    frames()
                    for name, step in states:
                        step()
                        app.relayout()
                        frames()
                        tile_w, tile_h = dpg.get_item_rect_size("tile_core")
                        line_w = dpg.get_item_rect_size("r_core")[0]
                        scrolls = {key: dpg.get_y_scroll_max(f"tile_{key}")
                                   for key, *_ in app.TILES}
                        out.append({"size": [width, height], "state": name, "tile_h": tile_h,
                                    "scroll": max(scrolls.values()),
                                    # the line fits inside the tile's padding on both sides
                                    "fits_x": line_w <= tile_w - 2 * app.s(8),
                                    "visible": dpg.get_item_state("r_core").get("visible", False),
                                    "line": dpg.get_value("r_core")})
                # Refresh capabilities rebuilds the tab: its line must colour again
                amber()
                app.refresh_domains({"clk_domains": [row(0, "GPC", 2115000, 2087100)],
                                     "clk_domains_err": None})
                frames()
                before = list(dpg.get_item_configuration("r_core")["color"])
                dpg.delete_item("root")
                build()
                dpg.set_primary_window("root", True)
                amber()
                frames()
                after = list(dpg.get_item_configuration("r_core")["color"])
                # the domains table rebuilt with it must show and name its rows again
                snap = {"clk_domains": [row(0, "GPC", 2115000, 2087100)], "clk_domains_err": None}
                app.refresh_domains(snap)
                frames()
                table = {"shown": dpg.get_item_configuration("dom_row_0")["show"],
                         "name": dpg.get_value("dom_0_name")}
                print(json.dumps({"rows": out, "before": before, "after": after, "table": table}))
            finally:
                dpg.destroy_context()
            """
        )
        environment = dict(os.environ)
        source = str(ROOT / "src")
        environment["PYTHONPATH"] = source + os.pathsep + environment.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=ROOT, env=environment,
            capture_output=True, text=True, timeout=90,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads(result.stdout.strip().splitlines()[-1])
        rows = data["rows"]
        self.assertEqual(len(rows), 24)
        for size in {tuple(r["size"]) for r in rows}:
            at = [r for r in rows if tuple(r["size"]) == size]
            self.assertEqual(len({r["tile_h"] for r in at}), 1, at)
        self.assertTrue(all(r["scroll"] == 0 for r in rows), rows)
        self.assertTrue(all(r["fits_x"] and r["visible"] for r in rows), rows)
        self.assertIn("measured 2087", next(r["line"] for r in rows if r["state"] == "amber"))
        self.assertEqual(data["after"], data["before"])            # amber again after a rebuild
        self.assertEqual(data["table"], {"shown": True, "name": "GPC"})


if __name__ == "__main__":
    unittest.main()
