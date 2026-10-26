"""Synthetic linked-current topology and exact loaded packet preservation."""

import copy
import json
import struct
import threading
from types import SimpleNamespace

import pytest

from druta import linked_current_runtime as lc

LAYOUT = {
    "info": [lc.INFO_BASE, lc.INFO_STRIDE],
    "control": [lc.CONTROL_BASE, lc.CONTROL_STRIDE],
    "status": [lc.STATUS_BASE, lc.STATUS_STRIDE],
    "sizes": {0x2080A618: lc.INFO_SIZE, 0x2080A61A: lc.CONTROL_SIZE, 0x2080A619: lc.STATUS_SIZE},
}


def observation(source=7, target=9, relationship=2, *, partial=False, **identity):
    ident = {"slot": "0000:72:1d.3", "architecture": 9, "device_id": 0xABCD, "subsystem_id": 0x12345678}
    ident.update(identity)
    info, control = bytearray(lc.INFO_SIZE), bytearray(lc.CONTROL_SIZE)
    indices = {source, target} | (set() if partial else {next(i for i in range(32) if i not in (source, target))})
    mask = sum(1 << i for i in indices)
    struct.pack_into("<II", info, 0, 320, mask)
    struct.pack_into("<5I", control, 0, 0, 0, 0, 255, mask)
    rows = {}
    for index in indices:
        kind, channel, unit, limits = (
            (32, 5, 0, (1, 500000, 900000))
            if index == source
            else ((30, 19, 1, (5000, 50000, 90000)) if index == target else (231, 255, 7, (0, 0, 0)))
        )
        ip, cp = lc.INFO_BASE + index * lc.INFO_STRIDE, lc.CONTROL_BASE + index * lc.CONTROL_STRIDE
        struct.pack_into("<IIIII", info, ip, 0x40, kind | channel << 8 | unit << 16, *limits)
        struct.pack_into("<II", control, cp, kind, limits[1])
        rows[str(index)] = {"type": kind}
    ip, cp = lc.INFO_BASE + source * lc.INFO_STRIDE, lc.CONTROL_BASE + source * lc.CONTROL_STRIDE
    struct.pack_into("<H", info, ip + 0x34, relationship)
    struct.pack_into("<I", control, cp + 0x28, 45000)
    rows[str(source)]["linked_current_hex"] = struct.pack("<f", 45000).hex()
    info[-1], control[-1] = 0xA7, 0xC1
    return {
        "schema": 1,
        "stable_reads": 2,
        "identity": ident,
        "info_hex": info.hex(),
        "control_hex": control.hex(),
        "status_mask": mask,
        "status_rows": rows,
    }


class GPU:
    def __init__(self, obs=None):
        self.observation = observation() if obs is None else copy.deepcopy(obs)
        ident = self.observation["identity"]
        self.address, self.architecture = ident["slot"], ident["architecture"]
        self._lock = threading.RLock()
        self.nvapi = SimpleNamespace(
            ok=True,
            gpu=123,
            selected={
                "slot": ident["slot"],
                "handle": 123,
                "devid": ident["device_id"],
                "subsys": ident["subsystem_id"],
            },
        )
        self.nvml = SimpleNamespace(selected=dict(self.nvapi.selected))
        self.pairing_error = ""
        self.calls, self.actions = 0, []
        self.layout = copy.deepcopy(LAYOUT)

    def slot(self):
        return self.address

    def arch(self):
        return self.architecture

    def _power_policy_table(self):
        self.calls += 1
        if self.actions:
            self.actions.pop(0)(self)
        state = self.observation
        status = bytearray(lc.STATUS_SIZE)
        struct.pack_into("<I", status, 4, state["status_mask"])
        for index, row in state["status_rows"].items():
            at = lc.STATUS_BASE + int(index) * lc.STATUS_STRIDE
            status[at] = row["type"]
            struct.pack_into("<II", status, at + 4, self.calls * 123, self.calls * 456)
            if "linked_current_hex" in row:
                status[at + 0x148 : at + 0x14C] = bytes.fromhex(row["linked_current_hex"])
        return (
            copy.deepcopy(self.layout),
            list(struct.unpack(f"<{lc.INFO_SIZE // 4}I", bytes.fromhex(state["info_hex"]))),
            list(struct.unpack(f"<{lc.CONTROL_SIZE // 4}I", bytes.fromhex(state["control_hex"]))),
            list(struct.unpack(f"<{lc.STATUS_SIZE // 4}I", status)),
        )


def mutate(state, packet, at, value, fmt="I"):
    raw = bytearray.fromhex(state[packet + "_hex"])
    struct.pack_into("<" + fmt, raw, at, value)
    state[packet + "_hex"] = raw.hex()


def inspection(source=7, target=9, relationship=2):
    table = {
        "name": "power",
        "offset": 1000,
        "version": 0x40,
        "header_size": 56,
        "stride": 102,
        "count": max(source, target) + 1,
        "object_id": 19,
    }
    table["size"] = table["header_size"] + table["stride"] * table["count"]
    fields = []
    for index, kind, native, channel, unit, limits in (
        (source, 13, 32, 5, "mW", [1, 500000, 900000]),
        (target, 11, 30, 19, "mA", [5000, 50000, 90000]),
    ):
        row = table["offset"] + table["header_size"] + index * table["stride"]
        for ordinal, role in enumerate(("minimum", "default", "maximum")):
            fields.append(
                {
                    "key": f"power.{index}.{role}",
                    "policy": index,
                    "units": unit,
                    "capability": "editable",
                    "encoding": "I",
                    "width": 4,
                    "scale": 1,
                    "mask": None,
                    "offsets": [row + 13 + ordinal * 4],
                    "current": limits[ordinal],
                    "runtime_binding": {
                        "policy_index": index,
                        "vbios_type": kind,
                        "runtime_type": native,
                        "channel": channel,
                        "units": unit,
                    },
                }
            )
    row = table["offset"] + table["header_size"] + source * table["stride"]
    relation_at = table["offset"] + table["size"] + relationship * 18
    fields.append(
        {
            "key": f"power.{source}.linked_current",
            "policy": source,
            "linked_policy": target,
            "linked_relationship": relationship,
            "linked_relationship_offset": relation_at,
            "relationship_binding": {
                "kind": 1,
                "stride": 18,
                "count": relationship + 1,
                "offset": relation_at,
                "target_policy": target,
                "parameter_u16": 4096,
            },
            "units": "mA",
            "capability": "editable",
            "encoding": "I",
            "width": 4,
            "scale": 1,
            "mask": None,
            "offsets": [row + 61],
            "current": 45000,
        }
    )
    return {"tables": [table], "fields": fields}


def contract(monkeypatch, *, obs=None, bios=None, changes=None):
    obs = observation() if obs is None else obs
    bios = inspection() if bios is None else bios
    changes = {"power.7.linked_current": 44000} if changes is None else changes
    monkeypatch.setattr(lc, "read", lambda gpu: (copy.deepcopy(obs), None))
    return lc.contracts(object(), bios, changes)


def observed(saved, *, restore=False):
    state = copy.deepcopy(saved["before"])
    if not restore:
        state["control_hex"] = saved["after_control_hex"]
        for key, entry in saved["selected"].items():
            index = lc.KEY.fullmatch(key)[1]
            state["status_rows"][index]["linked_current_hex"] = struct.pack("<f", entry["after_ma"]).hex()
    return state


@pytest.mark.parametrize("source,target,relationship", [(7, 9, 2), (4, 12, 5)])
@pytest.mark.parametrize("partial", [False, True])
def test_contract_projects_only_selected_cache_and_resolves_distinct_relationship(
    monkeypatch, source, target, relationship, partial
):
    obs = observation(source, target, relationship, partial=partial)
    bios = inspection(source, target, relationship)
    key = f"power.{source}.linked_current"
    saved = contract(monkeypatch, obs=obs, bios=bios, changes={key: 44000})
    projected = bytearray.fromhex(obs["control_hex"])
    struct.pack_into("<I", projected, lc.CONTROL_BASE + source * lc.CONTROL_STRIDE + 0x28, 44000)
    assert saved["after_control_hex"] == projected.hex()
    assert saved["selected"][key]["binding"]["target"]["index"] == target
    assert saved["selected"][key]["binding"]["relationship"]["index"] == relationship
    assert lc.verify(observed(saved), saved) == [key]
    assert lc.verify(observed(saved, restore=True), saved, restore=True) == [key]
    lc.validate_contracts({key: 44000}, json.loads(json.dumps(saved)))


@pytest.mark.parametrize("fault", ["cache", "status", "info_peer", "control_peer"])
def test_contract_rejects_ignored_or_changed_loaded_state(monkeypatch, fault):
    saved = contract(monkeypatch)
    live = observed(saved)
    if fault == "cache":
        mutate(live, "control", lc.CONTROL_BASE + 7 * lc.CONTROL_STRIDE + 0x28, 45000)
    elif fault == "status":
        live["status_rows"]["7"]["linked_current_hex"] = struct.pack("<f", 45000).hex()
    else:
        packet = fault.split("_")[0]
        mutate(live, packet, (lc.INFO_SIZE if packet == "info" else lc.CONTROL_SIZE) - 4, 123)
    with pytest.raises(lc.LinkedCurrentError):
        lc.verify(live, saved)


def test_read_retains_opaque_configuration_and_ignores_dynamic_status():
    gpu = GPU()
    observed, error = lc.read(gpu)
    assert error is None and observed == gpu.observation and gpu.calls == 2
    assert "status_hex" not in observed
    assert observed["info_hex"][-2:] == "a7"


@pytest.mark.parametrize("architecture", [5, 6, 7, 8, 9, 10])
@pytest.mark.parametrize("partial", [False, True])
def test_alternate_identity_inventory_and_domain(architecture, partial):
    obs = observation(19, 3, 11, architecture=architecture, device_id=0x4321, subsystem_id=0x87654321, partial=partial)
    assert lc.read(GPU(obs)) == (obs, None)


def test_one_transient_get_retries_bounded():
    def fail(gpu):
        raise RuntimeError("temporary GET failure")

    gpu = GPU()
    gpu.actions = [fail]
    assert lc.read(gpu)[1] is None and gpu.calls == 3
    gpu = GPU()
    gpu.actions = [fail, fail]
    assert lc.read(gpu)[0] is None and gpu.calls == 2


def test_unstable_configuration_gets_one_new_pair():
    gpu = GPU()
    gpu.actions = [lambda g: None, lambda g: mutate(g.observation, "control", lc.CONTROL_SIZE - 4, 18)]
    assert lc.read(gpu)[1] is None and gpu.calls == 4


def test_identity_change_cannot_be_accepted_on_retry():
    gpu = GPU()

    def change(g):
        g.nvapi.selected["devid"] = g.nvml.selected["devid"] = 0x4321

    gpu.actions = [change]
    assert lc.read(gpu)[0] is None and gpu.calls == 1


@pytest.mark.parametrize("name", ["info", "control", "status"])
def test_unrecognized_native_geometry(name):
    gpu = GPU()
    gpu.layout[name][1] += 4
    assert lc.read(gpu)[0] is None and gpu.calls == 2


@pytest.mark.parametrize(
    "value", [True, False, None, "40000", 0, -1, 1.5, float("nan"), float("inf"), 16777217, 2147483647]
)
def test_current_requires_positive_exact_whole_float32(value):
    with pytest.raises(lc.LinkedCurrentError):
        lc._ma(value)


@pytest.mark.parametrize("value", [1, 44000, 44000.0, 16777218, 2147483520])
def test_exact_uint_float_intersection(value):
    assert lc._ma(value) == int(value)


@pytest.mark.parametrize(
    "field,value", [("schema", True), ("stable_reads", 1), ("status_mask", True), ("status_rows", {})]
)
def test_malformed_observation(field, value):
    obs = observation()
    obs[field] = value
    with pytest.raises(lc.LinkedCurrentError):
        lc.validate_observation(obs)


@pytest.mark.parametrize("packet", ["info", "control"])
@pytest.mark.parametrize("mode", ["short", "long", "nonhex", "uppercase"])
def test_packet_encodings_are_exact(packet, mode):
    obs = observation()
    key = packet + "_hex"
    obs[key] = {
        "short": obs[key][2:],
        "long": obs[key] + "00",
        "nonhex": "zz" + obs[key][2:],
        "uppercase": obs[key].upper(),
    }[mode]
    with pytest.raises(lc.LinkedCurrentError):
        lc.validate_observation(obs)


def test_unrelated_unknown_policy_does_not_erase_selected_capability():
    obs = observation()
    obs["status_rows"]["0"]["type"] = 199
    assert lc.read(GPU(obs))[1] is None
    assert lc._selected_current(lc.validate_observation(obs), 7) == 45000


@pytest.mark.parametrize("value", [44999, 45000.5, float("nan"), float("inf")])
def test_selected_status_rounding_or_disagreement(value):
    obs = observation()
    obs["status_rows"]["7"]["linked_current_hex"] = struct.pack("<f", value).hex()
    with pytest.raises(lc.LinkedCurrentError):
        lc._selected_current(lc.validate_observation(obs), 7)
