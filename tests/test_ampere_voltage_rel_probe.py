"""Bounded offline experiment orchestration; no hardware modules are imported."""

import copy
import importlib.util
import json
import struct
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "experiments" / "ampere_voltage_rel_probe_20261101.py"
spec = importlib.util.spec_from_file_location("ampere_rel_probe", SCRIPT)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


@pytest.fixture
def harness(monkeypatch):
    inventory = json.loads((ROOT / "experiments" / "ampere-voltage-inventory-20261101-181618.json").read_text())
    state = copy.deepcopy(inventory["read_rounds"][0]["voltage_request_replay"]["observation"])
    identity = state["identity"]
    pci = inventory["pci_identity_before"]["value"]
    bracket = copy.deepcopy(inventory["before"])
    bracket["vbios_image"].pop("image_hex", None)
    source = {"files_sha256": {"test": "abc"}}
    calls, checkpoints = [], []

    class PartialError(RuntimeError):
        attempted = True
        kind = "partial"

        def __init__(self, result):
            super().__init__("SET returned 0; unchanged from before")
            self.result = result

    class Replay:
        HEADER, CONTROL_STRIDE = 12, 32
        controls = SimpleNamespace(ROW_OFFSET=72, ROW_STRIDE=84)
        ignored = False
        restoration_fails = False

        def validate(self, saved):
            return copy.deepcopy(saved)

        def project(self, saved, *, offsets_uv):
            desired = copy.deepcopy(saved)
            for rail, words in offsets_uv.items():
                for key, at in (("native_hex", 12 + int(rail) * 32 + 4), ("public", 72 + int(rail) * 84 + 4)):
                    raw = bytearray.fromhex(saved[key][rail] if key == "public" else saved[key])
                    struct.pack_into("<6i", raw, at, *words)
                    if key == "public":
                        desired[key][rail] = raw.hex()
                    else:
                        desired[key] = raw.hex()
            return desired

        def read(self, gpu):
            return copy.deepcopy(state), None

        def replay(self, gpu, desired):
            # Assert that baseline + exact desired were durably checkpointed before trial.
            assert checkpoints and checkpoints[0]["baseline"] and checkpoints[0]["desired"]
            calls.append(copy.deepcopy(desired))
            before = copy.deepcopy(state)
            if not self.ignored and not (len(calls) == 2 and self.restoration_fails):
                state.update(copy.deepcopy(desired))
                # Stored request changes, independent policy_uv intentionally does not.
                state["policy"]["rails"]["0"]["offsets_uv"]["reliability"] = probe._words(state, "0", self)[0]
            result = {
                "attempted": True,
                "status": 0,
                "before": before,
                "desired": desired,
                "after": copy.deepcopy(state),
                "verified": probe._request(state) == probe._request(desired),
            }
            if not result["verified"]:
                raise PartialError(result)
            return result

    replay = Replay()
    policy = SimpleNamespace(
        read=lambda gpu, **kw: (copy.deepcopy(state["policy"]), None),
        validate_policy=lambda value, **kw: copy.deepcopy(value),
    )
    gate = SimpleNamespace(read=lambda: {"enabled": True, "identity": identity})
    monkeypatch.setattr(probe.util, "_pci_identity", lambda gpu: copy.deepcopy(pci))
    monkeypatch.setattr(probe.util, "_bracket", lambda gpu, registry: copy.deepcopy(bracket))

    def run():
        return probe.run_probe(
            object(),
            identity["slot"],
            "0",
            replay=replay,
            policy=policy,
            registry=object(),
            gate=gate,
            checkpoint=lambda value: checkpoints.append(copy.deepcopy(value)),
            source=lambda: copy.deepcopy(source),
        )

    return SimpleNamespace(
        run=run, replay=replay, calls=calls, checkpoints=checkpoints, state=state, gate=gate, policy=policy
    )


def test_successful_stored_request_does_not_claim_policy_response_and_restores(harness):
    report = harness.run()
    assert probe.exit_status(report) == 0
    assert len(harness.calls) == 2
    assert probe._words(harness.calls[0], "0", harness.replay) == [-5000, 0, 0, 0, 0, 0]
    assert probe._request(harness.calls[1]) == probe._request(report["baseline"])
    assert report["projection_proof"]["exact_patch_proven"]
    assert report["trial_observation"]["stored_request_matches_desired"] is True
    assert report["trial_observation"]["selected_policy_uv_changed"] is False
    assert report["trial_observation"]["selected_policy_delta_uv"] == {"reliability": 0, "effective": 0}
    assert report["trial_observation"]["physical_voltage_effect"] == "not measured"
    assert all(report["final_checks"].values())


def test_ignored_set_retains_detailed_error_and_still_restores_once(harness):
    harness.replay.ignored = True
    report = harness.run()
    assert len(harness.calls) == 2
    assert probe.exit_status(report) == 3
    assert report["trial"]["ok"] is False
    assert report["trial"]["result"]["status"] == 0
    assert report["trial"]["result"]["after"]
    assert report["restoration"]["ok"] is True
    assert report["final_stored_request_matches_baseline"] is True


def test_restoration_failure_is_distinct(harness):
    harness.replay.restoration_fails = True
    report = harness.run()
    assert len(harness.calls) == 2
    assert report["trial"]["ok"] is True
    assert report["restoration"]["ok"] is False
    assert probe.exit_status(report) == 4
    assert report["final_stored_request_matches_baseline"] is False


@pytest.mark.parametrize("failure", ["incomplete", "absent", "disabled", "missing_policy"])
def test_preflight_failure_never_calls_replay(harness, failure):
    if failure == "incomplete":
        harness.state["complete"] = False
        harness.state["missing_indices"] = [1]
    elif failure == "absent":
        harness.state["public"] = {}
    elif failure == "disabled":
        harness.gate.read = lambda: {"enabled": False, "identity": harness.state["identity"]}
    else:
        harness.policy.read = lambda gpu, **kw: (None, "missing")
    report = harness.run()
    assert not harness.calls
    assert report["errors"]
    assert probe.exit_status(report) == 2


def test_projection_rejects_opaque_mutation(harness):
    project = harness.replay.project

    def corrupted(*args, **kwargs):
        desired = project(*args, **kwargs)
        desired["info_hex"] = "00" + desired["info_hex"][2:]
        return desired

    harness.replay.project = corrupted
    report = harness.run()
    assert not harness.calls
    assert "outside" in report["errors"][0]["message"]


def test_help_does_not_import_hardware_modules():
    code = f"""import importlib.util,sys
spec=importlib.util.spec_from_file_location('probe',{str(SCRIPT)!r})
probe=importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
try: probe.main(['--help'])
except SystemExit as exc: assert exc.code == 0
assert not any(name.startswith('druta') for name in sys.modules)
"""
    completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
