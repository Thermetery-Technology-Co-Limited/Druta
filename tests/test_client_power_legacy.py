"""Legacy client-power geometry, coupled policies and exact compensation."""

import ctypes
import struct
from unittest.mock import Mock

import pytest

from druta import client_power_runtime as cp
from tests.test_client_power_runtime import FakeGpu, put


class LegacyGpu(FakeGpu):
    def __init__(self, client_index=3, native_index=19, architecture=7):
        super().__init__(client_index=client_index, native_index=native_index, architecture=architecture, unrelated=False)
        self.peer = 5 if native_index != 5 else 7
        self.client_mask, self.native_mask = 1 << client_index, (1 << native_index) | (1 << self.peer)
        self.info = bytearray(cp.LEGACY_SIZES[cp.INFO])
        self.info[0] = 0xff
        put(self.info, 4, self.client_mask)
        at = 8 + client_index * 36
        self.info[at:at + 4] = b'\x57\x57\x01\x00'
        struct.pack_into('<III', self.info, at + 4, 200000, 321000, 550000)
        self.native_info = bytearray(cp.LEGACY_SIZES[cp.NATIVE_INFO])
        put(self.native_info, 4, self.native_mask)
        at = 88 + native_index * 228
        self.native_info[at:at + 7] = b'\x30\x00\x00\x00\x00\x04\x00'
        struct.pack_into('<III', self.native_info, at + 8, 200000, 321000, 550000)
        at = 88 + self.peer * 228
        self.native_info[at:at + 7] = b'\x30\x00\x00\x00\x03\x06\x00'
        struct.pack_into('<III', self.native_info, at + 8, 1000, 50000, 80000)
        self.native_control = bytearray(cp.LEGACY_SIZES[cp.NATIVE_CONTROL])
        struct.pack_into('<5I', self.native_control, 0, 0, 0, 0, 255, self.native_mask)
        put(self.native_control, 20 + self.peer * 184, 3)
        put(self.native_control, 20 + self.peer * 184 + 4, 50000)
        put(self.native_control, 20 + self.native_index * 184, 0)
        self._legacy_update(321000)
        self.restore_peer = Mock(side_effect=self._restore_peer)
        self._write_policy_limit = self.restore_peer

    def _legacy_update(self, value):
        self.requested = value
        put(self.native_control, 20 + self.native_index * 184 + 4, value)
        put(self.native_control, 20 + self.peer * 184 + 4, 50000 if value == 321000 else 60000)

    def _restore_peer(self, index, value):
        put(self.native_control, 20 + index * 184 + 4, value)
        return True, 'restored'

    def _legacy_clk_escape(self, packet, fields):
        assert self._lock.held
        command, size = int(packet[14]), int(packet[15])
        request = bytes(packet)[68:]
        self.calls.append((command, request))
        self.counts[command] += 1
        event = self.faults.get((command, self.counts[command]), {})
        if command == cp.INFO and size == cp.SIZES[cp.INFO]:
            packet[5], packet[11], packet[16] = 1, 1, event.get('rm', 0x1f)
            return event.get('nt', 0)
        assert size == cp.LEGACY_SIZES[command]
        result = bytearray(request)
        if command == cp.INFO:
            result[:] = self.info
        elif command == cp.ROLE_INFO:
            at = 88 + self.native_index * 228
            result[4:] = self.native_info[at:at + 228]
        elif command == cp.NATIVE_INFO:
            result[:] = self.native_info
        elif command == cp.NATIVE_CONTROL:
            result[:] = self.native_control
        elif command == cp.CONTROL:
            at = 8 + self.client_index * 12
            assert cp._u32(request, 4) == self.client_mask and request[at + 8] == 0xfe
            put(result, at + 4, self.requested)
        elif command == cp.SET:
            self.set_payloads.append(request)
            at = 8 + self.client_index * 12
            if not event.get('ignore'):
                self._legacy_update(cp._u32(request, at + 4))
        else:
            raise AssertionError(f'unexpected command {command:#x}')
        if event.get('mutate'):
            event['mutate'](self)
        ctypes.memmove(ctypes.addressof(packet) + 68, bytes(result), size)
        packet[5], packet[11], packet[16] = 1, 1, event.get('rm', 0)
        return event.get('nt', 0)


@pytest.mark.parametrize('architecture,client,native', [(7, 0, 2), (7, 31, 30), (7, 3, 19)])
def test_legacy_fallback_binds_real_row_indices_and_accepts_coupled_requests(architecture, client, native):
    gpu = LegacyGpu(client, native, architecture)
    controller = cp.ClientPowerController(gpu)
    before = controller.read()
    assert before['binding']['layout'] == 'legacy32'
    assert before['binding']['client_index'] == client and before['binding']['native_index'] == native
    result = controller.set_request(551000)
    assert result['after']['requested_mw'] == 551000
    assert cp._u32(bytes.fromhex(result['after']['native_control_hex']), 20 + gpu.peer * 184 + 4) == 60000
    assert len(gpu.set_payloads) == 1
    assert controller.set_request(321000)['after']['requested_mw'] == 321000
    assert cp._state(controller.read()) == cp._state(before)


def test_unexpected_opaque_native_mutation_refuses_success_and_reports_unrestored():
    gpu = LegacyGpu()
    gpu.faults[(cp.SET, 1)] = {'mutate': lambda g: g.native_control.__setitem__(-1, 1)}
    with pytest.raises(cp.ClientPowerError) as caught:
        cp.ClientPowerController(gpu).set_request(551000)
    assert caught.value.kind == 'readback'
    assert caught.value.rollback['attempted'] and not caught.value.rollback['verified']
    assert gpu.requested == 321000


def test_failed_write_restores_preexisting_peer_value():
    gpu = LegacyGpu()
    put(gpu.native_control, 20 + gpu.peer * 184 + 4, 51000)
    gpu.faults[(cp.SET, 1)] = {'rm': 0x55}
    with pytest.raises(cp.ClientPowerError) as caught:
        cp.ClientPowerController(gpu).set_request(551000)
    assert caught.value.rollback['verified']
    gpu.restore_peer.assert_called_once_with(gpu.peer, 51000)
    assert cp._u32(gpu.native_control, 20 + gpu.peer * 184 + 4) == 51000


def test_failed_peer_restoration_is_reported_without_claiming_success():
    gpu = LegacyGpu()
    put(gpu.native_control, 20 + gpu.peer * 184 + 4, 51000)
    gpu.restore_peer.side_effect = None
    gpu.restore_peer.return_value = (False, 'driver refused')
    gpu.faults[(cp.SET, 1)] = {'rm': 0x55}
    with pytest.raises(cp.ClientPowerError) as caught:
        cp.ClientPowerController(gpu).set_request(551000)
    assert not caught.value.rollback['verified']
    assert 'driver refused' in caught.value.rollback['error']
    assert gpu.requested == 321000


@pytest.mark.parametrize('mutation', ['type', 'units', 'bounds', 'missing'])
def test_unrecognized_peer_recalculation_cannot_be_accepted(mutation):
    gpu = LegacyGpu()
    info_at = 88 + gpu.peer * 228
    control_at = 20 + gpu.peer * 184
    if mutation == 'type':
        put(gpu.native_control, control_at, 9)
    elif mutation == 'units':
        gpu.native_info[info_at + 6] = 7
    elif mutation == 'bounds':
        struct.pack_into('<III', gpu.native_info, info_at + 8, 1, 100, 0)
    else:
        put(gpu.native_control, control_at + 4, cp.MISSING)
    with pytest.raises(cp.ClientPowerError) as caught:
        cp.ClientPowerController(gpu).set_request(551000)
    assert caught.value.kind == 'readback'
    assert caught.value.rollback['attempted']


def test_legacy_fallback_does_not_guess_after_transient_failure():
    gpu = LegacyGpu()
    gpu.faults[(cp.INFO, 1)] = gpu.faults[(cp.INFO, 2)] = {'nt': -1}
    with pytest.raises(cp.ClientPowerError):
        cp.ClientPowerController(gpu).read()
    assert all(len(payload) == cp.SIZES[cp.INFO] for cmd, payload in gpu.calls if cmd == cp.INFO)
    assert not gpu.set_payloads


def test_ada_does_not_enable_untested_legacy_setter():
    gpu = LegacyGpu(architecture=8)
    with pytest.raises(cp.ClientPowerError):
        cp.ClientPowerController(gpu).set_request(551000)
    assert len(gpu.calls) == 1 and len(gpu.calls[0][1]) == cp.SIZES[cp.INFO]
    assert not gpu.set_payloads
