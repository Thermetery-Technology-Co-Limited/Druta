"""Offline collector behavior; no NVAPI/NVML or production hardware imports."""

import copy
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "experiments" / "slcg_live_validation.py"
spec = importlib.util.spec_from_file_location("slcg_validation", SCRIPT)
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)
SLOT = "0000:01:00.0"


class FakeGPU:
    def __init__(self):
        self.calls = []

    def _legacy_clk_escape(self, packet, fields):
        self.calls.append(list(packet))
        packet[11] = 1
        return 0


class FakeController:
    def __init__(self, gpu, *, failure=None):
        self.gpu = gpu
        self.identity = {"slot": SLOT, "architecture": 7, "device_id": 1234, "subsystem_id": 5678}
        self.failure = failure
        self.owned = False
        self.opened = False
        self.value = 0
        self.close_calls = 0
        self.read_calls = 0

    def read(self):
        self.read_calls += 1
        return {
            "support": True,
            "architecture": {"architecture": 0x170, "implementation": 4},
            "layout": "ga10x-slcg",
            "registers": {"0x248018": self.value},
            "unavailable_registers": {"0x251a18": {"classification": "access_denied", "operation_status": 32}},
            "expected_disabled_values": {"0x248018": 3, "0x251a18": 3},
            "owned_request": self.owned,
            "uncertain_outcome": False,
        }

    def _send(self, kind):
        size = {"allocate": 22, "request": 20, "release": 18, "free": 16}[kind]
        packet = [0] * size
        if kind == "allocate":
            packet[4], packet[15] = 0x0500002A, 0xB2CC
        elif kind == "free":
            packet[4] = 0x0500000C
        elif kind == "request":
            packet[14], packet[18] = 0xB0CC0301, 16
        else:
            packet[14], packet[17] = 0xB0CC0302, 48
        self.gpu._legacy_clk_escape(packet, {"adapter": 9})

    def hold(self):
        self._send("allocate")
        self.opened = True
        self._send("request")
        self.owned = True
        if self.failure == "hold":
            raise RuntimeError("lost request response")
        self.value = 0 if self.failure == "noop" else 3
        return self.read()

    def release(self):
        self._send("release")
        self.owned = False
        self.value = 0
        self._send("free")
        self.opened = False
        return self.read()

    def close(self):
        self.close_calls += 1
        if self.failure == "cleanup":
            raise RuntimeError("cleanup failed")
        return self.release() if self.opened else None


@pytest.fixture
def run(monkeypatch):
    monkeypatch.setattr(
        collector.util,
        "_pci_identity",
        lambda gpu: {"requested_slot": SLOT, "selected_slot": SLOT, "pairing_error": ""},
    )

    def execute(*, trial=False, failure=None, before_value=0, native=0x170):
        gpu = FakeGPU()
        controller = FakeController(gpu, failure=failure)
        controller.value = before_value
        read = controller.read

        def snapshot():
            state = read()
            state["architecture"]["architecture"] = native
            return state

        controller.read = snapshot
        original = gpu._legacy_clk_escape
        checkpoints = []
        report = collector.collect(
            gpu,
            SLOT,
            trial=trial,
            controller_factory=lambda gpu: controller,
            checkpoint=lambda value: checkpoints.append(copy.deepcopy(value)),
            source=lambda: {"files_sha256": {"test": "abc"}},
        )
        assert gpu._legacy_clk_escape == original
        return report, controller, gpu, checkpoints

    return execute


def test_default_read_only_has_no_mutation_packets(run):
    report, controller, gpu, _ = run()
    assert not gpu.calls and report["events"] == []
    assert controller.close_calls == 1
    assert report["read_success"] is True
    assert report["qualified"] is False
    assert report["before"]["unavailable_registers"]


def test_one_trial_records_exact_packets_and_positive_transition(run):
    report, controller, gpu, checkpoints = run(trial=True)
    assert report["qualified"] is True
    assert len(gpu.calls) == 4
    assert report["event_counts"] == {"allocate": 1, "request": 1, "release": 1, "free": 1}
    assert report["events"][1]["packet_before"][18] == 16
    assert report["events"][1]["packet_after"][11] == 1
    assert report["held"]["registers"] == {"0x248018": 3}
    assert report["after_cleanup"]["value"]["registers"] == {"0x248018": 0}
    assert controller.close_calls == 1
    assert checkpoints[0]["events"] == []


def test_hold_failure_always_closes_own_request_and_reads_cleanup(run):
    report, controller, gpu, _ = run(trial=True, failure="hold")
    assert report["qualified"] is False
    assert report["errors"][0]["phase"] == "hold"
    assert controller.close_calls == 1 and not controller.opened
    assert len(gpu.calls) == 4
    assert report["cleanup"]["ok"] is True
    assert report["after_cleanup"]["ok"] is True


def test_noop_never_qualifies(run):
    report, _, _, _ = run(trial=True, failure="noop")
    assert report["qualified"] is False
    assert report["checks"]["positive_transition"] is False


@pytest.mark.parametrize("kwargs", [{"before_value": 3}, {"native": 0x1B0}])
def test_trial_requires_zero_baseline_and_native170(run, kwargs):
    report, controller, gpu, _ = run(trial=True, **kwargs)
    assert report["qualified"] is False
    assert not gpu.calls
    assert controller.close_calls == 1


def test_cleanup_error_is_recorded_with_independent_cleanup_read(run):
    report, _, _, _ = run(trial=True, failure="cleanup")
    assert report["qualified"] is False
    assert report["cleanup"]["ok"] is False
    assert report["after_cleanup"]["ok"] is True


def test_existing_output_is_rejected_before_hardware_import(tmp_path):
    output = tmp_path / "existing.json"
    output.write_text("preserve")
    before = set(sys.modules)
    with pytest.raises(SystemExit, match="2"):
        collector.main(["--gpu", SLOT, "--out", str(output), "--trial"])
    assert output.read_text() == "preserve"
    assert not any(name.startswith("druta") for name in set(sys.modules) - before)


def test_required_target_and_help_never_import_hardware():
    before = set(sys.modules)
    with pytest.raises(SystemExit, match="0"):
        collector.main(["--help"])
    with pytest.raises(SystemExit, match="2"):
        collector._parse_args(["--out", "new.json"])
    assert not any(name.startswith("druta") for name in set(sys.modules) - before)
