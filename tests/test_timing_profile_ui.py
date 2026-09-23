# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from druta import druta, timingprofiles
from tests.test_arch_ui_regressions import FakeUiTest
from tests.test_timing_profiles import capture, table


class TimingProfileUiTests(FakeUiTest):
    def setUp(self):
        super().setUp()
        self.app.gpu = SimpleNamespace(slot=lambda: "0000:01:00.0")
        self.app._tim_ft = table()
        self.app._tim = capture()
        self.app._tw_base = {"RC": 45, "FAW": 24}
        self.app._tw_pending = {"RC": 47}
        self.app.tw_plan = Mock()
        self.app.tw_theme = Mock(return_value=1)
        self.app.tw_apply = Mock()
        self.app._tim_profile_result = None
        self.values.update(twv_RC=47, twv_FAW=24, tw_profile_status="")

    def test_loading_stages_replacement_and_preview_without_applying(self):
        self.app.stage_timing_profile({"fields": {"FAW": 25, "RC": 45}})
        self.assertEqual(self.app._tw_pending, {"FAW": 25})
        self.assertEqual(self.values["twv_RC"], 45)
        self.assertEqual(self.values["twv_FAW"], 25)
        self.app.tw_plan.assert_called_once()
        self.app.tw_apply.assert_not_called()
        self.app.autosave_before.assert_not_called()

    def test_invalid_or_unread_fields_preserve_all_existing_edits(self):
        for fields in ({"OFFSET0": 1}, {"RC": True}, {"TYPO": 1}, {"RC": 999}):
            self.app.stage_timing_profile({"fields": fields})
            self.assertEqual(self.app._tw_pending, {"RC": 47})
            self.app.tw_plan.assert_not_called()
        self.app._tw_base.pop("FAW")
        self.app.stage_timing_profile({"fields": {"FAW": 25}})
        self.assertEqual(self.app._tw_pending, {"RC": 47})
        self.assertIn("Capture these fields", self.values["tw_profile_status"])

    def result(self, *, generation=0, gpu=None, pending=None):
        self.app._tim_profile_dialog = True
        self.app._tim_profile_result = (
            generation, self.app.gpu if gpu is None else gpu,
            {"RC": 47} if pending is None else pending, False,
            "test.json", "", {"fields": {"FAW": 25}})

    def test_late_dialog_result_cannot_target_another_card_or_replace_new_edits(self):
        for kwargs in ({"generation": -1}, {"gpu": object()}, {"pending": {"RC": 46}}):
            self.result(**kwargs)
            self.app.poll_timing_profile()
            self.assertEqual(self.app._tw_pending, {"RC": 47})
            self.assertIsNone(self.app._tim_profile_result)
            self.app.tw_plan.assert_not_called()

    def test_helper_metadata_is_revalidated_on_dialog_completion(self):
        self.result()
        self.app._tim_ft.fields = [f for f in self.app._tim_ft.fields if f.name != "FAW"]
        self.app.poll_timing_profile()
        self.assertEqual(self.app._tw_pending, {"RC": 47})
        self.app.tw_plan.assert_not_called()

    def test_upstream_profile_cannot_lose_its_device_binding(self):
        self.app.stage_timing_profile({"slot": "0000:02:00.0", "fields": {"FAW": 25}})
        self.assertEqual(self.app._tw_pending, {"RC": 47})
        self.assertIn("select that GPU", self.values["tw_profile_status"])
        self.app.tw_plan.assert_not_called()

    def test_save_freezes_capture_and_edits_before_file_dialog(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "profile.json"
            def picker(*args, **kwargs):
                self.assertTrue(kwargs["save"])
                self.app._tw_pending["RC"] = 50
                return str(path), ""
            self.app.pick_file_native = picker
            with patch.object(druta.paths, "profile_dir", return_value=Path(folder)), \
                    patch.object(druta.threading, "Thread") as thread:
                self.app.open_timing_profile(save=True)
                thread.call_args.kwargs["target"]()
            self.app.poll_timing_profile()
            self.assertEqual(json.loads(path.read_text())["fields"]["RC"], 47)
            self.assertEqual(self.app._tw_pending, {"RC": 50})
            self.app.tw_apply.assert_not_called()

    def test_reading_raw_backup_reports_format_without_changing_edits(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "raw.json"
            path.write_text('{"registers": {"broadcast": {"CONFIG0": "0x1"}}}')
            self.app.pick_file_native = Mock(return_value=(str(path), ""))
            with patch.object(druta.paths, "profile_dir", return_value=Path(folder)), \
                    patch.object(druta.threading, "Thread") as thread:
                self.app.open_timing_profile(save=False)
                thread.call_args.kwargs["target"]()
            self.app.poll_timing_profile()
            self.assertIn("raw nvtune register snapshot", self.values["tw_profile_status"])
            self.assertEqual(self.app._tw_pending, {"RC": 47})
            self.app.tw_plan.assert_not_called()
