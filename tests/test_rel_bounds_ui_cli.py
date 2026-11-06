"""CLI validation for the developer live REL bounds capture mode."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from druta import druta


def _no_launch(monkeypatch):
    app_factory = Mock()
    startup_factory = Mock()
    tell = Mock()
    gpu_factory = Mock(side_effect=AssertionError("GPU construction must not start during CLI validation"))
    monkeypatch.setattr(druta, "Druta", app_factory)
    monkeypatch.setattr(druta.startup, "Startup", startup_factory)
    monkeypatch.setattr(druta, "_tell", tell)
    monkeypatch.setattr(druta, "GPU", gpu_factory)
    return app_factory, startup_factory, tell, gpu_factory


def _launch_stubs(monkeypatch):
    app = SimpleNamespace(
        gpu=SimpleNamespace(available=Mock(return_value=True)),
        run=Mock(),
        shutdown_is_clean=Mock(return_value=True),
    )
    manager = SimpleNamespace(
        begin=Mock(return_value=None),
        watch_shutdown=Mock(),
        close=Mock(),
    )
    app_factory = Mock(return_value=app)
    startup_factory = Mock(return_value=manager)
    monkeypatch.setattr(druta, "Druta", app_factory)
    monkeypatch.setattr(druta.startup, "Startup", startup_factory)
    monkeypatch.setattr(druta, "_tell", Mock())
    monkeypatch.setattr(druta, "GPU", Mock(side_effect=AssertionError("test must not construct a GPU")))
    return app, manager, app_factory, startup_factory


@pytest.mark.parametrize(
    ("rail", "value_uv"),
    [
        ("-1", "100000"),
        ("2", "100000"),
        ("gpu0", "100000"),
        ("0", "0"),
        ("1", "-1"),
        ("0", "1.5"),
        ("0", "not-a-number"),
        ("1", "2147483648"),
    ],
)
def test_invalid_rail_or_microvolts_reject_before_startup_or_gpu(
    monkeypatch, tmp_path, rail, value_uv
):
    app_factory, startup_factory, tell, gpu_factory = _no_launch(monkeypatch)

    result = druta.main(
        ["--ui-capture-live-rel-bounds", rail, value_uv, str(tmp_path / "new")]
    )

    assert result == 2
    app_factory.assert_not_called()
    startup_factory.assert_not_called()
    gpu_factory.assert_not_called()
    assert tell.called


@pytest.mark.parametrize(
    "tail",
    [[], ["0"], ["0", "1045000"]],
)
def test_incomplete_live_rel_bounds_arguments_reject_before_startup(
    monkeypatch, tail
):
    app_factory, startup_factory, _tell, gpu_factory = _no_launch(monkeypatch)

    assert druta.main(["--ui-capture-live-rel-bounds", *tail]) == 2
    app_factory.assert_not_called()
    startup_factory.assert_not_called()
    gpu_factory.assert_not_called()


@pytest.mark.parametrize(
    "other_args",
    [
        ["--ui-capture", "{other}"],
        ["--ui-capture-live-vmin", "{other}"],
        ["--ui-capture-pstates-trial", "{other}"],
        ["--ui-capture-power-trial", "{other}"],
        ["--ui-capture-lpwr-trial", "gr_pg", "{other}"],
        ["--ui-capture-live-rel-bounds", "1", "1050000", "{other}"],
        ["--startup-profile"],
        ["--recovery-result", "receipt.json"],
    ],
)
@pytest.mark.parametrize("new_mode_first", [True, False])
def test_live_rel_bounds_rejects_every_other_capture_and_automatic_mode(
    monkeypatch, tmp_path, other_args, new_mode_first
):
    app_factory, startup_factory, _tell, gpu_factory = _no_launch(monkeypatch)
    own_args = [
        "--ui-capture-live-rel-bounds",
        "0",
        "1045000",
        str(tmp_path / "new"),
    ]
    expanded = [str(tmp_path / "other") if item == "{other}" else item for item in other_args]
    args = own_args + expanded if new_mode_first else expanded + own_args

    assert druta.main(args) == 2
    app_factory.assert_not_called()
    startup_factory.assert_not_called()
    gpu_factory.assert_not_called()


@pytest.mark.parametrize("destination_kind", ["directory", "file"])
def test_existing_output_path_rejects_before_startup(monkeypatch, tmp_path, destination_kind):
    app_factory, startup_factory, _tell, gpu_factory = _no_launch(monkeypatch)
    destination = tmp_path / "already-there"
    if destination_kind == "directory":
        destination.mkdir()
    else:
        destination.write_text("occupied", encoding="utf-8")

    result = druta.main(
        ["--ui-capture-live-rel-bounds", "0", "1045000", str(destination)]
    )

    assert result == 2
    app_factory.assert_not_called()
    startup_factory.assert_not_called()
    gpu_factory.assert_not_called()


def test_missing_output_parent_rejects_before_startup(monkeypatch, tmp_path):
    app_factory, startup_factory, tell, gpu_factory = _no_launch(monkeypatch)
    destination = tmp_path / "missing" / "new"

    assert druta.main(
        ["--ui-capture-live-rel-bounds", "1", "1045000", str(destination)]
    ) == 2
    app_factory.assert_not_called()
    startup_factory.assert_not_called()
    gpu_factory.assert_not_called()
    assert "parent" in tell.call_args.args[0].lower()


@pytest.mark.parametrize(
    ("rail", "value_uv"),
    [(0, 1), (1, 2147483647)],
)
def test_valid_bounds_arguments_reach_only_the_requested_live_trial(
    monkeypatch, tmp_path, rail, value_uv
):
    app, manager, app_factory, startup_factory = _launch_stubs(monkeypatch)
    destination = tmp_path / "capture with spaces"

    result = druta.main(
        ["--ui-capture-live-rel-bounds", str(rail), str(value_uv), str(destination)]
    )

    assert result == 0
    app_factory.assert_called_once_with(None)
    startup_factory.assert_called_once_with()
    assert app._ui_capture_dir == str(Path(destination).resolve())
    assert app._ui_capture_rel_bounds == (rail, value_uv)
    assert app._ui_capture_live_vmin is False
    app.run.assert_called_once_with()
    manager.close.assert_called_once_with(clean=True)


def test_ordinary_launch_does_not_activate_any_capture_trial(monkeypatch):
    app, _manager, app_factory, startup_factory = _launch_stubs(monkeypatch)

    assert druta.main([]) == 0

    app_factory.assert_called_once_with(None)
    startup_factory.assert_called_once_with()
    assert not any(name.startswith("_ui_capture") for name in vars(app))
    app.run.assert_called_once_with()
