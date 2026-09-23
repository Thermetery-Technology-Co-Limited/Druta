"""Rail-diagnostic presentation and write-gate regressions."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call

from druta.druta import Druta
from tests.test_arch_ui_regressions import FakeUiTest


class RailDiagnosticsUiTests(FakeUiTest):
    valid_state = {"type": 1, "reliability": 1000.0,
                   "alt_reliability": 1000.0, "overvoltage": 1100.0,
                   "vmin": 700.0}
    controls = {0: {"_base_mv": {"reliability": 1000.0}}}

    def test_missing_backend_diagnostics_fail_closed(self):
        self.app.gpu = SimpleNamespace()
        diagnostic = self.app.rail_limit_diagnostics()
        support = self.app.rail_limit_support(self.controls, diagnostic)
        self.assertEqual(support, {0: False, 1: False})
        self.assertFalse(diagnostic["available"])
        self.assertIn("does not provide rail diagnostics", diagnostic["reason"])

    def test_backend_diagnosis_owns_rail_verdict_but_needs_fresh_controls(self):
        diagnostic = {
            "available": True,
            "reason": "",
            "rails": {0: {"available": True, "reason": ""},
                      1: {"available": False, "reason": "status unavailable"}},
        }
        # The status is deliberately invalid.  The backend's successful
        # diagnosis is authoritative; old UI code duplicated this check.
        support = self.app.rail_limit_support(self.controls, diagnostic)
        self.assertEqual(support, {0: True, 1: False})

        # A positive diagnosis cannot revive a slider after its current
        # control record disappeared.
        support = self.app.rail_limit_support({}, diagnostic)
        self.assertEqual(support, {0: False, 1: False})
        message, available = self.app.rail_limit_status(diagnostic, support)
        self.assertFalse(available)
        self.assertIn("Rail limits are unavailable", message)

    def test_unavailable_diagnosis_disables_mode_and_explains_why(self):
        self.values.update(vlim_mode=False, vlim_status="")
        diagnostic = {"available": False, "reason": "driver control read failed",
                      "rails": {0: {"available": False,
                                    "reason": "driver control read failed"}}}
        self.app.update_rail_limit_diagnostic_ui(diagnostic, {0: False, 1: False})
        self.assertEqual(self.app._rail_writable, set())
        self.assertFalse(self.app._rail_limits_available)
        self.assertIn("driver control read failed", self.values["vlim_status"])
        self.assertIn(call("vlim_mode", enabled=False), self.ui.configure_item.call_args_list)

    def test_prior_write_protocol_error_is_visible_when_reads_are_available(self):
        diagnostic = {"available": True, "reason": "",
                      "last_write_error": "native packet was not recognized",
                      "rails": {0: {"available": True, "reason": ""}}}
        message, available = self.app.rail_limit_status(diagnostic, {0: True, 1: False})
        self.assertTrue(available)
        self.assertIn("last rail-limit write was not confirmed", message)
        self.assertIn("native packet was not recognized", message)

    def test_stale_disabled_rail_callback_cannot_write(self):
        self.app.gpu = SimpleNamespace(set_volt_rail_limits=Mock())
        self.app._rail_writable = set()
        self.app.guard = Mock(return_value=True)
        self.app.apply_vlim(0, reliability=1000)
        self.app.guard.assert_not_called()
        self.app.gpu.set_volt_rail_limits.assert_not_called()
        self.assertIn("Retry detection", self.app.log.call_args.args[0])

    def test_copy_info_handles_backend_without_diagnostics(self):
        self.app.gpu = SimpleNamespace()
        self.app._rail_writable = set()
        self.app.copy_rail_limit_diagnostics()
        copied = self.ui.set_clipboard_text.call_args.args[0]
        self.assertIn("does not provide rail diagnostics", copied)


if __name__ == "__main__":
    unittest.main()
