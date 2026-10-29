import copy
import json
import struct

import pytest

from druta import grpg_runtime as gr
from druta import lpwr_runtime as lp
from druta import media_lpwr_runtime as media
from tests.test_grpg_runtime import _attribute_reply, _bit1_packet, _main_packets, main_observation, observed_main
from tests.test_grpg_runtime import bios as gr_bios
from tests.test_lpwr_runtime import controller
from tests.test_vbios_fields import extend


def bios(family, flags=None):
    ptr, owner, _, _, _, route, _, _ = media._PROFILES[family]
    flags = flags if flags is not None else ((0xFFF7,) * 3 if family == "nvjpg" else (0xFFEB,) * 3)
    raw = bytearray(10 + len(flags) * 2)
    raw[:4] = bytes((0x10, 10, 2, len(flags)))
    for i, value in enumerate(flags):
        struct.pack_into("<H", raw, 10 + 2 * i, value)
    image = bytearray(extend(gr_bios(), [(ptr, owner, 0x4A00, raw)]))
    for i in range(3):
        image[0x4500 + 6 + 18 * i + route] = (0, 0, 2)[i]
    return bytes(image)


def observation(family, **kwargs):
    result = main_observation(**kwargs)
    _, _, native, alternate, role, _, _, _ = media._PROFILES[family]
    c = result["controllers"]
    c[str(native)] = controller(True, 0x142)
    if alternate is not None:
        c[str(alternate)] = controller(False)
    result["support_hex"] = _attribute_reply(lp.SUPPORT, gr.CONTROLLERS, c).hex()
    ids = tuple(i for i in gr.CONTROLLERS if c[str(i)]["supported"] is True)
    result["states_hex"] = _attribute_reply(lp.STATES, ids, c).hex()
    roles = copy.deepcopy(result["bit1"]["roles"])
    roles[str(role)] = {"available": True, "supported": True}
    result["bit1"] = _bit1_packet(roles)
    main = copy.deepcopy(result["main"])
    main["features"][str(native)] = {"available": True, "value": 0x3E060}
    if alternate is not None:
        main["features"][str(alternate)] = {"available": False, "value": None}
    result["main"] = _main_packets(c, main)
    return result


def make_contract(monkeypatch, family="nvenc", *, image=None, obs=None, changes=None):
    monkeypatch.setattr(media, "read", lambda gpu: (observation(family) if obs is None else obs, None))
    return media.contracts(
        None,
        bios(family) if image is None else image,
        {f"lpwr.{family}.0.enabled": False} if changes is None else changes,
    )


def observed(saved):
    return observed_main(dict(saved, schema=6))


@pytest.mark.parametrize("family", ["nvenc", "ofa", "nvjpg"])
def test_partial_and_shutdown_durable(monkeypatch, family):
    saved = json.loads(json.dumps(make_contract(monkeypatch, family)))
    native, role = str(media._PROFILES[family][2]), str(media._PROFILES[family][4])
    assert saved["after"]["controllers"][native]["configured_pstate_mask"] == 0x100
    assert saved["after"]["bit1"]["roles"][role]["supported"] is True
    assert media.verify(observed(saved), saved) == [f"lpwr.{family}.0.enabled"]
    assert media.verify(saved["before"], saved, restore=True)
    all_changes = {f"lpwr.{family}.{i}.enabled": False for i in range(3)}
    shutdown = make_contract(monkeypatch, family, changes=all_changes)
    assert shutdown["after"]["controllers"][native]["supported"] is False
    assert shutdown["after"]["main"]["features"][native] == {"available": False, "value": None}
    assert shutdown["after"]["bit1"]["roles"][role] == {"available": False, "supported": None}
    assert media.verify(observed(shutdown), shutdown) == sorted(all_changes)


@pytest.mark.parametrize("family", ["nvenc", "ofa", "nvjpg"])
def test_family_word_and_tamper(monkeypatch, family):
    saved = make_contract(monkeypatch, family)
    row = saved["selected"][f"lpwr.{family}.0.enabled"]
    assert row["before"] ^ row["after"] == (2 if family == "nvjpg" else 1)
    if family == "nvjpg":
        assert row["after"] & 1 == row["before"] & 1
    saved["family"] = "other"
    with pytest.raises(media.MediaLpwrError):
        media.validate_contracts({f"lpwr.{family}.0.enabled": False}, saved)


def test_mixed_family_rejected():
    with pytest.raises(media.MediaLpwrError):
        media.selected({"lpwr.nvenc.0.enabled": False, "lpwr.ofa.0.enabled": False})


@pytest.mark.parametrize("family", ["nvenc", "ofa", "nvjpg"])
def test_fallback_zero_and_duplicate_id(monkeypatch, family):
    _, _, native, _, _, route, _, _ = media._PROFILES[family]
    image = bytearray(bios(family))
    image[0x4500 + 6 + 2 * 18 + route] = 255
    obs = observation(family)
    obs["controllers"][str(native)]["configured_pstate_mask"] = 0x42
    ids = tuple(i for i in gr.CONTROLLERS if obs["controllers"][str(i)]["supported"] is True)
    obs["states_hex"] = _attribute_reply(lp.STATES, ids, obs["controllers"]).hex()
    saved = make_contract(monkeypatch, family, image=bytes(image), obs=obs)
    assert saved["after"]["controllers"][str(native)]["configured_pstate_mask"] == 0
    routes = ((3, 0x40, 0, 1), (12, 0x2, 1, 0), (27, 0x40, 2, 2))
    obs = observation(family, routes=routes, slot="0000:44:01.0", device=0x8888)
    obs["controllers"][str(native)]["configured_pstate_mask"] = 0x42
    ids = tuple(i for i in gr.CONTROLLERS if obs["controllers"][str(i)]["supported"] is True)
    obs["states_hex"] = _attribute_reply(lp.STATES, ids, obs["controllers"]).hex()
    saved = make_contract(monkeypatch, family, obs=obs)
    assert saved["after"]["controllers"][str(native)]["configured_pstate_mask"] == 0x40
    assert media.verify(observed(saved), saved)


@pytest.mark.parametrize("family", ["nvenc", "ofa", "nvjpg"])
def test_bad_flags_feature_and_peer_refused(monkeypatch, family):
    _, _, native, alternate, role, _, _, _ = media._PROFILES[family]
    flags = (0xFFF7, 0xFFD7, 0xFFD7) if family == "nvjpg" else (0xFFEB, 0xFFCB, 0xFFCB)
    with pytest.raises(media.MediaLpwrError, match="aggregate flags"):
        make_contract(monkeypatch, family, image=bios(family, flags))
    obs = observation(family)
    main = copy.deepcopy(obs["main"])
    main["features"][str(native)] = {"available": False, "value": None}
    obs["main"] = _main_packets(obs["controllers"], main)
    with pytest.raises(media.MediaLpwrError, match="feature readback"):
        make_contract(monkeypatch, family, obs=obs)
    obs = observation(family)
    roles = copy.deepcopy(obs["bit1"]["roles"])
    for key in roles:
        if key != str(role):
            roles[key] = {"available": False, "supported": None}
    obs["bit1"] = _bit1_packet(roles)
    changes = {f"lpwr.{family}.{i}.enabled": False for i in range(3)}
    saved = json.loads(json.dumps(make_contract(monkeypatch, family, obs=obs, changes=changes)))
    assert saved["after"]["bit1"]["roles"] == {
        str(i): {"available": False, "supported": None} for i in gr.CONTROLLERS[:11]
    }
    assert saved["after"]["controllers"][str(native)]["supported"] is False
    assert media.verify(observed(saved), saved) == sorted(changes)
    assert media.verify(saved["before"], saved, restore=True) == sorted(changes)
    if alternate is not None:
        obs = observation(family)
        obs["controllers"][str(alternate)] = controller(True, 0)
        obs["support_hex"] = _attribute_reply(lp.SUPPORT, gr.CONTROLLERS, obs["controllers"]).hex()
        ids = tuple(i for i in gr.CONTROLLERS if obs["controllers"][str(i)]["supported"] is True)
        obs["states_hex"] = _attribute_reply(lp.STATES, ids, obs["controllers"]).hex()
        main = copy.deepcopy(obs["main"])
        main["features"][str(alternate)] = {"available": True, "value": 0}
        obs["main"] = _main_packets(obs["controllers"], main)
        with pytest.raises(media.MediaLpwrError, match="Alternate media"):
            make_contract(monkeypatch, family, obs=obs)


def test_unrouted_and_get_failure_refused(monkeypatch):
    with pytest.raises(media.MediaLpwrError, match="no independently observable"):
        make_contract(monkeypatch, changes={"lpwr.nvenc.1.enabled": False})
    monkeypatch.setattr(media, "read", lambda gpu: (None, "transient read failed"))
    with pytest.raises(media.MediaLpwrError, match="transient read failed"):
        media.contracts(None, bios("nvenc"), {"lpwr.nvenc.0.enabled": False})


@pytest.mark.parametrize("family", ["nvenc", "ofa", "nvjpg"])
def test_missing_role_or_already_unavailable(monkeypatch, family):
    obs = observation(family)
    obs["bit1"] = _bit1_packet({str(i): {"available": False, "supported": None} for i in gr.CONTROLLERS[:11]})
    changes = {f"lpwr.{family}.{i}.enabled": False for i in range(3)}
    with pytest.raises(media.MediaLpwrError, match="role support"):
        make_contract(monkeypatch, family, obs=obs, changes=changes)
    flags = (0xFFF3,) * 3 if family == "nvjpg" else (0xFFE3,) * 3
    saved = make_contract(monkeypatch, family, image=bios(family, flags), obs=obs, changes=changes)
    assert saved["after"]["bit1"] == gr._observation(obs, allow_missing_aux_feature=True)["bit1"]
    assert media.verify(observed(saved), saved)
