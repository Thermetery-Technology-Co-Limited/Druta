"""Synthetic rated-clock requests, original-copy linkage and exact recovery."""

import copy
import ctypes
import struct
import threading
from types import SimpleNamespace

import pytest

from druta import rated_clock_runtime as rated
from tests import test_factory_clock_runtime as factory

clock = rated.clock
ERRORS = (rated.RatedClockError, rated.factory.FactoryClockError, clock.ClockBiosError)
NAMES = ("base", "peer", "boost")


def observation(
    *,
    core=7,
    memory=3,
    xbar=12,
    indices=(5, 14, 62),
    current=(2100, 2100, 2500),
    originals=(1900, 1900, 2400),
    **ident,
):
    """Build distinct current, original and preserved domain records, with opaque peers."""
    preserved = factory.observation(core=core, memory=memory, xbar=xbar, **ident)["state"]
    rows = clock._decode(bytes.fromhex(preserved["clock"]["info_hex"]))
    masks = sorted(row["api_domain_mask"] for row in rows.values())
    count = max(map(int, rows)) + 1
    info, control = bytearray(rated.INFO_SIZE), bytearray(rated.CONTROL_SIZE)
    mask = sum(1 << i for i in set(indices))
    info[0], info[36], info[39], info[-1] = 3, 0x30, 0x91, 0xA7
    info[4:36] = mask.to_bytes(32, "little")
    struct.pack_into("<I", info, 40, count)
    control[0], control[-1] = 3, 0xE3
    control[4:36] = info[4:36]
    for name, index, mhz, original in zip(NAMES, indices, current, originals, strict=True):
        info[44 + rated._SELECTORS[name][0]] = index
        ip, cp = rated.INFO_HEADER + index * rated.INFO_STRIDE, rated.CONTROL_HEADER + index * rated.CONTROL_STRIDE
        info[ip : ip + 2] = control[cp : cp + 2] = b"\x03\x03"
        info[ip + 3], control[cp + 3] = 0x52, 0x69
        for ordinal in range(count):
            freq = mhz if ordinal == core else 300 + ordinal + index
            old = original if ordinal == core else 120 + ordinal
            struct.pack_into("<HHHH", info, ip + 6 + 8 * ordinal, freq, 0x19, old, freq)
            struct.pack_into("<HH", control, cp + 6 + 4 * ordinal, freq, 0x19)
    state = {"factory": preserved, "info_hex": info.hex(), "control_hex": control.hex(), "native": {}, "public": {}}
    for name, index, mhz in zip(NAMES, indices, current, strict=True):
        _, selector, mode = rated._SELECTORS[name]
        native = bytearray(rated.QUERY_SIZE)
        struct.pack_into("<I", native, 0, selector)
        native[4], native[7], native[-1] = index, 0x91, 0xAB
        struct.pack_into("<I", native, 8, 0x10000040)
        struct.pack_into("<I", native, 16, len(masks))
        for i, domain in enumerate(masks):
            struct.pack_into("<IIII", native, 20 + 16 * i, domain, 0xB7, mhz * 1000 if domain == 1 else 47000 + i, 0xC9)
        state["native"][name] = native.hex()
        if mode is not None:
            public = bytearray(264)
            struct.pack_into("<IIII", public, 0, 0x20108, 0x100 | mode, 0x81, mhz * 1000)
            public[-1] = 0xD5
            state["public"][name] = public.hex()
    return {"stable_reads": 2, "state": state}


def inspection(core=7, base=0, boost=0):
    fields = []
    for record, current in ((2, base), (3, boost)):
        binding = copy.deepcopy(factory.inspection(core)["fields"][0]["factory_binding"])
        binding.update(record_index=record, record_offset=2007 + 5 * record)
        binding["clock_binding"]["entry_count"] = max(31, core + 1)
        fields.append(
            {
                "key": f"factory.{record}.rated",
                "current": current,
                "capability": "editable",
                "units": "MHz",
                "encoding": "I",
                "width": 4,
                "scale": 1000,
                "mask": None,
                "offsets": [2008 + 5 * record],
                "factory_flag_offset": 2007 + 5 * record,
                "factory_rated": True,
                "factory_enabled": bool(current),
                "factory_binding": binding,
            }
        )
    return {"fields": fields}


def contract(monkeypatch, *, obs=None, bios=None, changes=None):
    obs = observation() if obs is None else obs
    bios = inspection() if bios is None else bios
    changes = {"factory.2.rated": 2101} if changes is None else changes
    monkeypatch.setattr(rated, "read", lambda gpu: (copy.deepcopy(obs), None))
    return rated.contracts(object(), bios, changes)


def observed(state):
    return {"stable_reads": 2, "state": copy.deepcopy(state)}


def packed_hex(value, offset, wanted, fmt="B"):
    raw = bytearray.fromhex(value)
    struct.pack_into("<" + fmt, raw, offset, wanted)
    return raw.hex()


def expected_projection(before, affected, new, core, indices):
    """Explicit expected bytes, independent of the implementation's projection helper."""
    result = copy.deepcopy(before)
    info, control = bytearray.fromhex(result["info_hex"]), bytearray.fromhex(result["control_hex"])
    for index in affected:
        ip = 0x4C + index * 0x108 + 6 + core * 8
        struct.pack_into("<H", info, ip, new)
        struct.pack_into("<H", info, ip + 6, new)
        struct.pack_into("<H", control, 0x28 + index * 0x88 + 6 + core * 4, new)
    result.update(info_hex=info.hex(), control_hex=control.hex())
    for name, index in zip(NAMES, indices, strict=True):
        if index in affected:
            # Domain masks are numerically sorted and GPC mask 1 is first.
            result["native"][name] = packed_hex(result["native"][name], 28, new * 1000, "I")
            if name in result["public"]:
                result["public"][name] = packed_hex(result["public"][name], 12, new * 1000, "I")
    return result


@pytest.mark.parametrize(
    "record,new,affected", [(2, 2101, [5, 14]), (3, 2499, [62]), (2, 1, [5, 14]), (3, 65535, [62])]
)
def test_exact_projection_apply_and_restore(monkeypatch, record, new, affected):
    changes = {f"factory.{record}.rated": new}
    obs = observation()
    pristine = copy.deepcopy(obs)
    saved = contract(monkeypatch, obs=obs, changes=changes)
    assert obs == pristine
    assert saved["before"] == obs["state"]
    assert saved["after"] == expected_projection(saved["before"], affected, new, 7, (5, 14, 62))
    assert saved["after"]["factory"] == saved["before"]["factory"]
    assert saved["selected"][next(iter(changes))]["indices"] == affected
    rated.validate_contracts(changes, saved)
    assert rated.verify(observed(saved["after"]), saved) == list(changes)
    assert rated.verify(observed(saved["before"]), saved, restore=True) == list(changes)
    for state, restore in ((saved["before"], False), (saved["after"], True)):
        with pytest.raises(rated.RatedClockError, match="differ"):
            rated.verify(observed(state), saved, restore=restore)


@pytest.mark.parametrize(
    "core,memory,xbar,indices",
    [(0, 19, 4, (0, 127, 254)), (21, 2, 8, (201, 9, 17)), (15, 24, None, (7, 4, 230)), (31, 2, 17, (253, 8, 0))],
)
def test_current_adapter_domain_and_vpstate_indices(monkeypatch, core, memory, xbar, indices):
    obs = observation(
        core=core,
        memory=memory,
        xbar=xbar,
        indices=indices,
        architecture=9,
        device_id=0x9876,
        subsystem_id=0xCAFEBABE,
        slot="0000:95:02.1",
    )
    saved = contract(monkeypatch, obs=obs, bios=inspection(core), changes={"factory.2.rated": 2099})
    assert saved["after"] == expected_projection(saved["before"], set(indices[:2]), 2099, core, indices)
    assert saved["selected"]["factory.2.rated"]["indices"] == sorted(indices[:2])
    assert rated.verify(observed(saved["after"]), saved) == ["factory.2.rated"]


@pytest.mark.parametrize(
    "current,originals,affected",
    [
        ((2100, 1700, 2500), (1900, 1900, 2400), [5, 14]),
        ((2100, 2100, 2500), (1900, 1800, 2400), [5]),
        ((2100, 1700, 2500), (1900, 1800, 2400), [5]),
    ],
)
def test_peer_copy_uses_original_frequency_not_current_equality(monkeypatch, current, originals, affected):
    saved = contract(monkeypatch, obs=observation(current=current, originals=originals))
    assert saved["selected"]["factory.2.rated"]["indices"] == affected
    assert saved["after"] == expected_projection(saved["before"], affected, 2101, 7, (5, 14, 62))


def test_boost_can_link_peer_using_its_own_original_word(monkeypatch):
    saved = contract(monkeypatch, obs=observation(originals=(1900, 2400, 2400)), changes={"factory.3.rated": 2501})
    assert saved["selected"]["factory.3.rated"]["indices"] == [14, 62]
    assert saved["after"] == expected_projection(saved["before"], [14, 62], 2501, 7, (5, 14, 62))


def test_aliasing_selectors_updates_every_query_without_duplicate_write(monkeypatch):
    obs = observation(indices=(91, 91, 91), current=(2100,) * 3, originals=(1800,) * 3)
    saved = contract(monkeypatch, obs=obs)
    assert saved["selected"]["factory.2.rated"]["indices"] == [91]
    assert saved["after"] == expected_projection(saved["before"], [91], 2101, 7, (91,) * 3)


@pytest.mark.parametrize("base,new", [(0, 2100), (2100, 2100), (2100, 2102)])
def test_inactive_record_and_enabled_noop_are_recoverable(monkeypatch, base, new):
    saved = contract(monkeypatch, bios=inspection(base=base), changes={"factory.2.rated": float(new)})
    assert saved["selected"]["factory.2.rated"]["before_mhz"] == base
    assert type(saved["selected"]["factory.2.rated"]["after_mhz"]) is int
    assert rated.verify(observed(saved["before"]), saved, restore=True) == ["factory.2.rated"]


@pytest.mark.parametrize("wanted", [True, False, None, "2101", 0, -1, 65536, 2100.5, float("nan"), float("inf")])
def test_invalid_requested_frequency(monkeypatch, wanted):
    with pytest.raises(ERRORS):
        contract(monkeypatch, changes={"factory.2.rated": wanted})


@pytest.mark.parametrize(
    "mutation",
    [
        "info_size",
        "control_size",
        "group",
        "mask255",
        "empty",
        "layout",
        "count0",
        "count33",
        "count_below_core",
        "control_group",
        "control_mask",
        "control_selector",
        "missing_selector",
        "absent_selector",
        "info_type",
        "control_type",
        "active_pair",
        "original_copy",
        "native_selector",
        "native_index",
        "native_count",
        "native_original_flag",
        "native_domain",
        "native_frequency",
        "public_version",
        "public_mode",
        "public_present",
        "public_frequency",
        "missing_native",
        "missing_public",
        "factory_identity",
        "factory_user_request",
        "factory_boost",
        "stable_reads",
        "extra_state",
    ],
)
def test_malformed_or_incomplete_observation_refuses(monkeypatch, mutation):
    obs = observation()
    state = obs["state"]
    ip, cp = 0x4C + 5 * 0x108, 0x28 + 5 * 0x88
    info_changes = {
        "group": (0, 2, "B"),
        "mask255": (35, 128, "B"),
        "layout": (36, 0x31, "B"),
        "count0": (40, 0, "I"),
        "count33": (40, 33, "I"),
        "count_below_core": (40, 7, "I"),
        "missing_selector": (60, 255, "B"),
        "absent_selector": (60, 88, "B"),
        "info_type": (ip, 77, "B"),
        "active_pair": (ip + 6 + 7 * 8, 2101, "H"),
        "original_copy": (ip + 12 + 7 * 8, 2101, "H"),
    }
    ctl_changes = {
        "control_group": (0, 2, "B"),
        "control_mask": (4, 0, "I"),
        "control_selector": (36, 1, "B"),
        "control_type": (cp, 77, "B"),
    }
    native_changes = {
        "native_selector": (0, 0x400, "I"),
        "native_index": (4, 14, "B"),
        "native_count": (16, 3, "I"),
        "native_original_flag": (8, 0, "I"),
        "native_domain": (20, 4, "I"),
        "native_frequency": (28, 2101000, "I"),
    }
    public_changes = {
        "public_version": (0, 0x30108, "I"),
        "public_mode": (4, 2, "I"),
        "public_present": (8, 0, "I"),
        "public_frequency": (12, 2101000, "I"),
    }
    if mutation in info_changes:
        state["info_hex"] = packed_hex(state["info_hex"], *info_changes[mutation])
    elif mutation in ctl_changes:
        state["control_hex"] = packed_hex(state["control_hex"], *ctl_changes[mutation])
    elif mutation in native_changes:
        state["native"]["base"] = packed_hex(state["native"]["base"], *native_changes[mutation])
    elif mutation in public_changes:
        state["public"]["base"] = packed_hex(state["public"]["base"], *public_changes[mutation])
    elif mutation == "info_size":
        state["info_hex"] += "00"
    elif mutation == "control_size":
        state["control_hex"] = state["control_hex"][:-2]
    elif mutation == "empty":
        raw = bytearray.fromhex(state["info_hex"])
        raw[4:36] = bytes(32)
        state["info_hex"] = raw.hex()
    elif mutation == "missing_native":
        del state["native"]["peer"]
    elif mutation == "missing_public":
        del state["public"]["boost"]
    elif mutation == "factory_identity":
        state["factory"]["clock"]["identity"]["device_id"] = True
    elif mutation == "factory_user_request":
        state["factory"]["clock"]["user_control"] = None
    elif mutation == "factory_boost":
        state["factory"]["boost"]["n_entries"] += 1
    elif mutation == "stable_reads":
        obs["stable_reads"] = True
    elif mutation == "extra_state":
        state["unbound"] = 1
    with pytest.raises(ERRORS):
        contract(monkeypatch, obs=obs)


@pytest.mark.parametrize(
    "mutation",
    [
        "capability",
        "units",
        "encoding",
        "width",
        "scale",
        "mask",
        "offsets",
        "flag_offset",
        "rated",
        "enabled",
        "current",
        "binding_schema",
        "binding_record",
        "binding_offset",
        "binding_domain",
        "binding_header",
        "binding_core",
    ],
)
def test_inspection_requires_exact_bios_binding(monkeypatch, mutation):
    bios = inspection()
    field = bios["fields"][0]
    changes = {
        "capability": "readonly",
        "units": "kHz",
        "encoding": "i",
        "width": 2,
        "scale": 1,
        "mask": 1,
        "offsets": [2017],
        "flag_offset": 2018,
        "rated": False,
        "enabled": 1,
        "current": 2100,
    }
    if mutation in changes:
        name = {"flag_offset": "factory_flag_offset", "rated": "factory_rated", "enabled": "factory_enabled"}.get(
            mutation, mutation
        )
        field[name] = changes[mutation]
    else:
        binding = field["factory_binding"]
        if mutation == "binding_schema":
            binding["schema"] = True
        elif mutation == "binding_record":
            binding["record_index"] = 3
        elif mutation == "binding_offset":
            binding["record_offset"] += 1
        elif mutation == "binding_domain":
            binding["domain"] = "DRAM"
        elif mutation == "binding_header":
            binding["header_hex"] = "10070502050301"
        elif mutation == "binding_core":
            binding["clock_binding"]["bios_index"] = 3
    with pytest.raises(ERRORS):
        contract(monkeypatch, bios=bios)


def test_enabled_bios_value_must_match_loaded_query(monkeypatch):
    with pytest.raises(rated.RatedClockError, match="enabled BIOS"):
        contract(monkeypatch, bios=inspection(base=2099))


@pytest.mark.parametrize("location", ["observation", "state", "factory", "native", "public", "info_hex", "control_hex"])
@pytest.mark.parametrize("invalid", [None, [], True, "untyped"])
def test_invalid_observation_container_types(monkeypatch, location, invalid):
    obs = observation()
    if location == "observation":
        obs = invalid
    elif location == "state":
        obs["state"] = invalid
    else:
        obs["state"][location] = invalid
    # Set read directly so None remains an invalid observation, not the helper's default.
    monkeypatch.setattr(rated, "read", lambda gpu: (copy.deepcopy(obs), None))
    with pytest.raises(ERRORS):
        rated.contracts(object(), inspection(), {"factory.2.rated": 2101})


@pytest.mark.parametrize("domain", [0, 3, 16])
def test_gpc_api_mask_must_be_unique_and_one_bit(monkeypatch, domain):
    obs = observation()
    state = obs["state"]["factory"]["clock"]
    state["info_hex"] = packed_hex(state["info_hex"], clock.HEADER + 7 * clock.STRIDE + 4, domain, "I")
    with pytest.raises(ERRORS):
        contract(monkeypatch, obs=obs)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "schema",
        "selected",
        "key",
        "after_value",
        "before_bool",
        "after_bool",
        "omit_peer",
        "extra_peer",
        "bool_index",
        "binding",
        "identity",
        "opaque_info",
        "opaque_control",
        "native_peer",
        "public_peer",
        "boost_request",
        "user_request",
        "linked_word",
    ],
)
def test_malformed_receipt_cannot_bypass_peer_or_restore_verification(monkeypatch, mutation):
    saved = contract(monkeypatch)
    entry = saved["selected"]["factory.2.rated"]
    if mutation == "missing":
        del saved["before"]
    elif mutation == "schema":
        saved["schema"] = True
    elif mutation == "selected":
        saved["selected"] = {}
    elif mutation == "key":
        saved["selected"]["factory.3.rated"] = saved["selected"].pop("factory.2.rated")
    elif mutation == "after_value":
        entry["after_mhz"] += 1
    elif mutation in ("before_bool", "after_bool"):
        entry[mutation.split("_")[0] + "_mhz"] = True
    elif mutation in ("omit_peer", "extra_peer", "bool_index"):
        entry["indices"] = {"omit_peer": [5], "extra_peer": [5, 14, 62], "bool_index": [True, 14]}[mutation]
    elif mutation == "binding":
        entry["binding"]["clock_binding"]["bios_index"] = 3
    elif mutation == "identity":
        saved["after"]["factory"]["clock"]["identity"]["device_id"] += 1
    elif mutation in ("opaque_info", "opaque_control"):
        name = "info_hex" if mutation == "opaque_info" else "control_hex"
        saved["after"][name] = packed_hex(saved["after"][name], 3, 17)
    elif mutation == "native_peer":
        saved["after"]["native"]["boost"] = packed_hex(saved["after"]["native"]["boost"], 32, 77, "I")
    elif mutation == "public_peer":
        saved["after"]["public"]["boost"] = packed_hex(saved["after"]["public"]["boost"], 263, 77)
    elif mutation == "boost_request":
        saved["after"]["factory"]["boost"]["payload_hex"] = packed_hex(
            saved["after"]["factory"]["boost"]["payload_hex"], 90, 77
        )
    elif mutation == "user_request":
        ctl = saved["after"]["factory"]["clock"]["user_control"]
        ctl["payload_hex"] = packed_hex(ctl["payload_hex"], 70, 77)
    elif mutation == "linked_word":
        saved["after"]["info_hex"] = packed_hex(saved["after"]["info_hex"], 0x4C + 14 * 0x108 + 6 + 7 * 8, 2100, "H")
    with pytest.raises(ERRORS):
        rated.validate_contracts({"factory.2.rated": 2101}, saved)
    with pytest.raises(ERRORS):
        rated.verify(observation(), saved, restore=True)


@pytest.mark.parametrize("mutation", ["identity", "info", "control", "native", "public", "user", "boost"])
@pytest.mark.parametrize("restore", [False, True])
def test_readback_rejects_changed_peers_on_apply_and_restore(monkeypatch, mutation, restore):
    saved = contract(monkeypatch)
    live = observed(saved["before" if restore else "after"])
    state = live["state"]
    if mutation == "identity":
        state["factory"]["clock"]["identity"]["subsystem_id"] += 1
    elif mutation in ("info", "control"):
        state[mutation + "_hex"] = packed_hex(state[mutation + "_hex"], 3, 77)
    elif mutation in ("native", "public"):
        state[mutation]["base"] = packed_hex(state[mutation]["base"], 7, 77)
    elif mutation == "user":
        ctl = state["factory"]["clock"]["user_control"]
        ctl["payload_hex"] = packed_hex(ctl["payload_hex"], 70, 77)
    else:
        state["factory"]["boost"]["payload_hex"] = packed_hex(state["factory"]["boost"]["payload_hex"], 90, 77)
    with pytest.raises(ERRORS):
        rated.verify(live, saved, restore=restore)


def test_inspection_is_rebound_before_mutation(monkeypatch):
    saved = contract(monkeypatch)
    bios = inspection()
    rated.validate_inspection(bios, {"factory.2.rated": 2101}, saved)
    bios["fields"][0]["factory_binding"]["clock_binding"]["bios_hal_selector"] += 1
    with pytest.raises(rated.RatedClockError, match="binding changed"):
        rated.validate_inspection(bios, {"factory.2.rated": 2101}, saved)


def test_unrelated_or_legacy_empty_contract_needs_no_getter(monkeypatch):
    monkeypatch.setattr(rated, "read", lambda gpu: pytest.fail("Unexpected getter"))
    for changes in ({}, {"power.0.maximum": 999}, {"factory.0.offset": 7}):
        assert rated.contracts(None, None, changes) == {}
        rated.validate_contracts(changes, {})
    assert rated.verify(None, {}) == []
    rated.validate_inspection(None, {}, {})


@pytest.mark.parametrize(
    "changes",
    [
        None,
        [],
        {1: 3},
        {"factory.0.rated": 3},
        {"factory.2.rated": 2101, "factory.3.rated": 2501},
        {"factory.2.rated": 2101, "power.0.maximum": 999},
        {"factory.2.rated": 2101, "factory.0.offset": 3},
    ],
)
def test_unknown_or_combined_changes_are_rejected_before_get(monkeypatch, changes):
    monkeypatch.setattr(rated, "read", lambda gpu: pytest.fail("Unexpected getter"))
    with pytest.raises(rated.RatedClockError):
        rated.contracts(None, None, changes)


@pytest.mark.parametrize(
    "faults,success,calls_expected",
    [
        ((), True, 2),
        (("exception",), True, 3),
        (("exception", "exception"), False, 2),
        (("changing",) * 4, False, 4),
        (("changing", "ok"), True, 4),
    ],
)
def test_bounded_two_complete_read_recovery(monkeypatch, faults, success, calls_expected):
    calls, state = [], observation()["state"]

    def once(gpu):
        fault = faults[len(calls)] if len(calls) < len(faults) else "ok"
        calls.append(fault)
        if fault == "exception":
            raise OSError("temporary transport failure")
        result = copy.deepcopy(state)
        if fault == "changing":
            result["info_hex"] = packed_hex(result["info_hex"], 3, len(calls))
        return result

    monkeypatch.setattr(rated, "_once", once)
    result, error = rated.read(SimpleNamespace(_lock=threading.RLock()))
    assert (result is not None) is success and (error is None) is success
    assert len(calls) == calls_expected
    if success:
        assert result == observed(state)


def getter_gpu(monkeypatch, fault=None):
    """Exercise the real guarded RM/public GET wrappers, without loading a driver."""
    state = observation()["state"]
    identity = state["factory"]["clock"]["identity"]
    header = [0] * 17
    for index, value in clock._ENVELOPE.items():
        header[index] = value
    fields = {"client": 13, "device": 29, "subdevice": 41}
    calls = []
    gpu = SimpleNamespace(_lock=threading.RLock(), identity=copy.deepcopy(identity))
    reader = SimpleNamespace(_transport=lambda: (header, fields), gpu=gpu)
    monkeypatch.setattr(clock, "PerfDiagnosticsReader", lambda supplied: reader)
    monkeypatch.setattr(clock, "_binding", lambda supplied: copy.deepcopy(supplied.gpu.identity))
    monkeypatch.setattr(rated.factory, "read", lambda supplied: (factory.observed(state["factory"]), None))

    def escape(packet, supplied):
        assert supplied == fields
        assert packet[5] == packet[11] == packet[16] == 0
        command, size = packet[14], packet[15]
        assert packet[2] == 68 + size
        address = ctypes.addressof(packet)
        incoming = ctypes.string_at(address + 68, size)
        calls.append(("rm", command, incoming))
        if command == rated.INFO_COMMAND:
            assert incoming == bytes(rated.INFO_SIZE)
            response = state["info_hex"]
        elif command == rated.CONTROL_COMMAND:
            assert incoming[4:36] == bytes.fromhex(state["info_hex"])[4:36]
            assert incoming[:4] == bytes(4) and incoming[36:] == bytes(size - 36)
            response = state["control_hex"]
        else:
            assert command == rated.QUERY_COMMAND
            (selector,) = struct.unpack_from("<I", incoming)
            name = next(name for name, (_, sel, _) in rated._SELECTORS.items() if sel == selector)
            assert struct.unpack_from("<I", incoming, 8)[0] == 0x10000000
            assert struct.unpack_from("<I", incoming, 16)[0] == 4
            assert [struct.unpack_from("<I", incoming, 20 + 16 * i)[0] for i in range(4)] == [1, 2, 16, 0x80000000]
            response = state["native"][name]
        ctypes.memmove(address + 68, bytes.fromhex(response), size)
        packet[5], packet[11] = 1, 1
        if fault == "rm_status":
            packet[16] = 5
        elif fault == "completion":
            packet[11] = 0
        elif fault == "context":
            packet[5] = 2
        elif fault == "geometry":
            packet[15] += 4
        elif fault == "envelope":
            packet[7] += 1
        elif fault in ("guard_before", "guard_after"):
            ctypes.c_ubyte.from_address(address - 1 if fault == "guard_before" else address + 68 + size).value = 0
        elif fault == "target":
            gpu.identity["device_id"] += 1
        return {"nt_status": -1, "bool_status": False, "none_status": None}.get(fault, 0)

    def public(handle, pointer):
        assert handle == 123
        address = ctypes.cast(pointer, ctypes.c_void_p).value
        version, mode = struct.unpack("<II", ctypes.string_at(address, 8))
        assert version == 0x20108 and mode in (1, 2)
        calls.append(("public", mode))
        ctypes.memmove(address, bytes.fromhex(state["public"]["base" if mode == 1 else "boost"]), 264)
        if fault == "public_guard":
            ctypes.c_ubyte.from_address(address + 264).value = 0
        elif fault == "public_target":
            gpu.identity["device_id"] += 1
        return -1 if fault == "public_status" else 0

    gpu._legacy_clk_escape = escape
    gpu.nvapi = SimpleNamespace(gpu=123, AllClocks=public)
    return gpu, calls, header, fields, state


def test_actual_guarded_getters_construct_only_bounded_get_packets(monkeypatch):
    gpu, calls, _, _, expected = getter_gpu(monkeypatch)
    result, error = rated.read(gpu)
    assert error is None and result == observed(expected)
    assert len(calls) == 14  # Two complete sets: five RM GETs and two public GETs.
    assert [call[1] for call in calls if call[0] == "rm"] == [
        rated.INFO_COMMAND,
        rated.CONTROL_COMMAND,
        rated.QUERY_COMMAND,
        rated.QUERY_COMMAND,
        rated.QUERY_COMMAND,
    ] * 2


@pytest.mark.parametrize(
    "fault",
    [
        "rm_status",
        "nt_status",
        "bool_status",
        "none_status",
        "completion",
        "context",
        "geometry",
        "envelope",
        "guard_before",
        "guard_after",
        "target",
        "public_guard",
        "public_target",
        "public_status",
    ],
)
def test_get_failure_or_envelope_corruption_is_bounded(monkeypatch, fault):
    gpu, calls, _, _, _ = getter_gpu(monkeypatch, fault)
    result, error = rated.read(gpu)
    assert result is None and error
    assert len(calls) == (8 if fault.startswith("public_") else 2)


@pytest.mark.parametrize("fault", ["header", "field_bool", "field_negative", "field_large"])
def test_malformed_transport_never_reaches_escape(monkeypatch, fault):
    gpu, calls, header, fields, _ = getter_gpu(monkeypatch)
    if fault == "header":
        header[0] = 0
    else:
        fields["client"] = {"field_bool": True, "field_negative": -1, "field_large": 1 << 32}[fault]
    result, error = rated.read(gpu)
    assert result is None and error and calls == []


@pytest.mark.parametrize(
    "command,size",
    [
        (0x2080A0A5, rated.INFO_SIZE),
        (rated.INFO_COMMAND, rated.INFO_SIZE - 4),
        (rated.CONTROL_COMMAND, rated.INFO_SIZE),
        (rated.QUERY_COMMAND, 0),
    ],
)
def test_unknown_command_or_size_rejected_before_transport(monkeypatch, command, size):
    gpu, calls, _, _, _ = getter_gpu(monkeypatch)
    with pytest.raises(rated.RatedClockError, match="Only the understood"):
        rated._get(gpu, command, bytes(size))
    assert calls == []


def test_missing_public_getter_keeps_failure_explicit(monkeypatch):
    gpu, calls, _, _, _ = getter_gpu(monkeypatch)
    gpu.nvapi.AllClocks = None
    result, error = rated.read(gpu)
    assert result is None and "unavailable" in error
    assert len(calls) == 6 and all(call[0] == "rm" for call in calls)


def test_missing_independent_factory_state_stops_before_vpstate_get(monkeypatch):
    gpu, calls, _, _, _ = getter_gpu(monkeypatch)
    monkeypatch.setattr(rated.factory, "read", lambda supplied: (None, "VF tuning requests unavailable"))
    result, error = rated.read(gpu)
    assert result is None and error == "VF tuning requests unavailable" and calls == []
