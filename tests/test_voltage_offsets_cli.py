import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from druta import druta, voltage_offsets_cli

SLOT = "0000:01:00.0"


def _engine(monkeypatch, **methods):
    engine = SimpleNamespace(**methods)
    monkeypatch.setattr(voltage_offsets_cli, "_engine", lambda: engine)
    return engine


def test_main_dispatches_voltage_offsets_before_normal_startup(monkeypatch):
    command = Mock(return_value=0)
    monkeypatch.setattr(voltage_offsets_cli, "cli", command)
    monkeypatch.setattr(druta, "Druta", Mock(side_effect=AssertionError("GUI must not start")))

    assert druta.main(["--voltage-offsets", "--gpu", SLOT, "read"]) == 0

    command.assert_called_once_with(["--gpu", SLOT, "read"], druta._tell)


def test_ordinary_main_does_not_dispatch_voltage_offsets(monkeypatch):
    app = SimpleNamespace(
        gpu=SimpleNamespace(available=Mock(return_value=True)),
        run=Mock(),
        shutdown_is_clean=Mock(return_value=True),
    )
    manager = SimpleNamespace(begin=Mock(return_value=None), watch_shutdown=Mock(), close=Mock())
    command = Mock(side_effect=AssertionError("voltage-offset CLI must stay opt-in"))
    monkeypatch.setattr(voltage_offsets_cli, "cli", command)
    monkeypatch.setattr(druta, "Druta", Mock(return_value=app))
    monkeypatch.setattr(druta.startup, "Startup", Mock(return_value=manager))
    monkeypatch.setattr(druta, "_tell", Mock())

    assert druta.main([]) == 0

    command.assert_not_called()
    app.run.assert_called_once_with()


def test_enable_requires_explicit_slot_and_reports_only_user_data(monkeypatch, capsys):
    result = {
        "slot": SLOT,
        "values": {"voltage.0.REL.offset": 0},
        "policy": {"rails": {"0": {"policy_uv": {"reliability": 1000000}}}},
        "saved_values": {"voltage.0.REL.offset": 0},
        "unsupported": [],
        "original_image_hex": "must not be printed",
        "native_packet_hex": "must not be printed",
    }
    engine = _engine(monkeypatch, enable=Mock(return_value=result))
    wait = Mock(side_effect=AssertionError("direct enable must not wait for a parent"))
    reopen = Mock(side_effect=AssertionError("direct enable must not reopen a second GUI"))
    monkeypatch.setattr(voltage_offsets_cli, "_wait_for_parent", wait)
    monkeypatch.setattr(voltage_offsets_cli, "_reopen_gui", reopen)

    assert voltage_offsets_cli.cli(["--gpu", SLOT, "enable"], Mock()) == 0

    engine.enable.assert_called_once_with(SLOT)
    wait.assert_not_called()
    reopen.assert_not_called()
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "command": "enable",
        "slot": SLOT,
        "values": result["values"],
        "policy": result["policy"],
        "saved_values": result["saved_values"],
        "unsupported": [],
    }


def test_read_returns_reported_policy_and_saved_values(monkeypatch, capsys):
    result = {
        "slot": SLOT,
        "values": {"voltage.0.REL.offset": 0},
        "policy": {"rails": {"0": {"policy_uv": {"reliability": 1000000}}}},
        "saved_values": {"voltage.0.REL.offset": 0},
        "unsupported": ["voltage.0.OV.offset"],
    }
    engine = _engine(monkeypatch, read=Mock(return_value=result))

    assert voltage_offsets_cli.cli(["--gpu", SLOT, "read"], Mock()) == 0

    engine.read.assert_called_once_with(SLOT)
    assert json.loads(capsys.readouterr().out) == {"command": "read", **result}


def test_set_parses_repeated_signed_microvolt_offsets(monkeypatch, capsys):
    result = {
        "slot": SLOT,
        "values": {"voltage.0.REL.offset": -5000, "voltage.1.OV.offset": 2500},
        "policy": {},
        "saved_values": {},
        "unsupported": [],
    }
    engine = _engine(monkeypatch, set_offsets=Mock(return_value=result))

    assert voltage_offsets_cli.cli(
        ["--gpu", SLOT, "set", "--offset", "0:rel=-5000", "--offset", "1:OV=+2500"], Mock()
    ) == 0

    engine.set_offsets.assert_called_once_with(
        SLOT,
        {"voltage.0.REL.offset": -5000, "voltage.1.OV.offset": 2500},
    )
    assert json.loads(capsys.readouterr().out)["values"] == result["values"]


@pytest.mark.parametrize(
    "items",
    [
        ["bad"],
        ["0:UNKNOWN=1"],
        ["0:REL=1.5"],
        ["0:REL=1_000"],
        ["0:REL=2147483648"],
        ["0:REL=-2147483649"],
        ["0:REL=1", "0:rel=2"],
    ],
)
def test_invalid_or_duplicate_offsets_reject_before_engine(monkeypatch, items):
    engine_factory = Mock(side_effect=AssertionError("invalid input must fail before engine import"))
    monkeypatch.setattr(voltage_offsets_cli, "_engine", engine_factory)

    with pytest.raises(SystemExit) as exc:
        voltage_offsets_cli.cli(["--gpu", SLOT, "set", *sum((["--offset", x] for x in items), [])], Mock())

    assert exc.value.code == 2
    engine_factory.assert_not_called()


@pytest.mark.parametrize(
    "argv",
    [
        ["read"],
        ["--gpu", SLOT],
        ["--gpu", SLOT, "set"],
        ["--gpu", SLOT, "replay"],
    ],
)
def test_slot_and_command_arguments_are_required_before_engine_import(monkeypatch, argv):
    engine_factory = Mock(side_effect=AssertionError("invalid syntax must fail before engine import"))
    monkeypatch.setattr(voltage_offsets_cli, "_engine", engine_factory)

    with pytest.raises(SystemExit) as exc:
        voltage_offsets_cli.cli(argv, Mock())

    assert exc.value.code == 2
    engine_factory.assert_not_called()


def test_capture_saves_slot_bound_profile_and_replay_unwraps_values(monkeypatch, tmp_path, capsys):
    values = {"voltage.0.REL.offset": -5000, "voltage.1.VMIN.offset": 2500}
    result = {"slot": SLOT, "values": values, "policy": {}, "saved_values": values, "unsupported": []}
    engine = _engine(monkeypatch, capture=Mock(return_value=values), replay=Mock(return_value=result))
    path = tmp_path / "offset profile.json"

    assert voltage_offsets_cli.cli(["--gpu", SLOT, "capture", "--out", str(path)], Mock()) == 0
    capture_payload = json.loads(capsys.readouterr().out)
    assert capture_payload["values"] == values
    assert json.loads(path.read_text(encoding="utf-8")) == {"schema": 1, "slot": SLOT, "values": values}

    assert voltage_offsets_cli.cli(["--gpu", SLOT, "replay", "--profile", str(path)], Mock()) == 0
    engine.replay.assert_called_once_with(SLOT, values)
    assert json.loads(capsys.readouterr().out)["values"] == values


def test_capture_without_output_prints_values_only(monkeypatch, capsys):
    values = {"voltage.0.ALT.offset": 2500}
    engine = _engine(monkeypatch, capture=Mock(return_value=values))

    assert voltage_offsets_cli.cli(["--gpu", SLOT, "capture"], Mock()) == 0

    engine.capture.assert_called_once_with(SLOT)
    assert json.loads(capsys.readouterr().out) == {
        "command": "capture", "slot": SLOT, "values": values
    }


def test_capture_requires_existing_profile_parent_before_engine(monkeypatch, tmp_path):
    engine_factory = Mock(side_effect=AssertionError("invalid path must fail before engine import"))
    monkeypatch.setattr(voltage_offsets_cli, "_engine", engine_factory)

    with pytest.raises(SystemExit) as exc:
        voltage_offsets_cli.cli(
            ["--gpu", SLOT, "capture", "--out", str(tmp_path / "missing" / "profile.json")], Mock()
        )

    assert exc.value.code == 2
    engine_factory.assert_not_called()


def test_replay_refuses_profile_from_another_slot_before_engine_import(monkeypatch, tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(
        json.dumps({"schema": 1, "slot": "0000:02:00.0", "values": {"voltage.0.REL.offset": 1}}),
        encoding="utf-8",
    )
    engine_factory = Mock(side_effect=AssertionError("wrong-slot profile must fail before engine import"))
    monkeypatch.setattr(voltage_offsets_cli, "_engine", engine_factory)

    with pytest.raises(SystemExit) as exc:
        voltage_offsets_cli.cli(["--gpu", SLOT, "replay", "--profile", str(path)], Mock())

    assert exc.value.code == 2
    engine_factory.assert_not_called()


def test_engine_error_is_reported_without_traceback(monkeypatch, capsys):
    notify = Mock()
    _engine(monkeypatch, read=Mock(side_effect=RuntimeError("reported policy unavailable")))

    assert voltage_offsets_cli.cli(["--gpu", SLOT, "read"], notify) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"command": "read", "error": "reported policy unavailable"}
    notify.assert_not_called()


def test_help_explains_enable_restart_and_does_not_import_engine(monkeypatch, capsys):
    engine_factory = Mock(side_effect=AssertionError("help must not import the live engine"))
    monkeypatch.setattr(voltage_offsets_cli, "_engine", engine_factory)

    with pytest.raises(SystemExit) as exc:
        voltage_offsets_cli.cli(["--help"], Mock())

    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "--gpu SLOT" in output
    assert "GPU PnP restart" in output
    assert "Windows does not reboot" in output
    engine_factory.assert_not_called()


def test_windowed_output_uses_notifier_when_stdout_is_missing(monkeypatch):
    notify = Mock()
    result = {"slot": SLOT, "values": {}, "policy": {}, "saved_values": {}, "unsupported": []}
    engine = _engine(monkeypatch, read=Mock(return_value=result))
    monkeypatch.setattr(voltage_offsets_cli.sys, "stdout", None)

    assert voltage_offsets_cli.cli(["--gpu", SLOT, "read"], notify) == 0

    engine.read.assert_called_once_with(SLOT)
    assert json.loads(notify.call_args.args[0]) == {"command": "read", **result}


def test_windowed_help_uses_notifier_without_importing_engine(monkeypatch):
    notify = Mock()
    engine_factory = Mock(side_effect=AssertionError("help must not import the live engine"))
    monkeypatch.setattr(voltage_offsets_cli, "_engine", engine_factory)
    monkeypatch.setattr(voltage_offsets_cli.sys, "stdout", None)

    with pytest.raises(SystemExit) as exc:
        voltage_offsets_cli.cli(["--help"], notify)

    assert exc.value.code == 0
    assert any("usage: Druta --voltage-offsets" in call.args[0] for call in notify.call_args_list)
    engine_factory.assert_not_called()


def test_launch_enable_uses_hidden_selected_slot_helper(monkeypatch):
    command = ["Druta.exe", "--voltage-offsets", "--gpu", SLOT, "enable"]
    application_command = Mock(return_value=command)
    popen = Mock(return_value="child")
    monkeypatch.setattr(druta.startup, "application_command", application_command)
    monkeypatch.setattr(voltage_offsets_cli.os, "getpid", lambda: 4321)
    monkeypatch.setattr(voltage_offsets_cli.subprocess, "Popen", popen)

    assert voltage_offsets_cli.launch_enable(SLOT) == "child"

    application_command.assert_called_once_with(
        "--voltage-offsets", "--gpu", SLOT, "enable", "--parent-pid", "4321", "--reopen", windowed=True
    )
    popen.assert_called_once_with(
        command,
        close_fds=True,
        creationflags=getattr(voltage_offsets_cli.subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.mark.parametrize("helper_flags", [["--parent-pid", "4321"], ["--reopen"]])
def test_enable_requires_parent_and_reopen_flags_together(monkeypatch, helper_flags):
    engine_factory = Mock(side_effect=AssertionError("incomplete helper mode must not load engine"))
    monkeypatch.setattr(voltage_offsets_cli, "_engine", engine_factory)
    wait = Mock(side_effect=AssertionError("incomplete helper mode must not wait"))
    monkeypatch.setattr(voltage_offsets_cli, "_wait_for_parent", wait)

    with pytest.raises(SystemExit) as exc:
        voltage_offsets_cli.cli(["--gpu", SLOT, "enable", *helper_flags], Mock())

    assert exc.value.code == 2
    engine_factory.assert_not_called()
    wait.assert_not_called()


def test_helper_waits_then_enables_then_reopens(monkeypatch, capsys):
    events = []
    engine = _engine(monkeypatch, enable=Mock(side_effect=lambda slot: events.append(("enable", slot))))
    monkeypatch.setattr(voltage_offsets_cli, "_wait_for_parent", lambda pid: events.append(("wait", pid)))
    monkeypatch.setattr(voltage_offsets_cli, "_reopen_gui", lambda slot: events.append(("reopen", slot)))
    notify = Mock()

    assert voltage_offsets_cli.cli(
        ["--gpu", SLOT, "enable", "--parent-pid", "4321", "--reopen"], notify
    ) == 0

    assert events == [("wait", 4321), ("enable", SLOT), ("reopen", SLOT)]
    engine.enable.assert_called_once_with(SLOT)
    notify.assert_not_called()
    assert capsys.readouterr().out == ""


def test_helper_wait_failure_skips_activation_and_reopen(monkeypatch):
    engine_factory = Mock(side_effect=AssertionError("must not load engine before old GUI exits"))
    monkeypatch.setattr(voltage_offsets_cli, "_engine", engine_factory)
    monkeypatch.setattr(voltage_offsets_cli, "_wait_for_parent", Mock(side_effect=TimeoutError("still running")))
    reopen = Mock(side_effect=AssertionError("must not reopen before parent is confirmed exited"))
    monkeypatch.setattr(voltage_offsets_cli, "_reopen_gui", reopen)
    notify = Mock()

    assert voltage_offsets_cli.cli(
        ["--gpu", SLOT, "enable", "--parent-pid", "4321", "--reopen"], notify
    ) == 1

    engine_factory.assert_not_called()
    reopen.assert_not_called()
    assert "activation was not attempted" in notify.call_args.args[0]


def test_helper_engine_error_still_reopens_and_surfaces_error(monkeypatch, capsys):
    engine = _engine(monkeypatch, enable=Mock(side_effect=RuntimeError("activation verification failed")))
    events = []
    monkeypatch.setattr(voltage_offsets_cli, "_wait_for_parent", lambda pid: events.append(("wait", pid)))
    monkeypatch.setattr(voltage_offsets_cli, "_reopen_gui", lambda slot: events.append(("reopen", slot)))
    notify = Mock()

    assert voltage_offsets_cli.cli(
        ["--gpu", SLOT, "enable", "--parent-pid", "4321", "--reopen"], notify
    ) == 1

    assert events == [("wait", 4321), ("reopen", SLOT)]
    engine.enable.assert_called_once_with(SLOT)
    assert "activation verification failed" in notify.call_args.args[0]
    assert capsys.readouterr().out == ""
