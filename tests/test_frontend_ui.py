# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""UI wiring over both real backends, with only rendering and persistence mocked."""
from contextlib import ExitStack
from unittest.mock import patch

import pytest

from druta import druta, gpu_backend, native, native_frontend
from tests.fake_driver import SECOND_SLOT, XP
from tests.frontend_rig import both, frontend
from tests.test_fake_driver import KINDS


class FirstCurveReadError(Exception):
    pass


@pytest.mark.parametrize("kind", KINDS)
def test_real_app_construction_and_backend_line(kind):
    with frontend(kind) as session, ExitStack() as stack:
        # The factory entry is real here: it must honor the existing process pin.
        app = druta.Druta(session.gpu.slot())
        assert type(app.gpu) is type(session.gpu)
        assert app.gpu.slot() == session.gpu.slot()
        assert app.gpu.available()
        app.gpu.static.get("pl_min_mw")
        for name in ("load_fonts", "build_ui", "relayout", "sync_lock_ui"):
            stack.enter_context(patch.object(app, name))
        stack.enter_context(patch.object(druta.threading, "Thread"))
        stack.enter_context(patch.object(app, "vf_read", side_effect=FirstCurveReadError))
        log = stack.enter_context(patch.object(app, "log"))
        with pytest.raises(FirstCurveReadError):
            app.run()
        lines = [call.args[0] for call in log.call_args_list]
        status = lines.index("backend: " + app.gpu.status_line())
        assert lines[status + 1] == gpu_backend.describe()
        assert lines.count(gpu_backend.describe()) == 1
        assert gpu_backend.choice().kind == kind
        assert native_frontend.drain_missing_members() == []
        assert session.driver.errors == []


@pytest.mark.parametrize("kind", KINDS)
def test_power_apply_reads_enforced_limit_and_updates_status(kind):
    def flow(session):
        app = session.app
        session.values.update(sl_pl=280.0, in_pl=280.0, power_request_status="")
        app.apply_pl(290.0)
        assert session.gpu.read_power_limit_mw() == 290000
        assert len(session.saved) == 1
        assert "Configured request: 290 W" in session.values["power_request_status"]
        assert "NVML-reported enforced limit" in session.values["power_request_status"]
        assert session.messages[-1][0]
        return dict(session.values)
    both(kind, flow)


@pytest.mark.parametrize("kind", KINDS)
def test_rail_refresh_and_first_read_reset_capture_real_fields(kind):
    def flow(session):
        app, gpu = session.app, session.gpu
        gpu.volt_limits_write_enabled = True
        session.values.update(sl_vlim_rel=0, in_vlim_rel=0, vlim_txt0="", vlim_live0="")
        app.refresh_volt_limits()
        assert session.values["sl_vlim_rel"] >= gpu.VOLT_LIMIT_MIN_MV
        assert "rel " in session.values["vlim_txt0"]
        if session.kind == "native":
            # Match the Python fake's refused VirtualProtect, before the hook
            # can be installed or any rail SET can be dispatched.
            native.hook_test_fail_protect(True)
        try:
            result = app.write_rail_limit_action(None, gpu.reset_volt_rail_limits, reset=True)
        finally:
            if session.kind == "native":
                native.hook_test_fail_protect(False)
        # The shared fake has no rail SET capture; both real writers report its
        # failed reset, while the UI must still preserve a complete Undo record.
        assert result == (False, "reset refused (NV_STATUS 0x0)")
        assert len(session.saved) == 1
        label, snapshot = session.saved[0]
        assert label == "rail-limits-reset"
        assert snapshot["rail_limits"]
        assert "continuing the explicit first-read reset" not in repr(session.messages)
        return result, dict(session.values)
    both(kind, flow)


@pytest.mark.parametrize("kind", KINDS)
def test_headroom_ui_refuses_raise_when_write_ahead_marker_fails(kind):
    def flow(session):
        app, gpu = session.app, session.gpu
        app.headroom_on, app.headroom_mv = True, 25.0
        hold = gpu.hold_headroom_ceiling_mv(gpu.read_volt_rail_limits()[0])
        app._clk_lock = {"kind": app.LOCK_VF, "verified": True, "req_mv": hold, "domain": 0}
        before = session.driver.state()
        with patch.object(druta.vfheadroom, "marker_state", return_value=("missing", None)), \
                patch.object(druta.vfheadroom, "mark_active", return_value=(False, "disk full")) as marker:
            app.sync_hold_headroom()
        assert marker.call_count > 0, "the native before_write hook was never reached"
        assert session.driver.state() == before
        assert gpu.hold_headroom_record() is None
        assert app._headroom_note[0] == "unread"
        assert "disk full" in repr(session.messages)
        return app._headroom_note, marker.call_count
    both(kind, flow)


@pytest.mark.parametrize("kind", KINDS)
def test_actual_card_switch_and_failed_switch_preserve_state(kind):
    def flow(session):
        app = session.app
        before = app.gpu
        with ExitStack() as stack:
            def open_ready(slot):
                fresh = gpu_backend.open_gpu(slot)
                # Rendering is mocked below. Request the capability snapshot its
                # widgets consume here, so lazy native and eager Python reads
                # occur at the same comparison boundary.
                fresh.static.get("pl_min_mw")
                return fresh
            stack.enter_context(patch.object(druta, "open_gpu", side_effect=open_ready))
            # Render and asynchronous tools are outside the card-switch operation.
            for name in ("build_ui", "repaint_log", "relayout", "sync_lock_ui", "vf_read", "timings_capture"):
                stack.enter_context(patch.object(app, name))
            stack.enter_context(patch.object(druta.vfheadroom, "marker_state", return_value=("missing", None)))
            assert app.swap_gpu(SECOND_SLOT)
            assert app.gpu is not before
            assert type(app.gpu) is type(before)
            assert app.gpu.static["uuid"] == XP["uuid"]
            assert app._gpu_gen == 1
            chosen = app.gpu
            app.vf_work = {1: 15000}
            assert not app.swap_gpu("0000:ff:00.0")
            assert app.gpu is chosen
            assert app.vf_work == {1: 15000}
            assert app._gpu_gen == 1
            assert not app._rebuilding
        return app.gpu.slot(), app._gpu_gen, app.vf_work
    both(kind, flow, second_card=True)
