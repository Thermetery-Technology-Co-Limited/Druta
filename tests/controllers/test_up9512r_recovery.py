# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""A failed recovery must not re-enable the provisional voltage request."""
from unittest.mock import patch
import threading

import pytest

from druta.controllers import up9512r as u
from tests.controllers.test_up9512r import Bus


def controls(bus):
    return tuple(bus.regs[reg] for reg in u.CONTROL_REGISTERS)


@pytest.mark.parametrize("failing_reg", (0x0A, 0x0B, 0x0C))
@pytest.mark.parametrize("accepted_before_error", (False, True))
def test_partial_recovery_never_undoes_successful_disable(failing_reg, accepted_before_error):
    bus = Bus()
    rail = u.UP9512R(bus)
    original, original_raw = rail.capture_control(), controls(bus)
    bus.regs.update({0x0A: 0x11, 0x0B: 0x11, 0x0C: 0x1B, 0x2A: 0x6C})
    fired = False

    def fail_once(reg, value, packet):
        nonlocal fired
        if reg == failing_reg and not fired:
            fired = True
            if accepted_before_error:
                bus.regs[reg] = value
                raise RuntimeError("accepted recovery byte, then transport failed")
            return -1

    bus.write_hook = fail_once
    ok, message = rail.restore_control(original, recovery=True)

    assert not ok
    assert fired
    assert bus.writes[0] == (0x2A, 0x2C)
    assert bus.regs[0x2A] == 0x2C, "Recovery must retain the successful disable"
    assert not any(reg == 0x2A and value & 0x40 for reg, value in bus.writes)
    assert bus.writes[-1][0] == failing_reg, "No rollback to the enabled trial is permitted"
    restored = controls(bus) == original_raw
    assert rail._verification_restore_ok is restored
    assert bool(rail._verification_restore_error) is not restored
    assert rail.last_transaction["residual"] == list(controls(bus))
    assert "restore" in message.lower() or "recovery" in message.lower()
    assert rail.last_transaction["error"] == message


def test_verifier_reports_residual_staged_fields_without_reenabling_trial():
    bus = Bus()
    bus.response = True
    rail = u.UP9512R(bus)
    rail.p.rungs = [10]
    original = controls(bus)

    def fail_offset_restore(reg, value, packet):
        if reg == 0x0A and value == original[0]:
            return -1

    bus.write_hook = fail_offset_restore
    with patch("druta.controllers.up9512r.time.sleep"):
        ok, message, ladder = rail.verify(
            acknowledged=True, operating_point=lambda: (0, 3060, 1438))

    assert not ok
    assert ladder[0]["moved"]
    assert "RESTORE FAILED" in message
    assert not rail._verification_restore_ok
    assert rail._verification_restore_error
    assert controls(bus) == (0x11, 0x11, 0x1B, original[3])
    enable_writes = [(reg, value) for reg, value in bus.writes if reg == 0x2A]
    assert enable_writes == [(0x2A, 0x6C), (0x2A, 0x2C)]


def test_successful_recovery_restores_exact_control_and_preserved_bits():
    bus = Bus()
    rail = u.UP9512R(bus)
    original, original_raw = rail.capture_control(), controls(bus)
    bus.regs.update({0x0A: 0x11, 0x0B: 0x11, 0x0C: 0x1B, 0x2A: 0x6C})

    ok, message = rail.restore_control(original, recovery=True)

    assert ok, message
    assert controls(bus) == original_raw
    assert rail._verification_restore_ok
    assert not rail._verification_restore_error
    assert bus.writes == [(0x2A, 0x2C), (0x0A, 0), (0x0B, 0), (0x0C, 0x0B)]


def test_external_controlled_field_drift_is_not_overwritten_by_rollback():
    bus = Bus()
    rail = u.UP9512R(bus)

    def foreign_writer(reg, value, packet):
        bus.regs[reg] = value
        bus.regs[0x0B] = 0x22
        return 0

    bus.write_hook = foreign_writer
    ok, message = rail.set_offset_mv(10, acknowledged=True)

    assert not ok
    assert "outside this transaction" in message
    assert bus.writes == [(0x0A, 0x11)]
    assert controls(bus) == (0x11, 0x22, 0x0B, 0x2C)
    assert not rail._verification_restore_ok


def test_verifier_finally_does_not_overwrite_external_controlled_field():
    bus = Bus()
    bus.response = True
    rail = u.UP9512R(bus)
    rail.p.rungs = [10]
    drifted = False

    def point():
        nonlocal drifted
        if bus.regs[0x2A] & 0x40 and not drifted:
            drifted = True
            bus.regs[0x0B] = 0x22
        return (0, 3060, 1438)

    with patch("druta.controllers.up9512r.time.sleep"):
        ok, message, _ = rail.verify(acknowledged=True, operating_point=point)

    assert not ok
    assert drifted
    assert "RESTORE FAILED" in message
    assert "outside this transaction" in message
    assert bus.writes == [(0x0A, 0x11), (0x0B, 0x11), (0x0C, 0x1B), (0x2A, 0x6C)]
    assert controls(bus) == (0x11, 0x22, 0x1B, 0x6C)
    assert not rail._verification_restore_ok


def test_two_controllers_on_same_nvapi_serialize_complete_transactions():
    bus = Bus()
    first, second = u.UP9512R(bus), u.UP9512R(bus)
    started, finished = threading.Event(), threading.Event()
    result = []

    def apply_from_second():
        started.set()
        result.append(second.set_offset_mv(10, acknowledged=True))
        finished.set()

    worker = threading.Thread(target=apply_from_second)
    with first._mutex:
        worker.start()
        assert started.wait(1)
        assert not finished.wait(0.05)
        assert not bus.writes
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert finished.is_set()
    assert result[0][0], result[0][1]
    assert controls(bus) == (0x11, 0x11, 0x1B, 0x6C)
