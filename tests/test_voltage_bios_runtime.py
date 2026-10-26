"""Constant BIOS VMIN must match independent effective rail policy exactly."""

import copy
import json
import threading
from types import SimpleNamespace

import pytest

from druta import voltage_bios_runtime as vr


class GPU:
    def __init__(self, architecture=8):
        self.architecture = architecture
        self.address = "0000:72:1d.3"
        self._lock = threading.RLock()
        self.pairing_error = ""
        self.nvapi = SimpleNamespace(
            ok=True, gpu=123, selected={"slot": self.address, "handle": 123, "devid": 0xABCD, "subsys": 0x12345678}
        )
        self.nvml = SimpleNamespace(selected=dict(self.nvapi.selected))
        self.state = {
            r: {
                "type": t,
                "live": 812.5,
                "reliability": 1127.5,
                "alt_reliability": 1150,
                "overvoltage": 1250,
                "effective": 1127.5,
                "vmin": 787.5,
                "_boost_mv": 0,
            }
            for r, t in ((0, 1), (1, 3))
        }
        self.deltas = {
            r: {"type": 3, "reliability": -12.5, "alt_reliability": 0, "overvoltage": 0, "vmin": 0} for r in (0, 1)
        }
        self.reads = self.refreshes = 0
        self.actions = []

    def slot(self):
        return self.address

    def arch(self):
        return self.architecture

    def read_volt_rail_state(self):
        self.reads += 1
        if self.actions:
            self.actions.pop(0)(self)
        return copy.deepcopy(self.state)

    def _read_volt_rail_deltas(self):
        return copy.deepcopy(self.deltas)

    def _refresh_volt_rail_capabilities(self):
        self.refreshes += 1


def inspection():
    return {
        "fields": [
            {
                "key": f"voltage.{i}.VMIN",
                "current": 787500,
                "rail": name,
                "role": "VMIN",
                "equation_type": 1,
                "shared": False,
                "units": "uV",
                "capability": "editable",
            }
            for i, name in ((0, "NVVDD"), (1, "MSVDD"))
        ]
    }


def test_exact_units_and_independent_rails():
    gpu = GPU()
    observation, error = vr.read(gpu)
    assert error is None
    assert observation["rails"]["0"]["policy_uv"]["vmin"] == 787500
    assert observation["rails"]["1"]["offsets_uv"]["reliability"] == -12500
    assert gpu.reads == 2
    gpu.state.pop(1)
    observation, error = vr.read(gpu)
    assert error is None and set(observation["rails"]) == {"0"}


@pytest.mark.parametrize("arch", [5, 6, 8, 9, 10])
def test_alternate_architectures_and_identity_are_not_local_allowlists(arch):
    observation, error = vr.read(GPU(arch))
    assert error is None and observation["identity"]["device_id"] == 0xABCD


@pytest.mark.parametrize("key,value", [("type", True), ("vmin", float("nan")), ("effective", 0), ("vmin", 787.5001)])
def test_malformed_peer_keeps_valid_peer(key, value):
    gpu = GPU()
    gpu.state[1][key] = value
    observed, error = vr.read(gpu)
    assert error is None and set(observed["rails"]) == {"0"}


def test_live_variation_does_not_invalidate_policy():
    gpu = GPU()
    gpu.actions = [lambda g: g.state[0].update(live=800), lambda g: g.state[0].update(live=900)]
    assert vr.read(gpu)[1] is None


def test_one_transient_failure_has_bounded_fresh_retry():
    gpu = GPU()

    def fail(g):
        raise RuntimeError("temporary getter failure")

    gpu.actions = [fail]
    assert vr.read(gpu)[1] is None
    assert gpu.refreshes == 1 and gpu.reads == 3
    gpu.actions = [fail, fail]
    assert vr.read(gpu)[0] is None
    assert gpu.refreshes == 2


def test_changed_identity_is_never_accepted_on_retry():
    gpu = GPU()

    def change(g):
        g.nvapi.selected["devid"] = 0x1234
        g.nvml.selected["devid"] = 0x1234

    gpu.actions = [change]
    assert vr.read(gpu)[0] is None
    assert gpu.reads == 1


@pytest.mark.parametrize("selected", [(0,), (1,), (0, 1)])
def test_contracts_preserve_every_peer_and_support_combined_undo(selected):
    gpu = GPU()
    changes = {f"voltage.{i}.VMIN": 800000 for i in selected}
    contract = vr.contracts(gpu, inspection(), changes)
    vr.validate_contracts(changes, json.loads(json.dumps(contract)))
    assert vr.verify(contract["after"], contract) == list(changes)
    assert vr.verify(contract["before"], contract, restore=True) == list(changes)
    bad = copy.deepcopy(contract["after"])
    bad["rails"]["1"]["policy_uv"]["overvoltage"] += 1
    with pytest.raises(vr.VoltageBiosError, match="preserved"):
        vr.verify(bad, contract)


def test_retained_vmin_offset_is_accounted_for_without_rewriting():
    gpu = GPU()
    gpu.state[0]["vmin"] = 800
    gpu.deltas[0]["vmin"] = 12.5
    c = vr.contracts(gpu, inspection(), {"voltage.0.VMIN": 800000})
    assert c["after"]["rails"]["0"]["policy_uv"]["vmin"] == 812500
    assert c["after"]["rails"]["0"]["offsets_uv"]["vmin"] == 12500
    vr.validate_contracts({"voltage.0.VMIN": 800000}, c)


def test_rounding_is_not_silently_accepted():
    gpu = GPU()
    c = vr.contracts(gpu, inspection(), {"voltage.0.VMIN": 788500})
    rounded = copy.deepcopy(c["after"])
    rounded["rails"]["0"]["policy_uv"]["vmin"] = 793750
    with pytest.raises(vr.VoltageBiosError, match="exact request"):
        vr.verify(rounded, c)


@pytest.mark.parametrize(
    "key,value",
    [
        ("rail", "unknown"),
        ("shared", True),
        ("units", "mV"),
        ("current", 780000),
        ("equation_type", 7),
        ("capability", "read-only"),
    ],
)
def test_refuses_unbound_equation(key, value):
    fields = inspection()
    fields["fields"][0][key] = value
    with pytest.raises(vr.VoltageBiosError):
        vr.contracts(GPU(), fields, {"voltage.0.VMIN": 800000})


@pytest.mark.parametrize("value", [True, float("nan"), 800000.5, 1200000, -1])
def test_refuses_nonrepresentable_or_crossed_vmin(value):
    with pytest.raises(vr.VoltageBiosError):
        vr.contracts(GPU(), inspection(), {"voltage.0.VMIN": value})


def test_separate_upper_equation_edits():
    with pytest.raises(vr.VoltageBiosError, match="separate"):
        vr.contracts(GPU(), inspection(), {"voltage.0.VMIN": 800000, "voltage.1.REL.maximum": 1200000})
    assert vr.contracts(None, None, {"unrelated": 1}) == {}
    assert vr.verify(None, {}) == []
    vr.validate_contracts({}, {})


@pytest.mark.parametrize(
    "mutation", ["empty", "keys", "after", "before", "identity", "schema", "peer", "bool", "selected"]
)
def test_malformed_contract_rejected_without_gpu(mutation):
    changes = {"voltage.0.VMIN": 800000}
    c = vr.contracts(GPU(), inspection(), changes)
    if mutation == "empty":
        c = {}
    elif mutation == "keys":
        c["extra"] = True
    elif mutation == "after":
        c["after"]["rails"]["0"]["policy_uv"]["vmin"] += 1
    elif mutation == "before":
        c["selected"]["voltage.0.VMIN"]["before_bios_uv"] += 1
    elif mutation == "identity":
        c["before"]["identity"]["device_id"] = True
    elif mutation == "schema":
        c["schema"] = True
    elif mutation == "peer":
        c["after"]["rails"]["1"]["policy_uv"]["vmin"] += 1
    elif mutation == "bool":
        c["before"]["rails"]["0"]["policy_uv"]["vmin"] = True
    elif mutation == "selected":
        c["selected"] = {}
    with pytest.raises(vr.VoltageBiosError):
        vr.validate_contracts(changes, c)
