"""Observable transaction behavior, independent of a developer's GPU."""

import copy
import hashlib
import struct
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from druta import registry_overrides as ro
from druta import vbios_fields


def digest(data):
    return hashlib.sha256(data).hexdigest()


def plan(image=b"abcdef", after=b"abXYef"):
    return {
        "baseline_sha256": digest(image),
        "expected_sha256": digest(after),
        "patches": [{"offset": 2, "before": "6364", "after": "5859", "field_keys": ["example"]}],
    }


def test_append_preserves_existing_values_and_selector():
    original = {
        "romOverride00": {"type": 3, "value": "01020304050607080900"},
        "vbios": {"type": 3, "value": "aabb"},
        "EnableGpuFirmware": {"type": 4, "value": 16},
    }
    desired = ro.compose_records(b"abcdef", plan(), original, 0x12345678, 9)
    assert desired["romOverride00"] == original["romOverride00"]
    assert desired["vbios"] == original["vbios"]
    assert desired["romOverride01"]["value"] == "123456780902000000025859"
    assert desired["EnableGpuFirmware"]["value"] == 16
    assert original["EnableGpuFirmware"]["value"] == 16


@pytest.mark.parametrize("entry", [None, {"type": 4, "value": 0xABCDEF12}, {"type": 3, "value": "deadbeef"}])
def test_record_edits_preserve_firmware_selector_absence_type_and_value(entry):
    original = {} if entry is None else {"eNaBlEgPuFiRmWaRe": entry}
    before = copy.deepcopy(original)
    after = ro.compose_records(b"abcdef", plan(), original, 1, 0)
    assert original == before
    assert {k: v for k, v in after.items() if not k.startswith("romOverride")} == before


def test_unknown_firmware_policy_is_rejected():
    with pytest.raises(ro.OverrideError, match="firmware-selector"):
        ro.compose_records(b"abcdef", plan(), {}, 1, 0, firmware_policy="guess")


@pytest.mark.parametrize(
    "records",
    [
        {"romOverride01": {"type": 3, "value": "00"}},
        {"romOverride00": {"type": 4, "value": 1}},
    ],
)
def test_existing_record_conflict_is_preserved(records):
    saved = copy.deepcopy(records)
    with pytest.raises(ro.OverrideError):
        ro.compose_records(b"abcdef", plan(), records, 1, 0)
    assert records == saved


def test_long_payload_splits_with_exact_offsets():
    image = bytes(500)
    changed = bytes([1]) * 500
    p = {
        "baseline_sha256": digest(image),
        "expected_sha256": digest(changed),
        "patches": [{"offset": 0, "before": image.hex(), "after": changed.hex()}],
    }
    values = ro.compose_records(image, p, {}, 0xAABBCCDD, 7)
    chunks = [bytes.fromhex(values[f"romOverride{i:02X}"]["value"]) for i in range(3)]
    assert [len(c) for c in chunks] == [256, 256, 18]
    assert [int.from_bytes(c[5:9], "little") for c in chunks] == [0, 246, 492]
    assert b"".join(c[10:] for c in chunks) == changed


@pytest.mark.parametrize("mutation", ["hash", "before", "offset", "overlap", "result"])
def test_reject_corrupt_plan(mutation):
    p = plan()
    if mutation == "hash":
        p["baseline_sha256"] = "0" * 64
    if mutation == "before":
        p["patches"][0]["before"] = "0000"
    if mutation == "offset":
        p["patches"][0]["offset"] = 99
    if mutation == "overlap":
        p["patches"].append(copy.deepcopy(p["patches"][0]))
    if mutation == "result":
        p["expected_sha256"] = "0" * 64
    with pytest.raises(ro.OverrideError):
        ro.compose_records(b"abcdef", p, {}, 1, 0)


def test_record_capacity():
    records = {f"romOverride{i:02X}": {"type": 3, "value": "00"} for i in range(256)}
    with pytest.raises(ro.OverrideError, match="insufficient"):
        ro.compose_records(b"abcdef", plan(), records, 1, 0)


@pytest.mark.parametrize("value", [301000, 9000])
def test_semantic_policy_check_accepts_adapter_values(value):
    snap = {"policies": [{"policy": 13, "consistent": True, "default": value}]}
    assert ro.verify_policy_fields(snap, {"power.13.default": value}) == ["power.13.default"]
    with pytest.raises(ro.OverrideError, match="runtime policy reports"):
        ro.verify_policy_fields(snap, {"power.13.default": value + 1})


class FakeRegistry:
    def __init__(self):
        self.values = {}
        self.operations = []

    def snapshot(self):
        return copy.deepcopy(self.values)

    def replace(self, expected, desired):
        if self.values != expected:
            raise ro.OverrideError("concurrent write")
        self.operations.append(copy.deepcopy(desired))
        self.values = copy.deepcopy(desired)


def transaction(monkeypatch):
    target = ro.Target("0000:08:05.1", r"PCI\VEN_10DE&DEV_ABCD\OTHER", "some-driver-key")
    p = plan()
    after = ro.compose_records(b"abcdef", p, {}, 1, 0)
    receipt = {
        "id": "x",
        "path": "receipt.json",
        "target": ro.asdict(target),
        "state": "prepared",
        "before": {},
        "after": after,
        "before_image_sha256": digest(b"abcdef"),
        "after_image_sha256": digest(b"abXYef"),
        "plan": p,
        "changes": {},
        "events": [],
        "parent": None,
        "ok": False,
        "reboot_required": False,
        "firmware_selector_policy": "preserve",
    }
    monkeypatch.setattr(
        vbios_fields, "inspect", lambda image: {"identity": {"vbios_version_raw": 1, "oem_revision": 0}}
    )
    monkeypatch.setattr(vbios_fields, "plan", lambda image, changes: p)
    monkeypatch.setattr(ro, "transaction_lock", lambda target: nullcontext())
    registry = FakeRegistry()
    observation = {"image": b"abcdef"}
    restarts = []

    def restart(device):
        restarts.append(device)
        observation["image"] = b"abXYef" if registry.values else b"abcdef"
        return ro.devicereset.RestartResult(True, False, "restarted", 0)

    kwargs = {
        "gpu_factory": lambda slot: SimpleNamespace(),
        "registry_factory": lambda target: registry,
        "restart": restart,
        "bind": lambda gpu: target,
        "reader": lambda gpu: observation["image"],
        "save": lambda path, data: None,
    }
    return receipt, registry, observation, restarts, kwargs


def test_apply_then_exact_restore(monkeypatch):
    receipt, reg, observed, restarts, kwargs = transaction(monkeypatch)
    assert ro.execute(receipt, **kwargs)["state"] == "active"
    assert reg.values == receipt["after"]
    assert observed["image"] == b"abXYef"
    assert ro.execute(receipt, restore=True, **kwargs)["state"] == "restored"
    assert reg.values == {}
    assert observed["image"] == b"abcdef"
    assert len(restarts) == 2


@pytest.mark.parametrize("failure", [None, "before", "rounding", "peer", "undo", "contract"])
def test_vmin_contract_checks_effective_state_and_exact_recovery(monkeypatch, failure):
    from tests.test_voltage_bios_runtime import GPU, inspection

    vr = ro.voltage_bios_runtime
    receipt, reg, observed, restarts, kwargs = transaction(monkeypatch)
    gpu = GPU()
    gpu.address = receipt["target"]["slot"]
    gpu.nvapi.selected["slot"] = gpu.address
    gpu.nvml.selected["slot"] = gpu.address
    receipt["changes"] = {"voltage.0.VMIN": 800000}
    contract = vr.contracts(gpu, inspection(), receipt["changes"])
    receipt["voltage_contracts"] = copy.deepcopy(contract)
    observed["voltage_policy"] = copy.deepcopy(contract["before"])
    if failure == "before":
        observed["voltage_policy"]["rails"]["0"]["policy_uv"]["vmin"] += 1
    if failure == "contract":
        receipt["voltage_contracts"]["selected"] = {}
    kwargs["gpu_factory"] = lambda slot: observed
    original = kwargs["restart"]

    def restart(target):
        result = original(target)
        observed["voltage_policy"] = copy.deepcopy(contract["after"] if reg.values else contract["before"])
        if reg.values and failure == "rounding":
            observed["voltage_policy"]["rails"]["0"]["policy_uv"]["vmin"] += 6250
        if reg.values and failure == "peer":
            observed["voltage_policy"]["rails"]["1"]["policy_uv"]["overvoltage"] += 1
        if not reg.values and failure == "undo":
            observed["voltage_policy"]["rails"]["0"]["policy_uv"]["vmin"] += 1
        return result

    kwargs["restart"] = restart
    result = ro.execute(receipt, **kwargs)
    if failure in ("before", "contract"):
        assert result["state"] == "failed" and not reg.operations and not restarts
    elif failure in ("rounding", "peer"):
        assert result["state"] == "rolled-back" and not reg.values and len(restarts) == 2
    else:
        assert result["state"] == "active" and result["runtime_verified_fields"] == ["voltage.0.VMIN"]
        # Restoration must clean registry before requiring a functional getter.
        kwargs["gpu_factory"] = lambda slot: observed if not reg.values else pytest.fail("Premature recovery GET")
        result = ro.execute(receipt, restore=True, **kwargs)
        assert result["state"] == ("recovery-required" if failure == "undo" else "restored")
        assert not reg.values


def test_missing_new_vmin_contract_refused_before_restore(monkeypatch):
    receipt, reg, observed, restarts, kwargs = transaction(monkeypatch)
    receipt["changes"] = {"voltage.0.VMIN": 800000}
    receipt["voltage_contracts"] = {}
    reg.values = copy.deepcopy(receipt["after"])
    kwargs["gpu_factory"] = lambda slot: pytest.fail("Invalid receipt queried GPU")
    assert ro.execute(receipt, restore=True, **kwargs)["state"] == "failed"
    assert reg.values == receipt["after"] and not reg.operations and not restarts


@pytest.mark.parametrize("failure", [None, "before", "ignored", "peer", "route", "cooler", "undo", "contract", "slot"])
def test_fan_policy_contract_checks_configuration_and_exact_recovery(monkeypatch, failure):
    from tests.test_fan_policy_runtime import Gpu, apply, inspection, observe

    fr = ro.fan_policy_runtime
    receipt, reg, observed, restarts, kwargs = transaction(monkeypatch)
    gpu = Gpu(slot=receipt["target"]["slot"])
    key = "fan_stop.3.low"
    receipt["changes"] = {key: 40.03125}
    contract = fr.contracts(gpu, inspection(), receipt["changes"])
    receipt["fan_policy_contracts"] = copy.deepcopy(contract)
    baseline = observe(gpu)
    apply(gpu, receipt["changes"])
    active = observe(gpu)
    observed["fan_policies"] = copy.deepcopy(baseline)

    def alter_temperature(observation, index, offset, value):
        row = next(row for row in observation["entries"] if row["index"] == index)
        raw = bytearray.fromhex(row["control_hex"])
        struct.pack_into("<i", raw, offset, value)
        row["control_hex"] = raw.hex()

    if failure == "before":
        alter_temperature(observed["fan_policies"], 3, 0x4C, 39 * 256)
    if failure == "contract":
        receipt["fan_policy_contracts"] = {}
    if failure == "slot":
        state = receipt["fan_policy_contracts"][key]["unchanged"]
        state["state"]["identity"]["slot"] = "0000:72:1d.0"
        state["cooler"]["state"]["identity"]["slot"] = "0000:72:1d.0"
    kwargs["gpu_factory"] = lambda slot: observed
    original = kwargs["restart"]

    def restart(target):
        result = original(target)
        policy = copy.deepcopy(active if reg.values else baseline)
        if reg.values and failure == "ignored":
            policy = copy.deepcopy(baseline)
        if reg.values and failure == "peer":
            alter_temperature(policy, 9, 0x50, 46 * 256)
        if reg.values and failure == "route":
            raw = bytearray.fromhex(policy["routing_entries"][0]["raw_hex"])
            struct.pack_into("<I", raw, 12, 1 << 9)
            policy["routing_entries"][0]["raw_hex"] = raw.hex()
        if reg.values and failure == "cooler":
            policy["cooler"]["entries"][0]["pwm_frequency_hz"] += 1
        if not reg.values and failure == "undo":
            alter_temperature(policy, 3, 0x4C, 39 * 256)
        observed["fan_policies"] = policy
        return result

    kwargs["restart"] = restart
    result = ro.execute(receipt, **kwargs)
    if failure in ("before", "contract", "slot"):
        assert result["state"] == "failed" and not reg.operations and not restarts
    elif failure in ("ignored", "peer", "route", "cooler"):
        assert result["state"] == "rolled-back" and not reg.values and len(restarts) == 2
    else:
        assert result["state"] == "active" and result["runtime_verified_fields"] == [key]
        kwargs["gpu_factory"] = lambda slot: observed if not reg.values else pytest.fail("Premature recovery GET")
        result = ro.execute(receipt, restore=True, **kwargs)
        assert result["state"] == ("recovery-required" if failure == "undo" else "restored")
        assert not reg.values


def test_missing_new_fan_policy_contract_refused_before_restore(monkeypatch):
    receipt, reg, observed, restarts, kwargs = transaction(monkeypatch)
    receipt["changes"] = {"fan_stop.3.low": 40.03125}
    receipt["fan_policy_contracts"] = {}
    reg.values = copy.deepcopy(receipt["after"])
    kwargs["gpu_factory"] = lambda slot: pytest.fail("Invalid receipt queried GPU")
    assert ro.execute(receipt, restore=True, **kwargs)["state"] == "failed"
    assert reg.values == receipt["after"] and not reg.operations and not restarts


def test_old_prepared_receipt_retains_its_explicit_firmware_plan_and_exact_undo(monkeypatch):
    receipt, reg, observed, restarts, kwargs = transaction(monkeypatch)
    del receipt["firmware_selector_policy"]
    receipt["after"] = ro.compose_records(b"abcdef", receipt["plan"], {}, 1, 0, firmware_policy="legacy-enable")
    assert ro.execute(receipt, **kwargs)["state"] == "active"
    assert reg.values["EnableGpuFirmware"] == {"type": 4, "value": 0x11}
    assert ro.execute(receipt, restore=True, **kwargs)["state"] == "restored"
    assert reg.values == {} and len(restarts) == 2


def flags_observation(enabled=True, peer=0):
    from druta.perf_diagnostics import LAYOUTS

    layout = LAYOUTS["limits"]
    return {
        "group": "limits",
        "capacity": 512,
        "info_command": f"0x{layout.info_command:08X}",
        "control_command": f"0x{layout.control_command:08X}",
        "info_bytes": layout.info_size,
        "control_bytes": layout.control_size,
        "slot": "0000:08:05.1",
        "architecture": 8,
        "identity": {"slot": "0000:08:05.1", "architecture": 8, "device_id": 0xABCD, "subsystem_id": 0x12345678},
        "mask_hex": (1 << 17).to_bytes(64, "little").hex(),
        "present_count": 1,
        "entries": [{"index": 17, "type": 6, "control_type": 6}],
        "flags": {"limits_control_0x4c": {"raw": peer}, "limits_control_0x4d": {"raw": int(enabled)}},
        "cache_enabled": enabled,
        "stable_reads": 2,
    }


def test_flag_only_prepare_does_not_require_bios_or_vfe(monkeypatch, tmp_path):
    target = ro.Target("0000:08:05.1", r"PCI\VEN_10DE&DEV_ABCD\OTHER", "some-driver-key")
    monkeypatch.setattr(ro, "bind_target", lambda gpu: target)
    monkeypatch.setattr(ro, "state_directory", lambda: tmp_path)
    monkeypatch.setattr(ro, "active_receipt", lambda target: None)
    monkeypatch.setattr(ro, "read_vbios", lambda gpu: pytest.fail("Unrelated BIOS read"))
    monkeypatch.setattr(ro, "read_model_inputs", lambda gpu: (None, "VFE unavailable"))
    monkeypatch.setattr(ro.registry_flags, "read", lambda gpu: (flags_observation(), None))
    receipt = ro.prepare(SimpleNamespace(), {}, registry=FakeRegistry(), flag_changes={"perf_limits.cache": False})
    assert not receipt["verify_bios"]
    assert receipt["after"] == {"RmPerfLimitsOverride": {"type": 4, "value": 4}}
    assert receipt["flag_contracts"]["perf_limits.cache"]["before"] is True


def pstate_observation(enabled=False):
    # Independent two-state/two-domain fixture, unrelated to the development GPU.
    public, native, mapping = bytearray(7416), bytearray(15768), bytearray(392)
    struct.pack_into("<5I", public, 0, 0x31CF8, 1, 2, 2, 0)
    struct.pack_into("<5I", native, 0, 0x23D98, 1, 2, 2, 0)
    struct.pack_into("<I", mapping, 0, 0x20188)
    for index in range(32):
        struct.pack_into("<3I", mapping, 8 + index * 12, 32, 0, 100)
    for domain, native_domain in ((0, 0), (4, 6)):
        struct.pack_into("<3I", mapping, 8 + domain * 12, native_domain, 1, 100)
    for index, pstate in enumerate((0, 5)):
        active = pstate == 0 or enabled
        pp, np = 20 + index * 456, 20 + index * 968
        struct.pack_into("<2I", public, pp, pstate, int(active))
        struct.pack_into("<2I", native, np, pstate, int(active))
        for clock, (domain, native_domain) in enumerate(((0, 0), (4, 6))):
            flag = active and domain == 0
            pc, nc = pp + 8 + clock * 44, np + 8 + clock * 44
            struct.pack_into("<3I", public, pc, domain, 1, int(flag))
            struct.pack_into("<3I", native, nc, native_domain, 2, int(flag))
            triple = (0, -500000, 500000) if domain == 0 else (0, 0, 0)
            struct.pack_into("<3i", native, nc + 12, *triple)
            struct.pack_into("<3i", public, pc + 12, *(triple if flag else (0, 0, 0)))
            for packet, pos in ((public, pc), (native, nc)):
                struct.pack_into("<5I", packet, pos + 24, 1000, 2000, 16, 0, 0)
    identity = {"slot": "0000:08:05.1", "architecture": 8, "device_id": 0xABCD, "subsystem_id": 0x12345678}
    result = ro.all_pstates_registry._observe(identity, bytes(public), bytes(native), bytes(mapping))
    result["stable_reads"] = 2
    return result


def test_pstate_only_prepare_is_independent_and_manages_case_insensitive_name(monkeypatch, tmp_path):
    target = ro.Target("0000:08:05.1", r"PCI\VEN_10DE&DEV_ABCD\OTHER", "some-driver-key")
    monkeypatch.setattr(ro, "bind_target", lambda gpu: target)
    monkeypatch.setattr(ro, "state_directory", lambda: tmp_path)
    monkeypatch.setattr(ro, "active_receipt", lambda target: None)
    monkeypatch.setattr(ro, "read_vbios", lambda gpu: pytest.fail("Unrelated BIOS read"))
    monkeypatch.setattr(ro, "read_model_inputs", lambda gpu: (None, "VFE unavailable"))
    monkeypatch.setattr(ro.all_pstates_registry, "read", lambda gpu: (pstate_observation(), None))
    receipt = ro.prepare(SimpleNamespace(), {}, registry=FakeRegistry(), pstate_changes={"pstates.all_editable": True})
    assert not receipt["verify_bios"] and receipt["all_pstate_contracts"]
    assert receipt["after"] == {"RMEnableOverclockingAllPstates": {"type": 4, "value": 1}}
    assert ro.managed_name("rmenableoverclockingallpstates")
    assert not ro.managed_name("RMEnableOverclockingAllPstatesOther")


@pytest.mark.parametrize("failure", [None, "before", "apply", "peer", "rollback"])
def test_all_pstate_transaction_preflight_activation_rollback_and_exact_undo(monkeypatch, failure):
    receipt, reg, observed, restarts, kwargs = transaction(monkeypatch)
    changes = {"pstates.all_editable": True}
    receipt.update(
        verify_bios=False,
        before_image_sha256=None,
        after_image_sha256=None,
        plan={"patches": []},
        pstate_changes=changes,
        all_pstate_contracts=ro.all_pstates_registry.contracts(changes, pstate_observation()),
        after=ro.all_pstates_registry.compose({}, changes, pstate_observation()),
    )

    def gpu(slot):
        enabled = bool(reg.values)
        if failure in ("apply", "rollback") and reg.values:
            enabled = False
        if failure == "before" or failure == "rollback" and len(restarts) > 1:
            enabled = True
        value = pstate_observation(enabled)
        if failure == "peer" and reg.values:
            value["identity"]["subsystem_id"] += 1
        return {"all_pstates": value}

    kwargs["gpu_factory"] = gpu
    result = ro.execute(receipt, **kwargs)
    if failure is None:
        assert result["state"] == "active" and result["runtime_verified_fields"] == list(changes)
        assert ro.execute(receipt, restore=True, **kwargs)["state"] == "restored"
    elif failure == "before":
        assert result["state"] == "failed" and not restarts
    elif failure in ("apply", "peer"):
        assert result["state"] == "rolled-back" and len(restarts) == 2
    else:
        assert result["state"] == "recovery-required" and len(restarts) == 2
    assert reg.values == {}


def test_all_pstate_default_restore_keeps_noncanonical_original_dword(monkeypatch):
    receipt, reg, observed, restarts, kwargs = transaction(monkeypatch)
    name = "rmenableoverclockingallpstates"
    original = {name: {"type": 4, "value": 42}}
    changes = {"pstates.all_editable": False}
    reg.values = copy.deepcopy(original)
    receipt.update(
        verify_bios=False,
        before_image_sha256=None,
        after_image_sha256=None,
        plan={"patches": []},
        pstate_changes=changes,
        all_pstate_contracts=ro.all_pstates_registry.contracts(changes, pstate_observation(True)),
        before=original,
        after=ro.all_pstates_registry.compose(original, changes, pstate_observation(True)),
    )
    kwargs["gpu_factory"] = lambda slot: {"all_pstates": pstate_observation(bool(reg.values))}
    assert ro.execute(receipt, **kwargs)["state"] == "active" and reg.values == {}
    assert ro.execute(receipt, restore=True, **kwargs)["state"] == "restored" and reg.values == original


def test_combining_all_pstate_and_bios_clock_ranges_is_rejected_before_reads(monkeypatch):
    monkeypatch.setattr(ro, "bind_target", lambda gpu: None)
    with pytest.raises(ro.OverrideError, match="separate transactions"):
        ro.prepare(SimpleNamespace(), {"clock.0.maximum": 1001}, pstate_changes={"pstates.all_editable": True})


def clock_vf_observation(monotonicity=True, smoothening=True):
    # Alternate adapter and sentinel-filled reserved bytes: only the traced
    # current header members are written, with no developer-board assumptions.
    header = bytearray([0xA5] * 60)
    header[0] = 255
    header[4:8] = bytes(4)
    header[8:14] = bytes((0, 0, int(monotonicity), int(smoothening), 0, 0))
    header[16:56] = bytes(40)
    header[56] = header[8]
    return {
        "group": "clock_domains",
        "control_command": "0x2080901B",
        "control_bytes": 2108,
        "request_mask": 0,
        "identity": {"slot": "0000:08:05.1", "architecture": 8, "device_id": 0xABCD, "subsystem_id": 0x12345678},
        "header_hex": header.hex(),
        "vf_monotonicity_enforced": monotonicity,
        "vf_smoothening_enforced": smoothening,
        "stable_reads": 2,
    }


def clock_vf_transaction(monkeypatch, *, changes=None, original=None):
    receipt, reg, observed, restarts, kwargs = transaction(monkeypatch)
    changes = {"clock_vf.monotonicity": False} if changes is None else changes
    original = {} if original is None else original

    def observation():
        entry = next(
            (entry for name, entry in reg.values.items() if name.casefold() == "rmclkvfoverride"), {"value": 0}
        )
        return clock_vf_observation((entry["value"] & 3) != 3, (entry["value"] & 12) != 12)

    reg.values = copy.deepcopy(original)
    before = observation()
    receipt.update(
        verify_bios=False,
        before_image_sha256=None,
        after_image_sha256=None,
        plan={"patches": []},
        before=copy.deepcopy(original),
        clock_vf_changes=changes,
        clock_vf_contracts=ro.clock_vf_registry.contracts(changes, before),
        after=ro.clock_vf_registry.compose(original, changes, before),
    )
    kwargs["gpu_factory"] = lambda slot: {"clock_vf": observation()}
    return receipt, reg, observed, restarts, kwargs, observation


def test_clock_vf_only_prepare_does_not_require_unrelated_getters(monkeypatch, tmp_path):
    target = ro.Target("0000:08:05.1", r"PCI\VEN_10DE&DEV_ABCD\OTHER", "some-driver-key")
    monkeypatch.setattr(ro, "bind_target", lambda gpu: target)
    monkeypatch.setattr(ro, "state_directory", lambda: tmp_path)
    monkeypatch.setattr(ro, "active_receipt", lambda target: None)
    monkeypatch.setattr(ro, "read_vbios", lambda gpu: pytest.fail("Unrelated BIOS read"))
    monkeypatch.setattr(ro, "read_model_inputs", lambda gpu: (None, "VFE unavailable"))
    monkeypatch.setattr(ro.registry_flags, "read", lambda gpu: pytest.fail("Unrelated PERF read"))
    monkeypatch.setattr(ro.all_pstates_registry, "read", lambda gpu: pytest.fail("Unrelated P-state read"))
    monkeypatch.setattr(ro.clock_vf_registry, "read", lambda gpu: (clock_vf_observation(), None))
    changes = {"clock_vf.monotonicity": False}
    receipt = ro.prepare(SimpleNamespace(), {}, registry=FakeRegistry(), clock_vf_changes=changes)
    assert not receipt["verify_bios"]
    assert receipt["after"] == {"RMClkVfOverride": {"type": 4, "value": 3}}
    assert receipt["clock_vf_changes"] == changes and receipt["clock_vf_contracts"]
    assert ro.managed_name("rMcLkVfOvErRiDe") and not ro.managed_name("RMClkVfOverrideOther")


@pytest.mark.parametrize("combined", [False, True])
@pytest.mark.parametrize("failure", [None, "before", "apply", "peer", "rollback", "unavailable"])
def test_clock_vf_transaction_checks_preflight_apply_peers_rollback_and_undo(monkeypatch, failure, combined):
    changes = {"clock_vf.monotonicity": False}
    if combined:
        changes["clock_vf.smoothening"] = False
    receipt, reg, _, restarts, kwargs, observation = clock_vf_transaction(monkeypatch, changes=changes)

    def gpu(slot):
        current = observation()
        if failure in ("apply", "rollback") and reg.values:
            current = clock_vf_observation()
        if failure == "before" or failure == "rollback" and len(restarts) > 1:
            current = clock_vf_observation(False)
        if failure == "peer" and reg.values:
            header = bytearray.fromhex(current["header_hex"])
            header[16] = 1
            current["header_hex"] = header.hex()
        if failure == "unavailable" and reg.values:
            current = None
        return {"clock_vf": current, "clock_vf_error": "busy" if current is None else None}

    kwargs["gpu_factory"] = gpu
    result = ro.execute(receipt, **kwargs)
    if failure is None:
        assert result["state"] == "active" and result["runtime_verified_fields"] == sorted(changes)
        assert ro.execute(receipt, restore=True, **kwargs)["state"] == "restored"
    elif failure == "before":
        assert result["state"] == "failed" and not restarts and not reg.operations
    elif failure == "rollback":
        assert result["state"] == "recovery-required" and len(restarts) == 2
    else:
        assert result["state"] == "rolled-back" and len(restarts) == 2
    assert reg.values == {}


def test_clock_vf_exact_undo_preserves_original_case_and_inherited_selector(monkeypatch):
    original = {"rMcLkVfOvErRiDe": {"type": 4, "value": 2}}
    receipt, reg, _, restarts, kwargs, _ = clock_vf_transaction(monkeypatch, original=original)
    assert ro.execute(receipt, **kwargs)["state"] == "active"
    assert reg.values == {"rMcLkVfOvErRiDe": {"type": 4, "value": 3}}
    assert ro.execute(receipt, restore=True, **kwargs)["state"] == "restored"
    assert reg.values == original and len(restarts) == 2


@pytest.mark.parametrize("mutation", ["empty", "missing", "request", "no_request", "before", "extra", "registry"])
def test_clock_vf_malformed_or_unbound_receipt_fails_before_writes(monkeypatch, mutation):
    receipt, reg, _, restarts, kwargs, _ = clock_vf_transaction(monkeypatch)
    contracts = receipt["clock_vf_contracts"]
    if mutation == "empty":
        receipt["clock_vf_contracts"] = {}
    elif mutation == "missing":
        del receipt["clock_vf_contracts"]
    elif mutation == "request":
        receipt["clock_vf_changes"]["clock_vf.monotonicity"] = True
    elif mutation == "no_request":
        receipt["clock_vf_changes"] = {}
    elif mutation == "before":
        contracts["clock_vf.monotonicity"]["before"] = 1
    elif mutation == "extra":
        contracts["clock_vf.untraced"] = copy.deepcopy(contracts["clock_vf.monotonicity"])
    else:
        receipt["after"]["RMClkVfOverride"]["value"] = 15
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "failed" and not reg.operations and not restarts


@pytest.mark.parametrize(
    "mutation",
    ["missing", "empty", "changed_key", "after", "integer_after", "identity", "slot", "header", "no_changes"],
)
def test_clock_vf_restore_rejects_malformed_binding_before_registry_cleanup(monkeypatch, mutation):
    receipt, reg, _, restarts, kwargs, _ = clock_vf_transaction(monkeypatch)
    reg.values = copy.deepcopy(receipt["after"])
    original = copy.deepcopy(reg.values)
    receipt["state"] = "recovery-required"
    contracts = receipt["clock_vf_contracts"]
    contract = contracts["clock_vf.monotonicity"]
    if mutation == "missing":
        del receipt["clock_vf_contracts"]
    elif mutation == "empty":
        receipt["clock_vf_contracts"] = {}
    elif mutation == "changed_key":
        contracts["clock_vf.smoothening"] = contracts.pop("clock_vf.monotonicity")
    elif mutation == "after":
        contract["after"] = True
    elif mutation == "integer_after":
        contract["after"] = 0
    elif mutation == "identity":
        contract["unchanged"]["identity"]["device_id"] = "43981"
    elif mutation == "slot":
        contract["unchanged"]["identity"]["slot"] = "0000:03:00.0"
    elif mutation == "header":
        contract["unchanged"]["header_hex"] = "00"
    else:
        receipt["clock_vf_changes"] = {}
    kwargs["gpu_factory"] = lambda slot: pytest.fail("Malformed recovery must not query the GPU")
    result = ro.execute(receipt, restore=True, **kwargs)
    assert result["state"] == "failed" and not reg.operations and not restarts
    assert reg.values == original


def test_clock_vf_valid_recovery_restores_registry_before_requiring_gpu(monkeypatch):
    receipt, reg, _, restarts, kwargs, observation = clock_vf_transaction(monkeypatch)
    reg.values = copy.deepcopy(receipt["after"])
    receipt["state"] = "recovery-required"

    def gpu(slot):
        assert reg.values == receipt["before"]
        return {"clock_vf": observation()}

    kwargs["gpu_factory"] = gpu
    assert ro.execute(receipt, restore=True, **kwargs)["state"] == "restored"
    assert not reg.values and len(restarts) == 1


def test_clock_vf_and_bios_clock_changes_rejected_during_prepare_and_preflight(monkeypatch):
    monkeypatch.setattr(ro, "bind_target", lambda gpu: None)
    with pytest.raises(ro.OverrideError, match="separate transactions"):
        ro.prepare(SimpleNamespace(), {"clock.0.maximum": 1001}, clock_vf_changes={"clock_vf.monotonicity": False})
    receipt, reg, _, restarts, kwargs, _ = clock_vf_transaction(monkeypatch)
    receipt["changes"] = {"clock.0.maximum": 1001}
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "failed" and "separate transactions" in result["message"]
    assert not reg.operations and not restarts


@pytest.mark.parametrize("outcome", [(None, "unavailable"), (clock_vf_observation(False), None)])
def test_existing_clock_vf_key_requires_reconciled_active_state_before_unrelated_prepare(
    monkeypatch, tmp_path, outcome
):
    target = ro.Target("0000:08:05.1", r"PCI\VEN_10DE&DEV_ABCD\OTHER", "some-driver-key")
    monkeypatch.setattr(ro, "bind_target", lambda gpu: target)
    monkeypatch.setattr(ro, "state_directory", lambda: tmp_path)
    monkeypatch.setattr(ro, "active_receipt", lambda target: None)
    monkeypatch.setattr(ro, "read_model_inputs", lambda gpu: (None, None))
    monkeypatch.setattr(ro.registry_flags, "read", lambda gpu: (flags_observation(), None))
    monkeypatch.setattr(ro.clock_vf_registry, "read", lambda gpu: outcome)
    reg = FakeRegistry()
    reg.values = {"RMClkVfOverride": {"type": 4, "value": 1}}
    with pytest.raises(ro.OverrideError):
        ro.prepare(SimpleNamespace(), {}, registry=reg, flag_changes={"perf_limits.cache": False})
    assert reg.values == {"RMClkVfOverride": {"type": 4, "value": 1}} and not list(tmp_path.iterdir())


def test_old_unrelated_transaction_never_requires_clock_vf_read(monkeypatch):
    receipt, reg, _, restarts, kwargs = transaction(monkeypatch)
    monkeypatch.setattr(ro.clock_vf_registry, "read", lambda gpu: pytest.fail("Unrelated clock VF read"))
    assert ro.execute(receipt, **kwargs)["state"] == "active"
    assert ro.execute(receipt, restore=True, **kwargs)["state"] == "restored"
    assert not reg.values and len(restarts) == 2


def test_clock_vf_backend_object_injection_still_verifies_activation(monkeypatch):
    receipt, reg, _, restarts, kwargs, observation = clock_vf_transaction(monkeypatch)
    kwargs["gpu_factory"] = lambda slot: SimpleNamespace()
    calls = []

    def read(gpu):
        calls.append(True)
        return observation(), None

    monkeypatch.setattr(ro.clock_vf_registry, "read", read)
    assert ro.execute(receipt, **kwargs)["state"] == "active"
    assert ro.execute(receipt, restore=True, **kwargs)["state"] == "restored"
    assert not reg.values and len(restarts) == 2 and len(calls) == 3
    assert receipt["runtime_verified_fields"] == ["clock_vf.monotonicity"]


def test_existing_clock_vf_pending_change_stops_unrelated_apply(monkeypatch):
    receipt, reg, _, restarts, kwargs = transaction(monkeypatch)
    reg.values = {"RMClkVfOverride": {"type": 4, "value": 1}}
    receipt["before"] = copy.deepcopy(reg.values)
    receipt["after"].update(copy.deepcopy(reg.values))
    kwargs["gpu_factory"] = lambda slot: {"clock_vf": clock_vf_observation(False)}
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "failed" and not restarts and not reg.operations
    assert reg.values == receipt["before"]


@pytest.mark.parametrize("available", [False, True])
def test_snapshot_records_clock_vf_capability_independently(monkeypatch, tmp_path, available):
    from druta import nvbackend, voltage_request_gate

    target = ro.Target("0000:08:05.1", r"PCI\VEN_10DE&DEV_ABCD\OTHER", "some-driver-key")
    gpu = SimpleNamespace(
        static={"name": "alternate GPU"},
        read_power_policies=lambda: ([], "policy unavailable"),
        read_power_limit_mw=lambda: 123000,
    )
    monkeypatch.setattr(nvbackend, "GPU", lambda slot: gpu)
    monkeypatch.setattr(ro, "bind_target", lambda obj: target)
    monkeypatch.setattr(ro, "state_directory", lambda: tmp_path)
    monkeypatch.setattr(ro, "read_vbios", lambda obj: b"abcdef")
    monkeypatch.setattr(ro, "read_model_inputs", lambda obj: (None, "VFE unavailable"))
    monkeypatch.setattr(ro.registry_flags, "read", lambda obj: (None, "PERF unavailable"))
    monkeypatch.setattr(ro.all_pstates_registry, "read", lambda obj: (None, "P-states unavailable"))
    value, error = (clock_vf_observation(), None) if available else (None, "clock VF unavailable")
    monkeypatch.setattr(ro.clock_vf_registry, "read", lambda obj: (value, error))
    monkeypatch.setattr(ro.bios_runtime, "read_clock_ranges", lambda obj: {})
    monkeypatch.setattr(ro.bios_runtime, "read_thermal_thresholds", lambda obj: {})
    monkeypatch.setattr(ro.fan_runtime, "read", lambda obj: (None, "fan unavailable"))
    monkeypatch.setattr(voltage_request_gate, "VoltageRequestGate", lambda obj: SimpleNamespace(read=lambda: {}))
    saved = []
    monkeypatch.setattr(ro.startup, "atomic_json", lambda path, data: saved.append(copy.deepcopy(data)))
    assert ro.snapshot_cli([target.slot, str(tmp_path / ("probe-" + "a" * 32 + ".json"))]) == 0
    assert saved[0]["clock_vf"] == value and saved[0]["clock_vf_error"] == error
    assert saved[0]["policy_error"] == "policy unavailable" and saved[0]["fan_bounds_error"] == "fan unavailable"
    assert saved[0]["configured_power_mw"] == 123000


@pytest.mark.parametrize("failure", [None, "before", "apply", "peer", "rollback"])
def test_flag_transaction_verifies_preflight_apply_and_exact_undo(monkeypatch, failure):
    receipt, reg, observed, restarts, kwargs = transaction(monkeypatch)
    changes = {"perf_limits.cache": False}
    receipt.update(
        verify_bios=False,
        before_image_sha256=None,
        after_image_sha256=None,
        plan={"patches": []},
        flag_changes=changes,
        flag_contracts=ro.registry_flags.contracts(changes, flags_observation()),
        after=ro.registry_flags.compose({}, changes, flags_observation()),
    )

    def gpu(slot):
        enabled = not bool(reg.values)
        if failure in ("apply", "rollback") and reg.values:
            enabled = True
        if failure == "before" or failure == "rollback" and len(restarts) > 1:
            enabled = False
        peer = 1 if failure == "peer" and reg.values else 0
        return {"driver_flags": flags_observation(enabled, peer)}

    kwargs["gpu_factory"] = gpu
    result = ro.execute(receipt, **kwargs)
    if failure is None:
        assert result["state"] == "active"
        assert result["runtime_verified_fields"] == ["perf_limits.cache"]
        assert ro.execute(receipt, restore=True, **kwargs)["state"] == "restored"
    elif failure == "before":
        assert result["state"] == "failed" and not restarts
    elif failure in ("apply", "peer"):
        assert result["state"] == "rolled-back" and len(restarts) == 2
    else:
        assert result["state"] == "recovery-required" and len(restarts) == 2
    assert reg.values == {}


@pytest.mark.parametrize("combined", [False, True])
def test_skip_updates_transaction_and_combined_peer_restoration(monkeypatch, combined):
    receipt, reg, observed, restarts, kwargs = transaction(monkeypatch)

    def flags():
        value = reg.values.get("RmPerfLimitsOverride", {}).get("value", 0)
        skip = (value & 3) == 2
        observation = flags_observation(enabled=(value & 12) != 4, peer=int(skip))
        observation["skip_updates"] = skip
        return observation

    before = flags()
    changes = {"perf_limits.skip_updates": True}
    if combined:
        changes["perf_limits.cache"] = False
    receipt.update(
        verify_bios=False,
        before_image_sha256=None,
        after_image_sha256=None,
        plan={"patches": []},
        flag_changes=changes,
        flag_contracts=ro.registry_flags.contracts(changes, before),
        after=ro.registry_flags.compose({}, changes, before),
    )
    kwargs["gpu_factory"] = lambda slot: {"driver_flags": flags()}
    applied = ro.execute(receipt, **kwargs)
    assert applied["ok"] and set(applied["runtime_verified_fields"]) == set(changes)
    assert flags()["skip_updates"] is True
    assert reg.values["RmPerfLimitsOverride"]["value"] == (6 if combined else 2)
    restored = ro.execute(receipt, restore=True, **kwargs)
    assert restored["ok"] and reg.values == {} and flags() == before


def test_nested_cache_transactions_restore_explicit_parent_then_original_absence(monkeypatch):
    parent, registry, _observed, restarts, kwargs = transaction(monkeypatch)
    parent.update(
        verify_bios=False,
        before_image_sha256=None,
        after_image_sha256=None,
        plan={"patches": []},
        flag_changes={"perf_limits.cache": False},
        flag_contracts=ro.registry_flags.contracts({"perf_limits.cache": False}, flags_observation()),
        after={"RmPerfLimitsOverride": {"type": 4, "value": 4}},
        path="parent.json",
    )
    saved = []
    kwargs["save"] = lambda path, data: saved.append(copy.deepcopy(data))
    kwargs["gpu_factory"] = lambda slot: {
        "driver_flags": flags_observation(registry.values.get("RmPerfLimitsOverride", {}).get("value") != 4)
    }
    assert ro.execute(parent, **kwargs)["state"] == "active"
    child = copy.deepcopy(parent)
    child.update(
        id="child",
        path="child.json",
        state="prepared",
        parent="parent.json",
        events=[],
        before=copy.deepcopy(parent["after"]),
        after={"RmPerfLimitsOverride": {"type": 4, "value": 8}},
        flag_changes={"perf_limits.cache": True},
        flag_contracts=ro.registry_flags.contracts({"perf_limits.cache": True}, flags_observation(False)),
    )
    assert ro.execute(child, **kwargs)["state"] == "active"
    assert registry.values["RmPerfLimitsOverride"]["value"] == 8
    assert ro.execute(child, restore=True, **kwargs)["state"] == "restored"
    assert registry.values == parent["after"]
    assert {"receipt": "parent.json"} in saved
    assert ro.execute(parent, restore=True, **kwargs)["state"] == "restored"
    assert registry.values == {} and len(restarts) == 4
    assert {"receipt": None} in saved


def test_image_mismatch_automatically_rolls_back(monkeypatch):
    receipt, reg, observed, restarts, kwargs = transaction(monkeypatch)
    normal_restart = kwargs["restart"]

    def failed_activation(target):
        outcome = normal_restart(target)
        if reg.values:
            observed["image"] = b"abcdef"
        return outcome

    kwargs["restart"] = failed_activation
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "rolled-back"
    assert result["ok"] is False
    assert reg.values == {}
    assert len(restarts) == 2


def test_reboot_request_never_reboots_or_retries_restart(monkeypatch):
    receipt, reg, observed, restarts, kwargs = transaction(monkeypatch)
    kwargs["restart"] = lambda target: ro.devicereset.RestartResult(False, True, "reboot required", 3010)
    result = ro.execute(receipt, **kwargs)
    assert result["reboot_required"] is True
    assert result["state"] == "recovery-required"
    assert reg.values == {}
    assert len([e for e in result["events"] if e["event"] == "restart"]) == 1


def test_recovery_can_finish_after_registry_already_restored(monkeypatch):
    receipt, reg, observed, restarts, kwargs = transaction(monkeypatch)
    receipt["state"] = "recovery-required"
    assert ro.execute(receipt, restore=True, **kwargs)["state"] == "restored"
    assert reg.values == {}
    assert len(restarts) == 1


def test_concurrent_registry_change_prevents_restart(monkeypatch):
    receipt, reg, observed, restarts, kwargs = transaction(monkeypatch)
    reg.values["EnableGpuFirmware"] = {"type": 4, "value": 99}
    assert ro.execute(receipt, **kwargs)["state"] == "failed"
    assert reg.values["EnableGpuFirmware"]["value"] == 99
    assert restarts == []


def test_tampered_receipt_prevents_restart(monkeypatch):
    receipt, reg, observed, restarts, kwargs = transaction(monkeypatch)
    receipt["after"]["romOverride00"]["value"] = "00000000"
    assert ro.execute(receipt, **kwargs)["state"] == "failed"
    assert reg.values == {}
    assert restarts == []


def test_pending_pointer_precedes_first_registry_mutation(monkeypatch):
    receipt, reg, observed, restarts, kwargs = transaction(monkeypatch)
    saved = []
    kwargs["save"] = lambda path, data: saved.append((str(path), copy.deepcopy(data)))
    replace = reg.replace

    def checked(expected, desired):
        assert any(data == {"receipt": receipt["path"]} for _, data in saved)
        replace(expected, desired)

    reg.replace = checked
    assert ro.execute(receipt, **kwargs)["state"] == "active"


def test_partial_write_keeps_discoverable_recovery(monkeypatch):
    receipt, reg, observed, restarts, kwargs = transaction(monkeypatch)

    def partial(expected, desired):
        reg.values = {"romOverride00": desired["romOverride00"]}
        raise OSError("write interrupted")

    reg.replace = partial
    saved = []
    kwargs["save"] = lambda path, data: saved.append(copy.deepcopy(data))
    assert ro.execute(receipt, **kwargs)["state"] == "recovery-required"
    assert {"receipt": receipt["path"]} in saved
    assert restarts == []


def test_restore_does_not_require_live_gpu_before_registry_cleanup(monkeypatch):
    receipt, reg, observed, restarts, kwargs = transaction(monkeypatch)
    reg.values = copy.deepcopy(receipt["after"])

    def gpu(slot):
        assert reg.values == {}, "Registry must restore before querying a failed GPU"
        return SimpleNamespace()

    kwargs["gpu_factory"] = gpu
    assert ro.execute(receipt, restore=True, **kwargs)["state"] == "restored"
    assert reg.values == {}


def test_failed_restore_leaves_original_registry_in_place(monkeypatch):
    receipt, reg, observed, restarts, kwargs = transaction(monkeypatch)
    reg.values = copy.deepcopy(receipt["after"])
    kwargs["restart"] = lambda target: ro.devicereset.RestartResult(False, False, "device failed", 1)
    result = ro.execute(receipt, restore=True, **kwargs)
    assert result["state"] == "recovery-required"
    assert reg.values == {}
    assert len([e for e in result["events"] if e["event"] == "restart"]) == 1


def test_restore_handles_interrupted_partial_write(monkeypatch):
    receipt, reg, observed, restarts, kwargs = transaction(monkeypatch)
    reg.values = {"romOverride00": receipt["after"]["romOverride00"]}
    assert ro.execute(receipt, restore=True, **kwargs)["state"] == "restored"
    assert reg.values == {}


def test_journal_failure_after_write_still_restores_registry(monkeypatch):
    receipt, reg, observed, restarts, kwargs = transaction(monkeypatch)

    def save(path, data):
        if data.get("events") and any(event["event"] == "registry-verified" for event in data["events"]):
            raise OSError("disk full")

    kwargs["save"] = save
    result = ro.execute(receipt, **kwargs)
    assert reg.values == {}
    assert observed["image"] == b"abcdef"
    assert result["state"] == "rolled-back"
    assert result["journal_errors"]


def test_existing_pending_record_is_rejected():
    pending = ro.compose_records(b"abcdef", plan(), {}, 1, 0)
    with pytest.raises(ro.OverrideError, match="differ from the running image"):
        ro.validate_existing_image(pending, b"abcdef", 1, 0)
    ro.validate_existing_image(pending, b"abXYef", 1, 0)


def model(active=2000, override=0, present=False):
    return {
        "registry_name": "RMHwSpeedoOverride",
        "index": 17,
        "fuse_id": 0,
        "fuse_version_field_id": 1,
        "cache_ready": True,
        "signed": False,
        "active": active,
        "raw_hardware": 2000,
        "raw_hardware_signed": False,
        "default": 1900,
        "default_signed": False,
        "minimum": 1800,
        "maximum": 2200,
        "override": override,
        "override_present": present,
        "hardware_scale_q12": 4096,
        "hardware_offset": 0,
    }


def test_named_speedo_compose_is_bounded_and_preserves_other_values():
    original = {"EnableGpuFirmware": {"type": 4, "value": 16}}
    vfe = {"inputs": [model()]}
    desired = ro.compose_named(original, {"RMHwSpeedoOverride": 2001}, vfe)
    assert desired["EnableGpuFirmware"] == original["EnableGpuFirmware"]
    assert desired["RMHwSpeedoOverride"] == {"type": 4, "value": 2001}
    for bad in (0, 3000, 2000.5, True):
        with pytest.raises(ro.OverrideError):
            ro.compose_named(original, {"RMHwSpeedoOverride": bad}, vfe)
    with pytest.raises(ro.OverrideError):
        ro.compose_named(original, {"RmPmgrIddqNvvddOverride": 26001}, vfe)


def test_named_readback_requires_active_override_and_hardware_preservation():
    receipt = {"named_changes": {"RMHwSpeedoOverride": 2001}, "model_contracts": {"RMHwSpeedoOverride": model()}}
    snap = {"vfe": {"inputs": [model(2001, 2001, True)]}}
    assert ro.verify_named_inputs(snap, receipt) == ["RMHwSpeedoOverride"]
    snap["vfe"]["inputs"][0]["active"] = 2000
    with pytest.raises(ro.OverrideError, match="active driver readback"):
        ro.verify_named_inputs(snap, receipt)
    snap["vfe"]["inputs"] = [model()]
    assert ro.verify_named_inputs(snap, receipt, restore=True)
    snap["vfe"]["inputs"][0]["raw_hardware"] = 1999
    with pytest.raises(ro.OverrideError, match="hardware data changed"):
        ro.verify_named_inputs(snap, receipt, restore=True)


def prepare_named_fixture(monkeypatch, tmp_path, values, item=None, image=None):
    target = ro.Target("0000:08:05.1", r"PCI\VEN_10DE&DEV_ABCD\OTHER", "some-driver-key")
    registry = FakeRegistry()
    registry.values = copy.deepcopy(values)
    monkeypatch.setattr(ro, "bind_target", lambda gpu: target)
    monkeypatch.setattr(ro, "state_directory", lambda: tmp_path)
    monkeypatch.setattr(ro, "active_receipt", lambda target: None)
    monkeypatch.setattr(ro, "read_model_inputs", lambda gpu: ({"inputs": [item or model()]}, None))

    def read(gpu):
        if image is None:
            raise AssertionError("Unrelated BIOS getter must not be required")
        return image

    monkeypatch.setattr(ro, "read_vbios", read)
    monkeypatch.setattr(vbios_fields, "inspect", lambda data: {"identity": {"vbios_version_raw": 1, "oem_revision": 0}})
    return lambda: ro.prepare(SimpleNamespace(), {}, registry=registry, named_changes={"RMHwSpeedoOverride": 2001})


def test_named_only_preparation_without_bios_getter(monkeypatch, tmp_path):
    prepare = prepare_named_fixture(monkeypatch, tmp_path, {})
    receipt = prepare()
    assert receipt["verify_bios"] is False
    assert receipt["before_image_sha256"] is None


def test_iddq_preparation_saves_effective_peer_contract_without_image_getter(monkeypatch, tmp_path):
    from tests.test_model_registry import NVVDD, observation

    prepare_named_fixture(monkeypatch, tmp_path, {})
    baseline = observation(names=(NVVDD,), slot="0000:08:05.1")
    monkeypatch.setattr(ro, "read_model_inputs", lambda gpu: (baseline, None))
    wanted = baseline["inputs"][0]["active"] + 1
    receipt = ro.prepare(SimpleNamespace(), {}, registry=FakeRegistry(), named_changes={NVVDD: wanted})
    assert receipt["verify_bios"] is False
    assert receipt["after"] == {NVVDD: {"type": 4, "value": wanted}}
    ro.model_registry.validate_contracts({NVVDD: wanted}, receipt["model_peer_contracts"])
    assert receipt["model_peer_contracts"]["before"]["model_static_sha256"] == baseline["model_static_sha256"]


@pytest.mark.parametrize(
    "failure",
    [None, "before", "contract", "missing", "slot", "ignored", "rounded", "raw", "peer", "static", "control", "undo"],
)
def test_iddq_transaction_requires_effective_value_and_preserved_peers(monkeypatch, failure):
    from tests.test_model_registry import NVVDD, active_for, observation

    receipt, reg, observed, restarts, kwargs = transaction(monkeypatch)
    baseline = observation(slot=receipt["target"]["slot"])
    wanted = next(row["active"] for row in baseline["inputs"] if row["registry_name"] == NVVDD) + 1
    changes = {NVVDD: wanted}
    active = active_for(baseline, changes)
    receipt.update(
        before={},
        after=ro.compose_named({}, changes, baseline),
        verify_bios=False,
        before_image_sha256=None,
        after_image_sha256=None,
        plan={"patches": []},
        named_changes=changes,
        model_contracts={NVVDD: ro.model_input(baseline, NVVDD)},
        model_peer_contracts=ro.model_registry.contracts(baseline, changes),
    )
    observed["vfe"] = copy.deepcopy(baseline)
    if failure == "before":
        observed["vfe"]["model_static_sha256"] = "f" * 64
    if failure == "contract":
        receipt["model_peer_contracts"] = {}
    if failure == "missing":
        del receipt["model_peer_contracts"]
    if failure == "slot":
        for phase in ("before", "after"):
            receipt["model_peer_contracts"][phase]["slot"] = "0000:7e:1d.6"
    kwargs["gpu_factory"] = lambda slot: observed

    def restart(target):
        restarts.append(target)
        current = copy.deepcopy(active if reg.values else baseline)
        if reg.values:
            selected = ro.model_input(current, NVVDD)
            if failure == "ignored":
                current = copy.deepcopy(baseline)
            if failure == "rounded":
                selected["active"] += 1
            if failure == "raw":
                selected["raw_hardware"] += 1
            if failure == "peer":
                next(row for row in current["inputs"] if row["registry_name"] != NVVDD)["active"] += 1
            if failure in ("static", "control"):
                current["model_static_sha256" if failure == "static" else "control_sha256"] = "f" * 64
        elif failure == "undo":
            current["model_static_sha256"] = "f" * 64
        observed["vfe"] = current
        return ro.devicereset.RestartResult(True, False, "restarted", 0)

    kwargs["restart"] = restart
    result = ro.execute(receipt, **kwargs)
    if failure in ("before", "contract", "missing", "slot"):
        assert result["state"] == "failed" and not reg.operations and not restarts
    elif failure in ("ignored", "rounded", "raw", "peer", "static", "control"):
        assert result["state"] == "rolled-back" and not reg.values and len(restarts) == 2
    else:
        assert result["state"] == "active" and result["runtime_verified_fields"] == [NVVDD]
        kwargs["gpu_factory"] = lambda slot: observed if not reg.values else pytest.fail("Premature recovery GET")
        result = ro.execute(receipt, restore=True, **kwargs)
        assert result["state"] == ("recovery-required" if failure == "undo" else "restored")
        assert not reg.values


@pytest.mark.parametrize("missing", [False, True])
def test_iddq_recovery_rejects_missing_peer_contract_before_get_or_write(monkeypatch, missing):
    from tests.test_model_registry import NVVDD

    receipt, reg, observed, restarts, kwargs = transaction(monkeypatch)
    receipt["named_changes"] = {NVVDD: 12345}
    if not missing:
        receipt["model_peer_contracts"] = {}
    reg.values = copy.deepcopy(receipt["after"])
    kwargs["gpu_factory"] = lambda slot: pytest.fail("Malformed recovery queried GPU")
    assert ro.execute(receipt, restore=True, **kwargs)["state"] == "failed"
    assert not reg.operations and not restarts and reg.values == receipt["after"]


def test_pending_iddq_value_prevents_restart_but_unrelated_absence_does_not():
    from tests.test_model_registry import NVVDD, observation

    vfe = observation(names=(NVVDD,))
    pending = {NVVDD: {"type": 4, "value": vfe["inputs"][0]["active"] + 1}}
    with pytest.raises(ro.OverrideError, match="reconcile pending"):
        ro.validate_existing_model_inputs(pending, vfe)
    vfe["inputs"][0]["cache_ready"] = False
    ro.validate_existing_model_inputs({}, vfe)


def test_pending_named_value_prevents_preparation(monkeypatch, tmp_path):
    prepare = prepare_named_fixture(monkeypatch, tmp_path, {"RMHwSpeedoOverride": {"type": 4, "value": 2002}})
    with pytest.raises(ro.OverrideError, match="reconcile pending"):
        prepare()
    assert not list(tmp_path.iterdir())


def test_pending_bios_value_prevents_named_only_preparation(monkeypatch, tmp_path):
    values = ro.compose_records(b"abcdef", plan(), {}, 1, 0)
    prepare = prepare_named_fixture(monkeypatch, tmp_path, values, image=b"abcdef")
    with pytest.raises(ro.OverrideError, match="reconcile pending"):
        prepare()


def test_loaded_bios_records_are_verified_during_named_transaction(monkeypatch, tmp_path):
    values = ro.compose_records(b"abcdef", plan(), {}, 1, 0)
    prepare = prepare_named_fixture(monkeypatch, tmp_path, values, image=b"abXYef")
    receipt = prepare()
    assert receipt["verify_bios"] is True
    assert receipt["before_image_sha256"] == receipt["after_image_sha256"] == digest(b"abXYef")
    assert receipt["after"]["romOverride00"] == values["romOverride00"]


@pytest.mark.parametrize("failure", [None, "apply", "before", "rollback"])
def test_clock_range_contract_covers_apply_preflight_and_undo(monkeypatch, failure):
    receipt, reg, observed, restarts, kwargs = transaction(monkeypatch)
    receipt["clock_contracts"] = {
        "GPC": {
            "identity": {"domain": 0, "kind": 1, "flags": 1},
            "before": {"minimum": -500000, "maximum": 1000000},
            "after": {"minimum": -500000, "maximum": 1001000},
            "keys": ["clock.0.maximum"],
        }
    }

    def gpu(slot):
        maximum = 1001000 if reg.values else 1000000
        if failure in ("apply", "rollback") and reg.values:
            maximum = 1000000
        if failure == "before" or failure == "rollback" and len(restarts) > 1:
            maximum = 999000
        return {"clock_ranges": {"GPC": {"domain": 0, "kind": 1, "flags": 1, "minimum": -500000, "maximum": maximum}}}

    kwargs["gpu_factory"] = gpu
    result = ro.execute(receipt, **kwargs)
    if failure is None:
        assert result["state"] == "active"
        assert result["runtime_verified_fields"] == ["clock.0.maximum"]
        assert ro.execute(receipt, restore=True, **kwargs)["state"] == "restored"
    elif failure == "before":
        assert result["state"] == "failed" and not restarts
    elif failure == "apply":
        assert result["state"] == "rolled-back" and len(restarts) == 2
    else:
        assert result["state"] == "recovery-required" and len(restarts) == 2
    assert reg.values == {}


@pytest.mark.parametrize("failure", [None, "apply", "before", "peer", "rollback", "unavailable"])
def test_thermal_contract_covers_preflight_activation_peers_and_undo(monkeypatch, failure):
    receipt, reg, observed, restarts, kwargs = transaction(monkeypatch)
    receipt["thermal_contracts"] = {
        "thermal.operating.maximum": {
            "before": 86,
            "after": 85,
            "unchanged": {"shutdown": 97, "slowdown": 94},
        }
    }

    def gpu(slot):
        maximum = 85 if reg.values else 86
        if failure in ("apply", "rollback") and reg.values:
            maximum = 86
        if failure == "before" or failure == "rollback" and len(restarts) > 1:
            maximum = 84
        shutdown = 98 if failure == "peer" and reg.values else 97
        rows = {
            "shutdown": {"status": 0, "degrees_c": shutdown},
            "slowdown": {"status": 0, "degrees_c": 94},
            "gpu_max": {"status": 0, "degrees_c": maximum},
            "memory_max": {"status": 3, "degrees_c": None},
        }
        if failure == "unavailable" and reg.values:
            rows["gpu_max"] = {"status": 999, "degrees_c": None}
        return {"thermal_thresholds": rows}

    kwargs["gpu_factory"] = gpu
    result = ro.execute(receipt, **kwargs)
    if failure is None:
        assert result["state"] == "active"
        assert result["runtime_verified_fields"] == ["thermal.operating.maximum"]
        assert ro.execute(receipt, restore=True, **kwargs)["state"] == "restored"
    elif failure == "before":
        assert result["state"] == "failed" and not restarts
    elif failure == "rollback":
        assert result["state"] == "recovery-required" and len(restarts) == 2
    else:
        assert result["state"] == "rolled-back" and len(restarts) == 2
    assert reg.values == {}


@pytest.mark.parametrize("failure", [None, "before", "minimum", "default", "maximum", "rollback"])
def test_coupled_current_contract_covers_preflight_all_bounds_and_undo(monkeypatch, failure):
    receipt, reg, observed, restarts, kwargs = transaction(monkeypatch)
    key = "power.27.default"
    receipt["changes"] = {key: 301000}
    receipt["policy_contracts"] = {
        key: {
            "schema": 2,
            "policy": 27,
            "mode": "rated-maximum",
            "type": 18,
            "channel": 9,
            "unit": "mA",
            "before": 300000,
            "architecture": {"generation": 10, "architecture": 0x1B0, "implementation": 5},
            "before_bounds": {"minimum": 0, "default": 300000, "maximum": 300000},
            "after_bounds": {"minimum": 0, "default": 301000, "maximum": 301000},
            "rom_before_bounds": {"minimum": 0, "default": 300000, "maximum": 5001000},
            "rom_after_bounds": {"minimum": 0, "default": 301000, "maximum": 5001000},
        }
    }

    def gpu(slot):
        value = 301000 if reg.values else 300000
        row = {
            "policy": 27,
            "consistent": True,
            "type": 18,
            "channel": 9,
            "unit": "mA",
            "minimum": 0,
            "default": value,
            "maximum": value,
        }
        if failure == "before" or failure == "rollback" and len(restarts) > 1:
            row["minimum"] = 1
        if reg.values:
            if failure == "minimum":
                row["minimum"] = 1
            elif failure == "default":
                row["default"] = 300000
            elif failure in ("maximum", "rollback"):
                row["maximum"] = 5001000
        return {"policies": [row]}

    kwargs["gpu_factory"] = gpu
    result = ro.execute(receipt, **kwargs)
    if failure is None:
        assert result["state"] == "active"
        assert result["runtime_verified_fields"] == [key]
        assert ro.execute(receipt, restore=True, **kwargs)["state"] == "restored"
    elif failure == "before":
        assert result["state"] == "failed" and not restarts
    elif failure == "rollback":
        assert result["state"] == "recovery-required" and len(restarts) == 2
    else:
        assert result["state"] == "rolled-back" and len(restarts) == 2
    assert reg.values == {}


@pytest.mark.parametrize("failure", [None, "before", "apply", "peer", "rollback"])
@pytest.mark.parametrize("role", ["level_minimum", "pwm_hz"])
def test_fan_contract_checks_raw_bounds_before_apply_and_during_undo(monkeypatch, failure, role):
    receipt, reg, observed, restarts, kwargs = transaction(monkeypatch)
    fr = ro.fan_runtime
    info, control = bytearray(fr.INFO_BYTES), bytearray(fr.CONTROL_BYTES)
    mask = (1 << 1) | (1 << 4)
    struct.pack_into("<I", info, 4, mask)
    struct.pack_into("<I", info, 12, mask)
    struct.pack_into("<I", control, 4, mask)
    info[16:20] = bytes((4, 1, 255, 255))
    for index in (1, 4):
        a, b = fr.INFO_OFFSET + index * fr.INFO_STRIDE, fr.CONTROL_OFFSET + index * fr.CONTROL_STRIDE
        info[a] = control[b] = 3
        info[a + 14] = 2
        struct.pack_into("<I", info, a + 24, 25000)
        struct.pack_into("<II", control, b + 8, 500, 3100)
        struct.pack_into("<II", control, b + 24, 19661, 65536)
    baseline = fr.decode(bytes(info), bytes(control))
    baseline.update(
        slot="0000:08:05.1",
        architecture=8,
        stable_reads=2,
        identity={"slot": "0000:08:05.1", "architecture": 8, "device_id": 0xABCD, "subsystem_id": 0x12345678},
    )
    fields = [
        {"key": f"fan.4.{role}", "units": unit, "current": value, "client_id_to_index": baseline["client_id_to_index"]}
        for role, unit, value in (
            ("level_minimum", "%", 30),
            ("level_maximum", "%", 100),
            ("rpm_minimum", "RPM", 500),
            ("rpm_maximum", "RPM", 3100),
            ("pwm_hz", "Hz", 25000),
        )
    ]
    monkeypatch.setattr(fr, "read", lambda gpu: (baseline, None))
    selected_key = f"fan.4.{role}"
    new_value = 25001 if role == "pwm_hz" else 31
    active_value, initial_value = (25001, 25000) if role == "pwm_hz" else (20316, 19661)
    member = "pwm_frequency_hz" if role == "pwm_hz" else "duty_minimum_q16"
    receipt["fan_contracts"] = fr.contracts(SimpleNamespace(), {"fields": fields}, {selected_key: new_value})

    def gpu(slot):
        fan = copy.deepcopy(baseline)
        row = next(row for row in fan["entries"] if row["index"] == 4)
        row[member] = active_value if reg.values else initial_value
        if failure in ("apply", "rollback") and reg.values:
            row[member] = initial_value
        if failure == "before" or failure == "rollback" and len(restarts) > 1:
            row[member] = initial_value + 1
        if failure == "peer" and reg.values:
            fan["entries"][0]["rpm_maximum"] = 3200
        return {"fan_bounds": fan}

    kwargs["gpu_factory"] = gpu
    result = ro.execute(receipt, **kwargs)
    if failure is None:
        assert result["state"] == "active"
        assert result["runtime_verified_fields"] == [selected_key]
        assert ro.execute(receipt, restore=True, **kwargs)["state"] == "restored"
    elif failure == "before":
        assert result["state"] == "failed" and not restarts
    elif failure == "rollback":
        assert result["state"] == "recovery-required" and len(restarts) == 2
    else:
        assert result["state"] == "rolled-back" and len(restarts) == 2
    assert reg.values == {}
