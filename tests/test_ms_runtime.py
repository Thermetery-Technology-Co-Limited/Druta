import copy
import json
import struct

import pytest

from druta import grpg_runtime as gr
from druta import lpwr_runtime as lp
from druta import ms_runtime as ms
from tests.test_grpg_runtime import (
    _attribute_reply,
    _aux_packets,
    _main_packets,
    main_observation,
    observed_main,
)
from tests.test_grpg_runtime import (
    bios as gr_bios,
)
from tests.test_lpwr_runtime import controller
from tests.test_vbios_fields import extend


def image(flags=(0x31, 0x31, 0x31)):
    data = bytearray(7 + 16 * len(flags))
    data[:4] = bytes((16, 7, 16, len(flags)))
    for i, flag in enumerate(flags):
        struct.pack_into("<I", data, 7 + 16 * i, flag)
    result = bytearray(extend(gr_bios(), [(0x84, 0x3A, 0x4A00, data)]))
    # Existing test image has three LPWR index rows at 0x4500.
    for i in range(3):
        result[0x4500 + 6 + 18 * i + 2] = 0 if i < 2 else 5
    return bytes(result)


def observation(**kwargs):
    value = main_observation(**kwargs)
    c = value["controllers"]
    c["2"] = controller(False)
    c["4"] = controller(True, 0x42)
    c["1"] = controller(True, 0x42)
    c["6"], c["16"] = controller(False), controller(False)
    value["support_hex"] = _attribute_reply(lp.SUPPORT, gr.CONTROLLERS, c).hex()
    ids = tuple(i for i in gr.CONTROLLERS if c[str(i)]["supported"] is True)
    value["states_hex"] = _attribute_reply(lp.STATES, ids, c).hex()
    aux = copy.deepcopy(value["auxiliary"])
    aux["group"]["800:1"] = {"available": True, "value": 1}
    aux["group"]["806:1"] = {"available": True, "value": 0x42}
    value["auxiliary"] = _aux_packets(aux["feature"], aux["group"])
    main = copy.deepcopy(value["main"])
    main["features"]["2"] = {"available": False, "value": None}
    main["bit4_group"]["805:1"]["value"] = 0x42
    value["main"] = _main_packets(c, main)
    return value


def prepare(monkeypatch, changes=None, img=None, obs=None):
    monkeypatch.setattr(ms, "read", lambda gpu: (observation() if obs is None else obs, None))
    return ms.contracts(None, image() if img is None else img, changes or {"lpwr.ms.0.enabled": False})


def actual(saved):
    surrogate = dict(saved, schema=6)
    return observed_main(surrogate)


def test_partial_roundtrip_and_peer_preservation(monkeypatch):
    saved = prepare(monkeypatch)
    saved = json.loads(json.dumps(saved))
    assert saved["after"]["controllers"]["1"]["configured_pstate_mask"] == 0
    assert saved["after"]["controllers"]["4"]["configured_pstate_mask"] == 0
    assert saved["after"]["auxiliary"]["group"]["806:1"]["value"] == 0
    assert saved["after"]["main"]["bit4_group"]["805:1"]["value"] == 0
    assert ms.verify(actual(saved), saved) == ["lpwr.ms.0.enabled"]
    assert ms.verify(saved["before"], saved, restore=True)
    bad = actual(saved)
    bad["identity"]["device_id"] ^= 1
    with pytest.raises(ms.MsError):
        ms.verify(bad, saved)


def test_shutdown(monkeypatch):
    saved = prepare(monkeypatch, {f"lpwr.ms.{i}.enabled": False for i in range(3)})
    after = saved["after"]
    assert after["controllers"]["4"]["supported"] is False
    assert after["controllers"]["1"]["configured_pstate_mask"] == 0
    assert after["auxiliary"]["group"]["800:1"]["value"] == 0
    assert ms.verify(actual(saved), saved)


def test_tampering_and_unresolved_loss(monkeypatch):
    saved = prepare(monkeypatch)
    saved["after"]["controllers"]["4"]["configured_pstate_mask"] = 8
    with pytest.raises(ms.MsError):
        ms.validate_contracts({"lpwr.ms.0.enabled": False}, saved)
    with pytest.raises(ms.MsError, match="aggregate flags"):
        prepare(monkeypatch, img=image((0x31, 0x21, 0x21)))


def test_independent_support_refused(monkeypatch):
    obs = observation()
    obs["controllers"]["6"] = controller(True, 0)
    obs["support_hex"] = _attribute_reply(lp.SUPPORT, gr.CONTROLLERS, obs["controllers"]).hex()
    ids = tuple(i for i in gr.CONTROLLERS if obs["controllers"][str(i)]["supported"] is True)
    obs["states_hex"] = _attribute_reply(lp.STATES, ids, obs["controllers"]).hex()
    main = copy.deepcopy(obs["main"])
    main["features"]["6"] = {"available": True, "value": 0}
    obs["main"] = _main_packets(obs["controllers"], main)
    with pytest.raises(ms.MsError, match="controllers 6 and 16"):
        prepare(monkeypatch, obs=obs)


def test_duplicate_pstate_id_retained_for_surviving_slot(monkeypatch):
    routes = ((3, 0x40, 0, 1), (12, 0x2, 1, 0), (27, 0x40, 2, 2))
    obs = observation(routes=routes, slot="0000:44:01.0", device=0x8888)
    img = bytearray(image())
    img[0x4500 + 6 + 2 * 18 + 2] = 2
    saved = prepare(monkeypatch, img=bytes(img), obs=obs)
    assert saved["after"]["controllers"]["4"]["configured_pstate_mask"] == 0x40
    assert saved["after"]["main"]["bit4_group"]["805:1"]["value"] == 0x40
    assert ms.verify(actual(saved), saved)


def test_shutdown_removes_unwitnessed_type7_group(monkeypatch):
    obs = observation()
    main = copy.deepcopy(obs["main"])
    for role in (0, 2):
        main["bit4_group"][f"801:{role}"]["value"] = 0
    main["bit4_group"]["805:0"]["value"] = 0
    obs["main"] = _main_packets(obs["controllers"], main)
    saved = prepare(monkeypatch, {f"lpwr.ms.{i}.enabled": False for i in range(3)}, obs=obs)
    assert all(not row["available"] for row in saved["after"]["main"]["bit4_group"].values())
    assert ms.verify(actual(saved), saved)


def test_unrouted_partial_disable_refused(monkeypatch):
    with pytest.raises(ms.MsError, match="no independently observable"):
        prepare(monkeypatch, {"lpwr.ms.1.enabled": False})


# Shared fake-only fixtures for registry receipt integration tests.
bios = image
make_contract = prepare
observed = actual


def test_malformed_table_and_restore_peer_change_rejected(monkeypatch):
    saved = prepare(monkeypatch)
    bad_contract = copy.deepcopy(saved)
    bad_contract["tables"]["ms"]["raw_hex"] = "not-hex"
    with pytest.raises(ms.MsError):
        ms.validate_contracts({"lpwr.ms.0.enabled": False}, bad_contract)
    bad_restore = copy.deepcopy(saved["before"])
    bad_restore["identity"]["device_id"] ^= 1
    with pytest.raises(ms.MsError):
        ms.verify(bad_restore, saved, restore=True)


def test_unavailable_unrelated_controller_is_preserved(monkeypatch):
    obs = observation()
    obs["controllers"]["1"] = controller(None, available=False)
    obs["support_hex"] = _attribute_reply(lp.SUPPORT, gr.CONTROLLERS, obs["controllers"]).hex()
    ids = tuple(i for i in gr.CONTROLLERS if obs["controllers"][str(i)]["supported"] is True)
    obs["states_hex"] = _attribute_reply(lp.STATES, ids, obs["controllers"]).hex()
    main = copy.deepcopy(obs["main"])
    main["features"]["1"] = {"available": False, "value": None}
    obs["main"] = _main_packets(obs["controllers"], main)
    saved = prepare(monkeypatch, obs=obs)
    assert saved["after"]["controllers"]["1"] == obs["controllers"]["1"]
    assert ms.verify(actual(saved), saved)


def partial_image(flags=(0x77D, 0x401, 0x401), difr_flags=0x31):
    difr = bytearray(11 + 18)
    difr[:4] = bytes((0x10, 11, 18, 1))
    struct.pack_into("<I", difr, 11, difr_flags)
    return extend(image(flags), [(0xE8, 0x8E, 0x4B00, difr)])


def partial_observation(**kwargs):
    value = observation(**kwargs)
    main = copy.deepcopy(value["main"])
    main["features"]["4"] = {"available": True, "value": 0x023971EF}
    value["main"] = _main_packets(value["controllers"], main)
    return value


def test_schema2_partial_aggregate_exact_and_restore(monkeypatch):
    obs = partial_observation(slot="0000:44:01.0", device=0x8888)
    saved = prepare(monkeypatch, img=partial_image(), obs=obs)
    assert saved["schema"] == 2 and "difr" in saved["tables"]
    after = saved["after"]
    assert after["main"]["features"]["4"] == {"available": True, "value": 0x020910CC}
    assert after["controllers"]["4"]["supported"] is True
    assert after["controllers"]["4"]["configured_pstate_mask"] == 0
    assert after["auxiliary"]["group"]["800:1"] == {"available": True, "value": 0}
    assert after["auxiliary"]["group"]["806:1"]["value"] == 0
    assert after["main"]["bit4_group"]["801:1"]["value"] == 1
    assert after["main"]["bit4_group"]["805:1"]["value"] == 0
    saved = json.loads(json.dumps(saved))
    assert ms.verify(actual(saved), saved) == ["lpwr.ms.0.enabled"]
    assert ms.verify(obs, saved, restore=True)


@pytest.mark.parametrize("flags,difr", [((0x87D, 0x401, 0x401), 0x31),
                                         ((0x77D, 0x411, 0x411), 0x31),
                                         ((0x77D, 0x401, 0x401), 0x21)])
def test_schema2_rejects_unknown_loss_and_missing_witness(monkeypatch, flags, difr):
    with pytest.raises(ms.MsError):
        prepare(monkeypatch, img=partial_image(flags, difr), obs=partial_observation())


def test_schema2_rejects_unavailable_feature_and_tamper(monkeypatch):
    obs = partial_observation()
    main = copy.deepcopy(obs["main"])
    main["features"]["4"] = {"available": True, "value": 0x00000100}
    obs["main"] = _main_packets(obs["controllers"], main)
    with pytest.raises(ms.MsError, match="feature predicate"):
        prepare(monkeypatch, img=partial_image(), obs=obs)
    saved = prepare(monkeypatch, img=partial_image(), obs=partial_observation())
    for mutate in (lambda v: v["tables"]["difr"].update(owner=1),
                   lambda v: v["after"]["main"]["features"]["4"].update(value=1),
                   lambda v: v["selected"]["lpwr.ms.0.enabled"].update(after=1)):
        bad = copy.deepcopy(saved)
        mutate(bad)
        with pytest.raises((ms.MsError, lp.LpwrError)):
            ms.validate_contracts({"lpwr.ms.0.enabled": False}, bad)


def test_schema2_read_error_preserves_other_controls(monkeypatch):
    monkeypatch.setattr(ms, "read", lambda gpu: (None, "transient"))
    with pytest.raises(ms.MsError, match="transient"):
        ms.contracts(None, partial_image(), {"lpwr.ms.0.enabled": False})


def test_schema2_alternate_flags_and_duplicate_routes(monkeypatch):
    routes = ((3, 0x40, 0, 1), (12, 0x2, 1, 0), (27, 0x40, 2, 2))
    obs = partial_observation(routes=routes, slot="0000:44:01.0", device=0x1234)
    img = bytearray(partial_image((0x47D, 0x401, 0x401)))
    img[0x4500 + 6 + 2 * 18 + 2] = 2
    saved = prepare(monkeypatch, img=bytes(img), obs=obs)
    assert saved["after"]["controllers"]["4"]["configured_pstate_mask"] == 0x40
    assert saved["after"]["main"]["features"]["4"]["value"] == 0x020910CC
    assert saved["after"]["main"]["bit4_group"]["805:1"]["value"] == 0
    assert ms.verify(actual(saved), saved)


def test_schema2_invalid_difr_bytes_refused(monkeypatch):
    saved = prepare(monkeypatch, img=partial_image(), obs=partial_observation())
    data = bytearray.fromhex(saved["tables"]["difr"]["raw_hex"])
    data[11] &= ~0x20
    saved["tables"]["difr"]["raw_hex"] = data.hex()
    with pytest.raises(ms.MsError, match="DIFR witness"):
        ms.validate_contracts({"lpwr.ms.0.enabled": False}, saved)


@pytest.mark.parametrize("flag,feature", [(0x04, 0x02), (0x08, 0x01),
                                            (0x20, 0x100), (0x40, 0x100000),
                                            (0x80, 0xC00000), (0x400, 0x1000)])
def test_schema2_refuses_inconsistent_before_feature(monkeypatch, flag, feature):
    # Remove the source from every row but retain its native feature bit.
    flags = tuple(value & ~flag for value in (0x77D, 0x401, 0x401))
    obs = partial_observation()
    main = copy.deepcopy(obs["main"])
    main["features"]["4"]["value"] |= feature
    obs["main"] = _main_packets(obs["controllers"], main)
    with pytest.raises(ms.MsError):
        prepare(monkeypatch, img=partial_image(flags), obs=obs)


def test_schema2_projects_alternate_feature_word(monkeypatch):
    obs = partial_observation()
    main = copy.deepcopy(obs["main"])
    main["features"]["4"]["value"] = 0x003971EF
    obs["main"] = _main_packets(obs["controllers"], main)
    saved = prepare(monkeypatch, img=partial_image(), obs=obs)
    assert saved["after"]["main"]["features"]["4"]["value"] == 0x000910CC
    assert ms.verify(actual(saved), saved)
