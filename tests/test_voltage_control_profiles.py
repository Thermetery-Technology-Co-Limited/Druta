import copy
from types import SimpleNamespace

import pytest

from druta import profiles
from druta import voltage_control_profiles as scoped

IDENTITY = {"slot": "0000:01:00.0", "architecture": 7, "device_id": 1, "subsystem_id": 2}


def gpu():
    return SimpleNamespace(static={"uuid": "u"}, slot=lambda: IDENTITY["slot"])


def rel_snapshot():
    return {
        "identity": IDENTITY,
        "rows": [
            {"rail": 0, "status": "ready", "fixed_status": "ready", "equation": 4, "lower_uv": 700, "upper_uv": 1000},
            {"rail": 1, "status": "ready", "fixed_status": "ready", "equation": 5, "lower_uv": 600, "upper_uv": 900},
        ],
    }


def test_upper_only_snapshot_and_undo_preserve_lower(monkeypatch):
    snapshot = rel_snapshot()
    writes = []

    class Controller:
        def read(self):
            return copy.deepcopy(snapshot)

        def set(self, rail, upper):
            writes.append((rail, upper))

    monkeypatch.setattr(scoped, "controller", lambda gpu, kind: Controller())
    state = profiles.capture_voltage_controls(gpu(), "rel_bounds", snapshot=snapshot, selection=(0, "upper"))
    assert state["voltage_controls"]["rows"] == [{"rail": 0, "equation": 4, "upper_uv": 1000}]
    snapshot["rows"][0]["lower_uv"] = 650
    assert all(ok for ok, _ in profiles.restore(gpu(), state))
    assert writes == [(0, 1000)]


def test_all_identity_checks_precede_writes(monkeypatch):
    snapshot = rel_snapshot()
    state = profiles.capture_voltage_controls(gpu(), "rel_bounds", snapshot=snapshot)
    snapshot["rows"][1]["equation"] = 99
    writes = []

    class Controller:
        def read(self):
            return copy.deepcopy(snapshot)

        def set_bounds(self, *args):
            writes.append(args)

    monkeypatch.setattr(scoped, "controller", lambda gpu, kind: Controller())
    assert profiles.preflight(gpu(), state)
    assert not profiles.restore(gpu(), state)[0][0]
    assert writes == []


def test_partial_failure_stops(monkeypatch):
    snapshot = rel_snapshot()
    state = profiles.capture_voltage_controls(gpu(), "rel_bounds", snapshot=snapshot)
    writes = []

    class Controller:
        def read(self):
            return copy.deepcopy(snapshot)

        def set_bounds(self, rail, lower, upper):
            writes.append(rail)
            if rail == 1:
                raise RuntimeError("write failure")

    monkeypatch.setattr(scoped, "controller", lambda gpu, kind: Controller())
    result = profiles.restore(gpu(), state)
    assert [ok for ok, _ in result] == [True, False]
    assert writes == [0, 1]


def test_gate_and_delay_profiles_reject_untyped_or_opaque_data():
    gate = profiles.capture_voltage_controls(gpu(), "gate", snapshot={"identity": IDENTITY, "enabled": True})
    assert not profiles.incomplete(gate)
    gate["voltage_controls"]["rows"][0]["enabled"] = 1
    assert profiles.incomplete(gate)
    delay = {
        "identity": IDENTITY,
        "rows": [
            {
                "index": 2,
                "type": 3,
                "voltage_domain": 1,
                "operation_type": 1,
                "i2c_device_index": 4,
                "switch_delay_us": 6,
                "status": "ready",
                "info_hex": "secret",
            }
        ],
    }
    state = profiles.capture_voltage_controls(gpu(), "switch_delay", snapshot=delay)
    assert "info_hex" not in str(state)
    state["voltage_controls"]["rows"][0]["info_hex"] = "00"
    with pytest.raises(ValueError):
        scoped.validate(state)


def test_unrelated_unready_rows_do_not_block_constants():
    snapshot = {
        "identity": IDENTITY,
        "rows": [
            {"rail": 0, "role": "ALT", "equation": 2, "value_uv": 0, "status": "ready"},
            {"rail": 1, "role": "OV", "status": "unsupported"},
        ],
    }
    state = profiles.capture_voltage_controls(gpu(), "constants", snapshot=snapshot)
    assert len(state["voltage_controls"]["rows"]) == 1


def test_named_rel_keeps_zero_lower_and_upper_only_capability():
    snapshot = rel_snapshot()
    snapshot["rows"][0]["lower_uv"] = 0
    snapshot["rows"][1]["fixed_status"] = "unsupported"
    state = profiles.capture_voltage_controls(gpu(), "rel_bounds", snapshot=snapshot)
    rows = state["voltage_controls"]["rows"]
    assert rows[0]["lower_uv"] == 0
    assert rows[1] == {"rail": 1, "equation": 5, "upper_uv": 900}
    assert not profiles.incomplete(state)


def test_autosave_captures_exact_target_fields(monkeypatch):
    snapshot = rel_snapshot()
    template = profiles.capture_voltage_controls(gpu(), "rel_bounds", snapshot=snapshot, selection=(1, "upper"))
    snapshot["rows"][1]["upper_uv"] = 850
    monkeypatch.setattr(scoped, "controller", lambda gpu, kind: SimpleNamespace(read=lambda: copy.deepcopy(snapshot)))
    monkeypatch.setattr(profiles, "autosave_state", lambda action, state: state)
    undo = profiles.autosave_voltage_controls(gpu(), "test", template)
    assert undo["voltage_controls"]["rows"] == [{"rail": 1, "equation": 5, "upper_uv": 850}]


def test_device_descriptions_do_not_override_uuid(monkeypatch):
    adapter = gpu()
    adapter.static.update(name="before", driver="old", vbios="old")
    snapshot = {"identity": IDENTITY, "enabled": True}
    state = profiles.capture_voltage_controls(adapter, "gate", snapshot=snapshot)
    adapter.static.update(name="after", driver="new", vbios="new")
    monkeypatch.setattr(scoped, "controller", lambda gpu, kind: SimpleNamespace(read=lambda: snapshot))
    assert profiles.preflight(adapter, state) is None
    adapter.static["uuid"] = "other"
    assert "UUID" in profiles.preflight(adapter, state)


@pytest.mark.parametrize(
    "kind,snapshot,expected",
    [
        ("gate", {"identity": IDENTITY, "enabled": True}, ("gate", True, True)),
        (
            "constants",
            {
                "identity": IDENTITY,
                "rows": [{"rail": 0, "role": "OV", "equation": 2, "value_uv": 1000, "status": "ready"}],
            },
            ("constants", 0, "OV", 1000),
        ),
        (
            "switch_delay",
            {
                "identity": IDENTITY,
                "rows": [
                    {
                        "index": 2,
                        "type": 3,
                        "voltage_domain": 1,
                        "operation_type": 2,
                        "i2c_device_index": 4,
                        "switch_delay_us": 6,
                        "status": "ready",
                    }
                ],
            },
            ("switch_delay", 2, 6, 1, 2),
        ),
    ],
)
def test_each_control_restores_via_qualified_setter(monkeypatch, kind, snapshot, expected):
    calls = []

    class Controller:
        def read(self):
            return copy.deepcopy(snapshot)

        def set_enabled(self, value, *, expected):
            calls.append(("gate", value, expected))

        def set(self, *args, **kwargs):
            if kind == "constants":
                calls.append(("constants", *args))
            else:
                calls.append(("switch_delay", *args, kwargs["voltage_domain"], kwargs["operation_type"]))

    monkeypatch.setattr(scoped, "controller", lambda gpu, kind: Controller())
    state = profiles.capture_voltage_controls(gpu(), kind, snapshot=snapshot)
    assert all(ok for ok, _ in profiles.restore(gpu(), state))
    assert calls == [expected]


def test_failure_identifies_target(monkeypatch):
    snapshot = {
        "identity": IDENTITY,
        "rows": [{"rail": 1, "role": "VMIN", "equation": 2, "value_uv": 1000, "status": "ready"}],
    }

    class Controller:
        def read(self):
            return snapshot

        def set(self, *args):
            raise RuntimeError("failure")

    monkeypatch.setattr(scoped, "controller", lambda gpu, kind: Controller())
    state = profiles.capture_voltage_controls(gpu(), "constants", snapshot=snapshot)
    assert "rail 1 VMIN" in profiles.restore(gpu(), state)[0][1]
