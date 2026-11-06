"""Offline typed CLK INFO and retained request contract coverage."""

import copy
import ctypes
import struct
import threading
from types import SimpleNamespace

import pytest

from druta import clock_bios_runtime as cb


def identity(**changes):
    return {"slot": "0000:72:1d.3", "architecture": 10, "device_id": 0xABCD, "subsystem_id": 0x12345678, **changes}


def info_payload(index=1, domain=2, minimum=-200, maximum=400):
    raw = bytearray(cb.SIZE)
    raw[0], raw[8], raw[9], raw[17] = 255, 0x50, 11, 0xC7
    struct.pack_into("<I", raw, 4, (1 << index) | (1 << 30))
    at = cb.HEADER + index * cb.STRIDE
    raw[at], raw[at + 1] = 16, 16
    struct.pack_into("<I", raw, at + 4, domain)
    struct.pack_into("<hh", raw, at + 0x26, minimum, maximum)
    raw[at + 0x90] = 0xB4
    other = cb.HEADER + 30 * cb.STRIDE
    raw[other], raw[other + 1] = 77, 5
    struct.pack_into("<I", raw, other + 4, 0x80000000)
    raw[other + 383] = 0xDA
    raw[-1] = 0xE6  # Preserve absent rows too.
    return raw


def control_payload(index=1, request=0, other=900000):
    raw = bytearray(cb._CONTROL_SIZE)
    struct.pack_into("<I", raw, 0, cb._CONTROL_VERSION)
    struct.pack_into("<I", raw, 8, (1 << index) | (1 << 30))
    for i, value in ((index, request), (30, other)):
        at = 0x124 + i * 0x304
        struct.pack_into("<I", raw, at, 15)
        struct.pack_into("<i", raw, at + 0x114, value)
    raw[0x7F], raw[-1] = 0xD3, 0xE7
    return raw


def observation(index=1, domain=2, minimum=-200, maximum=400, request=0, other=900000, **ident):
    raw, control = info_payload(index, domain, minimum, maximum), control_payload(index, request, other)
    return {
        **cb._LAYOUT,
        "identity": identity(**ident),
        "info_hex": raw.hex(),
        "user_control": {**cb._CONTROL_LAYOUT, "mask": (1 << index) | (1 << 30), "payload_hex": control.hex()},
        "rows": cb._decode(raw),
        "stable_reads": 2,
        "user_control_error": None,
    }


def inspection(index=1, domain="XBAR", minimum=-200, maximum=400, **binding_changes):
    binding = {
        "schema": 1,
        "table_version": 80,
        "bios_row_type": 0x22,
        "bios_index": index,
        "bios_hal_selector": 4,
        "table_offset": 1000,
        "header_size": 13,
        "entry_size": 15,
        "entry_count": 31,
        "subentry_count": 3,
        "subentry_size": 2,
        **binding_changes,
    }
    start = (
        binding["table_offset"]
        + binding["header_size"]
        + index * (binding["entry_size"] + binding["subentry_count"] * binding["subentry_size"])
    )
    return {
        "fields": [
            {
                "key": f"clock.{index}.{role}",
                "clock_table_version": 80,
                "domain": domain,
                "clock_binding": copy.deepcopy(binding),
                "current": current,
                "units": "MHz",
                "encoding": "h",
                "width": 2,
                "scale": 1,
                "mask": None,
                "offsets": [start + offset],
                "capability": "editable",
            }
            for role, current, offset in (("minimum", minimum, 3), ("maximum", maximum, 5))
        ]
    }


def contract(monkeypatch, *, obs=None, fields=None, changes=None):
    obs = observation() if obs is None else obs
    fields = inspection() if fields is None else fields
    changes = {"clock.1.minimum": -199} if changes is None else changes
    monkeypatch.setattr(cb, "read", lambda gpu: (copy.deepcopy(obs), None))
    return cb.contracts(object(), fields, changes)


def observed(state):
    result = {**cb._LAYOUT, **copy.deepcopy(state), "stable_reads": 2, "user_control_error": None}
    result["rows"] = cb._decode(bytes.fromhex(result["info_hex"]))
    return result


def mutate_info(obs, offset, value):
    raw = bytearray.fromhex(obs["info_hex"])
    raw[offset] = value
    obs["info_hex"], obs["rows"] = raw.hex(), cb._decode(raw)


def mutate_domain(obs, index, domain):
    raw = bytearray.fromhex(obs["info_hex"])
    struct.pack_into("<I", raw, cb.HEADER + index * cb.STRIDE + 4, domain)
    obs["info_hex"], obs["rows"] = raw.hex(), cb._decode(raw)


def test_selected_narrowing_and_exact_undo_ignores_unrelated_memory_offset(monkeypatch):
    saved = contract(monkeypatch)
    cb.validate_contracts({"clock.1.minimum": -199}, saved)
    assert cb.verify(observed(saved["after"]), saved) == ["clock.1.minimum"]
    assert cb.verify(observed(saved["before"]), saved, restore=True) == ["clock.1.minimum"]
    assert saved["before"]["user_control"] == saved["after"]["user_control"]
    assert [
        i
        for i, (a, b) in enumerate(
            zip(bytes.fromhex(saved["before"]["info_hex"]), bytes.fromhex(saved["after"]["info_hex"]), strict=True)
        )
        if a != b
    ] == [cb.HEADER + cb.STRIDE + 0x26]


@pytest.mark.parametrize(
    "index,label,mask",
    [(0, "GPC", 1), (2, "DRAM", 16), (7, "SYS", 4), (9, "VID", 0x100000), (14, "domain14", 0x400000)],
)
def test_alternate_ordinals_identity_and_known_names(monkeypatch, index, label, mask):
    obs = observation(index, mask, slot="0000:03:00.1", architecture=9, device_id=1234)
    saved = contract(monkeypatch, obs=obs, fields=inspection(index, label), changes={f"clock.{index}.maximum": 500})
    assert cb.verify(observed(saved["after"]), saved) == [f"clock.{index}.maximum"]
    assert saved["before"]["identity"]["device_id"] == 1234


def test_runtime_header_hal_need_not_equal_bios_selector(monkeypatch):
    saved = contract(monkeypatch)
    assert bytes.fromhex(saved["before"]["info_hex"])[9] == 11
    assert saved["selected"]["clock.1.minimum"]["binding"]["bios_hal_selector"] == 4


def test_both_bounds_and_noop(monkeypatch):
    changes = {"clock.1.minimum": -200, "clock.1.maximum": 401}
    saved = contract(monkeypatch, changes=changes)
    cb.validate_contracts(changes, saved)
    assert set(cb.verify(observed(saved["after"]), saved)) == set(changes)


def test_unrelated_and_legacy_do_not_read(monkeypatch):
    monkeypatch.setattr(cb, "read", lambda gpu: pytest.fail("Unexpected getter"))
    assert cb.contracts(None, None, {"power.0.maximum": 999}) == {}
    fields = inspection(0, "GPC")
    for f in fields["fields"]:
        f["clock_table_version"] = 0x40
        f.pop("clock_binding")
    assert cb.contracts(None, fields, {"clock.0.minimum": -199}) == {}
    cb.validate_contracts({}, {})
    assert cb.verify(None, {}) == []


@pytest.mark.parametrize("value", [True, False, None, "1", float("nan"), float("inf"), 1.25, 32768, -32769])
def test_strict_request_values(monkeypatch, value):
    with pytest.raises(cb.ClockBiosError):
        contract(monkeypatch, changes={"clock.1.minimum": value})


@pytest.mark.parametrize(
    "mutate",
    [
        lambda x: x["identity"].update(device_id=True),
        lambda x: x["identity"].update(architecture=3),
        lambda x: x.update(parameter_bytes=12335),
        lambda x: x.update(stable_reads=True),
        lambda x: x.update(info_hex=x["info_hex"][:-2]),
        lambda x: x["rows"]["1"].update(minimum_mhz=-201),
        lambda x: mutate_info(x, 8, 0x40),
        lambda x: mutate_info(x, cb.HEADER + cb.STRIDE, 15),
        lambda x: mutate_info(x, cb.HEADER + cb.STRIDE + 1, 15),
        lambda x: mutate_info(x, cb.HEADER + cb.STRIDE + 4, 3),
        lambda x: mutate_domain(x, 30, 2),
        lambda x: x.update(user_control=None),
        lambda x: x["user_control"].update(mask=True),
        lambda x: x["user_control"].update(parameter_bytes=1),
    ],
)
def test_malformed_or_unbound_observation_rejected(monkeypatch, mutate):
    obs = observation()
    mutate(obs)
    with pytest.raises(cb.ClockBiosError):
        contract(monkeypatch, obs=obs)


@pytest.mark.parametrize(
    "field,value",
    [
        ("bios_index", 2),
        ("bios_row_type", 1),
        ("table_version", 64),
        ("schema", True),
        ("entry_count", 1),
        ("header_size", 12),
        ("entry_size", 14),
        ("subentry_size", 8),
        ("bios_hal_selector", 256),
    ],
)
def test_binding_geometry_rejected(monkeypatch, field, value):
    with pytest.raises(cb.ClockBiosError):
        contract(monkeypatch, fields=inspection(**{field: value}))


@pytest.mark.parametrize(
    "field,value",
    [
        ("offsets", [999]),
        ("units", "kHz"),
        ("encoding", "H"),
        ("scale", 1000),
        ("width", 4),
        ("mask", 1),
        ("domain", "VID"),
        ("current", -201),
        ("capability", "read-only"),
    ],
)
def test_bios_field_binding_rejected(monkeypatch, field, value):
    fields = inspection()
    fields["fields"][0][field] = value
    with pytest.raises(cb.ClockBiosError):
        contract(monkeypatch, fields=fields)


def test_missing_peer_and_factory_combination(monkeypatch):
    fields = inspection()
    fields["fields"].pop()
    with pytest.raises(cb.ClockBiosError):
        contract(monkeypatch, fields=fields)
    with pytest.raises(cb.ClockBiosError):
        contract(monkeypatch, changes={"clock.1.maximum": 500, "factory.0.offset": 10})


@pytest.mark.parametrize("requested_khz,wanted", [(-200000, -199), (400000, 399)])
def test_selected_request_must_fit_tightening(monkeypatch, requested_khz, wanted):
    role = "minimum" if requested_khz < 0 else "maximum"
    with pytest.raises(cb.ClockBiosError, match="selected retained"):
        contract(monkeypatch, obs=observation(request=requested_khz), changes={f"clock.1.{role}": wanted})


def test_unknown_selected_control_allows_expansion_only(monkeypatch):
    obs = observation()
    raw = bytearray.fromhex(obs["user_control"]["payload_hex"])
    struct.pack_into("<I", raw, 0x124 + 0x304, 77)
    obs["user_control"]["payload_hex"] = raw.hex()
    with pytest.raises(cb.ClockBiosError, match="type15"):
        contract(monkeypatch, obs=obs)
    saved = contract(monkeypatch, obs=obs, changes={"clock.1.minimum": -201})
    assert cb.verify(observed(saved["after"]), saved)


@pytest.mark.parametrize(
    "offset", [0, 9, 17, cb.HEADER + cb.STRIDE + 0x90, cb.HEADER + 30 * cb.STRIDE + 383, cb.SIZE - 1]
)
def test_full_unknown_info_peer_preservation(monkeypatch, offset):
    saved = contract(monkeypatch)
    obs = observed(saved["after"])
    mutate_info(obs, offset, bytes.fromhex(obs["info_hex"])[offset] ^ 1)
    with pytest.raises(cb.ClockBiosError):
        cb.verify(obs, saved)


def test_full_user_control_and_identity_preservation(monkeypatch):
    saved = contract(monkeypatch)
    for change in ("peer", "identity"):
        obs = observed(saved["after"])
        if change == "peer":
            raw = bytearray.fromhex(obs["user_control"]["payload_hex"])
            raw[-1] ^= 1
            obs["user_control"]["payload_hex"] = raw.hex()
        else:
            obs["identity"]["device_id"] += 1
        with pytest.raises(cb.ClockBiosError):
            cb.verify(obs, saved)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda s: s.update(schema=True),
        lambda s: s["selected"].clear(),
        lambda s: s["selected"]["clock.1.minimum"].update(after_mhz=-198),
        lambda s: s["selected"]["clock.1.minimum"].update(before_mhz=-201),
        lambda s: s["selected"]["clock.1.minimum"].update(api_domain_mask=True),
        lambda s: s["after"]["identity"].update(subsystem_id=1),
        lambda s: s["before"].update(unexpected=1),
    ],
)
def test_malformed_receipts_fail_pure_validation(monkeypatch, mutation):
    saved = contract(monkeypatch)
    mutation(saved)
    with pytest.raises(cb.ClockBiosError):
        cb.validate_contracts({"clock.1.minimum": -199}, saved)


def test_missing_changed_key_and_direction_contracts(monkeypatch):
    saved = contract(monkeypatch)
    for changes, receipt in [
        ({"clock.1.minimum": -199}, {}),
        ({"clock.1.maximum": -199}, saved),
        ({}, saved),
        ({"clock.01.minimum": -199}, saved),
        ({"clock.1.minimum": True}, saved),
    ]:
        with pytest.raises(cb.ClockBiosError):
            cb.validate_contracts(changes, receipt)


def test_restore_uses_before_and_rejects_active_state(monkeypatch):
    saved = contract(monkeypatch)
    with pytest.raises(cb.ClockBiosError):
        cb.verify(observed(saved["after"]), saved, restore=True)
    assert cb.verify(observed(saved["before"]), saved, restore=True)


class FakeGPU:
    def __init__(self, actions=(), controls=()):
        self._lock = threading.RLock()
        self.pairing_error = ""
        self.address = "0000:72:1d.3"
        self.nvapi = SimpleNamespace(
            ok=True,
            gpu=123,
            selected={"slot": self.address, "handle": 123, "devid": 0xABCD, "subsys": 0x12345678},
            _i=lambda *a: object(),
            ClkDomCtlGet=self.control_get,
        )
        self.nvml = SimpleNamespace(selected={"slot": self.address, "devid": 0xABCD, "subsys": 0x12345678})
        self._current_limit_transport = None
        self._current_limit_layout_choice = "old"
        self.actions, self.controls = list(actions), list(controls)
        self.info, self.control = info_payload(), control_payload()
        self.calls, self.control_calls, self.discoveries = [], 0, 0

    def slot(self):
        return self.address

    def arch(self):
        return 10

    def clkdom_domains(self):
        return [1, 30]

    def _current_limit_transport_layouts(self):
        self.discoveries += 1
        if self._current_limit_transport is None:
            header = [0] * 17
            header[0], header[1], header[2], header[3], header[4] = 0x4E564441, 0x10002, 84, 0x4E562A2A, 0x0500002B
            header[14], header[15] = 0x2080A612, 16
            self._current_limit_transport = (
                header,
                dict.fromkeys(("hAdapter", "hDevice", "Type", "Flags", "hContext"), 0),
            )

    def _legacy_clk_escape(self, packet, fields):
        self.calls.append((packet[14], packet[15]))
        assert self.calls[-1] == (0x20809019, 0x3030)
        ctypes.memmove(ctypes.addressof(packet) + 68, bytes(self.info), cb.SIZE)
        packet[5], packet[11], packet[16] = 1, 1, 0
        action = self.actions.pop(0) if self.actions else None
        return action(self, packet) if action else 0

    def control_get(self, handle, pointer):
        self.control_calls += 1
        assert handle == 123
        packet = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ubyte * cb._CONTROL_SIZE)).contents
        assert struct.unpack_from("<II", bytes(packet), 0)[0] == cb._CONTROL_VERSION
        ctypes.memmove(ctypes.addressof(packet), bytes(self.control), cb._CONTROL_SIZE)
        action = self.controls.pop(0) if self.controls else None
        return action(self, packet) if action else 0


def test_read_guarded_pair_and_unknown_rows():
    gpu = FakeGPU()
    obs, error = cb.read(gpu)
    assert error is None and obs["stable_reads"] == 2
    assert obs["rows"]["1"]["minimum_mhz"] == -200
    assert obs["rows"]["30"]["minimum_mhz"] is None
    assert len(gpu.calls) == gpu.control_calls == 2
    assert cb._shape(obs, writable=True)


def test_transient_info_and_control_independent_retry():
    gpu = FakeGPU(actions=[lambda g, p: 1])
    obs, error = cb.read(gpu)
    assert error is None and obs["user_control"] is not None and len(gpu.calls) == 3
    gpu = FakeGPU(controls=[lambda g, p: 1])
    obs, error = cb.read(gpu)
    assert error is None and obs["user_control"] is not None and len(gpu.calls) == 4


def test_missing_control_keeps_info_diagnostic():
    gpu = FakeGPU()
    gpu.nvapi.ClkDomCtlGet = None
    obs, error = cb.read(gpu)
    assert error is None and obs["rows"]["1"] and obs["user_control"] is None
    assert obs["user_control_error"]
    with pytest.raises(cb.ClockBiosError):
        cb._shape(obs, writable=True)


def mutate_packet(index, value):
    def mutate(gpu, packet):
        packet[index] = value
        return 0

    return mutate


@pytest.mark.parametrize(
    "action", [mutate_packet(14, 0x2080D01C), mutate_packet(15, 1), mutate_packet(5, 2), mutate_packet(16, 0x56)]
)
def test_no_retry_unsupported_or_corrupt_envelope(action):
    gpu = FakeGPU(actions=[action])
    obs, error = cb.read(gpu)
    assert obs is None and error and len(gpu.calls) == 1


def test_info_and_control_guard_corruption_stop():
    def info_overflow(gpu, packet):
        ctypes.c_ubyte.from_address(ctypes.addressof(packet) + 68 + cb.SIZE).value = 0
        return 0

    def control_overflow(gpu, packet):
        ctypes.c_ubyte.from_address(ctypes.addressof(packet) + cb._CONTROL_SIZE).value = 0
        return 0

    for gpu in (FakeGPU(actions=[info_overflow]), FakeGPU(controls=[control_overflow])):
        obs, error = cb.read(gpu)
        assert obs is None and "guarded" in error


def test_pairing_export_and_identity_failure():
    gpu = FakeGPU()
    gpu.pairing_error = "different adapters"
    assert cb.read(gpu)[0] is None and not gpu.calls
    gpu = FakeGPU()
    gpu.nvapi._i = lambda *args: None
    assert cb.read(gpu)[0] is None and not gpu.calls

    def changed(g, p):
        g.nvapi.selected["devid"] += 1
        return 0

    gpu = FakeGPU(actions=[changed])
    assert cb.read(gpu)[0] is None and len(gpu.calls) == 1


def test_transport_exception_is_bounded():
    gpu = FakeGPU()

    def fail():
        raise OSError("transient transport")

    gpu._current_limit_transport_layouts = fail
    obs, error = cb.read(gpu)
    assert obs is None and "transient" in error and not gpu.calls
