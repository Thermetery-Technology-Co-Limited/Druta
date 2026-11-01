# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""One explicit Ampere REL -5000 uV request trial, followed by restoration.

No hardware modules are imported by --help or by importing this file. Historical
inventory is context only; every previous value comes from the current adapter.
The report distinguishes stored requests and firmware policy; neither measures
physical voltage. No gate SET, registry write, restart or workload is performed.
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
import struct
import sys
from pathlib import Path


def _inventory_helpers():
    path = Path(__file__).with_name("ampere_voltage_inventory_20261030.py")
    spec = importlib.util.spec_from_file_location("ampere_inventory_helpers", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


util = _inventory_helpers()
SCHEMA = "ampere-voltage-rel-probe-v1"
DELTA_UV = -5000


def _require(condition, message):
    if not condition:
        raise RuntimeError(message)


def _slot(text):
    _require(isinstance(text, str) and util.SLOT_RE.fullmatch(text), "Explicit PCI slot required")
    domain, bus, device_function = text.lower().split(":")
    device, function = device_function.split(".")
    _require(int(domain, 16) == 0 and int(device, 16) <= 31, "PCI address is outside the understood range")
    return f"{int(domain, 16):04x}:{int(bus, 16):02x}:{int(device, 16):02x}.{int(function)}"


def _request(state):
    return {key: value for key, value in state.items() if key != "policy"}


def _words(state, rail, replay):
    at = replay.controls.ROW_OFFSET + int(rail) * replay.controls.ROW_STRIDE + 4
    return list(struct.unpack_from("<6i", bytes.fromhex(state["public"][rail]), at))


def _project(baseline, rail, replay):
    """Independently prove that only the selected REL word changed in both GETs."""
    baseline = replay.validate(baseline)
    _require(baseline["complete"] and not baseline["missing_indices"], "Incomplete request inventory")
    _require(rail in baseline["public"], "Explicitly selected rail is absent")
    words = _words(baseline, rail, replay)
    wanted = words.copy()
    wanted[0] += DELTA_UV
    _require(-(2**31) <= wanted[0] < 2**31, "REL decrement overflows signed microvolts")
    desired = replay.validate(replay.project(baseline, offsets_uv={rail: wanted}))
    expected = copy.deepcopy(baseline)
    for key, at in (
        ("native_hex", replay.HEADER + int(rail) * replay.CONTROL_STRIDE + 4),
        ("public", replay.controls.ROW_OFFSET + int(rail) * replay.controls.ROW_STRIDE + 4),
    ):
        raw = bytearray.fromhex(baseline[key][rail] if key == "public" else baseline[key])
        struct.pack_into("<i", raw, at, wanted[0])
        if key == "public":
            expected[key][rail] = raw.hex()
        else:
            expected[key] = raw.hex()
    _require(desired == expected, "Projection changed data outside the selected public/native REL word")
    return desired, {"before_six_uv": words, "desired_six_uv": wanted, "exact_patch_proven": True}


def _identity(gpu, slot, identity):
    pci = util._pci_identity(gpu)
    _require(not pci["pairing_error"], "GPU pairing error")
    _require(_slot(pci["requested_slot"]) == slot == _slot(pci["selected_slot"]), "PCI target changed")
    _require(identity["architecture"] == 7 and _slot(identity["slot"]) == slot, "Expected a paired Ampere GPU")
    for name in ("nvapi", "nvml"):
        api = pci[name]
        selected = api["selected"]
        _require(api["ok"] and isinstance(selected, dict), name + " has no complete selected identity")
        _require(_slot(selected.get("slot")) == slot, name + " identifies another PCI slot")
        _require(
            selected.get("devid") == identity["device_id"] and selected.get("subsys") == identity["subsystem_id"],
            name + " PCI identity differs from request identity",
        )
    return pci


def _pair_read(function):
    observation, error = function()
    _require(error is None and observation is not None, f"Fresh read unavailable: {error}")
    return observation


def _bracket(gpu, registry):
    result = util._bracket(gpu, registry)
    # Retain the hash and length, not a second large VBIOS image in every checkpoint.
    result.get("vbios_image", {}).pop("image_hex", None)
    return result


def _complete_bracket(bracket, slot):
    _require(
        all(bracket.get(key, {}).get("ok") for key in ("target", "managed_registry", "vbios_image")),
        "Target/registry/VBIOS bracket is incomplete",
    )
    target = bracket["target"]["value"]
    _require(
        _slot(target.get("slot")) == slot and target.get("instance_id") and target.get("driver_key"),
        "Incomplete Windows target binding",
    )
    _require(isinstance(bracket["managed_registry"]["value"], dict), "Registry snapshot unavailable")
    image = bracket["vbios_image"]
    _require(image.get("byte_length", 0) > 0 and len(image.get("sha256", "")) == 64, "VBIOS hash unavailable")


def _replay_once(gpu, saved, replay):
    try:
        return {"ok": True, "result": replay.replay(gpu, saved)}
    except BaseException as exc:
        # Includes ReplayError.result and interruption results: do not erase partial evidence.
        return {
            "ok": False,
            "error": util._error(exc),
            "kind": getattr(exc, "kind", None),
            "attempted": getattr(exc, "attempted", None),
            "result": getattr(exc, "result", None),
        }


def run_probe(gpu, slot, rail, *, replay, policy, registry, gate, checkpoint, source):
    """Inject readers/setter so offline tests never initialize hardware."""
    report = {
        "schema": SCHEMA,
        "created_at_utc": util._utc_now(),
        "slot": slot,
        "rail": rail,
        "delta_uv": DELTA_UV,
        "source_before": source(),
        "preflight_rounds": [],
        "trial": None,
        "restoration": None,
        "errors": [],
        "scope": "One REL request decrement relative to fresh baseline; one restoration attempt. "
        "Stored request and firmware policy observations do not establish physical voltage effect.",
    }
    baseline = None
    restoration_needed = False
    try:
        report["before"] = _bracket(gpu, registry)
        _complete_bracket(report["before"], slot)
        _require(
            all(isinstance(value, str) for value in report["source_before"]["files_sha256"].values()),
            "Source hashes unavailable",
        )
        for _ in range(2):
            state = replay.validate(_pair_read(lambda: replay.read(gpu)))
            _require(state["complete"] and not state["missing_indices"], "Incomplete fresh request capture")
            pci = _identity(gpu, slot, state["identity"])
            admission = gate.read()
            _require(
                admission.get("enabled") is True and admission.get("identity") == state["identity"],
                "Voltage-request admission is disabled, unavailable or identifies another GPU",
            )
            report["preflight_rounds"].append({"request": state, "pci_identity": pci, "gate": admission})
        first, second = report["preflight_rounds"]
        _require(
            _request(first["request"]) == _request(second["request"])
            and first["pci_identity"] == second["pci_identity"],
            "Fresh requests or PCI identities disagree",
        )
        baseline = second["request"]
        desired, proof = _project(baseline, rail, replay)
        report.update(baseline=baseline, desired=desired, projection_proof=proof)
        identities = {int(i): row["policy_type"] for i, row in baseline["rail_identity"].items()}
        before_policy = policy.validate_policy(
            _pair_read(lambda: policy.read(gpu, request_identities=identities)), request=True
        )
        _require(before_policy["identity"] == baseline["identity"], "Independent policy identifies another GPU")
        for index in baseline["public"]:
            row = before_policy["rails"].get(index)
            _require(row is not None, "Independent policy lacks a captured rail")
            _require(
                [row["offsets_uv"][key] for key in ("reliability", "alt_reliability", "overvoltage", "vmin")]
                == _words(baseline, index, replay)[:4],
                "Policy offsets differ from fresh baseline",
            )
        report["policy_before"] = before_policy
        report["phase"] = "baseline_and_exact_desired_persisted_before_trial"
        checkpoint(report)  # Mandatory durable journal before any replay call.
        restoration_needed = True  # A call interrupted without a result is an uncertain trial.
        report["trial"] = _replay_once(gpu, desired, replay)
        result = report["trial"].get("result")
        if isinstance(result, dict):
            restoration_needed = result.get("attempted") is not False
        elif report["trial"].get("attempted") is False:
            restoration_needed = False
        report["phase"] = "trial_completed"
        checkpoint(report)
    except BaseException as exc:
        report["errors"].append({"phase": report.get("phase", "preflight"), **util._error(exc)})
    finally:
        if restoration_needed and baseline is not None:
            report["restoration"] = _replay_once(gpu, baseline, replay)
        # Each final read is independent so a failed one does not suppress the others.
        report["final_request"] = util._attempt(lambda: replay.validate(_pair_read(lambda: replay.read(gpu))))
        report["final_policy"] = util._attempt(
            lambda: policy.validate_policy(
                _pair_read(
                    lambda: policy.read(
                        gpu,
                        request_identities={int(i): row["policy_type"] for i, row in baseline["rail_identity"].items()}
                        if baseline
                        else None,
                    )
                ),
                request=True,
            )
        )
        report["final_gate"] = util._attempt(gate.read)
        report["pci_identity_after"] = (
            util._attempt(lambda: _identity(gpu, slot, baseline["identity"])) if baseline else None
        )
        report["final"] = util._attempt(lambda: _bracket(gpu, registry))
        report["source_after"] = util._attempt(source)
        report["phase"] = "finished"
    final = report["final_request"]
    report["final_stored_request_matches_baseline"] = (
        _request(final["value"]) == _request(baseline) if baseline is not None and final["ok"] else None
    )
    trial_result = (report["trial"] or {}).get("result") or {}
    trial_after = trial_result.get("after")
    prior_policy_uv = report.get("policy_before", {}).get("rails", {}).get(rail, {}).get("policy_uv")
    after_policy_uv = trial_after["policy"]["rails"].get(rail, {}).get("policy_uv") if trial_after else None
    report["trial_observation"] = {
        "stored_request_matches_desired": _request(trial_after) == _request(report["desired"]) if trial_after else None,
        "selected_rel_request_uv": _words(trial_after, rail, replay)[0]
        if trial_after and rail in trial_after["public"]
        else None,
        "selected_policy_uv_changed": after_policy_uv != prior_policy_uv
        if after_policy_uv and prior_policy_uv
        else None,
        "selected_policy_uv_before": prior_policy_uv,
        "selected_policy_uv_after": after_policy_uv,
        "selected_policy_delta_uv": {
            key: after_policy_uv[key] - prior_policy_uv[key] for key in ("reliability", "effective")
        }
        if after_policy_uv and prior_policy_uv
        else None,
        "policy_after": trial_after["policy"] if trial_after else None,
        "physical_voltage_effect": "not measured",
    }
    report["bracket_comparison"] = (
        util._comparison(report["before"], report["final"]["value"])
        if report.get("before") and report["final"]["ok"]
        else None
    )
    report["final_checks"] = {
        "stored_request_restored": report["final_stored_request_matches_baseline"],
        "policy_identity_matches": report["final_policy"]["ok"]
        and baseline is not None
        and report["final_policy"]["value"]["identity"] == baseline["identity"],
        "policy_matches_before": report["final_policy"]["ok"]
        and "policy_before" in report
        and report["final_policy"]["value"] == report["policy_before"],
        "gate_enabled_and_identity_matches": report["final_gate"]["ok"]
        and baseline is not None
        and report["final_gate"]["value"] == {"enabled": True, "identity": baseline["identity"]},
        "pci_identity_matches": bool(report["pci_identity_after"] and report["pci_identity_after"]["ok"])
        and bool(report["preflight_rounds"])
        and report["pci_identity_after"]["value"] == report["preflight_rounds"][-1]["pci_identity"],
        "target_registry_vbios_unchanged": bool(report["bracket_comparison"])
        and all(value is True for value in report["bracket_comparison"].values()),
        "source_unchanged": report["source_after"]["ok"] and report["source_after"]["value"] == report["source_before"],
    }
    report["final_policy_changed_from_before"] = (
        report["final_policy"]["value"] != report["policy_before"]
        if report["final_policy"]["ok"] and "policy_before" in report
        else None
    )
    return report


def exit_status(report):
    """Keep trial, restoration and independent-final-check failures distinguishable."""
    if report.get("restoration") and not report["restoration"]["ok"]:
        return 4
    if report.get("trial") and not report["trial"]["ok"]:
        return 3
    if not report.get("trial"):
        return 2
    if report.get("errors") or not report.get("restoration") or not all(report["final_checks"].values()):
        return 5
    return 0


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gpu", required=True, help="Explicit PCI slot; no default target")
    parser.add_argument("--rail", required=True, choices=("0", "1"), help="Explicit captured rail index")
    parser.add_argument("--out", required=True, help="New JSON report in an existing directory")
    args = parser.parse_args(argv)
    try:
        slot = _slot(args.gpu.strip())
    except RuntimeError as exc:
        parser.error(str(exc))
    output = Path(args.out).expanduser().absolute()
    if not output.parent.is_dir() or output.exists():
        parser.error("--out must be a new file in an existing directory")
    return slot, args.rail, output


def main(argv=None):
    slot, rail, output = _parse_args(argv)
    # The exclusive descriptor remains ours throughout; checkpoints never reopen by name.
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:

        def checkpoint(report):
            encoded = json.dumps(util._jsonable(report), indent=2, sort_keys=True) + "\n"
            stream.seek(0)
            stream.write(encoded)
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())

        root = util._repo_root()
        try:
            sys.path.insert(0, str(root / "src"))
            from druta import registry_overrides, voltage_bios_runtime, voltage_request_gate, voltage_request_replay
            from druta.nvbackend import GPU

            gpu = GPU(slot)
            report = run_probe(
                gpu,
                slot,
                rail,
                replay=voltage_request_replay,
                policy=voltage_bios_runtime,
                registry=registry_overrides,
                gate=voltage_request_gate.VoltageRequestGate(gpu),
                checkpoint=checkpoint,
                source=lambda: util._source_state(root, Path(__file__)),
            )
        except BaseException as exc:
            report = {"schema": SCHEMA, "initialization_error": util._error(exc)}
        checkpoint(report)
    print(
        json.dumps(
            {
                "output": str(output),
                "trial_ok": (report.get("trial") or {}).get("ok"),
                "final_stored_request_matches_baseline": report.get("final_stored_request_matches_baseline"),
            }
        )
    )
    return exit_status(report)


if __name__ == "__main__":
    raise SystemExit(main())
