"""CLI validation for the explicit multi-row LPWR UI capture mode."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from druta import druta

FAMILIES = ("gr_pg", "ms", "nvdec", "nvenc", "ofa", "nvjpg")


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
    ("family", "rows"),
    [
        ("unknown", "0"),
        ("ms", ""),
        ("ms", "5"),
        ("ms", "-1"),
        ("ms", "1,1"),
        ("ms", "1,x"),
        ("ms", "1,"),
        ("ms", ",1"),
    ],
)
def test_invalid_family_or_rows_reject_before_startup_or_gpu(monkeypatch, tmp_path, family, rows):
    app_factory, startup_factory, tell, gpu_factory = _no_launch(monkeypatch)

    result = druta.main(
        ["--ui-capture-lpwr-rows", family, rows, str(tmp_path / "new")]
    )

    assert result == 2
    app_factory.assert_not_called()
    startup_factory.assert_not_called()
    gpu_factory.assert_not_called()
    assert tell.called


@pytest.mark.parametrize("tail", [[], ["ms"], ["ms", "0"]])
def test_incomplete_arguments_reject_before_startup(monkeypatch, tail):
    app_factory, startup_factory, _tell, gpu_factory = _no_launch(monkeypatch)

    assert druta.main(["--ui-capture-lpwr-rows", *tail]) == 2
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

    assert druta.main(
        ["--ui-capture-lpwr-rows", "ms", "0,1", str(destination)]
    ) == 2
    app_factory.assert_not_called()
    startup_factory.assert_not_called()
    gpu_factory.assert_not_called()


def test_missing_output_parent_rejects_before_startup(monkeypatch, tmp_path):
    app_factory, startup_factory, tell, gpu_factory = _no_launch(monkeypatch)
    destination = tmp_path / "missing" / "new"

    assert druta.main(
        ["--ui-capture-lpwr-rows", "ms", "0,1", str(destination)]
    ) == 2
    app_factory.assert_not_called()
    startup_factory.assert_not_called()
    gpu_factory.assert_not_called()
    assert "parent" in tell.call_args.args[0].lower()


@pytest.mark.parametrize(
    "other_args",
    [
        ["--ui-capture", "{other}"],
        ["--ui-capture-live-vmin", "{other}"],
        ["--ui-capture-live-rel-bounds", "0", "1045000", "{other}"],
        ["--ui-capture-pstates-trial", "{other}"],
        ["--ui-capture-power-trial", "{other}"],
        ["--ui-capture-lpwr-trial", "ms", "{other}"],
        ["--ui-capture-lpwr-rows", "ms", "0", "{other}"],
        ["--startup-profile"],
        ["--recovery-result", "receipt.json"],
    ],
)
@pytest.mark.parametrize("rows_mode_first", [True, False])
def test_multirow_trial_rejects_other_capture_and_startup_modes(
    monkeypatch, tmp_path, other_args, rows_mode_first
):
    app_factory, startup_factory, _tell, gpu_factory = _no_launch(monkeypatch)
    own_args = [
        "--ui-capture-lpwr-rows",
        "ms",
        "3,1",
        str(tmp_path / "rows"),
    ]
    expanded = [str(tmp_path / "other") if item == "{other}" else item for item in other_args]
    args = own_args + expanded if rows_mode_first else expanded + own_args

    assert druta.main(args) == 2
    app_factory.assert_not_called()
    startup_factory.assert_not_called()
    gpu_factory.assert_not_called()


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("rows", ["3,1", "4,0,2"])
def test_valid_multirow_arguments_preserve_requested_order_and_reach_lpwr_trial(
    monkeypatch, tmp_path, family, rows
):
    app, manager, app_factory, startup_factory = _launch_stubs(monkeypatch)
    destination = tmp_path / "capture with spaces"
    requested_rows = [int(value) for value in rows.split(",")]

    result = druta.main(
        ["--ui-capture-lpwr-rows", family, rows, str(destination)]
    )

    assert result == 0
    app_factory.assert_called_once_with(None)
    startup_factory.assert_called_once_with()
    assert app._ui_pstates_trial_dir == str(Path(destination).resolve())
    assert app._ui_registry_trial_kind == "lpwr"
    assert app._ui_lpwr_family == family
    assert app._ui_lpwr_rows == requested_rows
    app.run.assert_called_once_with()
    manager.close.assert_called_once_with(clean=True)


@pytest.mark.parametrize("family", FAMILIES)
def test_legacy_lpwr_trial_keeps_row0_default(monkeypatch, tmp_path, family):
    app, manager, app_factory, startup_factory = _launch_stubs(monkeypatch)
    destination = tmp_path / "legacy row0"

    result = druta.main(
        ["--ui-capture-lpwr-trial", family, str(destination)]
    )

    assert result == 0
    app_factory.assert_called_once_with(None)
    startup_factory.assert_called_once_with()
    assert app._ui_pstates_trial_dir == str(Path(destination).resolve())
    assert app._ui_registry_trial_kind == "lpwr"
    assert app._ui_lpwr_family == family
    assert getattr(app, "_ui_lpwr_rows", None) is None
    app.run.assert_called_once_with()
    manager.close.assert_called_once_with(clean=True)


def test_ordinary_launch_does_not_activate_a_multirow_trial(monkeypatch):
    app, _manager, app_factory, startup_factory = _launch_stubs(monkeypatch)

    assert druta.main([]) == 0

    app_factory.assert_called_once_with(None)
    startup_factory.assert_called_once_with()
    assert not any(name.startswith("_ui_lpwr") for name in vars(app))
    app.run.assert_called_once_with()
