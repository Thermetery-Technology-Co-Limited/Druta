# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The PnP recovery action belongs to the shared header, never a tuning tab."""
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, call, patch

from druta import druta


LABEL = "Panic Button (PnP Reset, Deeper than Shift+Ctrl+B)"


class PanicButtonUiTests(unittest.TestCase):
    def setUp(self):
        self.app = druta.Druta.__new__(druta.Druta)
        self.app.gpu = SimpleNamespace(
            static={"name": "Test GPU", "driver": "1", "vbios": "1", "admin": True},
            slot=lambda: "0000:01:00.0",
        )
        self.app.gpu_list = []
        self.app.s = lambda value: value
        self.app.bind = Mock()
        self.app.card_labels = Mock(return_value=["Test GPU"])
        self.app.card_label = Mock(return_value="Test GPU")
        self.app.on_pick_card = Mock()
        self.app.open_device_restart = Mock()
        self.ui = MagicMock()
        self.patcher = patch.object(druta, "dpg", self.ui)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_panic_button_is_full_width_directly_under_the_shared_header(self):
        self.app.build_shared_header()
        self.assertIn(call(tag="panic_row", parent="root", width=-1), self.ui.group.call_args_list)
        button = next(
            item for item in self.ui.add_button.call_args_list
            if item.kwargs.get("tag") == "panic_pnp_reset"
        )
        self.assertEqual(button.kwargs["label"], LABEL)
        self.assertEqual(button.kwargs["width"], -1)
        self.assertEqual(button.kwargs["height"], 42)
        self.assertIs(button.kwargs["callback"], self.app.open_device_restart)
        self.ui.bind_item_theme.assert_called_once_with("panic_pnp_reset", unittest.mock.ANY)

    def test_body_builds_shared_recovery_header_before_every_tab(self):
        self.app.build_shared_header = Mock()
        self.app.build_control = Mock()
        self.app.build_monitor = Mock()
        self.app.build_timings = Mock()
        self.app.build_body()
        self.app.build_shared_header.assert_called_once_with()
        self.app.build_control.assert_called_once_with()
        self.app.build_monitor.assert_called_once_with()
        self.app.build_timings.assert_called_once_with()
        self.ui.child_window.assert_called_once_with(
            tag="tab_content", parent="root", width=-1, height=480, border=False
        )
        self.ui.tab_bar.assert_called_once_with(tag="tabs")

    def test_root_header_measurements_define_the_tab_viewport_height(self):
        positions = {
            "root": ([0, 0], [1418, 944]),
            "menu_pad": ([0, 0], [1418, 36]),
            "hdr_row": ([8, 36], [1402, 35]),
            "panic_row": ([8, 71], [1402, 42]),
            "tab_content": ([8, 113], [1402, 480]),
        }
        self.ui.does_item_exist.side_effect = lambda tag: tag in positions
        self.ui.get_item_rect_min.side_effect = lambda tag: positions[tag][0]
        self.ui.get_item_rect_size.side_effect = lambda tag: positions[tag][1]
        self.app.layout_tab_content(944)
        # The 36px menu pad is included in the measured fixed region. The tab
        # child gets every remaining pixel after the actual panic-row bottom.
        self.ui.configure_item.assert_called_once_with("tab_content", height=823, width=-1)

    def test_card_rebuild_removes_the_fixed_row_and_tab_viewport_together(self):
        self.app._orphans = []
        self.app._ctl_widgets = []
        self.app._slider_ranges = {}
        self.app._knob_decimals = {}
        self.app._current_limits = {}
        self.app._knob_cb = {}
        self.app.build_menu_bar = Mock()
        self.app.build_body = Mock()
        self.app.build_tool_windows = Mock()
        self.ui.does_item_exist.return_value = True
        self.ui.get_all_items.return_value = []
        self.app.build_ui(rebuild=True)
        deleted = self.ui.delete_item.call_args_list
        self.assertIn(call("panic_row"), deleted)
        self.assertIn(call("tab_content"), deleted)
        self.app.build_body.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
