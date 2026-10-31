# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
import copy
import struct

import pytest

from experiments import validate_short_power_override as trial
from tests.test_vbios_fields import v30_power_fixture


def packet_state(legacy, ci, ni):
    binding = {"client_index": ci, "native_index": ni, "maximum_mw": 380000}
    if legacy:
        binding["layout"] = "legacy32"
    sizes = trial.client.LEGACY_SIZES if legacy else trial.client.SIZES
    state = {"schema": 1, "stable_reads": 2, "identity": {"slot": "other"}, "binding": binding,
             "requested_mw": 310000}
    for name, command in (("info_hex", trial.client.INFO), ("control_hex", trial.client.CONTROL),
                          ("role_info_hex", trial.client.ROLE_INFO), ("native_info_hex", trial.client.NATIVE_INFO),
                          ("native_control_hex", trial.client.NATIVE_CONTROL)):
        state[name] = bytes([0xA5]) * sizes[command]
    offsets = {"info_hex": (8 if legacy else 0x24) + ci * 36 + 12,
               "role_info_hex": 20, "native_info_hex": (88 if legacy else 0xCC) + ni * (228 if legacy else 0x104) + 16}
    for name, offset in offsets.items():
        raw = bytearray(state[name])
        struct.pack_into("<I", raw, offset, 380000)
        state[name] = raw.hex()
    for name in ("control_hex", "native_control_hex"):
        state[name] = state[name].hex()
    return state, offsets


@pytest.mark.parametrize("legacy,ci,ni", [(True, 7, 3), (False, 19, 11)])
def test_complete_packet_projection_changes_only_bound_maximum(legacy, ci, ni):
    before, offsets = packet_state(legacy, ci, ni)
    saved = copy.deepcopy(before)
    after = trial.project_client(before)
    assert before == saved
    assert after["binding"]["maximum_mw"] == 381000
    for name in ("info_hex", "role_info_hex", "native_info_hex", "control_hex", "native_control_hex"):
        original, actual = bytes.fromhex(before[name]), bytes.fromhex(after[name])
        expected = bytearray(original)
        if name in offsets:
            struct.pack_into("<I", expected, offsets[name], 381000)
        assert actual == bytes(expected)
    info = bytes.fromhex(after["info_hex"])
    assert info[offsets["info_hex"] + 4:offsets["info_hex"] + 8] == b"\xa5" * 4


def test_image_projection_preserves_tail_and_rejects_bad_binding():
    image = bytearray(v30_power_fixture(stride=0x47, count=3, channel=7))
    row = 0x3800 + 0x2C + 2 * 0x47
    image[row + 0x43:row + 0x47] = b"\x01\x02\x03\x04"
    image = bytes(image)
    inspection = trial.vbios_fields.inspect(image)
    binding = {"native_index": 2, "units": "mW", "minimum_mw": 100000, "rated_mw": 310000, "maximum_mw": 380000}
    actual, offset = trial.project_image(image, inspection, binding)
    expected = bytearray(image)
    struct.pack_into("<I", expected, row + 10, 381000)
    assert offset == row + 10 and actual == bytes(expected)
    for altered in ({**binding, "maximum_mw": 379999}, {**binding, "units": "mA"}, {**binding, "native_index": 3}):
        with pytest.raises(ValueError):
            trial.project_image(image, inspection, altered)
    bad = copy.deepcopy(inspection)
    bad["tables"][-1]["stride"] = 0x48
    with pytest.raises(ValueError):
        trial.project_image(image, bad, binding)
    raw = bytearray(image)
    raw[row + 0x42] = 1
    with pytest.raises(ValueError):
        trial.project_image(bytes(raw), inspection, binding)


def test_packet_projection_rejects_mismatched_bounds():
    before, _ = packet_state(True, 1, 1)
    before["binding"]["maximum_mw"] = 380001
    with pytest.raises(ValueError):
        trial.project_client(before)


def test_cli_orchestration(monkeypatch, tmp_path):
    from dataclasses import dataclass
    from types import SimpleNamespace

    @dataclass
    class Target:
        slot: str

    target = Target("slot")
    image = v30_power_fixture(stride=0x47, channel=7)
    observed, offsets = packet_state(True, 1, 0)
    observed["identity"] = {"slot": "slot"}
    observed["binding"].update(units="mW", minimum_mw=100000, rated_mw=310000)
    role = bytearray.fromhex(observed["role_info_hex"])
    role[9] = 7
    observed["role_info_hex"] = role.hex()
    active_client = trial.project_client(observed)
    active_image, _ = trial.project_image(image, trial.vbios_fields.inspect(image), observed["binding"])
    context = {"active": False, "calls": [], "receipt": None, "transient": False, "fail_apply": False,
               "fail_undo": False}
    monkeypatch.setattr(trial, "GPU", lambda slot: object())
    monkeypatch.setattr(trial.ro, "bind_target", lambda gpu: target)
    monkeypatch.setattr(trial.ro, "WindowsRegistry", lambda target: SimpleNamespace(snapshot=lambda: {}))
    monkeypatch.setattr(trial.ro, "policy_contracts", lambda *args: {"power.0.maximum": {"before": 380000}})
    monkeypatch.setattr(trial.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="commit\n"))
    monkeypatch.setattr(trial.time, "sleep", lambda _: None)

    def snapshot(slot):
        context["calls"].append("get")
        if context["active"] and context["transient"]:
            context["transient"] = False
            raise RuntimeError("transient")
        return {"target": target, "image": active_image if context["active"] else image}

    monkeypatch.setattr(trial.ro, "fresh_snapshot", snapshot)
    monkeypatch.setattr(trial.client, "ClientPowerController", lambda gpu: SimpleNamespace(
        read=lambda: copy.deepcopy(active_client if context["active"] else observed)))
    monkeypatch.setattr(trial.ro, "active_receipt", lambda target: context["receipt"] if context["active"] else None)

    def prepare(gpu, changes):
        context["calls"].append("prepare")
        receipt = {"path": str(tmp_path / "receipt.json"), "target": {"slot": "slot"}, "before": {},
                   "plan": trial.vbios_fields.plan(image, changes),
                   "policy_contracts": {"power.0.maximum": {"before": 380000}}}
        context["receipt"] = receipt
        return receipt

    def execute(receipt, restore=False):
        context["calls"].append("undo" if restore else "apply")
        context["active"] = not restore or context["fail_undo"]
        if not restore:
            context["transient"] = True
        return {"ok": not ((context["fail_apply"] and not restore) or (context["fail_undo"] and restore)),
                "runtime_verified_fields": ["power.0.maximum"]}

    monkeypatch.setattr(trial.ro, "prepare", prepare)
    monkeypatch.setattr(trial.ro, "load_receipt", lambda path: context["receipt"])
    monkeypatch.setattr(trial.ro, "execute", execute)
    for mode in ("read", "run", "fail", "undo_fail"):
        context.update(active=False, calls=[], receipt=None, transient=False, fail_apply=mode == "fail",
                       fail_undo=mode == "undo_fail")
        out = tmp_path / mode
        args = ["trial", "--slot", "slot", "--out", str(out)] + ([] if mode == "read" else ["--run"])
        monkeypatch.setattr(trial.sys, "argv", args)
        if mode in ("fail", "undo_fail"):
            with pytest.raises(SystemExit):
                trial.main()
        else:
            trial.main()
        journal = trial.json.loads((out / "journal.json").read_text(encoding="utf-8"))
        assert "source_sha256" in journal and journal["source_commit"] == "commit"
        if mode == "read":
            assert context["calls"] == ["get"] and journal["state"] == "read-only-complete"
        else:
            assert context["calls"].count("apply") == context["calls"].count("undo") == 1
            if mode == "undo_fail":
                assert context["active"] and journal["state"] == "recovery-required"
                assert journal["receipt_path"] == context["receipt"]["path"]
                assert trial.ro.active_receipt(target) == context["receipt"]
            else:
                assert not context["active"] and journal["state"] == "restored"
            if mode == "run":
                assert any("traceback" in attempt for attempt in journal["attempts"])
            else:
                assert journal["errors"]
