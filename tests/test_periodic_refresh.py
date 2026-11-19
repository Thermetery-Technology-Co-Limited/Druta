# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Hidden pages skip diagnostics without dropping independent model services."""

import unittest
from unittest.mock import Mock, patch

from druta.druta import Druta


class PeriodicRefreshTests(unittest.TestCase):
    def setUp(self):
        self.app = Druta.__new__(Druta)
        for name in ("refresh_real_clocks", "refresh_monitor", "refresh_control",
                     "refresh_power_policies", "refresh_timings", "save_power_policy_names",
                     "check_clock_gap", "watch_hold_headroom", "refresh_power_request_status",
                     "sync_slider_ranges", "clear_once", "log_once"):
            setattr(self.app, name, Mock())
        self.app.power_request_carryover = Mock(return_value=False)
        self.visible = {"page_control"}
        self.app.page_is_visible = Mock(side_effect=lambda tag: tag in self.visible)
        self.now = 10.0
        self.data = {"core": 900, "pl_requested_mw": 300000}
        for name, kwargs in (
                ("dpg.does_item_exist", {"return_value": True}),
                ("dpg.is_item_visible", {"side_effect": lambda tag: tag in self.visible}),
                ("time.monotonic", {"side_effect": lambda: self.now})):
            p = patch("druta.druta." + name, **kwargs)
            p.start()
            self.addCleanup(p.stop)

    def tick(self):
        self.app.refresh_periodic(self.data, 9.0)

    def test_hidden_pages_keep_evidence_readback_and_armed_capture_services(self):
        self.tick()
        self.app.refresh_control.assert_called_once_with(self.data, periodic=True)
        self.app.refresh_monitor.assert_not_called()
        self.app.refresh_power_policies.assert_not_called()
        self.app.refresh_real_clocks.assert_called_once_with(self.data, paint=False)
        self.app.refresh_timings.assert_called_once_with(self.data, paint=False)
        self.app.refresh_power_request_status.assert_called_once_with(300000, paint=False)
        self.app.check_clock_gap.assert_called_once_with(self.data)
        self.app.watch_hold_headroom.assert_called_once_with(self.data, 9.0)

    def test_unchanged_visible_panel_avoids_duplicate_reads_until_next_cadence(self):
        self.tick()
        self.now += 0.25
        self.tick()
        self.app.refresh_control.assert_called_once()
        self.assertEqual(self.app.watch_hold_headroom.call_count, 2)
        self.now += 0.75
        self.tick()
        self.assertEqual(self.app.refresh_control.call_count, 2)

    def test_new_snapshot_and_local_edit_refresh_visible_panel_promptly(self):
        self.tick()
        self.now += 0.25
        self.data = dict(self.data)
        self.tick()
        self.app._refresh_revision = 1
        self.now += 0.25
        self.tick()
        self.assertEqual(self.app.refresh_control.call_count, 3)

    def test_page_visibility_handles_detached_monitor_and_advanced_independently(self):
        self.tick()
        self.visible = {"page_monitor", "page_advanced"}
        self.now += 0.25
        self.tick()
        self.app.refresh_monitor.assert_called_once_with(self.data)
        self.app.refresh_power_policies.assert_called_once_with()
        self.app.refresh_control.assert_called_once()
        self.visible.add("page_control")
        self.now += 0.25
        self.tick()
        self.assertEqual(self.app.refresh_control.call_count, 2)

    def test_one_panel_failure_does_not_silence_other_services_and_is_retried(self):
        self.visible.add("page_monitor")
        self.app.refresh_monitor.side_effect = RuntimeError("temporary widget failure")
        self.tick()
        self.app.refresh_control.assert_called_once()
        self.app.watch_hold_headroom.assert_called_once()
        self.now += 0.25
        self.tick()
        self.assertEqual(self.app.refresh_monitor.call_count, 2)

    def test_new_capture_is_painted_immediately_on_visible_timings_page(self):
        self.visible = {"page_timings"}
        self.tick()
        self.now += 0.25
        self.tick()
        self.app.refresh_timings.assert_called_with(self.data, paint=False)
        self.app._tim_new = True
        self.tick()
        self.app.refresh_timings.assert_called_with(self.data, paint=True)


if __name__ == "__main__":
    unittest.main()
