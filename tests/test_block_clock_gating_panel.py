"""Offline UI coverage for block clock-gating request ownership."""

import sys
from types import SimpleNamespace
from unittest.mock import Mock

import dearpygui.dearpygui as dpg
import pytest

from druta.block_clock_gating_panel import BlockClockGatingPanel


class FakeController:
    def __init__(self, gpu):
        self.gpu = gpu
        self.uncertain = False
        self.fail_read = False
        self.releases = 0

    def read(self):
        if self.fail_read:
            raise RuntimeError("read failed")
        return {
            "support": True,
            "registers": {"0x1234": 0x45},
            "allowed_clear_masks": {"0x1234": 4},
            "observed_register_bits": {"0x1234": 4, "0x5678": 0},
            "owned_request": False,
            "uncertain_outcome": self.uncertain,
            "last_verification_error": "readback mismatch",
        }

    def hold(self):
        self.uncertain = True
        raise RuntimeError("request outcome unknown")

    def release(self):
        self.releases += 1
        self.uncertain = False
        self.fail_read = False
        return self.read()


@pytest.fixture
def panel(monkeypatch):
    monkeypatch.setitem(sys.modules, "druta.block_clock_gating_live",
                        SimpleNamespace(BlockClockGatingController=FakeController))
    dpg.create_context()
    app = SimpleNamespace(gpu=object(), s=lambda value: value, guard=Mock(return_value=True), log=Mock())
    instance = BlockClockGatingPanel(app)
    with dpg.window():
        instance.build()
    yield instance
    dpg.destroy_context()


def enabled(tag):
    return dpg.get_item_configuration("block_clock_gating_" + tag)["enabled"]


def test_uncertain_hold_retains_release_through_failed_refresh(panel):
    panel.read()
    readback = dpg.get_value("block_clock_gating_readback")
    assert "Register 0x1234: 0x00000045" in readback
    assert "Registers with observed changes: 1" in readback
    assert enabled("hold")

    panel.hold()
    assert not enabled("hold")
    assert enabled("release")
    panel._controller().fail_read = True
    panel.read()
    assert enabled("release")

    panel.app.guard.return_value = False
    panel.release()
    assert panel._controller().releases == 1
    assert not enabled("release")
    assert "readback mismatch" in dpg.get_value("block_clock_gating_readback")


def test_reset_view_disables_actions(panel):
    panel.read()
    panel.reset_view()
    assert not enabled("hold")
    assert not enabled("release")
    assert dpg.get_value("block_clock_gating_readback") == ""
