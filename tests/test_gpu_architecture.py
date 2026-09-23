# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Adapter identity must not depend on NVML or a driver/board allowlist."""
import ctypes
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from druta import nvbackend as n


def gpu_with(nvml_reader=None, nvapi_reader=None):
    gpu = n.GPU.__new__(n.GPU)
    gpu._lock = threading.RLock()
    dll = SimpleNamespace()
    if nvml_reader is not None:
        dll.nvmlDeviceGetArchitecture = nvml_reader
    gpu.nvml = SimpleNamespace(ok=nvml_reader is not None, selected=None,
                               dev=object(), dll=dll,
                               has=lambda name: hasattr(dll, name))
    gpu.nvapi = SimpleNamespace(ok=True, gpu=object(), GetArchInfo=nvapi_reader,
                               RamType=None, selected=None)
    gpu.static = {"name": "NVIDIA GeForce RTX 5090"}  # Never a detection input.
    gpu.slot = Mock(return_value="0000:04:00.0")
    gpu._offset_range = Mock(return_value=None)
    gpu._native_fan_data = Mock(return_value=None)
    return gpu


def arch_reader(architecture, implementation=0, status=0):
    def read(handle, pointer):
        info = ctypes.cast(pointer, ctypes.POINTER(n._GpuArchInfo)).contents
        info.architecture = architecture
        info.implementation = implementation
        info.revision = 0xA1
        return status
    return Mock(side_effect=read)


def nvml_arch(value, status=0):
    def read(handle, pointer):
        ctypes.cast(pointer, ctypes.POINTER(n.u32))[0] = value
        return status
    return Mock(side_effect=read)


@pytest.mark.parametrize("implementation", [0x2, 0x6, 0x7, 0xFE])
def test_pascal_detection_uses_architecture_not_board_or_implementation(implementation):
    reader = arch_reader(0x130, implementation)
    gpu = gpu_with(nvapi_reader=reader)
    assert gpu.arch() == n.GPU.ARCH_PASCAL
    assert gpu.arch_name() == "Pascal"
    assert gpu._arch_source == "NVAPI V2"
    assert reader.call_count == 1  # Successful identity is stable on this object.
    assert reader.call_args.args[0] is gpu.nvapi.gpu


@pytest.mark.parametrize("architecture, expected", [
    (0xE0, 2), (0xF0, 2), (0x100, 2), (0x110, 3), (0x120, 3),
    (0x140, 5), (0x150, 5), (0x160, 6), (0x170, 7), (0x190, 8), (0x1B0, 10),
])
def test_public_architecture_enum_translation(architecture, expected):
    assert gpu_with(nvapi_reader=arch_reader(architecture)).arch() == expected


def test_version_negotiation_accepts_v1_only_driver():
    versions = []

    def read(handle, pointer):
        info = ctypes.cast(pointer, ctypes.POINTER(n._GpuArchInfo)).contents
        versions.append(info.version)
        if info.version == 0x20010:
            return -9  # NVAPI_INCOMPATIBLE_STRUCT_VERSION
        info.architecture = 0x130
        return 0

    gpu = gpu_with(nvapi_reader=read)
    assert gpu.arch() == n.GPU.ARCH_PASCAL
    assert versions == [0x20010, 0x10010]
    assert gpu._arch_source == "NVAPI V1"


def test_nvml_success_keeps_existing_identity_source():
    reader = arch_reader(0x130)
    nvml = nvml_arch(6)
    gpu = gpu_with(nvml_reader=nvml, nvapi_reader=reader)
    assert gpu.arch() == n.GPU.ARCH_TURING
    assert gpu.arch() == n.GPU.ARCH_TURING
    assert nvml.call_count == 1
    reader.assert_not_called()


@pytest.mark.parametrize("nvml", [
    nvml_arch(4, status=3), nvml_arch(0xFFFFFFFF), Mock(side_effect=OSError("reload")),
])
def test_nvml_failure_or_unknown_enum_falls_back(nvml):
    assert gpu_with(nvml_reader=nvml, nvapi_reader=arch_reader(0x130)).arch() == 4


def test_nvml_missing_architecture_export_still_uses_nvapi():
    gpu = gpu_with(nvapi_reader=arch_reader(0x130))
    gpu.nvml.ok = True
    assert gpu.arch() == n.GPU.ARCH_PASCAL


def test_nvapi_transient_failure_is_retried_on_next_attempt():
    ready = arch_reader(0x130)
    reader = Mock(side_effect=[-1, None])
    gpu = gpu_with(nvapi_reader=reader)
    assert gpu.arch() is None
    reader.side_effect = ready
    assert gpu.arch() == n.GPU.ARCH_PASCAL
    assert reader.call_count == 2  # No immediate version sweep on unrelated error.


def test_nvml_transient_failure_is_retried_on_next_attempt():
    gpu = gpu_with(nvml_reader=nvml_arch(4, status=3))
    assert gpu.arch() is None
    gpu.nvml.dll.nvmlDeviceGetArchitecture = nvml_arch(4)
    assert gpu.arch() == n.GPU.ARCH_PASCAL


@pytest.mark.parametrize("architecture", [0, 0x180, 0xFFFFFFFF])
def test_unknown_architecture_is_not_guessed_or_cached(architecture):
    gpu = gpu_with(nvapi_reader=arch_reader(architecture))
    assert gpu.arch() is None
    gpu.nvapi.GetArchInfo = arch_reader(0x130)
    assert gpu.arch() == n.GPU.ARCH_PASCAL


def test_changed_struct_version_is_not_accepted():
    def read(handle, pointer):
        info = ctypes.cast(pointer, ctypes.POINTER(n._GpuArchInfo)).contents
        info.architecture = 0x130
        info.version = 0x10010
        return 0

    assert gpu_with(nvapi_reader=read).arch() is None


def test_disabled_unpaired_nvapi_is_never_used():
    reader = arch_reader(0x130)
    gpu = gpu_with(nvapi_reader=reader)
    gpu.nvapi.ok = False  # _pair() disables the mismatched NVAPI handle.
    assert gpu.arch() is None
    reader.assert_not_called()


def test_architecture_cache_is_per_adapter():
    pascal = gpu_with(nvapi_reader=arch_reader(0x130))
    turing = gpu_with(nvapi_reader=arch_reader(0x160))
    assert (pascal.arch(), turing.arch(), pascal.arch()) == (4, 6, 4)


def string_reader(value, status=0):
    def read(*args):
        buffer = args[-1] if isinstance(args[-1], ctypes.Array) else args[-2]
        buffer.value = value
        return status
    return Mock(side_effect=read)


def test_nvapi_only_identity_names_current_adapter():
    gpu = gpu_with()
    gpu.nvapi.GetFullName = string_reader(b"GeForce GTX 1060 6GB")
    gpu.nvapi.GetVbiosVersionString = string_reader(b"86.06.4b.00.36")

    def driver(pointer, branch):
        ctypes.cast(pointer, ctypes.POINTER(n.u32))[0] = 58266
        return 0

    gpu.nvapi.GetDriverAndBranchVersion = driver
    static = gpu._read_static()
    assert (static["name"], static["driver"], static["vbios"]) == (
        "GeForce GTX 1060 6GB", "582.66", "86.06.4b.00.36")
    assert gpu.nvapi.GetFullName.call_args.args[0] is gpu.nvapi.gpu


def test_failed_identity_calls_do_not_reuse_other_fields_or_failure_bytes():
    gpu = gpu_with(nvml_reader=nvml_arch(4))
    gpu.nvml.dll.nvmlDeviceGetName = string_reader(b"NVML name")
    gpu.nvml.dll.nvmlSystemGetDriverVersion = string_reader(b"garbage", status=3)
    gpu.nvml.dll.nvmlDeviceGetVbiosVersion = string_reader(b"", status=3)
    gpu.nvapi.GetVbiosVersionString = string_reader(b"NVAPI VBIOS")
    static = gpu._read_static()
    assert (static["name"], static["driver"], static["vbios"]) == (
        "NVML name", "?", "NVAPI VBIOS")


def test_enumeration_uses_nvapi_name_when_nvml_does_not_enumerate_card():
    entry = {"slot": "0000:04:00.0", "name": "GeForce GTX 1060 6GB",
             "devid": 0x1C03, "subsys": 0xDEADBEEF}
    with patch.object(n, "Nvml", return_value=SimpleNamespace(gpus=[])), \
            patch.object(n, "NvAPI", return_value=SimpleNamespace(gpus=[entry])):
        cards = n.enumerate_gpus()
    assert len(cards) == 1
    assert cards[0]["name"] == entry["name"]
    assert cards[0]["nvml_index"] is None
    assert cards[0]["slot"] == entry["slot"]
