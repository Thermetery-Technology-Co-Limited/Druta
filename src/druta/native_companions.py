# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""NVAPI and NVML views over the native core's loader and paired handles.

These objects never load or enumerate another driver. The box keeps the core
handle alive for every view and cached callable; closing the Gpu invalidates
later calls, including functions a caller saved before close. Calls deliberately
take no card lock, matching nvbackend's companions. A caller's ``with gpu._lock``
still serializes its whole sequence against the core.
"""
import ctypes
import inspect

from .nvbackend import NVAPI_INTERFACES, NvAPI, Nvml

_INTERFACES = {name: (identifier, argtypes) for name, identifier, argtypes in NVAPI_INTERFACES}
_MISSING = object()


class _CdeclFunction(ctypes._CFuncPtr):
    # CDLL creates this same function-pointer type, with no fixed argument
    # prototype. CFUNCTYPE(c_int) would retain an empty argument-type tuple.
    _flags_ = ctypes._FUNCFLAG_CDECL
    _restype_ = ctypes.c_int


def _require_open(box):
    if box.closed:
        from .native import NativeClosed
        raise NativeClosed()


def _address(box, library, name):
    from .native import DC_E_UNRECOGNIZED, DC_OK, _raise
    _require_open(box)
    address = ctypes.c_void_p()
    status = box.library.dc_export_address(library.encode(), name.encode(), ctypes.byref(address))
    if status == DC_E_UNRECOGNIZED:
        return None
    if status != DC_OK:
        _raise(status)
    return address.value


class _BoundFunction:
    """A cached cdecl function with ctypes' mutable signature and box lifetime."""

    def __init__(self, box, function):
        self._box, self._function = box, function

    def __call__(self, *args):
        box = self._box
        _require_open(box)
        return self._function(*args)

    @property
    def restype(self):
        return self._function.restype

    @restype.setter
    def restype(self, value):
        self._function.restype = value

    @property
    def argtypes(self):
        return self._function.argtypes

    @argtypes.setter
    def argtypes(self, value):
        self._function.argtypes = value

    def __getattr__(self, name):
        return getattr(self._function, name)


class BoundNvAPI:
    """The core's selected NVAPI half, including the shared named interfaces."""

    ver = staticmethod(NvAPI.ver)

    def __init__(self, box, ok, err_detail, selected):
        self._box, self._ok = box, ok
        self.err_detail, self.selected = err_detail, selected
        self._gpu = (selected or {}).get("handle")
        self._named = {}
        self._query = None
        address = _address(box, "nvapi", "nvapi_QueryInterface")
        if address:
            self._query = _BoundFunction(box, ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_uint32)(address))

    @property
    def ok(self):
        return bool(self._ok and not self._box.closed)

    @property
    def gpu(self):
        return self._gpu if self.ok else None

    def _i(self, identifier, *argtypes):
        if self._box.closed or self._query is None:
            return None
        address = self._query(identifier)
        if not address:
            return None
        function = ctypes.CFUNCTYPE(ctypes.c_int, *argtypes)(address)
        return _BoundFunction(self._box, function)

    def __getattribute__(self, name):
        try:
            return object.__getattribute__(self, name)
        except AttributeError:
            # Named exports are lazy attributes. A descriptor that exists and
            # failed must keep its original exception, rather than looking like
            # a missing export or being called a second time.
            if name in _INTERFACES and inspect.getattr_static(type(self), name, _MISSING) is _MISSING:
                return object.__getattribute__(self, "_named_interface")(name)
            from .native_frontend import record_missing_member
            record_missing_member(self, name)
            raise

    def _named_interface(self, name):
        if self._box.closed:
            return None
        if name not in self._named:
            identifier, argtypes = _INTERFACES[name]
            self._named[name] = self._i(identifier, *argtypes)
        return self._named[name]


class _NvmlDLL:
    """CDLL-style lazy export lookup through dc_export_address, never CDLL(path)."""

    def __init__(self, box):
        self._box, self._functions = box, {}

    def __getattr__(self, name):
        _require_open(self._box)
        if name not in self._functions:
            address = _address(self._box, "nvml", name)
            if address is None:
                raise AttributeError(f"NVML export {name!r} is unavailable")
            # Like CDLL, default to int return and unspecified arguments.
            function = _CdeclFunction(address)
            self._functions[name] = _BoundFunction(self._box, function)
        return self._functions[name]


class BoundNvml:
    """The selected NVML device and callable exports from the core's module."""

    ver = staticmethod(Nvml.ver)

    def __init__(self, box, ok, err_detail, selected, dll_path):
        from .native import DC_OK, _raise
        self._box, self._ok = box, ok
        self.err_detail, self.selected, self.dll_path = err_detail, selected, dll_path
        self.dll = _NvmlDLL(box)
        self._device = ctypes.c_void_p()
        status = box.library.dc_gpu_nvml_device(box.handle, ctypes.byref(self._device))
        if status != DC_OK:
            _raise(status)
        if self.has("nvmlErrorString"):
            self.dll.nvmlErrorString.restype = ctypes.c_char_p

    @property
    def ok(self):
        return bool(self._ok and not self._box.closed)

    @property
    def dev(self):
        return self._device if self.ok and self._device.value else None

    def has(self, name):
        if self._box.closed:
            return False
        try:
            getattr(self.dll, name)
            return True
        except AttributeError:
            return False

    def errstr(self, status):
        try:
            return self.dll.nvmlErrorString(status).decode(errors="replace")
        except Exception:
            return str(status)

    def __getattribute__(self, name):
        try:
            return object.__getattribute__(self, name)
        except AttributeError:
            from .native_frontend import record_missing_member
            record_missing_member(self, name)
            raise
