"""Read-only UI tests for the instance-owned DIFR disable request."""

from types import SimpleNamespace
from unittest.mock import Mock

import dearpygui.dearpygui as dpg
import pytest

from druta import difr_live_control, difr_live_panel


def snapshot(*, support=True, enabled=True, masks=None, owned=False, uncertain=False):
    return {
        "support": support,
        "present": True,
        "enabled_predicate": enabled,
        "reason_masks": dict(masks or {0x48: 0, 0x49: 0}),
        "owned_request": owned,
        "uncertain_outcome": uncertain,
    }


class FakeController:
    def __init__(self, gpu):
        self.gpu = gpu
        self.state = snapshot()
        self.failure = None
        self.uncertain_on_failure = False
        self.read_calls = 0
        self.hold_calls = 0
        self.release_calls = 0

    def read(self):
        self.read_calls += 1
        if self.failure:
            raise self.failure
        return self.state.copy() | {"reason_masks": self.state["reason_masks"].copy()}

    def hold(self):
        self.hold_calls += 1
        if self.failure:
            if self.uncertain_on_failure:
                self.state["uncertain_outcome"] = True
            raise self.failure
        masks = self.state["reason_masks"].copy()
        masks[0x49] |= 1
        self.state = snapshot(enabled=self.state["enabled_predicate"], masks=masks, owned=True)
        return self.read()

    def release(self):
        self.release_calls += 1
        if self.failure:
            raise self.failure
        masks = self.state["reason_masks"].copy()
        masks[0x49] &= ~1
        self.state = snapshot(enabled=self.state["enabled_predicate"], masks=masks, owned=False)
        return self.read()


@pytest.fixture
def harness(monkeypatch):
    dpg.create_context()
    gpu = object()
    app = SimpleNamespace(gpu=gpu, s=lambda value: value, log=Mock(), guard=Mock(return_value=True))
    controllers = []

    def make_controller(target):
        controller = FakeController(target)
        controllers.append(controller)
        return controller

    monkeypatch.setattr(difr_live_control, "DifrController", make_controller)
    panel = difr_live_panel.DifrLivePanel(app)
    with dpg.window():
        panel.build()
    try:
        yield SimpleNamespace(app=app, panel=panel, controllers=controllers)
    finally:
        dpg.destroy_context()


def enabled(tag):
    return dpg.get_item_configuration(tag)["enabled"]


def test_live_difr_copy_keeps_request_and_factory_configuration_separate(harness):
    text = dpg.get_value("difr_live_explainer")
    assert "counted DIFR disable request" in text
    assert "driver-enabled readback is separate" in text
    assert "Release this request before switching GPUs or closing Druta" in text
    assert "Factory-disabled entries remain read-only" in text


def test_live_difr_read_keeps_driver_predicate_separate_from_request(harness):
    h = harness
    h.panel._controller().state.pop("uncertain_outcome")
    h.panel.read()

    text = dpg.get_value("difr_live_readback")
    assert "DIFR request support: available" in text
    assert "Driver-enabled readback (not residency): yes" in text
    assert "This Druta instance's disable request: not held" in text
    assert enabled("difr_live_hold") is True
    assert enabled("difr_live_release") is False

    h.panel.hold()
    text = dpg.get_value("difr_live_readback")
    assert "Driver-enabled readback (not residency): yes" in text
    assert "This Druta instance's disable request: held" in text
    assert enabled("difr_live_hold") is False
    assert enabled("difr_live_release") is True

    h.panel.release()
    text = dpg.get_value("difr_live_readback")
    assert "Driver-enabled readback (not residency): yes" in text
    assert "This Druta instance's disable request: not held" in text
    assert enabled("difr_live_hold") is True
    assert enabled("difr_live_release") is False


def test_existing_counted_request_cannot_be_released_by_this_instance(harness):
    h = harness
    controller = h.panel._controller()
    controller.state = snapshot(masks={0x48: 0, 0x49: 1}, owned=False)

    h.panel.read()

    assert "A disable request already exists; this instance cannot release it." in dpg.get_value(
        "difr_live_readback"
    )
    assert enabled("difr_live_hold") is False
    assert enabled("difr_live_release") is False


def test_unsupported_read_does_not_enable_a_hold(harness):
    h = harness
    controller = h.panel._controller()
    controller.state = snapshot(support=False, enabled=True)

    h.panel.read()

    text = dpg.get_value("difr_live_readback")
    assert "DIFR request support: unavailable" in text
    assert "Driver-enabled readback (not residency): yes" in text
    assert enabled("difr_live_hold") is False


def test_missing_control_presence_does_not_enable_a_hold(harness):
    h = harness
    controller = h.panel._controller()
    controller.state = snapshot()
    controller.state["present"] = False

    h.panel.read()

    assert "Disable request control: unavailable" in dpg.get_value("difr_live_readback")
    assert enabled("difr_live_hold") is False


def test_uncertain_hold_outcome_stays_read_only_after_refresh(harness):
    h = harness
    controller = h.panel._controller()
    controller.failure = RuntimeError("request state changed")
    controller.uncertain_on_failure = True

    h.panel.hold()

    assert "DIFR hold failed: request state changed" in dpg.get_value("difr_live_status")
    assert "Read current status before taking another action" in dpg.get_value("difr_live_status")
    assert enabled("difr_live_hold") is False
    assert enabled("difr_live_release") is False
    h.app.log.assert_called_once()

    controller.failure = None
    h.panel.read()
    readback = dpg.get_value("difr_live_readback")
    assert "request outcome is uncertain" in readback
    assert "Do not send another Hold or Release request" in readback
    assert "disable request: not held" not in readback
    assert enabled("difr_live_hold") is False
    assert enabled("difr_live_release") is False


def test_app_guard_blocks_hold_and_release_but_not_read(harness):
    h = harness
    controller = h.panel._controller()
    h.app.guard.return_value = False

    h.panel.read()
    assert controller.read_calls == 1
    h.panel.hold()
    assert controller.hold_calls == 0

    controller.state = snapshot(masks={0x48: 0, 0x49: 1}, owned=True)
    h.panel.read()
    assert controller.read_calls == 2
    h.panel.release()
    assert controller.release_calls == 0


def test_panel_rebuild_reuses_controller_only_for_same_gpu_object(harness):
    h = harness
    first = h.panel._controller()
    rebuilt = difr_live_panel.DifrLivePanel(h.app)
    assert rebuilt._controller() is first

    h.app.gpu = object()
    other_gpu = rebuilt._controller()
    assert other_gpu is not first
    assert other_gpu.gpu is h.app.gpu
    assert first.gpu is not h.app.gpu
