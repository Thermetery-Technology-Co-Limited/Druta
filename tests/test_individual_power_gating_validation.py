"""Offline collector behavior; no device access."""

import ctypes
import importlib.util
import struct
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "experiments" / "individual_power_gating_validation.py"
spec = importlib.util.spec_from_file_location("individual_validation", SCRIPT)
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)
SLOT = "0000:01:00.0"


class FakeGPU:
    def __init__(self):
        self.calls = []

    def _legacy_clk_escape(self, packet, fields):
        self.calls.append(bytes(packet))
        data = (ctypes.c_ubyte * 0x404).from_address(ctypes.addressof(packet) + 68)
        struct.pack_into("<H", data, 10, 1)
        return 0


class FakeController:
    def __init__(self, gpu, index, failure=None):
        self.gpu, self.index, self.failure = gpu, index, failure
        self.identity = {"slot": SLOT}
        self.mask, self.owned, self.close_calls = 0x20, False, 0

    def read(self):
        if self.failure == "selected_read":
            raise RuntimeError("selected read unavailable")
        return {"support": self.failure != "unsupported", "reason_masks": {0x48: self.mask, 0x49: 4},
                "owned_request": self.owned, "uncertain_outcome": False}

    def send(self, value):
        packet = (ctypes.c_uint32 * (17 + 0x404 // 4))()
        packet[14], packet[15] = 0x2080E802, 0x404
        payload = (ctypes.c_ubyte * 0x404).from_address(ctypes.addressof(packet) + 68)
        struct.pack_into("<IHHHHII", payload, 0, 1, 3, self.index, 0x37, 0, value, 0)
        self.gpu._legacy_clk_escape(packet, {})

    def hold(self):
        self.send(0)
        self.owned = True
        if self.failure != "noop":
            self.mask |= 1
        if self.failure == "hold":
            raise RuntimeError("response lost")
        return self.read()

    def release(self):
        self.send(1)
        self.mask &= ~1
        self.owned = False
        return self.read()

    def close(self):
        self.close_calls += 1
        if self.owned:
            self.release()


@pytest.fixture
def run(monkeypatch):
    monkeypatch.setattr(collector.util, "_pci_identity", lambda gpu: {
        "requested_slot": SLOT, "selected_slot": SLOT, "pairing_error": ""
    })

    def execute(*, trial=False, failure=None, missing_peer=None, changed_peer=None, cleanup_peer=None, index=None):
        gpu = FakeGPU()
        controller = FakeController(gpu, 12, failure)

        def peer_read(owner, index):
            if index == missing_peer:
                raise RuntimeError("peer unavailable")
            mask = (controller.mask if index == 12 else
                    int((index == changed_peer and controller.owned) or (index == cleanup_peer and controller.close_calls)))
            return {"57": {"available": True, "value": 1}, "72": {"available": True, "value": mask},
                    "73": {"available": True, "value": 4}}

        report = collector.collect(gpu, SLOT, trial=trial, index=12 if trial else index,
                                   controller_factory=lambda gpu, index: controller,
                                   source=lambda: {"revision": "test"}, peer_read=peer_read)
        return report, controller, gpu

    return execute


def test_default_inventory_is_read_only(run):
    report, controller, gpu = run()
    assert report["read_success"] and not report["qualified"]
    assert set(report["inventory_before"]) == {str(i) for i in range(22)}
    assert not gpu.calls and controller.close_calls == 1


def test_trial_records_exact_packets_and_balanced_transition(run):
    report, controller, gpu = run(trial=True)
    assert report["qualified"] and report["peer_check"] == {"status": "verified", "unchanged": True}
    assert len(gpu.calls) == 2 and controller.close_calls == 1
    for event, value in zip(report["events"], (0, 1), strict=True):
        request, response = bytes.fromhex(event["request_hex"]), bytes.fromhex(event["response_hex"])
        assert struct.unpack_from("<IHHHHII", request) == (1, 3, 12, 0x37, 0, value, 0)
        assert struct.unpack_from("<H", response, 10)[0] == 1


def test_unavailable_peer_is_reported_without_hiding_selected_result(run):
    report, _, _ = run(trial=True, missing_peer=9)
    assert report["qualified"] and report["peer_check"] == {"status": "unverified", "unchanged": None}


def test_observed_peer_change_prevents_full_trial_qualification(run):
    report, _, _ = run(trial=True, changed_peer=9)
    assert not report["qualified"] and report["peer_check"] == {"status": "verified", "unchanged": False}


def test_missing_peer_does_not_hide_another_peer_change(run):
    report, _, _ = run(trial=True, missing_peer=9, changed_peer=10)
    assert not report["qualified"] and report["peer_check"] == {"status": "unverified", "unchanged": False}


def test_cleanup_peer_change_is_compared(run):
    report, _, _ = run(trial=True, cleanup_peer=9)
    assert not report["qualified"] and report["peer_check"] == {"status": "verified", "unchanged": False}


def test_explicit_selected_read_failure_is_not_read_success(run):
    report, _, gpu = run(failure="selected_read", index=12)
    assert not report["read_success"] and not report["selected_read"]["ok"] and not gpu.calls


@pytest.mark.parametrize("failure", ["unsupported", "noop", "hold"])
def test_failed_trial_is_not_qualified_and_owned_reference_is_closed(run, failure):
    report, controller, gpu = run(trial=True, failure=failure)
    assert not report["qualified"] and controller.close_calls == 1 and not controller.owned
    assert len(gpu.calls) == (0 if failure == "unsupported" else 2)


def test_cli_requires_explicit_trial_target_and_new_output(tmp_path):
    out = tmp_path / "result.json"
    with pytest.raises(SystemExit):
        collector._parse_args(["--gpu", SLOT, "--out", str(out), "--trial"])
    with pytest.raises(SystemExit):
        collector._parse_args(["--gpu", SLOT, "--out", str(out), "--trial", "--controller", "9"])
    out.write_text("existing")
    with pytest.raises(SystemExit):
        collector._parse_args(["--gpu", SLOT, "--out", str(out)])
    assert out.read_text() == "existing"
