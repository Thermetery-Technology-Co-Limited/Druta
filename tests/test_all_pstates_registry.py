import copy
import ctypes
import struct
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from druta import all_pstates_registry as aps
from druta.perf_diagnostics import PerfDiagnosticError
from druta.registry_overrides import OverrideError

IDENTITY = {"slot": "0000:03:00.0", "architecture": 8, "device_id": 0x4321, "subsystem_id": 0xABCD1234}


def observation(enabled=False, *, states=(0, 3, 8), identity=None, voltage=False, version=0x31CF8):
    """Independent binary fixture with hidden nonzero offsets and nonunit scale."""
    public = bytearray(7316 if version == 0x11C94 else 7416)
    native, mapping = bytearray(0x3D98), bytearray(0x188)
    mapped = ((0, 0, 100), (4, 4, 50), (8, 21, 100)) if version == 0x31CF8 else ((0, 0, 100), (4, 4, 50))
    struct.pack_into("<5I", public, 0, version, 1, len(states), len(mapped), int(voltage))
    struct.pack_into("<5I", native, 0, 0x23D98, 1, len(states), 4, int(voltage))
    struct.pack_into("<I", mapping, 0, 0x20188 if version == 0x31CF8 else 0x10188)
    for i in range(32):
        struct.pack_into("<3I", mapping, 8 + 12 * i, 32, 0, 100)
    for domain, target, scale in mapped:
        struct.pack_into("<3I", mapping, 8 + 12 * domain, target, 1, scale)
    for i, state in enumerate(states):
        pp, np = 20 + i * 456, 20 + i * 968
        editable = state == 0 or enabled
        struct.pack_into("<2I", public, pp, state, int(editable))
        struct.pack_into("<2I", native, np, state, int(editable))
        for c, domain in enumerate((0, 4, 21, 31)):
            cp = np + 8 + c * 44
            flag = editable and domain in (0, 4)
            triple = (4000 + i * 1000, -500000, 1000000) if domain in (0, 4) else (0, 0, 0)
            data = (180000 + i * 1000, 3000000 - i * 1000, 16, 0, 0)
            struct.pack_into("<3I3i5I", native, cp, domain, 2, int(flag), *triple, *data)
            if c < len(mapped):
                public_domain, scale = ((0, 100), (4, 50), (8, 100))[c]
                visible = tuple(x * scale // 100 for x in triple) if flag else (0, 0, 0)
                struct.pack_into("<3I3i5I", public, pp + 8 + c * 44, public_domain, 1, int(flag), *visible, *data)
        if voltage:
            flag, triple = int(editable), (1000 + i * 100, -5000, 10000)
            struct.pack_into("<4I3iI", native, np + 712, 0, 0, flag, 900000 + i * 1000, *triple, 0)
            struct.pack_into("<3I3i", public, pp + 360, 0, flag, 900000 + i * 1000, *(triple if flag else (0, 0, 0)))
    result = aps._observe(copy.deepcopy(identity or IDENTITY), bytes(public), bytes(native), bytes(mapping))
    result["stable_reads"] = 2
    return result


def changed_packet(obs, name, offset, value, fmt="<I"):
    result = copy.deepcopy(obs)
    raw = bytearray.fromhex(result[name])
    struct.pack_into(fmt, raw, offset, value)
    result[name] = raw.hex()
    return result


def test_enable_contract_matches_independent_visible_projection():
    before, after = observation(), observation(True)
    saved = aps.contracts({aps.KEY: True}, before)
    assert saved[aps.KEY]["after_state"]["extended_hex"] == after["extended_hex"]
    assert saved[aps.KEY]["after_state"]["public_hex"] == after["public_hex"]
    assert aps.verify(after, saved) == [aps.KEY]
    assert aps.verify(before, saved, restore=True) == [aps.KEY]
    assert before["extended"]["pstates"][1]["clocks"][0]["delta"]["value"] == 5000
    assert before["public"]["pstates"][1]["clocks"][0]["delta"]["value"] == 0
    assert after["public"]["pstates"][1]["clocks"][0]["delta"]["value"] == 5000


@pytest.mark.parametrize("version", (0x11C94, 0x21CF8, 0x31CF8))
def test_supported_public_layouts(version):
    saved = aps.contracts({aps.KEY: True}, observation(version=version))
    assert aps.verify(observation(True, version=version), saved) == [aps.KEY]
    assert aps.verify(observation(version=version), saved, restore=True) == [aps.KEY]
    expected_map = 0x20188 if version == 0x31CF8 else 0x10188
    assert struct.unpack_from("<I", bytes.fromhex(saved[aps.KEY]["before_state"]["map_hex"])) == (expected_map,)
    assert saved[aps.KEY]["after_state"]["map_hex"] == saved[aps.KEY]["before_state"]["map_hex"]


@pytest.mark.parametrize("version", (0x11C94, 0x21CF8, 0x31CF8))
def test_map_version_must_match_public_layout(version):
    wrong_version = 0x10188 if version == 0x31CF8 else 0x20188
    obs = changed_packet(observation(version=version), "map_hex", 0, wrong_version)
    with pytest.raises(OverrideError, match="public layout"):
        aps.contracts({aps.KEY: True}, obs)


@pytest.mark.parametrize("offset,value", ((0, 21), (4, 1), (8, 99)))
def test_legacy_map_requires_traced_suppressed_domain8(offset, value):
    obs = changed_packet(observation(version=0x21CF8), "map_hex", 8 + 8 * 12 + offset, value)
    with pytest.raises(OverrideError, match="domain8"):
        aps.contracts({aps.KEY: True}, obs)


@pytest.mark.parametrize("version", (0x11C94, 0x21CF8, 0x31CF8))
def test_voltage_capability_gate_preserves_voltage_and_hidden_delta(version):
    before, after = observation(voltage=True, version=version), observation(True, voltage=True, version=version)
    saved = aps.contracts({aps.KEY: True}, before)
    assert aps.verify(after, saved) == [aps.KEY]
    assert aps.verify(before, saved, restore=True) == [aps.KEY]
    assert before["public"]["pstates"][1]["voltages"][0]["voltage_uv"] == 901000


@pytest.mark.parametrize("value", (1, 0x80000000, 0xFFFFFFFF))
def test_false_removes_exact_named_nonzero_override(value):
    name = "rmenableoverclockingallpstates"
    before = {name: {"type": 4, "value": value}, "other": {"type": 3, "value": "deadbeef"}}
    result = aps.compose(before, {aps.KEY: False}, observation(True))
    assert result == {"other": before["other"]}
    assert before[name]["value"] == value
    saved = aps.contracts({aps.KEY: False}, observation(True))
    assert aps.verify(observation(), saved) == [aps.KEY]
    assert aps.verify(observation(True), saved, restore=True) == [aps.KEY]


def test_enable_preserves_unknown_registry_state_and_input():
    original = {"other": {"type": 7, "value": ["one", "two"]}}
    result = aps.compose(original, {aps.KEY: True}, observation())
    assert result[aps.REGISTRY_NAME] == {"type": 4, "value": 1}
    result["other"]["value"].append("three")
    assert original == {"other": {"type": 7, "value": ["one", "two"]}}


@pytest.mark.parametrize("registry", ({}, {aps.REGISTRY_NAME: {"type": 4, "value": 0}}))
def test_naturally_enabled_default_cannot_be_disabled_by_absence(registry):
    with pytest.raises(OverrideError, match="driver default"):
        aps.compose(registry, {aps.KEY: False}, observation(True))


@pytest.mark.parametrize("enabled", (False, True))
def test_noop_has_no_contract_or_registry_change(enabled):
    before = {aps.REGISTRY_NAME: {"type": 4, "value": 47 if enabled else 0}}
    assert aps.compose(before, {aps.KEY: enabled}, observation(enabled)) == before
    assert aps.contracts({aps.KEY: enabled}, observation(enabled)) == {}


def test_empty_contracts_do_not_require_getters():
    assert aps.contracts({}, None) == {}
    assert aps.verify(None, {}) == []
    assert aps.validate_existing({}, None) is None


@pytest.mark.parametrize("changes", ({aps.KEY: 1}, {aps.KEY: "true"}, {"unknown": True}, [], None))
def test_strict_change_types(changes):
    with pytest.raises(OverrideError):
        aps.compose({}, changes, observation())


@pytest.mark.parametrize(
    "entry",
    (
        {"type": 3, "value": "01"},
        {"type": 4, "value": True},
        {"type": True, "value": 1},
        {"type": 4, "value": -1},
        {"type": 4, "value": 1 << 32},
        None,
    ),
)
def test_preserve_unrecognized_existing_types(entry):
    with pytest.raises(OverrideError):
        aps.compose({aps.REGISTRY_NAME: entry}, {aps.KEY: True}, observation())


def test_duplicate_registry_names_rejected():
    with pytest.raises(OverrideError):
        aps.compose(
            {aps.REGISTRY_NAME: {"type": 4, "value": 0}, aps.REGISTRY_NAME.lower(): {"type": 4, "value": 0}},
            {aps.KEY: True},
            observation(),
        )


@pytest.mark.parametrize("value,enabled", ((1, False), (123, False)))
def test_pending_registry_mismatch_rejected(value, enabled):
    with pytest.raises(OverrideError, match="pending"):
        aps.validate_existing({aps.REGISTRY_NAME: {"type": 4, "value": value}}, observation(enabled))


def test_zero_is_inherit_and_does_not_reject_naturally_enabled_state():
    assert aps.validate_existing({aps.REGISTRY_NAME: {"type": 4, "value": 0}}, observation(True)) is None
    assert aps.validate_existing({aps.REGISTRY_NAME: {"type": 4, "value": 0}}, None) is None


def test_alternate_architecture_identity_and_indices_supported():
    identity = {"slot": "0000:07:02.1", "architecture": 10, "device_id": 0x9876, "subsystem_id": 2}
    before = observation(states=(0, 2, 15), identity=identity)
    saved = aps.contracts({aps.KEY: True}, before)
    assert aps.verify(observation(True, states=(0, 2, 15), identity=identity), saved) == [aps.KEY]


@pytest.mark.parametrize(
    "key,value", (("device_id", 9), ("subsystem_id", 9), ("slot", "0000:04:00.0"), ("architecture", 10))
)
def test_changed_target_rejected(key, value):
    saved = aps.contracts({aps.KEY: True}, observation())
    after = observation(True)
    after["identity"][key] = value
    with pytest.raises(OverrideError):
        aps.verify(after, saved)


@pytest.mark.parametrize(
    "mutate",
    (
        lambda o: changed_packet(o, "extended_hex", 20 + 968 + 8 + 24, 123456),
        lambda o: changed_packet(o, "extended_hex", 20 + 968 + 8 + 16, -400000, "<i"),
        lambda o: changed_packet(o, "extended_hex", 20 + 8 + 12, 9999, "<i"),
        lambda o: changed_packet(o, "extended_hex", 20 + 8 + 3 * 44 + 24, 2),
        lambda o: changed_packet(o, "public_hex", 20 + 968, 9999),
        lambda o: changed_packet(o, "map_hex", 8 + 4 * 12 + 8, 100),
    ),
)
def test_changed_native_defaults_ranges_peers_or_map_rejected(mutate):
    saved = aps.contracts({aps.KEY: True}, observation())
    with pytest.raises(OverrideError):
        aps.verify(mutate(observation(True)), saved)


@pytest.mark.parametrize(
    "name,offset,value",
    (
        ("extended_hex", 0, 1),
        ("extended_hex", 8, 17),
        ("extended_hex", 12, 17),
        ("extended_hex", 16, 9),
        ("public_hex", 12, 9),
        ("map_hex", 0, 0),
        ("map_hex", 16, 0),
        ("extended_hex", 20 + 968, 0),
        ("extended_hex", 20 + 8 + 44, 0),
    ),
)
def test_geometry_and_mapping_corruption_rejected(name, offset, value):
    with pytest.raises(OverrideError):
        aps.contracts({aps.KEY: True}, changed_packet(observation(), name, offset, value))


def test_missing_p0_retains_diagnostics_without_write_capability():
    obs = observation(states=(3, 8))
    assert obs["enabled"] is None
    assert obs["issues"]
    with pytest.raises(OverrideError):
        aps.contracts({aps.KEY: True}, obs)


def test_mixed_editability_refuses_composition():
    obs = observation()
    obs = changed_packet(obs, "extended_hex", 20 + 968 + 8 + 8, 1)
    # Public/aggregate mismatch also independently makes this unusable.
    with pytest.raises(OverrideError):
        aps.compose({}, {aps.KEY: True}, obs)


@pytest.mark.parametrize("field,value", (("stable_reads", True), ("stable_reads", 1), ("enabled", 0)))
def test_stability_and_annotation_are_strict(field, value):
    obs = observation()
    obs[field] = value
    with pytest.raises(OverrideError):
        aps.contracts({aps.KEY: True}, obs)


def test_failed_activation_and_failed_restore_cannot_pass():
    saved = aps.contracts({aps.KEY: True}, observation())
    with pytest.raises(OverrideError):
        aps.verify(observation(), saved)
    with pytest.raises(OverrideError):
        aps.verify(observation(True), saved, restore=True)
    assert aps.verify(observation(), saved, restore=True) == [aps.KEY]


def test_contract_cannot_hide_other_changes():
    saved = aps.contracts({aps.KEY: True}, observation())
    saved[aps.KEY]["after_state"]["public_hex"] = changed_packet(observation(True), "public_hex", 3000, 1)["public_hex"]
    with pytest.raises(OverrideError, match="unrelated"):
        aps.verify(observation(True), saved)


@pytest.mark.parametrize("field,value", (("schema", True), ("before", 0), ("after", 1), ("registry_name", "other")))
def test_contract_type_and_name_tampering_rejected(field, value):
    saved = aps.contracts({aps.KEY: True}, observation())
    saved[aps.KEY][field] = value
    with pytest.raises(OverrideError):
        aps.verify(observation(True), saved)


def test_read_recovers_transient_without_historical_success(monkeypatch):
    calls = iter((PerfDiagnosticError("transient", "busy"), observation(), observation()))

    def once(*_):
        item = next(calls)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(aps, "_once", once)
    result, error = aps.read(SimpleNamespace(_lock=nullcontext()))
    assert error is None and result["stable_reads"] == 2
    monkeypatch.setattr(aps, "_once", lambda *_: (_ for _ in ()).throw(PerfDiagnosticError("transient", "gone")))
    assert aps.read(SimpleNamespace(_lock=nullcontext())) == (None, "gone")


def test_read_malformed_does_not_retry_as_capability(monkeypatch):
    calls = []

    def once(*_):
        calls.append(1)
        raise PerfDiagnosticError("malformed", "count")

    monkeypatch.setattr(aps, "_once", once)
    assert aps.read(SimpleNamespace(_lock=nullcontext())) == (None, "count")
    assert len(calls) == 1


def test_unstable_read_retries_complete_pair(monkeypatch):
    rounds = iter((observation(), observation(True), observation(), observation()))
    monkeypatch.setattr(aps, "_once", lambda *_: next(rounds))
    result, error = aps.read(SimpleNamespace(_lock=nullcontext()))
    assert error is None and result["enabled"] is False


@pytest.mark.parametrize("code,kind", ((-9, "unsupported"), (-104, "unsupported"), (-1, "transient")))
def test_get_status_classification(code, kind):
    api = SimpleNamespace(gpu=1, _i=lambda *_: lambda *_: code)
    with pytest.raises(PerfDiagnosticError) as error:
        aps._get(api, 0xC5DDF56E, 0x23D98, 0x3D98)
    assert error.value.kind == kind


def test_get_guard_detects_geometry_overwrite():
    def getter(_gpu, ptr):
        ctypes.memset(ctypes.cast(ptr, ctypes.c_void_p).value + 0x188, 0, 1)
        return 0

    api = SimpleNamespace(gpu=1, _i=lambda *_: getter)
    with pytest.raises(PerfDiagnosticError, match="outside"):
        aps._get(api, 0x1B46D4CC, 0x20188, 0x188)


def test_reader_requires_stable_paired_identity(monkeypatch):
    source = observation()
    identities = iter((IDENTITY, {**IDENTITY, "device_id": 7}))
    monkeypatch.setattr(aps, "_binding", lambda _: (SimpleNamespace(), next(identities)))
    monkeypatch.setattr(
        aps, "_get", lambda _api, query, *_: bytes.fromhex(source["map_hex" if query == 0x1B46D4CC else "extended_hex"])
    )
    gpu = SimpleNamespace(_read_pstates20=lambda: (bytes.fromhex(source["public_hex"]), None))
    with pytest.raises(PerfDiagnosticError, match="identity changed"):
        aps._once(gpu, object())


@pytest.mark.parametrize("version,expected_map", ((0x11C94, 0x10188), (0x21CF8, 0x10188), (0x31CF8, 0x20188)))
def test_reader_requests_map_corresponding_to_accepted_public_layout(monkeypatch, version, expected_map):
    source = observation(version=version)
    calls = []
    monkeypatch.setattr(aps, "_binding", lambda _: (SimpleNamespace(), IDENTITY))

    def get(_api, query, request_version, size):
        calls.append((query, request_version, size))
        return bytes.fromhex(source["map_hex" if query == 0x1B46D4CC else "extended_hex"])

    monkeypatch.setattr(aps, "_get", get)
    gpu = SimpleNamespace(_read_pstates20=lambda: (bytes.fromhex(source["public_hex"]), None))
    result = aps._once(gpu, object())
    assert calls == [(0x1B46D4CC, expected_map, 0x188), (0xC5DDF56E, 0x23D98, 0x3D98)]
    assert result["map_hex"] == source["map_hex"]


def test_reader_rejects_unknown_public_layout_before_map_get(monkeypatch):
    source = changed_packet(observation(), "public_hex", 0, 0x41CF8)
    monkeypatch.setattr(aps, "_binding", lambda _: (SimpleNamespace(), IDENTITY))
    calls = []
    monkeypatch.setattr(aps, "_get", lambda *_: calls.append(1))
    gpu = SimpleNamespace(_read_pstates20=lambda: (bytes.fromhex(source["public_hex"]), None))
    with pytest.raises(PerfDiagnosticError, match="version/size"):
        aps._once(gpu, object())
    assert calls == []


def test_contract_cannot_substitute_another_public_map_version():
    before, after = observation(), observation(True)
    saved = aps.contracts({aps.KEY: True}, before)
    saved[aps.KEY]["after_state"]["map_hex"] = changed_packet(after, "map_hex", 0, 0x10188)["map_hex"]
    with pytest.raises(OverrideError, match="public layout"):
        aps.verify(after, saved)
