# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Real front-end operations must retain their behavior through the native factory.

Each native case repeats the same flow on a fresh Python card and compares returned
data, every driver call, the final device state, saved Undo data and UI messages.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from druta import (
    bios_runtime,
    gpu_backend,
    native,
    nvbackend,
    power_action_profiles,
    profiles,
    rail_limit_profiles,
    registry_overrides,
    tuning_profiles,
    voltage_rel_live,
)
from tests.fake_driver import SECOND_SLOT, TITAN, XP, card
from tests.frontend_rig import Probe, both, frontend
from tests.test_fake_driver import KINDS


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("options", ({}, {"with_nvapi": False}, {"with_nvml": False},
                                   {"absent": ("nvmlDeviceGetClockOffsets",)},
                                   {"transient": {"nvmlDeviceGetPowerManagementLimitConstraints": None}}))
def test_manual_profile_capture_and_optional_probes(kind, options):
    def capture(session):
        gpu = Probe(session.gpu)
        snapshot = profiles.capture(gpu)
        assert snapshot["device"]["slot"] == "0000:01:00.0"
        assert not any("backend does not report" in item for item in profiles.incomplete(snapshot))
        # Existing lazy-state difference: Python creates pins on its first policy
        # write and its current-limit error after a read reaches that path; native
        # exposes empty values immediately. The consumer treats missing and empty
        # identically, which the full snapshot and driver-call comparison pin.
        for lazy in ("_power_policy_pins", "_current_limit_read_error"):
            if lazy in gpu.missing:
                assert session.kind == "python"
                assert not getattr(session.gpu, lazy, None)
                gpu.missing.remove(lazy)
        return snapshot, sorted(gpu.missing)
    both(kind, capture, **options)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("control", ("memory", "boost", "domain", "rail_offset"))
def test_tuning_snapshot_preflight_change_and_restore(kind, control):
    def flow(session):
        gpu = session.gpu
        index = 0 if control == "domain" else None
        before = tuning_profiles.capture(gpu, control, index)
        assert tuning_profiles.preflight(gpu, before) is None
        writes = {"memory": lambda: gpu.set_clock_offset(2, 25),
                  "boost": lambda: gpu.set_voltage_boost(25),
                  "domain": lambda: gpu.set_clk_domain_offset(0, 30),
                  "rail_offset": lambda: gpu.set_rail_offset_mv(25, 0)}
        changed = writes[control]()
        assert changed[0], changed
        restored = tuning_profiles.restore(gpu, before)
        assert all(ok for ok, _ in restored), restored
        after = tuning_profiles.capture(gpu, control, index)
        assert after == before
        return before, changed, restored, after
    both(kind, flow)


@pytest.mark.parametrize("kind", KINDS)
def test_power_action_saves_undo_before_write_and_restores_pins(kind):
    def flow(session):
        gpu, app = session.gpu, session.app
        assert gpu.set_power_policy(4, 150000)[0]
        before = power_action_profiles.capture(gpu)
        result = app.write_tuning_action("power", "board", lambda: gpu.set_power_limit_mw(290000))
        assert result[0], result
        assert len(session.saved) == 1
        assert session.saved[0][1]["power_action"] == before
        enforced = gpu.read_enforced_power_limit_mw()
        restored = power_action_profiles.restore(gpu, before)
        assert all(ok for ok, _ in restored), restored
        assert power_action_profiles.capture(gpu) == before
        return before, result, enforced, restored
    both(kind, flow)


@pytest.mark.parametrize("kind", KINDS)
def test_failed_undo_capture_prevents_power_write(kind):
    def flow(session):
        before = session.driver.state()
        with patch.object(profiles, "autosave_state", side_effect=OSError("disk full")):
            result = session.app.write_tuning_action("power", "board",
                                                    lambda: session.gpu.set_power_limit_mw(290000))
        assert result == (False, "Could not save power Undo: disk full; nothing written")
        assert session.driver.state() == before
        return result
    both(kind, flow)


@pytest.mark.parametrize("kind", KINDS)
def test_rail_profile_rejects_300_mv_without_driver_writes(kind):
    def flow(session):
        gpu = session.gpu
        gpu.volt_limits_write_enabled = True
        state = rail_limit_profiles.capture(gpu, {0: ["reliability"]})
        current = gpu.read_volt_rail_limits()[0]
        absolute = gpu.abs_limit_mv(current, "reliability")
        state["rail_limits"]["0"]["reliability"] = round((300 - absolute + current["reliability"]) * 1000)
        before = session.driver.state()
        refusal = rail_limit_profiles.preflight(gpu, state)
        assert refusal == "rail 0 reliability is outside current voltage bounds"
        result = rail_limit_profiles.restore(gpu, state)
        assert result == [(False, "rail limits not applied: " + refusal)]
        assert session.driver.state() == before
        return refusal, result
    both(kind, flow)


@pytest.mark.parametrize("kind", KINDS)
def test_clock_and_thermal_consumers_use_companions(kind):
    def flow(session):
        clocks = voltage_rel_live._clocks(session.gpu)
        assert clocks["pstates"] is not None
        thresholds = bios_runtime.read_thermal_thresholds(session.gpu)
        return clocks, thresholds
    both(kind, flow)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("second", (False, True))
def test_registry_target_binding_keeps_selected_card(kind, second):
    def flow(session):
        gpu = session.open(SECOND_SLOT) if second else session.gpu
        gpu.static.get("pl_min_mw")  # merge the native handle's lazy capability snapshot
        identity = XP if second else TITAN
        instance = f"PCI\\VEN_10DE&DEV_{identity['devid']:04X}&SUBSYS_{identity['subsys']:08X}\\TEST"
        target = SimpleNamespace(slot=gpu.slot(), instance_id=instance)
        with patch.object(registry_overrides.devicereset, "resolve_target", return_value=target), \
                patch.object(registry_overrides, "driver_key_for_instance", return_value="driver-key"):
            binding = registry_overrides.bind_target(gpu)
        assert binding.slot == (SECOND_SLOT if second else "0000:01:00.0")
        return binding
    both(kind, flow, second_card=True)


@pytest.mark.parametrize("kind", KINDS)
def test_registry_binding_refuses_wrong_windows_identity(kind):
    def flow(session):
        target = SimpleNamespace(slot=session.gpu.slot(), instance_id="PCI\\VEN_10DE&DEV_FFFF&SUBSYS_00000000\\TEST")
        with patch.object(registry_overrides.devicereset, "resolve_target", return_value=target):
            with pytest.raises(registry_overrides.OverrideError) as caught:
                registry_overrides.bind_target(session.gpu)
        assert str(caught.value) == "Windows and NVAPI adapter identities disagree"
        return str(caught.value)
    both(kind, flow)


@pytest.mark.parametrize("kind", KINDS)
def test_reset_keeps_named_release_contract_and_discards_policy_pins(kind):
    def flow(session):
        gpu = session.gpu
        assert gpu.set_power_policy(4, 150000)[0]
        assert gpu.set_voltage_boost(25)[0]
        steps = gpu.reset_all()
        assert all(isinstance(step, nvbackend.ResetStep) for step in steps)
        assert gpu._power_policy_pins == {}
        assert gpu.read_voltage_boost() == 0
        return [(step.name, tuple(step)) for step in steps]
    both(kind, flow)


@pytest.mark.parametrize("kind", KINDS)
def test_profile_captures_msvdd_requests_written_this_session(kind):
    def flow(session):
        gpu = session.gpu
        gpu.msvdd_write_enabled = True
        result = gpu.set_rail_offset_mv(25, domain=0, rail=1)
        assert result[0], result
        written, seen = gpu.msvdd_offset_domains()
        assert 0 in written and 0 in seen
        captured = profiles.capture(gpu)
        assert captured["msvdd_offsets_mv"]["0"] == 25
        assert not any("MSVDD" in missing for missing in profiles.incomplete(captured))
        return result, written, seen, captured
    both(kind, flow)


@pytest.mark.parametrize("kind", KINDS)
def test_one_poll_iteration_publishes_real_telemetry(kind):
    def flow(session):
        app = session.app
        class OneTick:
            stopped = False
            def is_set(self):
                return self.stopped
            def wait(self, unused):
                self.stopped = True
        app._stop = OneTick()
        app.poll_loop()
        assert app._snap_err is None
        assert app._snap is not None
        # G4h-1 records this extra native diagnostic payload; the UI consumes
        # the decoded clk_domains shared by both kinds, never these raw words.
        tick = dict(app._snap)
        if session.kind == "native" and "clk_domains" in tick:
            assert "priv_clocks" in tick
        tick.pop("priv_clocks", None)
        return tick
    both(kind, flow)


@pytest.mark.parametrize("kind", KINDS)
def test_kepler_frontend_p0_presence_hold_and_release(kind):
    def flow(session):
        app, gpu = session.app, session.gpu
        assert app.legacy_p0_supported()
        held = gpu.hold_legacy_p0()
        assert held[0], held
        assert gpu.legacy_p0_owned()
        released = gpu.release_legacy_p0()
        assert released[0], released
        assert not gpu.legacy_p0_owned()
        return held, released
    both(kind, flow, architecture="kepler")


@pytest.mark.parametrize("kind", KINDS)
def test_factory_reopens_an_alternate_card_and_refuses_missing_slot(kind):
    with frontend(kind, second_card=True) as session:
        selected = gpu_backend.choice()
        second = session.open(SECOND_SLOT)
        assert type(second) is type(session.gpu)
        assert second.slot() == SECOND_SLOT
        assert second.static["uuid"] == XP["uuid"]
        missing = session.open("0000:ff:00.0")
        assert not missing.available()
        assert gpu_backend.choice() == selected
        assert session.gpu.available()
        with pytest.raises(gpu_backend.BackendMismatch):
            gpu_backend.open_gpu(kind="native" if kind == "python" else "python")


def test_fake_sessions_do_not_bypass_the_factory_surface_gate():
    if not native.available():
        pytest.skip("native core not built")
    with patch.object(gpu_backend, "surface_gate", return_value=("deliberately_missing",)):
        with pytest.raises(gpu_backend.BackendRefused, match="deliberately_missing"):
            with card("native"):
                pytest.fail("incomplete native surface was admitted")
