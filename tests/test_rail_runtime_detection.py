"""Runtime discovery regressions using owned buffers; never touches a GPU."""
import ctypes
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from druta.nvbackend import GPU, u32
from tests.test_volt_rails import fake_gpu


def test_transient_initial_failure_gets_one_immediate_read_retry():
    gpu = fake_gpu()
    original = gpu.nvapi.VoltRailsCtlGet
    calls = []

    def transient(handle, buf):
        p = ctypes.cast(buf, ctypes.POINTER(u32))
        calls.append(p[1])
        return -1 if len(calls) == 1 else original(handle, buf)

    gpu.nvapi.VoltRailsCtlGet = transient
    assert gpu.volt_rail_limits_supported(0)
    assert calls[:2] == [1, 1]


def test_empty_discovery_recovers_after_backoff_without_losing_initial_state():
    gpu = fake_gpu()
    original = gpu.nvapi.VoltRailsCtlGet
    failed = Mock(return_value=-1)
    gpu.nvapi.VoltRailsCtlGet = failed
    with patch("druta.nvbackend.time.monotonic", return_value=10):
        assert not gpu.volt_rail_limits_supported()
        count = failed.call_count
        assert not gpu.volt_rail_limits_supported()
        assert failed.call_count == count
    gpu.nvapi.VoltRailsCtlGet = original
    with patch("druta.nvbackend.time.monotonic", return_value=13):
        assert gpu.volt_rail_limits_supported(0)
    initial = dict(gpu._volt_rail_initial_uv)
    gpu.nvapi.control[0][0] = 1000
    gpu.nvapi.sync_live()
    gpu._refresh_volt_rail_capabilities()
    assert gpu.volt_rail_limits_supported(0)
    assert gpu._volt_rail_initial_uv == initial


def test_unknown_absolute_layout_does_not_borrow_v1_discriminator():
    gpu = fake_gpu()
    gpu.nvapi.VoltRailsAbs = Mock(return_value=-9)
    assert not gpu.volt_rail_limits_supported(0)
    assert all(ctypes.cast(call.args[1], ctypes.POINTER(u32))[0] == 0x10AC8
               for call in gpu.nvapi.VoltRailsAbs.call_args_list)
    assert not gpu.volt_rail_diagnostics()["available"]
    gpu._write_rail_records.assert_not_called()


def test_diagnostics_identify_missing_architecture_without_writes():
    gpu = fake_gpu(architecture=None)
    report = gpu.volt_rail_diagnostics()
    json.dumps(report)  # Support output must contain data, not bound methods.
    assert not report["available"]
    assert "architecture" in report["reason"]
    assert report["getters"]["VoltRailsAbs"][0]["valid"]
    gpu._write_rail_records.assert_not_called()


def test_previously_read_rail_is_not_evicted_by_transient_error():
    gpu = fake_gpu()
    assert gpu.volt_rail_limits_supported(0)
    original = gpu.nvapi.VoltRailsCtlGet
    gpu.nvapi.VoltRailsCtlGet = Mock(return_value=-1)
    assert gpu.read_volt_rail_limits() is None
    gpu.nvapi.VoltRailsCtlGet = original
    assert gpu.volt_rail_limits_supported(0)


def exercise_native_write(*, legacy_packet=False, native_976=False,
                          unrelated_first=False, getter_version=0x20AC8,
                          ntstatus=0, rm_status=0, set_ntstatus=0, set_rm_status=0,
                          set_exception=False, response_params_size=None):
    gpu = fake_gpu()
    gpu.volt_rail_limits_supported = Mock(return_value=True)
    gpu._volt_rail_profile = lambda: {"control_version": getter_version,
                                    "fields": {0: GPU.VOLT_LIMIT_FIELDS}}
    if native_976:
        size, params, cmd, mask_word = 976, 908, 0x2080B213, 18
    elif legacy_packet:
        size, params, cmd, mask_word = 716, 648, 0x20803213, 17
    else:
        size, params, cmd, mask_word = 1104, 1036, 0x2080B213, 18
    payload = (u32 * (size // 4))()
    payload[14], payload[15], payload[mask_word] = cmd, params, 1
    escape = GPU._Escape(pPrivateDriverData=ctypes.addressof(payload),
                         PrivateDriverDataSize=size)
    unrelated = (u32 * (size // 4))()
    unrelated[14] = 0x20800123
    other_escape = GPU._Escape(pPrivateDriverData=ctypes.addressof(unrelated),
                               PrivateDriverDataSize=size)
    original_other = bytes(unrelated)
    code = ctypes.create_string_buffer(b"original bytes")
    original_code = bytes(code)
    callback_buffer = ctypes.create_string_buffer(14)
    address = ctypes.addressof(code)
    sent = []

    def real(pesc):
        assert bytes(code) == original_code
        e = GPU._Escape.from_address(pesc)
        sent.append(ctypes.string_at(e.pPrivateDriverData, e.PrivateDriverDataSize))
        words = ctypes.cast(e.pPrivateDriverData, ctypes.POINTER(u32))
        if words[14] == cmd:
            words[16] = rm_status
            if native_976:
                # A completed GET supplies its valid mask and full seven-word
                # records. The SET must retain the opaque record tail without
                # copying the GET-only valid-mask header.
                words[17] = 0xFF
                words[20] = 2
                words[25] = 0x11223344
                words[26] = 0xAABBCCDD
            if response_params_size is not None:
                words[15] = response_params_size
            return ntstatus
        if words[14] in (0x20803214, 0x2080F214):
            if set_exception:
                raise RuntimeError("SET call outcome unavailable")
            words[16] = set_rm_status
            return set_ntstatus
        return 0

    def prototype(*args):
        def bind(target):
            if callable(target):
                def getter(handle, buf):
                    assert handle is gpu.nvapi.gpu
                    assert ctypes.cast(buf, ctypes.POINTER(u32))[0] == getter_version
                    if unrelated_first:
                        target(ctypes.addressof(other_escape))
                        assert bytes(code) != original_code, "rearm after unrelated call"
                    return target(ctypes.addressof(escape))
                gpu.nvapi.VoltRailsCtlGet = getter
                return ctypes.c_void_p(ctypes.addressof(callback_buffer))
            assert target == address
            return real
        return bind

    kernel = SimpleNamespace(VirtualProtect=Mock(return_value=1),
                             GetCurrentThreadId=Mock(return_value=731))
    gdi = SimpleNamespace(D3DKMTEscape=ctypes.c_void_p(address))
    with patch("druta.nvbackend.ctypes.WinDLL", new=lambda name, **kw:
               gdi if name == "gdi32.dll" else kernel), \
            patch("druta.nvbackend.ctypes.WINFUNCTYPE", new=prototype):
        result = GPU._write_rail_records_locked(gpu, {0: [-12500, 0, 0, 0]})
    assert bytes(code) == original_code
    assert bytes(unrelated) == original_other
    return result, gpu, sent


def test_public_getter_version_does_not_choose_private_write_packet():
    for legacy, version in ((True, 0x20AC8), (False, 0x10AC8)):
        result, gpu, sent = exercise_native_write(legacy_packet=legacy, getter_version=version)
        assert result == (True, 0)
        assert len(sent) == 2
        assert sent[0][14 * 4:15 * 4] != sent[-1][14 * 4:15 * 4]
        assert gpu._volt_rail_write_protocol["set_command"] == (
            "0x20803214" if legacy else "0x2080F214")


def test_unrelated_escape_does_not_consume_rail_detection():
    result, gpu, sent = exercise_native_write(unrelated_first=True)
    assert result == (True, 0)
    assert len(sent) == 3
    assert len(gpu._volt_rail_observed_packets) == 2


def test_native_transport_failure_is_not_reported_as_success():
    result, _, sent = exercise_native_write(ntstatus=-1)
    assert result == (False, None)
    assert len(sent) == 1


def test_native_rm_failure_does_not_issue_set():
    result, _, sent = exercise_native_write(rm_status=0x57)
    assert result == (False, None)
    assert len(sent) == 1


def test_changed_response_geometry_does_not_issue_set():
    result, _, sent = exercise_native_write(response_params_size=904)
    assert result == (False, None)
    assert len(sent) == 1


def test_976_set_uses_original_header_and_completed_get_records():
    result, _, sent = exercise_native_write(native_976=True)
    assert result == (True, 0)
    assert len(sent) == 2
    request = (u32 * (976 // 4)).from_buffer_copy(sent[0])
    written = (u32 * (976 // 4)).from_buffer_copy(sent[-1])
    signed = ctypes.cast(written, ctypes.POINTER(ctypes.c_int32))
    assert request[14] == 0x2080B213
    assert written[14] == 0x2080F214
    assert request[17] == written[17] == 0
    assert written[18] == 1
    assert written[19] == 37
    assert written[20] == 2
    assert [signed[21 + index] for index in range(4)] == [-12500, 0, 0, 0]
    assert written[25:27] == [0x11223344, 0xAABBCCDD]


def test_native_set_failures_never_become_success():
    for kwargs, expected in (({"set_ntstatus": -1}, 0xFFFFFFFF),
                             ({"set_rm_status": 0x57}, 0x57),
                             ({"set_exception": True}, 0xFFFFFFFF)):
        result, gpu, sent = exercise_native_write(**kwargs)
        assert result == (True, expected)
        assert len(sent) == 2
        if kwargs.get("set_exception"):
            assert "outcome unavailable" in gpu._volt_rail_write_error
