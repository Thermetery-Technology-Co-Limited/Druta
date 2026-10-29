"""Framebuffer capture staging is display-only and always restores UI state."""

import json
import struct
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from druta import druta
from druta import registry_ui_trial as trial
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
        self.plot_visible = False

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

    def is_item_visible(self, item):
        assert item == "vf_plot"
        return self.plot_visible

    def get_item_configuration(self, item):
        return dict(self.config[item])

    def configure_item(self, item, **kwargs):
        self.config[item].update(kwargs)

    def get_item_pos(self, item):
        assert item != "page_control"
        return (0, 300)

    def get_item_parent(self, item):
        return 0

    def get_item_type(self, item):
        return ""

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


def live_panel(monkeypatch, *, maximum=900000, fail_apply=False, offset=0):
    from threading import RLock

    from druta import registry_panel, voltage_constants_live

    dpg, application = FakeDpg(), app()
    dpg.values.update(registry_header=False, reg_rel_live_header=False, reg_constants_live_header=False,
                      reg_rel_live_select="", reg_constants_live_select="", reg_rel_live_value="",
                      reg_constants_live_value="", reg_rel_live_status="", reg_constants_live_status="",
                      reg_constants_live_inventory="")
    for name in ("reg_constants_live_select", "reg_constants_live_value", "reg_constants_live_apply",
                 "reg_constants_live_restore"):
        dpg.config[name] = {}
    application.gpu._lock = RLock()
    application.gpu.slot = lambda: "fake"
    application.guard = lambda: True
    application.log = Mock()
    monkeypatch.setattr(registry_panel, "dpg", dpg)
    class Control:
        def __init__(self, gpu):
            self.gpu, self.value, self.calls = gpu, 800000, []
        def _capture(self):
            return {"value": self.value}
        def _summary(self, raw):
            value = raw["value"]
            return {"identity": {"slot": "fake"}, "image_sha256": "image", "unavailable_peers": [],
                    "rows": [{"rail": 0, "label": "NVVDD", "role": "VMIN", "status": "ready",
                              "value_uv": value, "reported_uv": value + offset, "stored_value_uv": 800000,
                              "maximum_uv": maximum, "equation": 9}]}
        def read(self):
            return self._summary(self._capture())
        def set(self, rail, role, value):
            self.calls.append(value)
            self.value = value
            if fail_apply and value == 805000:
                raise RuntimeError("injected apply failure")
            return self.read()
    monkeypatch.setattr(voltage_constants_live, "VoltageConstantsController", Control)
    panel = registry_panel.RegistryPanel(application)
    panel.rel_live_choices = {}
    application._registry_panel = panel
    return dpg, application, panel


def test_live_vmin_capture_applies_and_restores_through_panel(tmp_path, monkeypatch):
    from druta.ui_capture import LiveVminCapture
    dpg, application, panel = live_panel(monkeypatch)
    capture = LiveVminCapture(dpg, application, tmp_path / "new")
    run(capture, dpg)
    assert capture.manifest["state"] == "complete"
    assert panel.constants_live_control.calls == [805000, 800000]
    assert capture.manifest["restoration_verified"] is True
    assert [a["action"] for a in capture.manifest["actions"]] == ["apply", "restore"]
    assert all((tmp_path / "new" / (name + ".json")).exists() for name in ("before", "active", "restored"))


def test_live_vmin_capture_restores_after_apply_failure(tmp_path, monkeypatch):
    from druta.ui_capture import LiveVminCapture
    dpg, application, panel = live_panel(monkeypatch, fail_apply=True)
    capture = LiveVminCapture(dpg, application, tmp_path / "new")
    run(capture, dpg)
    assert capture.manifest["state"] == "failed"
    assert panel.constants_live_control.calls == [805000, 800000]
    assert capture.manifest["restoration_verified"] is True
    assert "failed" in capture.manifest["actions"][0]["status"].lower()


def test_live_vmin_capture_refuses_out_of_bounds(tmp_path, monkeypatch):
    from druta.ui_capture import LiveVminCapture
    dpg, application, panel = live_panel(monkeypatch, maximum=804999)
    capture = LiveVminCapture(dpg, application, tmp_path / "new")
    assert not capture.active and capture.manifest["state"] == "failed"
    assert panel.constants_live_control.calls == []


@pytest.mark.parametrize("other", ["--ui-capture", "--startup-profile", "--recovery-result"])
def test_live_capture_cli_rejects_other_modes(tmp_path, monkeypatch, other):
    gpu_app = Mock()
    monkeypatch.setattr(druta, "Druta", gpu_app)
    monkeypatch.setattr(druta, "_tell", Mock())
    args = ["--ui-capture-live-vmin", str(tmp_path / "new"), other]
    if other != "--startup-profile":
        args.append(str(tmp_path / "other"))
    assert druta.main(args) == 2
    gpu_app.assert_not_called()


def test_live_vmin_capture_interruption_restores_before_display_cleanup(tmp_path, monkeypatch):
    from druta.ui_capture import LiveVminCapture
    dpg, application, panel = live_panel(monkeypatch)
    capture = LiveVminCapture(dpg, application, tmp_path / "new")
    while capture.active and not capture.change_attempted:
        dpg.frame += 1
        capture.tick()
    assert panel.constants_live_control.value == 805000
    capture._finish(RuntimeError("Capture interrupted before completion"))
    assert panel.constants_live_control.calls == [805000, 800000]
    assert capture.manifest["restoration_verified"] is True
    assert capture.manifest["state"] == "failed"
    assert dpg.values["tabs"] == "tab_monitor"


def test_live_vmin_capture_reports_failed_restoration(tmp_path, monkeypatch):
    from druta.ui_capture import LiveVminCapture
    dpg, application, panel = live_panel(monkeypatch)
    capture = LiveVminCapture(dpg, application, tmp_path / "new")
    control = panel.constants_live_control
    original_set = control.set
    def failing_restore(rail, role, value):
        if value == 800000:
            control.calls.append(value)
            raise RuntimeError("injected restore failure")
        return original_set(rail, role, value)
    control.set = failing_restore
    run(capture, dpg)
    assert control.calls == [805000, 800000]
    assert capture.manifest["state"] == "failed"
    assert capture.manifest["restoration_verified"] is False
    assert any("restoration failed" in message for message in capture.manifest["errors"])


def test_live_vmin_nonzero_retained_offset_and_frame_details(tmp_path, monkeypatch):
    from druta.ui_capture import LiveVminCapture
    dpg, application, panel = live_panel(monkeypatch, offset=-10000)
    capture = LiveVminCapture(dpg, application, tmp_path / "new")
    run(capture, dpg)
    assert capture.manifest["state"] == "complete"
    assert panel.constants_live_control.calls == [805000, 800000]
    frames = {frame["stage"]: frame for frame in capture.manifest["captures"]}
    assert all("live" in frames[name] for name in ("vmin-before", "vmin-active", "vmin-restored"))
    assert frames["vmin-active"]["live"]["value"] == "805"
    assert capture.original_reported == 790000


def test_live_vmin_known_identity_change_never_writes_restore(tmp_path, monkeypatch):
    from druta.ui_capture import LiveVminCapture
    dpg, application, panel = live_panel(monkeypatch)
    capture = LiveVminCapture(dpg, application, tmp_path / "new")
    control = panel.constants_live_control
    original_summary = control._summary
    def changed_identity(raw):
        result = original_summary(raw)
        if raw["value"] == 805000:
            result["image_sha256"] = "different"
        return result
    control._summary = changed_identity
    run(capture, dpg)
    assert control.calls == [805000]
    assert capture.manifest["state"] == "failed"
    assert capture.manifest["restoration_verified"] is False


def test_live_vmin_transient_cleanup_read_uses_one_existing_restore(tmp_path, monkeypatch):
    from druta.ui_capture import LiveVminCapture
    dpg, application, panel = live_panel(monkeypatch)
    capture = LiveVminCapture(dpg, application, tmp_path / "new")
    control = panel.constants_live_control
    while capture.active and not capture.change_attempted:
        dpg.frame += 1
        capture.tick()
    original_capture = control._capture
    failures = [True]
    def transient_once():
        if failures:
            failures.pop()
            raise RuntimeError("transient read")
        return original_capture()
    control._capture = transient_once
    capture._finish(RuntimeError("Capture interrupted"))
    assert control.calls == [805000, 800000]
    assert capture.manifest["restoration_verified"] is True
    assert capture.manifest["state"] == "failed"


def setup_trial(monkeypatch):
    dpg, application = FakeDpg(), app()
    dpg.values.update(registry_header=False, reg_rel_live_header=False, reg_constants_live_header=False,
                      reg_rel_live_select="", reg_constants_live_select="", reg_status="", reg_preview="")
    target = SimpleNamespace(slot="fake", instance_id="id", driver_key="key")
    from dataclasses import dataclass
    @dataclass
    class Target:
        slot: str = "fake"
        instance_id: str = "id"
        driver_key: str = "key"
    target = Target()
    state = {"registry": {}, "enabled": False, "receipt": None}
    panel = SimpleNamespace(rel_live_choices={}, constants_live_choices={}, active=None, preview=None,
                            fields={}, inputs={})
    def refresh():
        panel.active = state["receipt"] if state["receipt"] and state["receipt"]["state"] == "active" else None
        panel.fields = {trial.FIELD: {"capability": "editable", "current": state["enabled"]}}
        panel.inputs = {trial.FIELD: (trial.TAG, 1)}
        dpg.set_value(trial.TAG, state["enabled"])
        dpg.set_value("reg_status", "read")
    def prepare():
        wanted = dpg.get_value(trial.TAG)
        panel.preview = {"path": "receipt", "target": {"slot": "fake", "instance_id": "id", "driver_key": "key"},
                         "pstate_changes": {trial.KEY: wanted},
                         "all_pstate_contracts": {trial.KEY: {"before": False, "after": wanted}},
                         "state": "prepared", "before": {}, "after": {"flag": 1}}
        state["receipt"] = panel.preview
        dpg.set_value("reg_preview", "preview")
        dpg.set_value("reg_status", "prepared")
    def apply():
        state["receipt"]["state"] = "active"
        state["registry"] = {"flag": 1}
        state["enabled"] = True
        application._closing = True
    def restore():
        state["receipt"]["state"] = "restored"
        state["registry"] = {}
        state["enabled"] = False
        application._closing = True
    panel.refresh, panel.prepare, panel.apply, panel.restore = refresh, prepare, Mock(side_effect=apply), Mock(side_effect=restore)
    application._registry_panel = panel
    monkeypatch.setattr(trial.overrides, "bind_target", lambda gpu: target)
    monkeypatch.setattr(trial.overrides, "load_receipt", lambda path: state["receipt"])
    monkeypatch.setattr(trial.overrides, "WindowsRegistry", lambda target: SimpleNamespace(snapshot=lambda: state["registry"]))
    monkeypatch.setattr(trial.all_pstates_registry, "read", lambda gpu: ({"enabled": state["enabled"]}, None))
    monkeypatch.setattr(trial.all_pstates_registry, "verify", lambda *a, **kw: [trial.KEY])
    return dpg, application, panel, state


def test_trial_normal_prepare_apply_resume_restore(monkeypatch, tmp_path):
    dpg, application, panel, state = setup_trial(monkeypatch)
    root = tmp_path / "trial"
    capture = trial.RegistryUiTrial(dpg, application, root)
    assert state["registry"] == {} and panel.apply.call_count == 0
    run(capture, dpg)
    assert state["registry"] == {"flag": 1} and panel.apply.call_count == 1
    assert capture.manifest["state"] == "handoff"
    assert application._registry_ui_trial == str(root)
    assert (root / "pstates-before.json").exists()
    application._closing = False
    application._recovery_result = {"ok": True, "reboot_required": False}
    capture = trial.RegistryUiTrial(dpg, application, root, resume=True)
    run(capture, dpg)
    assert panel.restore.call_count == 1 and state["registry"] == {}
    assert capture.manifest["state"] == "handoff"
    assert (root / "pstates-active.json").exists()
    application._closing = False
    capture = trial.RegistryUiTrial(dpg, application, root, resume=True)
    run(capture, dpg)
    assert capture.trial["phase"] == "complete"
    assert application._registry_ui_trial is None
    assert (root / "pstates-restored.json").exists()
    assert capture.trial["receipt"] == "receipt"
    assert (root / "initial" / "preview.png").exists()
    assert (root / "applying" / "active.png").exists()
    assert (root / "restoring" / "restored.png").exists()


def test_trial_changed_target_halts_before_undo(monkeypatch, tmp_path):
    dpg, application, panel, state = setup_trial(monkeypatch)
    root = tmp_path / "trial"
    capture = trial.RegistryUiTrial(dpg, application, root)
    run(capture, dpg)
    application._closing = False
    application._recovery_result = {"ok": True, "reboot_required": False}
    state["receipt"]["target"]["slot"] = "other"
    capture = trial.RegistryUiTrial(dpg, application, root, resume=True)
    assert not capture.active and capture.trial["phase"] == "attention"
    assert application._registry_ui_trial is None
    assert capture.trial["receipt"] == "receipt"
    panel.restore.assert_not_called()


@pytest.mark.parametrize("other", ["--ui-capture", "--ui-capture-live-vmin", "--startup-profile", "--recovery-result"])
def test_trial_cli_exclusivity(monkeypatch, tmp_path, other):
    gpu_app = Mock()
    monkeypatch.setattr(druta, "Druta", gpu_app)
    monkeypatch.setattr(druta, "_tell", Mock())
    args = ["--ui-capture-pstates-trial", str(tmp_path / "trial"), other]
    if other != "--startup-profile":
        args.append(str(tmp_path / "other"))
    assert druta.main(args) == 2
    gpu_app.assert_not_called()


def test_trial_apply_helper_failure_keeps_error_and_uses_panel_undo(monkeypatch, tmp_path):
    dpg, application, panel, state = setup_trial(monkeypatch)
    root = tmp_path / "trial"
    capture = trial.RegistryUiTrial(dpg, application, root)
    run(capture, dpg)
    application._closing = False
    application._recovery_result = {"ok": False, "reboot_required": False, "message": "injected failure"}
    capture = trial.RegistryUiTrial(dpg, application, root, resume=True)
    run(capture, dpg)
    assert panel.restore.call_count == 1 and panel.apply.call_count == 1
    application._closing = False
    application._recovery_result = {"ok": True, "reboot_required": False}
    capture = trial.RegistryUiTrial(dpg, application, root, resume=True)
    run(capture, dpg)
    assert capture.trial["phase"] == "attention"
    assert "injected failure" in capture.trial["error"]
    assert capture.trial["receipt"] == "receipt" and state["registry"] == {}


def test_registry_helper_carries_trial_argument_only_when_requested(monkeypatch):
    from druta import registry_overrides
    launch = Mock()
    monkeypatch.setattr(registry_overrides.subprocess, "Popen", launch)
    monkeypatch.setattr(registry_overrides.startup, "application_command", lambda *a, **kw: list(a))
    registry_overrides.launch_helper({"path": "receipt"}, ui_trial="trial-dir")
    command = launch.call_args.args[0]
    assert command[-2:] == ["--ui-pstates-trial-resume", "trial-dir"]
    assert command[command.index("--action") + 1] == "apply"
    registry_overrides.launch_helper({"path": "receipt"}, restore=True)
    command = launch.call_args.args[0]
    assert "--ui-pstates-trial-resume" not in command
    assert command[command.index("--action") + 1] == "restore"


def test_registry_helper_relaunch_carries_trial_and_recovery(monkeypatch, tmp_path):
    from druta import registry_overrides
    monkeypatch.setattr(registry_overrides, "load_receipt", lambda path: {"target": {"slot": "fake"}})
    monkeypatch.setattr(registry_overrides, "execute", lambda receipt, restore: {
        "target": {"slot": "fake"}, "ok": True, "reboot_required": False,
        "path": "receipt", "message": "verified"})
    monkeypatch.setattr(registry_overrides.startup, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(registry_overrides.startup, "application_command", lambda *a, **kw: list(a))
    launch = Mock()
    monkeypatch.setattr(registry_overrides.subprocess, "Popen", launch)
    assert registry_overrides.cli(["receipt", "--action", "apply", "--ui-pstates-trial-resume", "trial-dir"], Mock()) == 0
    command = launch.call_args.args[0]
    assert command[command.index("--gpu") + 1] == "fake"
    assert "--recovery-result" in command
    assert command[command.index("--ui-pstates-trial-resume") + 1] == "trial-dir"


def test_trial_preview_verifies_unchanged_registry_and_pstates(monkeypatch, tmp_path):
    dpg, application, panel, state = setup_trial(monkeypatch)
    verify = Mock(return_value=[trial.KEY])
    monkeypatch.setattr(trial.all_pstates_registry, "verify", verify)
    root = tmp_path / "trial"
    capture = trial.RegistryUiTrial(dpg, application, root)
    while capture.active and capture.trial.get("phase") != "preview":
        dpg.frame += 1
        capture.tick()
    assert state["registry"] == {} and panel.apply.call_count == 0
    assert verify.call_args.kwargs["restore"] is True
    assert (root / "pstates-before.json").exists()
    state["registry"] = {"unexpected": 1}
    run(capture, dpg)
    assert capture.trial["phase"] == "attention"
    assert application._registry_ui_trial is None
    panel.apply.assert_not_called()


@pytest.mark.parametrize("failure", ["load", "wait"])
def test_trial_helper_reports_pre_execution_failure(monkeypatch, tmp_path, failure):
    from druta import registry_overrides

    root = tmp_path / "trial"
    root.mkdir()
    (root / "trial.json").write_text(json.dumps({"phase": "applying", "receipt": "saved", "error": None}))
    load = Mock(return_value={"target": {"slot": "fake"}})
    wait = Mock()
    if failure == "load":
        load.side_effect = RuntimeError("load failed")
    else:
        wait.side_effect = RuntimeError("wait failed")
    execute, spawn, notify = Mock(), Mock(), Mock()
    monkeypatch.setattr(registry_overrides, "load_receipt", load)
    monkeypatch.setattr(registry_overrides.devicerecovery, "wait_for_exit", wait)
    monkeypatch.setattr(registry_overrides, "execute", execute)
    monkeypatch.setattr(registry_overrides.subprocess, "Popen", spawn)
    assert registry_overrides.cli(["saved", "--parent-pid", "123", "--ui-pstates-trial-resume", str(root)], notify) == 1
    result = json.loads((root / "trial.json").read_text(encoding="utf-8-sig"))
    assert result["phase"] == "attention" and result["receipt"] == "saved"
    assert failure + " failed" in result["error"]
    execute.assert_not_called()
    spawn.assert_not_called()
    notify.assert_called_once()
    assert "no second application opened" in notify.call_args.args[0]


def test_trial_helper_relaunch_error_preserves_first_error(monkeypatch, tmp_path):
    from druta import registry_overrides

    root = tmp_path / "trial"
    root.mkdir()
    (root / "trial.json").write_text(json.dumps({"phase": "restoring", "receipt": "saved", "error": "first failure"}))
    monkeypatch.setattr(registry_overrides, "load_receipt", lambda path: {"target": {"slot": "fake"}})
    monkeypatch.setattr(registry_overrides, "execute", lambda *a, **kw: {
        "target": {"slot": "fake"}, "ok": True, "reboot_required": False, "path": "saved", "message": "verified"})
    monkeypatch.setattr(registry_overrides.startup, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(registry_overrides.startup, "application_command", lambda *a, **kw: list(a))
    monkeypatch.setattr(registry_overrides.subprocess, "Popen", Mock(side_effect=RuntimeError("reopen failed")))
    notify = Mock()
    registry_overrides.cli(["saved", "--ui-pstates-trial-resume", str(root)], notify)
    result = json.loads((root / "trial.json").read_text(encoding="utf-8-sig"))
    assert result["phase"] == "attention" and result["receipt"] == "saved"
    assert result["error"] == "first failure" and result["relaunch_error"] == "reopen failed"
    assert "reopen failed" in notify.call_args.args[0]


def test_nontrial_helper_wait_failure_keeps_existing_behavior(monkeypatch):
    from druta import registry_overrides

    monkeypatch.setattr(registry_overrides, "load_receipt", lambda path: {"target": {"slot": "fake"}})
    monkeypatch.setattr(registry_overrides.devicerecovery, "wait_for_exit", Mock(side_effect=RuntimeError("wait failed")))
    execute, spawn, notify = Mock(), Mock(), Mock()
    monkeypatch.setattr(registry_overrides, "execute", execute)
    monkeypatch.setattr(registry_overrides.subprocess, "Popen", spawn)
    assert registry_overrides.cli(["saved", "--parent-pid", "123"], notify) == 1
    execute.assert_not_called()
    spawn.assert_not_called()
    assert "original Druta process is still running" in notify.call_args.args[0]


@pytest.mark.parametrize("plot", ["absent", "hidden", "visible"])
def test_capture_waits_one_render_only_when_plot_becomes_visible(tmp_path, plot):
    dpg = FakeDpg()
    capture = UiCapture(dpg, app(), tmp_path / "new")
    if plot != "absent":
        dpg.values["vf_plot"] = None
    capture.wait = 1
    dpg.plot_visible = plot == "visible"
    dpg.frame += 1
    capture.tick()
    assert len(dpg.output_values) == (0 if plot == "visible" else 1)
    if plot == "visible":
        dpg.frame += 1
        capture.tick()
        assert len(dpg.output_values) == 1


def test_plot_visibility_is_tracked_while_framebuffer_pending(tmp_path):
    dpg = FakeDpg()
    dpg.values["vf_plot"] = None
    capture = UiCapture(dpg, app(), tmp_path / "new")
    capture.wait = 1
    dpg.frame += 1
    capture.tick()
    assert capture.pending is not None
    dpg.plot_visible = True
    dpg.frame += 1
    capture.tick()
    assert capture.plot_visible
    dpg.frame += 1
    capture.tick()
    capture.wait = 1
    dpg.frame += 1
    capture.tick()
    assert len(dpg.output_values) == 2
