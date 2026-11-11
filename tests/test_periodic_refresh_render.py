# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Opt-in DPG page visibility qualification with synthetic tabs and no GPU backend."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
ENABLED = (sys.platform == "win32" and os.environ.get("DRUTA_TEST_WINDOWIDLE_RENDER") == "1"
           and importlib.util.find_spec("dearpygui") is not None)


@unittest.skipUnless(ENABLED, "set DRUTA_TEST_WINDOWIDLE_RENDER=1 with Windows DearPyGui installed")
class PeriodicRefreshRenderTests(unittest.TestCase):
    def test_real_tab_selection_detached_hidden_collapsed_and_rebuilt_pages(self):
        script = textwrap.dedent(
            """
            import json
            import uuid
            from unittest.mock import patch
            import dearpygui.dearpygui as dpg
            from druta.druta import Druta

            def forbidden(*_args, **_kwargs):
                raise AssertionError('Synthetic visibility test must not initialize a GPU backend')

            app = Druta.__new__(Druta)
            report = {'observations': []}
            pages = [page for _tab, page, _label in app.TEAR_PAGES]

            def frames():
                for _ in range(4):
                    dpg.render_dearpygui_frame()

            def build_tabs():
                with dpg.tab_bar(tag='tabs', parent='root'):
                    for tab, page, label in app.TEAR_PAGES:
                        with dpg.tab(tag=tab, label=label):
                            with dpg.child_window(tag=page, width=-1, height=-1):
                                dpg.add_text(label + ' synthetic contents')

            def check(label, expected):
                observed = {page for page in pages if app.page_is_visible(page)}
                report['observations'].append({'stage': label, 'visible': sorted(observed)})
                if observed != set(expected):
                    raise AssertionError((label, sorted(expected), sorted(observed), report))

            dpg.create_context()
            try:
                report['dpg_version'] = dpg.get_dearpygui_version()
                with patch('druta.druta.open_gpu', side_effect=forbidden), \
                        patch('druta.nvbackend.GPU.__init__', side_effect=forbidden):
                    dpg.create_viewport(title='Druta page visibility synthetic ' + uuid.uuid4().hex,
                                        width=720, height=480)
                    with dpg.window(tag='root'):
                        pass
                    build_tabs()
                    dpg.setup_dearpygui()
                    dpg.show_viewport()
                    dpg.set_primary_window('root', True)
                    # Before the first frame a selected page need not exist yet,
                    # but the helper must safely avoid guessing that all are shown.
                    initial = [page for page in pages if app.page_is_visible(page)]
                    report['before_first_frame'] = initial
                    if len(initial) > 1:
                        raise AssertionError(('Multiple pages selected before first frame', initial))
                    frames()
                    report['child_state_keys'] = sorted(dpg.get_item_state('page_control'))
                    check('initial control', {'page_control'})
                    for tab, page, _label in app.TEAR_PAGES:
                        dpg.set_value('tabs', tab)
                        frames()
                        check('selected ' + tab, {page})

                    with dpg.window(tag='win_page_monitor', label='Floating monitor',
                                    width=300, height=220, pos=(390, 100)):
                        pass
                    dpg.move_item('page_monitor', parent='win_page_monitor')
                    dpg.configure_item('tab_monitor', show=False)
                    dpg.set_value('tabs', 'tab_control')
                    frames()
                    check('monitor detached with control selected', {'page_control', 'page_monitor'})
                    dpg.set_value('tabs', 'tab_advanced')
                    frames()
                    check('monitor detached with advanced selected', {'page_advanced', 'page_monitor'})

                    dpg.configure_item('win_page_monitor', show=False)
                    frames()
                    check('floating monitor hidden', {'page_advanced'})
                    dpg.configure_item('win_page_monitor', show=True, collapsed=False)
                    frames()
                    report['expanded_window_state'] = dpg.get_item_state('win_page_monitor')
                    report['expanded_configuration_collapsed'] = dpg.get_item_configuration(
                        'win_page_monitor').get('collapsed', 'absent')
                    check('floating monitor shown', {'page_advanced', 'page_monitor'})
                    dpg.configure_item('win_page_monitor', collapsed=True)
                    frames()
                    report['collapsed_window_state'] = dpg.get_item_state('win_page_monitor')
                    report['collapsed_configuration_collapsed'] = dpg.get_item_configuration(
                        'win_page_monitor').get('collapsed', 'absent')
                    check('floating monitor collapsed', {'page_advanced'})
                    dpg.configure_item('win_page_monitor', collapsed=False)
                    frames()
                    check('floating monitor expanded again', {'page_advanced', 'page_monitor'})

                    dpg.delete_item('win_page_monitor')
                    dpg.delete_item('tabs')
                    for page in pages:
                        if app.page_is_visible(page):
                            raise AssertionError(('Deleted page reported visible', page))
                    app._gpu_gen = 2
                    app._ui_gen = 3
                    build_tabs()
                    frames()
                    check('rebuilt tab tree', {'page_control'})
                    dpg.set_value('tabs', 'tab_monitor')
                    frames()
                    check('rebuilt monitor selected', {'page_monitor'})
                    if hasattr(app, 'gpu'):
                        raise AssertionError('Synthetic app acquired a GPU backend')
                    report['gpu_backend_initialized'] = False
            finally:
                dpg.destroy_context()
            print(json.dumps(report))
            """
        )
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + environment.get("PYTHONPATH", "")
        result = subprocess.run([sys.executable, "-X", "faulthandler", "-c", script], cwd=ROOT, env=environment,
                                capture_output=True, text=True, timeout=25)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        print(json.dumps(report))
        self.assertFalse(report["gpu_backend_initialized"])
        self.assertGreaterEqual(len(report["observations"]), 13)
        self.assertEqual(report["observations"][-1]["visible"], ["page_monitor"])


if __name__ == "__main__":
    unittest.main()
