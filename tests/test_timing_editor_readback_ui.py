# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pending timing edits follow confirmed readback, never an assumed commit."""
from types import SimpleNamespace
from unittest.mock import Mock, patch

from druta import druta
from tests.test_arch_ui_regressions import FakeUiTest
from tests.test_timing_profiles import capture, table


class TimingEditorReadbackTests(FakeUiTest):
    def setUp(self):
        super().setUp()
        self.app.gpu = SimpleNamespace(slot=lambda: "0000:01:00.0")
        self.app._tim_ft = table()
        self.app._tim = capture()
        self.app._tw_base = {"RC": 45, "FAW": 24}
        self.app._tw_pending = {"RC": 46, "FAW": 25}
        self.app._tw_btn = None
        self.app._tw_themes = {"btn": 1}
        self.values.update(tw_apply="", tw_plan="")

    def snapshot(self, *, faw=24, rc=45):
        snap = capture()
        word = (faw << 8) | rc
        snap.registers["broadcast"]["CONFIG0"] = word
        snap.registers["fbpa0"]["CONFIG0"] = word
        for reading in snap.readings:
            if reading.name == "FAW":
                reading.cycles = faw
            elif reading.name == "RC":
                reading.cycles = rc
        return snap

    def test_failed_readback_keeps_every_pending_edit(self):
        failed = self.snapshot(faw=25, rc=46)
        failed.ok = False
        self.app.tw_reconcile_capture(failed, self.app._tim_ft)
        self.assertEqual(self.app._tw_pending, {"RC": 46, "FAW": 25})
        self.app.tim_columns = Mock()
        self.app.draw_comparison = Mock()
        self.app.draw_timings(failed, None, {})
        self.assertEqual(self.app._tw_pending, {"RC": 46, "FAW": 25})
        self.assertIn("readback unavailable", self.values["tw_plan"])
        self.assertEqual(self.ui.configure_item.call_args_list[-2].kwargs.get("label"),
                         "Apply 2 change(s) to the memory controller")

    def test_partial_readback_clears_only_confirmed_field_and_updates_preview_count(self):
        self.app.tw_button()
        initial_label = [c.kwargs["label"] for c in self.ui.configure_item.call_args_list
                         if "label" in c.kwargs][-1]
        self.assertEqual(initial_label, "Apply 2 change(s) to the memory controller")
        snap = self.snapshot(faw=25, rc=45)
        self.app.tw_reconcile_capture(snap, self.app._tim_ft)
        self.assertEqual(self.app._tw_pending, {"RC": 46})
        self.app._tim = snap
        preview = SimpleNamespace(ok=True, needs_force=False,
                                  summary=lambda: "RC 45->46")
        with patch.object(druta.timingwrite, "plan", return_value=preview) as plan:
            self.app.tw_plan()
        self.assertEqual(plan.call_args.args[0], {"RC": 46})
        self.assertEqual(self.values["tw_plan"], "RC 45->46")
        updated_label = [c.kwargs["label"] for c in self.ui.configure_item.call_args_list
                         if "label" in c.kwargs][-1]
        self.assertEqual(updated_label, "Apply 1 change(s) to the memory controller")

    def test_all_partitions_and_card_band_must_confirm_before_edit_disappears(self):
        mismatched_partition = self.snapshot(faw=25)
        mismatched_partition.registers["fbpa0"]["CONFIG0"] = 0x182D
        self.app.tw_reconcile_capture(mismatched_partition, self.app._tim_ft)
        self.assertEqual(self.app._tw_pending, {"RC": 46, "FAW": 25})

        wrong_band = self.snapshot(faw=25, rc=46)
        wrong_band.pstate_before = wrong_band.pstate_after = 8
        self.app.tw_reconcile_capture(wrong_band, self.app._tim_ft)
        self.assertEqual(self.app._tw_pending, {"RC": 46, "FAW": 25})

        wrong_card = self.snapshot(faw=25, rc=46)
        wrong_card.slot = "0000:02:00.0"
        self.app.tw_reconcile_capture(wrong_card, self.app._tim_ft)
        self.assertEqual(self.app._tw_pending, {"RC": 46, "FAW": 25})

        confirmed = self.snapshot(faw=25, rc=46)
        self.app.tw_reconcile_capture(confirmed, self.app._tim_ft)
        self.assertEqual(self.app._tw_pending, {})

    def test_new_capture_redraw_reconciles_and_refreshes_plan_once(self):
        snap = self.snapshot(faw=25)
        self.app.s = lambda value: value
        self.app.bind = Mock()
        self.app.tim_columns = Mock()
        self.app.tim_ns_cell = Mock()
        self.app.tw_cell = Mock()
        self.app.draw_comparison = Mock()
        self.app.draw_divergence = Mock()
        self.app.tw_plan = Mock()
        with patch.object(druta.timings, "field_table", return_value=self.app._tim_ft):
            self.app.draw_timings(snap, None, {})
        self.assertEqual(self.app._tw_pending, {"RC": 46})
        self.app.tw_plan.assert_called_once_with()
