"""Constant BIOS limits: exact definitions, retained requests, computed outputs."""

import copy
import ctypes
import struct

import pytest

from druta import voltage_ceiling_runtime as ceiling
from tests import test_vfe_equation_runtime as native
from tests.test_voltage_bios_runtime import GPU


def controls(policy):
    result = {"identity": copy.deepcopy(policy["identity"]), "rails": {}}
    for index, row in policy["rails"].items():
        raw = bytearray(2760)
        struct.pack_into("<II", raw, 0, 0x20AC8, 1 << int(index))
        at = 0x48 + int(index) * 0x54
        struct.pack_into("<I", raw, at, row["control_type"])
        for j, name in enumerate(ceiling.voltage._OFFSETS):
            struct.pack_into("<i", raw, at + 4 + 4 * j, row["offsets_uv"][name])
        struct.pack_into("<ii", raw, at + 20, 1234, -4567)
        result["rails"][index] = {"version": 0x20AC8, "payload_hex": raw.hex()}
    return result


def observation(role="OV", rail=0, *, boost=0, **identity):
    current, nxt = (1200000, 65535) if role == "OV" else (20000, 401)
    eq = native.observation(before=current, next_index=nxt, **identity)
    gpu = GPU()
    policy, error = ceiling.voltage.read(gpu)
    assert error is None
    policy["identity"] = copy.deepcopy(eq["identity"])
    policy["rails"][str(rail)]["offsets_uv"]["overvoltage"] = 50000
    return {"equations": eq, "policy": policy, "boost_percent": boost, "rail_control": controls(policy)}


def inspection(role="OV", rail=0):
    current, nxt = (1200000, 65535) if role == "OV" else (20000, 401)
    key = f"voltage.{rail}." + ("OV.constant" if role == "OV" else "ALT.addend")
    return {
        "fields": [
            {
                "key": key,
                "rail": ("NVVDD", "MSVDD")[rail],
                "role": role,
                "equation": 37,
                "equation_type": 1,
                "next_equation": nxt,
                "variable_index": 22,
                "output_type": 2,
                "shared": False,
                "sharing_graph_complete": True,
                "offsets": [1003, 1007, 1011],
                "current": current,
                "units": "uV",
                "capability": "editable",
            },
            {
                "key": f"voltage.{rail}.REL.maximum",
                "equation": 401,
                "role": "REL",
                "rail": ("NVVDD", "MSVDD")[rail],
            },
        ]
    }


def contract(monkeypatch, role="OV", rail=0, *, obs=None, bios=None, changes=None):
    obs = observation(role, rail) if obs is None else obs
    bios = inspection(role, rail) if bios is None else bios
    key = bios["fields"][0]["key"]
    changes = {key: bios["fields"][0]["current"] + 5000} if changes is None else changes
    monkeypatch.setattr(ceiling, "read", lambda gpu: (copy.deepcopy(obs), None))
    return ceiling.contracts(object(), bios, changes)


@pytest.mark.parametrize("role,rail", [("OV", 0), ("OV", 1), ("ALT", 0), ("ALT", 1)])
def test_exact_projection_and_restoration(monkeypatch, role, rail):
    saved = contract(monkeypatch, role, rail)
    key = next(iter(saved["selected"]))
    ceiling.validate_contracts({key: saved["selected"][key]["current"] + 5000}, saved)
    assert ceiling.verify(saved["after"], saved) == [key]
    assert ceiling.verify(saved["before"], saved, restore=True) == [key]
    assert saved["before"]["equations"]["info_hex"] == saved["after"]["equations"]["info_hex"]
    name = "overvoltage" if role == "OV" else "alt_reliability"
    assert (
        saved["after"]["policy"]["rails"][str(rail)]["policy_uv"][name]
        == saved["before"]["policy"]["rails"][str(rail)]["policy_uv"][name] + 5000
    )


@pytest.mark.parametrize("boost", [0, 37, 100])
def test_alt_shared_derived_outputs_may_change_with_fixed_request(monkeypatch, boost):
    saved = contract(monkeypatch, "ALT", obs=observation("ALT", boost=boost))
    after = copy.deepcopy(saved["after"])
    for row in after["policy"]["rails"].values():
        row["boost_uv"] += 5000
        row["policy_uv"]["reliability"] += 2500
        row["policy_uv"]["effective"] += 1000
    assert ceiling.verify(after, saved) == ["voltage.0.ALT.addend"]
    restored = copy.deepcopy(saved["before"])
    restored["policy"]["rails"]["0"]["boost_uv"] += 1
    with pytest.raises(ceiling.VoltageCeilingError):
        ceiling.verify(restored, saved, restore=True)


def test_ov_effective_may_recompute_without_assuming_min_formula(monkeypatch):
    saved = contract(monkeypatch)
    after = copy.deepcopy(saved["after"])
    after["policy"]["rails"]["0"]["policy_uv"]["effective"] = 1100000
    assert ceiling.verify(after, saved)
    after["policy"]["rails"]["1"]["policy_uv"]["effective"] -= 1000
    with pytest.raises(ceiling.VoltageCeilingError):
        ceiling.verify(after, saved)


@pytest.mark.parametrize("role", ["OV", "ALT"])
@pytest.mark.parametrize(
    "field", ["offset", "peer_alt", "vmin", "ov", "boost_request", "native", "identity", "quantized"]
)
def test_unexpected_state_is_rejected(monkeypatch, role, field):
    saved = contract(monkeypatch, role)
    after = copy.deepcopy(saved["after"])
    row = after["policy"]["rails"]["0"]
    if field == "offset":
        row["offsets_uv"]["reliability"] += 1
    elif field == "peer_alt":
        after["policy"]["rails"]["1"]["policy_uv"]["alt_reliability"] += 1
    elif field in ("vmin", "ov"):
        row["policy_uv"]["vmin" if field == "vmin" else "overvoltage"] += 1
    elif field == "boost_request":
        after["boost_percent"] = 1
    elif field == "native":
        native.mutate(after["equations"], "control", native.vfe.CONTROL_SIZE - 1, 23)
    elif field == "identity":
        after["policy"]["identity"]["device_id"] += 1
    else:
        row["policy_uv"]["overvoltage" if role == "OV" else "alt_reliability"] += 2500
    with pytest.raises(RuntimeError):
        ceiling.verify(after, saved)


@pytest.mark.parametrize("arch", [4, 5, 6, 7, 8, 9, 10])
def test_partial_capability_and_alternate_adapter(monkeypatch, arch):
    obs = observation(architecture=arch, device_id=14, subsystem_id=0x23456789, slot="0000:21:1b.7")
    obs["policy"]["rails"].pop("1")
    obs["rail_control"]["rails"].pop("1")
    obs["boost_percent"] = None  # OV does not depend on this unrelated export.
    saved = contract(monkeypatch, obs=obs)
    assert ceiling.verify(saved["after"], saved)


def test_alt_needs_boost_request_but_not_unrelated_rail(monkeypatch):
    obs = observation("ALT")
    obs["policy"]["rails"].pop("1")
    obs["rail_control"]["rails"].pop("1")
    assert contract(monkeypatch, "ALT", obs=obs)
    obs["boost_percent"] = None
    with pytest.raises(ceiling.VoltageCeilingError, match="boost request GET"):
        contract(monkeypatch, "ALT", obs=obs)


@pytest.mark.parametrize(
    "name,value",
    [
        ("shared", True),
        ("sharing_graph_complete", False),
        ("equation_type", 7),
        ("role", "REL"),
        ("offsets", [1003, 1008, 1011]),
        ("next_equation", 401),
        ("variable_index", True),
        ("current", 1195000),
        ("output_type", True),
        ("capability", "read-only"),
    ],
)
def test_bad_rom_binding_refuses(monkeypatch, name, value):
    bios = inspection()
    bios["fields"][0][name] = value
    with pytest.raises(RuntimeError):
        contract(monkeypatch, bios=bios)


def test_alt_must_reference_its_own_rel(monkeypatch):
    bios = inspection("ALT")
    bios["fields"][1]["equation"] = 400
    with pytest.raises(ceiling.VoltageCeilingError, match="linked"):
        contract(monkeypatch, "ALT", bios=bios)


@pytest.mark.parametrize("value", [True, -1, 1.5, float("nan"), float("inf"), 0x7FFFFFFF, "1200000"])
def test_bad_requested_value(monkeypatch, value):
    with pytest.raises(RuntimeError):
        contract(monkeypatch, changes={"voltage.0.OV.constant": value})


def test_zero_alt_is_supported(monkeypatch):
    saved = contract(monkeypatch, "ALT", changes={"voltage.0.ALT.addend": 0})
    assert ceiling.verify(saved["after"], saved)


@pytest.mark.parametrize("path", ["schema", "selected", "native", "peer", "after", "before"])
def test_receipt_corruption_is_detected_without_hardware(monkeypatch, path):
    saved = contract(monkeypatch)
    if path == "schema":
        saved["schema"] = True
    elif path == "selected":
        saved["selected"]["voltage.0.OV.constant"]["variable_index"] += 1
    elif path == "native":
        saved["equation_selections"]["voltage.0.OV.constant"]["after_uv"] += 1
    elif path == "peer":
        saved["after"]["policy"]["rails"]["1"]["offsets_uv"]["vmin"] += 1
    elif path == "after":
        saved["after"]["policy"]["rails"]["0"]["policy_uv"]["effective"] += 1
    else:
        saved["before"]["policy"]["identity"]["slot"] = "0000:45:00.0"
    with pytest.raises(RuntimeError):
        ceiling.validate_contracts({"voltage.0.OV.constant": 1205000}, saved)


def test_changed_inspection_and_mixed_changes_refuse(monkeypatch):
    saved = contract(monkeypatch)
    bios = inspection()
    bios["fields"][0]["offsets"] = [2003, 2007, 2011]
    with pytest.raises(ceiling.VoltageCeilingError, match="binding changed"):
        ceiling.validate_inspection(bios, {"voltage.0.OV.constant": 1205000}, saved)
    with pytest.raises(ceiling.VoltageCeilingError, match="separately"):
        contract(monkeypatch, changes={"voltage.0.OV.constant": 1205000, "voltage.0.VMIN": 800000})


def test_unselected_controls_need_no_voltage_capability():
    assert ceiling.contracts(None, None, {"power.0.maximum": 999000}) == {}
    assert ceiling.verify(None, {}) == []
    assert ceiling.validate_contracts({}, {}) is None


def boost_gpu(monkeypatch, action=None):
    gpu = GPU()

    def getter(handle, pointer):
        data = (ctypes.c_ubyte * 40).from_address(ctypes.cast(pointer, ctypes.c_void_p).value)
        assert data[4] == 255 and not any(data[5:])
        data[4] = 27
        return action(data) if action else 0

    gpu.nvapi.VoltCtrlGet = getter
    return gpu


def test_boost_reader_uses_one_byte_and_preserves_caller_memory(monkeypatch):
    assert ceiling._boost(boost_gpu(monkeypatch)) == 27
    gpu = boost_gpu(monkeypatch)
    gpu.nvapi.VoltCtrlGet = None
    assert ceiling._boost(gpu) is None


@pytest.mark.parametrize("mode", ["error", "missing", "padding", "version", "overrun"])
def test_boost_malformed_reply(monkeypatch, mode):
    def action(data):
        if mode == "error":
            return -1
        if mode == "missing":
            data[4] = 255
        elif mode == "padding":
            data[5] = 3
        elif mode == "version":
            data[0] = 0
        else:
            ctypes.c_ubyte.from_address(ctypes.addressof(data) + 40).value = 0
        return 0

    with pytest.raises(RuntimeError):
        ceiling._boost(boost_gpu(monkeypatch, action))


def test_combined_reader_retries_transients_and_rejects_drift(monkeypatch):
    gpu, obs = GPU(), observation()
    calls = []

    def once(_):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("temporary")
        return copy.deepcopy(obs)

    monkeypatch.setattr(ceiling, "_once", once)
    assert ceiling.read(gpu) == (obs, None) and len(calls) == 3

    def drift(_):
        calls.append(1)
        value = copy.deepcopy(obs)
        value["boost_percent"] = len(calls)
        return value

    monkeypatch.setattr(ceiling, "_once", drift)
    assert ceiling.read(gpu)[0] is None and len(calls) == 7


@pytest.mark.parametrize("status", [-3, -9, -104])
def test_boost_export_present_but_unsupported_keeps_ov_capability(monkeypatch, status):
    gpu = boost_gpu(monkeypatch, lambda data: status)
    assert ceiling._boost(gpu) is None
    obs = observation(boost=None)
    assert contract(monkeypatch, obs=obs)


@pytest.mark.parametrize("offset", [20, 24, 60])
def test_additional_rail_offsets_and_opaque_bytes_must_survive(monkeypatch, offset):
    saved = contract(monkeypatch)
    actual = copy.deepcopy(saved["after"])
    block = actual["rail_control"]["rails"]["0"]
    raw = bytearray.fromhex(block["payload_hex"])
    raw[0x48 + offset] ^= 1
    block["payload_hex"] = raw.hex()
    with pytest.raises(RuntimeError):
        ceiling.verify(actual, saved)


@pytest.mark.parametrize("role", ["OV", "ALT"])
def test_noop_field_does_not_relax_derived_output_checks(monkeypatch, role):
    bios = inspection(role)
    f = bios["fields"][0]
    saved = contract(monkeypatch, role, bios=bios, changes={f["key"]: f["current"]})
    assert ceiling.verify(saved["after"], saved)
    changed = copy.deepcopy(saved["after"])
    changed["policy"]["rails"]["0"]["policy_uv"]["effective"] += 1
    with pytest.raises(RuntimeError):
        ceiling.verify(changed, saved)
