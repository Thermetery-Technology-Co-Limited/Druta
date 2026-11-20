# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Boundary failures of the front-end ABI, independent of the Python facade."""
import ctypes

import pytest

from druta import native
from tests.fake_driver import TITAN, XP, ClkDomRig, FakeNvml, install, write_u32

if not native.available():
    pytest.skip("native core not built (native/build.sh linux)", allow_module_level=True)


def entry(name, *args):
    # A local prototype lets malformed-pointer tests leave the binding's shared function untouched.
    address = ctypes.cast(getattr(native.load(), name), ctypes.c_void_p).value
    return ctypes.CFUNCTYPE(ctypes.c_int, *args)(address)


@pytest.fixture(autouse=True)
def clear_resolver():
    yield
    native.set_export_resolver(None)


def test_envelope_bad_size_is_refused_without_overwriting_caller():
    get = entry("dc_request_envelopes_get", ctypes.c_void_p)
    output = (ctypes.c_uint32 * 80)(*([0xA5A5A5A5] * 80))
    output[0] = 4
    before = bytes(output)
    assert get(output) == native.DC_E_ARGUMENT
    assert bytes(output) == before
    assert get(None) == native.DC_E_ARGUMENT


def test_export_lookup_absence_arguments_and_resolver_replacement():
    get = entry("dc_export_address", ctypes.c_char_p, ctypes.c_char_p,
                ctypes.POINTER(ctypes.c_void_p))
    first = FakeNvml([TITAN])
    install(first, None)
    out = ctypes.c_void_p()
    assert get(b"nvml", b"nvmlInit_v2", ctypes.byref(out)) == native.DC_OK
    assert out.value == ctypes.cast(first.exports["nvmlInit_v2"], ctypes.c_void_p).value
    assert get(b"nvml", b"absent", ctypes.byref(out)) == native.DC_E_UNRECOGNIZED
    assert out.value is None
    for library, name in ((None, b"x"), (b"gdi32", b"x"), (b"nvml", None), (b"nvml", b"")):
        assert get(library, name, ctypes.byref(out)) == native.DC_E_ARGUMENT
    assert get(b"nvml", b"nvmlInit_v2", None) == native.DC_E_ARGUMENT
    second = FakeNvml([XP])
    install(second, None)
    assert get(b"nvml", b"nvmlInit_v2", ctypes.byref(out)) == native.DC_OK
    assert out.value == ctypes.cast(second.exports["nvmlInit_v2"], ctypes.c_void_p).value
    install(None, None)
    assert get(b"nvml", b"nvmlInit_v2", ctypes.byref(out)) == native.DC_E_UNRECOGNIZED
    assert out.value is None


def test_nvml_device_comes_from_selected_slot_and_is_absent_when_disabled():
    get = entry("dc_gpu_nvml_device", ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
    install(FakeNvml([XP, TITAN]), None)
    out = ctypes.c_void_p()
    with native.Gpu("0000:01:00.0") as gpu:
        assert get(gpu._handle, ctypes.byref(out)) == native.DC_OK
        assert out.value == 0x1001
        assert get(gpu._handle, None) == native.DC_E_ARGUMENT
    with native.Gpu("0000:03:00.0") as gpu:
        assert get(gpu._handle, ctypes.byref(out)) == native.DC_OK
        assert out.value is None
    assert get(None, ctypes.byref(out)) == native.DC_E_ARGUMENT


def test_msvdd_count_query_small_buffer_and_sorted_session_snapshot():
    get = entry("dc_gpu_msvdd_domains_get", ctypes.c_void_p, ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32), ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t))
    rig = ClkDomRig(preload={0: {"msvdd": 2000}, 3: {"msvdd": 10000}})
    with rig.native_gpu() as gpu:
        count = ctypes.c_size_t(99)
        assert get(gpu._handle, 0, None, 0, ctypes.byref(count)) == native.DC_OK
        assert count.value == 0
        gpu.clkdom_layout()
        gpu.msvdd_write_enabled = True
        assert gpu.set_rail_offset_mv(20, 3, rail=1)[0]
        assert gpu.set_rail_offset_mv(10, 0, rail=1)[0]
        assert get(gpu._handle, 0, None, 0, ctypes.byref(count)) == native.DC_E_BUFFER
        assert count.value == 2
        out = (ctypes.c_uint32 * 3)(91, 92, 93)
        assert get(gpu._handle, 0, out, 1, ctypes.byref(count)) == native.DC_E_BUFFER
        assert list(out) == [91, 92, 93]
        assert get(gpu._handle, 0, out, 3, ctypes.byref(count)) == native.DC_OK
        assert count.value == 2 and list(out) == [0, 3, 93]
        before = bytes(out)
        for which, pointer, capacity, length in ((2, out, 3, ctypes.byref(count)),
                                                 (0, None, 1, ctypes.byref(count)),
                                                 (0, out, 3, None)):
            assert get(gpu._handle, which, pointer, capacity, length) == native.DC_E_ARGUMENT
        assert bytes(out) == before


def test_enforced_limit_is_one_fresh_call_and_keeps_successful_zero():
    get = entry("dc_pl_enforced_read", ctypes.c_void_p, ctypes.POINTER(ctypes.c_int32),
                ctypes.POINTER(ctypes.c_uint32))
    reads = []
    values = iter(((0, 123456), (7, 999), (0, 0)))

    @ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
    def enforced(device, pointer):
        reads.append(device)
        status, value = next(values)
        write_u32(pointer, value)
        return status

    nvml = FakeNvml([XP, TITAN], extra={"nvmlDeviceGetEnforcedPowerLimit": enforced})
    install(nvml, None)
    with native.Gpu("0000:01:00.0") as gpu:
        has, mw = ctypes.c_int32(), ctypes.c_uint32()
        for expected in ((1, 123456), (0, 0), (1, 0)):
            assert get(gpu._handle, ctypes.byref(has), ctypes.byref(mw)) == native.DC_OK
            assert (has.value, mw.value) == expected
        assert reads == [0x1001] * 3
        assert get(None, ctypes.byref(has), ctypes.byref(mw)) == native.DC_E_ARGUMENT
        assert get(gpu._handle, None, ctypes.byref(mw)) == native.DC_E_ARGUMENT
        assert get(gpu._handle, ctypes.byref(has), None) == native.DC_E_ARGUMENT
        del nvml.exports["nvmlDeviceGetEnforcedPowerLimit"]
        assert get(gpu._handle, ctypes.byref(has), ctypes.byref(mw)) == native.DC_OK
        assert (has.value, mw.value) == (0, 0)
        assert reads == [0x1001] * 3
