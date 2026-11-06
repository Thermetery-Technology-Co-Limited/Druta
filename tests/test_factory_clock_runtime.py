"""Factory offset mapping and preservation independent of a board profile."""

import copy
import ctypes
import struct
import threading
from types import SimpleNamespace

import pytest

from druta import factory_clock_runtime as fc
from druta import vbios_fields
from tests import test_clock_bios_runtime as clock
from tests.test_vbios_fields import factory_fixture

cb = fc.clock


def observation(*, core=7, memory=3, xbar=12, eligible=True, **ident):
    base = clock.observation(core, 1, **ident)
    raw = bytearray.fromhex(base["info_hex"])
    for index, mask, offset in ((core, 1, 42), (memory, 16, -11), (xbar, 2, 42 if eligible else 17)):
        if index is None:
            continue
        struct.pack_into("<I", raw, 4, struct.unpack_from("<I", raw, 4)[0] | (1 << index))
        at = cb.HEADER + index * cb.STRIDE
        raw[at : at + 2] = bytes((16, 16))
        struct.pack_into("<I", raw, at + 4, mask)
        struct.pack_into("<hh", raw, at + 0x26, -100, 800)
        struct.pack_into("<i", raw, at + 0x30, offset * 1000)
        struct.pack_into("<I", raw, at + 0x3C, 64)
        raw[at + 0x40 + 24 * 6] = 1 if eligible else 2
    base.update(info_hex=raw.hex(), rows=cb._decode(raw))
    boost = bytearray(fc._BOOST_SIZE)
    struct.pack_into("<I", boost, 0, fc._BOOST_VERSION)
    boost[4:20] = ((1 << 103) - 1).to_bytes(16, "little")
    struct.pack_into("<i", boost, 68 + 36 * 60 + 20, -37500)
    boost[-1] = 0xA7
    return {
        "stable_reads": 2,
        "state": {"clock": cb._shape(base, writable=True), "boost": {"n_entries": 103, "payload_hex": boost.hex()}},
    }


def inspection(core=7, memory=3):
    fields = []
    for record, index, domain, current in ((0, core, "GPC", 42), (1, memory, "DRAM", -11)):
        cbinding = clock.inspection(index, domain)["fields"][0]["clock_binding"]
        fields.append(
            {
                "key": f"factory.{record}.offset",
                "current": current,
                "capability": "editable",
                "units": "MHz",
                "encoding": "i",
                "width": 4,
                "scale": 1000,
                "mask": None,
                "offsets": [2008 + record * 5],
                "factory_flag_offset": 2007 + record * 5,
                "factory_effective": True,
                "factory_binding": {
                    "schema": 1,
                    "table_offset": 2000,
                    "header_hex": "10070502050201",
                    "record_index": record,
                    "record_offset": 2007 + record * 5,
                    "domain": domain,
                    "clock_binding": cbinding,
                },
            }
        )
    return {"fields": fields}


def contract(monkeypatch, *, obs=None, bios=None, changes=None):
    obs = observation() if obs is None else obs
    bios = inspection() if bios is None else bios
    changes = {"factory.0.offset": 43} if changes is None else changes
    monkeypatch.setattr(fc, "read", lambda gpu: (copy.deepcopy(obs), None))
    return fc.contracts(object(), bios, changes)


def observed(state):
    return {"stable_reads": 2, "state": copy.deepcopy(state)}


def mutate_info(obs, at, value, fmt="B"):
    cs = obs["state"]["clock"]
    raw = bytearray.fromhex(cs["info_hex"])
    struct.pack_into("<" + fmt, raw, at, value)
    cs["info_hex"] = raw.hex()


@pytest.mark.parametrize("record,value", [(0, 43), (0, 0), (0, -51), (1, 0), (1, 31), (1, -500)])
def test_factory_delta_and_exact_restore(monkeypatch, record, value):
    changes = {f"factory.{record}.offset": value}
    saved = contract(monkeypatch, changes=changes)
    assert fc.verify(observed(saved["after"]), saved) == list(changes)
    assert fc.verify(observed(saved["before"]), saved, restore=True) == list(changes)
    assert saved["before"]["boost"] == saved["after"]["boost"]
    assert saved["before"]["clock"]["user_control"] == saved["after"]["clock"]["user_control"]
    expected = [7, 12] if record == 0 else [3]
    assert saved["selected"][next(iter(changes))]["indices"] == expected
    raw = bytearray.fromhex(saved["before"]["clock"]["info_hex"])
    for i in expected:
        struct.pack_into("<i", raw, cb.HEADER + i * cb.STRIDE + 0x30, value * 1000)
    assert saved["after"]["clock"]["info_hex"] == raw.hex()


@pytest.mark.parametrize("core,memory,xbar", [(0, 2, 1), (17, 26, 4), (15, 9, None)])
@pytest.mark.parametrize("eligible", [False, True])
def test_live_domain_binding_and_xbar_eligibility(monkeypatch, core, memory, xbar, eligible):
    obs = observation(core=core, memory=memory, xbar=xbar, eligible=eligible, architecture=9, device_id=0x4321)
    saved = contract(
        monkeypatch, obs=obs, bios=inspection(core, memory), changes={"factory.0.offset": -12, "factory.1.offset": 97}
    )
    assert saved["selected"]["factory.0.offset"]["indices"] == [core] + (
        [xbar] if xbar is not None and eligible else []
    )
    assert saved["selected"]["factory.1.offset"]["indices"] == [memory]
    assert fc.verify(observed(saved["after"]), saved) == ["factory.0.offset", "factory.1.offset"]


def test_inactive_type_one_subentry_does_not_trigger_copy(monkeypatch):
    obs = observation(eligible=False)
    mutate_info(obs, cb.HEADER + 12 * cb.STRIDE + 0x40, 1)
    saved = contract(monkeypatch, obs=obs)
    assert saved["selected"]["factory.0.offset"]["indices"] == [7]


@pytest.mark.parametrize("value", [True, False, None, "1", 1.5, float("nan"), float("inf"), 2147484, -2147484])
def test_strict_values(monkeypatch, value):
    with pytest.raises((fc.FactoryClockError, cb.ClockBiosError)):
        contract(monkeypatch, changes={"factory.0.offset": value})


@pytest.mark.parametrize(
    "mutation",
    [
        "type",
        "delta_type",
        "baseline",
        "duplicate_gpc",
        "duplicate_xbar",
        "xbar_type",
        "xbar_mask",
        "inventory",
        "version",
        "request_missing",
        "identity",
        "boost_mask",
        "boost_size",
    ],
)
def test_unknown_or_inconsistent_state_refuses(monkeypatch, mutation):
    obs = observation()
    gpc, xbar = cb.HEADER + 7 * cb.STRIDE, cb.HEADER + 12 * cb.STRIDE
    if mutation == "type":
        mutate_info(obs, gpc, 15)
    elif mutation == "delta_type":
        mutate_info(obs, gpc + 0x2C, 1)
    elif mutation == "baseline":
        mutate_info(obs, gpc + 0x30, 43000, "i")
    elif mutation == "duplicate_gpc":
        mutate_info(obs, xbar + 4, 1, "I")
    elif mutation == "duplicate_xbar":
        mutate_info(obs, cb.HEADER + 30 * cb.STRIDE + 4, 2, "I")
    elif mutation == "xbar_type":
        mutate_info(obs, xbar, 3)
    elif mutation == "xbar_mask":
        mutate_info(obs, xbar + 0x3C, 128, "I")
    elif mutation == "inventory":
        mutate_info(obs, 4, 1 << 3, "I")
    elif mutation == "version":
        mutate_info(obs, 8, 64)
    elif mutation == "request_missing":
        obs["state"]["clock"]["user_control"] = None
    elif mutation == "identity":
        obs["state"]["clock"]["identity"]["device_id"] = True
    elif mutation == "boost_mask":
        obs["state"]["boost"]["n_entries"] = 104
    elif mutation == "boost_size":
        obs["state"]["boost"]["payload_hex"] += "00"
    with pytest.raises((fc.FactoryClockError, cb.ClockBiosError)):
        contract(monkeypatch, obs=obs)


@pytest.mark.parametrize(
    "mutation",
    [
        "omit_xbar",
        "extra_domain",
        "opaque_info",
        "boost_request",
        "user_request",
        "request",
        "schema",
        "binding",
        "missing_selection",
        "missing_binding",
        "target",
    ],
)
def test_saved_contract_cannot_change_opaque_peers_or_omit_targets(monkeypatch, mutation):
    saved = contract(monkeypatch)
    entry = saved["selected"]["factory.0.offset"]
    if mutation == "omit_xbar":
        entry["indices"] = [7]
    elif mutation == "extra_domain":
        entry["indices"].append(3)
    elif mutation == "opaque_info":
        temp = observed(saved["after"])
        mutate_info(temp, 17, 0)
        saved["after"] = temp["state"]
    elif mutation == "boost_request":
        saved["after"]["boost"]["payload_hex"] = saved["after"]["boost"]["payload_hex"][:-2] + "00"
    elif mutation == "user_request":
        saved["after"]["clock"]["user_control"]["payload_hex"] = (
            saved["after"]["clock"]["user_control"]["payload_hex"][:-2] + "00"
        )
    elif mutation == "request":
        entry["after_mhz"] = 44
    elif mutation == "schema":
        saved["schema"] = True
    elif mutation == "binding":
        entry["binding"]["record_index"] = 1
    elif mutation == "missing_selection":
        saved["selected"] = {}
    elif mutation == "missing_binding":
        entry["binding"]["clock_binding"] = None
    elif mutation == "target":
        saved["after"]["clock"]["identity"]["device_id"] += 1
    with pytest.raises((fc.FactoryClockError, cb.ClockBiosError)):
        fc.validate_contracts({"factory.0.offset": 43}, saved)


def test_actual_bios_binding_checked_before_apply(monkeypatch):
    saved = contract(monkeypatch)
    fields = inspection()
    fields["fields"][0]["factory_binding"]["clock_binding"]["bios_hal_selector"] += 1
    with pytest.raises(fc.FactoryClockError, match="binding changed"):
        fc.validate_inspection(fields, {"factory.0.offset": 43}, saved)


def test_unrelated_and_mixed_changes(monkeypatch):
    monkeypatch.setattr(fc, "read", lambda gpu: pytest.fail("Unexpected read"))
    assert fc.contracts(None, None, {"power.0.maximum": 999}) == {}
    fc.validate_contracts({"power.0.maximum": 999}, {})
    assert fc.verify(None, {}) == []
    with pytest.raises(fc.FactoryClockError, match="separately"):
        fc.contracts(None, None, {"factory.0.offset": 45, "clock.0.minimum": -100})
    with pytest.raises(fc.FactoryClockError):
        fc.contracts(None, None, {"factory.2.offset": 45})


def test_parser_carries_raw_table_and_clock_binding():
    fields = {f["key"]: f for f in vbios_fields.inspect(factory_fixture())["fields"]}
    for record, index in ((0, 0), (1, 2)):
        binding = fields[f"factory.{record}.offset"]["factory_binding"]
        assert binding["header_hex"] == "10070502050200"
        assert binding["record_offset"] == 0x4D00 + 7 + record * 5
        assert binding["clock_binding"] == fields[f"clock.{index}.minimum"]["clock_binding"]


def getter_gpu(monkeypatch, faults=()):
    obs = observation()
    calls = []

    def getter(handle, packet):
        calls.append(len(calls))
        fault = faults[calls[-1]] if calls[-1] < len(faults) else None
        if fault == "status":
            return -1
        address = ctypes.cast(packet, ctypes.c_void_p).value
        if fault == "guard":
            ctypes.c_ubyte.from_address(address + fc._BOOST_SIZE).value = 0
        if fault == "version":
            ctypes.c_uint32.from_address(address).value = 0
        if fault == "changing":
            ctypes.c_ubyte.from_address(address + 90).value = len(calls)
        return 0

    gpu = SimpleNamespace(
        _lock=threading.RLock(),
        nvapi=SimpleNamespace(BoostTableGet=getter, gpu=123),
        vfp_layout=lambda: SimpleNamespace(n_entries=103),
    )
    monkeypatch.setattr(cb, "read", lambda gpu: (clock.observed(obs["state"]["clock"]), None))
    monkeypatch.setattr(cb, "PerfDiagnosticsReader", lambda gpu: object())
    monkeypatch.setattr(cb, "_binding", lambda reader: obs["state"]["clock"]["identity"])
    return gpu, calls


@pytest.mark.parametrize(
    "faults,success,count",
    [
        ((), True, 2),
        (("status",), True, 3),
        (("status", "status"), False, 2),
        (("guard", "guard"), False, 2),
        (("version", "version"), False, 2),
        (("changing",) * 4, False, 4),
    ],
)
def test_guarded_get_and_bounded_recovery(monkeypatch, faults, success, count):
    gpu, calls = getter_gpu(monkeypatch, faults)
    obs, error = fc.read(gpu)
    assert (obs is not None) is success and (error is None) is success
    assert len(calls) == count


def test_missing_boost_capability_does_not_call_setter(monkeypatch):
    gpu, calls = getter_gpu(monkeypatch)
    gpu.nvapi.BoostTableGet = None
    obs, error = fc.read(gpu)
    assert obs is None and "unavailable" in error and calls == []


def test_target_change_during_read_refuses(monkeypatch):
    gpu, _ = getter_gpu(monkeypatch)
    monkeypatch.setattr(cb, "_binding", lambda reader: clock.identity(device_id=1))
    obs, error = fc.read(gpu)
    assert obs is None and "target changed" in error
