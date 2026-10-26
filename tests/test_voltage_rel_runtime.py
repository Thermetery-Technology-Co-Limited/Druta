"""Synthetic REL definitions and rail policies; no NVIDIA DLL or GPU access."""

import copy
import json
import struct

import pytest

from druta import vfe_equation_runtime as equations
from druta import voltage_rel_runtime as rel
from tests import test_vfe_equation_runtime as native
from tests import test_voltage_ceiling_runtime as ceiling


def observation(rail=0, *, boost=0, partial=False, **identity):
    result = ceiling.observation(rail=rail, boost=boost, **identity)
    result["equations"] = native.clamp_observation(**identity)
    if partial:
        result["policy"]["rails"].pop(str(1 - rail))
        result["rail_control"]["rails"].pop(str(1 - rail))
    return result


def root(rail=0, role="REL", equation=37, *, dependencies=None):
    return {
        "rail_index": rail,
        "rail_identity": rail + 1,
        "role": role,
        "selector_offset": 800 + 32 * rail + (2 if role == "ALT" else 0),
        "equation": equation,
        "dependency_indices": sorted({37, equation}) if dependencies is None else dependencies,
    }


def inspection(obs=None, rail=0, *, roots=None, index=37, operation="maximum"):
    obs = observation(rail) if obs is None else obs
    row = equations.upper_clamp_row(obs["equations"], index)
    binding = {key: value for key, value in row.items() if key != "upper_uv"}
    binding["before_uv"] = row["upper_uv"]
    return {
        "fields": [
            {
                "key": f"voltage.{rail}.REL.{operation}",
                "rail": ("NVVDD", "MSVDD")[rail],
                "role": "REL",
                "equation": index,
                "equation_type": 7,
                "equation_offset": 1000 + index * 25,
                "sharing_graph_complete": True,
                "offsets": [1003 + index * 25, 1007 + index * 25] if operation == "value" else [1007 + index * 25],
                "current": row["upper_uv"],
                "units": "uV",
                "capability": "editable",
                "upper_clamp_binding": binding,
                "affected_rail_roots": [root(rail, equation=index, dependencies=[index])] if roots is None else roots,
                "affected_clock_roots": [],
            }
        ]
    }


def contract(monkeypatch, rail=0, *, obs=None, bios=None, changes=None):
    obs = observation(rail) if obs is None else obs
    bios = inspection(obs, rail) if bios is None else bios
    changes = {bios["fields"][0]["key"]: bios["fields"][0]["current"] - 5000} if changes is None else changes
    monkeypatch.setattr(rel, "read", lambda gpu: (copy.deepcopy(obs), None))
    return rel.contracts(object(), bios, changes)


@pytest.mark.parametrize("rail", [0, 1])
def test_exact_upper_only_projection_accepts_unchanged_computed_rel(monkeypatch, rail):
    obs = observation(rail)
    original = copy.deepcopy(obs)
    saved = contract(monkeypatch, rail, obs=obs)
    assert obs == original
    assert json.loads(json.dumps(saved, allow_nan=False)) == saved
    assert set(saved["native"]) == {"schema", "kind", "selected"}
    assert saved["after"]["policy"] == saved["before"]["policy"]
    assert saved["after"]["equations"]["info_hex"] == obs["equations"]["info_hex"]
    projected = bytearray.fromhex(obs["equations"]["control_hex"])
    struct.pack_into("<f", projected, equations.HEADER + 37 * equations.CONTROL_STRIDE + 8, 1115000)
    assert bytes.fromhex(saved["after"]["equations"]["control_hex"]) == projected
    assert rel.verify(saved["after"], saved) == [f"voltage.{rail}.REL.maximum"]
    assert rel.verify(saved["before"], saved, restore=True) == [f"voltage.{rail}.REL.maximum"]


@pytest.mark.parametrize("boost", [0, 1, 37, 100])
def test_linked_alt_crossrail_rel_and_effective_are_observations(monkeypatch, boost):
    obs = observation(boost=boost)
    roots = [root(), root(0, "ALT", 401), root(1, "REL", 7999), root(1, "ALT", 401)]
    saved = contract(monkeypatch, obs=obs, bios=inspection(obs, roots=roots))
    active = copy.deepcopy(saved["after"])
    for row in active["policy"]["rails"].values():
        row["policy_uv"]["reliability"] -= 1234
        row["policy_uv"]["alt_reliability"] -= 3456
        row["policy_uv"]["effective"] -= 7890
        if boost:
            row["boost_uv"] += 99
    assert rel.verify(active, saved) == list(saved["selected"])
    with pytest.raises(RuntimeError):
        rel.verify(active, saved, restore=True)


@pytest.mark.parametrize("boost", [1, 37, 100])
def test_nonzero_request_pure_contract_allows_shared_boost_crossrail_recompute(monkeypatch, boost):
    saved = contract(monkeypatch, obs=observation(boost=boost))
    active = copy.deepcopy(saved["after"])
    for row in active["policy"]["rails"].values():
        row["boost_uv"] += 2500
        row["policy_uv"]["reliability"] += 5000
        row["policy_uv"]["effective"] -= 5000
    assert rel.verify(active, saved) == list(saved["selected"])
    active["policy"]["rails"]["1"]["policy_uv"]["alt_reliability"] += 1
    with pytest.raises(rel.VoltageRelError, match="preserved"):
        rel.verify(active, saved)


@pytest.mark.parametrize(
    "rail,section,name",
    [
        (0, None, "boost_uv"),
        (1, None, "boost_uv"),
        (1, "policy_uv", "reliability"),
        (1, "policy_uv", "effective"),
        (0, "policy_uv", "alt_reliability"),
        (1, "policy_uv", "alt_reliability"),
        (0, "policy_uv", "overvoltage"),
        (1, "policy_uv", "overvoltage"),
        (0, "policy_uv", "vmin"),
        (1, "policy_uv", "vmin"),
        (0, "offsets_uv", "reliability"),
        (1, "offsets_uv", "vmin"),
    ],
)
def test_zero_boost_retains_unaffected_limits_offsets_and_boost(monkeypatch, rail, section, name):
    saved = contract(monkeypatch)
    active = copy.deepcopy(saved["after"])
    row = active["policy"]["rails"][str(rail)]
    (row if section is None else row[section])[name] += 1
    with pytest.raises(RuntimeError):
        rel.verify(active, saved)


@pytest.mark.parametrize("field", ["reliability", "alt_reliability", "effective", "boost_uv"])
@pytest.mark.parametrize("boost", [0, 37])
def test_noop_never_relaxes_computed_state(monkeypatch, field, boost):
    obs = observation(boost=boost)
    bios = inspection(obs, roots=[root(), root(0, "ALT", 401)])
    saved = contract(monkeypatch, obs=obs, bios=bios, changes={"voltage.0.REL.maximum": 1120000})
    active = copy.deepcopy(saved["after"])
    row = active["policy"]["rails"]["0"]
    (row if field == "boost_uv" else row["policy_uv"])[field] += 1
    with pytest.raises(rel.VoltageRelError, match="preserved"):
        rel.verify(active, saved)


@pytest.mark.parametrize("restore", [False, True])
@pytest.mark.parametrize(
    "location",
    ["native_info", "native_control", "native_lower", "native_coefficient", "rail_opaque", "boost", "identity"],
)
def test_native_bytes_opaque_controls_request_and_identity_are_exact(monkeypatch, restore, location):
    saved = contract(monkeypatch)
    live = copy.deepcopy(saved["before" if restore else "after"])
    if location.startswith("native_"):
        name = "info" if location == "native_info" else "control"
        offset = {
            "native_info": 3,
            "native_control": 3,
            "native_lower": equations.HEADER + 37 * equations.CONTROL_STRIDE + 4,
            "native_coefficient": equations.HEADER + 37 * equations.CONTROL_STRIDE + 0x2C,
        }[location]
        value = bytes.fromhex(live["equations"][name + "_hex"])[offset] ^ 1
        native.mutate(live["equations"], name, offset, value)
    elif location == "rail_opaque":
        raw = bytearray.fromhex(live["rail_control"]["rails"]["0"]["payload_hex"])
        raw[0x48 + 24] ^= 1
        live["rail_control"]["rails"]["0"]["payload_hex"] = raw.hex()
    elif location == "boost":
        live["boost_percent"] = 1
    else:
        for section in ("equations", "policy", "rail_control"):
            live[section]["identity"]["device_id"] = 0x1234
    with pytest.raises(RuntimeError):
        rel.verify(live, saved, restore=restore)


@pytest.mark.parametrize("architecture", [4, 5, 6, 7, 8, 9, 10])
@pytest.mark.parametrize("rail", [0, 1])
def test_alternate_identity_and_unrelated_missing_rail_preserve_capability(monkeypatch, architecture, rail):
    obs = observation(
        rail, partial=True, slot="0000:03:04.2", architecture=architecture, device_id=42, subsystem_id=0xDEADBEEF
    )
    saved = contract(monkeypatch, rail, obs=obs)
    assert rel.verify(saved["after"], saved) == list(saved["selected"])
    assert set(saved["before"]["policy"]["rails"]) == {str(rail)}


@pytest.mark.parametrize("kind", ["selected", "affected", "controls", "request"])
def test_missing_required_independent_readback_refuses_contract(monkeypatch, kind):
    obs = observation()
    bios = inspection(obs, roots=[root(), root(1, "REL", 7999)])
    if kind in ("selected", "affected"):
        index = "0" if kind == "selected" else "1"
        obs["policy"]["rails"].pop(index)
        obs["rail_control"]["rails"].pop(index)
    elif kind == "controls":
        obs["rail_control"]["rails"].pop("1")
    else:
        obs["boost_percent"] = None
    with pytest.raises(RuntimeError):
        contract(monkeypatch, obs=obs, bios=bios)


@pytest.mark.parametrize(
    "field,value",
    [
        ("equation", True),
        ("equation", -1),
        ("equation", 8192),
        ("equation", 10**100),
        ("equation_type", True),
        ("equation_type", 6),
        ("equation_offset", -1),
        ("equation_offset", 16 * 1024 * 1024),
        ("offsets", [1933, 1937]),
        ("rail", "MSVDD"),
        ("role", "ALT"),
        ("sharing_graph_complete", False),
        ("sharing_graph_complete", 1),
        ("units", "mV"),
        ("capability", "readonly"),
        ("upper_clamp_binding", {}),
        ("affected_clock_roots", [{}]),
        ("affected_clock_roots", None),
        ("affected_rail_roots", []),
        ("affected_rail_roots", None),
    ],
)
def test_invalid_bios_binding_rejected_before_read(monkeypatch, field, value):
    bios = inspection()
    bios["fields"][0][field] = value
    monkeypatch.setattr(rel, "read", lambda gpu: pytest.fail("Malformed binding triggered GET"))
    with pytest.raises(rel.VoltageRelError):
        rel.contracts(object(), bios, {"voltage.0.REL.maximum": 1115000})


@pytest.mark.parametrize(
    "field,value",
    [
        ("rail_index", True),
        ("rail_index", 2),
        ("rail_identity", True),
        ("rail_identity", 2),
        ("role", "OV"),
        ("role", "VMIN"),
        ("selector_offset", -1),
        ("selector_offset", True),
        ("selector_offset", 2**40),
        ("equation", 8192),
        ("equation", True),
        ("dependency_indices", [True, 37]),
        ("dependency_indices", [37, 37]),
        ("dependency_indices", [401, 37]),
        ("dependency_indices", [401]),
        ("dependency_indices", [37, 8192]),
        ("dependency_indices", None),
    ],
)
def test_complete_root_graph_requires_valid_typed_members(monkeypatch, field, value):
    bios = inspection()
    bios["fields"][0]["affected_rail_roots"][0][field] = value
    with pytest.raises(rel.VoltageRelError):
        contract(monkeypatch, bios=bios)


@pytest.mark.parametrize(
    "fault", ["duplicate", "no_own", "wrong_own_equation", "missing_root_dependency", "extra_root_field"]
)
def test_complete_root_graph_cannot_omit_or_alias_authority(monkeypatch, fault):
    bios = inspection()
    roots = bios["fields"][0]["affected_rail_roots"]
    if fault == "duplicate":
        roots.append(copy.deepcopy(roots[0]))
    elif fault == "no_own":
        roots[0] = root(1, "REL", 7999)
    elif fault == "wrong_own_equation":
        roots[0] = root(0, "REL", 401)
    elif fault == "missing_root_dependency":
        roots.append(root(0, "ALT", 401, dependencies=[37]))
    else:
        roots[0]["extra"] = 1
    with pytest.raises(rel.VoltageRelError):
        contract(monkeypatch, bios=bios)


@pytest.mark.parametrize(
    "field,value",
    [
        ("before_uv", 1119999),
        ("index", True),
        ("lower_uv", 900001),
        ("next_index", 7999),
        ("output_type", 1),
        ("variable", 254),
        ("variables", [{"type": 2, "value": 4}]),
        ("coefficients", [{"type": 1, "value": 0x7FC00000}] * 6),
    ],
)
def test_native_binding_must_match_loaded_definition(monkeypatch, field, value):
    bios = inspection()
    bios["fields"][0]["upper_clamp_binding"][field] = value
    with pytest.raises(RuntimeError):
        contract(monkeypatch, bios=bios)


@pytest.mark.parametrize(
    "wanted", [True, None, "1115000", 0, -1, 899999, 1115000.5, float("nan"), float("inf"), 2**31, 16777217]
)
def test_invalid_request_never_silently_rounds(monkeypatch, wanted):
    with pytest.raises(RuntimeError):
        contract(monkeypatch, changes={"voltage.0.REL.maximum": wanted})


@pytest.mark.parametrize(
    "fault",
    [
        "schema",
        "missing",
        "extra",
        "selected",
        "direction",
        "native_kind",
        "native_extra",
        "after_policy",
        "before_identity",
        "native_peer",
        "root_schema",
    ],
)
def test_receipt_validation_reconstructs_exact_projection(monkeypatch, fault):
    saved = contract(monkeypatch)
    if fault == "schema":
        saved["schema"] = True
    elif fault == "missing":
        saved.pop("native")
    elif fault == "extra":
        saved["extra"] = 1
    elif fault == "selected":
        saved["selected"] = {}
    elif fault == "direction":
        saved["native"]["selected"]["voltage.0.REL.maximum"]["after_uv"] -= 5000
    elif fault == "native_kind":
        saved["native"]["kind"] = "constants"
    elif fault == "native_extra":
        saved["native"]["extra"] = 1
    elif fault == "after_policy":
        saved["after"]["policy"]["rails"]["0"]["policy_uv"]["reliability"] -= 5000
    elif fault == "before_identity":
        saved["before"]["policy"]["identity"]["device_id"] = 42
    elif fault == "native_peer":
        native.mutate(saved["after"]["equations"], "control", 3, 1)
    else:
        saved["selected"]["voltage.0.REL.maximum"]["affected_rail_roots"][0]["extra"] = True
    with pytest.raises(RuntimeError):
        rel.validate_contracts({"voltage.0.REL.maximum": 1115000}, saved)


def test_fresh_complete_binding_must_match_preview(monkeypatch):
    obs = observation()
    bios = inspection(obs)
    saved = contract(monkeypatch, obs=obs, bios=bios)
    bios["fields"][0]["description"] = "Harmless parser display metadata"
    rel.validate_inspection(bios, {"voltage.0.REL.maximum": 1115000}, saved)
    bios["fields"][0]["affected_rail_roots"].append(root(1, "REL", 7999))
    with pytest.raises(rel.VoltageRelError, match="binding changed"):
        rel.validate_inspection(bios, {"voltage.0.REL.maximum": 1115000}, saved)


@pytest.mark.parametrize("name", ["before", "after"])
def test_duplicate_native_packet_payloads_are_rejected(monkeypatch, name):
    saved = contract(monkeypatch)
    saved["native"][name] = copy.deepcopy(saved[name]["equations"])
    with pytest.raises(rel.VoltageRelError, match="exact native projection"):
        rel.validate_contracts({"voltage.0.REL.maximum": 1115000}, saved)


@pytest.mark.parametrize("changes", [{}, {"power.0.default": 360000}])
def test_unrelated_changes_require_no_voltage_capability(monkeypatch, changes):
    monkeypatch.setattr(rel, "read", lambda gpu: pytest.fail("Unrelated change triggered GET"))
    assert rel.contracts(None, None, changes) == {}
    rel.validate_contracts(changes, {})
    rel.validate_inspection(None, changes, {})
    assert rel.verify(None, {}) == []


def test_mixed_bios_changes_rejected_before_get(monkeypatch):
    monkeypatch.setattr(rel, "read", lambda gpu: pytest.fail("Mixed change triggered GET"))
    with pytest.raises(rel.VoltageRelError, match="separately"):
        rel.contracts(None, None, {"voltage.0.REL.maximum": 1115000, "power.0.default": 360000})


def test_read_failure_is_unavailable_not_a_zero_clamp(monkeypatch):
    monkeypatch.setattr(rel, "read", lambda gpu: (None, "temporary equation GET failure"))
    with pytest.raises(rel.VoltageRelError, match="temporary equation GET failure"):
        rel.contracts(object(), inspection(), {"voltage.0.REL.maximum": 1115000})


def test_two_selected_rails_project_only_their_own_upper_words(monkeypatch):
    obs = observation()
    other = native.clamp_observation(index=401, next_index=0xFFFF, peers=(37, 7999))
    for name, stride in (("info", equations.INFO_STRIDE), ("control", equations.CONTROL_STRIDE)):
        raw = bytearray.fromhex(obs["equations"][name + "_hex"])
        source = bytes.fromhex(other[name + "_hex"])
        offset = equations.HEADER + 401 * stride
        raw[offset : offset + stride] = source[offset : offset + stride]
        obs["equations"][name + "_hex"] = raw.hex()
    bios = inspection(obs)
    bios["fields"] += inspection(obs, 1, index=401)["fields"]
    changes = {"voltage.0.REL.maximum": 1115000, "voltage.1.REL.maximum": 1110000}
    saved = contract(monkeypatch, obs=obs, bios=bios, changes=changes)
    assert rel.verify(saved["after"], saved) == list(changes)
    assert rel.verify(saved["before"], saved, restore=True) == list(changes)
    expected = bytearray.fromhex(obs["equations"]["control_hex"])
    for index, value in ((37, 1115000), (401, 1110000)):
        struct.pack_into("<f", expected, equations.HEADER + index * equations.CONTROL_STRIDE + 8, value)
    assert bytes.fromhex(saved["after"]["equations"]["control_hex"]) == expected


def fixed_contract(monkeypatch, rail=0, *, wanted=1045000, obs=None, roots=None):
    obs = observation(rail) if obs is None else obs
    bios = inspection(obs, rail, roots=roots, operation="value")
    return contract(monkeypatch, rail, obs=obs, bios=bios, changes={f"voltage.{rail}.REL.value": wanted})


@pytest.mark.parametrize("rail", [0, 1])
@pytest.mark.parametrize("wanted", [850000, 1035000, 1045000, 1120000, 1125000])
def test_fixed_head_projects_exact_two_bounds_preserving_polynomial_and_chain(monkeypatch, rail, wanted):
    obs = observation(rail)
    saved = fixed_contract(monkeypatch, rail, wanted=wanted, obs=obs)
    key = f"voltage.{rail}.REL.value"
    assert saved["native"]["kind"] == "voltage_bounds"
    assert saved["native"]["selected"][key]["after_lower_uv"] == wanted
    assert saved["native"]["selected"][key]["after_uv"] == wanted
    assert set(saved["native"]) == {"schema", "kind", "selected"}
    expected = bytearray.fromhex(obs["equations"]["control_hex"])
    struct.pack_into("<ff", expected, equations.HEADER + 37 * equations.CONTROL_STRIDE + 4, wanted, wanted)
    assert saved["after"]["equations"]["control_hex"] == expected.hex()
    assert saved["after"]["equations"]["info_hex"] == obs["equations"]["info_hex"]
    assert saved["before"]["policy"] == saved["after"]["policy"]
    assert saved["after"]["rail_control"] == obs["rail_control"]
    assert rel.verify(saved["after"], saved) == [key]
    assert rel.verify(saved["before"], saved, restore=True) == [key]
    rel.validate_contracts({key: wanted}, json.loads(json.dumps(saved, allow_nan=False)))


@pytest.mark.parametrize("rail", [0, 1])
@pytest.mark.parametrize("boost", [0, 37])
def test_lower_only_change_allows_affected_outputs_without_claiming_exact_final_rel(monkeypatch, rail, boost):
    obs = observation(rail, boost=boost)
    saved = fixed_contract(monkeypatch, rail, wanted=1120000, obs=obs, roots=[root(rail), root(rail, "ALT", 401)])
    # Upper unchanged; lower changed from900000. This is a real model change.
    row = saved["native"]["selected"][f"voltage.{rail}.REL.value"]
    assert row["before_uv"] == row["after_uv"] and row["lower_uv"] != row["after_lower_uv"]
    active = copy.deepcopy(saved["after"])
    policy = active["policy"]["rails"][str(rail)]
    policy["policy_uv"]["reliability"] += 123
    policy["policy_uv"]["alt_reliability"] += 456
    policy["policy_uv"]["effective"] += 789
    if boost:
        for peer in active["policy"]["rails"].values():
            peer["boost_uv"] += 17
            peer["policy_uv"]["reliability"] += 19
            peer["policy_uv"]["effective"] += 23
    assert rel.verify(active, saved) == list(saved["selected"])
    with pytest.raises(RuntimeError):
        rel.verify(active, saved, restore=True)


@pytest.mark.parametrize("boost", [0, 37])
@pytest.mark.parametrize("field", ["reliability", "alt_reliability", "effective", "boost_uv"])
def test_fixed_head_true_noop_keeps_every_computed_value_exact(monkeypatch, boost, field):
    obs = observation(boost=boost)
    raw = bytearray.fromhex(obs["equations"]["control_hex"])
    struct.pack_into("<f", raw, equations.HEADER + 37 * equations.CONTROL_STRIDE + 4, 1120000)
    obs["equations"]["control_hex"] = raw.hex()
    saved = fixed_contract(monkeypatch, wanted=1120000, obs=obs, roots=[root(), root(0, "ALT", 401)])
    assert saved["after"] == saved["before"]
    active = copy.deepcopy(saved["after"])
    row = active["policy"]["rails"]["0"]
    (row if field == "boost_uv" else row["policy_uv"])[field] += 1
    with pytest.raises(rel.VoltageRelError, match="preserved"):
        rel.verify(active, saved)


@pytest.mark.parametrize("rail", [0, 1])
@pytest.mark.parametrize("boost", [0, 1, 100])
def test_fixed_head_alternate_identity_partial_rails_and_existing_offsets(monkeypatch, rail, boost):
    obs = observation(
        rail, partial=True, boost=boost, slot="0000:52:03.1", architecture=9, device_id=0x3456, subsystem_id=0x12345678
    )
    policy = obs["policy"]["rails"][str(rail)]
    policy["offsets_uv"] = {"reliability": -12345, "alt_reliability": 6789, "overvoltage": 23456, "vmin": -9876}
    obs["rail_control"] = ceiling.controls(obs["policy"])
    saved = fixed_contract(monkeypatch, rail, obs=obs)
    assert saved["after"]["rail_control"] == obs["rail_control"]
    assert saved["after"]["boost_percent"] == boost
    assert rel.verify(saved["after"], saved) == list(saved["selected"])
    assert rel.verify(saved["before"], saved, restore=True) == list(saved["selected"])


@pytest.mark.parametrize("operation", ["maximum", "value"])
def test_same_rail_alias_selection_is_refused_before_read(monkeypatch, operation):
    obs = observation()
    bios = inspection(obs)
    bios["fields"] += inspection(obs, operation="value")["fields"]
    monkeypatch.setattr(rel, "read", lambda gpu: pytest.fail("Alias request triggered GET"))
    with pytest.raises(rel.VoltageRelError, match="one REL operation"):
        rel.contracts(None, bios, {"voltage.0.REL.maximum": 1045000, "voltage.0.REL.value": 1045000})


def test_crossrail_same_equation_alias_is_refused_before_read(monkeypatch):
    obs = observation()
    bios = inspection(obs)
    bios["fields"] += inspection(obs, 1, operation="value")["fields"]
    monkeypatch.setattr(rel, "read", lambda gpu: pytest.fail("Alias request triggered GET"))
    with pytest.raises(rel.VoltageRelError, match="alias"):
        rel.contracts(None, bios, {"voltage.0.REL.maximum": 1045000, "voltage.1.REL.value": 1045000})


@pytest.mark.parametrize("modes", [("value", "value"), ("maximum", "value"), ("value", "maximum")])
def test_mixed_and_two_fixed_rails_use_single_exact_bounds_contract(monkeypatch, modes):
    obs = observation()
    other = native.clamp_observation(index=401, next_index=0xFFFF, peers=(37, 7999))
    for name, stride in (("info", equations.INFO_STRIDE), ("control", equations.CONTROL_STRIDE)):
        raw = bytearray.fromhex(obs["equations"][name + "_hex"])
        source = bytes.fromhex(other[name + "_hex"])
        at = equations.HEADER + 401 * stride
        raw[at : at + stride] = source[at : at + stride]
        obs["equations"][name + "_hex"] = raw.hex()
    bios = {"fields": []}
    changes = {}
    expected = bytearray.fromhex(obs["equations"]["control_hex"])
    for rail, index, wanted in ((0, 37, 1045000), (1, 401, 1115000)):
        operation = modes[rail]
        bios["fields"] += inspection(obs, rail, index=index, operation=operation)["fields"]
        changes[f"voltage.{rail}.REL.{operation}"] = wanted
        lower = wanted if operation == "value" else 900000
        struct.pack_into("<ff", expected, equations.HEADER + index * equations.CONTROL_STRIDE + 4, lower, wanted)
    saved = contract(monkeypatch, obs=obs, bios=bios, changes=changes)
    assert saved["native"]["kind"] == "voltage_bounds"
    assert saved["after"]["equations"]["control_hex"] == expected.hex()
    assert rel.verify(saved["after"], saved) == list(changes)
    assert rel.verify(saved["before"], saved, restore=True) == list(changes)
    rel.validate_inspection(bios, changes, saved)


def test_old_all_maximum_receipt_shape_is_unchanged(monkeypatch):
    obs = observation()
    saved = contract(monkeypatch, obs=obs)
    selection = native.clamp_selections(obs["equations"], after_uv=1115000)
    expected_native = equations.clamp_contracts(obs["equations"], selection)
    assert saved["native"] == {key: expected_native[key] for key in ("schema", "kind", "selected")}
    assert saved["native"]["kind"] == "upper_clamps"
    assert all("after_lower_uv" not in row for row in saved["native"]["selected"].values())
    rel.validate_contracts({"voltage.0.REL.maximum": 1115000}, saved)
    assert rel.verify(saved["before"], saved, restore=True) == ["voltage.0.REL.maximum"]


@pytest.mark.parametrize(
    "wanted", [True, None, "1045000", 0, -1, 1045000.5, float("nan"), float("inf"), 2**31, 16777217]
)
def test_fixed_head_invalid_request_never_silently_rounds(monkeypatch, wanted):
    with pytest.raises(RuntimeError):
        fixed_contract(monkeypatch, wanted=wanted)


@pytest.mark.parametrize("offsets", [[1932, 1936], [1932], [1936], [1936, 1932], [1933, 1937, 1941], [True, 1932]])
def test_fixed_head_requires_exact_ordered_two_bound_offsets(monkeypatch, offsets):
    bios = inspection(operation="value")
    bios["fields"][0]["offsets"] = offsets
    monkeypatch.setattr(rel, "read", lambda gpu: pytest.fail("Malformed binding triggered GET"))
    with pytest.raises(rel.VoltageRelError, match="bound floats"):
        rel.contracts(None, bios, {"voltage.0.REL.value": 1045000})


@pytest.mark.parametrize(
    "fault", ["kind", "lower", "missing_lower", "extra", "before_lower", "after_peer", "source_offset"]
)
def test_fixed_head_receipt_reconstructs_two_bounds_and_exact_context(monkeypatch, fault):
    saved = fixed_contract(monkeypatch)
    key = "voltage.0.REL.value"
    if fault == "kind":
        saved["native"]["kind"] = "upper_clamps"
    elif fault == "lower":
        saved["native"]["selected"][key]["after_lower_uv"] = 1040000
    elif fault == "missing_lower":
        saved["native"]["selected"][key].pop("after_lower_uv")
    elif fault == "extra":
        saved["native"]["after"] = saved["after"]["equations"]
    elif fault == "before_lower":
        saved["selected"][key]["upper_clamp_binding"]["lower_uv"] += 1
    elif fault == "after_peer":
        native.mutate(saved["after"]["equations"], "control", 3, 1)
    else:
        saved["selected"][key]["offsets"] = [1932]
    with pytest.raises(RuntimeError):
        rel.validate_contracts({key: 1045000}, saved)


@pytest.mark.parametrize("location", ["bounds", "coefficient", "info", "sixth_offset", "boost", "peer_ov", "peer_alt"])
@pytest.mark.parametrize("restore", [False, True])
def test_fixed_head_exact_peers_and_requests_on_apply_and_undo(monkeypatch, location, restore):
    saved = fixed_contract(monkeypatch)
    actual = copy.deepcopy(saved["before" if restore else "after"])
    if location in ("bounds", "coefficient", "info"):
        name = "info" if location == "info" else "control"
        offset = (
            3
            if location == "info"
            else equations.HEADER + 37 * equations.CONTROL_STRIDE + (4 if location == "bounds" else 0x2C)
        )
        raw = bytes.fromhex(actual["equations"][name + "_hex"])
        native.mutate(actual["equations"], name, offset, raw[offset] ^ 1)
    elif location == "sixth_offset":
        raw = bytearray.fromhex(actual["rail_control"]["rails"]["0"]["payload_hex"])
        raw[0x48 + 24] ^= 1
        actual["rail_control"]["rails"]["0"]["payload_hex"] = raw.hex()
    elif location == "boost":
        actual["boost_percent"] = 1
    else:
        field = "overvoltage" if location == "peer_ov" else "alt_reliability"
        actual["policy"]["rails"]["1"]["policy_uv"][field] += 1
    with pytest.raises(RuntimeError):
        rel.verify(actual, saved, restore=restore)
