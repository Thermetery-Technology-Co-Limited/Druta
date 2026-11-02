"""Offline UI coverage for second-level clock-gating request ownership."""

import sys
from types import SimpleNamespace
from unittest.mock import Mock

import dearpygui.dearpygui as dpg
import pytest

from druta.second_level_clock_gating_panel import SecondLevelClockGatingPanel


class FakeController:
    def __init__(self, gpu):
        self.gpu = gpu
        self.uncertain = False
        self.owned = False
        self.mode = None
        self.fail_read = False
        self.registers = {"0x1234": 0}
        self.releases = 0

    def read(self):
        if self.fail_read:
            raise RuntimeError("read failed")
        return {
            "support": True,
            "registers": self.registers.copy(),
            "expected_release_values": {"0x1234": 0},
            "expected_disabled_values": {"0x1234": 1},
            "unavailable_registers": {"0x5678": "unavailable"},
            "observed_registers": {},
            "owned_request": self.owned,
            "owned_mode": self.mode,
            "arbitration_verified": self.mode == "enabled",
            "uncertain_outcome": self.uncertain,
            "last_verification_error": "readback mismatch",
        }

    def hold(self):
        self.uncertain = True
        raise RuntimeError("request outcome unknown")

    def hold_enabled(self):
        self.owned = True
        self.mode = "enabled"
        return self.read()

    def release(self):
        self.releases += 1
        self.uncertain = False
        self.fail_read = False
        self.owned = False
        self.mode = None
        return self.read()


@pytest.fixture
def panel(monkeypatch):
    monkeypatch.setitem(sys.modules, "druta.second_level_clock_gating_live",
                        SimpleNamespace(SecondLevelClockGatingController=FakeController))
    dpg.create_context()
    app = SimpleNamespace(gpu=object(), s=lambda value: value, guard=Mock(return_value=True), log=Mock())
    instance = SecondLevelClockGatingPanel(app)
    with dpg.window():
        instance.build()
    yield instance
    dpg.destroy_context()


def enabled(tag):
    return dpg.get_item_configuration("second_level_clock_gating_" + tag)["enabled"]


def test_eligibility_distinguishes_unavailable_and_existing_request(panel):
    panel.read()
    text = dpg.get_value("second_level_clock_gating_readback")
    assert "Register 0x1234: 0x00000000" in text
    assert "Register 0x5678: unavailable" in text
    assert enabled("hold")
    panel._controller().registers = {"0x1234": 1}
    panel.read()
    assert not enabled("hold")
    panel._controller().owned = True
    panel._controller().mode = "disabled"
    panel.read()
    assert enabled("hold") and enabled("release")
    panel._controller().owned = False
    panel._controller().mode = None
    panel._controller().registers = {}
    panel.read()
    assert not enabled("hold")


def test_uncertain_hold_retains_release_through_failed_refresh(panel):
    panel.read()
    panel.hold()
    assert not enabled("hold") and enabled("release")
    panel._controller().fail_read = True
    panel.read()
    assert enabled("release")
    panel.app.guard.return_value = False
    panel.release()
    assert panel._controller().releases == 1
    assert not enabled("release")
    assert "readback mismatch" in dpg.get_value("second_level_clock_gating_readback")
    panel.reset_view()
    assert not enabled("hold")


@pytest.mark.parametrize("value,eligible", [(1, True), (0, False), (2, False)])
def test_turing_panel_uses_nonzero_baseline_and_discloses_unverified_peers(panel, value, eligible):
    snapshot = panel._controller().read()
    snapshot.update(
        status="partial", registers={"0x24a124": value},
        expected_release_values={"0x24a124": 1},
        expected_disabled_values={"0x24a124": 0},
        unverified_source_peers=[hex(0x246018 + i * 0x200) for i in range(15)],
    )
    panel._show(snapshot, "Refreshed")
    assert enabled("hold") is eligible
    text = dpg.get_value("second_level_clock_gating_readback")
    assert "15 unverified" in text and "prior values are not verified" in text
    assert ("required starting values" in text) is (value == 2)


def test_enable_action_uses_arbitration_witness_and_requires_release_for_opposite_mode(panel):
    panel.read()
    assert enabled("hold") and enabled("hold_enabled")
    panel.hold_enabled()
    text = dpg.get_value("second_level_clock_gating_readback")
    assert "request: held enabled" in text
    assert "Conflicting request rejection verified" in text
    assert "Registers with observed changes" not in text
    assert not enabled("hold") and enabled("hold_enabled") and enabled("release")
    panel.release()
    assert enabled("hold") and enabled("hold_enabled") and not enabled("release")


def test_enable_eligibility_requires_known_source_values_and_available_witness(panel):
    controller = panel._controller()
    controller.registers = {"0x1234": 1}
    panel.read()
    assert enabled("hold_enabled") and not enabled("hold")
    controller.registers = {"0x1234": 2}
    panel.read()
    assert not enabled("hold_enabled") and not enabled("hold")
    controller.registers = {}
    panel.read()
    assert not enabled("hold_enabled") and not enabled("hold")


def test_missing_expected_values_never_enables_a_new_request(panel):
    snapshot = panel._controller().read()
    snapshot.pop("expected_disabled_values")
    snapshot.pop("expected_release_values")
    panel._show(snapshot, "Refreshed")
    assert not enabled("hold_enabled") and not enabled("hold")


def test_mixed_known_source_values_allow_enable_but_not_disable(panel):
    snapshot = panel._controller().read()
    snapshot.update(
        registers={"0x1234": 0, "0x5678": 1}, unavailable_registers={},
        expected_release_values={"0x1234": 0, "0x5678": 0},
        expected_disabled_values={"0x1234": 1, "0x5678": 1},
    )
    panel._show(snapshot, "Refreshed")
    assert enabled("hold_enabled") and not enabled("hold")


def test_enable_explainer_and_unknown_owned_mode_do_not_claim_more(panel):
    text = dpg.get_value("second_level_clock_gating_explainer")
    assert "rejects conflicting profiler requests" in text
    assert "other driver mechanisms" in text and "does not guarantee physical gating" in text
    snapshot = panel._controller().read()
    snapshot.update(owned_request=True, owned_mode=None)
    panel._show(snapshot, "Held")
    assert "held (mode unknown)" in dpg.get_value("second_level_clock_gating_readback")
    assert not enabled("hold") and not enabled("hold_enabled") and enabled("release")
