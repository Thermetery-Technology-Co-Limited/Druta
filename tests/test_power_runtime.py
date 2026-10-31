"""Power policy projections, independent of local card/driver identities."""

import copy
import ctypes
import json
from types import SimpleNamespace

import pytest

from druta import power_runtime as pr
from druta.nvbackend import NvAPI, _GpuArchInfo
from druta.vbios_fields import POWER_V40_RUNTIME_TYPES


def row(index=5, kind=7, channel=4, unit="mA", bounds=(7000, 99000, 99000)):
    return {
        "policy": index,
        "type": POWER_V40_RUNTIME_TYPES[kind],
        "channel": channel,
        "unit": unit,
        "minimum": bounds[0],
        "default": bounds[1],
        "maximum": bounds[2],
        "consistent": True,
        "requested": bounds[1],
        "value": 1300,
        "limit": bounds[1],
    }


def inspection(index=5, kind=7, channel=4, unit="mA", bounds=(7000, 99000, 500000)):
    return {
        "tables": [{"name": "power", "version": 0x40, "header_size": 0x38, "stride": 0x66, "count": 32}],
        "identity": {"pci_device": 0xDEAD, "vbios_version_raw": 0x12345678},
        "fields": [
            {
                "key": f"power.{index}.{bound}",
                "policy": index,
                "policy_type": kind,
                "channel": channel,
                "units": unit,
                "current": value,
                "runtime_binding": {
                    "policy_index": index,
                    "vbios_type": kind,
                    "channel": channel,
                    "units": unit,
                    "runtime_type": POWER_V40_RUNTIME_TYPES[kind],
                },
            }
            for bound, value in zip(("minimum", "default", "maximum"), bounds, strict=True)
        ],
    }


class GPU:
    def __init__(self, rows=None, generation=10, architecture=0x1B0, implementation=5):
        self.rows = rows or [row()]
        self.generation = generation
        self.architecture = architecture
        self.implementation = implementation
        self.pairing_error = None
        self.error = None
        self.calls = []
        self.responses = []
        self.nvapi = SimpleNamespace(ok=True, gpu=1234, GetArchInfo=self.get_arch)

    def read_power_policies(self):
        return copy.deepcopy(self.rows), self.error

    def arch(self):
        return self.generation

    def get_arch(self, handle, pointer):
        assert handle == 1234
        value = ctypes.cast(pointer, ctypes.POINTER(_GpuArchInfo)).contents
        self.calls.append(value.version)
        status = self.responses.pop(0) if self.responses else 0
        if isinstance(status, Exception):
            raise status
        if status:
            return status
        value.architecture = self.architecture
        value.implementation = self.implementation
        return 0


def snapshot(rows):
    return {"policies": copy.deepcopy(rows)}


@pytest.mark.parametrize(
    "index,impl,kind,unit", [(3, 2, 6, "mW"), (5, 3, 7, "mA"), (17, 5, 7, "mA"), (21, 6, 6, "mW"), (30, 7, 7, "mA")]
)
def test_rated_projection_on_all_traced_architectures_and_different_rows(index, impl, kind, unit):
    gpu = GPU([row(index, kind, unit=unit)], implementation=impl)
    change = {f"power.{index}.default": 101000}
    contract = pr.contracts(gpu, inspection(index, kind, unit=unit), change)
    saved = json.loads(json.dumps(contract))
    item = saved[next(iter(change))]
    assert item["mode"] == "rated-maximum"
    assert item["before"] == 99000
    assert item["after_bounds"] == {"minimum": 7000, "default": 101000, "maximum": 101000}
    assert item["rom_after_bounds"]["maximum"] == 500000
    assert pr.verify(snapshot(gpu.rows), change, saved, restore=True) == list(change)
    applied = snapshot([row(index, kind, unit=unit, bounds=(7000, 101000, 101000))])
    assert pr.verify(applied, change, saved) == list(change)


def test_combined_minimum_and_rated_edits_verify_complete_projection():
    gpu = GPU()
    changes = {"power.5.minimum": 8000, "power.5.default": 101000}
    saved = pr.contracts(gpu, inspection(), changes)
    assert pr.verify(snapshot([row(bounds=(8000, 101000, 101000))]), changes, saved) == list(changes)
    assert pr.verify(snapshot(gpu.rows), changes, saved, restore=True) == list(changes)


@pytest.mark.parametrize(
    "changes", [{"power.5.maximum": 600000}, {"power.5.maximum": 600000, "power.5.default": 101000}]
)
def test_discarded_maximum_is_rejected_with_actionable_rated_field(changes):
    with pytest.raises(pr.PowerRuntimeError, match=r"edit power\.5\.default"):
        pr.contracts(GPU(), inspection(), changes)


def test_equal_rom_rated_and_maximum_does_not_select_native_mode():
    original = inspection(bounds=(7000, 99000, 99000))
    saved = pr.contracts(GPU(), original, {"power.5.default": 98000})
    assert saved["power.5.default"]["mode"] == "rated-maximum"
    assert saved["power.5.default"]["after_bounds"]["maximum"] == 98000


@pytest.mark.parametrize(
    "generation,architecture,implementation",
    [(8, 0x190, 2), (7, 0x170, 2), (10, 0x1A0, 0), (10, 0x1B0, 11), (10, 0x1B0, 12)],
)
def test_other_architectures_keep_native_semantics_only_with_all_bounds_equal(generation, architecture, implementation):
    gpu = GPU([row(bounds=(7000, 99000, 500000))], generation, architecture, implementation)
    change = {"power.5.default": 101000}
    saved = pr.contracts(gpu, inspection(), change)
    assert saved["power.5.default"]["mode"] == "native"
    assert saved["power.5.default"]["after_bounds"]["maximum"] == 500000
    gpu.rows[0]["maximum"] = 99000
    with pytest.raises(pr.PowerRuntimeError, match="outside the understood mapping"):
        pr.contracts(gpu, inspection(), change)


@pytest.mark.parametrize("kind", [1, 2, 3, 9, 11, 12, 13])
def test_unrelated_classes_remain_native_and_do_not_need_implementation_query(kind):
    gpu = GPU([row(kind=kind, bounds=(7000, 99000, 500000))], generation=None)
    gpu.nvapi = None
    changes = {"power.5.maximum": 600000}
    saved = pr.contracts(gpu, inspection(kind=kind), changes)
    assert saved["power.5.maximum"]["mode"] == "native"
    assert not gpu.calls
    assert pr.verify(snapshot([row(kind=kind, bounds=(7000, 99000, 600000))]), changes, saved) == list(changes)


@pytest.mark.parametrize("role", ["minimum", "default", "maximum"])
def test_every_before_bound_is_required_even_for_another_selected_field(role):
    gpu = GPU()
    gpu.rows[0][role] += 1
    with pytest.raises(pr.PowerRuntimeError):
        pr.contracts(gpu, inspection(), {"power.5.minimum": 8000})


@pytest.mark.parametrize("role", ["minimum", "default", "maximum"])
@pytest.mark.parametrize("restore", [False, True])
def test_every_bound_is_checked_after_apply_or_restore(role, restore):
    gpu = GPU()
    changes = {"power.5.default": 101000}
    saved = pr.contracts(gpu, inspection(), changes)
    observed = row(bounds=(7000, 99000, 99000) if restore else (7000, 101000, 101000))
    observed[role] += 1
    with pytest.raises(pr.PowerRuntimeError):
        pr.verify(snapshot([observed]), changes, saved, restore=restore)


@pytest.mark.parametrize("identity,value", [("type", 17), ("channel", 7), ("unit", "mW"), ("consistent", False)])
def test_row_identity_change_is_rejected(identity, value):
    gpu = GPU()
    change = {"power.5.default": 101000}
    saved = pr.contracts(gpu, inspection(), change)
    applied = row(bounds=(7000, 101000, 101000))
    applied[identity] = value
    with pytest.raises(pr.PowerRuntimeError):
        pr.verify(snapshot([applied]), change, saved)


@pytest.mark.parametrize("change", [0, -1, True, 1.2, 0x80000000, 600000])
def test_invalid_new_ranges_are_rejected(change):
    with pytest.raises(pr.PowerRuntimeError):
        pr.contracts(GPU(), inspection(), {"power.5.default": change})


@pytest.mark.parametrize(
    "mutation",
    [
        "version",
        "stride",
        "header",
        "count",
        "duplicate",
        "kind",
        "runtime_class",
        "binding_index",
        "binding_channel",
        "binding_unit",
    ],
)
def test_transformation_requires_understood_table_and_authoritative_binding(mutation):
    original = inspection()
    if mutation in {"version", "stride", "header", "count"}:
        name = {"header": "header_size"}.get(mutation, mutation)
        original["tables"][0][name] = 3
    elif mutation == "duplicate":
        original["tables"].append(copy.deepcopy(original["tables"][0]))
    else:
        field = original["fields"][0]
        if mutation == "kind":
            field["policy_type"] = 6
        else:
            name, value = {
                "runtime_class": ("runtime_type", 17),
                "binding_index": ("policy_index", 1),
                "binding_channel": ("channel", 99),
                "binding_unit": ("units", "mW"),
            }[mutation]
            field["runtime_binding"][name] = value
    with pytest.raises(pr.PowerRuntimeError):
        pr.contracts(GPU(), original, {"power.5.default": 101000})


def test_alternate_recognized_header_is_accepted():
    original = inspection()
    original["tables"][0]["header_size"] = 0x3D
    assert pr.contracts(GPU(), original, {"power.5.default": 101000})


def test_nvapi_version_fallback_and_bounded_transient_retry():
    gpu = GPU()
    gpu.responses = [-9, -1, -9, 0]
    assert pr.contracts(gpu, inspection(), {"power.5.default": 101000})
    assert gpu.calls == [NvAPI.ver(_GpuArchInfo, version) for version in (2, 1, 2, 1)]


def test_nvapi_exception_is_retryable_but_failed_attempts_are_bounded():
    gpu = GPU()
    gpu.responses = [RuntimeError("first"), 0]
    assert pr.contracts(gpu, inspection(), {"power.5.default": 101000})
    gpu.responses = [RuntimeError("first"), RuntimeError("second")]
    with pytest.raises(pr.PowerRuntimeError, match="refresh and retry"):
        pr.contracts(gpu, inspection(), {"power.5.default": 101000})


@pytest.mark.parametrize("missing", ["generation", "api", "export", "paired", "read", "version"])
def test_unavailable_capability_never_guesses_transformation(missing):
    gpu = GPU()
    if missing == "generation":
        gpu.generation = None
    elif missing == "api":
        gpu.nvapi.ok = False
    elif missing == "export":
        gpu.nvapi.GetArchInfo = None
    elif missing == "paired":
        gpu.pairing_error = "wrong GPU"
    elif missing == "read":
        gpu.error = "transient"
    else:

        def wrong_version(handle, pointer):
            info = ctypes.cast(pointer, ctypes.POINTER(_GpuArchInfo)).contents
            info.version = 0
            info.architecture = 0x1B0
            return 0

        gpu.nvapi.GetArchInfo = wrong_version
    with pytest.raises(pr.PowerRuntimeError):
        pr.contracts(gpu, inspection(), {"power.5.default": 101000})


@pytest.mark.parametrize("mutation", ["bound", "identity", "absent", "inconsistent"])
def test_unselected_known_policy_metadata_is_preserved(mutation):
    peer = row(index=23, kind=11, channel=16, bounds=(1000, 19000, 26000))
    gpu = GPU([row(), peer])
    changes = {"power.5.default": 101000}
    saved = pr.contracts(gpu, inspection(), changes)
    observed = snapshot([row(bounds=(7000, 101000, 101000)), peer])
    assert pr.verify(observed, changes, saved) == list(changes)
    if mutation == "bound":
        observed["policies"][1]["maximum"] += 1
    elif mutation == "identity":
        observed["policies"][1]["channel"] += 1
    elif mutation == "inconsistent":
        observed["policies"][1]["consistent"] = False
    else:
        observed["policies"].pop()
    with pytest.raises(pr.PowerRuntimeError, match="unselected policy"):
        pr.verify(observed, changes, saved)


def test_unknown_unrelated_policy_and_telemetry_do_not_block_supported_control():
    unknown = {"policy": 31, "type": 250, "unit": None, "consistent": False}
    gpu = GPU([row(), unknown])
    change = {"power.5.default": 101000}
    saved = pr.contracts(gpu, inspection(), change)
    applied = row(bounds=(7000, 101000, 101000))
    applied.update(value=1876, limit=100001)
    assert pr.verify(snapshot([applied]), change, saved) == list(change)


def test_multiple_selected_policies_are_not_mistaken_for_preserved_peers():
    gpu = GPU([row(), row(index=21, kind=6, unit="mW")])
    original = inspection()
    original["fields"] += inspection(index=21, kind=6, unit="mW")["fields"]
    changes = {"power.5.default": 101000, "power.21.minimum": 8000}
    saved = pr.contracts(gpu, original, changes)
    observed = snapshot([row(bounds=(7000, 101000, 101000)), row(21, kind=6, unit="mW", bounds=(8000, 99000, 99000))])
    assert pr.verify(observed, changes, saved) == list(changes)


def test_duplicate_indices_and_stale_selected_values_are_rejected():
    gpu = GPU([row(), row()])
    with pytest.raises(pr.PowerRuntimeError, match="duplicate"):
        pr.contracts(gpu, inspection(), {"power.5.default": 101000})
    saved = pr.contracts(GPU(), inspection(), {"power.5.default": 101000})
    with pytest.raises(pr.PowerRuntimeError, match="requested value differs"):
        pr.verify(snapshot([row(bounds=(7000, 101000, 101000))]), {"power.5.default": 102000}, saved)


def test_legacy_receipts_and_no_contract_remain_readable():
    change = {"power.2.default": 88000}
    legacy = {"power.2.default": {"type": 0, "channel": 7, "unit": "mW", "before": 77000}}
    snap = {"policies": [{"policy": 2, "consistent": True, "type": 0, "channel": 7, "unit": "mW", "default": 88000}]}
    assert pr.verify(snap, change, legacy) == list(change)
    snap["policies"][0]["default"] = 77000
    assert pr.verify(snap, change, legacy, restore=True) == list(change)
    assert pr.verify(snap, {"power.2.default": 77000}) == list(change)
    with pytest.raises(pr.PowerRuntimeError, match="runtime policy reports"):
        pr.verify(snap, change, legacy)


def test_no_power_fields_does_not_require_power_or_architecture():
    assert pr.contracts(SimpleNamespace(), {}, {"clock.2.maximum": 5}) == {}
    assert pr.verify({}, {"clock.2.maximum": 5}) == []


@pytest.mark.parametrize("header,stride", [(0x2C, 0x47), (0x2C, 0x4B), (0x32, 0x53), (0x34, 0x5B)])
def test_v30_class1_persistent_contract_requires_exact_runtime_match(header, stride):
    from druta import vbios_fields
    from tests.test_vbios_fields import v30_power_fixture

    parsed = vbios_fields.inspect(v30_power_fixture(header, stride))
    policy = {
        "policy": 0,
        "type": 0,
        "channel": 5,
        "unit": "mW",
        "minimum": 100000,
        "default": 310000,
        "maximum": 380000,
        "consistent": True,
    }
    gpu = GPU([policy])
    changes = {"power.0.maximum": 390000}
    saved = pr.contracts(gpu, parsed, changes)
    assert saved["power.0.maximum"]["after_bounds"]["maximum"] == 390000
    assert pr.verify(snapshot([{**policy, "maximum": 390000}]), changes, saved) == list(changes)
    assert pr.verify(snapshot([policy]), changes, saved, restore=True) == list(changes)
    for name, value in (("type", 1), ("channel", 6), ("unit", "mA"), ("minimum", 100001)):
        altered = {**policy, name: value}
        with pytest.raises(pr.PowerRuntimeError):
            pr.contracts(GPU([altered]), parsed, changes)
    unknown = vbios_fields.inspect(v30_power_fixture(header, stride, extension=1))
    with pytest.raises(pr.PowerRuntimeError):
        pr.contracts(gpu, unknown, changes)


def test_short_v30_nonzero_policy_current_contract():
    from druta import vbios_fields
    from tests.test_vbios_fields import v30_power_fixture

    parsed = vbios_fields.inspect(v30_power_fixture(stride=0x47, device=0xBEEF, count=3, channel=7, unit=1))
    policy = {"policy": 2, "type": 0, "channel": 7, "unit": "mA", "minimum": 100000,
              "default": 310000, "maximum": 380000, "consistent": True}
    changes = {"power.2.maximum": 400000}
    saved = pr.contracts(GPU([policy]), parsed, changes)
    assert pr.verify(snapshot([{**policy, "maximum": 400000}]), changes, saved) == list(changes)
    with pytest.raises(pr.PowerRuntimeError):
        pr.contracts(GPU([{**policy, "channel": 5}]), parsed, changes)
