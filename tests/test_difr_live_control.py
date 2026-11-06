import struct
import threading
from types import SimpleNamespace

import pytest

from druta import difr_live_control as live


@pytest.fixture
def device(monkeypatch):
    gpu = SimpleNamespace(_lock=threading.RLock())
    monkeypatch.setattr(live.clock, '_binding', lambda reader: 'slot')
    data = {'masks': {0x48: 8, 0x49: 16}, 'writes': [], 'predicate': 1, 'target': 'slot'}
    def get(reader, identity, command, size, request):
        if identity != data['target']:
            raise RuntimeError('target changed')
        if command == 0x20802801:
            value = struct.unpack('<I', request)[0]
            if value == 3:
                return struct.pack('<I', 1)
            data['writes'].append(value)
            data['masks'][0x49] = (data['masks'][0x49] | 1 if value == 1
                                    else data['masks'][0x49] & ~1)
            return request
        raw = bytearray(request)
        attr = struct.unpack_from('<H', raw, 8)[0]
        value = data['masks'].get(attr, data['predicate'] if attr == 0x37 else 1)
        struct.pack_into('<HI', raw, 10, 1, value)
        return bytes(raw)
    monkeypatch.setattr(live.lpwr, '_get', get)
    return live.DifrController(gpu), data


def test_balanced_request_and_observational_predicate(device):
    controller, data = device
    initial = controller.read()
    assert initial['reason_masks'] == {0x48: 8, 0x49: 16}
    assert initial['uncertain_outcome'] is False
    held = controller.hold()
    assert held['owned_request'] and held['enabled_predicate']
    assert held['reason_masks'] == {0x48: 8, 0x49: 17}
    controller.hold()
    assert data['writes'] == [1]
    assert not controller.release()['owned_request']
    assert data['writes'] == [1, 2]


def test_refuses_existing_request_and_unowned_release(device):
    controller, data = device
    data['masks'][0x48] |= 1
    with pytest.raises(live.DifrControlError):
        controller.hold()
    with pytest.raises(live.DifrControlError):
        controller.release()
    assert data['writes'] == []


def test_target_change_and_peer_change_stop_release(device):
    controller, data = device
    controller.hold()
    data['target'] = 'other'
    with pytest.raises(RuntimeError, match='target changed'):
        controller.release()
    data['target'] = 'slot'
    data['masks'][0x48] |= 2
    data['masks'][0x49] |= 4
    controller.hold()
    released = controller.release()
    assert released['reason_masks'] == {0x48: 10, 0x49: 20}
    assert data['writes'] == [1, 2]


def test_uncertain_dispatched_request_is_not_retried(device, monkeypatch):
    controller, data = device
    original = controller._control
    def fail_after_dispatch(value):
        result = original(value)
        if value == 1:
            raise RuntimeError('response lost')
        return result
    monkeypatch.setattr(controller, '_control', fail_after_dispatch)
    with pytest.raises(RuntimeError, match='response lost'):
        controller.hold()
    assert controller.read()['uncertain_outcome'] is True
    with pytest.raises(live.DifrControlError, match='uncertain'):
        controller.hold()
    with pytest.raises(live.DifrControlError, match='uncertain'):
        controller.release()
    assert data['writes'] == [1]
