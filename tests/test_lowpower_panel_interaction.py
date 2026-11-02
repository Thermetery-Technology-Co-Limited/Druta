"""Cross-panel invalidation for known low-power policy side effects."""

import sys
from types import SimpleNamespace
from unittest.mock import Mock

import dearpygui.dearpygui as dpg
import pytest

from druta.registry_panel import RegistryPanel


class Adaptive:
    def __init__(self, gpu, index):
        self.gpu, self.index = gpu, index

    def read(self):
        self.gpu.calls.append(("adaptive_read", self.index))
        return {"index": self.index, "available": True, "enabled": True, "name": "role"}

    def set_allowed(self, value):
        self.gpu.calls.append(("adaptive_set", self.index, value))
        if self.gpu.fail:
            raise RuntimeError("post-write failure")
        return {"index": self.index, "available": True, "enabled": True,
                "requested_allowed": value, "request_accepted": True, "name": "role",
                "verification": "already_allowed" if self.gpu.noop else "aggregate_only"}


class Threshold:
    def __init__(self, gpu, index):
        self.gpu, self.index = gpu, index

    def read(self):
        self.gpu.calls.append(("threshold_read", self.index))
        return {"index": self.index, "available": True, "name": "threshold", "applied_us": 100,
                "minimum_us": 1, "maximum_us": 100000, "scale_factor": 108000}

    def apply(self, value):
        self.gpu.calls.append(("threshold_set", self.index, value))
        if self.gpu.fail:
            raise RuntimeError("post-write failure")
        return {"index": self.index, "available": True, "name": "threshold", "applied_us": value,
                "minimum_us": 1, "maximum_us": 100000, "scale_factor": 108000,
                "changed": not self.gpu.noop}


@pytest.fixture
def views(monkeypatch):
    monkeypatch.setitem(sys.modules, "druta.adaptive_power_live", SimpleNamespace(AdaptivePowerController=Adaptive))
    monkeypatch.setitem(sys.modules, "druta.gr_idle_threshold_live", SimpleNamespace(IdleThresholdController=Threshold))
    app = SimpleNamespace(gpu=SimpleNamespace(calls=[], fail=False, noop=False), s=lambda n: n,
                          guard=Mock(return_value=True), log=Mock())
    parent = RegistryPanel(app)
    dpg.create_context()
    with dpg.window():
        parent.build()
    yield parent
    dpg.destroy_context()


def select(parent, threshold, adaptive):
    dpg.set_value("gr_idle_threshold_controller", threshold)
    parent.gr_idle_threshold_panel._selection_changed()
    dpg.set_value("adaptive_power_controller", adaptive)
    parent.adaptive_power_panel._selection_changed()
    parent.gr_idle_threshold_panel.read()
    parent.adaptive_power_panel.read()
    dpg.set_value("gr_idle_threshold_value", "101")
    parent.gr_idle_threshold_panel._input_changed()
    parent.app.gpu.calls.clear()


def enabled(tag):
    return dpg.get_item_configuration(tag)["enabled"]


@pytest.mark.parametrize("name", ["Graphics (GR)", "Memory subsystem (MS)", "NVDEC", "NVENC", "NVJPG", "OFA"])
def test_both_directions_invalidate_matching_role_and_refresh(views, name):
    select(views, name, name)
    views.adaptive_power_panel.disable()
    assert views.app.gpu.calls == [("adaptive_set", {"Graphics (GR)": 0, "Memory subsystem (MS)": 2,
            "NVDEC": 4, "NVENC": 5, "NVJPG": 6, "OFA": 7}[name], False)]
    assert "refresh required" in dpg.get_value("gr_idle_threshold_readback")
    assert dpg.get_value("gr_idle_threshold_value") == "101" and not enabled("gr_idle_threshold_apply")
    views.gr_idle_threshold_panel.read()
    assert enabled("gr_idle_threshold_apply") and dpg.get_value("gr_idle_threshold_value") == "101"
    views.app.gpu.calls.clear()
    views.gr_idle_threshold_panel.apply()
    assert len(views.app.gpu.calls) == 1 and views.app.gpu.calls[0][0] == "threshold_set"
    assert "refresh required" in dpg.get_value("adaptive_power_readback")
    assert not enabled("adaptive_power_allow") and not enabled("adaptive_power_disable")
    views.adaptive_power_panel.read()
    assert enabled("adaptive_power_allow") and enabled("adaptive_power_disable")


@pytest.mark.parametrize("threshold", ["NVENC", "MS DIFR SW-ASR", "MS DIFR CG"])
def test_unrelated_and_difr_selections_remain_current(views, threshold):
    select(views, threshold, "NVDEC")
    views.adaptive_power_panel.disable()
    assert enabled("gr_idle_threshold_apply")
    views.gr_idle_threshold_panel.apply()
    assert enabled("adaptive_power_allow")


@pytest.mark.parametrize("direction", ["adaptive", "threshold"])
def test_failed_after_possible_write_invalidates_sibling(views, direction):
    select(views, "Graphics (GR)", "Graphics (GR)")
    views.app.gpu.fail = True
    if direction == "adaptive":
        views.adaptive_power_panel.disable()
        assert not enabled("gr_idle_threshold_apply")
        assert dpg.get_value("gr_idle_threshold_value") == "101"
    else:
        views.gr_idle_threshold_panel.apply()
        assert not enabled("adaptive_power_allow")
    assert len(views.app.gpu.calls) == 1


def test_confirmed_noops_do_not_invalidate(views):
    select(views, "Graphics (GR)", "Graphics (GR)")
    views.app.gpu.noop = True
    views.adaptive_power_panel.allow()
    assert enabled("gr_idle_threshold_apply")
    views.gr_idle_threshold_panel.apply()
    assert enabled("adaptive_power_allow")


def test_other_gpu_and_rejected_guard_do_not_invalidate(views):
    select(views, "Graphics (GR)", "Graphics (GR)")
    views._adaptive_requested(object(), 0)
    views._threshold_requested(object(), 0)
    views.app.guard.return_value = False
    views.adaptive_power_panel.disable()
    views.gr_idle_threshold_panel.apply()
    assert enabled("gr_idle_threshold_apply") and enabled("adaptive_power_allow")
    assert not views.app.gpu.calls
