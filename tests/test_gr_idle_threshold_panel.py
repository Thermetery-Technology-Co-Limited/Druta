"""Offline coverage for the graphics idle threshold panel."""

import sys
from types import SimpleNamespace
from unittest.mock import Mock

import dearpygui.dearpygui as dpg
import pytest

from druta.gr_idle_threshold_panel import GrIdleThresholdPanel


class FakeController:
    instances = []

    def __init__(self, gpu, index):
        self.gpu = gpu
        self.index = index
        self.instances.append(self)

    def read(self):
        if self.gpu.error:
            raise RuntimeError(self.gpu.error)
        return self.gpu.snapshot | {"index": self.index, "name": str(self.index),
                                    "available": self.gpu.snapshot["available"] and self.index not in self.gpu.unavailable}

    def apply(self, value):
        if self.gpu.error:
            raise RuntimeError(self.gpu.error)
        self.gpu.applied.append((self.index, value))
        return self.read() | {"requested_us": value, "applied_us": value - 1, "changed": True}


def gpu(**updates):
    snap = {"available": True, "status": "ready", "architecture": {}, "raw_pair": [10800, 108000000],
            "scaled_pair": [100, 1000000], "scale_factor": 108000, "minimum_us": 1,
            "maximum_us": 39768215, "applied_us": 100}
    return SimpleNamespace(snapshot=snap | updates, error=None, applied=[], unavailable=set())


@pytest.fixture
def panel(monkeypatch):
    FakeController.instances.clear()
    monkeypatch.setitem(sys.modules, "druta.gr_idle_threshold_live",
                        SimpleNamespace(IdleThresholdController=FakeController))
    dpg.create_context()
    app = SimpleNamespace(gpu=gpu(), s=lambda value: value, guard=Mock(return_value=True), log=Mock())
    view = GrIdleThresholdPanel(app)
    with dpg.window():
        view.build()
    try:
        yield view
    finally:
        dpg.destroy_context()


def enabled():
    return dpg.get_item_configuration("gr_idle_threshold_apply")["enabled"]


def enter(view, text):
    dpg.set_value("gr_idle_threshold_value", text)
    view._input_changed()


def test_refresh_only_reads_and_preserves_pending_value(panel):
    assert not enabled() and not FakeController.instances
    panel.read()
    assert dpg.get_value("gr_idle_threshold_value") == "100" and enabled()
    assert panel.app.gpu.applied == []
    assert "108000000" in dpg.get_value("gr_idle_threshold_readback")
    enter(panel, "101")
    panel.read()
    assert dpg.get_value("gr_idle_threshold_value") == "101" and enabled()
    assert panel.app.gpu.applied == []
    assert "not measured" in dpg.get_value("gr_idle_threshold_explainer")


def test_apply_shows_requested_and_actual_value(panel):
    panel.read()
    enter(panel, "101")
    panel.apply()
    assert panel.app.gpu.applied == [(0, 101)]
    assert "Requested 101 µs; driver readback 100 µs" in dpg.get_value("gr_idle_threshold_status")
    assert dpg.get_value("gr_idle_threshold_value") == "101"


def test_guard_and_strict_decimal_bounds(panel):
    panel.read()
    for value in ("0", "-1", "1.5", "1e2", "39768216", "１２", "9" * 21):
        enter(panel, value)
        assert not enabled()
        panel.apply()
    enter(panel, "101")
    panel.app.guard.return_value = False
    panel.apply()
    assert panel.app.gpu.applied == []


def test_ambiguous_or_unavailable_readback_disables_only_apply(panel):
    panel.app.gpu.snapshot.update(status="ambiguous_scale", scale_factor=None, maximum_us=None)
    panel.read()
    assert not enabled() and dpg.get_item_configuration("gr_idle_threshold_read")["enabled"]
    assert "scale could not be determined" in dpg.get_value("gr_idle_threshold_status")
    panel.app.gpu.snapshot.update(available=False, status="not supported")
    panel.read()
    assert not enabled() and "not supported" in dpg.get_value("gr_idle_threshold_readback")


def test_failure_requires_fresh_read_and_preserves_pending_text(panel):
    panel.read()
    enter(panel, "101")
    panel.app.gpu.error = "temporary"
    panel.apply()
    assert not enabled() and dpg.get_value("gr_idle_threshold_value") == "101"
    assert "temporary" in dpg.get_value("gr_idle_threshold_status")
    panel.app.log.assert_called_once()
    panel.app.gpu.error = None
    panel.read()
    assert enabled() and dpg.get_value("gr_idle_threshold_value") == "101"


def test_gpu_switch_requires_fresh_status_and_uses_current_gpu(panel):
    panel.read()
    previous = panel.app.gpu
    panel.app.gpu = gpu()
    panel.apply()
    assert previous.applied == [] and panel.app.gpu.applied == [] and not enabled()
    panel.reset_view()
    panel.read()
    panel.apply()
    assert panel.app.gpu.applied == [(0, 100)] and FakeController.instances[-1].gpu is panel.app.gpu


def select(panel, value):
    dpg.set_value("gr_idle_threshold_controller", value)
    panel._selection_changed()


def test_selection_requires_refresh_and_targets_selected_controller(panel):
    panel.read()
    enter(panel, "101")
    select(panel, "NVENC")
    assert not enabled() and dpg.get_value("gr_idle_threshold_value") == ""
    assert dpg.get_value("gr_idle_threshold_readback") == ""
    assert panel.app.gpu.applied == []
    panel.read()
    assert enabled() and FakeController.instances[-1].index == 13
    enter(panel, "102")
    panel.apply()
    assert panel.app.gpu.applied == [(13, 102)]


def test_unavailable_controller_does_not_block_another(panel):
    panel.app.gpu.unavailable.add(4)
    select(panel, "Memory subsystem (MS)")
    panel.read()
    assert not enabled()
    select(panel, "NVDEC")
    panel.read()
    assert enabled() and FakeController.instances[-1].index == 12


@pytest.mark.parametrize("name,index", [("MS DIFR SW-ASR", 10), ("MS DIFR CG", 11)])
def test_difr_threshold_selection_routes_to_selected_controller(panel, name, index):
    select(panel, name)
    panel.read()
    assert enabled() and FakeController.instances[-1].index == index
    enter(panel, "101")
    panel.apply()
    assert panel.app.gpu.applied == [(index, 101)]


def test_unavailable_difr_does_not_block_other_difr(panel):
    panel.app.gpu.unavailable.add(10)
    select(panel, "MS DIFR SW-ASR")
    panel.read()
    assert not enabled()
    select(panel, "MS DIFR CG")
    panel.read()
    assert enabled() and FakeController.instances[-1].index == 11


def test_invalid_selection_cannot_retarget_stale_readback(panel):
    panel.read()
    enter(panel, "101")
    # A bad value cannot be used even if a selection callback was missed.
    dpg.set_value("gr_idle_threshold_controller", "invalid")
    panel.apply()
    panel.read()
    assert not enabled() and panel.app.gpu.applied == []
    assert "valid controller" in dpg.get_value("gr_idle_threshold_status")


def test_mismatched_controller_readback_never_enables_apply(panel, monkeypatch):
    monkeypatch.setattr(FakeController, "read", lambda self: self.gpu.snapshot | {"index": 13})
    panel.read()
    assert not enabled() and "does not match" in dpg.get_value("gr_idle_threshold_status")
