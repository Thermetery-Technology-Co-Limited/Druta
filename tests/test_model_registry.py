"""Offline effective-model contracts, independent of GPU/registry operations."""

import copy
import dataclasses
import json
import struct

import pytest

from druta import model_registry as model
from druta import vfe_inputs as vfe

NVVDD = "RmPmgrIddqNvvddOverride"
MSVDD = "RmPmgrIddqMsvddOverride"
SPEEDO = "RMHwSpeedoOverride"


def row(name=NVVDD, index=31, active=19000):
    selector, descriptor = next(
        (selector, descriptor) for selector, descriptor in vfe._FUSE_SELECTORS.items() if descriptor[0] == name
    )
    return dataclasses.asdict(
        vfe.VfeInput(
            index=index,
            registry_name=name,
            label=descriptor[1],
            kind=descriptor[2],
            fuse_id=selector[0],
            fuse_version_field_id=selector[1],
            active=active,
            raw_hardware=380,
            default=2700,
            signed=False,
            raw_hardware_signed=False,
            default_signed=False,
            override=0,
            override_present=False,
            cache_ready=True,
            fuse_version=2,
            expected_fuse_version=254,
            hardware_scale_q12=204800,
            hardware_offset=0,
            minimum=0.0,
            maximum=1000000.0,
        )
    )


def observation(*, names=(NVVDD, MSVDD), slot="0000:7f:1e.7", architecture=8):
    inputs = [row(name, 31 + index * 7, 19000 - index * 2000) for index, name in enumerate(names)]
    variables = [
        {
            "index": value["index"],
            "type": 13,
            "minimum": value["minimum"],
            "maximum": value["maximum"],
            "control_mode": 0,
            "control_words": (0, 0),
        }
        for value in inputs
    ]
    variables += [
        {"index": 200, "type": 79, "minimum": None, "maximum": None, "control_mode": 2, "control_words": (4, 5)}
    ]
    return {
        "slot": slot,
        "architecture": architecture,
        "inputs": inputs,
        "variables": variables,
        "control_sha256": "a1" * 32,
        "model_static_sha256": "b2" * 32,
        "captured_at": 10.0,
        "info_sha256": "c3" * 32,
        "issues": [],
    }


def active_for(before, changes):
    after = copy.deepcopy(before)
    for value in after["inputs"]:
        if value["registry_name"] in changes:
            value.update(
                active=changes[value["registry_name"]], override=changes[value["registry_name"]], override_present=True
            )
    after["info_sha256"] = "d4" * 32
    after["captured_at"] += 1
    return after


@pytest.mark.parametrize(
    "names,changes",
    [
        ((NVVDD,), {NVVDD: 19001}),
        ((MSVDD,), {MSVDD: 19001}),
        ((NVVDD, MSVDD), {NVVDD: 19001}),
        ((NVVDD, MSVDD), {MSVDD: 17001}),
        ((NVVDD, MSVDD, SPEEDO), {NVVDD: 19001, MSVDD: 17001}),
        ((SPEEDO,), {SPEEDO: 19001}),
    ],
)
@pytest.mark.parametrize("architecture,slot", [(4, "0000:7f:1e.7"), (9, "0000:23:02.1"), (10, "0000:01:00.0")])
def test_exact_contracts_apply_restore_partial_rails_and_alternate_identity(names, changes, architecture, slot):
    before = observation(names=names, architecture=architecture, slot=slot)
    untouched = copy.deepcopy(before)
    saved = json.loads(json.dumps(model.contracts(before, changes), allow_nan=False))
    assert saved["selected"] == changes
    model.validate_contracts(changes, saved)
    assert model.verify(active_for(before, changes), saved) == sorted(changes)
    assert model.verify(before, saved, restore=True) == sorted(changes)
    assert before == untouched
    with pytest.raises(model.ModelRegistryError):
        model.verify(before, saved)


def test_identity_write_retains_explicit_selection_when_state_is_identical():
    before = active_for(observation(), {NVVDD: 19000})
    saved = model.contracts(before, {NVVDD: 19000})
    assert saved["before"] == saved["after"]
    assert model.verify(before, saved) == [NVVDD]
    model.validate_contracts({NVVDD: 19000}, saved)
    with pytest.raises(model.ModelRegistryError):
        model.validate_contracts({MSVDD: 17000}, saved)


def test_no_selected_controls_requires_no_capability_or_snapshot():
    assert model.contracts(None, {}) == {}
    model.validate_contracts({}, {})
    assert model.verify(None, {}) == []


BAD_VALUES = [True, False, None, "19001", 19001.0, 0, -1, 2**32, 10**10000, float("nan"), float("inf")]


@pytest.mark.parametrize("value", BAD_VALUES, ids=[f"bad-{n}" for n in range(len(BAD_VALUES))])
def test_request_is_exact_positive_dword_not_truthy_number(value):
    with pytest.raises(model.ModelRegistryError):
        model.contracts(observation(), {NVVDD: value})


@pytest.mark.parametrize("changes", [None, False, [], [NVVDD], {"unknown": 1}])
def test_malformed_changes_are_rejected_even_when_falsy(changes):
    with pytest.raises(model.ModelRegistryError):
        model.contracts(None, changes)
    with pytest.raises(model.ModelRegistryError):
        model.validate_contracts(changes, {})


BAD_ROW = [
    ("index", True),
    ("index", -1),
    ("index", 255),
    ("index", "31"),
    ("index", 10**10000),
    ("active", True),
    ("active", -1),
    ("active", 2**32),
    ("raw_hardware", False),
    ("default", 2**32),
    ("override", -1),
    ("override", 10**10000),
    ("hardware_scale_q12", 2**32),
    ("hardware_offset", -(2**31) - 1),
    ("minimum", True),
    ("minimum", 10**10000),
    ("minimum", float("nan")),
    ("maximum", float("inf")),
    ("maximum", -1),
    ("fuse_id", True),
    ("fuse_id", 255),
    ("fuse_version_field_id", 1),
    ("fuse_version", 256),
    ("expected_fuse_version", -1),
    ("signed", 0),
    ("raw_hardware_signed", 0),
    ("default_signed", 1),
    ("override_present", 0),
    ("cache_ready", 1),
    ("registry_name", MSVDD),
    ("label", "Current"),
    ("kind", "current"),
]


@pytest.mark.parametrize("key,value", BAD_ROW, ids=[f"{key}-{n}" for n, (key, _) in enumerate(BAD_ROW)])
def test_malformed_typed_metadata_has_domain_error(key, value):
    before = observation()
    before["inputs"][0][key] = value
    with pytest.raises(model.ModelRegistryError):
        model.contracts(before, {NVVDD: 19001})


@pytest.mark.parametrize("key", ["signed", "raw_hardware_signed", "default_signed", "cache_ready"])
def test_selected_input_requires_ready_unsigned_domain(key):
    before = observation()
    before["inputs"][0][key] = key != "cache_ready"
    with pytest.raises(model.ModelRegistryError, match="unsigned"):
        model.contracts(before, {NVVDD: 19001})


def test_unavailable_signed_peer_does_not_suppress_independent_selected_rail():
    before = observation()
    peer = before["inputs"][1]
    peer.update(signed=True, raw_hardware_signed=True, default_signed=True, cache_ready=False)
    saved = model.contracts(before, {NVVDD: 19001})
    assert model.verify(active_for(before, {NVVDD: 19001}), saved) == [NVVDD]


@pytest.mark.parametrize(
    "damage",
    [
        "missing_static",
        "empty_static",
        "uppercase_static",
        "bad_control",
        "missing_variables",
        "missing_row",
        "duplicate_row",
        "duplicate_name",
        "duplicate_variable",
        "wrong_variable_type",
        "wrong_range",
        "extra_field",
    ],
)
def test_inventory_and_fingerprint_required(damage):
    before = observation()
    if damage == "missing_static":
        before.pop("model_static_sha256")
    elif damage == "empty_static":
        before["model_static_sha256"] = ""
    elif damage == "uppercase_static":
        before["model_static_sha256"] = "B2" * 32
    elif damage == "bad_control":
        before["control_sha256"] = True
    elif damage == "missing_variables":
        before.pop("variables")
    elif damage == "missing_row":
        before["variables"].pop(0)
    elif damage == "duplicate_row":
        before["inputs"].append(copy.deepcopy(before["inputs"][0]))
    elif damage == "duplicate_name":
        value = row(NVVDD, 38)
        before["inputs"][1] = value
        before["variables"][1].update(minimum=value["minimum"], maximum=value["maximum"])
    elif damage == "duplicate_variable":
        before["variables"].append(copy.deepcopy(before["variables"][0]))
    elif damage == "wrong_variable_type":
        before["variables"][0]["type"] = 7
    elif damage == "wrong_range":
        before["variables"][0]["minimum"] = 1.0
    elif damage == "extra_field":
        before["inputs"][0]["volts"] = 1
    with pytest.raises(model.ModelRegistryError):
        model.contracts(before, {NVVDD: 19001})


@pytest.mark.parametrize(
    "key,value",
    [("slot", "0001:01:00.0"), ("slot", "0000:01:20.0"), ("slot", None), ("architecture", True), ("architecture", 3)],
)
def test_invalid_target_metadata(key, value):
    before = observation()
    before[key] = value
    with pytest.raises(model.ModelRegistryError):
        model.contracts(before, {NVVDD: 19001})


@pytest.mark.parametrize("key,value", [("control_mode", 1), ("control_words", [0, 1])])
def test_existing_runtime_override_not_combined(key, value):
    before = observation()
    before["variables"][0][key] = value
    with pytest.raises(model.ModelRegistryError, match="runtime control"):
        model.contracts(before, {NVVDD: 19001})


@pytest.mark.parametrize(
    "key,value",
    [
        ("active", 19000),
        ("override", 19000),
        ("override_present", False),
        ("raw_hardware", 381),
        ("default", 2701),
        ("hardware_scale_q12", 4096),
        ("hardware_offset", 1),
        ("fuse_version", 3),
        ("cache_ready", False),
    ],
)
def test_active_selected_fields_and_all_selected_metadata_must_match(key, value):
    before, changes = observation(), {NVVDD: 19001}
    saved = model.contracts(before, changes)
    actual = active_for(before, changes)
    actual["inputs"][0][key] = value
    with pytest.raises(model.ModelRegistryError):
        model.verify(actual, saved)


@pytest.mark.parametrize(
    "damage",
    [
        "peer_active",
        "peer_override",
        "peer_removed",
        "variable_word",
        "unknown_variable_removed",
        "static_hash",
        "control_hash",
        "slot",
        "architecture",
    ],
)
def test_all_peers_and_unknown_model_configuration_preserved(damage):
    before, changes = observation(), {NVVDD: 19001}
    saved = model.contracts(before, changes)
    actual = active_for(before, changes)
    if damage == "peer_active":
        actual["inputs"][1]["active"] += 1
    elif damage == "peer_override":
        actual["inputs"][1]["override"] = 1
    elif damage == "peer_removed":
        actual["inputs"].pop()
    elif damage == "variable_word":
        actual["variables"][-1]["control_words"] = [4, 6]
    elif damage == "unknown_variable_removed":
        actual["variables"].pop()
    elif damage == "static_hash":
        actual["model_static_sha256"] = "00" * 32
    elif damage == "control_hash":
        actual["control_sha256"] = "00" * 32
    elif damage == "slot":
        actual["slot"] = "0000:23:02.1"
    elif damage == "architecture":
        actual["architecture"] = 9
    with pytest.raises(model.ModelRegistryError):
        model.verify(actual, saved)


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "missing_selected",
        "wrong_selected",
        "extra_selected",
        "changed_after",
        "changed_peer",
        "changed_hash",
        "schema_bool",
        "range_huge",
        "index_bool",
    ],
)
def test_pure_receipt_validation_rejects_tampering_before_apply_or_restore(damage):
    changes = {NVVDD: 19001}
    saved = model.contracts(observation(), changes)
    if damage == "missing":
        saved = {}
    elif damage == "missing_selected":
        saved.pop("selected")
    elif damage == "wrong_selected":
        saved["selected"][NVVDD] = 19002
    elif damage == "extra_selected":
        saved["selected"][MSVDD] = 17001
    elif damage == "changed_after":
        saved["after"]["inputs"][0]["active"] = 19002
    elif damage == "changed_peer":
        saved["after"]["inputs"][1]["active"] += 1
    elif damage == "changed_hash":
        saved["after"]["model_static_sha256"] = "00" * 32
    elif damage == "schema_bool":
        saved["schema"] = True
    elif damage == "range_huge":
        saved["before"]["inputs"][0]["minimum"] = -(10**10000)
    elif damage == "index_bool":
        saved["before"]["inputs"][0]["index"] = True
    with pytest.raises(model.ModelRegistryError):
        model.validate_contracts(changes, saved)
    if damage not in ("wrong_selected", "missing"):  # Verify has no separate external change mapping.
        with pytest.raises(model.ModelRegistryError):
            model.verify(observation(), saved, restore=True)


def test_existing_direct_override_can_be_changed_and_exactly_restored():
    before = active_for(observation(), {NVVDD: 19001})
    saved = model.contracts(before, {NVVDD: 19002})
    assert model.verify(active_for(before, {NVVDD: 19002}), saved) == [NVVDD]
    assert model.verify(before, saved, restore=True) == [NVVDD]
    before["inputs"][0]["active"] = 19002
    with pytest.raises(model.ModelRegistryError, match="disagree"):
        model.contracts(before, {NVVDD: 19003})


def test_range_limits_are_enforced_without_rounding():
    before = observation()
    before["inputs"][0]["maximum"] = before["variables"][0]["maximum"] = 19000.5
    with pytest.raises(model.ModelRegistryError, match="range"):
        model.contracts(before, {NVVDD: 19001})


def test_reordering_inventory_is_canonical_and_no_argument_aliases_escape():
    before = observation()
    before["inputs"].reverse()
    before["variables"].reverse()
    saved = model.contracts(before, {NVVDD: 19001})
    before["inputs"][1]["active"] = 4
    assert saved["before"]["inputs"][0]["active"] == 19000
    saved["after"]["inputs"][0]["active"] = 5
    assert saved["before"]["inputs"][0]["active"] == 19000


@pytest.mark.parametrize("saved", [None, False, [], 0])
def test_verify_does_not_treat_unavailable_as_empty_contract(saved):
    with pytest.raises(model.ModelRegistryError):
        model.verify(None, saved)


def test_verify_requires_actual_boolean_phase():
    with pytest.raises(model.ModelRegistryError):
        model.verify(None, {}, restore=1)


def test_decoder_to_contract_preserves_unknown_info_and_active_mirrors():
    # Real binary decoder geometry with synthetic rows; no native call.
    info, control = bytearray(vfe.INFO_SIZE), bytearray(vfe.CONTROL_SIZE)
    struct.pack_into("<I", info, 0, vfe.INFO_VERSION)
    struct.pack_into("<I", control, 0, vfe.CONTROL_VERSION)
    info[4:36] = control[4:36] = (1 << 42).to_bytes(32, "little")
    base, ctl = 42 * vfe.INFO_STRIDE, 42 * vfe.CONTROL_STRIDE
    struct.pack_into("<I", info, base + 0x128, 13)
    struct.pack_into("<Iff", control, ctl + 0x68, 13, 0.0, 1000000.0)
    info[base + 0x77C : base + 0x77E] = bytes((0x18, 3))
    info[base + 0x260] = 1
    for offset, value in ((0x244, 18000), (0x24C, 360), (0x254, 2700), (0x264, 18000), (0x268, 360), (0x374, 204800)):
        struct.pack_into("<I", info, base + offset, value)
    info, control = bytes(info), bytes(control)
    before = dataclasses.asdict(vfe.decode_packets(info, control, slot="0000:39:04.0", architecture=9))
    saved = model.contracts(before, {NVVDD: 18001})
    changed = bytearray(info)
    for offset in (0x244, 0x258, 0x264):
        struct.pack_into("<I", changed, 42 * vfe.INFO_STRIDE + offset, 18001)
    changed[42 * vfe.INFO_STRIDE + 0x25C] = 1
    actual = dataclasses.asdict(vfe.decode_packets(bytes(changed), control, slot="0000:39:04.0", architecture=9))
    assert model.verify(actual, saved) == [NVVDD]
    changed[42 * vfe.INFO_STRIDE + 0x400] ^= 1
    actual = dataclasses.asdict(vfe.decode_packets(bytes(changed), control, slot="0000:39:04.0", architecture=9))
    with pytest.raises(model.ModelRegistryError, match="preserved"):
        model.verify(actual, saved)


@pytest.mark.parametrize(
    "key,value",
    [
        ("index", True),
        ("index", "200"),
        ("index", 255),
        ("type", -1),
        ("type", True),
        ("control_mode", 256),
        ("control_words", [0, True]),
        ("control_words", [0, 2**32]),
        ("control_words", [0]),
        ("minimum", 10**10000),
    ],
    ids=[f"metadata-{n}" for n in range(10)],
)
def test_malformed_variable_metadata_is_rejected(key, value):
    before = observation()
    before["variables"][-1][key] = value
    with pytest.raises(model.ModelRegistryError):
        model.contracts(before, {NVVDD: 19001})


def test_unusable_unrelated_variable_range_remains_a_preserved_peer():
    before = observation()
    before["variables"][-1].update(minimum=5.0, maximum=4.0)
    saved = model.contracts(before, {NVVDD: 19001})
    assert model.verify(active_for(before, {NVVDD: 19001}), saved) == [NVVDD]
