# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Registry diagnostics through both real backends and the shared fake driver."""
import contextlib
import ctypes
import struct
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from druta import nvbackend, registry_overrides, registry_panel
from tests import fake_driver
from tests.fake_driver import SECOND_SLOT
from tests.frontend_rig import both
from tests.test_fake_driver import KINDS
from tests.test_power_arbitration import CONTROL, INFO, STATUS, packets
from tests.test_power_relationships import packet as relationship_packet


@contextlib.contextmanager
def configured_driver(configure):
    """Configure real fake-driver packets/exports before either backend opens it."""
    original = fake_driver.FakeDriver

    def construct(**options):
        driver = original(**options)
        configure(driver)
        return driver

    with patch.object(fake_driver, "FakeDriver", side_effect=construct):
        yield


def diagnostic_packets(driver):
    layout, info, control, status = packets()
    _, relationship = relationship_packet()
    info[:] = struct.unpack(f"<{len(relationship) // 4}I", relationship)
    payloads = {INFO: info, CONTROL: control, STATUS: status}

    def exchange(request, fields):
        command, size = request[14], request[15]
        assert command in payloads, "registry diagnostics must never issue a SET"
        if size != layout["sizes"][command]:
            request[16] = 0x1F  # a definite layout refusal, then try the next known geometry
            return 0
        request[17:] = payloads[command]
        return 0

    driver.escapes.inner = exchange


@contextlib.contextmanager
def panel_output():
    values = {}
    # Freeze only this module's clock facade; the driver's sleep/monotonic seams remain intact.
    clock = SimpleNamespace(time=lambda: 1700000000.0,
                            sleep=nvbackend.time.sleep, monotonic=nvbackend.time.monotonic)
    with patch.object(registry_panel, "dpg", SimpleNamespace(set_value=values.__setitem__)), \
            patch.object(nvbackend, "time", clock):
        yield values


@pytest.mark.parametrize("kind", KINDS)
def test_registry_diagnostics_decode_and_render_real_policy_packets(kind):
    def flow(session):
        panel = registry_panel.RegistryPanel(session.app)
        before = session.driver.state()
        with panel_output() as values:
            panel.refresh_arbitration()
            panel.refresh_relationships()
            assert "Policy 9: type 30, channel 9" in values["reg_arbitration"]
            assert "Requested 45 A; effective 36.002 A" in values["reg_arbitration"]
            assert "client 2: 36.002 A" in values["reg_arbitration"]
            assert "Relationship 9 -> target policy 9 (type 0)" in values["reg_relationships"]
            assert "Weight: 1 (raw 4096)" in values["reg_relationships"]
            assert panel.arbitration["identity"]["slot"] == session.gpu.slot()
            assert panel.relationships["identity"] == panel.arbitration["identity"]
            assert session.driver.state() == before
            commands = [entry[1][14] for entry in session.driver.escapes.log]
            assert set(commands) == {INFO, CONTROL, STATUS}
            return values, panel.arbitration, panel.relationships

    with configured_driver(diagnostic_packets):
        both(kind, flow)


@pytest.mark.parametrize("kind", KINDS)
def test_registry_refresh_retains_history_on_failure_and_clears_it_on_card_switch(kind):
    def flow(session):
        panel = registry_panel.RegistryPanel(session.app)
        with panel_output() as values:
            panel.refresh_arbitration()
            panel.refresh_relationships()
            prior = panel.arbitration, panel.relationships
            session.driver.escapes.refuse = lambda request: 0x1F
            panel.refresh_arbitration()
            panel.refresh_relationships()
            historical = dict(values)
            assert all("historical" in value for value in historical.values())
            assert (panel.arbitration, panel.relationships) == prior
            session.app.gpu = session.open(SECOND_SLOT)
            session.app.gpu.static.get("pl_min_mw")
            panel.refresh_arbitration()
            panel.refresh_relationships()
            assert panel.arbitration is None and panel.relationships is None
            assert all("historical" not in value for value in values.values())
            assert "Policy 9:" not in values["reg_arbitration"]
            assert "Relationship 9 ->" not in values["reg_relationships"]
            return historical, values

    with configured_driver(diagnostic_packets):
        both(kind, flow, second_card=True)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("reader", ("refresh_arbitration", "refresh_relationships"))
def test_registry_diagnostics_report_unavailable_nvapi_without_driver_access(kind, reader):
    def flow(session):
        panel = registry_panel.RegistryPanel(session.app)
        with panel_output() as values:
            mark = session.driver.mark()
            getattr(panel, reader)()
            assert session.driver.since(mark) == []
            assert "unavailable" in next(iter(values.values()))
            return values

    both(kind, flow, with_nvapi=False)


class Image(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("size", ctypes.c_uint32), ("buffer", ctypes.c_void_p)]


def vbios_export(mode):
    def configure(driver):
        reads = []

        @ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def getter(handle, pointer):
            image = Image.from_address(pointer)
            assert image.version == 0x20010
            assert image.size == registry_overrides._IMAGE_CAPACITY
            reads.append(handle)
            if mode == "status":
                return -7
            if mode == "size":
                image.size = 1
                return 0
            if mode == "version":
                image.version = 0x10010
                return 0
            attempt = len(reads) - 1
            value = 65 if mode == "stable" else (65 + attempt % 2 if mode == "unstable" else min(65 + attempt, 67))
            payload = bytes([value]) * 63 + bytes([handle - 0x2000])
            ctypes.memmove(image.buffer, payload, len(payload))
            image.size = len(payload)
            return 0

        driver.vbios_reads = reads
        driver.nvapi_table[0xFC13EE11] = getter
        driver._keep.append(getter)

    return configure


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("second", (False, True))
@pytest.mark.parametrize("mode", ("stable", "retry", "unstable", "status", "size", "version"))
def test_registry_vbios_reads_use_paired_companion_and_bounded_validation(kind, second, mode):
    def flow(session):
        gpu = session.open(SECOND_SLOT) if second else session.gpu
        gpu.static.get("pl_min_mw")
        if mode in ("stable", "retry"):
            result = registry_overrides.read_vbios(gpu)
            assert result == bytes([65 if mode == "stable" else 67]) * 63 + bytes([int(second)])
            assert len(session.driver.vbios_reads) == (2 if mode == "stable" else 4)
        else:
            with pytest.raises(registry_overrides.OverrideError) as caught:
                registry_overrides.read_vbios(gpu)
            result = str(caught.value)
            assert len(session.driver.vbios_reads) == (6 if mode == "unstable" else 1)
            if mode == "unstable":
                assert result == "VBIOS image changed between reads; retry when the GPU is stable"
            elif mode == "status":
                assert result == "VBIOS getter returned status -7"
            else:
                assert result == "VBIOS getter returned an unexpected size/version"
        assert set(session.driver.vbios_reads) == {0x2001 if second else 0x2000}
        return result, list(session.driver.vbios_reads)

    with configured_driver(vbios_export(mode)):
        both(kind, flow, second_card=True)


@pytest.mark.parametrize("kind", KINDS)
def test_registry_vbios_missing_export_is_a_capability_refusal(kind):
    def flow(session):
        with pytest.raises(registry_overrides.OverrideError) as caught:
            registry_overrides.read_vbios(session.gpu)
        assert str(caught.value) == "This driver does not expose the VBIOS image getter"
        return str(caught.value)

    both(kind, flow)
