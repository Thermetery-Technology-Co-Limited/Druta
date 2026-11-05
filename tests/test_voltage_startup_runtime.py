"""Startup REL contracts with synthetic adapters, packets, and no GPU writes."""

import copy
import json
import struct
from types import SimpleNamespace

import pytest

from druta import voltage_startup_runtime as startup
from tests import test_vfe_equation_runtime as native
from tests.test_voltage_request_replay import Gpu


@pytest.fixture(autouse=True)
def fake_policy(monkeypatch):
    monkeypatch.setattr(startup.requests.voltage, "read", lambda gpu: gpu.policy())


def observation(*, rails=(0, 1), affected=(1,), boost=0, extra=(), **identity):
    gpu = Gpu(rails=rails, extra=extra, **identity)
    gpu.boost = boost
    for index in (*rails, *extra):
        gpu.info[startup.requests.HEADER + index * startup.requests.INFO_STRIDE + 0x33] = 3 if index in affected else 97
    state = startup.requests.read_checked(gpu)
    definitions = native.clamp_observation(**state["identity"])
    result = {"schema": 1, "requests": state, "equations": definitions}
    return startup.validate_observation(result)


def policy_offsets(state):
    """Make an independent fresh policy offset report for a synthetic request."""
    for index, raw in state["public"].items():
        at = startup.requests.controls.ROW_OFFSET + int(index) * startup.requests.controls.ROW_STRIDE
        values = startup.requests._six(bytes.fromhex(raw), at)
        state["policy"]["rails"][index]["offsets_uv"] = dict(
            zip(startup.requests.voltage._OFFSETS, values[:4], strict=True)
        )
    return state


def contract(*, enabled=True, **kwargs):
    return startup.from_observation(observation(**kwargs), {startup.KEY: enabled})


def applied(saved):
    return {
        "schema": 1,
        "requests": policy_offsets(copy.deepcopy(saved["after_requests"])),
        "equations": copy.deepcopy(saved["before"]["equations"]),
    }


def restart_observation(saved):
    result = applied(saved)
    wanted = -50000 if saved["selected"][startup.KEY] else 0
    values = {
        index: [wanted if index in saved["affected_indices"] else 0, 0, 0, 0, 0, 0]
        for index in result["requests"]["public"]
    }
    result["requests"] = policy_offsets(
        startup.requests.project(result["requests"], offsets_uv=values, boost_percent=0)
    )
    return result


@pytest.mark.parametrize("enabled,registry_value,wanted", [(True, 1, -50000), (False, 2, 0)])
@pytest.mark.parametrize("affected", [(0,), (1,), (0, 1)])
@pytest.mark.parametrize("boost", [0, 37])
def test_exact_rel_only_projection_preserves_other_five_words_peers_and_boost(
    enabled, registry_value, wanted, affected, boost
):
    before = observation(affected=affected, boost=boost)
    saved = startup.from_observation(before, {startup.KEY: enabled})
    assert startup.registry_dword(enabled) == registry_value
    assert saved["affected_indices"] == [str(index) for index in affected]
    assert saved["before"] == before and saved["before"] is not before
    startup.validate_contracts({startup.KEY: enabled}, saved)
    assert saved["after_requests"]["policy"] == before["requests"]["policy"]
    assert saved["after_requests"]["info_hex"] == before["requests"]["info_hex"]
    assert saved["after_requests"]["boost_percent"] == boost
    actual_native = bytearray.fromhex(before["requests"]["native_hex"])
    for index, raw in before["requests"]["public"].items():
        expected = bytearray.fromhex(raw)
        at = startup.requests.controls.ROW_OFFSET + int(index) * startup.requests.controls.ROW_STRIDE
        if int(index) in affected:
            struct.pack_into("<i", expected, at + 4, wanted)
            struct.pack_into("<i", actual_native, startup.requests.HEADER + int(index) * 32 + 4, wanted)
        assert saved["after_requests"]["public"][index] == expected.hex()
    assert saved["after_requests"]["native_hex"] == actual_native.hex()
    assert startup.verify(applied(saved), saved) == [startup.KEY]
    assert startup.verify(before, saved, restore=True) == [startup.KEY]


@pytest.mark.parametrize("rail", [0, 1])
@pytest.mark.parametrize("architecture", [8, 9])
def test_single_complete_rail_and_alternate_identity_are_supported(rail, architecture):
    saved = contract(
        rails=(rail,),
        affected=(rail,),
        architecture=architecture,
        slot="0000:91:13.6",
        device_id=0x4321,
        subsystem_id=0xA1B2C3D4,
    )
    assert saved["affected_indices"] == [str(rail)]
    assert saved["before"]["requests"]["complete"]
    assert startup.verify_activation(restart_observation(saved), saved) == [startup.KEY]
    assert startup.verify(applied(saved), saved) == [startup.KEY]


@pytest.mark.parametrize("current", [-654321, -50000, 0, 90000])
@pytest.mark.parametrize("enabled", [False, True])
def test_current_offset_does_not_select_provenance_or_change_explicit_request(current, enabled):
    before = observation(affected=(0,))
    raw = before["requests"]["public"]["0"]
    values = startup.requests._six(bytes.fromhex(raw), startup.requests.controls.ROW_OFFSET)
    values[0] = current
    before["requests"] = policy_offsets(startup.requests.project(before["requests"], offsets_uv={"0": values}))
    saved = startup.from_observation(before, {startup.KEY: enabled})
    after = applied(saved)
    assert after["requests"]["policy"]["rails"]["0"]["offsets_uv"]["reliability"] == (-50000 if enabled else 0)
    assert saved["before"]["requests"]["policy"]["rails"]["0"]["offsets_uv"]["reliability"] == current
    assert startup.verify(before, saved, restore=True) == [startup.KEY]


@pytest.mark.parametrize("enabled", [False, True])
def test_initializer_proof_precedes_replay_and_does_not_claim_final_preservation(enabled):
    saved = contract(enabled=enabled, boost=37)
    initialized = restart_observation(saved)
    assert startup.verify_activation_requests(initialized["requests"], saved) == [startup.KEY]
    assert startup.verify_activation(initialized, saved) == [startup.KEY]
    with pytest.raises(startup.VoltageStartupError, match="preserved requests"):
        startup.verify(initialized, saved)
    assert startup.verify(applied(saved), saved) == [startup.KEY]


@pytest.mark.parametrize("enabled", [False, True])
def test_ignored_registry_effect_is_rejected_before_replay(enabled):
    saved = contract(enabled=enabled)
    with pytest.raises(startup.VoltageStartupError, match="before request replay") as caught:
        startup.verify_activation_requests(saved["before"]["requests"], saved)
    assert caught.value.kind == "activation"
    with pytest.raises(startup.VoltageStartupError, match="before request replay"):
        startup.verify_activation(saved["before"], saved)


@pytest.mark.parametrize("boost", [0, 37])
def test_firmware_recomputation_scope_is_observed_without_a_voltage_formula(boost):
    saved = contract(boost=boost, affected=(0,))
    after = applied(saved)
    for index, row in after["requests"]["policy"]["rails"].items():
        if boost or index == "0":
            row["policy_uv"]["reliability"] += 1234
            row["policy_uv"]["effective"] -= 5678
        if boost:
            row["boost_uv"] += 991
    assert startup.verify(after, saved) == [startup.KEY]
    with pytest.raises(startup.VoltageStartupError):
        startup.verify(after, saved, restore=True)


@pytest.mark.parametrize("boost", [0, 37])
@pytest.mark.parametrize("index", ["0", "1"])
@pytest.mark.parametrize("field", ["alt_reliability", "overvoltage", "vmin"])
def test_every_other_limit_remains_exact(boost, index, field):
    saved = contract(boost=boost, affected=(0,))
    after = applied(saved)
    after["requests"]["policy"]["rails"][index]["policy_uv"][field] += 1
    with pytest.raises(startup.VoltageStartupError, match="preserved rail limits"):
        startup.verify(after, saved)


@pytest.mark.parametrize(
    "index,field", [("0", "boost_uv"), ("1", "boost_uv"), ("1", "reliability"), ("1", "effective")]
)
def test_zero_boost_keeps_unaffected_policies_and_all_boost_exact(index, field):
    saved = contract(boost=0, affected=(0,))
    after = applied(saved)
    row = after["requests"]["policy"]["rails"][index]
    if field == "boost_uv":
        row[field] += 1
    else:
        row["policy_uv"][field] += 1
    with pytest.raises(startup.VoltageStartupError, match="preserved rail limits"):
        startup.verify(after, saved)


@pytest.mark.parametrize("index,word", [("0", 0), ("0", 5), ("1", 1), ("1", 2), ("1", 3), ("1", 4), ("1", 5)])
def test_unowned_request_words_cannot_change(index, word):
    saved = contract()
    after = applied(saved)
    at = startup.requests.controls.ROW_OFFSET + int(index) * startup.requests.controls.ROW_STRIDE
    values = startup.requests._six(bytes.fromhex(after["requests"]["public"][index]), at)
    values[word] += 1
    after["requests"] = policy_offsets(startup.requests.project(after["requests"], offsets_uv={index: values}))
    with pytest.raises(startup.VoltageStartupError, match="preserved requests"):
        startup.verify(after, saved)


@pytest.mark.parametrize(
    "drift",
    [
        "boost",
        "public_opaque",
        "native_opaque",
        "info_opaque",
        "selector",
        "equation_info",
        "coefficient",
        "peer_coefficient",
    ],
)
def test_opaque_inventory_and_all_loaded_definitions_are_preserved(drift):
    saved = contract()
    after = applied(saved)
    state = after["requests"]
    if drift == "boost":
        after["requests"] = startup.requests.project(state, boost_percent=1)
    elif drift == "public_opaque":
        raw = bytearray.fromhex(state["public"]["0"])
        raw[-1] ^= 1
        state["public"]["0"] = raw.hex()
    elif drift in ("native_opaque", "info_opaque", "selector"):
        key = "native_hex" if drift == "native_opaque" else "info_hex"
        raw = bytearray.fromhex(state[key])
        raw[startup.requests.HEADER + startup.requests.INFO_STRIDE + 0x33 if drift == "selector" else -1] ^= 1
        state[key] = raw.hex()
    elif drift == "equation_info":
        native.mutate(after["equations"], "info", 3, 0xCC)
    elif drift == "peer_coefficient":
        native.mutate(after["equations"], "control", native.vfe.HEADER + 7999 * native.vfe.CONTROL_STRIDE + 37, 17)
    else:
        native.mutate(
            after["equations"], "control", native.vfe.HEADER + 37 * native.vfe.CONTROL_STRIDE + 0x2C, 999999, "f"
        )
    with pytest.raises(startup.VoltageStartupError):
        startup.verify(after, saved)
    if drift != "boost":
        with pytest.raises(startup.VoltageStartupError):
            startup.verify_activation(after, saved)


@pytest.mark.parametrize("drift", ["policy", "request", "coefficient", "info"])
def test_undo_requires_full_original_policy_requests_and_definitions(drift):
    saved = contract()
    before = copy.deepcopy(saved["before"])
    if drift == "policy":
        before["requests"]["policy"]["rails"]["1"]["policy_uv"]["reliability"] += 1
    elif drift == "request":
        before["requests"] = startup.requests.project(before["requests"], boost_percent=1)
    elif drift == "info":
        raw = bytearray.fromhex(before["requests"]["info_hex"])
        raw[-1] ^= 1
        before["requests"]["info_hex"] = raw.hex()
    else:
        native.mutate(
            before["equations"], "control", native.vfe.HEADER + 37 * native.vfe.CONTROL_STRIDE + 0x2C, 999999, "f"
        )
    with pytest.raises(startup.VoltageStartupError, match="undo did not restore"):
        startup.verify(before, saved, restore=True)


@pytest.mark.parametrize("failure", ["partial", "no_target"])
def test_partial_or_unselected_inventory_remains_diagnostic_but_cannot_create_contract(failure):
    obs = observation(extra=(7,) if failure == "partial" else (), affected=(1,) if failure == "partial" else ())
    assert startup.validate_observation(obs) == obs
    with pytest.raises(startup.VoltageStartupError) as caught:
        startup.from_observation(obs, {startup.KEY: True})
    assert caught.value.kind == "unsupported"


@pytest.mark.parametrize("value", [None, 0, 1, 2, 0.0, "true", [], {}])
def test_only_explicit_boolean_setting_is_accepted(value):
    with pytest.raises(startup.VoltageStartupError):
        startup.registry_dword(value)
    with pytest.raises(startup.VoltageStartupError):
        startup.from_observation(None, {startup.KEY: value})


def test_unrelated_changes_do_not_read_or_remove_other_capabilities(monkeypatch):
    monkeypatch.setattr(startup, "read_checked", lambda gpu: pytest.fail("Unrelated startup read"))
    assert startup.contracts(object(), {"clock.0.maximum": 1001}) == {}
    assert startup.from_observation(None, {}) == {}
    startup.validate_contracts({}, {})
    assert startup.verify(None, {}) == startup.verify_activation(None, {}) == []
    assert startup.verify_activation_requests(None, {}) == []
    with pytest.raises(startup.VoltageStartupError, match="separately"):
        startup.contracts(object(), {startup.KEY: True, "clock.0.maximum": 1001})


@pytest.mark.parametrize(
    "mutation",
    [
        "schema",
        "selected",
        "missing",
        "extra",
        "affected",
        "order",
        "after_offset",
        "after_policy",
        "after_info",
        "before_identity",
    ],
)
def test_pure_receipt_validation_recomputes_inventory_and_projection(mutation):
    saved = contract(affected=(0, 1))
    if mutation == "schema":
        saved["schema"] = True
    elif mutation == "selected":
        saved["selected"][startup.KEY] = 1
    elif mutation == "missing":
        saved.pop("before")
    elif mutation == "extra":
        saved["unused"] = None
    elif mutation == "affected":
        saved["affected_indices"] = ["1"]
    elif mutation == "order":
        saved["affected_indices"].reverse()
    elif mutation == "after_offset":
        saved["after_requests"] = startup.requests.project(saved["after_requests"], offsets_uv={"1": [0] * 6})
    elif mutation == "after_policy":
        saved["after_requests"]["policy"]["rails"]["1"]["offsets_uv"]["reliability"] = -50000
    elif mutation == "after_info":
        raw = bytearray.fromhex(saved["after_requests"]["info_hex"])
        raw[-1] ^= 1
        saved["after_requests"]["info_hex"] = raw.hex()
    else:
        saved["before"]["requests"]["identity"]["device_id"] = float(
            saved["before"]["requests"]["identity"]["device_id"]
        )
    with pytest.raises(startup.VoltageStartupError):
        startup.validate_contracts({startup.KEY: True}, saved)


@pytest.mark.parametrize(
    "failure",
    [
        "equation_identity",
        "policy_identity",
        "policy_offset",
        "native_offset",
        "native_type",
        "missing_equations",
        "schema",
    ],
)
def test_fresh_observation_requires_strict_independent_binding(failure):
    obs = observation()
    if failure == "equation_identity":
        obs["equations"]["identity"]["device_id"] ^= 1
    elif failure == "policy_identity":
        obs["requests"]["policy"]["identity"]["subsystem_id"] ^= 1
    elif failure == "policy_offset":
        obs["requests"]["policy"]["rails"]["1"]["offsets_uv"]["reliability"] += 1
    elif failure in ("native_offset", "native_type"):
        raw = bytearray.fromhex(obs["requests"]["native_hex"])
        raw[startup.requests.HEADER + (4 if failure == "native_offset" else 0)] ^= 1
        obs["requests"]["native_hex"] = raw.hex()
    elif failure == "missing_equations":
        obs["equations"] = None
    else:
        obs["schema"] = True
    with pytest.raises(startup.VoltageStartupError):
        startup.validate_observation(obs)


class TrackingLock:
    def __init__(self):
        self.depth = 0

    def __enter__(self):
        self.depth += 1

    def __exit__(self, *args):
        self.depth -= 1


def reader(monkeypatch, obs):
    gpu = SimpleNamespace(_lock=TrackingLock())
    calls = []

    def current(target):
        assert target is gpu and gpu._lock.depth == 1
        calls.append("requests")
        return copy.deepcopy(obs["requests"])

    def definitions(target):
        assert target is gpu and gpu._lock.depth == 1
        calls.append("equations")
        return copy.deepcopy(obs["equations"]), None

    monkeypatch.setattr(startup.requests, "read_checked", current)
    monkeypatch.setattr(startup.equations, "read", definitions)
    return gpu, calls, current, definitions


@pytest.mark.parametrize("partial", [False, True])
def test_read_brackets_definitions_with_stable_requests_under_lock(monkeypatch, partial):
    obs = observation(extra=(7,) if partial else ())
    gpu, calls, _, _ = reader(monkeypatch, obs)
    assert startup.read_checked(gpu) == obs
    assert calls == ["requests", "equations", "requests"]
    assert gpu._lock.depth == 0
    assert startup.read(gpu) == (obs, None)


@pytest.mark.parametrize("failure", ["transient", "policy_drift", "request_drift"])
def test_read_retries_known_transient_observation_once(monkeypatch, failure):
    obs = observation()
    gpu, calls, current, _ = reader(monkeypatch, obs)
    count = [0]

    def read(target):
        count[0] += 1
        if failure == "transient" and count[0] == 1:
            raise startup.requests.ReplayError("transient", "request read failed briefly")
        result = current(target)
        if count[0] == 2:
            if failure == "policy_drift":
                result["policy"]["rails"]["1"]["boost_uv"] += 1
            elif failure == "request_drift":
                result = startup.requests.project(result, boost_percent=1)
        return result

    monkeypatch.setattr(startup.requests, "read_checked", read)
    assert startup.read_checked(gpu) == obs
    assert count[0] == (3 if failure == "transient" else 4)
    assert calls.count("equations") == (1 if failure == "transient" else 2)
    assert gpu._lock.depth == 0


@pytest.mark.parametrize("kind", ["unsupported", "identity", "malformed", "transient"])
def test_read_preserves_typed_failure_and_bounded_retry(monkeypatch, kind):
    gpu = SimpleNamespace(_lock=TrackingLock())
    count = []
    failure = startup.requests.ReplayError(kind, "typed rail failure")

    def read(target):
        count.append(target)
        raise failure

    monkeypatch.setattr(startup.requests, "read_checked", read)
    monkeypatch.setattr(startup.equations, "read", lambda gpu: pytest.fail("No equation GET after failed requests"))
    with pytest.raises(startup.VoltageStartupError) as caught:
        startup.read_checked(gpu)
    assert caught.value.kind == kind and caught.value.__cause__ is failure
    assert len(count) == (2 if kind == "transient" else 1) and gpu._lock.depth == 0
    assert startup.read(gpu) == (None, "typed rail failure")


def test_missing_equation_get_is_unavailable_without_inventing_capability(monkeypatch):
    obs = observation()
    gpu, calls, _, _ = reader(monkeypatch, obs)
    monkeypatch.setattr(startup.equations, "read", lambda gpu: (None, "VFE GET unsupported"))
    with pytest.raises(startup.VoltageStartupError) as caught:
        startup.read_checked(gpu)
    assert caught.value.kind == "unavailable" and calls == ["requests"]


def test_contracts_capture_and_pure_serialization_keep_definitions_once(monkeypatch):
    obs = observation(boost=37)
    gpu, calls, _, _ = reader(monkeypatch, obs)
    saved = startup.contracts(gpu, {startup.KEY: False})
    assert calls == ["requests", "equations", "requests"]
    serialized = json.dumps(saved, allow_nan=False)
    assert len(serialized) < 4 * 1024 * 1024
    assert serialized.count('"equations"') == 1
    assert json.loads(serialized) == saved
    startup.validate_contracts({startup.KEY: False}, saved)
