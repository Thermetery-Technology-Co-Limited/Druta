"""Offline flag composition, target contracts and fresh readback behavior."""

import copy
import json
from types import SimpleNamespace

import pytest

from druta import registry_flags as flags
from druta.registry_overrides import OverrideError


def observation(enabled=True, *, indices=(1, 257), architecture=8, slot="0000:72:1d.3"):
    return {
        "group": "limits",
        "capacity": 512,
        "info_command": "0x2080A022",
        "control_command": "0x2080A024",
        "info_bytes": 31372,
        "control_bytes": 92240,
        "slot": slot,
        "architecture": architecture,
        "identity": {"slot": slot, "architecture": architecture, "device_id": 0xABCD, "subsystem_id": 0x12345678},
        "mask_hex": sum(1 << index for index in indices).to_bytes(64, "little").hex(),
        "present_count": len(indices),
        "entries": [{"index": index, "type": 6, "control_type": 6} for index in indices],
        "flags": {
            "limits_control_0x4c": {"raw": 0},
            "limits_control_0x4d": {"raw": int(enabled)},
            "limits_info_0x7a88": {"raw": 1},
        },
        "cache_enabled": enabled,
        "stable_reads": 2,
    }


def registry(value, *, name=flags.REGISTRY_NAME):
    return {name: {"type": 4, "value": value}}


def change(enabled=False):
    return {flags.CACHE_KEY: enabled}


def set_cache(snapshot, enabled):
    snapshot["flags"][flags.CACHE_FLAG]["raw"] = int(enabled)
    snapshot["cache_enabled"] = enabled


def test_unrelated_transactions_need_no_perf_capability():
    assert flags.contracts({}, None) == {}
    assert flags.verify(None, {}) == []
    assert flags.validate_existing({}, None) is None
    assert flags.validate_existing(registry(0), None) is None
    assert flags.validate_existing({"romOverride00": {"type": 3, "value": b"unchanged"}}, None) is None


@pytest.mark.parametrize("enabled, expected", [(False, 4), (True, 8)])
def test_compose_explicit_cache_encoding_from_absent_value(enabled, expected):
    before = {}
    source = observation()
    assert flags.compose(before, change(enabled), source) == registry(expected)
    assert before == {} and source == observation()


@pytest.mark.parametrize("enabled, expected", [(False, 0x85), (True, 0x89)])
def test_compose_preserves_reconciled_fields_case_and_unrelated_state(enabled, expected):
    name = "RMPERFLIMITSOVERRIDE"
    before = registry(0x81, name=name)
    before["romOverride00"] = {"type": 3, "value": b"unknown binary\0\xff"}
    before["Other"] = {"type": 7, "value": ["one", "two"]}
    saved = copy.deepcopy(before)
    after = flags.compose(before, change(enabled), observation())
    assert set(after) == set(before)
    assert after[name] == {"type": 4, "value": expected}
    assert after["romOverride00"] == before["romOverride00"]
    after["Other"]["value"].append("three")
    assert before == saved


@pytest.mark.parametrize("architecture", [4, 5, 6, 7, 8, 9, 10])
def test_capability_checks_accept_alternate_identities_and_full_inventory_capacity(architecture):
    source = observation(indices=(0, 257, 511), architecture=architecture, slot="00000000:fe:1f.7")
    source["identity"].update(device_id=0x1234, subsystem_id=0xFEDCBA98)
    assert flags.compose({}, change(), source) == registry(4)
    contract = flags.contracts(change(), source)
    applied = copy.deepcopy(source)
    set_cache(applied, False)
    assert flags.verify(applied, contract) == [flags.CACHE_KEY]


def test_cache_flag_does_not_require_the_subclass_only_info_flag_or_decoded_row_labels():
    source = observation()
    source["flags"].pop("limits_info_0x7a88")
    for row in source["entries"]:
        row.update(type=99, control_type=99, issues=["No decoded label layout"])
    assert flags.compose({}, change(), source) == registry(4)


@pytest.mark.parametrize("value", [4, 1, 0x40])
def test_pending_known_field_is_rejected(value):
    source = observation()
    if value == 1:
        source["flags"]["limits_control_0x4c"]["raw"] = 1
    with pytest.raises(OverrideError, match="registry and active flags disagree"):
        flags.validate_existing(registry(value), source)
    with pytest.raises(OverrideError):
        flags.compose(registry(value), change(), source)


def test_explicit_subclass_field_needs_its_own_readback():
    source = observation()
    source["flags"].pop("limits_info_0x7a88")
    with pytest.raises(OverrideError, match="registry and active flags disagree"):
        flags.compose(registry(0x80), change(), source)


@pytest.mark.parametrize("value", [0x10, 0x20, 0x30, 0x100, 0x80000000, 0x80000081])
def test_undecoded_registry_bits_are_never_clobbered(value):
    before = registry(value)
    with pytest.raises(OverrideError, match="undecoded bits"):
        flags.compose(before, change(), observation())
    assert before == registry(value)


@pytest.mark.parametrize("value", [3, 0xC, 0xC0, 0xCF])
def test_reserved_field_encodings_are_rejected(value):
    with pytest.raises(OverrideError, match="reserved field encoding"):
        flags.validate_existing(registry(value), observation())


@pytest.mark.parametrize(
    "entry",
    [
        {"type": 3, "value": b"\x04\0\0\0"},
        {"type": True, "value": 4},
        {"type": 4.0, "value": 4},
        {"type": 4, "value": True},
        {"type": 4, "value": 4.0},
        {"type": 4, "value": -1},
        {"type": 4, "value": 0x100000000},
        {"type": 4},
        {"type": 4, "value": 4, "unrecognized": True},
        None,
    ],
)
def test_non_dword_or_malformed_existing_value_is_rejected(entry):
    with pytest.raises(OverrideError, match="not an understood DWORD"):
        flags.compose({flags.REGISTRY_NAME: entry}, change(), observation())


@pytest.mark.parametrize(
    "before",
    [
        {"RmPerfLimitsOverride": {"type": 4, "value": 0}, "rmperflimitsoverride": {"type": 4, "value": 0}},
        {"Unrelated": {}, "UNRELATED": {}},
        {None: {}},
        [],
    ],
)
def test_registry_names_must_be_unambiguous(before):
    with pytest.raises(OverrideError):
        flags.compose(before, change(), observation())


@pytest.mark.parametrize("changes", [None, [], {}, {"unknown": True}, change(1), change(0.0), change(None)])
def test_only_selected_boolean_changes_are_composable(changes):
    with pytest.raises(OverrideError):
        flags.compose({}, changes, observation())


@pytest.mark.parametrize(
    "key,value",
    [
        ("group", "cf_policy"),
        ("capacity", 256),
        ("capacity", 512.0),
        ("info_command", "0x2080A024"),
        ("control_command", "0x2080A025"),
        ("info_bytes", 31371),
        ("control_bytes", 92241),
        ("mask_hex", "00"),
        ("mask_hex", "gg" * 64),
        ("mask_hex", None),
        ("present_count", 3),
        ("present_count", 2.0),
        ("entries", None),
        ("flags", None),
        ("cache_enabled", 1),
        ("stable_reads", 1),
        ("stable_reads", 2.0),
        ("architecture", 8.0),
        ("slot", "0000:02:00.0"),
    ],
)
def test_malformed_observation_cannot_authorize_edits(key, value):
    source = observation()
    source[key] = value
    with pytest.raises(OverrideError):
        flags.compose({}, change(), source)


@pytest.mark.parametrize(
    "field,value",
    [
        ("slot", "unpaired"),
        ("slot", "0001:00:00.0"),
        ("slot", "0000:00:20.0"),
        ("slot", None),
        ("architecture", 3),
        ("architecture", 11),
        ("architecture", True),
        ("device_id", -1),
        ("device_id", 0x10000),
        ("device_id", True),
        ("subsystem_id", 0x100000000),
        ("subsystem_id", None),
    ],
)
def test_target_identity_needs_an_understood_architecture_and_exact_values(field, value):
    source = observation()
    source["identity"][field] = value
    if field in ("slot", "architecture"):
        source[field] = value
    with pytest.raises(OverrideError):
        flags.compose({}, change(), source)


@pytest.mark.parametrize(
    "row",
    [
        {"index": 1, "type": 6, "control_type": 7},
        {"index": True, "type": 6, "control_type": 6},
        {"index": 1, "type": 256, "control_type": 256},
        {"index": 1, "type": 6, "control_type": -1},
        {"index": 1, "type": 6},
        {"index": 257, "type": 6, "control_type": 6},
        {"index": 512, "type": 6, "control_type": 6},
    ],
)
def test_inventory_rows_must_exactly_match_the_mask_and_control_types(row):
    source = observation()
    source["entries"][0] = row
    with pytest.raises(OverrideError):
        flags.compose({}, change(), source)


@pytest.mark.parametrize("raw", [2, 255, -1, 256, True, None])
def test_cache_edit_requires_boolean_raw_flags(raw):
    source = observation()
    source["flags"]["limits_control_0x4c"]["raw"] = raw
    with pytest.raises(OverrideError):
        flags.compose({}, change(), source)


def test_missing_or_new_flag_layout_is_not_silently_assumed_writable():
    for name in ("limits_control_0x4c", flags.CACHE_FLAG):
        source = observation()
        source["flags"].pop(name)
        with pytest.raises(OverrideError):
            flags.compose({}, change(), source)
    source = observation()
    source["flags"]["new_flag"] = {"raw": 0}
    with pytest.raises(OverrideError, match="flag layout is not understood"):
        flags.compose({}, change(), source)


def test_contract_verifies_applied_value_and_exact_captured_undo():
    source = observation()
    saved = flags.contracts(change(), source)
    assert saved[flags.CACHE_KEY]["before"] is True
    assert saved[flags.CACHE_KEY]["after"] is False
    assert saved[flags.CACHE_KEY]["registry_mask"] == 0xC
    assert flags.CACHE_FLAG not in saved[flags.CACHE_KEY]["unchanged"]["flags"]
    applied = copy.deepcopy(source)
    set_cache(applied, False)
    assert flags.verify(applied, json.loads(json.dumps(saved))) == [flags.CACHE_KEY]
    assert flags.verify(source, saved, restore=True) == [flags.CACHE_KEY]
    with pytest.raises(OverrideError, match="expected active state"):
        flags.verify(source, saved)
    with pytest.raises(OverrideError, match="expected active state"):
        flags.verify(applied, saved, restore=True)


def test_contract_captures_initial_state_without_assuming_the_factory_default():
    source = observation(False)
    saved = flags.contracts(change(True), source)
    assert saved[flags.CACHE_KEY]["before"] is False
    assert flags.verify(source, saved, restore=True) == [flags.CACHE_KEY]


@pytest.mark.parametrize("mutation", ["identity", "slot", "architecture", "mask", "row_type", "peer", "extra_flag"])
def test_contract_rejects_target_inventory_and_untouched_flag_changes(mutation):
    saved = flags.contracts(change(), observation())
    current = observation(False)
    if mutation == "identity":
        current["identity"]["device_id"] += 1
    elif mutation == "slot":
        current["slot"] = current["identity"]["slot"] = "0000:05:00.1"
    elif mutation == "architecture":
        current["architecture"] = current["identity"]["architecture"] = 9
    elif mutation == "mask":
        current = observation(False, indices=(1, 256))
    elif mutation == "row_type":
        current["entries"][0].update(type=7, control_type=7)
    elif mutation == "peer":
        current["flags"]["limits_control_0x4c"]["raw"] = 1
    else:
        current["flags"].pop("limits_info_0x7a88")
    with pytest.raises(OverrideError, match="identity, inventory or untouched flags changed"):
        flags.verify(current, saved)


def test_dynamic_request_payloads_labels_and_timestamps_are_not_contract_fields():
    saved = flags.contracts(change(), observation())
    current = observation(False)
    current.update(captured_at=999, info_sha256="changed", control_sha256="changed")
    current["entries"][0].update(info_hex="aa", control_hex="bb", driver_label="OTHER")
    current["entries"].reverse()
    assert flags.verify(current, saved) == [flags.CACHE_KEY]


def test_contract_has_no_aliases_to_source_observation_or_changes():
    source, changes = observation(), change()
    saved = flags.contracts(changes, source)
    source["identity"]["device_id"] = 0
    source["entries"][0]["type"] = 0
    source["flags"]["limits_control_0x4c"]["raw"] = 1
    changes[flags.CACHE_KEY] = True
    assert flags.verify(observation(False), saved) == [flags.CACHE_KEY]


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", True),
        ("schema", 1.0),
        ("schema", 2),
        ("registry_mask", 12.0),
        ("registry_mask", 3),
        ("registry_name", "other"),
        ("before", 1),
        ("after", 0),
        ("unchanged", None),
    ],
)
def test_malformed_contract_cannot_be_verified(field, value):
    saved = flags.contracts(change(), observation())
    saved[flags.CACHE_KEY][field] = value
    with pytest.raises(OverrideError):
        flags.verify(observation(False), saved)


def test_receipt_numeric_types_are_exact_and_unknown_contract_keys_fail():
    saved = flags.contracts(change(), observation())
    saved[flags.CACHE_KEY]["unchanged"]["flags"]["limits_control_0x4c"] = False
    with pytest.raises(OverrideError):
        flags.verify(observation(False), saved)
    with pytest.raises(OverrideError):
        flags.verify(None, {"other": {}})
    with pytest.raises(OverrideError):
        flags.verify(None, [])


def fake_reads(monkeypatch, sources, *, statuses=None, errors=None, callback=None):
    target = SimpleNamespace(nvapi=SimpleNamespace(selected={"devid": 0xABCD, "subsys": 0x12345678}))
    calls = []

    class Reader:
        def __init__(self, gpu):
            assert gpu is target

        def read(self, groups):
            assert groups == ["limits"]
            index = len(calls)
            calls.append(groups)
            if callback:
                callback(target, index)
            status = statuses[index] if statuses else "ready"
            return {
                "groups": {
                    "limits": {
                        "status": status,
                        "snapshot": copy.deepcopy(sources[index]),
                        "last_valid": observation(),
                        "error": errors[index] if errors else None,
                    }
                }
            }

    monkeypatch.setattr(flags, "PerfDiagnosticsReader", Reader)
    return target, calls


def test_read_uses_two_fresh_snapshots_and_ignores_dynamic_payload_changes(monkeypatch):
    first, second = observation(), observation()
    second["control_sha256"] = "changing-request"
    second["entries"][0]["control_hex"] = "ff"
    target, calls = fake_reads(monkeypatch, [first, second])
    result, error = flags.read(target)
    assert error is None and result["stable_reads"] == 2
    assert result["cache_enabled"] is True and len(calls) == 2
    assert result["identity"] == second["identity"]
    assert result["control_sha256"] == "changing-request"


@pytest.mark.parametrize("mutation", ["cache", "mask", "identity"])
def test_read_rejects_unstable_target_inventory_or_flags(monkeypatch, mutation):
    first, second = observation(), observation()
    if mutation == "cache":
        set_cache(second, False)
    elif mutation == "mask":
        second = observation(indices=(0, 511))

    def callback(target, index):
        if mutation == "identity" and index:
            target.nvapi.selected["devid"] += 1

    target, calls = fake_reads(monkeypatch, [first, second], callback=callback)
    result, error = flags.read(target)
    assert result is None and "changed between reads" in error
    assert len(calls) == 2


@pytest.mark.parametrize("status", ["transient", "unsupported", "malformed", "identity", "ready"])
def test_read_never_substitutes_a_historical_snapshot(monkeypatch, status):
    target, calls = fake_reads(monkeypatch, [None], statuses=[status], errors=["fresh data unavailable"])
    result, error = flags.read(target)
    assert result is None and "fresh data unavailable" in error
    assert len(calls) == 1


def test_non_boolean_flags_remain_readable_but_cannot_authorize_composition(monkeypatch):
    source = observation()
    source["flags"][flags.CACHE_FLAG]["raw"] = 2
    target, _ = fake_reads(monkeypatch, [source, source])
    result, error = flags.read(target)
    assert error is None and result["cache_enabled"] is None
    assert result["flags"][flags.CACHE_FLAG]["raw"] == 2
    with pytest.raises(OverrideError, match="Boolean driver readback"):
        flags.compose({}, change(), result)


def test_unknown_flag_and_row_data_remain_readable(monkeypatch):
    source = observation()
    source["flags"]["future_flag"] = {"raw": 255}
    source["entries"][0].update(type=222, control_type=221)
    target, _ = fake_reads(monkeypatch, [source, source])
    result, error = flags.read(target)
    assert error is None and result["entries"][0]["type"] == 222
    assert result["flags"]["future_flag"]["raw"] == 255
    with pytest.raises(OverrideError):
        flags.compose({}, change(), result)


def test_empty_inventory_is_readable_but_not_writable(monkeypatch):
    source = observation(indices=())
    source["flags"] = {}
    target, _ = fake_reads(monkeypatch, [source, source], statuses=["empty", "empty"])
    result, error = flags.read(target)
    assert error is None and result["present_count"] == 0 and result["cache_enabled"] is None
    with pytest.raises(OverrideError, match="stable, populated"):
        flags.compose({}, change(), result)


def test_reader_failure_is_independent_and_does_not_swallow_process_interrupts(monkeypatch):
    def failure(*args):
        raise OSError("Getter unavailable")

    monkeypatch.setattr(flags, "PerfDiagnosticsReader", failure)
    assert flags.read(object()) == (None, "Getter unavailable")

    def interrupt(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(flags, "PerfDiagnosticsReader", interrupt)
    with pytest.raises(KeyboardInterrupt):
        flags.read(object())


def skip_observation(skip=False, cache=True, **kwargs):
    source = observation(cache, **kwargs)
    set_skip(source, skip)
    return source


def set_skip(snapshot, enabled):
    snapshot["flags"][flags.SKIP_FLAG]["raw"] = int(enabled)
    snapshot["skip_updates"] = enabled


@pytest.mark.parametrize("enabled,encoded", [(False, 1), (True, 2)])
def test_skip_updates_has_independent_explicit_encoding(enabled, encoded):
    source = skip_observation()
    changes = {flags.SKIP_KEY: enabled}
    assert flags.compose({}, changes, source) == registry(encoded)
    saved = flags.contracts(changes, source)
    assert set(saved) == {flags.SKIP_KEY}
    assert saved[flags.SKIP_KEY]["schema"] == 1
    assert saved[flags.SKIP_KEY]["registry_mask"] == 3
    assert saved[flags.SKIP_KEY]["before"] is False
    assert saved[flags.SKIP_KEY]["after"] is enabled
    assert flags.SKIP_FLAG not in saved[flags.SKIP_KEY]["unchanged"]["flags"]
    assert saved[flags.SKIP_KEY]["unchanged"]["flags"][flags.CACHE_FLAG] == 1
    current = skip_observation(enabled)
    assert flags.verify(current, json.loads(json.dumps(saved))) == [flags.SKIP_KEY]
    assert flags.verify(source, saved, restore=True) == [flags.SKIP_KEY]


@pytest.mark.parametrize("before_skip", [False, True])
@pytest.mark.parametrize("before_cache", [False, True])
@pytest.mark.parametrize("after_skip", [False, True])
@pytest.mark.parametrize("after_cache", [False, True])
def test_combined_changes_share_baseline_and_verify_every_apply_and_undo_state(
    before_skip, before_cache, after_skip, after_cache
):
    source = skip_observation(before_skip, before_cache)
    before_value = 0x80 | (2 if before_skip else 1) | (8 if before_cache else 4)
    before = registry(before_value)
    changes = {flags.SKIP_KEY: after_skip, flags.CACHE_KEY: after_cache}
    after_value = 0x80 | (2 if after_skip else 1) | (8 if after_cache else 4)
    assert flags.compose(before, changes, source) == registry(after_value)
    saved = flags.contracts(changes, source)
    receipt = json.loads(json.dumps(saved))
    current = skip_observation(after_skip, after_cache)
    assert flags.verify(current, receipt) == [flags.CACHE_KEY, flags.SKIP_KEY]
    assert flags.verify(source, receipt, restore=True) == [flags.CACHE_KEY, flags.SKIP_KEY]
    assert saved == receipt
    assert before == registry(before_value)


def test_skip_edit_preserves_cache_subclass_flags_and_registry_spelling():
    name = "rmperflimitsoverride"
    before = registry(0x89, name=name)
    before["Unrelated"] = {"type": 3, "value": b"unchanged"}
    expected = copy.deepcopy(before)
    expected[name]["value"] = 0x8A
    assert flags.compose(before, {flags.SKIP_KEY: True}, skip_observation()) == expected
    assert before[name]["value"] == 0x89


def test_skip_capability_uses_current_identity_and_inventory_not_developer_board():
    source = skip_observation(indices=(0, 345, 511), architecture=9, slot="0000:ae:18.6")
    source["identity"].update(device_id=0x1111, subsystem_id=0x00001234)
    source["flags"].pop("limits_info_0x7a88")
    source["entries"][1].update(type=77, control_type=77, issues=["Undecoded row"])
    changes = {flags.SKIP_KEY: True}
    assert flags.compose({}, changes, source) == registry(2)
    saved = flags.contracts(changes, source)
    current = copy.deepcopy(source)
    set_skip(current, True)
    assert flags.verify(current, saved) == [flags.SKIP_KEY]


@pytest.mark.parametrize("value", [0, 1, 1.0, None, "true"])
def test_skip_changes_are_strict_booleans(value):
    with pytest.raises(OverrideError, match="Only Boolean"):
        flags.compose({}, {flags.SKIP_KEY: value}, skip_observation())


@pytest.mark.parametrize("value", [0, 1, None, True, "false"])
def test_skip_observation_must_bind_derived_boolean_to_raw(value):
    source = skip_observation()
    source["skip_updates"] = value
    for changes in ({flags.SKIP_KEY: True}, change()):
        with pytest.raises(OverrideError, match="skip-updates state disagrees"):
            flags.contracts(changes, source)


def test_legacy_cache_only_observation_and_receipt_stay_compatible():
    source = observation()
    assert "skip_updates" not in source
    saved = flags.contracts(change(), source)
    assert saved[flags.CACHE_KEY] == {
        "schema": 1,
        "registry_name": flags.REGISTRY_NAME,
        "registry_mask": 12,
        "before": True,
        "after": False,
        "unchanged": {
            "identity": source["identity"],
            "layout": {
                key: source[key]
                for key in ("group", "capacity", "info_command", "control_command", "info_bytes", "control_bytes")
            },
            "mask_hex": source["mask_hex"],
            "row_types": [[1, 6, 6], [257, 6, 6]],
            "flags": {flags.SKIP_FLAG: 0, "limits_info_0x7a88": 1},
        },
    }
    assert flags.verify(observation(False), saved) == [flags.CACHE_KEY]
    assert flags.verify(skip_observation(False, False), saved) == [flags.CACHE_KEY]
    with pytest.raises(OverrideError, match="skip-updates state disagrees"):
        flags.compose({}, {flags.SKIP_KEY: True}, source)


@pytest.mark.parametrize("missing", [flags.SKIP_FLAG, flags.CACHE_FLAG])
def test_skip_edit_rejects_missing_shared_control_flags(missing):
    source = skip_observation()
    source["flags"].pop(missing)
    with pytest.raises(OverrideError, match="flag layout is not understood"):
        flags.compose({}, {flags.SKIP_KEY: True}, source)


@pytest.mark.parametrize("flag", [flags.SKIP_FLAG, flags.CACHE_FLAG, "limits_info_0x7a88"])
@pytest.mark.parametrize("raw", [True, 2, 255, None])
def test_skip_edit_does_not_treat_partial_or_unrecognized_flags_as_defaults(flag, raw):
    source = skip_observation()
    source["flags"][flag]["raw"] = raw
    with pytest.raises(OverrideError):
        flags.compose({}, {flags.SKIP_KEY: True}, source)


@pytest.mark.parametrize("remaining", [flags.CACHE_KEY, flags.SKIP_KEY])
def test_removing_combined_peer_contract_cannot_authorize_a_partial_receipt(remaining):
    saved = flags.contracts({flags.CACHE_KEY: False, flags.SKIP_KEY: True}, skip_observation())
    saved = {remaining: saved[remaining]}
    with pytest.raises(OverrideError, match="untouched flags changed"):
        flags.verify(skip_observation(True, False), saved)


@pytest.mark.parametrize("key,peer_flag", [(flags.CACHE_KEY, flags.SKIP_FLAG), (flags.SKIP_KEY, flags.CACHE_FLAG)])
@pytest.mark.parametrize("bad", [None, False, 2])
def test_combined_contract_rejects_peer_baseline_contradictions(key, peer_flag, bad):
    saved = flags.contracts({flags.CACHE_KEY: False, flags.SKIP_KEY: True}, skip_observation())
    saved[key]["unchanged"]["flags"][peer_flag] = bad
    with pytest.raises(OverrideError, match="captured baseline"):
        flags.verify(skip_observation(True, False), saved)


@pytest.mark.parametrize("key", [flags.CACHE_KEY, flags.SKIP_KEY])
@pytest.mark.parametrize("mutation", ["identity", "mask", "row_type", "untouched_flag"])
def test_combined_contract_cannot_hide_target_or_untouched_peer_changes(key, mutation):
    saved = flags.contracts({flags.CACHE_KEY: False, flags.SKIP_KEY: True}, skip_observation())
    unchanged = saved[key]["unchanged"]
    if mutation == "identity":
        unchanged["identity"]["subsystem_id"] += 1
    elif mutation == "mask":
        unchanged["mask_hex"] = "00" * 64
    elif mutation == "row_type":
        unchanged["row_types"][0][1] = 5
    else:
        unchanged["flags"]["limits_info_0x7a88"] = 0
    with pytest.raises(OverrideError, match="identity, inventory or untouched flags changed"):
        flags.verify(skip_observation(True, False), saved)


@pytest.mark.parametrize("key", [flags.CACHE_KEY, flags.SKIP_KEY])
@pytest.mark.parametrize("field,bad", [("schema", True), ("before", 0), ("after", 1), ("registry_mask", 15)])
def test_combined_contract_validates_each_record_before_verifying(key, field, bad):
    saved = flags.contracts({flags.CACHE_KEY: False, flags.SKIP_KEY: True}, skip_observation())
    saved[key][field] = bad
    with pytest.raises(OverrideError, match="Malformed"):
        flags.verify(skip_observation(True, False), saved)


@pytest.mark.parametrize("cache,skip", [(True, True), (False, False)])
def test_combined_partial_apply_is_rejected_and_original_state_remains_verifiable(cache, skip):
    source = skip_observation()
    saved = flags.contracts({flags.CACHE_KEY: False, flags.SKIP_KEY: True}, source)
    with pytest.raises(OverrideError):
        flags.verify(skip_observation(skip, cache), saved)
    assert flags.verify(source, saved, restore=True) == [flags.CACHE_KEY, flags.SKIP_KEY]


def test_read_derives_skip_updates_from_raw_flags(monkeypatch):
    source = observation()
    source["flags"][flags.SKIP_FLAG]["raw"] = 1
    source["skip_updates"] = False
    target, calls = fake_reads(monkeypatch, [source, source])
    current, error = flags.read(target)
    assert error is None and current["skip_updates"] is True and len(calls) == 2


def test_read_detects_skip_flag_change_even_if_cache_and_inventory_are_stable(monkeypatch):
    first, second = skip_observation(), skip_observation(True)
    target, _ = fake_reads(monkeypatch, [first, second])
    current, error = flags.read(target)
    assert current is None and "changed between reads" in error


def test_skip_unknown_readback_remains_diagnostic_without_edit_eligibility(monkeypatch):
    source = skip_observation()
    source["flags"][flags.SKIP_FLAG]["raw"] = 2
    target, _ = fake_reads(monkeypatch, [source, source])
    current, error = flags.read(target)
    assert error is None and current["skip_updates"] is None and current["cache_enabled"] is True
    with pytest.raises(OverrideError, match="Boolean driver readback"):
        flags.contracts({flags.SKIP_KEY: True}, current)


def test_transient_apply_read_does_not_verify_history_or_prevent_fresh_undo(monkeypatch):
    baseline = skip_observation()
    saved = flags.contracts({flags.CACHE_KEY: False, flags.SKIP_KEY: True}, baseline)
    target, _ = fake_reads(monkeypatch, [None], statuses=["transient"], errors=["GPU restarting"])
    current, error = flags.read(target)
    assert current is None and error == "GPU restarting"
    with pytest.raises(OverrideError, match="observation is unavailable"):
        flags.verify(current, saved)
    target, calls = fake_reads(monkeypatch, [baseline, baseline])
    restored, error = flags.read(target)
    assert error is None and len(calls) == 2
    assert flags.verify(restored, saved, restore=True) == [flags.CACHE_KEY, flags.SKIP_KEY]
