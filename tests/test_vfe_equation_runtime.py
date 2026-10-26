"""Synthetic native VFE packets; no NVIDIA DLL or GPU access."""

import copy
import ctypes
import struct
import threading
from types import SimpleNamespace

import pytest

from druta import vfe_equation_runtime as vfe
from druta.perf_diagnostics import PerfDiagnosticsReader


def clamp_observation(
    index=37, lower=900000, upper=1120000, next_index=401, variable=255, variable_count=2, peers=(401, 7999), **ident
):
    obs = observation(index=index, before=upper, next_index=next_index, variable=variable, peers=peers, **ident)
    at, ct = vfe.HEADER + index * vfe.INFO_STRIDE, vfe.HEADER + index * vfe.CONTROL_STRIDE
    for name, offsets in (("info", (at, at + 0x6C)), ("control", (ct, ct + 0x7C))):
        for offset in offsets:
            mutate(obs, name, offset, 6)
    mutate(obs, "control", ct + 4, lower, "f")
    coeff_count = {1: 3, 2: 6, 3: 10}[variable_count]
    mutate(obs, "control", ct + 0x78, variable_count)
    mutate(obs, "control", ct + 0x79, coeff_count)
    raw = bytearray.fromhex(obs["control_hex"])
    for j in range(variable_count):
        pos = ct + 0x10 + j * 8
        raw[pos : pos + 8] = bytes((2, 0xA5, 0xB6, 0xC7, 4 + j, 0xD8, 0xE9, 0xFA))
    for j in range(coeff_count):
        pos = ct + 0x28 + j * 8
        raw[pos : pos + 4] = bytes((1, 0x91, 0x82, 0x73))
        struct.pack_into("<f", raw, pos + 4, 1000000 if j == 0 else -j * 0.5)
    obs["control_hex"] = raw.hex()
    return obs


def clamp_selections(obs=None, index=37, after_uv=1035000, key="voltage.0.REL.maximum"):
    obs = clamp_observation(index=index) if obs is None else obs
    row = vfe.upper_clamp_row(obs, index)
    return {
        key: {
            **{k: value for k, value in row.items() if k != "upper_uv"},
            "before_uv": row["upper_uv"],
            "after_uv": after_uv,
        }
    }


def identity(**changes):
    return {"slot": "0000:75:1c.6", "architecture": 9, "device_id": 0xABCD, "subsystem_id": 0x12345678, **changes}


def payloads(index=37, before=20000, next_index=401, variable=22, output_type=2, peers=(401, 7999)):
    info, control = bytearray(vfe.INFO_SIZE), bytearray(vfe.CONTROL_SIZE)
    mask = sum(1 << i for i in {index, *peers})
    for raw in (info, control):
        raw[0] = 0xB6  # Group identity is not a local-board constant.
        raw[4 : 4 + vfe.MASK_BYTES] = mask.to_bytes(vfe.MASK_BYTES, "little")
    info[3], info[0x407], info[-1] = 0xCE, 0x91, 0xE3
    control[3], control[0x407], control[-1] = 0xDF, 0x92, 0xF4
    for i in peers:
        at, ct = vfe.HEADER + i * vfe.INFO_STRIDE, vfe.HEADER + i * vfe.CONTROL_STRIDE
        info[at], info[at + 0x6C], info[at + 13] = 97, 78, 0xCB
        control[ct], control[ct + 0x7C], control[ct + 37] = 98, 79, 0xDC
    at, ct = vfe.HEADER + index * vfe.INFO_STRIDE, vfe.HEADER + index * vfe.CONTROL_STRIDE
    info[at], info[at + 0x6C] = 3, 3
    struct.pack_into("<H", info, at + 2, next_index)
    info[at + 0x6D], info[at + 0x6E] = variable, output_type
    control[ct], control[ct + 0x7C] = 3, 3
    for offset in (4, 8, 0x10):
        struct.pack_into("<f", control, ct + offset, before)
    return bytes(info), bytes(control)


def observation(index=37, before=20000, next_index=401, variable=22, output_type=2, peers=(401, 7999), **ident):
    info, control = payloads(index, before, next_index, variable, output_type, peers)
    return {
        "schema": 1,
        "stable_reads": 2,
        "identity": identity(**ident),
        "info_hex": info.hex(),
        "control_hex": control.hex(),
    }


def selections(
    index=37, before_uv=20000, after_uv=25000, next_index=401, variable=22, output_type=2, key="voltage.1.ALT.addend"
):
    return {
        key: {
            "index": index,
            "before_uv": before_uv,
            "after_uv": after_uv,
            "next_index": next_index,
            "variable": variable,
            "output_type": output_type,
        }
    }


def mutate(obs, name, offset, value, fmt="B"):
    raw = bytearray.fromhex(obs[name + "_hex"])
    struct.pack_into("<" + fmt, raw, offset, value)
    obs[name + "_hex"] = raw.hex()


def test_constant_contract_projects_three_words_and_preserves_every_peer():
    obs, choices = observation(), selections()
    before = copy.deepcopy(obs)
    saved = vfe.contracts(obs, choices)
    assert obs == before
    vfe.validate_contracts(choices, saved)
    assert vfe.verify(saved["after"], saved) == list(choices)
    assert vfe.verify(saved["before"], saved, restore=True) == list(choices)
    assert saved["after"]["info_hex"] == obs["info_hex"]
    expected = bytearray.fromhex(obs["control_hex"])
    for offset in (4, 8, 16):
        struct.pack_into("<f", expected, vfe.HEADER + 37 * vfe.CONTROL_STRIDE + offset, 25000)
    assert bytes.fromhex(saved["after"]["control_hex"]) == expected
    assert vfe.constant_row(obs, 37) == {
        "index": 37,
        "value_uv": 20000,
        "next_index": 401,
        "variable": 22,
        "output_type": 2,
    }


@pytest.mark.parametrize("index,nxt,var,arch", [(0, 0xFFFF, 255, 4), (8111, 401, 0, 10), (8191, 7999, 214, 7)])
def test_alternate_identity_and_rom_ordinals(index, nxt, var, arch):
    obs = observation(
        index,
        next_index=nxt,
        variable=var,
        slot="0000:03:00.1",
        architecture=arch,
        device_id=42,
        subsystem_id=0xDEADBEEF,
    )
    choices = selections(index, next_index=nxt, variable=var)
    saved = vfe.contracts(obs, choices)
    vfe.validate_contracts(choices, saved)
    assert vfe.verify(saved["after"], saved) == list(choices)


def test_multiple_constants_and_exact_noop():
    obs = observation(next_index=0xFFFF)
    info, ctl = map(bytearray, (bytes.fromhex(obs["info_hex"]), bytes.fromhex(obs["control_hex"])))
    other_info, other_ctl = payloads(401, 900000, 0xFFFF, 3)
    ai, ac = vfe.HEADER + 401 * vfe.INFO_STRIDE, vfe.HEADER + 401 * vfe.CONTROL_STRIDE
    info[ai : ai + vfe.INFO_STRIDE] = other_info[ai : ai + vfe.INFO_STRIDE]
    ctl[ac : ac + vfe.CONTROL_STRIDE] = other_ctl[ac : ac + vfe.CONTROL_STRIDE]
    obs["info_hex"], obs["control_hex"] = info.hex(), ctl.hex()
    choices = {
        **selections(next_index=0xFFFF, after_uv=20000),
        **selections(401, 900000, 905000, 0xFFFF, 3, key="voltage.0.OV.constant"),
    }
    saved = vfe.contracts(obs, choices)
    assert vfe.verify(saved["after"], saved) == list(choices)
    assert vfe.constant_row(saved["after"], 401)["value_uv"] == 905000
    assert vfe.constant_row(saved["after"], 37)["value_uv"] == 20000


def test_unknown_mismatched_unselected_rows_do_not_erase_selected_capability():
    obs = observation()
    assert vfe.constant_row(obs, 37)["value_uv"] == 20000
    assert vfe.contracts(obs, selections())["after"]["info_hex"] == obs["info_hex"]
    with pytest.raises(vfe.VfeEquationError, match="type3"):
        vfe.constant_row(obs, 401)


def test_empty_selection_needs_no_observation_and_legacy_verification():
    assert vfe.contracts(None, {}) == {}
    assert vfe.validate_contracts({}, {}) is None
    assert vfe.verify(None, {}) == []
    obs = observation()
    assert vfe.project_constants(obs, {}) == obs
    canonical = vfe.validate_observation(obs)
    canonical["identity"]["device_id"] = 1
    assert canonical != obs


@pytest.mark.parametrize("index", [-1, 8192, True, 1.0, "37", None])
def test_invalid_selected_index(index):
    with pytest.raises(vfe.VfeEquationError):
        vfe.constant_row(observation(), index)


def test_absent_selected_or_linked_equation():
    with pytest.raises(vfe.VfeEquationError, match="absent"):
        vfe.constant_row(observation(), 38)
    obs = observation(peers=(7999,))
    with pytest.raises(vfe.VfeEquationError, match="absent"):
        vfe.contracts(obs, selections())


@pytest.mark.parametrize(
    "name,offset,value", [("info", 0, 6), ("info", 0x6C, 6), ("control", 0, 6), ("control", 0x7C, 6), ("info", 0x6E, 4)]
)
def test_selected_type_and_output_checks(name, offset, value):
    obs = observation()
    stride = vfe.INFO_STRIDE if name == "info" else vfe.CONTROL_STRIDE
    mutate(obs, name, vfe.HEADER + 37 * stride + offset, value)
    with pytest.raises(vfe.VfeEquationError):
        vfe.contracts(obs, selections())


@pytest.mark.parametrize(
    "offset,value",
    [
        (4, 19999),
        (8, 20001),
        (16, 19999),
        (20, 1),
        (24, -1),
        (4, float("nan")),
        (8, float("inf")),
        (16, -float("inf")),
        (20, float("nan")),
    ],
)
def test_nonconstant_or_nonfinite_selected_values(offset, value):
    obs = observation()
    mutate(obs, "control", vfe.HEADER + 37 * vfe.CONTROL_STRIDE + offset, value, "f")
    with pytest.raises(vfe.VfeEquationError):
        vfe.contracts(obs, selections())


def test_fractional_microvolt_constant_is_not_silently_rounded():
    with pytest.raises(vfe.VfeEquationError, match="whole"):
        vfe.constant_row(observation(before=20000.5), 37)


@pytest.mark.parametrize(
    "field,value",
    [
        ("before_uv", True),
        ("after_uv", 25000.0),
        ("after_uv", float("nan")),
        ("after_uv", 16777217),
        ("after_uv", 2**31),
        ("after_uv", -(2**31) - 1),
        ("variable", True),
        ("variable", 256),
        ("next_index", 8192),
        ("next_index", -1),
        ("output_type", True),
        ("output_type", 4),
        ("index", 8192),
    ],
)
def test_malformed_selection_values(field, value):
    choices = selections()
    choices[next(iter(choices))][field] = value
    with pytest.raises(vfe.VfeEquationError):
        vfe.contracts(observation(), choices)


@pytest.mark.parametrize("field,value", [("before_uv", 19999), ("variable", 21), ("next_index", 7999)])
def test_current_rom_metadata_must_match_loaded_row(field, value):
    choices = selections()
    choices[next(iter(choices))][field] = value
    with pytest.raises(vfe.VfeEquationError, match="ROM binding"):
        vfe.contracts(observation(), choices)


@pytest.mark.parametrize("wanted", [0, -5000, -(2**31), 16777218])
def test_pure_projection_accepts_exact_signed_float32_values_leaves_policy_to_caller(wanted):
    saved = vfe.contracts(observation(), selections(after_uv=wanted))
    assert vfe.constant_row(saved["after"], 37)["value_uv"] == wanted


def test_aliasing_selected_equations_rejected_even_when_same_value():
    choices = {**selections(key="one"), **selections(key="two")}
    with pytest.raises(vfe.VfeEquationError, match="alias"):
        vfe.contracts(observation(), choices)


@pytest.mark.parametrize("choices", [None, [], True, {"": {}}, {1: {}}, {"a": None}, {"a": {}}])
def test_bad_selection_structures(choices):
    with pytest.raises(vfe.VfeEquationError):
        vfe.contracts(None, choices)


@pytest.mark.parametrize(
    "field,value", [("schema", True), ("schema", 2), ("stable_reads", True), ("stable_reads", 1), ("identity", None)]
)
def test_bad_observation_structure(field, value):
    obs = observation()
    obs[field] = value
    with pytest.raises(vfe.VfeEquationError):
        vfe.validate_observation(obs)


@pytest.mark.parametrize(
    "field,value",
    [
        ("slot", "0001:00:00.0"),
        ("slot", "0000:00:20.0"),
        ("slot", "0000:00:00.8"),
        ("slot", None),
        ("architecture", 3),
        ("architecture", True),
        ("device_id", 65536),
        ("device_id", True),
        ("subsystem_id", -1),
    ],
)
def test_bad_identity(field, value):
    obs = observation()
    obs["identity"][field] = value
    with pytest.raises(vfe.VfeEquationError):
        vfe.validate_observation(obs)


@pytest.mark.parametrize("name", ["info", "control"])
@pytest.mark.parametrize("kind", ["short", "long", "upper", "nothex", "notstr"])
def test_malformed_packet_geometry_and_encoding(name, kind):
    obs = observation()
    value = obs[name + "_hex"]
    obs[name + "_hex"] = {
        "short": value[:-2],
        "long": value + "00",
        "upper": value.upper(),
        "nothex": "zz" + value[2:],
        "notstr": None,
    }[kind]
    with pytest.raises(vfe.VfeEquationError):
        vfe.validate_observation(obs)


@pytest.mark.parametrize("offset", [0, 4, 4 + 1023])
def test_group_and_mask_mismatch(offset):
    obs = observation()
    mutate(obs, "control", offset, bytes.fromhex(obs["control_hex"])[offset] ^ 1)
    with pytest.raises(vfe.VfeEquationError, match="inventories"):
        vfe.validate_observation(obs)


@pytest.mark.parametrize(
    "name,offset",
    [
        ("info", 3),
        ("control", 3),
        ("info", vfe.INFO_SIZE - 1),
        ("control", vfe.CONTROL_SIZE - 1),
        ("control", vfe.HEADER + 37 * vfe.CONTROL_STRIDE + 37),
        ("info", vfe.HEADER + 7999 * vfe.INFO_STRIDE + 13),
    ],
)
@pytest.mark.parametrize("restore", [False, True])
def test_every_unrelated_byte_is_preserved_during_apply_and_undo(name, offset, restore):
    saved = vfe.contracts(observation(), selections())
    live = copy.deepcopy(saved["before" if restore else "after"])
    mutate(live, name, offset, bytes.fromhex(live[name + "_hex"])[offset] ^ 1)
    with pytest.raises(vfe.VfeEquationError, match="preserved peers"):
        vfe.verify(live, saved, restore=restore)


@pytest.mark.parametrize("restore", [False, True])
def test_changed_adapter_fails_apply_and_restore(restore):
    saved = vfe.contracts(observation(), selections())
    live = copy.deepcopy(saved["before" if restore else "after"])
    live["identity"]["device_id"] = 42
    with pytest.raises(vfe.VfeEquationError, match="preserved peers"):
        vfe.verify(live, saved, restore=restore)


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "schema",
        "missing_selected",
        "different_key",
        "direction",
        "peer_after",
        "identity_after",
        "before_value",
        "extra",
        "selected_bool",
    ],
)
def test_malformed_contracts_fail_before_verification(fault):
    choices, obs = selections(), observation()
    saved = vfe.contracts(obs, choices)
    key = next(iter(choices))
    if fault == "missing":
        saved = {}
    elif fault == "schema":
        saved["schema"] = True
    elif fault == "missing_selected":
        saved["selected"] = {}
    elif fault == "different_key":
        saved["selected"]["wrong"] = saved["selected"].pop(key)
    elif fault == "direction":
        saved["selected"][key]["after_uv"] = 30000
    elif fault == "peer_after":
        mutate(saved["after"], "control", 3, 1)
    elif fault == "identity_after":
        saved["after"]["identity"]["subsystem_id"] = 1
    elif fault == "before_value":
        saved["selected"][key]["before_uv"] = 19000
    elif fault == "extra":
        saved["extra"] = 1
    else:
        saved["selected"][key]["index"] = True
    with pytest.raises(vfe.VfeEquationError):
        vfe.validate_contracts(choices, saved)
    # verify has no external key authority. The transaction owner must bind
    # its requested field keys through validate_contracts before any action.
    if saved and fault != "different_key":
        with pytest.raises(vfe.VfeEquationError):
            vfe.verify(obs, saved, restore=True)


class Gpu:
    def __init__(self, obs=None, fault=None, **ident):
        self.observation = observation(**ident) if obs is None else obs
        self.identity = self.observation["identity"]
        self.calls, self.exports, self.discoveries = [], [], 0
        self.fault = fault
        self._lock = threading.RLock()
        self._current_limit_transport = self._current_limit_layout_choice = None
        self.pairing_error = ""
        selected = {
            "slot": self.identity["slot"],
            "handle": 0x123456,
            "devid": self.identity["device_id"],
            "subsys": self.identity["subsystem_id"],
        }
        self.nvapi = SimpleNamespace(ok=True, gpu=0x123456, selected=selected, _i=self.export)
        self.nvml = SimpleNamespace(selected=dict(selected))
        self.missing_exports = set()

    def export(self, export, *_):
        self.exports.append(export)
        return None if export in self.missing_exports else object()

    def slot(self):
        return self.identity["slot"]

    def arch(self):
        return self.identity["architecture"]

    def _current_limit_transport_layouts(self):
        self.discoveries += 1
        if self._current_limit_transport is None:
            header = [0] * 17
            for i, word in vfe._ENVELOPE.items():
                header[i] = word
            header[2], header[14], header[15] = 68 + 240, 0x2080A612, 240
            header[5], header[11], header[7] = 1, 1, 0x12345
            fields = {"hAdapter": 1, "hDevice": 2, "Type": 2, "Flags": 1, "hContext": 0}
            self._current_limit_transport = (header, fields)

    def _legacy_clk_escape(self, packet, fields):
        assert set(fields) == vfe._ESCAPE_FIELDS
        assert packet[5] == packet[11] == packet[16] == 0
        name = next(name for name, layout in vfe.LAYOUTS.items() if layout[0] == packet[14])
        command, size, _, _ = vfe.LAYOUTS[name]
        assert packet[2] == 68 + size and packet[14] == command and packet[15] == size
        raw = bytes(packet)[68:]
        if name == "info":
            assert not any(raw)
        else:
            assert raw[4 : 4 + vfe.MASK_BYTES] == bytes.fromhex(self.observation["info_hex"])[4 : 4 + vfe.MASK_BYTES]
            assert not any(raw[:4]) and not any(raw[4 + vfe.MASK_BYTES :])
        self.calls.append(name)
        result = bytes.fromhex(self.observation[name + "_hex"])
        ctypes.memmove(ctypes.addressof(packet) + 68, result, len(result))
        packet[5] = packet[11] = 1
        return self.fault(self, packet) if self.fault else 0


def test_read_guarded_exact_exports_geometry_and_two_complete_pairs():
    gpu = Gpu()
    result, error = vfe.read(gpu)
    assert error is None and result == gpu.observation
    assert gpu.calls == ["info", "control", "info", "control"]
    assert gpu.exports == [0x8D49471C, 0x4C75C9FE] * 2
    assert vfe.constant_row(result, 37)["value_uv"] == 20000


def test_empty_inventory_is_diagnostic_only():
    obs = observation()
    for name in ("info", "control"):
        raw = bytearray.fromhex(obs[name + "_hex"])
        raw[4 : 4 + vfe.MASK_BYTES] = bytes(vfe.MASK_BYTES)
        obs[name + "_hex"] = raw.hex()
    result, error = vfe.read(Gpu(obs))
    assert error is None and result == obs
    with pytest.raises(vfe.VfeEquationError, match="absent"):
        vfe.contracts(result, selections())


@pytest.mark.parametrize("export", [0x8D49471C, 0x4C75C9FE])
def test_missing_getter_export_is_independent_unsupported(export):
    gpu = Gpu()
    gpu.missing_exports.add(export)
    result, error = vfe.read(gpu)
    assert result is None and "export" in error
    assert len(gpu.calls) == (0 if export == 0x8D49471C else 1)


@pytest.mark.parametrize("failure", ["nt", "rm", "incomplete", "exception"])
def test_transient_failures_retry_once_without_changing_saved_state(failure):
    def fault(gpu, packet):
        if len(gpu.calls) == 1:
            if failure == "nt":
                return -1
            if failure == "rm":
                packet[16] = 0x51
            elif failure == "incomplete":
                packet[11] = 0
            else:
                raise OSError("Transient native capture failure")
        return 0

    gpu = Gpu(fault=fault)
    baseline = copy.deepcopy(gpu.observation)
    result, error = vfe.read(gpu)
    assert error is None and result == baseline and gpu.observation == baseline
    assert len(gpu.calls) == 5


def test_permanent_transient_failure_is_bounded():
    gpu = Gpu(fault=lambda gpu, packet: -1)
    result, error = vfe.read(gpu)
    assert result is None and "transport" in error and len(gpu.calls) == 2


@pytest.mark.parametrize("status", [0x1F, 0x56])
def test_rm_unsupported_does_not_retry(status):
    def fault(gpu, packet):
        packet[16] = status
        return 0

    gpu = Gpu(fault=fault)
    assert vfe.read(gpu)[0] is None
    assert len(gpu.calls) == 1


@pytest.mark.parametrize("word", [0, 1, 2, 3, 4, 6, 7, 8, 9, 10, 12, 13, 14, 15])
def test_response_may_not_rewrite_unknown_envelope_words(word):
    def fault(gpu, packet):
        packet[word] ^= 1
        return 0

    gpu = Gpu(fault=fault)
    result, error = vfe.read(gpu)
    assert result is None and "envelope" in error and len(gpu.calls) == 1


@pytest.mark.parametrize("completion", [0, 1])
def test_transport_word5_documented_completion_values(completion):
    def fault(gpu, packet):
        packet[5] = completion
        return 0

    assert vfe.read(Gpu(fault=fault))[1] is None


def test_transport_word5_unknown_value_is_not_accepted():
    def fault(gpu, packet):
        packet[5] = 2
        return 0

    gpu = Gpu(fault=fault)
    assert "envelope" in vfe.read(gpu)[1] and len(gpu.calls) == 1


@pytest.mark.parametrize("side", ["before", "after"])
@pytest.mark.parametrize("raise_after", [False, True])
def test_get_guard_overflow_detected_even_if_native_call_raises(side, raise_after):
    def fault(gpu, packet):
        address = ctypes.addressof(packet) + (-1 if side == "before" else ctypes.sizeof(packet))
        ctypes.c_ubyte.from_address(address).value = 0
        if raise_after:
            raise RuntimeError("Native read failed after guard corruption")
        return 0

    gpu = Gpu(fault=fault)
    result, error = vfe.read(gpu)
    assert result is None and "guarded" in error and len(gpu.calls) == 1


@pytest.mark.parametrize("fault_name", ["nvml", "handle", "slot", "architecture", "pairing"])
def test_adapter_pairing_and_mid_get_target_change(fault_name):
    def fault(gpu, packet):
        if fault_name == "nvml":
            gpu.nvml.selected["devid"] = 2
        elif fault_name == "handle":
            gpu.nvapi.gpu = 0xDEAD
        elif fault_name == "slot":
            gpu.nvapi.selected["slot"] = "0000:04:00.0"
        elif fault_name == "architecture":
            gpu.identity["architecture"] = 8
        else:
            gpu.pairing_error = "Pairing changed"
        return 0

    gpu = Gpu(fault=fault)
    assert vfe.read(gpu)[0] is None and len(gpu.calls) == 1


def test_full_packets_must_be_stable_then_retry_can_recover():
    def fault(gpu, packet):
        if len(gpu.calls) == 3:
            data = (ctypes.c_ubyte * ctypes.sizeof(packet)).from_buffer(packet)
            data[-1] ^= 1
        return 0

    gpu = Gpu(fault=fault)
    result, error = vfe.read(gpu)
    assert error is None and result == gpu.observation and len(gpu.calls) == 8


def test_get_rejects_unvalidated_input_geometry_before_transport():
    gpu = Gpu()
    reader = PerfDiagnosticsReader(gpu)
    for name, mask in (("set", None), ("info", bytes(vfe.MASK_BYTES)), ("control", None), ("control", bytes(3))):
        with pytest.raises(vfe.VfeEquationError):
            vfe._get(gpu, reader, name, mask)
    assert gpu.calls == []


@pytest.mark.parametrize("fault", ["magic", "bool_field", "negative_field", "extra_field"])
def test_initial_envelope_and_escape_fields_are_validated(fault):
    gpu = Gpu()
    gpu._current_limit_transport_layouts()
    header, fields = gpu._current_limit_transport
    if fault == "magic":
        header[0] = 0
    elif fault == "bool_field":
        fields["Flags"] = True
    elif fault == "negative_field":
        fields["hAdapter"] = -1
    else:
        fields["extra"] = 1
    assert vfe.read(gpu)[0] is None and gpu.calls == []


def test_unavailable_object_returns_error_instead_of_raising_during_retry_cleanup():
    result, error = vfe.read(None)
    assert result is None and error


def test_cancellation_is_not_retried_or_swallowed():
    def fault(gpu, packet):
        raise KeyboardInterrupt()

    gpu = Gpu(fault=fault)
    with pytest.raises(KeyboardInterrupt):
        vfe.read(gpu)
    assert len(gpu.calls) == 1


def test_upper_clamp_contract_projects_one_word_and_preserves_every_other_byte():
    obs = clamp_observation()
    original = copy.deepcopy(obs)
    row = vfe.upper_clamp_row(obs, 37)
    assert {k: row[k] for k in ("index", "lower_uv", "upper_uv", "next_index", "variable", "output_type")} == {
        "index": 37,
        "lower_uv": 900000,
        "upper_uv": 1120000,
        "next_index": 401,
        "variable": 255,
        "output_type": 2,
    }
    assert row["variables"] == [{"type": 2, "value": 4}, {"type": 2, "value": 5}]
    assert row["coefficients"] == [
        {"type": 1, "value": struct.unpack("<I", struct.pack("<f", v))[0]} for v in (1000000, -0.5, -1, -1.5, -2, -2.5)
    ]
    choices = clamp_selections(obs)
    saved = vfe.clamp_contracts(obs, choices)
    assert saved["kind"] == "upper_clamps" and saved["schema"] == 1
    assert obs == original
    vfe.validate_clamp_contracts(choices, saved)
    assert vfe.verify_clamps(saved["after"], saved) == list(choices)
    assert vfe.verify_clamps(saved["before"], saved, restore=True) == list(choices)
    expected = bytearray.fromhex(obs["control_hex"])
    struct.pack_into("<f", expected, vfe.HEADER + 37 * vfe.CONTROL_STRIDE + 8, 1035000)
    assert saved["after"]["control_hex"] == expected.hex()
    assert saved["after"]["info_hex"] == obs["info_hex"]
    assert vfe.upper_clamp_row(saved["after"], 37)["upper_uv"] == 1035000


@pytest.mark.parametrize(
    "index,nxt,var,count,arch", [(0, 0xFFFF, 1, 1, 4), (8111, 401, 0, 3, 10), (8191, 7999, 255, 2, 7)]
)
def test_upper_clamp_alternate_identity_ordinals_and_polynomial_geometry(index, nxt, var, count, arch):
    obs = clamp_observation(
        index,
        next_index=nxt,
        variable=var,
        variable_count=count,
        slot="0000:01:00.1",
        architecture=arch,
        device_id=77,
        subsystem_id=0xAABBCCDD,
    )
    choices = clamp_selections(obs, index)
    saved = vfe.clamp_contracts(obs, choices)
    assert vfe.verify_clamps(saved["after"], saved) == list(choices)
    assert len(vfe.upper_clamp_row(obs, index)["coefficients"]) == {1: 3, 2: 6, 3: 10}[count]


def test_upper_clamp_partial_inventory_and_opaque_peers_remain_supported():
    obs = clamp_observation(next_index=0xFFFF, peers=())
    saved = vfe.clamp_contracts(obs, clamp_selections(obs))
    assert vfe.verify_clamps(saved["after"], saved) == ["voltage.0.REL.maximum"]
    obs = clamp_observation()  # Intentionally unknown and mismatched peer row types.
    assert vfe.clamp_contracts(obs, clamp_selections(obs))["after"]["info_hex"] == obs["info_hex"]


def test_upper_clamp_empty_selection_noop_and_constant_receipts_stay_separate():
    assert vfe.clamp_contracts(None, {}) == {}
    assert vfe.validate_clamp_contracts({}, {}) is None
    assert vfe.verify_clamps(None, {}) == []
    obs = clamp_observation()
    assert vfe.project_upper_clamps(obs, {}) == obs
    saved = vfe.clamp_contracts(obs, clamp_selections(obs, after_uv=1120000))
    assert saved["before"] == saved["after"]
    assert vfe.verify_clamps(obs, saved) == ["voltage.0.REL.maximum"]
    old = vfe.contracts(observation(), selections())
    assert set(old) == {"schema", "selected", "before", "after"}
    assert vfe.verify(old["before"], old, restore=True) == ["voltage.1.ALT.addend"]
    with pytest.raises(vfe.VfeEquationError):
        vfe.verify_clamps(obs, old)
    with pytest.raises(vfe.VfeEquationError):
        vfe.verify(obs, saved)


@pytest.mark.parametrize("where", [0x10, 0x28])
def test_upper_clamp_equation_descriptors_use_u16_and_preserve_opaque_high_bytes(where):
    obs = clamp_observation()
    ct = vfe.HEADER + 37 * vfe.CONTROL_STRIDE
    mutate(obs, "control", ct + where, 3)
    mutate(obs, "control", ct + where + 4, 7999, "H")
    mutate(obs, "control", ct + where + 6, 0xABCD, "H")
    choices = clamp_selections(obs)
    key = "variables" if where == 0x10 else "coefficients"
    assert choices["voltage.0.REL.maximum"][key][0] == {"type": 3, "value": 7999}
    saved = vfe.clamp_contracts(obs, choices)
    raw = bytes.fromhex(saved["after"]["control_hex"])
    assert raw[ct + where : ct + where + 8] == bytes.fromhex(obs["control_hex"])[ct + where : ct + where + 8]


@pytest.mark.parametrize("where,value", [(0x10, 37), (0x28, 38), (0x28, 8192), (0x10, 65535)])
def test_upper_clamp_absent_self_and_out_of_capacity_equation_terms_fail(where, value):
    obs = clamp_observation()
    ct = vfe.HEADER + 37 * vfe.CONTROL_STRIDE
    mutate(obs, "control", ct + where, 3)
    mutate(obs, "control", ct + where + 4, value, "H")
    with pytest.raises(vfe.VfeEquationError):
        vfe.upper_clamp_row(obs, 37)


@pytest.mark.parametrize("next_index", [37, 38, 8192, 65534])
def test_upper_clamp_invalid_next_reference(next_index):
    with pytest.raises(vfe.VfeEquationError):
        vfe.upper_clamp_row(clamp_observation(next_index=next_index), 37)


@pytest.mark.parametrize(
    "name,off,value", [("info", 0, 3), ("info", 0x6C, 97), ("control", 0, 3), ("control", 0x7C, 77), ("info", 0x6E, 1)]
)
def test_upper_clamp_requires_selected_runtime6_and_voltage_output(name, off, value):
    obs = clamp_observation()
    stride = vfe.INFO_STRIDE if name == "info" else vfe.CONTROL_STRIDE
    mutate(obs, name, vfe.HEADER + 37 * stride + off, value)
    with pytest.raises(vfe.VfeEquationError):
        vfe.upper_clamp_row(obs, 37)


@pytest.mark.parametrize(
    "off,value,fmt",
    [
        (4, float("nan"), "f"),
        (8, float("inf"), "f"),
        (4, -1, "f"),
        (8, 899999, "f"),
        (8, 1120000.5, "f"),
        (4, 900000.5, "f"),
        (0x78, 0, "B"),
        (0x78, 4, "B"),
        (0x79, 5, "B"),
        (0x10, 0, "B"),
        (0x28, 4, "B"),
        (0x14, 255, "B"),
        (0x2C, 0x7F800000, "I"),
        (0x34, 0x7FC00000, "I"),
    ],
)
def test_upper_clamp_malformed_bounds_counts_and_terms(off, value, fmt):
    obs = clamp_observation()
    mutate(obs, "control", vfe.HEADER + 37 * vfe.CONTROL_STRIDE + off, value, fmt)
    with pytest.raises(vfe.VfeEquationError):
        vfe.upper_clamp_row(obs, 37)


@pytest.mark.parametrize(
    "field,value",
    [
        ("index", True),
        ("index", 8192),
        ("lower_uv", -1),
        ("before_uv", True),
        ("after_uv", 1035000.0),
        ("after_uv", 0),
        ("after_uv", 899999),
        ("after_uv", 16777217),
        ("after_uv", 2**31),
        ("variable", True),
        ("variable", 256),
        ("output_type", True),
        ("output_type", 1),
        ("next_index", 8192),
        ("variables", []),
        ("variables", None),
        ("coefficients", []),
        ("coefficients", {}),
    ],
)
def test_upper_clamp_bad_selection_values(field, value):
    obs = clamp_observation()
    choices = clamp_selections(obs)
    choices["voltage.0.REL.maximum"][field] = value
    with pytest.raises(vfe.VfeEquationError):
        vfe.clamp_contracts(obs, choices)


@pytest.mark.parametrize(
    "term",
    [
        None,
        {},
        {"type": True, "value": 0},
        {"type": 4, "value": 0},
        {"type": 1, "value": 0x7F800000},
        {"type": 2, "value": 255},
        {"type": 3, "value": 8192},
        {"type": 1, "value": False},
        {"type": 1, "value": 0, "extra": 1},
    ],
)
def test_upper_clamp_malformed_selection_descriptors(term):
    choices = clamp_selections()
    choices["voltage.0.REL.maximum"]["variables"][0] = term
    with pytest.raises(vfe.VfeEquationError):
        vfe.clamp_contracts(clamp_observation(), choices)


@pytest.mark.parametrize(
    "field,value", [("lower_uv", 899000), ("before_uv", 1119000), ("variable", 254), ("next_index", 7999)]
)
def test_upper_clamp_rom_binding_must_match_all_selected_metadata(field, value):
    obs = clamp_observation()
    choices = clamp_selections(obs)
    choices["voltage.0.REL.maximum"][field] = value
    with pytest.raises(vfe.VfeEquationError, match="ROM binding"):
        vfe.clamp_contracts(obs, choices)


@pytest.mark.parametrize("part", ["variables", "coefficients"])
def test_upper_clamp_rom_descriptor_mismatch(part):
    obs = clamp_observation()
    choices = clamp_selections(obs)
    choices["voltage.0.REL.maximum"][part][0]["value"] += 1
    with pytest.raises(vfe.VfeEquationError, match="ROM binding"):
        vfe.clamp_contracts(obs, choices)


def test_upper_clamp_multiple_rows_and_alias_rejection():
    obs = clamp_observation(next_index=0xFFFF)
    other = clamp_observation(401, lower=0, upper=1000000, next_index=0xFFFF, variable_count=1)
    for name, stride in (("info", vfe.INFO_STRIDE), ("control", vfe.CONTROL_STRIDE)):
        at = vfe.HEADER + 401 * stride
        raw = bytearray.fromhex(obs[name + "_hex"])
        raw[at : at + stride] = bytes.fromhex(other[name + "_hex"])[at : at + stride]
        obs[name + "_hex"] = raw.hex()
    choices = {**clamp_selections(obs), **clamp_selections(obs, 401, 999000, "voltage.1.REL.maximum")}
    saved = vfe.clamp_contracts(obs, choices)
    assert vfe.verify_clamps(saved["after"], saved) == list(choices)
    assert vfe.upper_clamp_row(saved["after"], 401)["upper_uv"] == 999000
    choices["duplicate"] = dict(choices["voltage.1.REL.maximum"])
    with pytest.raises(vfe.VfeEquationError, match="alias"):
        vfe.clamp_contracts(obs, choices)


@pytest.mark.parametrize(
    "name,offset",
    [
        ("info", 3),
        ("control", 0x407),
        ("info", vfe.INFO_SIZE - 1),
        ("control", vfe.CONTROL_SIZE - 1),
        ("control", vfe.HEADER + 37 * vfe.CONTROL_STRIDE + 0x16),
        ("control", vfe.HEADER + 401 * vfe.CONTROL_STRIDE + 23),
    ],
)
@pytest.mark.parametrize("restore", [False, True])
def test_upper_clamp_preserves_reserved_term_bytes_full_packets_and_unknown_peers(name, offset, restore):
    obs = clamp_observation()
    saved = vfe.clamp_contracts(obs, clamp_selections(obs))
    live = copy.deepcopy(saved["before" if restore else "after"])
    mutate(live, name, offset, bytes.fromhex(live[name + "_hex"])[offset] ^ 1)
    with pytest.raises(vfe.VfeEquationError, match="preserved peers"):
        vfe.verify_clamps(live, saved, restore=restore)


@pytest.mark.parametrize("fault", ["empty", "missing", "kind", "schema", "selected", "peer", "identity", "binding"])
def test_upper_clamp_receipt_validation_recomputes_projection(fault):
    obs = clamp_observation()
    choices = clamp_selections(obs)
    saved = vfe.clamp_contracts(obs, choices)
    if fault == "empty":
        saved = {}
    elif fault == "missing":
        del saved["kind"]
    elif fault == "kind":
        saved["kind"] = "constants"
    elif fault == "schema":
        saved["schema"] = True
    elif fault == "selected":
        saved["selected"] = {}
    elif fault == "peer":
        mutate(saved["after"], "control", 3, 1)
    elif fault == "identity":
        saved["after"]["identity"]["device_id"] += 1
    else:
        saved["selected"]["voltage.0.REL.maximum"]["variables"][0]["value"] += 1
    with pytest.raises(vfe.VfeEquationError):
        vfe.validate_clamp_contracts(choices, saved)


@pytest.mark.parametrize("restore", [False, True])
def test_upper_clamp_changed_adapter_and_ignored_or_rounded_write_fail(restore):
    obs = clamp_observation()
    saved = vfe.clamp_contracts(obs, clamp_selections(obs))
    live = copy.deepcopy(saved["before" if restore else "after"])
    live["identity"]["slot"] = "0000:02:00.0"
    with pytest.raises(vfe.VfeEquationError, match="preserved peers"):
        vfe.verify_clamps(live, saved, restore=restore)
    with pytest.raises(vfe.VfeEquationError):
        vfe.verify_clamps(saved["after" if restore else "before"], saved, restore=restore)
    live = copy.deepcopy(saved["before" if restore else "after"])
    mutate(live, "control", vfe.HEADER + 37 * vfe.CONTROL_STRIDE + 8, 1034000, "f")
    with pytest.raises(vfe.VfeEquationError):
        vfe.verify_clamps(live, saved, restore=restore)


@pytest.mark.parametrize(
    "choices", [None, [], True, {"": {}}, {1: {}}, {"a": None}, {"a": {}}, {"a": {"index": 1, "extra": 2}}]
)
def test_upper_clamp_bad_selection_shapes_fail_before_observation(choices):
    with pytest.raises(vfe.VfeEquationError):
        vfe.clamp_contracts(None, choices)


def test_upper_clamp_binds_parser_metadata_on_a_partial_rail_alternate_identity():
    from druta import vbios_fields
    from tests.test_vbios_fields import voltage_fixture

    blob = voltage_fixture()
    field = next(f for f in vbios_fields.inspect(blob)["fields"] if f["key"] == "voltage.0.REL.maximum")
    binding = field["upper_clamp_binding"]
    obs = clamp_observation(
        index=binding["index"],
        lower=binding["lower_uv"],
        upper=binding["before_uv"],
        next_index=binding["next_index"],
        variable=binding["variable"],
        peers=(5, 7999),
        slot="0000:02:00.1",
        architecture=10,
        device_id=0x4567,
        subsystem_id=0x11223344,
    )
    raw = bytearray.fromhex(obs["control_hex"])
    ct = vfe.HEADER + binding["index"] * vfe.CONTROL_STRIDE
    for start, part in ((0x10, "variables"), (0x28, "coefficients")):
        for i, term in enumerate(binding[part]):
            pos = ct + start + i * 8
            raw[pos] = term["type"]
            struct.pack_into({1: "<I", 2: "<B", 3: "<H"}[term["type"]], raw, pos + 4, term["value"])
    obs["control_hex"] = raw.hex()
    choices = {field["key"]: {**binding, "after_uv": 1035000}}
    saved = vfe.clamp_contracts(obs, choices)
    assert vfe.verify_clamps(saved["after"], saved) == [field["key"]]


def bounds_selections(obs=None, index=37, lower=1035000, upper=1035000, key="voltage.0.REL.value"):
    row = clamp_selections(obs, index, upper, key)[key]
    return {key: {**row, "after_lower_uv": lower}}


@pytest.mark.parametrize("lower,upper", [(1035000, 1035000), (1045000, 1045000), (800000, 850000), (0, 1200000)])
def test_voltage_bounds_projects_exact_two_words_without_changing_polynomial(lower, upper):
    obs = clamp_observation()
    original = copy.deepcopy(obs)
    choices = bounds_selections(obs, lower=lower, upper=upper)
    selected_before = copy.deepcopy(choices)
    saved = vfe.voltage_bounds_contracts(obs, choices)
    assert obs == original and choices == selected_before
    assert saved["kind"] == "voltage_bounds" and saved["schema"] == 1
    assert saved["selected"] == choices
    expected = bytearray.fromhex(obs["control_hex"])
    struct.pack_into("<ff", expected, vfe.HEADER + 37 * vfe.CONTROL_STRIDE + 4, lower, upper)
    assert saved["after"] == {**obs, "control_hex": expected.hex()}
    assert vfe.project_voltage_bounds(obs, choices) == saved["after"]
    vfe.validate_voltage_bounds_contracts(choices, saved)
    assert vfe.verify_voltage_bounds(saved["after"], saved) == list(choices)
    assert vfe.verify_voltage_bounds(saved["before"], saved, restore=True) == list(choices)
    assert vfe.upper_clamp_row(saved["after"], 37)["lower_uv"] == lower
    assert vfe.upper_clamp_row(saved["after"], 37)["upper_uv"] == upper
    saved["selected"]["voltage.0.REL.value"]["coefficients"][0]["value"] = 0
    assert choices == selected_before


@pytest.mark.parametrize("index,nxt,count,arch", [(0, 0xFFFF, 1, 4), (8111, 401, 3, 10), (8191, 7999, 2, 7)])
def test_voltage_bounds_alternate_identity_ordinals_and_partial_opaque_peers(index, nxt, count, arch):
    obs = clamp_observation(
        index=index,
        next_index=nxt,
        variable_count=count,
        slot="0000:fe:1f.7",
        architecture=arch,
        device_id=0x4567,
        subsystem_id=0xFEDCBA98,
    )
    choices = bounds_selections(obs, index)
    saved = vfe.voltage_bounds_contracts(obs, choices)
    assert vfe.verify_voltage_bounds(saved["after"], saved) == list(choices)
    assert saved["after"]["identity"] == obs["identity"]
    obs = clamp_observation(index=index, next_index=0xFFFF, peers=())
    assert vfe.voltage_bounds_contracts(obs, bounds_selections(obs, index))["after"]["info_hex"] == obs["info_hex"]


def test_voltage_bounds_combines_fixed_and_upper_only_without_aliasing():
    obs = clamp_observation(next_index=0xFFFF)
    other = clamp_observation(401, lower=0, upper=1000000, next_index=0xFFFF, variable_count=1)
    for name, stride in (("info", vfe.INFO_STRIDE), ("control", vfe.CONTROL_STRIDE)):
        at = vfe.HEADER + 401 * stride
        raw = bytearray.fromhex(obs[name + "_hex"])
        raw[at : at + stride] = bytes.fromhex(other[name + "_hex"])[at : at + stride]
        obs[name + "_hex"] = raw.hex()
    choices = {
        **bounds_selections(obs),
        **bounds_selections(obs, 401, 0, 999000, "voltage.1.REL.maximum"),
    }
    saved = vfe.voltage_bounds_contracts(obs, choices)
    assert vfe.verify_voltage_bounds(saved["after"], saved) == list(choices)
    expected = bytearray.fromhex(obs["control_hex"])
    struct.pack_into("<ff", expected, vfe.HEADER + 37 * vfe.CONTROL_STRIDE + 4, 1035000, 1035000)
    struct.pack_into("<f", expected, vfe.HEADER + 401 * vfe.CONTROL_STRIDE + 8, 999000)
    assert saved["after"]["control_hex"] == expected.hex()
    choices["voltage.0.REL.maximum"] = dict(choices["voltage.0.REL.value"])
    with pytest.raises(vfe.VfeEquationError, match="alias"):
        vfe.voltage_bounds_contracts(obs, choices)


def test_voltage_bounds_empty_noop_and_legacy_receipts_remain_separate():
    assert vfe.voltage_bounds_contracts(None, {}) == {}
    assert vfe.validate_voltage_bounds_contracts({}, {}) is None
    assert vfe.verify_voltage_bounds(None, {}) == []
    obs = clamp_observation()
    assert vfe.project_voltage_bounds(obs, {}) == obs
    new = vfe.voltage_bounds_contracts(obs, bounds_selections(obs, lower=900000, upper=1120000))
    assert new["before"] == new["after"]
    old = vfe.clamp_contracts(obs, clamp_selections(obs))
    constant = vfe.contracts(observation(), selections())
    assert old["kind"] == "upper_clamps" and "kind" not in constant
    assert "after_lower_uv" not in old["selected"]["voltage.0.REL.maximum"]
    assert vfe.verify_clamps(old["after"], old) == ["voltage.0.REL.maximum"]
    assert vfe.verify(constant["before"], constant, restore=True) == ["voltage.1.ALT.addend"]
    for saved in (old, constant):
        with pytest.raises(vfe.VfeEquationError):
            vfe.verify_voltage_bounds(saved["before"], saved, restore=True)
    with pytest.raises(vfe.VfeEquationError):
        vfe.verify_clamps(new["after"], new)


@pytest.mark.parametrize(
    "field,value",
    [
        ("after_lower_uv", True),
        ("after_lower_uv", 1035000.0),
        ("after_lower_uv", float("nan")),
        ("after_lower_uv", -1),
        ("after_lower_uv", 1035001),
        ("after_lower_uv", 16777217),
        ("after_lower_uv", 2**31),
        ("after_uv", 0),
        ("after_uv", float("inf")),
        ("after_uv", 1034999),
        ("after_uv", 16777217),
        ("after_uv", 2**31),
        ("lower_uv", 1120001),
        ("before_uv", 899999),
        ("before_uv", True),
        ("index", True),
        ("next_index", 8192),
        ("output_type", 1),
        ("variables", []),
        ("coefficients", {}),
    ],
)
def test_voltage_bounds_rejects_bad_values_and_ordering(field, value):
    choices = bounds_selections()
    choices["voltage.0.REL.value"][field] = value
    with pytest.raises(vfe.VfeEquationError):
        vfe.voltage_bounds_contracts(clamp_observation(), choices)


@pytest.mark.parametrize(
    "field,value", [("lower_uv", 899000), ("before_uv", 1119000), ("next_index", 7999), ("variable", 5)]
)
def test_voltage_bounds_requires_exact_before_metadata(field, value):
    obs, choices = clamp_observation(), bounds_selections()
    choices["voltage.0.REL.value"][field] = value
    with pytest.raises(vfe.VfeEquationError, match="ROM binding"):
        vfe.voltage_bounds_contracts(obs, choices)


@pytest.mark.parametrize("part", ["variables", "coefficients"])
def test_voltage_bounds_preserves_original_descriptor_binding(part):
    obs, choices = clamp_observation(), bounds_selections()
    choices["voltage.0.REL.value"][part][0]["value"] += 1
    with pytest.raises(vfe.VfeEquationError, match="ROM binding"):
        vfe.voltage_bounds_contracts(obs, choices)


@pytest.mark.parametrize("restore", [False, True])
@pytest.mark.parametrize("offset", [4, 8])
def test_voltage_bounds_ignored_or_rounded_selected_word_fails_apply_and_recovery(restore, offset):
    obs = clamp_observation()
    saved = vfe.voltage_bounds_contracts(obs, bounds_selections(obs))
    with pytest.raises(vfe.VfeEquationError):
        vfe.verify_voltage_bounds(saved["after" if restore else "before"], saved, restore=restore)
    live = copy.deepcopy(saved["before" if restore else "after"])
    mutate(live, "control", vfe.HEADER + 37 * vfe.CONTROL_STRIDE + offset, 1036000, "f")
    with pytest.raises(vfe.VfeEquationError, match="preserved peers"):
        vfe.verify_voltage_bounds(live, saved, restore=restore)


@pytest.mark.parametrize("restore", [False, True])
@pytest.mark.parametrize(
    "name,offset",
    [
        ("info", 3),
        ("control", 0x407),
        ("info", vfe.INFO_SIZE - 1),
        ("control", vfe.CONTROL_SIZE - 1),
        ("control", vfe.HEADER + 37 * vfe.CONTROL_STRIDE + 0x16),
        ("control", vfe.HEADER + 37 * vfe.CONTROL_STRIDE + 0x2C),
        ("info", vfe.HEADER + 37 * vfe.INFO_STRIDE + 2),
        ("control", vfe.HEADER + 401 * vfe.CONTROL_STRIDE + 23),
    ],
)
def test_voltage_bounds_preserves_all_opaque_terms_links_and_peer_bytes(name, offset, restore):
    obs = clamp_observation()
    saved = vfe.voltage_bounds_contracts(obs, bounds_selections(obs))
    live = copy.deepcopy(saved["before" if restore else "after"])
    mutate(live, name, offset, bytes.fromhex(live[name + "_hex"])[offset] ^ 1)
    with pytest.raises(vfe.VfeEquationError, match="preserved peers"):
        vfe.verify_voltage_bounds(live, saved, restore=restore)


@pytest.mark.parametrize("fault", ["missing", "kind", "schema", "selected", "lower", "peer", "identity", "binding"])
def test_voltage_bounds_malformed_recovery_receipt_recomputed_before_verification(fault):
    obs, choices = clamp_observation(), bounds_selections()
    saved = vfe.voltage_bounds_contracts(obs, choices)
    if fault == "missing":
        del saved["selected"]["voltage.0.REL.value"]["after_lower_uv"]
    elif fault == "kind":
        saved["kind"] = "upper_clamps"
    elif fault == "schema":
        saved["schema"] = True
    elif fault == "selected":
        saved["selected"] = {}
    elif fault == "lower":
        saved["selected"]["voltage.0.REL.value"]["after_lower_uv"] = 1034000
    elif fault == "peer":
        mutate(saved["after"], "control", 3, 1)
    elif fault == "identity":
        saved["after"]["identity"]["device_id"] += 1
    else:
        saved["selected"]["voltage.0.REL.value"]["variables"][0]["value"] += 1
    with pytest.raises(vfe.VfeEquationError):
        vfe.validate_voltage_bounds_contracts(choices, saved)
    with pytest.raises(vfe.VfeEquationError):
        vfe.verify_voltage_bounds(obs, saved, restore=True)


@pytest.mark.parametrize("choices", [None, [], True, {"": {}}, {1: {}}, {"a": None}, {"a": {}}, {"a": {"extra": 2}}])
def test_voltage_bounds_bad_shapes_rejected_without_readback(choices):
    with pytest.raises(vfe.VfeEquationError):
        vfe.voltage_bounds_contracts(None, choices)
