# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Production uP9512R discovery over a byte-level, hardware-free I2C bus."""
import ctypes
from pathlib import Path
import tomllib
from unittest.mock import Mock

import pytest

from druta import profiles, railctl
from druta.controllers.up9512r import READ_REGISTERS, UP9512R


CONTROLLER = "uPI uP9512R"


class Bus:
    def __init__(self, device=0x1234, subsystem=0xFEDCBA98):
        self.ok = True
        self.gpu = ctypes.c_void_p(17)
        self.selected = {"slot": "0000:06:00.0", "devid": device, "subsys": subsystem}
        self.devices = {}
        self.reads = []
        self.writes = []
        self.read_hook = None

    def add(self, port, address, *, locked=False):
        registers = {0x27: 0, 0x28: 0x2B, 0x0A: 0x12, 0x0B: 0x34,
                     0x0C: 0x5A, 0x2A: 0x20, 0x39: 0x87 if locked else 0x94,
                     0x2C: 13, 0x2D: 95}
        self.devices[port, address] = registers
        return registers

    def _i(self, function_id, *signature):
        if function_id == railctl.I2C_READ_EX:
            return self.read
        if function_id == railctl.I2C_WRITE_EX:
            return self.write
        raise AssertionError(f"Unexpected API lookup: {function_id:#x}")

    def read(self, gpu, pointer, extra):
        packet = ctypes.cast(pointer, ctypes.POINTER(railctl._V3)).contents
        assert getattr(gpu, "value", gpu) == self.gpu.value
        assert packet.regAddrSize == 1 and packet.bIsPortIdSet == 1
        assert packet.i2cDevAddress & 1 == 0
        port, address = packet.portId, packet.i2cDevAddress >> 1
        register, width = int(packet.pbI2cRegAddress[0]), int(packet.cbSize)
        self.reads.append((port, address, register, width))
        if self.read_hook is not None:
            status = self.read_hook(port, address, register, packet)
            if status is not None:
                return status
        value = self.devices.get((port, address), {}).get(register)
        if value is None or width != 1:
            return -1
        packet.pbData[0] = value
        return 0

    def write(self, gpu, pointer, extra):
        packet = ctypes.cast(pointer, ctypes.POINTER(railctl._V3)).contents
        self.writes.append((packet.portId, packet.i2cDevAddress >> 1,
                            int(packet.pbI2cRegAddress[0]), int(packet.pbData[0])))
        raise AssertionError("Discovery must never write, including SMBus unlock")


@pytest.fixture(autouse=True)
def bundled_profiles_only(monkeypatch):
    """Keep production families while excluding user-installed recipes."""
    directory = Path(__file__).resolve().parents[1] / "i2c"
    recipes = []
    for name in ("rtx2080ti-mp2888a.toml", "asus-rtx5080-astral-mp29816.toml"):
        path = directory / name
        with path.open("rb") as stream:
            recipes.append(railctl.Profile(tomllib.load(stream), str(path)))
    monkeypatch.setattr(railctl, "load_profiles", lambda **kwargs: recipes)


@pytest.mark.parametrize("device,subsystem,architecture,route", [
    (0x1188, 0x12345678, 2, (0, 0x08)),
    (0x2702, 0xDEADBEEF, 9, (7, 0x77)),
    (0xFFFF, 0, None, (5, 0x31)),
])
def test_identity_matches_without_gpu_architecture_or_observed_route_gates(
        device, subsystem, architecture, route):
    bus = Bus(device, subsystem)
    bus.add(*route)
    # Supplied PCI IDs deliberately differ: controller evidence is authoritative.
    hits = railctl.discover(bus, dev_id=0, subsys=1, architecture=architecture,
                           controller=CONTROLLER, routes=[route])
    assert len(hits) == 1 and isinstance(hits[0], UP9512R)
    rail = hits[0]
    assert (rail.p.port, rail.addr7) == route
    assert (rail.p.src["port"], rail.p.src["addr7"]) == route
    assert (profiles.rail_identity(rail)["port"], profiles.rail_identity(rail)["addr7"]) == route
    assert "physical rail unassigned" in rail.p.name
    assert rail.discovery_telemetry["vout_mv"] == 950
    assert rail.discovery_telemetry["imon_mv"] == 130
    assert {row[:2] for row in bus.reads} == {route}
    assert all(register in READ_REGISTERS and width == 1 for _, _, register, width in bus.reads)
    assert bus.writes == []


def test_named_scope_scans_all_routes_and_preserves_every_matching_controller():
    bus = Bus()
    expected = [(4, 0x25), (0, 0x08), (7, 0x77)]
    for route in expected:
        bus.add(*route)
    progress = Mock()
    hits = railctl.discover(bus, controller=CONTROLLER, progress=progress)
    assert [(rail.p.port, rail.addr7) for rail in hits] == expected
    assert {row[:2] for row in bus.reads} == {
        (port, address) for port in range(8) for address in range(0x08, 0x78)}
    assert all(register in READ_REGISTERS and width == 1 for _, _, register, width in bus.reads)
    assert progress.call_args_list[0].args[:2] == (0, 896)
    assert progress.call_args_list[-1].args[:2] == (896, 896)
    assert [call.args[0] for call in progress.call_args_list] == list(range(897))
    assert bus.writes == []


def test_full_scope_retains_multiple_hits_and_find_does_not_choose_one():
    bus = Bus()
    expected = {(1, 0x25), (6, 0x32)}
    for route in expected:
        bus.add(*route)
    progress, log = Mock(), Mock()
    hits = railctl.discover(bus, progress=progress, log=log)
    assert {(rail.p.port, rail.addr7) for rail in hits} == expected
    assert all(isinstance(rail, UP9512R) for rail in hits)
    assert {0x27, 0x5D, 0x99, 0xBE, 0xAD} <= {row[2] for row in bus.reads}
    complete, total = progress.call_args.args[:2]
    assert complete == total and total > 896
    assert any("ambiguous" in call.args[0] for call in log.call_args_list)
    assert railctl.find(bus) is None
    assert bus.writes == []


def test_full_scope_respects_exact_route_filter_for_every_family():
    bus = Bus()
    bus.add(7, 0x77)
    bus.add(2, 0x25)
    progress = Mock()
    hits = railctl.discover(bus, routes=[(7, 0x77)], progress=progress)
    assert [(rail.p.port, rail.addr7) for rail in hits] == [(7, 0x77)]
    assert {row[:2] for row in bus.reads} == {(7, 0x77)}
    assert progress.call_args.args[:2] == (4, 4)
    assert bus.writes == []


def test_locked_controller_is_discovered_without_unlock_or_offset_writes():
    bus = Bus()
    registers = bus.add(3, 0x24, locked=True)
    before = dict(registers)
    hits = railctl.discover(bus, controller=CONTROLLER, routes=[(3, 0x24)])
    assert len(hits) == 1
    assert hits[0].discovery_telemetry["write_locked"] is True
    assert hits[0].discovery_telemetry["offsets_mv"] == [10, 20, 30, 40, 50]
    assert hits[0].discovery_telemetry["offset_enabled"] is False
    assert registers == before and bus.writes == []


@pytest.mark.parametrize("fault", ["wrong_vendor", "wrong_device", "short", "untouched", "changed_id"])
def test_acknowledgment_or_incomplete_unstable_identity_does_not_create_candidate(fault):
    bus = Bus()
    registers = bus.add(2, 0x25)
    if fault == "wrong_vendor":
        registers[0x27] = 1
    elif fault == "wrong_device":
        registers[0x28] = 0
    else:
        device_reads = 0

        def corrupt(port, address, register, packet):
            nonlocal device_reads
            if register != 0x28:
                return None
            device_reads += 1
            if fault == "short":
                packet.pbData[0] = 0x2B
                packet.cbSize = 0
                return 0
            if fault == "untouched":
                return 0
            if device_reads > 1:
                packet.pbData[0] = 0
                return 0
            return None

        bus.read_hook = corrupt
    assert railctl.discover(bus, controller=CONTROLLER, routes=[(2, 0x25)]) == []
    assert bus.writes == []


def test_pre_cancelled_scan_reports_initial_progress_and_touches_no_bus():
    bus = Bus()
    progress = Mock()
    assert railctl.discover(bus, controller=CONTROLLER, cancelled=lambda: True,
                           routes=[(2, 0x25)], progress=progress) == []
    assert progress.call_args_list[0].args[:2] == (0, 1)
    assert len(progress.call_args_list) == 1
    assert bus.reads == [] and bus.writes == []


def test_cancellation_retains_completed_hit_and_never_probes_next_route():
    bus = Bus()
    bus.add(0, 0x25)
    bus.add(7, 0x77)
    events = []

    def progress(complete, total, label):
        events.append((complete, total, label))

    hits = railctl.discover(bus, controller=CONTROLLER, routes=[(0, 0x25), (7, 0x77)],
                           progress=progress, cancelled=lambda: bool(events and events[-1][0] >= 1))
    assert [(rail.p.port, rail.addr7) for rail in hits] == [(0, 0x25)]
    assert [event[:2] for event in events] == [(0, 2), (1, 2)]
    assert {row[:2] for row in bus.reads} == {(0, 0x25)}
    assert bus.writes == []


def test_unavailable_api_or_invalid_scope_never_accesses_hardware():
    bus = Bus()
    bus.ok = False
    progress = Mock()
    assert railctl.discover(bus, controller=CONTROLLER, progress=progress) == []
    assert progress.call_args.args[:2] == (0, 0)
    bus.ok = True
    with pytest.raises(ValueError, match="unknown I2C controller"):
        railctl.discover(bus, controller="uP9512R typo")
    with pytest.raises(ValueError, match="routes"):
        railctl.discover(bus, controller=CONTROLLER, routes=[(2, 0x78)])
    assert CONTROLLER in railctl.controller_names()
    assert bus.reads == [] and bus.writes == []
