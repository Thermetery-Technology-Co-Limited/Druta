# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Preset behavior with an in-memory controller; no NVAPI or GPU is accessed."""
import copy
import json
import threading
from unittest.mock import patch

import pytest

from druta import icx_presets as presets

INITIAL = {
    "nvvdd_mode": "auto", "nvvdd_request_mv": None, "fbvdd_request_mv": 1350,
    "nvvdd_frequency_khz": 300, "fbvdd_frequency_khz": 300,
    "nvvdd_ocp_enabled": True, "fbvdd_ocp_enabled": True, "loadline_level": 0,
}
FLAGS = {
    "firmware_ok": True, "state_available": True, "frequency_state_available": True,
    "ocp_state_available": True, "loadline_state_available": True,
}


def telemetry(**changes):
    return {**FLAGS, **INITIAL, **changes}


def document(**changes):
    return presets.make_preset(telemetry(**changes))


class Controller:
    """Models requested state independently of JSON encoding and operation planning."""

    def __init__(self, **changes):
        self.settings = {**INITIAL, **changes}
        self._mutex = threading.RLock()
        self.calls = []
        self.reads = 0
        self.fail = None
        self.raise_failure = False
        self.apply_failure = False
        self.ignore = None
        self.missing_reads = 0
        self.missing_after_write = 0
        self.final_override = None

    def telemetry(self):
        self.reads += 1
        state = {**FLAGS, **self.settings}
        if self.missing_reads:
            self.missing_reads -= 1
            state["frequency_state_available"] = False
        elif self.calls and self.missing_after_write:
            self.missing_after_write -= 1
            state["ocp_state_available"] = False
        if self.calls and self.final_override:
            state.update(self.final_override)
        return state

    def _apply(self, call, changes, acknowledged):
        assert acknowledged is True
        self.calls.append(call)
        failed = self.fail == call[0]
        if self.ignore != call[0] and (not failed or self.apply_failure):
            self.settings.update(changes)
        if failed and self.raise_failure:
            raise RuntimeError("simulated transport exception")
        return (False, "simulated unverified setter") if failed else (True, "controller request verified")

    def set_nvvdd_auto(self, *, acknowledged=False):
        return self._apply(("auto",), {"nvvdd_mode": "auto", "nvvdd_request_mv": None}, acknowledged)

    def set_nvvdd_mv(self, value, *, acknowledged=False, xoc=False):
        return self._apply(("nv", value, xoc),
                           {"nvvdd_mode": "manual", "nvvdd_request_mv": value}, acknowledged)

    def set_fbvdd_mv(self, value, *, acknowledged=False):
        return self._apply(("fb", value), {"fbvdd_request_mv": value}, acknowledged)

    def set_switching_frequency_khz(self, rail, value, *, acknowledged=False):
        key = "nvvdd_frequency_khz" if rail == "NVVDD" else "fbvdd_frequency_khz"
        return self._apply(("frequency", rail, value), {key: value}, acknowledged)

    def set_ocp_enabled(self, rail, enabled, *, acknowledged=False):
        key = "nvvdd_ocp_enabled" if rail == "NVVDD" else "fbvdd_ocp_enabled"
        return self._apply(("ocp", rail, enabled), {key: enabled}, acknowledged)

    def set_loadline(self, level, *, acknowledged=False):
        return self._apply(("loadline", level), {"loadline_level": level}, acknowledged)


@pytest.fixture(autouse=True)
def no_real_waits():
    with patch.object(presets.time, "sleep"):
        yield


def test_creation_and_validation_copy_current_requests():
    tel = telemetry()
    doc = presets.make_preset(tel, origin="Connection initial requested state")
    assert doc["schema"] == "druta.icx.preset.v1"
    assert doc["settings"] == INITIAL
    assert "factory" not in doc["origin"].lower()
    tel["loadline_level"] = 9
    parsed = presets.validate_preset(doc)
    parsed["loadline_level"] = 8
    assert doc["settings"]["loadline_level"] == 0


@pytest.mark.parametrize("flag", FLAGS)
def test_creation_requires_every_current_readback_group(flag):
    tel = telemetry()
    tel[flag] = False
    with pytest.raises(ValueError, match="Complete current"):
        presets.make_preset(tel)


@pytest.mark.parametrize(("key", "value"), [
    ("nvvdd_mode", "unknown"), ("nvvdd_mode", 0),
    ("nvvdd_request_mv", 950),  # Auto requires null, not its placeholder.
    ("fbvdd_request_mv", None), ("fbvdd_request_mv", 1350.0),
    ("fbvdd_request_mv", True), ("fbvdd_request_mv", 1351),
    ("nvvdd_frequency_khz", 350), ("fbvdd_frequency_khz", 1100),
    ("nvvdd_ocp_enabled", 1), ("fbvdd_ocp_enabled", "Enable"),
    ("loadline_level", False), ("loadline_level", 1.0), ("loadline_level", 16),
])
def test_invalid_field_cannot_reach_any_controller_call(key, value):
    doc = document()
    doc["settings"][key] = value
    rail = Controller()
    ok, _message = presets.apply_preset(rail, doc, acknowledged=True)
    assert not ok
    assert rail.reads == 0
    assert rail.calls == []


@pytest.mark.parametrize("mutation", [
    lambda doc: doc.update(schema="other.schema"),
    lambda doc: doc.update(extra="unexpected"),
    lambda doc: doc["settings"].update(extra="unexpected"),
    lambda doc: doc["settings"].pop("loadline_level"),
    lambda doc: doc.update(origin={"device": 0x2203}),
    lambda doc: doc.update(origin=""),
    lambda doc: doc.update(origin="x" * 513),
])
def test_unknown_schema_keys_and_origin_types_are_rejected(mutation):
    doc = document()
    mutation(doc)
    with pytest.raises(ValueError):
        presets.validate_preset(doc)


def test_manual_high_request_stages_but_requires_xoc_before_any_apply_read():
    # This controller is an in-memory fake. No high voltage is sent to hardware.
    doc = document(nvvdd_mode="manual", nvvdd_request_mv=1280)
    assert presets.validate_preset(doc)["nvvdd_request_mv"] == 1280
    rail = Controller()
    ok, _message = presets.apply_preset(rail, doc, acknowledged=True, xoc=False)
    assert not ok and rail.calls == [] and rail.reads == 0
    ok, message = presets.apply_preset(rail, doc, acknowledged=True, xoc=True)
    assert ok, message
    assert rail.calls == [("nv", 1280, True)]


def test_manual_mode_requires_a_valid_integer_request():
    for value in (None, 0, 749, 951, 1605, 950.0, True):
        doc = document()
        doc["settings"].update(nvvdd_mode="manual", nvvdd_request_mv=value)
        with pytest.raises(ValueError):
            presets.validate_preset(doc)


def test_diagnostic_origin_never_gates_current_target():
    doc = presets.make_preset(telemetry(loadline_level=1), origin="Captured on a different adapter")
    rail = Controller()
    ok, message = presets.apply_preset(rail, doc, acknowledged=True)
    assert ok, message
    assert rail.calls == [("loadline", 1)]


def test_loading_and_preview_are_data_only(tmp_path):
    path = tmp_path / "icx.json"
    doc = document(loadline_level=4)
    presets.write_preset(path, doc)
    loaded = presets.read_preset(path)
    assert loaded == doc
    description = presets.describe_settings(loaded)
    assert "NVVDD Auto" in description
    assert "loadline Level 4" in description
    assert "factory" not in description.lower()
    assert description == presets.describe_settings(presets.validate_preset(loaded))


@pytest.mark.parametrize("kind", ("duplicate", "nonfinite", "oversize", "bad_utf8", "invalid_json"))
def test_file_reader_rejects_ambiguous_or_unbounded_data(tmp_path, kind):
    text = json.dumps(document())
    if kind == "duplicate":
        data = text.replace('"loadline_level": 0', '"loadline_level": 0, "loadline_level": 1').encode()
    elif kind == "nonfinite":
        data = text.replace('"fbvdd_request_mv": 1350', '"fbvdd_request_mv": NaN').encode()
    elif kind == "oversize":
        data = b" " * (presets.MAX_JSON_BYTES + 1)
    elif kind == "bad_utf8":
        data = bytes((255,))
    else:
        data = b"{"
    path = tmp_path / "invalid.json"
    path.write_bytes(data)
    with pytest.raises(ValueError):
        presets.read_preset(path)


def test_missing_acknowledgment_does_not_even_read_controller():
    rail = Controller()
    ok, _message = presets.apply_preset(rail, document(loadline_level=1))
    assert not ok and not rail.calls and rail.reads == 0


def test_unchanged_preset_only_reads_and_verifies():
    rail = Controller()
    ok, message = presets.apply_preset(rail, document(), acknowledged=True)
    assert ok, message
    assert not rail.calls
    assert rail.reads >= 2


def test_auto_and_enabling_protection_precede_other_changed_fields():
    rail = Controller(nvvdd_mode="manual", nvvdd_request_mv=1000,
                      fbvdd_request_mv=1300, nvvdd_ocp_enabled=False)
    desired = document(fbvdd_request_mv=1360, nvvdd_frequency_khz=400,
                       fbvdd_frequency_khz=500, loadline_level=2,
                       fbvdd_ocp_enabled=False)
    ok, message = presets.apply_preset(rail, desired, acknowledged=True)
    assert ok, message
    assert rail.calls == [
        ("auto",), ("ocp", "NVVDD", True), ("fb", 1360),
        ("frequency", "NVVDD", 400), ("frequency", "FBVDD", 500),
        ("loadline", 2), ("ocp", "FBVDD", False),
    ]
    assert rail.settings == desired["settings"]


def test_manual_nvvdd_is_late_and_disabling_protection_is_last():
    rail = Controller()
    desired = document(nvvdd_mode="manual", nvvdd_request_mv=950,
                       nvvdd_frequency_khz=400, loadline_level=1,
                       nvvdd_ocp_enabled=False, fbvdd_ocp_enabled=False)
    ok, message = presets.apply_preset(rail, desired, acknowledged=True)
    assert ok, message
    assert rail.calls == [
        ("frequency", "NVVDD", 400), ("loadline", 1), ("nv", 950, False),
        ("ocp", "NVVDD", False), ("ocp", "FBVDD", False),
    ]


def test_only_changed_fields_are_applied():
    rail = Controller()
    ok, message = presets.apply_preset(rail, document(fbvdd_frequency_khz=400), acknowledged=True)
    assert ok, message
    assert rail.calls == [("frequency", "FBVDD", 400)]


def test_missing_required_setter_prevents_every_change():
    rail = Controller()
    rail.set_loadline = None
    ok, _message = presets.apply_preset(
        rail, document(fbvdd_request_mv=1360, loadline_level=1), acknowledged=True)
    assert not ok and rail.calls == []


@pytest.mark.parametrize("raise_failure", (False, True))
def test_first_failure_stops_and_does_not_rollback_or_resend(raise_failure):
    rail = Controller()
    rail.fail = "frequency"
    rail.raise_failure = raise_failure
    rail.apply_failure = True
    desired = document(fbvdd_request_mv=1360, nvvdd_frequency_khz=400, loadline_level=1)
    ok, message = presets.apply_preset(rail, desired, acknowledged=True)
    assert not ok
    assert rail.calls == [("fb", 1360), ("frequency", "NVVDD", 400)]
    assert rail.settings["fbvdd_request_mv"] == 1360
    assert rail.settings["nvvdd_frequency_khz"] == 400
    assert rail.settings["loadline_level"] == 0
    assert "1 verified" in message
    assert "may also have applied" in message
    assert rail.reads == 1


def test_successful_setter_result_does_not_replace_final_readback():
    rail = Controller()
    rail.ignore = "loadline"
    ok, message = presets.apply_preset(rail, document(loadline_level=1), acknowledged=True)
    assert not ok
    assert "final readback" in message
    assert rail.calls == [("loadline", 1)]
    assert 2 <= rail.reads <= 5


def test_final_readback_checks_unselected_fields_too():
    rail = Controller()
    rail.final_override = {"fbvdd_ocp_enabled": False}
    ok, message = presets.apply_preset(rail, document(loadline_level=1), acknowledged=True)
    assert not ok
    assert "fbvdd_ocp_enabled" in message
    assert rail.calls == [("loadline", 1)]


def test_transient_incomplete_readback_recovers_without_write_retry():
    rail = Controller()
    rail.missing_reads = 1
    rail.missing_after_write = 1
    ok, message = presets.apply_preset(rail, document(loadline_level=1), acknowledged=True)
    assert ok, message
    assert rail.calls == [("loadline", 1)]


def test_persistent_incomplete_current_readback_prevents_apply():
    rail = Controller()
    rail.missing_reads = 20
    ok, _message = presets.apply_preset(rail, document(loadline_level=1), acknowledged=True)
    assert not ok
    assert rail.calls == []
    assert rail.reads <= 5


def test_persistent_incomplete_final_readback_is_not_success():
    rail = Controller()
    rail.missing_after_write = 20
    ok, message = presets.apply_preset(rail, document(loadline_level=1), acknowledged=True)
    assert not ok
    assert "final readback" in message
    assert rail.calls == [("loadline", 1)]


def test_connection_reset_is_an_explicit_captured_preset_not_factory_values():
    captured = presets.make_preset(
        telemetry(nvvdd_mode="manual", nvvdd_request_mv=950, loadline_level=3),
        origin="Connection initial requested state")
    before = copy.deepcopy(captured)
    rail = Controller(nvvdd_mode="manual", nvvdd_request_mv=1000, loadline_level=4)
    assert presets.validate_preset(captured)["loadline_level"] == 3
    assert rail.calls == []
    ok, message = presets.apply_preset(rail, captured, acknowledged=True)
    assert ok, message
    assert rail.calls == [("loadline", 3), ("nv", 950, False)]
    assert captured == before
    assert rail.settings == captured["settings"]
