"""Offline GRPG configured-mask contracts and shared guarded GET behavior."""

import copy
import ctypes
import hashlib
import json
import struct

import pytest

from druta import grpg_runtime as gr
from druta import lpwr_runtime as lp
from tests.test_clock_vf_registry import FakeGPU as PairedGPU
from tests.test_lpwr_runtime import controller
from tests.test_vbios_fields import extend, fixture

DEFAULT_CHANGES = {"lpwr.gr_pg.0.enabled": False}
ROUTES = ((3, 0x40, 0, 1), (12, 0x2, 1, 0), (27, 0x400, 2, 255), (31, 0x100, 3, 2))


def bios(*, flags=(0x1B, 0x1B, 0x1B), selectors=(1, 0, 2), first=(7, 255, 4), device=0x4321):
    index = bytearray(6 + 18 * len(selectors))
    index[:6] = bytes((0x10, 6, 18, len(selectors), 31, 7))
    for number, selector in enumerate(selectors):
        index[6 + number * 18 + 1] = selector
    grpg = bytearray(10 + 5 * len(flags))
    grpg[:4] = bytes((0x10, 10, 5, len(flags)))
    grpg[4:10] = b"opaque"
    for number, value in enumerate(flags):
        at = 10 + 5 * number
        grpg[at : at + 5] = bytes((value, 0xA2, 0xE3, 0xC4, 0xD5))
    perf = bytearray(14 + 17 * len(first))
    perf[:6] = bytes((0x70, 14, 5, len(first), 6, 2))
    for number, value in enumerate(first):
        perf[14 + 17 * number] = value
    return extend(
        fixture(device=device),
        [(0x74, 0x24, 0x4500, index), (0x80, 0x39, 0x4600, grpg), (0, 0x0E, 0x4800, perf)],
    )


def _attribute_reply(attribute, indices, controllers):
    raw = bytearray(lp._request(attribute, indices))
    for number, index in enumerate(indices):
        row = controllers[str(index)]
        available = row["available"] if attribute == lp.SUPPORT else row["states_available"]
        value = row["supported"] if attribute == lp.SUPPORT else row["configured_pstate_mask"]
        struct.pack_into("<HI", raw, 10 + 16 * number, int(available), int(value or 0))
    return bytes(raw)


def observation(*, routes=ROUTES, controllers=None, slot="0000:72:1d.3", architecture=8, device=0xABCD):
    if controllers is None:
        controllers = {str(i): controller(False) for i in gr.CONTROLLERS}
        controllers.update(
            {
                "0": controller(True, 0x8542),
                "1": controller(True, 0),
                "3": controller(None, available=False),
                "9": controller(True, 0x142),
                "21": controller(True, None, states_available=False),
            }
        )
    controllers = copy.deepcopy(controllers)
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
    indices = tuple(i for i in gr.CONTROLLERS if controllers[str(i)]["supported"] is True)
    states = _attribute_reply(lp.STATES, indices, controllers) if indices else None
    return {
        "schema": 1,
        "stable_reads": 2,
        "identity": {"slot": slot, "architecture": architecture, "device_id": device, "subsystem_id": 0x98765432},
        "pstate_info_hex": raw.hex(),
        "pstates": {"group": 9, "version": 80, "mask": sum(1 << r[0] for r in routes), "rows": rows},
        "controllers": controllers,
        "support_hex": _attribute_reply(lp.SUPPORT, gr.CONTROLLERS, controllers).hex(),
        "states_hex": states.hex() if states is not None else None,
    }


def make_contract(monkeypatch, *, image=None, obs=None, changes=None):
    image = bios() if image is None else image
    obs = observation() if obs is None else obs
    changes = DEFAULT_CHANGES if changes is None else changes
    monkeypatch.setattr(gr, "read", lambda gpu, **kwargs: (copy.deepcopy(obs), None))
    return gr.contracts(None, image, changes)


def _aux_packets(feature, group):
    feature_raw = bytearray(lp._request(0x38, (0,)))
    struct.pack_into("<HI", feature_raw, 10, int(feature["available"]), int(feature["value"] or 0))
    group_raw = bytearray(gr._aux_request())
    for ordinal, (role, attribute) in enumerate(gr._AUX_KEYS):
        value = group[f"{attribute:x}:{role}"]
        struct.pack_into("<HI", group_raw, 10 + ordinal * 16, int(value["available"]), int(value["value"] or 0))
    return {"feature_hex": feature_raw.hex(), "feature": feature, "group_hex": group_raw.hex(), "group": group}


def auxiliary_observation(*, group=None, feature=0x12345010, feature_available=True, **kwargs):
    result = observation(**kwargs)
    if group is None:
        group = {
            "1:0": {"available": True, "value": 1},
            "800:0": {"available": True, "value": 1},
            "800:1": {"available": True, "value": 1},
            "800:2": {"available": True, "value": 0},
            "800:3": {"available": False, "value": None},
            "806:0": {"available": True, "value": 0x40},
            "806:1": {"available": True, "value": 0x100},
            "806:2": {"available": True, "value": 0},
            "806:3": {"available": False, "value": None},
        }
    result["auxiliary"] = _aux_packets(
        {"available": feature_available, "value": feature if feature_available else None}, copy.deepcopy(group)
    )
    return result


def make_aux_contract(monkeypatch, *, image=None, obs=None, changes=None):
    return make_contract(
        monkeypatch,
        image=bios(flags=(0x1B, 0x13, 0x13)) if image is None else image,
        obs=auxiliary_observation() if obs is None else obs,
        changes=changes,
    )


def observed(saved, *, restore=False):
    result = copy.deepcopy(saved["before"])
    controllers = result["controllers"] if restore else saved["after"]
    result["controllers"] = copy.deepcopy(controllers)
    result["support_hex"] = _attribute_reply(lp.SUPPORT, gr.CONTROLLERS, controllers).hex()
    indices = tuple(i for i in gr.CONTROLLERS if controllers[str(i)]["supported"] is True)
    result["states_hex"] = _attribute_reply(lp.STATES, indices, controllers).hex() if indices else None
    if saved["schema"] == 2 and not restore:
        auxiliary = copy.deepcopy(saved["after_auxiliary"])
        result["auxiliary"] = _aux_packets(auxiliary["feature"], auxiliary["group"])
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
            selector, indices = None, None
        else:
            assert packet[14] == lp.ATTRIBUTE_COMMAND and packet[15] == lp.ATTRIBUTE_SIZE
            request = bytes(packet)[68:]
            count = struct.unpack_from("<I", request)[0]
            indices = tuple(struct.unpack_from("<H", request, 6 + 16 * i)[0] for i in range(count))
            selector = struct.unpack_from("<H", request, 8)[0]
            if struct.unpack_from("<H", request, 4)[0] == 1:
                assert request == gr._aux_request()
                raw = bytes.fromhex(self.observation["auxiliary"]["group_hex"])
            elif selector == 0x38:
                assert request == lp._request(0x38, (0,))
                raw = bytes.fromhex(self.observation["auxiliary"]["feature_hex"])
            else:
                assert request == lp._request(selector, indices)
                assert selector in (lp.SUPPORT, lp.STATES) and set(indices) <= set(gr.CONTROLLERS)
                raw = _attribute_reply(selector, indices, self.observation["controllers"])
        self.calls.append((packet[14], selector, indices))
        ctypes.memmove(ctypes.addressof(packet) + 68, raw, len(raw))
        packet[5], packet[11], packet[16] = 1, 1, 0
        action = self.actions.pop(0) if self.actions else None
        return action(self, packet) if action else 0


def status(code):
    def action(gpu, packet):
        packet[16] = code
        return 0

    return action


def test_exact_disable_preserves_fallback_opaque_and_all_peer_masks(monkeypatch):
    saved = make_contract(monkeypatch)
    assert saved["before_image_sha256"] == hashlib.sha256(bios()).hexdigest()
    assert saved["selected"] == {
        "lpwr.gr_pg.0.enabled": {"index": 0, "offset": 0x460A, "before_byte": 0x1B, "after_byte": 0x1A}
    }
    assert saved["after"]["0"] == controller(True, 0x8502)
    assert {k: v for k, v in saved["after"].items() if k != "0"} == {
        k: v for k, v in saved["before"]["controllers"].items() if k != "0"
    }
    assert gr.verify(observed(saved), saved) == list(DEFAULT_CHANGES)
    assert gr.verify(observed(saved, restore=True), saved, restore=True) == list(DEFAULT_CHANGES)
    gr.validate_contracts(DEFAULT_CHANGES, json.loads(json.dumps(saved, allow_nan=False)))
    assert len(json.dumps(saved)) < 64 * 1024


def test_multiple_disabled_rows_reconcile_masks_once(monkeypatch):
    changes = {**DEFAULT_CHANGES, "lpwr.gr_pg.1.enabled": False}
    saved = make_contract(monkeypatch, changes=changes)
    assert saved["after"]["0"]["configured_pstate_mask"] == 0x8500
    assert gr.verify(observed(saved), saved) == sorted(changes)


def test_unselected_category_has_no_get_or_bios_dependency(monkeypatch):
    monkeypatch.setattr(gr, "read", lambda gpu: pytest.fail("No capability GET expected"))
    assert gr.contracts(None, None, {}) == {}
    assert gr.contracts(None, None, {"power.7.maximum": 500000}) == {}
    assert gr.verify(None, {}) == []
    gr.validate_contracts({}, {})


@pytest.mark.parametrize(
    "changes",
    [
        {"lpwr.gr_pg.0.enabled": True},
        {"lpwr.gr_pg.0.enabled": 0},
        {"lpwr.gr_pg.0.enabled": None},
        {"lpwr.gr_pg.00.enabled": False},
        {"lpwr.gr_pg.5.enabled": False},
        {"lpwr.gr_pg.-1.enabled": False},
        {"lpwr.gr_pg." + "9" * 5000 + ".enabled": False},
        {"lpwr.gr_pg.0.other": False},
        {**DEFAULT_CHANGES, "lpwr.difr.0.enabled": False},
        {True: False},
        [],
    ],
)
def test_strict_disable_keys_values_and_category(changes):
    with pytest.raises(gr.GrpgError):
        gr.selected(changes)


@pytest.mark.parametrize("flags", [(0x1B,), (0x1B, 1, 1), (0x1B, 0x19, 0x19), (0x1B, 0x13, 0x13), (0x1B, 0xB, 0xB)])
def test_last_main_or_auxiliary_presence_loss_is_unqualified(monkeypatch, flags):
    with pytest.raises(gr.GrpgError, match="last enabled|auxiliary"):
        make_contract(monkeypatch, image=bios(flags=flags))


def test_disabled_auxiliary_bits_do_not_supply_aggregate_preservation(monkeypatch):
    with pytest.raises(gr.GrpgError, match="auxiliary"):
        make_contract(monkeypatch, image=bios(flags=(0x1B, 0x1A, 1)))


def test_multiple_removal_cannot_lose_last_auxiliary_entry(monkeypatch):
    with pytest.raises(gr.GrpgError, match="auxiliary"):
        make_contract(
            monkeypatch, image=bios(flags=(0x1B, 0x1B, 1)), changes={**DEFAULT_CHANGES, "lpwr.gr_pg.1.enabled": False}
        )


@pytest.mark.parametrize("enabled", [True, False])
def test_consumed_unselected_rows_must_match_the_before_model(monkeypatch, enabled):
    image = bios(flags=(0x1B, 0x1B if enabled else 0x1A, 0x1B))
    source = observation()
    source["controllers"]["0"] = controller(True, 0x8540 if enabled else 0x8542)
    source = observation(controllers=source["controllers"])
    with pytest.raises(gr.GrpgError, match="consumed BIOS entry"):
        make_contract(monkeypatch, image=image, obs=source)


def test_matching_disabled_peer_and_opaque_flag_bits_are_preserved(monkeypatch):
    image = bios(flags=(0xFF, 0xFE, 0x1B))
    source = observation()
    source["controllers"]["0"] = controller(True, 0x8540)
    source = observation(controllers=source["controllers"])
    saved = make_contract(monkeypatch, image=image, obs=source)
    assert saved["selected"]["lpwr.gr_pg.0.enabled"]["after_byte"] == 0xFE
    assert saved["after"]["0"]["configured_pstate_mask"] == 0x8500
    assert bytes.fromhex(saved["tables"]["grpg"]["raw_hex"]) == image[0x4600:0x4619]


@pytest.mark.parametrize("fallback", [5, 6, 255])
def test_fallback_membership_is_retained_from_live_get_not_zeroed(monkeypatch, fallback):
    routes = ((3, 0x40, 0, 1), (12, 0x2, 1, fallback))
    source = observation(routes=routes)
    if fallback == 5:
        # A valid unloaded index selector is different from a GRPG fallback slot.
        with pytest.raises(gr.GrpgError, match="unloaded index"):
            make_contract(monkeypatch, obs=source)
    else:
        saved = make_contract(monkeypatch, obs=source)
        assert saved["after"]["0"]["configured_pstate_mask"] == 0x8502


@pytest.mark.parametrize("slot", [4, 5, 6, 255])
def test_unselected_missing_or_fallback_grpg_slot_does_not_need_invented_flags(monkeypatch, slot):
    saved = make_contract(monkeypatch, image=bios(selectors=(slot, 0, 2)))
    assert saved["after"]["0"]["configured_pstate_mask"] == 0x8502


def test_selected_and_untouched_id_overlap_is_not_an_owned_removal(monkeypatch):
    source = observation(routes=((3, 0x40, 0, 1), (12, 0x40, 1, 255)))
    with pytest.raises(gr.GrpgError, match="overlap"):
        make_contract(monkeypatch, obs=source)


@pytest.mark.parametrize(
    "kind", ["no_route", "missing_membership", "zero", "support", "unavailable", "states", "disabled", "absent"]
)
def test_requires_selected_consumed_enabled_rows_and_real_mask(monkeypatch, kind):
    image, source, changes = bios(), observation(), DEFAULT_CHANGES
    if kind == "no_route":
        image = bios(selectors=(1, 2, 2))
    elif kind in ("missing_membership", "zero"):
        source["controllers"]["0"] = controller(True, 0x8502 if kind == "missing_membership" else 0)
    elif kind == "support":
        source["controllers"]["0"] = controller(False)
    elif kind == "unavailable":
        source["controllers"]["0"] = controller(None, available=False)
    elif kind == "states":
        source["controllers"]["0"] = controller(True, None, states_available=False)
    elif kind == "disabled":
        image = bios(flags=(0x1A, 0x1B, 0x1B))
    else:
        changes = {"lpwr.gr_pg.4.enabled": False}
    source = observation(controllers=source["controllers"])
    with pytest.raises(gr.GrpgError):
        make_contract(monkeypatch, image=image, obs=source, changes=changes)


def test_alternate_indices_identity_and_nondefault_pstate_bits(monkeypatch):
    routes = ((2, 0x80000000, 2, 0), (7, 0x8, 4, 1), (18, 0x200, 1, 255))
    source = observation(routes=routes, slot="0000:15:03.1", architecture=6, device=0x2468)
    source["controllers"]["0"] = controller(True, 0x80000208)
    source = observation(
        routes=routes, controllers=source["controllers"], slot="0000:15:03.1", architecture=6, device=0x2468
    )
    saved = make_contract(
        monkeypatch, image=bios(selectors=(2, 0)), obs=source, changes={"lpwr.gr_pg.2.enabled": False}
    )
    assert saved["after"]["0"]["configured_pstate_mask"] == 0x208
    assert gr.verify(observed(saved), saved) == ["lpwr.gr_pg.2.enabled"]


@pytest.mark.parametrize(
    "offset,value",
    [(0x4500, 0x20), (0x4501, 7), (0x4503, 6), (0x4601, 9), (0x4602, 6), (0x4603, 6), (0x4800, 0x60), (0x4802, 0)],
)
def test_bounded_table_geometry_is_required(monkeypatch, offset, value):
    image = bytearray(bios())
    image[offset] = value
    with pytest.raises((RuntimeError, ValueError)):
        make_contract(monkeypatch, image=image)


def test_empty_perf_wrong_owner_and_missing_index_pointer_refused(monkeypatch):
    with pytest.raises(gr.GrpgError, match="aggregation"):
        make_contract(monkeypatch, image=bios(first=(255, 255)))
    image = bytearray(bios())
    struct.pack_into("<I", image, 0x200 + 0x74, 0)
    with pytest.raises(ValueError, match="not populated"):
        make_contract(monkeypatch, image=image)
    image = bytearray(bios())
    struct.pack_into("<I", image, 0x2210 + 3 * 60, 0x25)
    with pytest.raises(ValueError, match="owner"):
        make_contract(monkeypatch, image=image)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda s: s.update(schema=True),
        lambda s: s.update(before_image_sha256="0" * 63),
        lambda s: s.update(kind="lpwr_difr_disable"),
        lambda s: s.update(selected={}),
        lambda s: s["selected"]["lpwr.gr_pg.0.enabled"].update(after_byte=0),
        lambda s: s["selected"]["lpwr.gr_pg.0.enabled"].update(index=False),
        lambda s: s["tables"]["grpg"].update(offset=True),
        lambda s: s["tables"]["grpg"].update(owner=36),
        lambda s: s["tables"]["perf"].update(row_first_hex="ffff"),
        lambda s: s["tables"]["index"].update(offset=0x4600),
        lambda s: s["after"]["0"].update(configured_pstate_mask=0),
        lambda s: s["after"]["9"].update(configured_pstate_mask=0),
        lambda s: s["before"]["identity"].update(architecture=True),
        lambda s: s["before"]["pstates"]["rows"][0].update(index=True),
        lambda s: s["before"].update(stable_reads=True),
    ],
)
def test_malformed_or_direction_changed_contract_refused_offline(monkeypatch, mutation):
    saved = make_contract(monkeypatch)
    mutation(saved)
    with pytest.raises((RuntimeError, ValueError)):
        gr.validate_contracts(DEFAULT_CHANGES, saved)


def test_apply_and_undo_need_exact_direction_and_preserve_peers(monkeypatch):
    saved = make_contract(monkeypatch)
    with pytest.raises(gr.GrpgError, match="expected state"):
        gr.verify(saved["before"], saved)
    with pytest.raises(gr.GrpgError, match="expected state"):
        gr.verify(observed(saved), saved, restore=True)
    current = observed(saved)
    current["controllers"]["9"] = controller(True, 0)
    current = observation(controllers=current["controllers"])
    with pytest.raises(gr.GrpgError, match="expected state"):
        gr.verify(current, saved)
    with pytest.raises(gr.GrpgError):
        gr.validate_contracts({"lpwr.gr_pg.1.enabled": False}, saved)


def test_clock_diagnostics_may_change_but_routes_and_identity_must_not(monkeypatch):
    saved = make_contract(monkeypatch)
    current = observed(saved)
    raw = bytearray.fromhex(current["pstate_info_hex"])
    at = 12 + 552 * ROUTES[0][0]
    raw[at + 8] ^= 16
    raw[at + 24] ^= 3
    current["pstate_info_hex"] = raw.hex()
    assert gr.verify(current, saved) == list(DEFAULT_CHANGES)
    current["identity"]["device_id"] ^= 1
    with pytest.raises(gr.GrpgError, match="target"):
        gr.verify(current, saved)
    current = observed(saved)
    raw = bytearray.fromhex(current["pstate_info_hex"])
    raw[at + 13] = 0
    current["pstate_info_hex"] = raw.hex()
    current["pstates"]["rows"][0]["lpwr_index"] = 0
    with pytest.raises(gr.GrpgError, match="routing"):
        gr.verify(current, saved)


def test_read_uses_shared_gets_all_known_controllers_and_partial_states():
    source = observation()
    gpu = FakeGPU(obs=source)
    result, error = gr.read(gpu)
    assert error is None and result["controllers"] == source["controllers"]
    assert len(gpu.calls) == 6 and gpu.calls[1][2] == tuple(range(22))
    assert gpu.calls[2][2] == (0, 1, 9, 21)
    assert result["controllers"]["1"]["configured_pstate_mask"] == 0
    assert result["controllers"]["3"]["supported"] is None
    assert result["controllers"]["21"]["configured_pstate_mask"] is None


def test_all_unsupported_is_readable_without_inventing_states():
    source = observation(controllers={str(i): controller(False) for i in gr.CONTROLLERS})
    gpu = FakeGPU(obs=source)
    result, error = gr.read(gpu)
    assert not error and result["states_hex"] is None
    assert len(gpu.calls) == 4 and all(a != lp.STATES for _, a, _ in gpu.calls)


def test_transient_retry_is_bounded_and_keeps_the_same_identity():
    gpu = FakeGPU(actions=(status(0x40),))
    result, error = gr.read(gpu)
    assert not error and result["stable_reads"] == 2
    assert len(gpu.calls) == 7 and gpu._current_limit_layout_choice is None
    gpu = FakeGPU(actions=(status(0x40), status(0x40)))
    assert gr.read(gpu)[0] is None and len(gpu.calls) == 2


def test_clock_bytes_are_diagnostic_between_stable_core_reads():
    def action(gpu, packet):
        raw = bytearray.fromhex(gpu.observation["pstate_info_hex"])
        raw[12 + 552 * ROUTES[0][0] + 0x20] ^= 1
        gpu.observation["pstate_info_hex"] = raw.hex()
        return 0

    result, error = gr.read(FakeGPU(actions=(action,)))
    assert error is None and result["stable_reads"] == 2


def test_unsupported_state_get_keeps_support_diagnostic():
    gpu = FakeGPU(actions=(None, None, status(0x56), None, None, status(0x56)))
    result, error = gr.read(gpu)
    assert error is None and result["states_hex"] is None
    assert result["controllers"]["0"]["supported"] is True
    assert result["controllers"]["0"]["configured_pstate_mask"] is None


@pytest.mark.parametrize("which", ["geometry", "type", "sentinel", "guard", "identity", "support", "opaque", "route"])
def test_bad_or_unstable_replies_are_not_retried_as_transients(which):
    def action(gpu, packet):
        address = ctypes.addressof(packet) + 68
        if which == "geometry":
            packet[15] -= 4
        elif which == "type":
            ctypes.c_ubyte.from_address(address + 12 + 552 * 3 + 14).value = 7
        elif which == "sentinel":
            ctypes.c_ubyte.from_address(address + 8).value = 0
        elif which == "guard":
            ctypes.c_ubyte.from_address(address + packet[15]).value = 0
        elif which == "identity":
            gpu.nvapi.selected["devid"] = 0x5678
        elif which == "support":
            ctypes.c_uint32.from_address(address + 12).value = 2
        elif which == "opaque":
            ctypes.c_uint32.from_address(address + 16).value = 2
        else:
            raw = bytearray.fromhex(gpu.observation["pstate_info_hex"])
            raw[12 + 552 * 3 + 13] = 0
            gpu.observation["pstate_info_hex"] = raw.hex()
        return 0

    actions = (None, action) if which in ("support", "opaque") else (action,)
    gpu = FakeGPU(actions=actions)
    result, error = gr.read(gpu)
    assert result is None and error
    assert len(gpu.calls) == {"support": 2, "opaque": 2, "route": 6}.get(which, 1)


@pytest.mark.parametrize("architecture", [4, 6, 10])
def test_reader_has_no_board_or_driver_allowlist(architecture):
    result, error = gr.read(FakeGPU(architecture=architecture))
    assert not error and result["identity"]["architecture"] == architecture


def test_last_bit3_projection_and_undo_preserve_other_requests(monkeypatch):
    saved = make_aux_contract(monkeypatch)
    assert saved["schema"] == 2
    assert saved["after"]["0"] == controller(True, 0x8502)
    before, after = saved["before"]["auxiliary"], saved["after_auxiliary"]
    assert after["feature"] == {"available": True, "value": before["feature"]["value"] & ~0x10}
    assert after["group"]["800:0"] == after["group"]["806:0"] == {"available": True, "value": 0}
    assert after["group"]["1:0"] == {"available": True, "value": 1}
    assert {k: v for k, v in after["group"].items() if k not in ("800:0", "806:0")} == {
        k: v for k, v in before["group"].items() if k not in ("800:0", "806:0")
    }
    assert gr.verify(observed(saved), saved) == list(DEFAULT_CHANGES)
    assert gr.verify(observed(saved, restore=True), saved, restore=True) == list(DEFAULT_CHANGES)
    gr.validate_contracts(DEFAULT_CHANGES, json.loads(json.dumps(saved, allow_nan=False)))
    assert len(json.dumps(saved)) < 64 * 1024


def test_multiple_last_bit3_routes_with_alternate_identity(monkeypatch):
    source = auxiliary_observation(slot="0000:16:03.4", architecture=6, device=0x6789)
    source["auxiliary"]["group"]["806:0"]["value"] = 0x42
    source["auxiliary"] = _aux_packets(source["auxiliary"]["feature"], source["auxiliary"]["group"])
    changes = {**DEFAULT_CHANGES, "lpwr.gr_pg.1.enabled": False}
    saved = make_aux_contract(monkeypatch, image=bios(flags=(0x1B, 0x1B, 0x13)), obs=source, changes=changes)
    assert saved["after"]["0"]["configured_pstate_mask"] == 0x8500
    assert gr.verify(observed(saved), saved) == sorted(changes)


def test_auxiliary_group_any_becomes_false_only_when_all_remaining_roles_known_false(monkeypatch):
    group = auxiliary_observation()["auxiliary"]["group"]
    for index in range(1, 4):
        group[f"800:{index}"] = {"available": True, "value": 0}
        group[f"806:{index}"] = {"available": True, "value": 0}
    saved = make_aux_contract(monkeypatch, obs=auxiliary_observation(group=group))
    assert saved["after_auxiliary"]["group"]["1:0"] == {"available": True, "value": 0}
    assert gr.verify(observed(saved), saved)
    group["800:3"] = {"available": False, "value": None}
    with pytest.raises(gr.GrpgError, match="remaining group support"):
        make_aux_contract(monkeypatch, obs=auxiliary_observation(group=group))


@pytest.mark.parametrize("extra", [0x400, 0x8000])
def test_auxiliary_fallback_or_unowned_mask_cannot_be_erased_by_support_projection(monkeypatch, extra):
    group = auxiliary_observation()["auxiliary"]["group"]
    group["806:0"]["value"] |= extra
    with pytest.raises(gr.GrpgError, match="Untouched or fallback"):
        make_aux_contract(monkeypatch, obs=auxiliary_observation(group=group))
    # The independently owned main-mask operation still preserves these bits.
    saved = make_contract(monkeypatch)
    assert saved["after"]["0"]["configured_pstate_mask"] & extra


@pytest.mark.parametrize("mask", [0, 0x42, 0x140])
def test_loaded_auxiliary_routes_must_match_raw_bit3_not_only_main_membership(monkeypatch, mask):
    group = auxiliary_observation()["auxiliary"]["group"]
    group["806:0"]["value"] = mask
    with pytest.raises(gr.GrpgError, match="auxiliary membership"):
        make_aux_contract(monkeypatch, obs=auxiliary_observation(group=group))


@pytest.mark.parametrize("missing", ["feature", "feature_bit", "1:0", "800:0", "806:0"])
def test_last_contributor_requires_independent_available_affected_fields(monkeypatch, missing):
    source = auxiliary_observation()
    feature, group = source["auxiliary"]["feature"], source["auxiliary"]["group"]
    if missing == "feature":
        feature.update(available=False, value=None)
    elif missing == "feature_bit":
        feature["value"] &= ~0x10
    else:
        group[missing] = {"available": False, "value": None}
    source["auxiliary"] = _aux_packets(feature, group)
    with pytest.raises(gr.GrpgError, match="must be available and active"):
        make_aux_contract(monkeypatch, obs=source)


def test_additional_main_bit1_bit4_loss_remains_unqualified_with_auxiliary_available(monkeypatch):
    for flags in ((0x1B,), (0x1B, 1, 1), (0x1B, 0x11, 0x11), (0x1B, 3, 3)):
        with pytest.raises(gr.GrpgError, match="unqualified auxiliary|last enabled"):
            make_aux_contract(monkeypatch, image=bios(flags=flags))


@pytest.mark.parametrize("changed", ["feature", "800:0", "806:0", "1:0", "800:1", "806:1", "806:3"])
def test_ignored_aux_activation_or_peer_change_fails_exact_verification(monkeypatch, changed):
    saved = make_aux_contract(monkeypatch)
    current = observed(saved)
    auxiliary = current["auxiliary"]
    if changed == "feature":
        auxiliary["feature"]["value"] |= 0x10
    elif changed == "806:3":
        auxiliary["group"][changed] = {"available": True, "value": 0}
    else:
        auxiliary["group"][changed]["value"] ^= 1
    current["auxiliary"] = _aux_packets(auxiliary["feature"], auxiliary["group"])
    with pytest.raises(gr.GrpgError, match="auxiliary.*expected state"):
        gr.verify(current, saved)
    with pytest.raises(gr.GrpgError, match="expected state"):
        gr.verify(observed(saved), saved, restore=True)
    assert gr.verify(observed(saved, restore=True), saved, restore=True)


@pytest.mark.parametrize("which", ["downgrade", "after", "before", "typed_bool", "scope", "added_raw"])
def test_schema2_receipt_validation_is_pure_and_recomputes_auxiliary_projection(monkeypatch, which):
    saved = make_aux_contract(monkeypatch)
    if which == "downgrade":
        saved["schema"] = 1
        saved.pop("after_auxiliary")
    elif which == "after":
        saved["after_auxiliary"]["feature"]["value"] |= 0x10
    elif which == "before":
        saved["before"].pop("auxiliary")
    elif which == "typed_bool":
        saved["after_auxiliary"]["group"]["800:0"]["value"] = False
    elif which == "scope":
        saved["tables"]["grpg"]["raw_hex"] = gr._tables(bios())["grpg"]["raw_hex"]
    else:
        saved["after_auxiliary"]["group_hex"] = saved["before"]["auxiliary"]["group_hex"]
    monkeypatch.setattr(gr, "read", lambda *args, **kwargs: pytest.fail("Pure validation must not GET"))
    with pytest.raises(gr.GrpgError):
        gr.validate_contracts(DEFAULT_CHANGES, saved)


def test_auxiliary_read_is_scoped_and_uses_only_two_stable_shared_configuration_gets():
    gpu = FakeGPU(obs=auxiliary_observation())
    result, error = gr.read(gpu, auxiliary=True)
    assert not error and result["auxiliary"] == gpu.observation["auxiliary"]
    assert len(gpu.calls) == 10
    assert gpu.calls[3][1:] == (0x38, (0,))
    assert gpu.calls[4][1] == 1
    plain = FakeGPU(obs=auxiliary_observation())
    result, error = gr.read(plain)
    assert not error and "auxiliary" not in result and len(plain.calls) == 6


def test_auxiliary_transient_recovery_remains_bounded():
    gpu = FakeGPU(obs=auxiliary_observation(), actions=(None, None, None, status(0x40)))
    result, error = gr.read(gpu, auxiliary=True)
    assert not error and len(gpu.calls) == 14 and result["stable_reads"] == 2
    gpu = FakeGPU(obs=auxiliary_observation(), actions=(None, None, None, status(0x40), status(0x40)))
    assert gr.read(gpu, auxiliary=True)[0] is None and len(gpu.calls) == 5
    # Unsupported auxiliary requests do not retry and do not affect ordinary read scope.
    gpu = FakeGPU(obs=auxiliary_observation(), actions=(None, None, None, status(0x56)))
    assert gr.read(gpu, auxiliary=True)[0] is None and len(gpu.calls) == 4
    assert gr.read(gpu)[0] is not None


@pytest.mark.parametrize("field", ["route", "flags", "support", "opaque", "guard", "identity", "unstable"])
def test_auxiliary_malformed_or_changing_replies_are_rejected_without_retry(field):
    def action(gpu, packet):
        address = ctypes.addressof(packet) + 68
        if field == "route":
            ctypes.c_uint16.from_address(address + 8).value = 0x808
        elif field == "flags":
            ctypes.c_uint16.from_address(address + 10).value = 3
        elif field == "support":
            ctypes.c_uint32.from_address(address + 12).value = 2
        elif field == "opaque":
            ctypes.c_uint32.from_address(address + 16).value = 1
        elif field == "guard":
            ctypes.c_ubyte.from_address(address + packet[15]).value = 0
        elif field == "identity":
            gpu.nvapi.selected["devid"] ^= 1
        else:
            auxiliary = gpu.observation["auxiliary"]
            auxiliary["group"]["806:1"]["value"] ^= 1
            gpu.observation["auxiliary"] = _aux_packets(auxiliary["feature"], auxiliary["group"])
        return 0

    gpu = FakeGPU(obs=auxiliary_observation(), actions=(None, None, None, None, action))
    result, error = gr.read(gpu, auxiliary=True)
    assert result is None and error
    assert len(gpu.calls) == (10 if field == "unstable" else 5)
