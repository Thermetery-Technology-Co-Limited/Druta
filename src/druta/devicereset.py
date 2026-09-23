# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded restart of one verified NVIDIA display device on Windows.

The binding deliberately comes from SetupAPI rather than display names or
``pnputil`` text.  Microsoft documents ``SPDRP_BUSNUMBER`` and
``SPDRP_ADDRESS`` as the device's bus number and address respectively, and
documents ``/restart-device`` as available from Windows 10 version 2004:

* https://learn.microsoft.com/windows/win32/api/setupapi/nf-setupapi-setupdigetdeviceregistrypropertyw
* https://learn.microsoft.com/windows-hardware/drivers/devtest/pnputil-command-syntax
* https://learn.microsoft.com/windows/win32/api/cfgmgr32/nf-cfgmgr32-cm_get_devnode_status

SetupAPI's documented bus/address properties contain no PCI segment/domain.
Consequently the module rejects non-zero domains and treats ``0000`` as the
caller's conventional Windows slot label, never as a value proven by SetupAPI.
It also rejects duplicate bus/device/function candidates.  That keeps the
action bounded on normal single-segment Windows systems, while leaving unusual
multi-segment mapping unsupported until Windows exposes documented evidence.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
import ntpath
import os
import platform
import re
import subprocess
from typing import Iterable


# GUID_DEVCLASS_DISPLAY from devguid.h.  A SetupAPI class GUID is independent
# of the localized Display class name.
_DISPLAY_CLASS = (0x4D36E968, 0xE325, 0x11CE, (0xBF, 0xC1, 0x08, 0x00, 0x2B, 0xE1, 0x03, 0x18))
_DIGCF_PRESENT = 0x00000002
_ERROR_INSUFFICIENT_BUFFER = 122
_ERROR_NO_MORE_ITEMS = 259

_SPDRP_DEVICEDESC = 0
_SPDRP_HARDWAREID = 1
_SPDRP_FRIENDLYNAME = 12
_SPDRP_BUSNUMBER = 21
_SPDRP_ENUMERATOR_NAME = 22
_SPDRP_ADDRESS = 28

_CR_SUCCESS = 0
_DN_STARTED = 0x00000008
_PNPUTIL_DIAGNOSTIC_LIMIT = 512
_SLOT = re.compile(r"^(?P<domain>[0-9A-Fa-f]{4}):(?P<bus>[0-9A-Fa-f]{2}):(?P<device>[0-9A-Fa-f]{2})\.(?P<function>[0-7])$")
_NVIDIA_PCI_ID = re.compile(r"^PCI\\VEN_10DE&DEV_[0-9A-F]{4}(?:&[A-Z0-9_]+)*$", re.IGNORECASE)


class DeviceResetError(RuntimeError):
    """The requested device cannot be proven safe to restart."""


@dataclass(frozen=True)
class DeviceTarget:
    """The exact PnP device selected by a canonical, domain-0000 PCI slot label."""

    slot: str
    instance_id: str
    name: str


@dataclass(frozen=True)
class RestartResult:
    ok: bool
    reboot_required: bool
    message: str
    returncode: int | None


def _parse_slot(slot: str) -> tuple[str, int, int, int]:
    if not isinstance(slot, str):
        raise DeviceResetError("GPU slot must be a PCI address such as 0000:01:00.0")
    match = _SLOT.fullmatch(slot)
    if match is None:
        raise DeviceResetError("GPU slot must be a PCI address such as 0000:01:00.0")
    domain = match.group("domain").lower()
    if domain != "0000":
        raise DeviceResetError("Windows PnP reset does not support a non-zero PCI segment for this GPU")
    bus = int(match.group("bus"), 16)
    device = int(match.group("device"), 16)
    function = int(match.group("function"), 10)
    if device > 31:
        raise DeviceResetError("GPU slot has an invalid PCI device number")
    return f"{domain}:{bus:02x}:{device:02x}.{function}", bus, device, function


def _target_from_properties(
    *, instance_id: str, name: str, enumerator: str, hardware_ids: Iterable[str], bus: int, address: int
) -> DeviceTarget | None:
    """Return a proven NVIDIA PCI display target, or ignore this device."""
    if enumerator.casefold() != "pci" or not any(_NVIDIA_PCI_ID.fullmatch(item) for item in hardware_ids):
        return None
    if not instance_id or not name or not 0 <= bus <= 0xFF:
        return None
    device, function = address >> 16, address & 0xFFFF
    if device > 31 or function > 7:
        return None
    return DeviceTarget(f"0000:{bus:02x}:{device:02x}.{function}", instance_id, name)


def _is_nvidia_pci(enumerator: str, hardware_ids: Iterable[str]) -> bool:
    return enumerator.casefold() == "pci" and any(_NVIDIA_PCI_ID.fullmatch(item) for item in hardware_ids)


def _target_from_display_info(setupapi, handle, info) -> DeviceTarget | None:
    """Read BDF properties only after this present Display device proves it is NVIDIA PCI.

    A virtual, remote, or non-NVIDIA display adapter need not expose PCI
    address properties.  Its incomplete data is irrelevant to a selected
    NVIDIA PCI target.  Once the vendor/bus identity matches, all following
    properties are mandatory so the candidate cannot be restarted ambiguously.
    """
    enumerator = _property_string(setupapi, handle, info, _SPDRP_ENUMERATOR_NAME)
    if enumerator.casefold() != "pci":
        return None
    hardware_ids = _property_multisz(setupapi, handle, info, _SPDRP_HARDWAREID)
    if not _is_nvidia_pci(enumerator, hardware_ids):
        return None
    try:
        name = _property_string(setupapi, handle, info, _SPDRP_FRIENDLYNAME)
    except DeviceResetError:
        name = _property_string(setupapi, handle, info, _SPDRP_DEVICEDESC)
    return _target_from_properties(
        instance_id=_instance_id(setupapi, handle, info),
        name=name,
        enumerator=enumerator,
        hardware_ids=hardware_ids,
        bus=_property_dword(setupapi, handle, info, _SPDRP_BUSNUMBER),
        address=_property_dword(setupapi, handle, info, _SPDRP_ADDRESS),
    )


def _windows_build() -> int | None:
    if platform.system() != "Windows":
        return None
    numbers = [int(value) for value in re.findall(r"\d+", platform.version())]
    if len(numbers) < 3 or numbers[0] < 10:
        return None
    return numbers[2]


def pnputil_restart_supported() -> bool:
    """``/restart-device`` was introduced in Windows 10 version 2004 (build 19041)."""
    build = _windows_build()
    return build is not None and build >= 19041


def is_admin() -> bool:
    if platform.system() != "Windows":
        return False
    try:
        return bool(ctypes.WinDLL("shell32", use_last_error=True).IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("ClassGuid", _GUID),
        ("DevInst", ctypes.c_ulong),
        ("Reserved", ctypes.c_void_p),
    ]


def _last_error() -> int:
    return ctypes.get_last_error()


def _property_bytes(setupapi, handle, info, property_id: int) -> bytes:
    property_type = ctypes.c_ulong()
    needed = ctypes.c_ulong()
    ctypes.set_last_error(0)
    setupapi.SetupDiGetDeviceRegistryPropertyW(
        handle, ctypes.byref(info), property_id, ctypes.byref(property_type), None, 0, ctypes.byref(needed)
    )
    if _last_error() != _ERROR_INSUFFICIENT_BUFFER or not needed.value:
        raise DeviceResetError(f"cannot read required device property {property_id}")
    buffer = (ctypes.c_ubyte * needed.value)()
    if not setupapi.SetupDiGetDeviceRegistryPropertyW(
        handle, ctypes.byref(info), property_id, ctypes.byref(property_type), buffer, needed.value, ctypes.byref(needed)
    ):
        raise DeviceResetError(f"cannot read required device property {property_id}")
    return bytes(buffer[: needed.value])


def _property_string(setupapi, handle, info, property_id: int) -> str:
    return _property_bytes(setupapi, handle, info, property_id).decode("utf-16-le").rstrip("\0")


def _property_multisz(setupapi, handle, info, property_id: int) -> list[str]:
    value = _property_bytes(setupapi, handle, info, property_id).decode("utf-16-le")
    return [item for item in value.split("\0") if item]


def _property_dword(setupapi, handle, info, property_id: int) -> int:
    value = _property_bytes(setupapi, handle, info, property_id)
    if len(value) != 4:
        raise DeviceResetError(f"device property {property_id} is not a DWORD")
    return int.from_bytes(value, "little")


def _instance_id(setupapi, handle, info) -> str:
    required = ctypes.c_ulong()
    ctypes.set_last_error(0)
    setupapi.SetupDiGetDeviceInstanceIdW(handle, ctypes.byref(info), None, 0, ctypes.byref(required))
    if _last_error() != _ERROR_INSUFFICIENT_BUFFER or not required.value:
        raise DeviceResetError("cannot read device instance ID")
    buffer = ctypes.create_unicode_buffer(required.value)
    if not setupapi.SetupDiGetDeviceInstanceIdW(handle, ctypes.byref(info), buffer, required.value, ctypes.byref(required)):
        raise DeviceResetError("cannot read device instance ID")
    return buffer.value


def _enumerate_display_pci_devices() -> list[DeviceTarget]:
    """Use SetupAPI's present Display setup class; never parse command output."""
    if platform.system() != "Windows":
        raise DeviceResetError("PnP restart is available only on Windows")
    setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
    setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(_GUID), ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_ulong]
    setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    setupapi.SetupDiEnumDeviceInfo.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(_SP_DEVINFO_DATA)]
    setupapi.SetupDiEnumDeviceInfo.restype = ctypes.c_int
    setupapi.SetupDiGetDeviceInstanceIdW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_SP_DEVINFO_DATA), ctypes.c_wchar_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
    setupapi.SetupDiGetDeviceInstanceIdW.restype = ctypes.c_int
    setupapi.SetupDiGetDeviceRegistryPropertyW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_SP_DEVINFO_DATA), ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ubyte), ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
    setupapi.SetupDiGetDeviceRegistryPropertyW.restype = ctypes.c_int
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
    setupapi.SetupDiDestroyDeviceInfoList.restype = ctypes.c_int
    guid = _GUID(_DISPLAY_CLASS[0], _DISPLAY_CLASS[1], _DISPLAY_CLASS[2], (ctypes.c_ubyte * 8)(*_DISPLAY_CLASS[3]))
    handle = setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, _DIGCF_PRESENT)
    if handle == ctypes.c_void_p(-1).value:
        raise DeviceResetError("cannot enumerate present Windows display devices")
    targets = []
    try:
        index = 0
        while True:
            info = _SP_DEVINFO_DATA()
            info.cbSize = ctypes.sizeof(_SP_DEVINFO_DATA)
            ctypes.set_last_error(0)
            if not setupapi.SetupDiEnumDeviceInfo(handle, index, ctypes.byref(info)):
                if _last_error() == _ERROR_NO_MORE_ITEMS:
                    break
                raise DeviceResetError("cannot enumerate present Windows display devices")
            index += 1
            target = _target_from_display_info(setupapi, handle, info)
            if target is not None:
                targets.append(target)
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(handle)
    return targets


def resolve_target(slot: str) -> DeviceTarget:
    """Resolve exactly one present NVIDIA PCI Display device for ``slot``."""
    canonical, bus, device, function = _parse_slot(slot)
    candidates = [
        target
        for target in _enumerate_display_pci_devices()
        if target.slot == f"0000:{bus:02x}:{device:02x}.{function}"
    ]
    if not candidates:
        raise DeviceResetError(f"no present NVIDIA display device is proven at {canonical}")
    if len(candidates) != 1:
        raise DeviceResetError(f"multiple present NVIDIA display devices claim {canonical}")
    return candidates[0]


def system_pnputil() -> str:
    root = os.environ.get("SystemRoot", r"C:\Windows")
    path = os.path.join(root, "System32", "pnputil.exe")
    if not ntpath.isabs(path):
        raise DeviceResetError("Windows SystemRoot is not an absolute path")
    return path


def _post_restart_status(target: DeviceTarget) -> tuple[bool, str]:
    """Require the same present target and a started, problem-free devnode."""
    try:
        current = resolve_target(target.slot)
    except DeviceResetError as error:
        return False, f"Windows cannot confirm that the restarted GPU is present: {error}"
    if current.instance_id != target.instance_id:
        return False, "Windows reports a different GPU at the selected PCI slot after restart"
    try:
        cfgmgr = ctypes.WinDLL("cfgmgr32", use_last_error=True)
        cfgmgr.CM_Locate_DevNodeW.argtypes = [ctypes.POINTER(ctypes.c_ulong), ctypes.c_wchar_p, ctypes.c_ulong]
        cfgmgr.CM_Locate_DevNodeW.restype = ctypes.c_ulong
        cfgmgr.CM_Get_DevNode_Status.argtypes = [ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong), ctypes.c_ulong, ctypes.c_ulong]
        cfgmgr.CM_Get_DevNode_Status.restype = ctypes.c_ulong
        devinst = ctypes.c_ulong()
        if cfgmgr.CM_Locate_DevNodeW(ctypes.byref(devinst), current.instance_id, 0) != _CR_SUCCESS:
            return False, "Windows cannot locate the restarted PnP device"
        status, problem = ctypes.c_ulong(), ctypes.c_ulong()
        if cfgmgr.CM_Get_DevNode_Status(ctypes.byref(status), ctypes.byref(problem), devinst, 0) != _CR_SUCCESS:
            return False, "Windows cannot confirm the restarted PnP device status"
        if not status.value & _DN_STARTED or problem.value:
            return False, "Windows has not confirmed that the restarted GPU is started without a device problem"
    except (AttributeError, OSError):
        return False, "Windows cannot confirm the restarted PnP device status"
    return True, "GPU PnP restart completed and Windows reports the device started"


def _pnputil_diagnostics(completed: subprocess.CompletedProcess) -> str:
    """Keep non-authoritative PnPUtil diagnostics small enough for a recovery receipt."""
    parts = []
    for label, value in (("stdout", completed.stdout), ("stderr", completed.stderr)):
        if not value:
            continue
        text = str(value).replace("\r", " ").replace("\n", " ")
        if len(text) > _PNPUTIL_DIAGNOSTIC_LIMIT:
            text = text[:_PNPUTIL_DIAGNOSTIC_LIMIT] + "…"
        parts.append(f"{label}={text!r}")
    return "; ".join(parts) or "no PnPUtil diagnostic output"


def restart_target(target: DeviceTarget, timeout: float = 30) -> RestartResult:
    """Restart exactly ``target`` once; this function never requests an OS reboot."""
    if not pnputil_restart_supported():
        return RestartResult(False, False, "PnP device restart requires Windows 10 version 2004 or later", None)
    if not is_admin():
        return RestartResult(False, False, "Administrator rights are required to restart this GPU device", None)
    try:
        current = resolve_target(target.slot)
    except DeviceResetError as error:
        return RestartResult(False, False, f"GPU target changed before restart: {error}", None)
    if current != target:
        return RestartResult(False, False, "GPU target changed before restart; no device was restarted", None)
    try:
        command = [system_pnputil(), "/restart-device", current.instance_id]
    except DeviceResetError as error:
        return RestartResult(False, False, str(error), None)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return RestartResult(False, False, "PnPUtil did not finish restarting the GPU before the timeout", None)
    except OSError as error:
        return RestartResult(False, False, f"cannot start Windows PnPUtil: {error}", None)
    if completed.returncode == 3010:
        return RestartResult(False, True, "GPU restart requires a Windows reboot; Druta will not reboot Windows", 3010)
    if completed.returncode != 0:
        message = (
            f"PnPUtil could not restart the selected GPU device (exit code {completed.returncode}; "
            f"{_pnputil_diagnostics(completed)})"
        )
        return RestartResult(False, False, message, completed.returncode)
    ok, message = _post_restart_status(current)
    return RestartResult(ok, False, message, completed.returncode)
