# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""DIFR diagnostics distinguish loaded routing, support and mask availability."""

import copy
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from druta.registry_panel import RegistryPanel


def observation():
    controller = {"available": True, "supported": True, "states_available": True, "configured_pstate_mask": 0x100}
    return {
        "identity": {"slot": "0000:04:00.0"},
        "pstates": {
            "rows": [
                {"index": 7, "pstate_id_mask": 0x100, "level": 2, "lpwr_index": 0},
                {"index": 1, "pstate_id_mask": 0x10, "level": 0, "lpwr_index": 3},
                {"index": 6, "pstate_id_mask": 2, "level": 4, "lpwr_index": 255},
            ]
        },
        "controllers": {str(key): copy.deepcopy(controller) for key in (9, 10, 11)},
    }


class LpwrDiagnosticViewTests(unittest.TestCase):
    def setUp(self):
        self.app = SimpleNamespace(gpu=SimpleNamespace(slot=lambda: "0000:04:00.0"), log=Mock())
        self.panel = RegistryPanel(self.app)
        self.values = {}
        self.ui = MagicMock()
        self.ui.set_value.side_effect = self.values.__setitem__
        self.patch(patch("druta.registry_panel.dpg", self.ui))
        self.reader = self.patch(patch("druta.lpwr_runtime.read", return_value=(observation(), None)))

    def patch(self, value):
        result = value.start()
        self.addCleanup(value.stop)
        return result

    def test_read_uses_selected_gpu_and_actual_pstate_ids_not_ordinals_or_levels(self):
        self.panel.refresh_difr()
        self.reader.assert_called_once_with(self.app.gpu)
        text = self.values["reg_difr_readback"]
        self.assertIn("P8: LPWR selector 0", text)
        self.assertIn("P4: LPWR selector 3", text)
        self.assertIn("P1: LPWR selector FF (unassigned)", text)
        self.assertNotIn("P7:", text)
        self.assertIn("DFPR: support yes; configured states P8 (0x100)", text)

    def test_false_support_is_available_but_its_mask_is_unavailable_not_zero(self):
        sample = observation()
        for row in sample["controllers"].values():
            row.update(supported=False, states_available=False, configured_pstate_mask=None)
        self.reader.return_value = sample, None
        self.panel.refresh_difr()
        text = self.values["reg_difr_readback"]
        self.assertEqual(text.count("support no; state mask unavailable"), 3)
        self.assertNotIn("0x0", text)
        self.assertIs(self.panel.difr_readback, sample)

    def test_supported_zero_mask_is_reported_as_none_with_a_real_zero_value(self):
        sample = observation()
        sample["controllers"]["10"]["configured_pstate_mask"] = 0
        self.reader.return_value = sample, None
        self.panel.refresh_difr()
        self.assertIn("MS DIFR SW-ASR: support yes; configured states none (0x0)", self.values["reg_difr_readback"])

    def test_partial_query_availability_does_not_turn_missing_values_into_support_false(self):
        sample = observation()
        sample["controllers"]["9"].update(
            available=False, supported=None, states_available=False, configured_pstate_mask=None
        )
        sample["controllers"]["10"].update(states_available=False, configured_pstate_mask=None)
        self.reader.return_value = sample, None
        self.panel.refresh_difr()
        text = self.values["reg_difr_readback"]
        self.assertIn("DFPR: support readback unavailable", text)
        self.assertIn("MS DIFR SW-ASR: support yes; state mask unavailable", text)
        self.assertIn("MS DIFR CG: support yes; configured states P8", text)

    def test_refresh_failure_labels_retained_same_adapter_data_as_previous(self):
        self.panel.refresh_difr()
        prior = self.panel.difr_readback
        self.reader.return_value = None, "temporary transport failure"
        self.panel.refresh_difr()
        self.assertIs(self.panel.difr_readback, prior)
        self.assertIn("Previous read; refresh failed", self.values["reg_difr_readback"])
        self.assertIn("temporary transport failure", self.values["reg_difr_readback"])
        self.ui.configure_item.assert_not_called()

    def test_new_adapter_failure_does_not_display_the_old_mapping(self):
        self.panel.refresh_difr()
        self.app.gpu = SimpleNamespace(slot=lambda: "0000:05:00.0")
        self.reader.return_value = None, "unavailable"
        self.panel.refresh_difr()
        self.assertIsNone(self.panel.difr_readback)
        self.assertNotIn("P8", self.values["reg_difr_readback"])
        self.assertNotIn("Previous read", self.values["reg_difr_readback"])

    def test_first_failed_read_has_no_fabricated_inventory_and_does_not_gate_other_controls(self):
        self.reader.return_value = None, "unsupported layout"
        self.panel.refresh_difr()
        self.assertIn("unsupported layout", self.values["reg_difr_readback"])
        self.assertNotIn("Configured support", self.values["reg_difr_readback"])
        self.ui.configure_item.assert_not_called()
        self.app.log.assert_called_once_with("DIFR configuration read unavailable: unsupported layout", False)

    def test_unexpected_reader_exception_is_shown_without_losing_prior_data(self):
        self.panel.refresh_difr()
        self.reader.side_effect = RuntimeError("adapter is restarting")
        self.panel.refresh_difr()
        self.assertIn("adapter is restarting", self.values["reg_difr_readback"])
        self.assertIn("Previous read; refresh failed", self.values["reg_difr_readback"])

    def test_success_after_failure_clears_historical_error(self):
        self.reader.return_value = None, "temporary"
        self.panel.refresh_difr()
        self.reader.return_value = observation(), None
        self.panel.refresh_difr()
        self.assertNotIn("unavailable:", self.values["reg_difr_readback"])
        self.assertNotIn("Previous read", self.values["reg_difr_readback"])


if __name__ == "__main__":
    unittest.main()


def test_readonly_boolean_and_numeric_widgets_have_stable_tags_without_inputs(monkeypatch):
    from druta import registry_panel

    ui = MagicMock()
    monkeypatch.setattr(registry_panel, "dpg", ui)
    app = SimpleNamespace(gpu=object(), s=lambda value: value, log=Mock())
    panel = RegistryPanel(app)
    panel.recover = Mock()
    panel.status = Mock()
    panel._startup_field = Mock(side_effect=RuntimeError("unavailable"))
    target = SimpleNamespace(slot="fake")
    monkeypatch.setattr(registry_panel.overrides, "bind_target", lambda gpu: target)
    monkeypatch.setattr(registry_panel.overrides, "read_vbios", lambda gpu: b"image")
    fields = [
        {"key": "lpwr.ms.0.enabled", "label": "MS", "group": "Idle", "units": "bool",
         "current": False, "capability": "read-only", "mask": 1},
        {"key": "power.0.maximum", "label": "Power", "group": "Power", "units": "mW",
         "current": 450000, "capability": "read-only", "mask": None},
    ]
    monkeypatch.setattr(registry_panel.vbios_fields, "inspect", lambda image: {"fields": fields})
    monkeypatch.setattr(registry_panel.overrides, "read_model_inputs", lambda gpu: (None, "unavailable"))
    for module in (registry_panel.overrides.registry_flags, registry_panel.overrides.all_pstates_registry,
                   registry_panel.overrides.clock_vf_registry):
        monkeypatch.setattr(module, "read", lambda gpu: (None, "unavailable"))
    panel.refresh()
    ui.add_text.assert_any_call("False", tag="reg_field_lpwr.ms.0.enabled")
    ui.add_text.assert_any_call("450.0", tag="reg_field_power.0.maximum")
    assert "lpwr.ms.0.enabled" not in panel.inputs and "power.0.maximum" not in panel.inputs
