"""Existing board-power controls route through verified requests, without hardware."""

import struct
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from druta import client_power_runtime as cp
from druta import nvbackend as n
from tests.test_client_power_runtime import FakeGpu, put


class ReentrantLock:
    def __init__(self):
        self.lock = threading.RLock()
        self.depth = 0

    @property
    def held(self):
        return self.depth > 0

    def __enter__(self):
        self.lock.acquire()
        self.depth += 1

    def __exit__(self, *_):
        self.depth -= 1
        self.lock.release()


class BackendGpu(FakeGpu, n.GPU):
    def __init__(self, **kwargs):
        FakeGpu.__init__(self, **kwargs)
        self._lock = ReentrantLock()
        self.static = {"pl_min_mw": 200000, "pl_def_mw": 321000, "pl_max_mw": 550000}
        self.voltage_xoc_enabled = False
        self.nvml_writes = []
        self.nvml_read_status = self.nvml_write_status = 0
        self.nvml_echo = True
        self.nvml.ok, self.nvml.dev = True, object()
        self.nvml.dll = SimpleNamespace(
            nvmlDeviceGetPowerManagementLimit=self.nvml_get,
            nvmlDeviceSetPowerManagementLimit=self.nvml_set,
        )
        self.nvml.has = lambda name: hasattr(self.nvml.dll, name)
        self.nvml.errstr = lambda status: f"status {status}"
        self._power_policy_pins = {self.native_peer: 87000}
        self._reapply_power_policy_pins = Mock(return_value=(True, "pins checked"))

    def nvml_get(self, _device, pointer):
        pointer._obj.value = self.requested
        return self.nvml_read_status

    def nvml_set(self, _device, value):
        self.nvml_writes.append(value.value)
        if self.nvml_write_status == 0 and self.nvml_echo:
            self._update(value.value)
        return self.nvml_write_status

    def limits(self, minimum, rated, maximum):
        self.static.update(pl_min_mw=minimum, pl_def_mw=rated, pl_max_mw=maximum)
        at = cp.INFO_BASE + self.client_index * cp.INFO_STRIDE
        native = cp.NATIVE_INFO_BASE + self.native_index * cp.NATIVE_INFO_STRIDE
        struct.pack_into("<III", self.info, at + 4, minimum, rated, maximum)
        struct.pack_into("<III", self.native_info, native + 8, minimum, rated, maximum)


def stock_rows(gpu):
    policy = gpu.native_peer
    at = cp.NATIVE_CONTROL_BASE + policy * cp.NATIVE_CONTROL_STRIDE
    row = {
        "policy": policy,
        "named_current": False,
        "writable": True,
        "minimum": 1,
        "default": 76000,
        "maximum": 100000,
        "unit": "mW",
    }
    gpu.read_power_policies = lambda: ([{**row, "requested": cp._u32(gpu.native_control, at + 4)}], None)

    def write(index, value):
        assert index == policy
        put(gpu.native_control, at + 4, value)
        return True, "selected policy verified"

    gpu._write_policy_limit = Mock(side_effect=write)
    return policy


@pytest.mark.parametrize("requested", [200000, 321000, 549999, 550000])
def test_normal_requests_keep_nvml_and_pin_reapplication_without_live_probe(requested):
    gpu = BackendGpu()
    ok, message = gpu.set_power_limit_mw(requested)
    assert ok, message
    assert gpu.nvml_writes == [requested] and not gpu.calls
    gpu._reapply_power_policy_pins.assert_called_once_with(board_mw=requested)


@pytest.mark.parametrize("event", [{"rm": 0x56}, {"rm": 0x55}, {"guard": "after"}])
def test_failed_extension_probe_keeps_ordinary_nvml(event):
    gpu = BackendGpu()
    gpu.faults[(cp.INFO, 1)] = gpu.faults[(cp.INFO, 2)] = event
    assert gpu.power_limit_bounds_mw(xoc=True) == (200000, 550000)
    ok, message = gpu.set_power_limit_mw(322000)
    assert ok, message
    assert gpu.nvml_writes == [322000] and not gpu.set_payloads


@pytest.mark.parametrize("client,native,architecture,slot", [(63, 29, 10, "0000:a2:1c.4"), (4, 19, 8, "0000:02:00.1")])
def test_live_api_binds_alternate_adapter_and_reuses_sticky_controller(client, native, architecture, slot):
    gpu = BackendGpu(client_index=client, native_index=native, architecture=architecture, slot=slot)
    first, error = gpu.read_live_power_request()
    assert error is None and first["binding"]["native_index"] == native
    assert first["binding"]["client_index"] == client and first["identity"]["slot"] == slot
    controller = gpu._live_power_controller
    second, error = gpu.read_live_power_request()
    assert error is None and second["requested_mw"] == first["requested_mw"]
    assert gpu._live_power_controller is controller
    assert gpu._board_policy == native
    gpu.nvapi.gpu = gpu.nvapi.selected["handle"] = 1777
    failed, error = gpu.read_live_power_request()
    assert failed is None and "binding changed" in error
    assert gpu._live_power_last is None and gpu._live_power_controller is controller


def test_bounds_default_to_existing_xoc_flag_and_override_does_not_change_it():
    gpu = BackendGpu()
    assert gpu.power_limit_bounds_mw() == (200000, 550000)
    assert not gpu.calls
    assert gpu.power_limit_bounds_mw(xoc=True) == (1, 999000)
    assert not gpu.voltage_xoc_enabled
    gpu.voltage_xoc_enabled = True
    assert gpu.power_limit_bounds_mw() == (1, 999000)
    assert gpu.power_limit_bounds_mw(xoc=False) == (200000, 550000)
    assert gpu.voltage_xoc_enabled


def test_software_xoc_cap_never_shrinks_a_larger_normal_range():
    gpu = BackendGpu()
    gpu.limits(180001, 321000, 1200123)
    assert gpu.power_limit_bounds_mw(xoc=True) == (1, 1200123)
    assert gpu.power_limit_bounds_mw(xoc=False) == (180001, 1200123)


def test_transient_live_read_returns_error_not_stale_observation_but_retains_range():
    gpu = BackendGpu()
    assert gpu.power_limit_bounds_mw(xoc=True) == (1, 999000)
    baseline = gpu._live_power_last
    next_read = gpu.counts[cp.INFO] + 1
    gpu.faults[(cp.INFO, next_read)] = gpu.faults[(cp.INFO, next_read + 1)] = {"nt": -1}
    assert gpu.read_live_power_request()[0] is None
    assert gpu._live_power_last is baseline
    next_read = gpu.counts[cp.INFO] + 1
    gpu.faults[(cp.INFO, next_read)] = gpu.faults[(cp.INFO, next_read + 1)] = {"nt": -1}
    assert gpu.power_limit_bounds_mw(xoc=True) == (1, 999000)
    assert gpu._live_power_error_kind == "transient"


def test_transient_first_probe_recovers_and_optional_status_does_not_gate_extension():
    gpu = BackendGpu()
    gpu.voltage_xoc_enabled = True
    gpu.faults[(cp.INFO, 1)] = {"nt": -1}
    for occurrence in range(1, 9):
        gpu.faults[(cp.NATIVE_STATUS, occurrence)] = {"rm": 0x56}
    assert gpu.power_limit_bounds_mw() == (1, 999000)
    ok, message = gpu.set_power_limit_mw(600001)
    assert ok, message
    assert gpu._live_power_last["arbitration"] is None
    assert gpu._live_power_last["arbitration_error"]
    assert gpu.requested == 600001 and not gpu.nvml_writes


@pytest.mark.parametrize("prior", [0, cp.MISSING])
def test_unrestorable_fe_keeps_normal_bounds_and_prevents_extended_set(prior):
    gpu = BackendGpu(requested=prior)
    gpu.voltage_xoc_enabled = True
    assert gpu.power_limit_bounds_mw() == (200000, 550000)
    ok, message = gpu.set_power_limit_mw(600000)
    assert not ok and "no write" in message
    assert not gpu.nvml_writes and not gpu.set_payloads


@pytest.mark.parametrize("requested", [1, 199999, 550001, 999000])
def test_xoc_extended_request_changes_only_source_and_never_reapplies_pins(requested):
    gpu = BackendGpu()
    gpu.voltage_xoc_enabled = True
    gpu.lower_pmu = 450000
    pins = dict(gpu._power_policy_pins)
    original = bytes(gpu.native_control)
    ok, message = gpu.set_power_limit_mw(requested)
    assert ok, message
    assert "request configured" in message and "can be lower" in message
    assert gpu.requested == requested and len(gpu.set_payloads) == 1 and not gpu.nvml_writes
    gpu._reapply_power_policy_pins.assert_not_called()
    assert gpu._power_policy_pins == pins
    at = cp.NATIVE_CONTROL_BASE + gpu.native_index * cp.NATIVE_CONTROL_STRIDE + 4
    assert gpu.native_control[:at] == original[:at] and gpu.native_control[at + 4 :] == original[at + 4 :]


@pytest.mark.parametrize('pin_result', [(True, 'pins restored'), (False, 'pin restoration failed')])
def test_legacy_live_request_reapplies_user_pins_and_reports_failure(pin_result):
    gpu = BackendGpu()
    gpu.voltage_xoc_enabled = True
    after = {'requested_mw': 600000, 'binding': {'native_index': 19, 'layout': 'legacy32'}}
    gpu.read_live_power_request = Mock(return_value=({'requested_mw': 321000}, None))
    gpu._live_power_controller = SimpleNamespace(set_request=Mock(return_value={'after': after}))
    gpu._reapply_power_policy_pins = Mock(return_value=pin_result)
    ok, message = gpu.set_power_limit_mw(600000)
    assert ok is pin_result[0]
    assert pin_result[1] in message
    gpu._reapply_power_policy_pins.assert_called_once_with(board_mw=600000)


@pytest.mark.parametrize("requested", [199999, 550001, 999000])
def test_first_extension_requires_existing_xoc_opt_in(requested):
    gpu = BackendGpu()
    ok, message = gpu.set_power_limit_mw(requested)
    assert not ok and "require XOC" in message
    assert not gpu.nvml_writes and not gpu.set_payloads


@pytest.mark.parametrize("requested", [999001, 0xFFFFFFFE, 0xFFFFFFFF])
def test_requests_above_software_envelope_do_not_dispatch(requested):
    gpu = BackendGpu()
    gpu.voltage_xoc_enabled = True
    assert not gpu.set_power_limit_mw(requested)[0]
    assert not gpu.nvml_writes and not gpu.set_payloads


@pytest.mark.parametrize("value", [None, True, "600000", float("inf"), float("nan"), 600000.5, 0, -1])
def test_malformed_requests_make_no_calls(value):
    gpu = BackendGpu()
    assert not gpu.set_power_limit_mw(value)[0]
    assert not gpu.nvml_writes and not gpu.calls


@pytest.mark.parametrize(
    "current,bounds,allowed,denied",
    [(800000, (200000, 800000), 700000, 800001), (100000, (100000, 550000), 150000, 99999)],
)
def test_xoc_off_carryover_allows_only_staying_or_moving_toward_normal(current, bounds, allowed, denied):
    gpu = BackendGpu(requested=current)
    assert gpu.power_limit_bounds_mw() == bounds
    assert not gpu.set_power_limit_mw(denied)[0] and not gpu.set_payloads
    assert gpu.set_power_limit_mw(current)[0]
    assert gpu.set_power_limit_mw(allowed)[0]
    assert not gpu.nvml_writes and len(gpu.set_payloads) == 2
    assert gpu.power_limit_bounds_mw() == (min(200000, allowed), max(550000, allowed))
    assert gpu.set_power_limit_mw(321000)[0]
    assert gpu.power_limit_bounds_mw() == (200000, 550000)
    assert len(gpu.set_payloads) == 3
    assert gpu.set_power_limit_mw(322000)[0] and gpu.nvml_writes == [322000]


def test_live_return_to_normal_does_not_depend_on_nvml_getter_or_setter():
    gpu = BackendGpu(requested=800000)
    gpu.nvml_read_status = 9
    del gpu.nvml.dll.nvmlDeviceSetPowerManagementLimit
    assert gpu.read_power_limit_mw() == 800000
    ok, message = gpu.set_power_limit_mw(321000)
    assert ok, message
    assert len(gpu.set_payloads) == 1 and not gpu.nvml_writes


@pytest.mark.parametrize("missing", ["getter", "setter", "both"])
def test_normal_request_uses_live_path_when_nvml_capability_is_partial(missing):
    gpu = BackendGpu()
    if missing in ("getter", "both"):
        del gpu.nvml.dll.nvmlDeviceGetPowerManagementLimit
    if missing in ("setter", "both"):
        del gpu.nvml.dll.nvmlDeviceSetPowerManagementLimit
    ok, message = gpu.set_power_limit_mw(322001)
    assert ok, message
    assert gpu.requested == 322001 and len(gpu.set_payloads) == 1 and not gpu.nvml_writes
    gpu._reapply_power_policy_pins.assert_not_called()


@pytest.mark.parametrize("static", [{}, {"pl_min_mw": None, "pl_max_mw": None}, {"pl_def_mw": 321000}])
def test_successful_live_binding_supplies_missing_static_range_and_default(static):
    gpu = BackendGpu(client_index=63, native_index=29)
    gpu.limits(180123, 321000, 680456)
    gpu.static = dict(static)
    assert gpu.power_limit_bounds_mw(xoc=False) == (180123, 680456)
    assert gpu.static == {"pl_min_mw": 180123, "pl_def_mw": 321000, "pl_max_mw": 680456}
    assert not gpu.set_payloads and not gpu.nvml_writes


def test_partial_static_set_uses_actual_range_before_route_selection():
    gpu = BackendGpu()
    gpu.limits(180123, 321000, 680456)
    gpu.static = {}
    ok, message = gpu.set_power_limit_mw(600000)
    assert ok, message
    assert gpu.nvml_writes == [600000] and not gpu.set_payloads
    assert gpu.static["pl_max_mw"] == 680456


def test_live_metadata_does_not_replace_existing_normal_static_values():
    gpu = BackendGpu()
    gpu.static["pl_min_mw"] = 0
    gpu.read_live_power_request()
    assert gpu.static["pl_min_mw"] == 0


def test_bounds_only_retains_observed_extended_endpoint_during_transient_loss_of_both_getters():
    gpu = BackendGpu(requested=800000)
    assert gpu.power_limit_bounds_mw(xoc=False) == (200000, 800000)
    gpu.nvml_read_status = 9
    next_read = gpu.counts[cp.INFO] + 1
    gpu.faults[(cp.INFO, next_read)] = gpu.faults[(cp.INFO, next_read + 1)] = {"nt": -1}
    assert gpu.power_limit_bounds_mw(xoc=False) == (200000, 800000)
    next_read = gpu.counts[cp.INFO] + 1
    gpu.faults[(cp.INFO, next_read)] = gpu.faults[(cp.INFO, next_read + 1)] = {"nt": -1}
    assert gpu.read_power_limit_mw() is None
    assert not gpu.nvml_writes and not gpu.set_payloads


@pytest.mark.parametrize("event", [{"ignore": True}, {"rm": 0x55}, {"raise": RuntimeError("partial SET")}])
def test_live_failure_uses_one_compensation_and_never_falls_back_to_nvml(event):
    gpu = BackendGpu()
    gpu.voltage_xoc_enabled = True
    gpu.faults[(cp.SET, 1)] = event
    ok, message = gpu.set_power_limit_mw(600000)
    assert not ok and "Original request restored and verified" in message
    assert len(gpu.set_payloads) == 2 and not gpu.nvml_writes
    assert gpu.requested == gpu.original
    gpu._reapply_power_policy_pins.assert_not_called()


def test_failed_live_rollback_is_reported_without_an_extra_nvml_write():
    gpu = BackendGpu()
    gpu.voltage_xoc_enabled = True
    gpu.faults[(cp.SET, 1)] = {"rm": 0x55}
    gpu.faults[(cp.SET, 2)] = {"ignore": True, "rm": 0x56}
    ok, message = gpu.set_power_limit_mw(600000)
    assert not ok and "restoration was not verified" in message
    assert len(gpu.set_payloads) == 2 and not gpu.nvml_writes
    assert gpu.read_power_limit_mw() == 600000


def test_transient_write_preflight_does_not_use_cached_capability_as_a_request():
    gpu = BackendGpu()
    gpu.voltage_xoc_enabled = True
    assert gpu.power_limit_bounds_mw() == (1, 999000)
    next_read = gpu.counts[cp.INFO] + 1
    gpu.faults[(cp.INFO, next_read)] = gpu.faults[(cp.INFO, next_read + 1)] = {"nt": -1}
    ok, message = gpu.set_power_limit_mw(600000)
    assert not ok and "no write issued" in message
    assert not gpu.set_payloads and not gpu.nvml_writes


@pytest.mark.parametrize("target", [322000, 600000])
def test_missing_nvml_getter_and_failed_live_probe_are_not_retried_by_route_selection(target):
    gpu = BackendGpu()
    gpu.voltage_xoc_enabled = True
    del gpu.nvml.dll.nvmlDeviceGetPowerManagementLimit
    gpu.faults[(cp.INFO, 1)] = gpu.faults[(cp.INFO, 2)] = {"nt": -1}
    ok, message = gpu.set_power_limit_mw(target)
    assert not ok and "no write issued" in message
    assert gpu.counts[cp.INFO] == 2
    assert not gpu.set_payloads and not gpu.nvml_writes


def test_stock_extended_request_sets_only_selected_default_and_reports_no_rederivation():
    gpu = BackendGpu(requested=800000)
    policy = stock_rows(gpu)
    gpu._power_policy_pins[11] = 88000
    ok, message = gpu.stock_power_policy(policy)
    assert ok, message
    assert "rated default" in message and "not recomputed" in message
    assert gpu.requested == 800000 and not gpu.nvml_writes and not gpu.set_payloads
    assert gpu._power_policy_pins == {11: 88000}
    gpu._write_policy_limit.assert_called_once_with(policy, 76000)
    gpu._reapply_power_policy_pins.assert_not_called()


def test_stock_ordinary_path_still_resends_nvml_and_reapplies_other_pins():
    gpu = BackendGpu()
    policy = stock_rows(gpu)
    ok, message = gpu.stock_power_policy(policy)
    assert ok, message
    assert gpu.nvml_writes == [gpu.original] and not gpu.calls
    gpu._reapply_power_policy_pins.assert_called_once_with(board_mw=gpu.original)


@pytest.mark.parametrize("missing", ["getter", "setter"])
def test_stock_with_partial_nvml_capability_reports_default_without_rederivation(missing):
    gpu = BackendGpu()
    policy = stock_rows(gpu)
    name = "nvmlDeviceGetPowerManagementLimit" if missing == "getter" else "nvmlDeviceSetPowerManagementLimit"
    delattr(gpu.nvml.dll, name)
    ok, message = gpu.stock_power_policy(policy)
    assert ok, message
    assert "rated default" in message and "not recomputed" in message
    assert gpu.requested == gpu.original and not gpu.set_payloads and not gpu.nvml_writes


def test_stock_refuses_before_default_when_extended_board_readback_is_unavailable():
    gpu = BackendGpu(requested=800000)
    policy = stock_rows(gpu)
    gpu.faults[(cp.INFO, 1)] = {"rm": 0x56}
    ok, message = gpu.stock_power_policy(policy)
    assert not ok and "default not written" in message
    gpu._write_policy_limit.assert_not_called()
    assert not gpu.nvml_writes and not gpu.set_payloads


def test_stock_reports_changed_extended_board_request_without_sending_a_second_control():
    gpu = BackendGpu(requested=800000)
    policy = stock_rows(gpu)
    write_default = gpu._write_policy_limit.side_effect

    def changed(index, value):
        result = write_default(index, value)
        gpu._update(700000)
        return result

    gpu._write_policy_limit.side_effect = changed
    ok, message = gpu.stock_power_policy(policy)
    assert not ok and "not preserved exactly" in message
    assert not gpu.nvml_writes and not gpu.set_payloads


def test_refresh_releases_sticky_power_capability_without_a_write():
    gpu = BackendGpu()
    gpu.read_live_power_request()
    controller = gpu._live_power_controller
    gpu._refresh_volt_rail_capabilities = Mock()
    gpu.refresh_capabilities()
    assert gpu._live_power_controller is None and gpu._live_power_last is None
    assert gpu.read_live_power_request()[1] is None
    assert gpu._live_power_controller is not controller
    assert not gpu.nvml_writes and not gpu.set_payloads
