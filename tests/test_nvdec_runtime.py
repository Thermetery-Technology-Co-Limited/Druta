import copy
import json
import struct

import pytest

from druta import grpg_runtime as gr
from druta import lpwr_runtime as lp
from druta import nvdec_runtime as nv
from tests.test_grpg_runtime import _attribute_reply, _bit1_packet, _main_packets, main_observation, observed_main
from tests.test_grpg_runtime import bios as gr_bios
from tests.test_lpwr_runtime import controller
from tests.test_vbios_fields import extend


def bios(flags=(0xFFEB, 0xFFEB, 0xFFEB)):
    raw = bytearray(10 + len(flags) * 2)
    raw[:4] = bytes((0x10, 10, 2, len(flags)))
    for i, value in enumerate(flags):
        struct.pack_into("<H", raw, 10 + 2 * i, value)
    image = bytearray(extend(gr_bios(), [(0x11C, 0xB4, 0x4A00, raw)]))
    for i in range(3):
        image[0x4500 + 6 + 18 * i + 13] = (0, 0, 2)[i]
    return bytes(image)


def observation(**kwargs):
    result = main_observation(**kwargs)
    controllers = result["controllers"]
    controllers["12"], controllers["17"] = controller(True, 0x142), controller(False)
    result["support_hex"] = _attribute_reply(lp.SUPPORT, gr.CONTROLLERS, controllers).hex()
    indices = tuple(i for i in gr.CONTROLLERS if controllers[str(i)]["supported"] is True)
    result["states_hex"] = _attribute_reply(lp.STATES, indices, controllers).hex()
    roles = copy.deepcopy(result["bit1"]["roles"])
    roles["4"] = {"available": True, "supported": True}
    result["bit1"] = _bit1_packet(roles)
    main = copy.deepcopy(result["main"])
    main["features"]["12"] = {"available": True, "value": 0x3E005}
    main["features"]["17"] = {"available": False, "value": None}
    result["main"] = _main_packets(controllers, main)
    return result


def make_contract(monkeypatch, *, image=None, obs=None, changes=None):
    monkeypatch.setattr(nv, "read", lambda gpu: (observation() if obs is None else obs, None))
    return nv.contracts(
        None, bios() if image is None else image, {"lpwr.nvdec.0.enabled": False} if changes is None else changes
    )


def observed(saved):
    return observed_main(dict(saved, schema=6))


def test_partial_exact_and_durable(monkeypatch):
    saved = json.loads(json.dumps(make_contract(monkeypatch)))
    assert saved["after"]["controllers"]["12"]["configured_pstate_mask"] == 0x100
    assert saved["after"]["controllers"]["12"]["supported"] is True
    assert saved["after"]["main"]["features"]["12"]["value"] == 0x3E005
    assert nv.verify(observed(saved), saved) == ["lpwr.nvdec.0.enabled"]
    assert nv.verify(saved["before"], saved, restore=True)


def test_shutdown_projection(monkeypatch):
    saved = make_contract(monkeypatch, changes={f"lpwr.nvdec.{i}.enabled": False for i in range(3)})
    assert saved["after"]["controllers"]["12"]["supported"] is False
    assert saved["after"]["main"]["features"]["12"] == {"available": False, "value": None}
    assert saved["after"]["bit1"]["roles"]["4"] == {"available": False, "supported": None}
    assert nv.verify(observed(saved), saved)


def test_invalid_or_unwitnessed_changes(monkeypatch):
    with pytest.raises(nv.NvdecError):
        make_contract(monkeypatch, changes={"lpwr.nvdec.0.enabled": True})
    with pytest.raises(nv.NvdecError, match="aggregate flags"):
        make_contract(monkeypatch, image=bios((0xFFEB, 0xFFCB, 0xFFCB)))
    saved = make_contract(monkeypatch)
    saved["after"]["controllers"]["12"]["configured_pstate_mask"] ^= 1
    with pytest.raises(nv.NvdecError):
        nv.validate_contracts({"lpwr.nvdec.0.enabled": False}, saved)


def test_alt_adapter_and_exact_restore(monkeypatch):
    saved = make_contract(monkeypatch, obs=observation(slot="0000:72:1d.3", device=0x8888))
    wrong = copy.deepcopy(saved["before"])
    wrong["identity"]["device_id"] ^= 1
    with pytest.raises(nv.NvdecError):
        nv.verify(wrong, saved, restore=True)


def test_duplicate_id_survives_selected_route(monkeypatch):
    routes = ((3, 0x40, 0, 1), (12, 0x2, 1, 0), (27, 0x40, 2, 2))
    obs = observation(routes=routes)
    obs["controllers"]["12"]["configured_pstate_mask"] = 0x42
    ids = tuple(i for i in gr.CONTROLLERS if obs["controllers"][str(i)]["supported"] is True)
    obs["states_hex"] = _attribute_reply(lp.STATES, ids, obs["controllers"]).hex()
    saved = make_contract(monkeypatch, obs=obs)
    assert saved["after"]["controllers"]["12"]["configured_pstate_mask"] == 0x40
    assert nv.verify(observed(saved), saved)


def test_no_type4_witness_rejects_shutdown(monkeypatch):
    obs = observation()
    roles = copy.deepcopy(obs["bit1"]["roles"])
    for role in roles:
        if role != "4":
            roles[role] = {"available": False, "supported": None}
    obs["bit1"] = _bit1_packet(roles)
    with pytest.raises(nv.NvdecError, match="peer support"):
        make_contract(monkeypatch, obs=obs, changes={f"lpwr.nvdec.{i}.enabled": False for i in range(3)})


def test_unknown_peer_change_is_rejected(monkeypatch):
    saved = make_contract(monkeypatch)
    altered = observed(saved)
    roles = copy.deepcopy(altered["bit1"]["roles"])
    roles["4"] = {"available": False, "supported": None}
    altered["bit1"] = _bit1_packet(roles)
    with pytest.raises(nv.NvdecError, match="dependency readback"):
        nv.verify(altered, saved)


def test_unsupported_row_class_cannot_be_selected(monkeypatch):
    with pytest.raises(nv.NvdecError, match="controller12 contributor"):
        make_contract(monkeypatch, image=bios((0xFFE5, 0xFFEB, 0xFFEB)))


def test_unexplained_before_mask_is_refused(monkeypatch):
    obs = observation()
    obs["controllers"]["12"]["configured_pstate_mask"] |= 0x8000
    ids = tuple(i for i in gr.CONTROLLERS if obs["controllers"][str(i)]["supported"] is True)
    obs["states_hex"] = _attribute_reply(lp.STATES, ids, obs["controllers"]).hex()
    with pytest.raises(nv.NvdecError, match="all routes"):
        make_contract(monkeypatch, obs=obs)


@pytest.mark.parametrize("selector", [5, 255, 6, 4])
def test_fallback_and_unloaded_slot_are_zero(monkeypatch, selector):
    img = bytearray(bios())
    img[0x4500 + 6 + 2 * 18 + 13] = selector
    obs = observation()
    obs["controllers"]["12"]["configured_pstate_mask"] = 0x42
    ids = tuple(i for i in gr.CONTROLLERS if obs["controllers"][str(i)]["supported"] is True)
    obs["states_hex"] = _attribute_reply(lp.STATES, ids, obs["controllers"]).hex()
    saved = make_contract(monkeypatch, image=bytes(img), obs=obs)
    assert saved["after"]["controllers"]["12"]["configured_pstate_mask"] == 0


def test_unloaded_lpwr_index_uses_fallback(monkeypatch):
    obs = observation(routes=((3, 0x40, 0, 1), (12, 0x2, 1, 0), (27, 0x100, 2, 4)))
    obs["controllers"]["12"]["configured_pstate_mask"] = 0x42
    ids = tuple(i for i in gr.CONTROLLERS if obs["controllers"][str(i)]["supported"] is True)
    obs["states_hex"] = _attribute_reply(lp.STATES, ids, obs["controllers"]).hex()
    saved = make_contract(monkeypatch, obs=obs)
    assert saved["after"]["controllers"]["12"]["configured_pstate_mask"] == 0


def test_bit1_priority_and_opaque_word_preserved(monkeypatch):
    saved = make_contract(monkeypatch, image=bios((0xFFFF, 0xFFFF, 0xFFFF)))
    selected = saved["selected"]["lpwr.nvdec.0.enabled"]
    assert selected["before"] == 0xFFFF and selected["after"] == 0xFFFE
    assert saved["after"]["controllers"]["17"]["supported"] is False
    assert saved["after"]["bit1"]["roles"]["4"] == {"available": True, "supported": True}


def test_role4_mismatch_and_feature_unavailable_refused(monkeypatch):
    obs = observation()
    roles = copy.deepcopy(obs["bit1"]["roles"])
    roles["4"] = {"available": False, "supported": None}
    obs["bit1"] = _bit1_packet(roles)
    with pytest.raises(nv.NvdecError, match="role4 support"):
        make_contract(monkeypatch, obs=obs)
    obs = observation()
    main = copy.deepcopy(obs["main"])
    main["features"]["12"] = {"available": False, "value": None}
    obs["main"] = _main_packets(obs["controllers"], main)
    with pytest.raises(nv.NvdecError, match="feature readback"):
        make_contract(monkeypatch, obs=obs)


def test_get_error_and_word_tamper(monkeypatch):
    monkeypatch.setattr(nv, "read", lambda gpu: (None, "transient unavailable"))
    with pytest.raises(nv.NvdecError, match="transient unavailable"):
        nv.contracts(None, bios(), {"lpwr.nvdec.0.enabled": False})
    saved = make_contract(monkeypatch)
    saved["selected"]["lpwr.nvdec.0.enabled"]["after"] ^= 0x100
    with pytest.raises(nv.NvdecError):
        nv.validate_contracts({"lpwr.nvdec.0.enabled": False}, saved)


def test_supported_17_profile_is_not_admitted(monkeypatch):
    obs = observation()
    obs["controllers"]["17"] = controller(True, 0)
    obs["support_hex"] = _attribute_reply(lp.SUPPORT, gr.CONTROLLERS, obs["controllers"]).hex()
    ids = tuple(i for i in gr.CONTROLLERS if obs["controllers"][str(i)]["supported"] is True)
    obs["states_hex"] = _attribute_reply(lp.STATES, ids, obs["controllers"]).hex()
    main = copy.deepcopy(obs["main"])
    main["features"]["17"] = {"available": True, "value": 0}
    obs["main"] = _main_packets(obs["controllers"], main)
    with pytest.raises(nv.NvdecError, match="unsupported controller17"):
        make_contract(monkeypatch, obs=obs)
