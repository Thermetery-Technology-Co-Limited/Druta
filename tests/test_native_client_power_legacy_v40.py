"""Native-only legacy32/V40 qualification; every driver call uses fake exports.

The measured Blackwell shape is client 1160/392, role 232, native 8632/6356
with 228/184-byte records. Firmware V40 uses strict full-state verification,
independently of that packet capacity. The deprecated Python controller is
not the oracle for this newly supported C++ combination.
"""
from contextlib import contextmanager

import pytest

from druta import client_power_runtime as cp, native
from tests.test_client_power_legacy import LegacyGpu
from tests.test_client_power_runtime import put
from tests.test_native_client_power_metadata import MetaRig, POWER_POL_INFO, legacy_accepted
from tests.test_native_identity import TITAN
from tests.test_native_target_binding import Card


class LegacyV40Gpu(LegacyGpu):
    def __init__(self, client=0, board=8):
        super().__init__(client_index=client, native_index=board, architecture=10)
        for index in (self.native_index, self.peer):
            self.native_info[88 + index * 228] = 0x40
        # Unknown fields must survive both requests and compensation.
        self.native_info[-1] = 0xA1
        self.native_control[-1] = 0xB2

    def _legacy_update(self, value):
        self.requested = value
        put(self.native_control, 20 + self.native_index * 184 + 4, value)


@contextmanager
def opened(factory=LegacyV40Gpu, configure=None, identity=None):
    rig = MetaRig(factory, configure=configure, accepted=legacy_accepted(), recapture=True)
    if identity:
        card = dict(TITAN, **identity)
        rig.card = Card(arch=10, nvml_device=card, nvapi_gpu=card)
        rig.card.nvapi.interfaces[POWER_POL_INFO] = rig.getter
    rig.fresh()
    gpu = rig.open()  # Installs the fake NVML/NVAPI/GDI resolver before open.
    try:
        rig.probe = lambda: native.hook_test_state()["patched"]
        rig.enter = native.hook_test_entry
        gpu._current_limit_transport = (list(rig.header), dict(rig.fields))
        yield gpu, rig
        assert not rig.errors
    finally:
        gpu.close()
        native.set_export_resolver(None)


@pytest.mark.parametrize("client,board,devid,subsys", [
    (0, 8, 0x2C02, 0x880E1043),
    (31, 30, 0xABCD, 0x12345678),
    (11, 4, 0xFFFF, 0x87654321),
])
def test_legacy_v40_xoc_bounds_and_exact_request_roundtrip(client, board, devid, subsys):
    with opened(lambda: LegacyV40Gpu(client, board), identity=dict(devid=devid, subsys=subsys)) as (gpu, rig):
        before = gpu._cp_read()
        assert before["identity"]["architecture"] == 10
        assert before["identity"]["device_id"] == devid
        assert before["binding"]["layout"] == "legacy32"
        assert (before["binding"]["client_index"], before["binding"]["native_index"]) == (client, board)
        assert gpu.power_limit_bounds_mw(xoc=True) == (1, 1999000)
        after = gpu._cp_set_request(1999000)["after"]
        assert after["requested_mw"] == 1999000
        expected_native = bytearray.fromhex(before["native_control_hex"])
        put(expected_native, 20 + board * 184 + 4, 1999000)
        assert bytes.fromhex(after["native_control_hex"]) == expected_native
        restored = gpu._cp_set_request(before["requested_mw"])["after"]
        assert cp._state(restored) == cp._state(before)
        assert len(rig.fake.set_payloads) == 2
        assert [(cmd, len(raw)) for cmd, raw in rig.fake.calls if cmd == cp.INFO].count((cp.INFO, 2340)) == 1


@pytest.mark.parametrize("change", ["version", "limits", "units", "missing_request"])
def test_legacy_v40_unknown_or_mismatched_definition_never_dispatches_set(change):
    def configure(fake):
        if change == "version":
            fake.native_info[88 + fake.native_index * 228] = 0x41
        elif change == "limits":
            put(fake.info, 8 + fake.client_index * 36 + 4, 200001)
        elif change == "units":
            fake.native_info[88 + fake.native_index * 228 + 6] = 1
        else:
            fake.requested = cp.MISSING
    with opened(configure=configure) as (gpu, rig):
        with pytest.raises(cp.ClientPowerError):
            gpu._cp_set_request(1999000)
        assert not rig.fake.set_payloads


def test_legacy_v40_refused_size_does_not_probe_unqualified_compact_records():
    with opened(configure=lambda f: f.faults.update({(cp.INFO, 2): {"rm": 0x1F}})) as (gpu, rig):
        with pytest.raises(cp.ClientPowerError):
            gpu._cp_set_request(1999000)
        assert [(cmd, len(raw)) for cmd, raw in rig.fake.calls] == [(cp.INFO, 2340), (cp.INFO, 1160)]
        assert not rig.fake.set_payloads


def test_legacy_v40_transient_read_does_not_select_another_size():
    faults = {(cp.INFO, 1): {"nt": -1}, (cp.INFO, 2): {"nt": -1}}
    with opened(configure=lambda f: f.faults.update(faults)) as (gpu, rig):
        with pytest.raises(cp.ClientPowerError):
            gpu._cp_set_request(1999000)
        assert [(cmd, len(raw)) for cmd, raw in rig.fake.calls] == [(cp.INFO, 2340)] * 2
        assert not rig.fake.set_payloads


def test_legacy_v40_peer_mutation_is_rejected_without_claiming_full_restoration():
    def configure(fake):
        peer_at = 20 + fake.peer * 184 + 4
        fake.faults[(cp.SET, 1)] = {"mutate": lambda f: put(f.native_control, peer_at, 60000)}
    with opened(configure=configure) as (gpu, rig):
        with pytest.raises(cp.ClientPowerError) as caught:
            gpu._cp_set_request(1999000)
        assert caught.value.kind == "readback"
        assert caught.value.rollback["attempted"]
        assert not caught.value.rollback["verified"]
        assert rig.fake.requested == 321000
        # V40 never treats a recalculated peer as an allowed change or writes
        # it through the V30 compensation path.
        rig.fake.restore_peer.assert_not_called()
        assert len(rig.fake.set_payloads) == 2


def test_legacy_v40_ignored_write_compensates_and_verifies_original_state():
    with opened(configure=lambda f: f.faults.update({(cp.SET, 1): {"ignore": True}})) as (gpu, rig):
        before = gpu._cp_read()
        with pytest.raises(cp.ClientPowerError) as caught:
            gpu._cp_set_request(1999000)
        assert caught.value.rollback["verified"]
        assert cp._state(gpu._cp_read()) == cp._state(before)
        assert len(rig.fake.set_payloads) == 2
