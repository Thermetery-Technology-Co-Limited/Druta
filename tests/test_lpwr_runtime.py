"""Offline loaded-selector contracts, partial capability and guarded GETs."""

import copy
import ctypes
import hashlib
import json
import struct

import pytest

from druta import lpwr_runtime as lp
from tests.test_clock_vf_registry import FakeGPU as PairedGPU
from tests.test_vbios_fields import extend, fixture

DEFAULT_CHANGES = {"lpwr.difr.0.enabled": False}
ROUTES = ((3, 0x40, 0, 1), (12, 0x2, 1, 0), (27, 0x400, 2, 255))


def bios(*, flags=(0xBF, 0, 3), selectors=(2, 0, 255), first=(7, 255, 4), device=0x4321):
    index = bytearray(6 + 18 * len(selectors))
    index[:6] = bytes((0x10, 6, 18, len(selectors), 31, 7))
    for number, selector in enumerate(selectors):
        index[6 + number * 18 + 10] = selector
    difr = bytearray(11 + 18 * len(flags))
    difr[:4] = bytes((0x10, 11, 18, len(flags)))
    difr[4:11] = b"opaque!"
    for number, value in enumerate(flags):
        at = 11 + 18 * number
        difr[at : at + 18] = bytes((value,)) + bytes(range(17))
    perf = bytearray(14 + 17 * len(first))
    perf[:6] = bytes((0x70, 14, 5, len(first), 6, 2))
    for number, value in enumerate(first):
        perf[14 + 17 * number] = value
    return extend(
        fixture(device=device),
        [
            (0x74, 0x24, 0x4500, index),
            (0xE8, 0x8E, 0x4600, difr),
            (0, 0x0E, 0x4800, perf),
        ],
    )


def controller(supported, mask=None, *, available=True, states_available=None):
    return {
        "available": available,
        "supported": supported if available else None,
        "states_available": bool(supported) if states_available is None else states_available,
        "configured_pstate_mask": mask if supported else None,
    }


def _attribute_reply(attribute, indices, controllers):
    raw = bytearray(lp._request(attribute, indices))
    for number, index in enumerate(indices):
        row = controllers[str(index)]
        available = row["available"] if attribute == lp.SUPPORT else row["states_available"]
        value = row["supported"] if attribute == lp.SUPPORT else row["configured_pstate_mask"]
        struct.pack_into("<HI", raw, 10 + 16 * number, int(available), int(value or 0))
    return bytes(raw)


def observation(*, routes=ROUTES, controllers=None, slot="0000:72:1d.3", architecture=8, device=0xABCD):
    controllers = copy.deepcopy(
        controllers
        if controllers is not None
        else {
            "9": controller(True, 0x42),
            "10": controller(True, 0x40),
            "11": controller(True, 0x40),
        }
    )
    raw = bytearray([0xA5]) * lp.PSTATE_SIZE
    raw[0], raw[8] = 9, 80
    struct.pack_into("<I", raw, 4, sum(1 << index for index, *_ in routes))
    rows = []
    for index, mask, level, selector in routes:
        at = 12 + 552 * index
        raw[at], raw[at + 14] = 8, 8
        struct.pack_into("<II", raw, at + 4, mask, 0x480)
        raw[at + 12], raw[at + 13] = level, selector
        rows.append({"index": index, "type": 8, "pstate_id_mask": mask, "level": level, "lpwr_index": selector})
    indices = tuple(i for i in lp.CONTROLLERS if controllers[str(i)]["supported"] is True)
    states = _attribute_reply(lp.STATES, indices, controllers) if indices else None
    return {
        "schema": 1,
        "stable_reads": 2,
        "identity": {"slot": slot, "architecture": architecture, "device_id": device, "subsystem_id": 0x98765432},
        "pstate_info_hex": raw.hex(),
        "pstates": {"group": 9, "version": 80, "mask": sum(1 << r[0] for r in routes), "rows": rows},
        "controllers": controllers,
        "support_hex": _attribute_reply(lp.SUPPORT, tuple(lp.CONTROLLERS), controllers).hex(),
        "states_hex": states.hex() if states is not None else None,
    }


def make_contract(monkeypatch, *, image=None, obs=None, changes=None):
    image = bios() if image is None else image
    obs = observation() if obs is None else obs
    changes = DEFAULT_CHANGES if changes is None else changes
    monkeypatch.setattr(lp, "read", lambda gpu: (copy.deepcopy(obs), None))
    return lp.contracts(None, image, changes)


def observed(saved, *, restore=False):
    result = copy.deepcopy(saved["before"])
    controllers = result["controllers"] if restore else saved["after"]
    result["controllers"] = copy.deepcopy(controllers)
    result["support_hex"] = _attribute_reply(lp.SUPPORT, tuple(lp.CONTROLLERS), controllers).hex()
    indices = tuple(i for i in lp.CONTROLLERS if controllers[str(i)]["supported"] is True)
    result["states_hex"] = _attribute_reply(lp.STATES, indices, controllers).hex() if indices else None
    return result


class FakeGPU(PairedGPU):
    def __init__(self, *, obs=None, actions=(), **kwargs):
        super().__init__(**kwargs)
        self.observation = copy.deepcopy(obs or observation(architecture=self.architecture))
        self.observation["identity"] = {
            "slot": self.address,
            "architecture": self.architecture,
            "device_id": 0xABCD,
            "subsystem_id": 0x12345678,
        }
        self.actions, self.calls = list(actions), []

    def _legacy_clk_escape(self, packet, fields):
        assert packet[2] == 68 + packet[15] and packet[5] == packet[11] == 0
        if packet[14] == lp.PSTATE_COMMAND:
            assert packet[15] == lp.PSTATE_SIZE
            raw = bytes.fromhex(self.observation["pstate_info_hex"])
            selector = None
        else:
            assert packet[14] == lp.ATTRIBUTE_COMMAND and packet[15] == lp.ATTRIBUTE_SIZE
            request = bytes(packet)[68:]
            count = struct.unpack_from("<I", request)[0]
            indices = tuple(struct.unpack_from("<H", request, 6 + 16 * i)[0] for i in range(count))
            selector = struct.unpack_from("<H", request, 8)[0]
            assert request == lp._request(selector, indices)
            assert selector in (lp.SUPPORT, lp.STATES) and set(indices) <= lp.CONTROLLERS.keys()
            raw = _attribute_reply(selector, indices, self.observation["controllers"])
        self.calls.append((packet[14], selector))
        ctypes.memmove(ctypes.addressof(packet) + 68, raw, len(raw))
        packet[5], packet[11], packet[16] = 1, 1, 0
        action = self.actions.pop(0) if self.actions else None
        return action(self, packet) if action else 0


def test_alternate_routes_and_multi_entry_disable_preserve_exact_model(monkeypatch):
    saved = make_contract(monkeypatch)
    assert saved["before_image_sha256"] == hashlib.sha256(bios()).hexdigest()
    assert saved["selected"] == {
        "lpwr.difr.0.enabled": {"index": 0, "offset": 0x460B, "before_byte": 0xBF, "after_byte": 0xBE}
    }
    assert saved["after"] == {"9": controller(True, 2), "10": controller(False), "11": controller(False)}
    assert lp.verify(observed(saved), saved) == list(DEFAULT_CHANGES)
    assert lp.verify(observed(saved, restore=True), saved, restore=True) == list(DEFAULT_CHANGES)
    assert len(json.dumps(saved, allow_nan=False)) < 64 * 1024
    both = {**DEFAULT_CHANGES, "lpwr.difr.2.enabled": False}
    saved = make_contract(monkeypatch, changes=both)
    assert all(row == controller(False) for row in saved["after"].values())
    assert lp.verify(observed(saved), saved) == sorted(both)
    lp.validate_contracts(both, json.loads(json.dumps(saved)))


def test_no_selected_controls_do_not_probe_or_parse(monkeypatch):
    monkeypatch.setattr(lp, "read", lambda gpu: pytest.fail("No GET expected"))
    assert lp.contracts(None, None, {}) == {}
    assert lp.contracts(None, None, {"power.4.maximum": 600000}) == {}
    assert lp.verify(None, {}) == []
    lp.validate_contracts({}, {})


@pytest.mark.parametrize(
    "changes",
    [
        {"lpwr.difr.0.enabled": True},
        {"lpwr.difr.0.enabled": 0},
        {"lpwr.difr.0.enabled": None},
        {"lpwr.difr.00.enabled": False},
        {"lpwr.difr.5.enabled": False},
        {"lpwr.difr.-1.enabled": False},
        {"lpwr.difr." + "9" * 5000 + ".enabled": False},
        {"lpwr.difr.0.other": False},
        {**DEFAULT_CHANGES, "lpwr.gr_pg.0.enabled": False},
        {True: False},
        [],
    ],
)
def test_disable_only_keys_and_types_are_exact(changes):
    with pytest.raises(lp.LpwrError):
        lp.selected(changes)


def test_supported_zero_mask_is_distinct_from_unsupported(monkeypatch):
    source = observation(controllers={"9": controller(True, 0), "10": controller(False), "11": controller(False)})
    image = bios(flags=(3,), selectors=(0,))
    source = observation(routes=((19, 4, 0, 255),), controllers=source["controllers"])
    saved = make_contract(monkeypatch, image=image, obs=source)
    assert saved["before"]["controllers"]["9"]["configured_pstate_mask"] == 0
    assert saved["after"]["9"]["configured_pstate_mask"] is None
    assert lp.verify(observed(saved), saved) == list(DEFAULT_CHANGES)


def test_unloaded_difr_and_fallback_are_zero_but_unloaded_index_is_unqualified(monkeypatch):
    source = observation(
        routes=((22, 8, 0, 0),),
        controllers={
            "9": controller(True, 0),
            "10": controller(True, 0),
            "11": controller(True, 0),
        },
    )
    saved = make_contract(monkeypatch, image=bios(flags=(15,), selectors=(4,)), obs=source)
    assert all(not row["supported"] for row in saved["after"].values())
    bad = observation(routes=((22, 8, 0, 5),), controllers=source["controllers"])
    with pytest.raises(lp.LpwrError, match="unloaded index"):
        make_contract(monkeypatch, image=bios(flags=(15,), selectors=(4,)), obs=bad)


@pytest.mark.parametrize(
    "offset,value",
    [
        (0x4500, 0x20),
        (0x4501, 7),
        (0x4502, 17),
        (0x4503, 6),
        (0x4601, 10),
        (0x4603, 6),
        (0x4800, 0x60),
        (0x4801, 20),
        (0x4802, 0),
    ],
)
def test_table_geometry_is_not_inferred_from_board_identity(monkeypatch, offset, value):
    image = bytearray(bios())
    image[offset] = value
    with pytest.raises((lp.LpwrError, ValueError)):
        make_contract(monkeypatch, image=bytes(image))


def test_empty_perf_branch_wrong_owner_and_missing_pointer_refused(monkeypatch):
    with pytest.raises(lp.LpwrError, match="aggregation"):
        make_contract(monkeypatch, image=bios(first=(255, 255)))
    image = bytearray(bios())
    struct.pack_into("<I", image, 0x200 + 0x74, 0)
    with pytest.raises(ValueError, match="not populated"):
        make_contract(monkeypatch, image=image)
    image = bytearray(bios())
    struct.pack_into("<I", image, 0x2210 + 3 * 60, 0x25)
    with pytest.raises(ValueError, match="owner"):
        make_contract(monkeypatch, image=image)


@pytest.mark.parametrize("kind", ["support", "mask", "unavailable", "states", "disabled", "absent"])
def test_contract_requires_real_before_model_match(monkeypatch, kind):
    image, state, changes = bios(), observation(), DEFAULT_CHANGES
    if kind == "support":
        state = observation(controllers={**state["controllers"], "10": controller(False)})
    elif kind == "mask":
        state = observation(controllers={**state["controllers"], "9": controller(True, 256)})
    elif kind == "unavailable":
        state = observation(controllers={**state["controllers"], "11": controller(None, available=False)})
    elif kind == "states":
        state = observation(controllers={**state["controllers"], "9": controller(True, None, states_available=False)})
    elif kind == "disabled":
        changes = {"lpwr.difr.1.enabled": False}
    else:
        changes = {"lpwr.difr.4.enabled": False}
    with pytest.raises(lp.LpwrError):
        make_contract(monkeypatch, image=image, obs=state, changes=changes)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda s: s.update(schema=True),
        lambda s: s.update(before_image_sha256="0" * 63),
        lambda s: s.update(selected={}),
        lambda s: s["selected"]["lpwr.difr.0.enabled"].update(after_byte=0),
        lambda s: s["selected"]["lpwr.difr.0.enabled"].update(index=False),
        lambda s: s["tables"]["difr"].update(offset=True),
        lambda s: s["tables"]["difr"].update(owner=36),
        lambda s: s["tables"]["perf"].update(row_first_hex="ffff"),
        lambda s: s["tables"]["index"].update(offset=0x4600),
        lambda s: s["after"]["10"].update(configured_pstate_mask=0),
        lambda s: s["before"]["identity"].update(architecture=True),
        lambda s: s["before"]["pstates"]["rows"][0].update(index=True),
        lambda s: s["before"].update(stable_reads=True),
    ],
)
def test_malformed_contract_is_rejected_offline(monkeypatch, mutation):
    saved = make_contract(monkeypatch)
    mutation(saved)
    with pytest.raises((lp.LpwrError, RuntimeError, ValueError)):
        lp.validate_contracts(DEFAULT_CHANGES, saved)


def test_active_and_undo_fail_on_ignored_or_wrong_direction_readback(monkeypatch):
    saved = make_contract(monkeypatch)
    with pytest.raises(lp.LpwrError, match="expected state"):
        lp.verify(saved["before"], saved)
    with pytest.raises(lp.LpwrError, match="expected state"):
        lp.verify(observed(saved), saved, restore=True)
    with pytest.raises(lp.LpwrError):
        lp.validate_contracts({"lpwr.difr.2.enabled": False}, saved)


def test_flags_and_frequency_tail_are_diagnostic_but_route_and_identity_are_preserved(monkeypatch):
    saved = make_contract(monkeypatch)
    current = observed(saved)
    raw = bytearray.fromhex(current["pstate_info_hex"])
    at = 12 + 552 * ROUTES[0][0]
    raw[at + 8] ^= 16
    raw[at + 24] ^= 3
    current["pstate_info_hex"] = raw.hex()
    assert lp.verify(current, saved) == list(DEFAULT_CHANGES)
    current["identity"]["device_id"] = 0x5678
    with pytest.raises(lp.LpwrError, match="target"):
        lp.verify(current, saved)
    current = observed(saved)
    raw = bytearray.fromhex(current["pstate_info_hex"])
    raw[at + 13] = 0
    current["pstate_info_hex"] = raw.hex()
    current["pstates"]["rows"][0]["lpwr_index"] = 0
    with pytest.raises(lp.LpwrError, match="routing"):
        lp.verify(current, saved)


def test_read_uses_only_known_gets_and_supports_partial_controller_availability():
    source = observation(
        controllers={"9": controller(True, 0), "10": controller(False), "11": controller(None, available=False)}
    )
    gpu = FakeGPU(obs=source)
    result, error = lp.read(gpu)
    assert error is None and result["controllers"] == source["controllers"]
    assert result["identity"]["slot"] == "0000:72:1d.3" and len(gpu.calls) == 6
    assert all(command in (lp.PSTATE_COMMAND, lp.ATTRIBUTE_COMMAND) for command, _ in gpu.calls)
    assert result["controllers"]["9"]["configured_pstate_mask"] == 0
    assert result["controllers"]["10"]["configured_pstate_mask"] is None


def test_all_unsupported_skips_states_get_without_fabricating_masks():
    source = observation(controllers={str(i): controller(False) for i in lp.CONTROLLERS})
    gpu = FakeGPU(obs=source)
    result, error = lp.read(gpu)
    assert not error and result["states_hex"] is None
    assert len(gpu.calls) == 4 and all(attribute != lp.STATES for _, attribute in gpu.calls)


def status(code):
    def action(gpu, packet):
        packet[16] = code
        return 0

    return action


def test_transient_pair_recovery_is_bounded_and_retains_sticky_identity():
    gpu = FakeGPU(actions=(status(0x40),))
    result, error = lp.read(gpu)
    assert not error and result["stable_reads"] == 2
    assert len(gpu.calls) == 7 and gpu._current_limit_layout_choice is None
    gpu = FakeGPU(actions=(status(0x40), status(0x40)))
    assert lp.read(gpu)[0] is None and len(gpu.calls) == 2


def test_missing_states_capability_retains_supported_diagnostic_readback():
    gpu = FakeGPU(actions=(None, None, status(0x56), None, None, status(0x56)))
    result, error = lp.read(gpu)
    assert error is None and result["states_hex"] is None
    assert all(row["supported"] and not row["states_available"] for row in result["controllers"].values())


@pytest.mark.parametrize("which", ["geometry", "type", "version", "guard", "identity", "support_encoding", "route"])
def test_malformed_or_unstable_get_is_not_silently_retried(which):
    def action(gpu, packet):
        address = ctypes.addressof(packet) + 68
        if which == "geometry":
            packet[15] -= 4
        elif which == "type":
            ctypes.c_ubyte.from_address(address + 12 + 552 * 3 + 14).value = 7
        elif which == "version":
            ctypes.c_ubyte.from_address(address + 8).value = 0
        elif which == "guard":
            ctypes.c_ubyte.from_address(address + packet[15]).value = 0
        elif which == "identity":
            gpu.nvapi.selected["devid"] = 0x5678
        elif which == "support_encoding":
            ctypes.c_uint32.from_address(address + 12).value = 2
        else:
            raw = bytearray.fromhex(gpu.observation["pstate_info_hex"])
            raw[12 + 552 * 3 + 13] = 0
            gpu.observation["pstate_info_hex"] = raw.hex()
        return 0

    actions = (None, action) if which == "support_encoding" else (action,)
    gpu = FakeGPU(actions=actions)
    result, error = lp.read(gpu)
    assert result is None and error
    assert len(gpu.calls) == {"support_encoding": 2, "route": 6}.get(which, 1)


def test_read_allows_diagnostic_clock_changes_between_stable_core_captures():
    def action(gpu, packet):
        raw = bytearray.fromhex(gpu.observation["pstate_info_hex"])
        raw[12 + 552 * 3 + 0x20] ^= 1
        gpu.observation["pstate_info_hex"] = raw.hex()
        return 0

    result, error = lp.read(FakeGPU(actions=(action,)))
    assert error is None and result["stable_reads"] == 2


@pytest.mark.parametrize("architecture", [4, 6, 10])
def test_architecture_probe_not_driver_or_board_allowlist(architecture):
    result, error = lp.read(FakeGPU(architecture=architecture))
    assert error is None and result["identity"]["architecture"] == architecture
