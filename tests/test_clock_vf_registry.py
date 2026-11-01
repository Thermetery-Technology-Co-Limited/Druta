"""Offline clock VF transport, exact selector composition and undo contracts."""

import copy
import ctypes
import json
import threading
from types import SimpleNamespace

import pytest

from druta import clock_vf_registry as vf
from druta.registry_overrides import OverrideError


def raw_header(mono=True, smooth=True):
    raw = bytearray([0xA5]) * 60
    raw[0] = 255
    raw[4:8] = bytes(4)
    raw[8:14] = bytes((0, 0, int(mono), int(smooth), 0, 0))
    raw[0x10:0x38] = bytes(range(40))
    raw[0x38] = raw[8]
    return raw


def observation(mono=True, smooth=True, *, architecture=8, slot="0000:72:1d.3"):
    return {
        "group": "clock_domains",
        "control_command": "0x2080901B",
        "control_bytes": 2108,
        "request_mask": 0,
        "header_hex": raw_header(mono, smooth).hex(),
        "stable_reads": 2,
        "identity": {"slot": slot, "architecture": architecture, "device_id": 0xABCD, "subsystem_id": 0x12345678},
        "vf_monotonicity_enforced": mono,
        "vf_smoothening_enforced": smooth,
    }


def registry(value, name=vf.REGISTRY_NAME):
    return {name: {"type": 4, "value": value}}


def set_flag(source, key, enabled):
    name, offset = ("vf_monotonicity_enforced", 10) if key == vf.MONOTONICITY_KEY else ("vf_smoothening_enforced", 11)
    raw = bytearray.fromhex(source["header_hex"])
    raw[offset] = int(enabled)
    source["header_hex"], source[name] = raw.hex(), enabled


class FakeGPU:
    def __init__(self, *, architecture=8, actions=()):
        self._lock = threading.RLock()
        self.pairing_error = ""
        self.architecture = architecture
        self.address = "0000:72:1d.3"
        self.nvapi = SimpleNamespace(
            ok=True, gpu=123, selected={"slot": self.address, "handle": 123, "devid": 0xABCD, "subsys": 0x12345678}
        )
        self.nvml = SimpleNamespace(selected={"slot": self.address, "devid": 0xABCD, "subsys": 0x12345678})
        self._current_limit_transport = None
        self._current_limit_layout_choice = "old"
        self.transport_count = 0
        self.calls = []
        self.actions = list(actions)
        self.raw = raw_header()

    def slot(self):
        return self.address

    def arch(self):
        return self.architecture

    def _current_limit_transport_layouts(self):
        self.transport_count += 1
        if self._current_limit_transport is None:
            header = [0] * 17
            header[0], header[1], header[2] = 0x4E564441, 0x10002, 68 + 16
            header[3], header[4] = 0x4E562A2A, 0x0500002B
            header[14], header[15] = 0x2080A612, 16
            self._current_limit_transport = (
                header,
                dict.fromkeys(("hAdapter", "hDevice", "Type", "Flags", "hContext"), 0),
            )

    def _legacy_clk_escape(self, packet, fields):
        self.calls.append((packet[14], packet[15], packet[18], packet[5], packet[11]))
        assert self.calls[-1] == (0x2080901B, 0x83C, 0, 0, 0)
        ctypes.memmove(ctypes.addressof(packet) + 68, bytes(self.raw), 60)
        packet[5], packet[11], packet[16] = 1, 1, 0
        action = self.actions.pop(0) if self.actions else None
        if action:
            return action(self, packet)
        return 0


def mutate_byte(offset, value):
    def action(gpu, packet):
        ctypes.c_ubyte.from_address(ctypes.addressof(packet) + 68 + offset).value = value
        return 0

    return action


def mutate_word(index, value):
    def action(gpu, packet):
        packet[index] = value
        return 0

    return action


def test_empty_unrelated_transactions_need_no_clock_capability():
    assert vf.contracts({}, None) == {}
    assert vf.verify(None, {}) == []
    assert vf.validate_existing({}, None) is None
    assert vf.validate_existing({"Other": {"type": 3, "value": "opaque"}}, None) is None


@pytest.mark.parametrize(
    "key,enabled,encoded",
    [
        (vf.MONOTONICITY_KEY, False, 3),
        (vf.MONOTONICITY_KEY, True, 1),
        (vf.SMOOTHENING_KEY, False, 12),
        (vf.SMOOTHENING_KEY, True, 4),
    ],
)
def test_explicit_selectors_from_absent(key, enabled, encoded):
    before, source = {}, observation()
    assert vf.compose(before, {key: enabled}, source) == registry(encoded)
    assert before == {} and source == observation()


@pytest.mark.parametrize("value", range(16))
def test_every_low_selector_encoding_preserves_peer_and_unrelated_registry(value):
    mono, smooth = value & 3 != 3, (value >> 2) & 3 != 3
    source = observation(mono, smooth)
    before = registry(value, "RMCLKVFOVERRIDE")
    before["Opaque"] = {"type": 7, "value": ["keep", "exact"]}
    saved = copy.deepcopy(before)
    vf.validate_existing(before, source)
    desired = vf.compose(before, {vf.MONOTONICITY_KEY: not mono}, source)
    assert desired["RMCLKVFOVERRIDE"] == {"type": 4, "value": (value & ~3) | (3 if mono else 1)}
    desired["Opaque"]["value"].append("different")
    assert before == saved


@pytest.mark.parametrize("value", [0x10, 0x20, 0x30, 0x40, 0x80, 0x100, 0x80000000, 0xFFFFFFFF])
def test_unreadable_high_bit_consumers_are_rejected_without_clearing(value):
    before = registry(value)
    with pytest.raises(OverrideError, match="high-bit consumers"):
        vf.validate_existing(before, observation())
    with pytest.raises(OverrideError, match="high-bit consumers"):
        vf.compose(before, {vf.MONOTONICITY_KEY: False}, observation())
    assert before == registry(value)


@pytest.mark.parametrize(
    "value,mono,smooth",
    [(0, False, True), (2, False, True), (8, True, False), (1, False, True), (3, True, True), (12, True, True)],
)
def test_pending_or_unreproducible_runtime_state_is_rejected(value, mono, smooth):
    with pytest.raises(OverrideError, match="registry and active policies disagree"):
        vf.validate_existing(registry(value), observation(mono, smooth))
    with pytest.raises(OverrideError, match="registry and active policies disagree"):
        vf.compose(registry(value), {vf.MONOTONICITY_KEY: False}, observation(mono, smooth))


def test_absent_registry_cannot_restore_an_unrecorded_runtime_override():
    with pytest.raises(OverrideError, match="runtime settings"):
        vf.compose({}, {vf.MONOTONICITY_KEY: False}, observation(True, False))


@pytest.mark.parametrize(
    "entry",
    [
        None,
        {},
        {"type": 4},
        {"type": True, "value": 0},
        {"type": 1, "value": "3"},
        {"type": 4, "value": True},
        {"type": 4, "value": -1},
        {"type": 4, "value": 2**32},
        {"type": 4, "value": 3, "extra": 0},
    ],
)
def test_unknown_existing_type_shape_or_value_is_preserved(entry):
    before = {vf.REGISTRY_NAME: entry}
    with pytest.raises(OverrideError):
        vf.compose(before, {vf.MONOTONICITY_KEY: False}, observation())
    assert before == {vf.REGISTRY_NAME: entry}


@pytest.mark.parametrize(
    "bad",
    [
        {vf.REGISTRY_NAME: {"type": 4, "value": 0}, "rmclkvfoverride": {"type": 4, "value": 0}},
        {1: {"type": 4, "value": 0}},
        [],
        None,
    ],
)
def test_invalid_registry_mapping_or_duplicate_names_fail(bad):
    with pytest.raises(OverrideError):
        vf.compose(bad, {vf.MONOTONICITY_KEY: False}, observation())


@pytest.mark.parametrize("changes", [{}, None, {vf.MONOTONICITY_KEY: 0}, {vf.SMOOTHENING_KEY: None}, {"raw": True}])
def test_only_named_boolean_changes_can_compose(changes):
    with pytest.raises(OverrideError):
        vf.compose({}, changes, observation())


@pytest.mark.parametrize("architecture", range(4, 11))
def test_alternate_supported_architectures_and_identifiers(architecture):
    source = observation(architecture=architecture, slot="00000000:fe:1f.7")
    source["identity"].update(device_id=0x1234, subsystem_id=0xFEDCBA98)
    assert vf.compose({}, {vf.MONOTONICITY_KEY: False}, source) == registry(3)


@pytest.mark.parametrize(
    "field,value",
    [
        ("architecture", 3),
        ("architecture", 11),
        ("architecture", True),
        ("slot", "0001:01:00.0"),
        ("slot", "0000:01:20.0"),
        ("device_id", None),
        ("device_id", True),
        ("device_id", 0x10000),
        ("subsystem_id", -1),
    ],
)
def test_bad_identity_does_not_authorize_edits(field, value):
    source = observation()
    source["identity"][field] = value
    with pytest.raises(OverrideError):
        vf.compose({}, {vf.MONOTONICITY_KEY: False}, source)


@pytest.mark.parametrize(
    "field,value",
    [
        ("control_bytes", 0x804),
        ("control_bytes", 2108.0),
        ("request_mask", True),
        ("request_mask", 1),
        ("control_command", "0x2080901A"),
        ("stable_reads", 1),
        ("stable_reads", 2.0),
        ("vf_monotonicity_enforced", 1),
        ("vf_smoothening_enforced", False),
        ("header_hex", "bad"),
    ],
)
def test_malformed_observation_fails_composition(field, value):
    source = observation()
    source[field] = value
    with pytest.raises(OverrideError):
        vf.compose({}, {vf.MONOTONICITY_KEY: False}, source)


@pytest.mark.parametrize(
    "selected",
    [
        {vf.MONOTONICITY_KEY: False},
        {vf.SMOOTHENING_KEY: False},
        {vf.MONOTONICITY_KEY: False, vf.SMOOTHENING_KEY: False},
    ],
)
def test_exact_active_and_restore_contracts_are_json_safe(selected):
    before = observation()
    contract = json.loads(json.dumps(vf.contracts(selected, before), allow_nan=False))
    active = copy.deepcopy(before)
    for key, value in selected.items():
        set_flag(active, key, value)
    assert set(vf.verify(active, contract)) == set(selected)
    assert set(vf.verify(before, contract, restore=True)) == set(selected)
    with pytest.raises(OverrideError, match="expected active state|untouched policy"):
        vf.verify(before, contract)


def test_captured_disabled_initial_state_is_the_undo_state():
    before = observation(False, True)
    contract = vf.contracts({vf.MONOTONICITY_KEY: True}, before)
    assert contract[vf.MONOTONICITY_KEY]["before"] is False
    assert vf.verify(before, contract, restore=True) == [vf.MONOTONICITY_KEY]


@pytest.mark.parametrize("offset", [0, 8, 9, 11, 12, 13, 0x10, 0x20, 0x37, 0x38])
def test_untouched_header_and_peer_changes_fail_verification(offset):
    saved = vf.contracts({vf.MONOTONICITY_KEY: False}, observation())
    source = observation(False)
    raw = bytearray.fromhex(source["header_hex"])
    raw[offset] ^= 1
    if offset == 8:
        raw[0x38] = raw[8]
    source["header_hex"] = raw.hex()
    if offset == 11:
        source["vf_smoothening_enforced"] = bool(raw[11])
    with pytest.raises(OverrideError):
        vf.verify(source, saved)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", True),
        ("schema", 2),
        ("before", 1),
        ("after", 0),
        ("registry_mask", True),
        ("registry_mask", 12),
        ("registry_name", "Other"),
        ("unchanged", {}),
    ],
)
def test_malformed_receipt_fields_fail(field, value):
    saved = vf.contracts({vf.MONOTONICITY_KEY: False}, observation())
    saved[vf.MONOTONICITY_KEY][field] = value
    with pytest.raises(OverrideError):
        vf.verify(observation(False), saved)


def test_receipt_target_types_and_peer_baselines_are_strict():
    saved = vf.contracts({vf.MONOTONICITY_KEY: False, vf.SMOOTHENING_KEY: False}, observation())
    saved[vf.MONOTONICITY_KEY]["unchanged"]["flags"]["vf_smoothening_enforced"] = True
    with pytest.raises(OverrideError, match="peer baseline"):
        vf.verify(observation(False, False), saved)
    saved = vf.contracts({vf.MONOTONICITY_KEY: False}, observation())
    saved[vf.MONOTONICITY_KEY]["unchanged"]["identity"]["architecture"] = 8.0
    with pytest.raises(OverrideError, match="target, header"):
        vf.verify(observation(False), saved)


@pytest.mark.parametrize("initial", [(False, False), (False, True), (True, False), (True, True)])
@pytest.mark.parametrize("desired", [(False, False), (False, True), (True, False), (True, True)])
def test_combined_contracts_reconcile_both_peer_directions(initial, desired):
    changes = dict(zip((vf.MONOTONICITY_KEY, vf.SMOOTHENING_KEY), desired, strict=True))
    before, after = observation(*initial), observation(*desired)
    saved = vf.contracts(changes, before)
    assert vf.validate_contracts(changes, saved) is None
    assert set(vf.verify(after, saved)) == set(changes)
    assert set(vf.verify(before, saved, restore=True)) == set(changes)


def test_empty_receipt_validation_is_pure_and_legacy_compatible(monkeypatch):
    monkeypatch.setattr(vf, "read", lambda *args: pytest.fail("No hardware read permitted"))
    assert vf.validate_contracts({}, {}) is None


@pytest.mark.parametrize(
    "changes,saved",
    [
        ({vf.MONOTONICITY_KEY: False}, {}),
        ({}, {vf.MONOTONICITY_KEY: {}}),
        (None, {}),
        ({}, None),
        ({}, {"unknown": {}}),
        ({vf.MONOTONICITY_KEY: 0}, {}),
    ],
)
def test_missing_unknown_or_nonboolean_receipt_binding_is_rejected(changes, saved):
    with pytest.raises(OverrideError):
        vf.validate_contracts(changes, saved)


@pytest.mark.parametrize("key", [vf.MONOTONICITY_KEY, vf.SMOOTHENING_KEY])
def test_receipt_selected_direction_cannot_change(key):
    saved = vf.contracts({key: False}, observation())
    with pytest.raises(OverrideError, match="selected changes"):
        vf.validate_contracts({key: True}, saved)


@pytest.mark.parametrize(
    "part,field,value",
    [
        ("identity", "architecture", 8.0),
        ("identity", "slot", "0001:01:00.0"),
        ("identity", "device_id", None),
        ("identity", "extra", 1),
        ("layout", "control_bytes", 2108.0),
        ("layout", "request_mask", 1),
        ("layout", "extra", 1),
        ("flags", "vf_smoothening_enforced", True),
        ("flags", "vf_smoothening_enforced", 2),
        ("flags", "extra", 1),
    ],
)
def test_receipt_preserved_state_requires_exact_supported_schema(part, field, value):
    changes = {vf.MONOTONICITY_KEY: False}
    saved = vf.contracts(changes, observation())
    saved[vf.MONOTONICITY_KEY]["unchanged"][part][field] = value
    with pytest.raises(OverrideError):
        vf.validate_contracts(changes, saved)


@pytest.mark.parametrize("offset,value", [(0, 0xA5), (1, 0), (4, 1), (8, 2), (10, 1), (11, 1), (0x38, 1), (0x39, 0)])
def test_receipt_header_requires_canonical_preserved_bytes(offset, value):
    changes = {vf.MONOTONICITY_KEY: False}
    saved = vf.contracts(changes, observation())
    state = saved[vf.MONOTONICITY_KEY]["unchanged"]
    raw = bytearray.fromhex(state["header_hex"])
    raw[offset] = value
    state["header_hex"] = raw.hex()
    with pytest.raises(OverrideError):
        vf.validate_contracts(changes, saved)


def test_receipt_contracts_must_share_one_captured_observation():
    changes = {vf.MONOTONICITY_KEY: False, vf.SMOOTHENING_KEY: False}
    saved = vf.contracts(changes, observation())
    saved[vf.SMOOTHENING_KEY]["unchanged"]["identity"]["device_id"] += 1
    with pytest.raises(OverrideError, match="captured state"):
        vf.validate_contracts(changes, saved)
    saved = vf.contracts(changes, observation())
    saved[vf.SMOOTHENING_KEY]["before"] = False
    with pytest.raises(OverrideError, match="captured state"):
        vf.validate_contracts(changes, saved)


def test_successful_read_is_two_fresh_gets_and_false_is_available():
    gpu = FakeGPU()
    gpu.raw = raw_header(False, True)
    data, error = vf.read(gpu)
    assert error is None and data == observation(False, True)
    assert len(gpu.calls) == 2


def test_transient_transport_failure_retries_boundedly_without_rebinding():
    gpu = FakeGPU(actions=[lambda g, p: -1])
    data, error = vf.read(gpu)
    assert error is None and data["stable_reads"] == 2
    assert len(gpu.calls) == 3 and gpu.transport_count == 2
    assert gpu._current_limit_layout_choice is None


def test_persistent_transient_failure_is_bounded():
    gpu = FakeGPU(actions=[lambda g, p: -1, lambda g, p: -1])
    data, error = vf.read(gpu)
    assert data is None and "NTSTATUS" in error and len(gpu.calls) == 2


@pytest.mark.parametrize("rm_status", [0x1F, 0x56])
def test_unsupported_response_is_not_false_or_retried(rm_status):
    gpu = FakeGPU(actions=[mutate_word(16, rm_status)])
    data, error = vf.read(gpu)
    assert data is None and "rejected" in error and len(gpu.calls) == 1


@pytest.mark.parametrize(
    "action",
    [
        mutate_word(0, 0),
        mutate_word(1, 0),
        mutate_word(2, 0),
        mutate_word(5, 2),
        mutate_word(14, 0),
        mutate_word(15, 0),
        mutate_byte(0, 0xA5),
        mutate_byte(1, 0),
        mutate_byte(4, 1),
        mutate_byte(10, 0xA5),
        mutate_byte(11, 2),
        mutate_byte(0x38, 1),
        mutate_byte(0x39, 0),
        mutate_byte(0x3C, 0),
        mutate_byte(0x83C, 0),
    ],
)
def test_malformed_envelope_header_and_overrun_are_not_retried(action):
    gpu = FakeGPU(actions=[action])
    data, error = vf.read(gpu)
    assert data is None and error and len(gpu.calls) == 1


def test_nvda_completion_failure_is_distinct_and_can_recover():
    gpu = FakeGPU(actions=[mutate_word(11, 0)])
    data, error = vf.read(gpu)
    assert error is None and data["stable_reads"] == 2 and len(gpu.calls) == 3


def test_header_change_between_reads_is_not_stable_or_retried():
    gpu = FakeGPU(actions=[None, mutate_byte(0x20, 7)])
    data, error = vf.read(gpu)
    assert data is None and "changed between fresh reads" in error and len(gpu.calls) == 2


def test_changed_adapter_after_get_is_never_rebound():
    def swap(gpu, packet):
        gpu.nvapi.selected["devid"] += 1
        gpu.nvml.selected["devid"] += 1
        return 0

    gpu = FakeGPU(actions=[swap])
    data, error = vf.read(gpu)
    assert data is None and "binding changed" in error and len(gpu.calls) == 1


def test_stale_pairing_fails_before_any_get():
    gpu = FakeGPU()
    gpu.nvapi.selected["slot"] = "0000:01:00.0"
    data, error = vf.read(gpu)
    assert data is None and "selected PCI" in error and not gpu.calls


def test_missing_captured_transport_has_bounded_recovery_before_get():
    gpu = FakeGPU()

    def capture_nothing():
        gpu.transport_count += 1

    gpu._current_limit_transport_layouts = capture_nothing
    data, error = vf.read(gpu)
    assert data is None and "No captured paired" in error
    assert gpu.transport_count == 2 and not gpu.calls


def test_malformed_captured_transport_is_not_retried_or_used():
    gpu = FakeGPU()
    gpu._current_limit_transport_layouts()
    gpu._current_limit_transport[0][14] = 0x2080901B
    data, error = vf.read(gpu)
    assert data is None and "validated modern RM transport" in error
    assert gpu.transport_count == 2 and not gpu.calls


def test_missing_handle_never_issues_get():
    gpu = FakeGPU()
    gpu.nvapi.gpu = None
    data, error = vf.read(gpu)
    assert data is None and "live PCI-paired" in error and not gpu.calls


def test_changed_transport_context_is_rejected():
    gpu = FakeGPU(actions=[mutate_word(6, 123)])
    data, error = vf.read(gpu)
    assert data is None and "bound envelope" in error and len(gpu.calls) == 1


def test_duplicate_debug_flag_may_be_true_but_must_agree():
    gpu = FakeGPU()
    gpu.raw[8] = gpu.raw[0x38] = 1
    data, error = vf.read(gpu)
    assert error is None and bytes.fromhex(data["header_hex"])[8] == 1
    vf.validate_contracts({vf.MONOTONICITY_KEY: False}, vf.contracts({vf.MONOTONICITY_KEY: False}, data))
    gpu.actions = [mutate_byte(0x38, 0)]
    data, error = vf.read(gpu)
    assert data is None and "duplicate flag" in error and len(gpu.calls) == 3


@pytest.mark.parametrize("architecture", [4, 5, 6, 7, 8, 9, 10])
def test_read_accepts_alternate_supported_generations(architecture):
    gpu = FakeGPU(architecture=architecture)
    data, error = vf.read(gpu)
    assert error is None and data["identity"]["architecture"] == architecture
