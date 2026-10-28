"""Framebuffer capture staging is display-only and always restores UI state."""

import json
import struct
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from druta import druta
from druta.ui_capture import UiCapture


class FakeDpg:
    def __init__(self):
        self.values = {"tabs": "tab_monitor", "sl_pl": 300.0, "in_pl": 300.0, "ctl_clocks": "Original clocks"}
        self.config = {"sl_pl": {"min_value": 100.0, "max_value": 400.0}}
        self.scroll = 23.0
        self.frame = 0
        self.fail_output = False
        self.callbacks = []
        self.scrolls = []
        self.output_values = []
        self.output_viewports = []

    def get_value(self, item):
        return self.values[item]

    def set_value(self, item, value):
        self.values[item] = value

    def get_y_scroll(self, item):
        return self.scroll

    def set_y_scroll(self, item, value):
        self.scroll = value
        self.scrolls.append(value)

    def does_item_exist(self, item):
        return item in self.values

    def get_item_configuration(self, item):
        return dict(self.config[item])

    def configure_item(self, item, **kwargs):
        self.config[item].update(kwargs)

    def get_item_pos(self, item):
        assert item != "page_control"
        return (0, 300)

    def get_item_rect_min(self, item):
        raise KeyError("rect_min")

    def get_y_scroll_max(self, item):
        return 1000

    def get_frame_count(self):
        return self.frame

    def output_frame_buffer(self, *, file):
        self.output_values.append(dict(self.values))
        self.output_viewports.append((self.scroll, dict(self.config["sl_pl"])))
        if self.fail_output:
            raise RuntimeError("capture failed")
        Path(file).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + struct.pack(">II", 640, 480)
                               + b"\0" * 9 + b"\0\0\0\0IEND\0\0\0\0")


def run(capture, dpg):
    for _ in range(100):
        if not capture.active:
            break
        dpg.frame += 1
        capture.tick()
    assert not capture.active


def app(bounds=(100.0, 400.0)):
    return SimpleNamespace(_power_slider_style=SimpleNamespace(original_bounds=bounds),
                           _power_slider_baseline={"minimum_mw": 100000, "maximum_mw": 400000, "source": "live-bios"},
                           refresh_power_slider_style=Mock(), gpu=SimpleNamespace(set_power_limit_mw=Mock()))


def test_stages_real_and_synthetic_frames_then_restores_without_setters(tmp_path):
    dpg, application = FakeDpg(), app()
    target = tmp_path / "new"
    capture = UiCapture(dpg, application, target)
    run(capture, dpg)
    result = json.loads((target / "manifest.json").read_text())
    assert result["state"] == "complete"
    assert [x["stage"] for x in result["captures"]] == [
        "control-actual", "original-upper", "extended-in-range", "extended-above", "restored-display"
    ]
    assert [x["synthetic"] for x in result["captures"]] == [False, True, True, True, False]
    assert result["captures"][3]["widget"]["input"] > 400
    assert all(x["width"] == 640 and x["height"] == 480 and len(x["sha256"]) == 64 for x in result["captures"])
    assert dpg.values == {"tabs": "tab_monitor", "sl_pl": 300.0, "in_pl": 300.0, "ctl_clocks": "Original clocks"}
    assert dpg.config["sl_pl"] == {"min_value": 100.0, "max_value": 400.0} and dpg.scroll == 23
    assert not dpg.callbacks
    application.gpu.set_power_limit_mw.assert_not_called()


def test_unknown_bounds_skip_synthetic_and_restore(tmp_path):
    dpg, application = FakeDpg(), app(None)
    capture = UiCapture(dpg, application, tmp_path / "new")
    run(capture, dpg)
    assert [item["stage"] for item in capture.manifest["captures"]] == ["control-actual", "restored-display"]
    assert any("bounds" in message for message in capture.manifest["skipped"])
    assert dpg.values["tabs"] == "tab_monitor"
    application.gpu.set_power_limit_mw.assert_not_called()


def test_unavailable_provenance_skips_synthetic_even_with_stale_bounds(tmp_path):
    dpg, application = FakeDpg(), app()
    application._power_slider_baseline = {"source": "unavailable", "reason": "unknown"}
    capture = UiCapture(dpg, application, tmp_path / "new")
    run(capture, dpg)
    assert [item["stage"] for item in capture.manifest["captures"]] == ["control-actual", "restored-display"]
    application.gpu.set_power_limit_mw.assert_not_called()


def test_capture_failure_restores_widgets_and_reports_error(tmp_path):
    dpg, application = FakeDpg(), app()
    capture = UiCapture(dpg, application, tmp_path / "new")
    # Fail after the actual frame has been saved and a synthetic stage is active.
    while capture.stage == 0:
        dpg.frame += 1
        capture.tick()
    assert dpg.values["ctl_clocks"].startswith("Developer capture:")
    dpg.fail_output = True
    run(capture, dpg)
    assert capture.manifest["state"] == "failed"
    assert "capture failed" in capture.manifest["errors"]
    assert dpg.values == {"tabs": "tab_monitor", "sl_pl": 300.0, "in_pl": 300.0, "ctl_clocks": "Original clocks"}
    assert dpg.config["sl_pl"] == {"min_value": 100.0, "max_value": 400.0} and dpg.scroll == 23
    application.gpu.set_power_limit_mw.assert_not_called()


@pytest.mark.parametrize("extra", ["--startup-profile", "--recovery-result"])
def test_capture_rejects_automatic_apply_before_app_creation(tmp_path, monkeypatch, extra):
    gpu_app = Mock()
    monkeypatch.setattr(druta, "Druta", gpu_app)
    monkeypatch.setattr(druta, "_tell", Mock())
    args = ["--ui-capture", str(tmp_path / "new"), extra]
    if extra == "--recovery-result":
        args.append("receipt")
    assert druta.main(args) == 2
    gpu_app.assert_not_called()


def test_capture_requires_new_directory(tmp_path, monkeypatch):
    gpu_app = Mock()
    monkeypatch.setattr(druta, "Druta", gpu_app)
    monkeypatch.setattr(druta, "_tell", Mock())
    assert druta.main(["--ui-capture", str(tmp_path)]) == 2
    gpu_app.assert_not_called()


def test_capture_requires_existing_parent_before_app_creation(tmp_path, monkeypatch):
    gpu_app = Mock()
    tell = Mock()
    monkeypatch.setattr(druta, "Druta", gpu_app)
    monkeypatch.setattr(druta, "_tell", tell)
    assert druta.main(["--ui-capture", str(tmp_path / "missing" / "new")]) == 2
    assert "existing parent" in tell.call_args.args[0]
    gpu_app.assert_not_called()


def test_normal_launch_does_not_enable_capture(monkeypatch):
    application = SimpleNamespace(gpu=SimpleNamespace(available=lambda: True), run=Mock(),
                                  shutdown_is_clean=Mock(return_value=True))
    manager = SimpleNamespace(begin=Mock(return_value=None), watch_shutdown=Mock(), close=Mock())
    monkeypatch.setattr(druta.startup, "Startup", lambda: manager)
    monkeypatch.setattr(druta, "Druta", lambda slot: application)
    assert druta.main([]) == 0
    assert not hasattr(application, "_ui_capture_dir")
    application.run.assert_called_once()


def test_live_reads_selection_invalid_and_restoration_are_display_only(tmp_path, monkeypatch):
    from druta import registry_panel

    dpg, application = FakeDpg(), app()
    dpg.values.update(registry_header=False, reg_rel_live_header=False, reg_constants_live_header=True,
                      reg_rel_live_select="", reg_constants_live_select="", reg_rel_live_value="",
                      reg_constants_live_value="", reg_rel_live_status="", reg_constants_live_status="")
    monkeypatch.setattr(registry_panel, "dpg", dpg)
    panel = registry_panel.RegistryPanel(application)
    panel.rel_live_choices = {}
    panel.constants_live_choices = {}
    panel._set_rel_live = Mock()
    panel._set_constants_live = Mock()
    def rel_read():
        panel.rel_live_choices = {"rail0": {"rail": 0}}
        panel.rel_live_stale = False
    def constants_read():
        panel.constants_live_choices = {"ov": {"rail": 0, "role": "OV"},
                                        "alt": {"rail": 0, "role": "ALT"},
                                        "vmin0": {"rail": 0, "role": "VMIN"},
                                        "vmin1": {"rail": 1, "role": "VMIN"}}
        panel.constants_live_stale = False
    panel.refresh_rel_live = rel_read
    panel.refresh_constants_live = constants_read
    panel.select_rel_live = lambda: dpg.set_value("reg_rel_live_value", "1000")
    panel.select_constants_live = lambda: dpg.set_value("reg_constants_live_value", "800")
    application._registry_panel = panel
    capture = UiCapture(dpg, application, tmp_path / "new")
    run(capture, dpg)
    names = [item["stage"] for item in capture.manifest["captures"]]
    assert names[-8:] == ["live-rel-0", "live-constants-0-ov", "live-constants-0-alt",
                          "live-constants-0-vmin", "live-constants-1-vmin",
                          "live-rel-invalid", "live-constants-invalid", "restored-display"]
    first_live = names.index("live-rel-0")
    assert capture.manifest["captures"][first_live]["live"] == {
        "selected": "rail0", "value": "1000", "status": ""
    }
    invalid = capture.manifest["captures"][names.index("live-constants-invalid")]["live"]
    assert invalid["selected"] == "ov" and invalid["value"] == "not a number"
    assert invalid["status"].startswith("Enter a finite")
    assert dpg.output_values[first_live]["sl_pl"] == 300.0
    assert dpg.output_values[first_live]["in_pl"] == 300.0
    assert dpg.output_viewports[first_live][1] == {"min_value": 100.0, "max_value": 400.0}
    assert dpg.output_values[-1]["tabs"] == "tab_monitor"
    assert dpg.output_viewports[-1][0] == 23.0
    assert any(v.get("reg_rel_live_status", "").startswith("Enter a finite") for v in dpg.output_values)
    assert any(v.get("reg_constants_live_status", "").startswith("Enter a finite") for v in dpg.output_values)
    panel._set_rel_live.assert_not_called()
    panel._set_constants_live.assert_not_called()
    assert dpg.values["reg_rel_live_value"] == "1000"
    assert dpg.values["reg_constants_live_value"] == "800"
    assert [dpg.values[tag] for tag in ("registry_header", "reg_rel_live_header", "reg_constants_live_header")] == [False, False, True]
    assert any(value > 23 for value in dpg.scrolls) and dpg.scroll == 23
    assert "normal panel caches" in capture.manifest["read_cache_note"]


def test_live_missing_role_does_not_hide_ready_peer(tmp_path):
    dpg, application = FakeDpg(), app()
    dpg.values.update(registry_header=False, reg_rel_live_header=False, reg_constants_live_header=False,
                      reg_rel_live_select="", reg_constants_live_select="", reg_rel_live_value="",
                      reg_constants_live_value="", reg_rel_live_status="Unavailable", reg_constants_live_status="")
    panel = SimpleNamespace(rel_live_choices={}, constants_live_choices={"peer": {"rail": 1, "role": "VMIN"}},
                            rel_live_stale=False, constants_live_stale=False,
                            refresh_rel_live=lambda: None, refresh_constants_live=lambda: None,
                            select_constants_live=lambda: dpg.set_value("reg_constants_live_value", "800"),
                            apply_constants_live=lambda: None)
    application._registry_panel = panel
    capture = UiCapture(dpg, application, tmp_path / "new")
    run(capture, dpg)
    assert "live-constants-1-vmin" in [x["stage"] for x in capture.manifest["captures"]]
    assert any("Live OV" in x for x in capture.manifest["skipped"])
    assert any("Live rel: Unavailable" in x for x in capture.manifest["skipped"])
