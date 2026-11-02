"""Offline UI coverage for independent session-owned individual requests."""

import sys
from types import SimpleNamespace
from unittest.mock import Mock

import dearpygui.dearpygui as dpg
import pytest

from druta.individual_power_gating_panel import IndividualPowerGatingPanel


class FakeController:
    def __init__(self, gpu, index):
        self.gpu, self.index = gpu, index
        self.name = {0: "Graphics (GR)", 12: "NVDEC", 13: "NVENC", 14: "OFA", 15: "NVJPG"}[index]
        self.support = self.available = True
        self.owned = self.uncertain = False
        self.masks = {0x48: 0, 0x49: 0}
        self.failure = None
        self.close = Mock()

    def read(self):
        if self.failure:
            raise RuntimeError(self.failure)
        return {"index": self.index, "name": self.name, "support": self.support, "available": self.available,
                "reason_masks": self.masks.copy(), "owned_request": self.owned,
                "uncertain_outcome": self.uncertain, "last_verification_error": None}

    def hold(self):
        if self.failure:
            raise RuntimeError(self.failure)
        self.owned = True
        self.masks[0x48] |= 1
        return self.read()

    def release(self):
        self.owned = self.uncertain = False
        self.masks[0x48] &= ~1
        self.failure = None
        return self.read()


@pytest.fixture
def panel(monkeypatch):
    monkeypatch.setitem(sys.modules, "druta.individual_power_gating_live",
                        SimpleNamespace(IndividualPowerGatingController=FakeController))
    dpg.create_context()
    app = SimpleNamespace(gpu=object(), s=lambda value: value, guard=Mock(return_value=True), log=Mock())
    instance = IndividualPowerGatingPanel(app)
    with dpg.window():
        instance.build()
    try:
        yield instance
    finally:
        dpg.destroy_context()


def enabled(index, action):
    return dpg.get_item_configuration(f"individual_power_gating_{index}_{action}")["enabled"]


def call(panel, operation, index):
    getattr(panel, operation)(None, None, index)


def test_per_controller_hold_release_and_explainer(panel):
    text = dpg.get_value("individual_power_gating_explainer")
    assert "individual power-gating inhibit" in text and "not physical" in text and "not reapplied" in text
    for index in (0, 12, 13, 14, 15):
        call(panel, "read", index)
        assert enabled(index, "hold") and not enabled(index, "release")
    call(panel, "hold", 12)
    assert "disable request: held" in dpg.get_value("individual_power_gating_12_readback")
    assert not enabled(12, "hold") and enabled(12, "release")
    assert all(enabled(index, "hold") for index in (13, 14, 15))
    call(panel, "release", 12)
    assert enabled(12, "hold") and not enabled(12, "release")


def test_graphics_request_is_independent_of_media_controllers(panel):
    panel._controller(12).available = False
    call(panel, "read", 0)
    call(panel, "read", 12)
    call(panel, "read", 13)
    assert enabled(0, "hold") and not enabled(12, "hold") and enabled(13, "hold")
    call(panel, "hold", 0)
    assert "Graphics (GR) request support: available" in dpg.get_value("individual_power_gating_0_readback")
    assert "disable request: held" in dpg.get_value("individual_power_gating_0_readback")
    assert not enabled(0, "hold") and enabled(0, "release") and enabled(13, "hold")
    assert panel._controller(13).masks[0x48] == 0
    call(panel, "release", 0)
    assert enabled(0, "hold") and not enabled(0, "release")
    assert enabled(13, "hold")


def test_unavailable_peer_and_existing_request_do_not_remove_other_controls(panel):
    panel._controller(12).available = False
    panel._controller(13).masks[0x48] = 1
    for index in (12, 13, 14):
        call(panel, "read", index)
    assert "support: unavailable" in dpg.get_value("individual_power_gating_12_readback")
    assert not enabled(12, "hold") and not enabled(13, "hold")
    assert enabled(14, "hold")


def test_other_reason_bank_does_not_block_independent_request(panel):
    panel._controller(15).masks[0x49] = 0x81
    call(panel, "read", 15)
    assert enabled(15, "hold")
    call(panel, "hold", 15)
    call(panel, "release", 15)
    assert panel._controller(15).masks[0x49] == 0x81
    assert "0x49=0x00000081" in dpg.get_value("individual_power_gating_15_readback")


def test_readback_distinguishes_other_inhibit_from_own_reference(panel):
    panel._controller(13).masks[0x48] = 1
    call(panel, "read", 13)
    text = dpg.get_value("individual_power_gating_13_readback")
    assert "disable request: not held" in text
    assert "Reason-zero inhibit readback: present (may include other owners)" in text
    assert not enabled(13, "hold") and not enabled(13, "release")


def test_uncertain_display_does_not_promise_a_release_retry(panel):
    panel._controller(12).uncertain = True
    call(panel, "read", 12)
    text = dpg.get_value("individual_power_gating_12_readback")
    assert "Release may already have been attempted" in text
    assert "use Release" not in text


def test_already_attempted_uncertain_release_does_not_claim_success(panel, monkeypatch):
    controller = panel._controller(12)
    snapshot = controller.read() | {"uncertain_outcome": True, "release_attempted": True}
    monkeypatch.setattr(controller, "release", Mock(return_value=snapshot))
    call(panel, "release", 12)
    status = dpg.get_value("individual_power_gating_12_status")
    assert "outcome remains uncertain" in status and "no new release was dispatched" in status
    assert "was released" not in status
    assert not enabled(12, "hold") and not enabled(12, "release")
    assert enabled(12, "read")


def test_failed_refresh_and_uncertain_hold_do_not_disable_peers(panel):
    call(panel, "read", 12)
    call(panel, "read", 13)
    controller = panel._controller(12)
    controller.failure = "temporary"
    call(panel, "hold", 12)
    assert not enabled(12, "hold") and enabled(12, "release") and enabled(13, "hold")
    assert "NVDEC hold failed" in dpg.get_value("individual_power_gating_12_status")
    call(panel, "read", 12)
    assert enabled(12, "release") and enabled(13, "hold")
    call(panel, "release", 12)
    assert not enabled(12, "release") and enabled(12, "hold")


def test_cache_is_per_gpu_and_controller(panel):
    first = panel._controller(12)
    assert panel._controller(12) is first and panel._controller(13) is not first
    panel.app.gpu = object()
    assert panel._controller(12) is not first


def test_cleanup_closes_only_selected_gpu_and_keeps_failed_peer(panel):
    from druta.druta import Druta

    old_gpu = panel.app.gpu
    first = panel._controller(12)
    failing = panel._controller(13)
    failing.close.side_effect = RuntimeError("close failed")
    panel.app.gpu = object()
    other = panel._controller(12)
    assert not Druta._close_live_gating_requests(panel.app, old_gpu)
    first.close.assert_called_once_with()
    failing.close.assert_called_once_with()
    other.close.assert_not_called()
    assert (id(old_gpu), 12) not in panel.app._individual_power_gating_controllers
    assert (id(old_gpu), 13) in panel.app._individual_power_gating_controllers
    failing.close.side_effect = None
    assert Druta._close_live_gating_requests(panel.app)
    other.close.assert_called_once_with()
