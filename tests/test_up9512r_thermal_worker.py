# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""uP9512R verifies voltage without CUDA or frequency settling prerequisites."""
from types import SimpleNamespace
from unittest.mock import Mock, patch

from druta import druta
from druta.controllers.up9512r import UP9512R
from tests import test_arch_ui_regressions as ui_fixture
from tests.controllers import test_mp_candidates_ui as candidate_fixture
from tests.controllers.test_up9512r import Bus


class UP9512RVoltageWorkerTests(ui_fixture.FakeUiTest):
    def setUp(self):
        super().setUp()
        self.bus = Bus()
        self.rail = UP9512R(self.bus)
        self.rail.verify = Mock(return_value=(True, "response and restoration confirmed", []))
        self.app.rail = self.rail
        self.app.gpu = self.gpu = SimpleNamespace(
            nvapi=self.bus, read_vcore_mv=Mock(return_value=900),
            clock_step_khz=Mock(side_effect=AssertionError("clock bin is irrelevant")),
            step_is_measured=Mock(side_effect=AssertionError("clock bin is irrelevant")))
        self.app._i2c_busy = True
        self.app._i2c_recovery_for = None
        self.point = Mock(return_value=(0, None, None))

    def run_worker(self, *, voltage_only):
        rail = self.app.rail
        with patch.object(druta.gpuload, "induce", return_value={"result": 900}) as warmup, \
                patch.object(druta.gpuload, "verify_in_p0",
                             side_effect=lambda gpu, callback, **kwargs: callback(self.point)) as held:
            self.app._i2c_verify_worker()
        if voltage_only:
            warmup.assert_not_called()
        else:
            warmup.assert_called_once()
        held.assert_called_once()
        self.assertIs(held.call_args.args[0], self.gpu)
        self.assertEqual(held.call_args.kwargs["voltage_mv"], None if voltage_only else 900)
        self.assertIs(held.call_args.kwargs.get("require_stable_clocks", True), not voltage_only)
        rail.verify.assert_called_once()
        kwargs = rail.verify.call_args.kwargs
        self.assertIs(kwargs["operating_point"], self.point)
        self.assertTrue(kwargs["acknowledged"])
        self.assertTrue(callable(kwargs["cancelled"]))
        self.assertNotIn("core_clock_tolerance_mhz", kwargs)
        self.assertTrue(self.app.i2c_verified())
        self.assertFalse(self.app._i2c_busy)
        self.assertTrue(self.app._i2c_ui_pending)
        self.gpu.step_is_measured.assert_not_called()
        self.gpu.clock_step_khz.assert_not_called()
        self.assertFalse(self.bus.reads)
        self.assertFalse(self.bus.writes)

    def test_actual_up9512r_skips_cuda_and_frequency_settling(self):
        self.run_worker(voltage_only=True)

    def test_other_offset_controller_keeps_cuda_and_strict_frequency_settling(self):
        self.app.rail = candidate_fixture.candidate()
        self.run_worker(voltage_only=False)
