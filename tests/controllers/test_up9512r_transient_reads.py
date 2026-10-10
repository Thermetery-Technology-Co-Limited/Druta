# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Transient read failures get bounded read-only recovery; mismatches do not."""
from unittest.mock import patch

import pytest

from druta.controllers import up9512r as u
from tests.controllers.test_up9512r import Bus


@pytest.fixture(autouse=True)
def no_sleep():
    with patch('druta.controllers.up9512r.time.sleep'):
        yield


def controls(bus):
    return tuple(bus.regs[reg] for reg in u.CONTROL_REGISTERS)


def fail_reads(bus, count, *, armed=lambda: True):
    """Fail the next ``count`` NVAPI reads once ``armed()`` holds."""
    left = [count]

    def hook(reg, packet):
        if armed() and left[0]:
            left[0] -= 1
            return -1
    bus.read_hook = hook
    return left


def verify(rail):
    return rail.verify(acknowledged=True, operating_point=lambda: (0, 2865, 1438))


@pytest.mark.parametrize('failures', (1, u.READ_ATTEMPTS, 2 * u.READ_ATTEMPTS + 1))
def test_transient_reads_at_restore_start_still_restore_exact_entry(failures):
    bus = Bus()
    bus.response = True
    rail = u.UP9512R(bus)
    original = controls(bus)
    real = rail._transaction
    recovering = []

    def transaction(state, **kwargs):
        if kwargs.get('recovery'):
            recovering.append(True)
        return real(state, **kwargs)
    rail._transaction = transaction
    left = fail_reads(bus, failures, armed=lambda: bool(recovering))

    ok, message, ladder = verify(rail)

    assert left == [0], 'every injected failure was consumed'
    assert ok, message
    assert ladder[0]['moved']
    assert controls(bus) == original
    assert rail._verification_restore_ok


def test_persistently_unreadable_restore_says_unreadable_not_identity_change():
    bus = Bus()
    bus.response = True
    rail = u.UP9512R(bus)
    real = rail._transaction
    recovering = []

    def transaction(state, **kwargs):
        if kwargs.get('recovery'):
            recovering.append(True)
        return real(state, **kwargs)
    rail._transaction = transaction
    fail_reads(bus, 10 ** 6, armed=lambda: bool(recovering))

    ok, message, _ = verify(rail)

    assert not ok
    assert 'RESTORE FAILED' in message
    assert 'unreadable' in message or 'read failed' in message
    assert 'no longer matches' not in message
    assert not rail._verification_restore_ok
    assert len(recovering) == u.RESTORE_ATTEMPTS


def test_identity_change_during_restore_is_reported_as_a_mismatch():
    bus = Bus()
    bus.response = True
    rail = u.UP9512R(bus)
    real = rail._transaction

    def transaction(state, **kwargs):
        if kwargs.get('recovery'):
            bus.regs[0x28] = 0x2A
        return real(state, **kwargs)
    rail._transaction = transaction

    ok, message, _ = verify(rail)

    assert not ok
    assert 'identity no longer matches' in message
    assert 'unreadable' not in message
    assert bus.regs[0x2A] & 0x40, 'no restore write without the identified controller'


def test_one_failed_read_keeps_complete_telemetry_and_lock_status():
    bus = Bus()
    rail = u.UP9512R(bus)
    for reg in (0x27, 0x0A, 0x2D, 0x39):
        fired = []

        def hook(r, packet, reg=reg, fired=fired):
            if r == reg and not fired:
                fired.append(r)
                return -1
        bus.read_hook = hook
        tel = rail.telemetry()
        assert fired == [reg]
        assert tel['write_locked'] is False, reg
        assert tel['vout_mv'] == 900, reg
        assert tel['offsets_mv'] == [0] * 5, reg


def test_discovery_probe_of_an_empty_route_stays_one_read():
    bus = Bus()
    bus.read_hook = lambda reg, packet: -1
    rail = u.UP9512R(bus)
    assert not rail.present()
    assert len(bus.reads) == 1


@pytest.mark.parametrize('lock,expected', ((0x94, False), (0x87, True), (0x00, True)))
def test_write_locked_reports_the_lock_register(lock, expected):
    bus = Bus()
    bus.regs[0x39] = lock
    assert u.UP9512R(bus).write_locked() is expected
    assert not bus.writes


def test_write_locked_is_unknown_while_unreadable_and_raises_on_a_changed_identity():
    bus = Bus()
    rail = u.UP9512R(bus)
    bus.read_hook = lambda reg, packet: -1 if reg == 0x39 else None
    assert rail.write_locked() is None
    bus.read_hook = lambda reg, packet: -1 if reg == 0x27 else None
    assert rail.write_locked() is None
    bus.read_hook = None
    bus.regs[0x28] = 0x2A
    with pytest.raises(ValueError, match='identity no longer matches'):
        rail.write_locked()
    assert not bus.writes


def fail_nth_read_of(bus, reg, nth):
    seen = []

    def hook(r, packet):
        if r == reg:
            seen.append(r)
            if len(seen) == nth:
                return -1
    bus.read_hook = hook
    return seen


def test_post_identification_identity_checks_retry_one_failed_read():
    bus = Bus()
    rail = u.UP9512R(bus)
    for nth in range(1, 9):
        fail_nth_read_of(bus, 0x27, nth)
        tel = rail.telemetry()
        assert tel.get('write_locked') is False and tel.get('vout_mv') == 900, nth
    for check in (rail.still_present, rail.read_vout, rail.write_locked):
        for nth in (1, 2):
            fail_nth_read_of(bus, 0x27, nth)
            assert check() in (True, 900, False), (check, nth)
    fail_nth_read_of(bus, 0x27, 1)
    assert rail.write_locked() is False
    bus.read_hook = None
    bus.regs[0x28] = 0x2A
    assert not rail.still_present()


def test_restore_verdict_follows_readback_when_a_landed_write_reports_failure():
    bus = Bus()
    bus.response = True
    bus.regs.update({0x0A: 0x11, 0x0B: 0x11, 0x0C: 0x1B, 0x2A: 0x6C})
    rail = u.UP9512R(bus)
    original = controls(bus)
    real = rail._transaction
    recovering = []

    def transaction(state, **kwargs):
        if kwargs.get('recovery'):
            recovering.append(True)
        return real(state, **kwargs)
    rail._transaction = transaction

    def landed_but_failed(reg, value, packet):
        if recovering:
            bus.regs[reg] = value
            return -1
    bus.write_hook = landed_but_failed

    ok, message, _ = verify(rail)

    assert controls(bus) == original
    assert rail._verification_restore_ok
    assert 'RESTORE FAILED' not in message
    assert ok, message


def test_failed_trial_rollback_text_does_not_contradict_a_confirmed_restore():
    bus = Bus()
    bus.response = True
    rail = u.UP9512R(bus)
    original = controls(bus)
    writes = []

    def hook(reg, value, packet):
        writes.append((reg, value))
        if len(writes) == 1:
            bus.regs[reg] = value  # The trial byte lands but reports failure.
            return -1
        if len(writes) == 2:
            return -1  # The trial's own rollback fails without landing.
    bus.write_hook = hook

    ok, message, _ = verify(rail)

    assert not ok
    assert 'INCONCLUSIVE' in message and 'verification write refused' in message
    assert 'RESTORE FAILED' not in message and 'state uncertain' not in message
    assert controls(bus) == original
    assert rail._verification_restore_ok


def test_matches_control_is_read_only_and_requires_the_understood_layout():
    bus = Bus()
    rail = u.UP9512R(bus)
    bus.regs.update({0x0A: 0x12, 0x0B: 0x34, 0x0C: 0x5B, 0x2A: 0x6C, 0x39: 0x87})
    live = {'kind': u.KIND, 'offsets_mv': [10, 20, 30, 40, 50], 'enabled': True}
    assert rail.matches_control(live), 'a locked controller can still already match'
    assert not rail.matches_control(dict(live, enabled=False))
    assert not rail.matches_control(dict(live, offsets_mv=[10] * 5))
    assert not rail.matches_control({'kind': 'other'})
    bus.regs[0x2A] = 0xEC  # Internal-test bit: the decoded fields are not understood.
    assert not rail.matches_control(live)
    assert not bus.writes


def test_no_headroom_verify_names_reset_as_the_release_route():
    bus = Bus()
    bus.regs.update({0x0A: 0x55, 0x0B: 0x55, 0x0C: 0x5B, 0x2A: 0x6C})
    rail = u.UP9512R(bus)
    ok, message, _ = verify(rail)
    assert not ok
    assert 'nothing written' in message and 'Reset releases' in message
    assert not bus.writes
    ok, message = rail.reset(expected=rail.capture_control())
    assert ok, message
    assert controls(bus) == (0, 0, 0x0B, 0x2C)
