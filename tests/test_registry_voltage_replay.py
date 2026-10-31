"""Offline voltage replay transactions, bounded receipts, and exact recovery."""

import copy
import hashlib
import json
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from druta import registry_overrides as ro
from druta import voltage_request_replay as replay
from tests.test_registry_voltage_rel import configured


def request_capture(combined):
    """Pair the existing combined fixture with independent complete native INFO."""
    public, identities = {}, {}
    info, native = bytearray(replay.INFO_SIZE), bytearray(replay.CONTROL_SIZE)
    info[0], info[3], info[-1] = 0xFF, 0xCF, 0xD9
    native[0], native[3], native[-1] = 0xFF, 0xB8, 0xE7
    mask = sum(1 << int(index) for index in combined["policy"]["rails"])
    struct.pack_into("<I", info, 4, mask)
    struct.pack_into("<I", native, 4, mask)
    native[8] = combined["boost_percent"]
    for index, row in combined["policy"]["rails"].items():
        raw = bytes.fromhex(combined["rail_control"]["rails"][index]["payload_hex"])
        at = replay.controls.ROW_OFFSET + int(index) * replay.controls.ROW_STRIDE
        nat = replay.HEADER + int(index) * replay.CONTROL_STRIDE
        info[replay.HEADER + int(index) * replay.INFO_STRIDE] = 5
        native[nat], native[nat + 1], native[nat + 28] = 5, 0xC8, 1
        native[nat + 4 : nat + 28] = raw[at + 4 : at + 28]
        public[index] = raw.hex()
        identities[index] = {"name": row["name"], "policy_type": row["type"], "control_type": 3}
    return replay.validate(
        {
            "schema": 1,
            "identity": copy.deepcopy(combined["policy"]["identity"]),
            "info_hex": info.hex(),
            "native_hex": native.hex(),
            "public": public,
            "rail_identity": identities,
            "boost_percent": combined["boost_percent"],
            "complete": True,
            "missing_indices": [],
            "policy": copy.deepcopy(combined["policy"]),
        }
    )


def make_replayable(combined):
    for index, row in combined["policy"]["rails"].items():
        row["control_type"] = 3
        raw = bytearray.fromhex(combined["rail_control"]["rails"][index]["payload_hex"])
        at = replay.controls.ROW_OFFSET + int(index) * replay.controls.ROW_STRIDE
        struct.pack_into("<I", raw, at, 3)
        raw[8], raw[9], raw[71], raw[-1] = combined["boost_percent"], 0xA4, 0xC6, 0xD8
        raw[at + 41] = 0xF2
        combined["rail_control"]["rails"][index]["payload_hex"] = raw.hex()


def scenario(monkeypatch, *, boost=37, rail=0, partial=False):
    monkeypatch.setattr(ro.time, "sleep", lambda seconds: None)
    receipt, registry, snapshot, restarts, kwargs, before, after = configured(
        monkeypatch, boost=boost, rail=rail, partial=partial
    )
    make_replayable(before)
    make_replayable(after)
    ro.voltage_rel_runtime.validate_contracts(receipt["changes"], receipt["voltage_rel_contracts"])
    monkeypatch.setattr(ro.voltage_rel_runtime, "read", lambda gpu: (copy.deepcopy(before), None))
    snapshot["voltage_ceiling"] = copy.deepcopy(before)
    saved = request_capture(before)
    receipt.update(voltage_replay_contract_version=1, voltage_replay_contracts={"schema": 1, "before": saved})
    ctx = SimpleNamespace(
        receipt=receipt,
        registry=registry,
        snapshot=snapshot,
        restarts=restarts,
        kwargs=kwargs,
        before=before,
        after=after,
        saved=saved,
        current=copy.deepcopy(saved),
        captures=[],
        writes=[],
        timeline=[],
    )
    replace = registry.replace

    def replace_recorded(expected, desired):
        ctx.timeline.append("registry")
        replace(expected, desired)

    registry.replace = replace_recorded
    original_restart = kwargs["restart"]

    def restart(target):
        ctx.timeline.append("restart")
        result = original_restart(target)
        # Restart loses requests. Exact activation cannot pass without replay.
        ctx.current = replay.project(saved, boost_percent=0, offsets_uv={key: [0] * 6 for key in saved["public"]})
        live = snapshot["voltage_ceiling"]
        live["boost_percent"] = 0
        for index, raw in ctx.current["public"].items():
            live["rail_control"]["rails"][index]["payload_hex"] = raw
            live["policy"]["rails"][index]["offsets_uv"] = dict.fromkeys(replay.voltage._OFFSETS, 0)
        return result

    def capture(target):
        ctx.timeline.append("capture")
        ctx.captures.append(target)
        return copy.deepcopy(ctx.current)

    def apply(target, state, image_hash):
        ctx.timeline.append("replay")
        ctx.writes.append((target, copy.deepcopy(state), image_hash))
        assert state == saved
        assert image_hash == hashlib.sha256(snapshot["image"]).hexdigest()
        assert ctx.receipt["events"][-1]["event"] == "voltage-replay-attempted"
        ctx.current = copy.deepcopy(state)
        snapshot["voltage_ceiling"] = copy.deepcopy(after if registry.values else before)
        return {"verified": True, "status": 0, "after": copy.deepcopy(state), "worker_paths": {"result": "worker.json"}}

    kwargs.update(restart=restart, voltage_capture=capture, voltage_replay=apply)
    ctx.apply = apply
    return ctx


def prepare_setup(monkeypatch, tmp_path, ctx, *, export=True):
    target = ro.Target(**ctx.receipt["target"])
    monkeypatch.setattr(ro, "bind_target", lambda gpu: target)
    monkeypatch.setattr(ro, "state_directory", lambda: tmp_path)
    monkeypatch.setattr(ro, "read_vbios", lambda gpu: b"abcdef")
    monkeypatch.setattr(ro, "read_model_inputs", lambda gpu: (None, "unrelated model readback unavailable"))
    monkeypatch.setattr(replay, "read_checked", lambda gpu: copy.deepcopy(ctx.saved))
    return SimpleNamespace(nvapi=SimpleNamespace(_i=lambda *args: object() if export else None))


@pytest.mark.parametrize("boost", [0, 1, 100])
@pytest.mark.parametrize("partial", [False, True])
def test_prepare_saves_complete_current_adapter_capture(monkeypatch, tmp_path, boost, partial):
    ctx = scenario(monkeypatch, boost=boost, rail=1, partial=partial)
    gpu = prepare_setup(monkeypatch, tmp_path, ctx)
    prepared = ro.prepare(gpu, ctx.receipt["changes"], registry=ctx.registry)
    assert prepared["voltage_replay_contract_version"] == 1
    assert prepared["voltage_replay_contracts"] == {"schema": 1, "before": ctx.saved}
    assert prepared["voltage_replay_contracts"]["before"]["complete"] is True
    assert ro.load_receipt(prepared["path"]) == prepared
    assert not ctx.registry.operations and not ctx.restarts and not ctx.writes


@pytest.mark.parametrize("capability", ["export", "unsupported", "incomplete"])
@pytest.mark.parametrize("boost", [0, 37])
def test_prepare_unsupported_replay_retains_only_independent_zero_boost_path(monkeypatch, tmp_path, capability, boost):
    ctx = scenario(monkeypatch, boost=boost, partial=True)
    gpu = prepare_setup(monkeypatch, tmp_path, ctx, export=capability != "export")

    def capture(gpu):
        if capability == "export":
            pytest.fail("Missing SET export must not cause a capture")
        if capability == "unsupported":
            raise replay.ReplayError("unsupported", "Native capture unsupported")
        saved = copy.deepcopy(ctx.saved)
        raw = bytearray.fromhex(saved["info_hex"])
        struct.pack_into("<I", raw, 4, struct.unpack_from("<I", raw, 4)[0] | (1 << 7))
        raw[replay.HEADER + 7 * replay.INFO_STRIDE] = 77
        saved.update(info_hex=raw.hex(), complete=False, missing_indices=[7])
        return replay.validate(saved)

    monkeypatch.setattr(replay, "read_checked", capture)
    if boost:
        with pytest.raises(RuntimeError, match="boost|request"):
            ro.prepare(gpu, ctx.receipt["changes"], registry=ctx.registry)
        assert not list(tmp_path.glob("*.json"))
    else:
        prepared = ro.prepare(gpu, ctx.receipt["changes"], registry=ctx.registry)
        assert prepared["voltage_replay_contracts"] == {}
        assert prepared["voltage_rel_contracts"]
    assert not ctx.registry.operations and not ctx.restarts and not ctx.writes


@pytest.mark.parametrize("kind", ["transient", "malformed", "identity"])
@pytest.mark.parametrize("boost", [0, 37])
def test_prepare_typed_capture_failures_never_fall_back_or_write(monkeypatch, tmp_path, kind, boost):
    ctx = scenario(monkeypatch, boost=boost)
    gpu = prepare_setup(monkeypatch, tmp_path, ctx)

    def fail(gpu):
        raise replay.ReplayError(kind, "captured request failure")

    monkeypatch.setattr(replay, "read_checked", fail)
    with pytest.raises(ro.OverrideError, match="capture failed"):
        ro.prepare(gpu, ctx.receipt["changes"], registry=ctx.registry)
    assert not ctx.registry.operations and not ctx.restarts and not list(tmp_path.glob("*.json"))


@pytest.mark.parametrize("drift", ["policy", "public", "boost"])
def test_prepare_requires_binding_to_independent_combined_observation(monkeypatch, tmp_path, drift):
    ctx = scenario(monkeypatch)
    gpu = prepare_setup(monkeypatch, tmp_path, ctx)
    changed = copy.deepcopy(ctx.saved)
    if drift == "policy":
        changed["policy"]["rails"]["0"]["boost_uv"] += 1
    elif drift == "boost":
        changed = replay.project(changed, boost_percent=38)
    else:
        raw = bytearray.fromhex(changed["public"]["0"])
        raw[-1] ^= 1
        changed["public"]["0"] = raw.hex()
    monkeypatch.setattr(replay, "read_checked", lambda gpu: changed)
    with pytest.raises(ro.OverrideError, match="differs"):
        ro.prepare(gpu, ctx.receipt["changes"], registry=ctx.registry)
    assert not ctx.registry.operations and not ctx.restarts


@pytest.mark.parametrize("boost", [1, 37, 100])
@pytest.mark.parametrize("partial", [False, True])
def test_nonzero_apply_and_cleanup_first_undo_replay_all_six_requests(monkeypatch, boost, partial):
    ctx = scenario(monkeypatch, boost=boost, partial=partial)
    result = ro.execute(ctx.receipt, **ctx.kwargs)
    assert result["state"] == "active", result["message"]
    assert ctx.snapshot["voltage_ceiling"] == ctx.after
    assert ctx.timeline == ["capture", "registry", "restart", "replay"]
    assert len(ctx.captures) == 1 and len(ctx.writes) == 1
    assert ctx.writes[0][2] == result["after_image_sha256"]
    ctx.kwargs["gpu_factory"] = lambda slot: (
        ctx.snapshot if not ctx.registry.values else pytest.fail("Undo read the GPU before registry cleanup")
    )
    ctx.kwargs["voltage_capture"] = lambda target: pytest.fail("Undo must not require a pre-cleanup capture")
    result = ro.execute(ctx.receipt, restore=True, **ctx.kwargs)
    assert result["state"] == "restored", result["message"]
    assert ctx.snapshot["voltage_ceiling"] == ctx.before and ctx.current == ctx.saved
    assert ctx.timeline[-3:] == ["registry", "restart", "replay"]
    assert [write[2] for write in ctx.writes] == [result["after_image_sha256"], result["before_image_sha256"]]
    assert not ctx.registry.values and len(ctx.restarts) == 2


@pytest.mark.parametrize("drift", ["sixth_offset", "boost", "info_opaque", "public_opaque", "policy"])
def test_changed_preflight_requests_cannot_make_first_write(monkeypatch, drift):
    ctx = scenario(monkeypatch)
    if drift == "sixth_offset":
        raw = bytes.fromhex(ctx.current["public"]["1"])
        at = replay.controls.ROW_OFFSET + replay.controls.ROW_STRIDE
        values = replay._six(raw, at)
        values[5] += 1
        ctx.current = replay.project(ctx.current, offsets_uv={"1": values})
    elif drift == "boost":
        ctx.current = replay.project(ctx.current, boost_percent=38)
    elif drift == "policy":
        ctx.current["policy"]["rails"]["0"]["boost_uv"] += 1
    else:
        key = "info_hex" if drift == "info_opaque" else None
        raw = bytearray.fromhex(ctx.current[key] if key else ctx.current["public"]["0"])
        raw[-1] ^= 1
        if key:
            ctx.current[key] = raw.hex()
        else:
            ctx.current["public"]["0"] = raw.hex()
    result = ro.execute(ctx.receipt, **ctx.kwargs)
    assert result["state"] == "failed" and "changed after" in result["message"]
    assert not ctx.registry.operations and not ctx.restarts and not ctx.writes


@pytest.mark.parametrize("kind", ["transient", "malformed", "identity", "unsupported"])
def test_preflight_capture_failure_never_mutates(monkeypatch, kind):
    ctx = scenario(monkeypatch)

    def capture(target):
        raise replay.ReplayError(kind, "preflight capture failed")

    ctx.kwargs["voltage_capture"] = capture
    result = ro.execute(ctx.receipt, **ctx.kwargs)
    assert result["state"] == "failed"
    assert not ctx.registry.operations and not ctx.restarts and not ctx.writes


@pytest.mark.parametrize("failure", ["set", "timeout", "snapshot", "readback", "unverified", "post_image"])
def test_apply_failure_never_retries_uncertain_write_and_replays_original_after_rollback(monkeypatch, failure):
    ctx = scenario(monkeypatch)
    snapshot_failure = [False]
    original_gpu = ctx.kwargs["gpu_factory"]

    def gpu(slot):
        if snapshot_failure[0] and ctx.registry.values:
            raise RuntimeError("snapshot unavailable after successful SET")
        return original_gpu(slot)

    def apply(target, saved, image_hash):
        result = ctx.apply(target, saved, image_hash)
        if len(ctx.writes) == 1:
            if failure == "set":
                raise replay.ReplayError("set", "SET partially applied", attempted=True, result=result)
            if failure == "timeout":
                raise TimeoutError("SET worker timed out; outcome unknown")
            if failure == "snapshot":
                snapshot_failure[0] = True
            elif failure == "readback":
                result["after"] = replay.project(saved, boost_percent=0)
            elif failure == "unverified":
                result["verified"] = False
            elif failure == "post_image":
                ctx.snapshot["image"] = b"other!"
        return result

    ctx.kwargs.update(gpu_factory=gpu, voltage_replay=apply)
    result = ro.execute(ctx.receipt, **ctx.kwargs)
    assert result["state"] == "rolled-back", result["message"]
    assert not result["ok"] and not ctx.registry.values
    assert ctx.registry.operations == [result["after"], result["before"]]
    assert [write[2] for write in ctx.writes] == [result["after_image_sha256"], result["before_image_sha256"]]
    assert ctx.current == ctx.saved and ctx.snapshot["voltage_ceiling"] == ctx.before
    assert len(ctx.restarts) == 2
    attempts = [event for event in result["events"] if event["event"] == "voltage-replay-attempted"]
    assert [event["phase"] for event in attempts] == ["apply", "rollback"]


@pytest.mark.parametrize("failure", ["set", "timeout", "snapshot", "readback"])
def test_restore_failure_keeps_original_registry_and_durable_recovery_pointer(monkeypatch, tmp_path, failure):
    ctx = scenario(monkeypatch)
    ctx.receipt.update(schema=1, id="b" * 32, path=str(tmp_path / ("b" * 32 + ".json")))
    monkeypatch.setattr(ro, "state_directory", lambda: tmp_path)
    ctx.kwargs["save"] = ro.startup.atomic_json
    result = ro.execute(ctx.receipt, **ctx.kwargs)
    assert result["state"] == "active", result["message"]
    ctx.writes.clear()
    original_gpu = ctx.kwargs["gpu_factory"]
    pending = [False]

    def gpu(slot):
        assert not ctx.registry.values, "Restore must clean registry before any GPU GET"
        if pending[0]:
            raise RuntimeError("post-SET readback unavailable")
        return original_gpu(slot)

    def apply(target, saved, image_hash):
        result = ctx.apply(target, saved, image_hash)
        if failure == "set":
            raise replay.ReplayError("set", "Restore SET failed", attempted=True, result=result)
        if failure == "timeout":
            raise TimeoutError("Restore SET outcome unknown")
        if failure == "snapshot":
            pending[0] = True
        elif failure == "readback":
            result["after"] = replay.project(saved, boost_percent=0)
        return result

    ctx.kwargs.update(gpu_factory=gpu, voltage_replay=apply)
    result = ro.execute(ctx.receipt, restore=True, **ctx.kwargs)
    assert result["state"] == "recovery-required", result["message"]
    assert not result["ok"] and not ctx.registry.values and len(ctx.writes) == 1
    assert ctx.writes[0][2] == result["before_image_sha256"]
    assert all(not state for state in ctx.registry.operations[1:])
    assert len(ctx.restarts) == 2
    assert ro.active_receipt(ro.Target(**result["target"])) == result


@pytest.mark.parametrize("failures", [1, 7])
@pytest.mark.parametrize("restore", [False, True])
def test_post_set_transient_snapshot_retries_only_get_and_keeps_one_write(monkeypatch, failures, restore):
    ctx = scenario(monkeypatch)
    if restore:
        assert ro.execute(ctx.receipt, **ctx.kwargs)["state"] == "active"
        ctx.writes.clear()
        ctx.restarts.clear()
    pending = [0]
    failed = []
    original_gpu = ctx.kwargs["gpu_factory"]

    def apply(target, saved, image_hash):
        result = ctx.apply(target, saved, image_hash)
        pending[0] = failures
        return result

    def gpu(slot):
        if pending[0]:
            pending[0] -= 1
            failed.append(slot)
            raise RuntimeError("Fresh GET briefly unavailable")
        return original_gpu(slot)

    ctx.kwargs.update(gpu_factory=gpu, voltage_replay=apply)
    result = ro.execute(ctx.receipt, restore=restore, **ctx.kwargs)
    assert result["state"] == ("restored" if restore else "active"), result["message"]
    assert len(failed) == failures and len(ctx.writes) == 1 and len(ctx.restarts) == 1
    assert ctx.current == ctx.saved


def test_wrong_active_image_prevents_set_until_original_image_restored(monkeypatch):
    ctx = scenario(monkeypatch)
    restart = ctx.kwargs["restart"]

    def wrong_image(target):
        result = restart(target)
        if ctx.registry.values:
            ctx.snapshot["image"] = b"other!"
        return result

    ctx.kwargs["restart"] = wrong_image
    result = ro.execute(ctx.receipt, **ctx.kwargs)
    assert result["state"] == "rolled-back", result["message"]
    assert len(ctx.writes) == 1 and ctx.writes[0][2] == result["before_image_sha256"]
    assert not ctx.registry.values and len(ctx.restarts) == 2
    assert [event["phase"] for event in result["events"] if event["event"] == "voltage-replay-attempted"] == [
        "rollback"
    ]


def test_failed_apply_and_failed_original_request_replay_preserve_recovery_pointer(monkeypatch, tmp_path):
    ctx = scenario(monkeypatch)
    ctx.receipt.update(schema=1, id="c" * 32, path=str(tmp_path / ("c" * 32 + ".json")))
    monkeypatch.setattr(ro, "state_directory", lambda: tmp_path)
    ctx.kwargs["save"] = ro.startup.atomic_json

    class WorkerError(RuntimeError):
        paths = {"result": "bounded-worker-result.json", "stderr": "bounded-worker-stderr.txt"}

    def apply(target, saved, image_hash):
        ctx.apply(target, saved, image_hash)
        raise WorkerError("Uncertain worker SET completion")

    ctx.kwargs["voltage_replay"] = apply
    result = ro.execute(ctx.receipt, **ctx.kwargs)
    assert result["state"] == "recovery-required", result["message"]
    assert not result["ok"] and not ctx.registry.values
    assert len(ctx.writes) == 2 and len(ctx.restarts) == 2
    assert [write[2] for write in ctx.writes] == [result["after_image_sha256"], result["before_image_sha256"]]
    failed = [event for event in result["events"] if event["event"] == "voltage-replay-failed"]
    assert [event["phase"] for event in failed] == ["apply", "rollback"]
    assert all(event["worker_paths"] == WorkerError.paths for event in failed)
    assert ro.active_receipt(ro.Target(**result["target"])) == result


@pytest.mark.parametrize("restore", [False, True])
@pytest.mark.parametrize(
    "mutation",
    ["version", "missing", "missing_version", "schema", "extra", "shape", "identity", "incomplete", "policy", "packet"],
)
def test_malformed_saved_replay_is_purely_rejected_before_any_mutation(monkeypatch, restore, mutation):
    ctx = scenario(monkeypatch)
    receipt = ctx.receipt
    saved = receipt["voltage_replay_contracts"]
    if mutation == "version":
        receipt["voltage_replay_contract_version"] = True
    elif mutation == "missing":
        receipt.pop("voltage_replay_contracts")
    elif mutation == "missing_version":
        receipt.pop("voltage_replay_contract_version")
    elif mutation == "schema":
        saved["schema"] = True
    elif mutation == "extra":
        saved["unused"] = None
    elif mutation == "shape":
        saved["before"] = None
    elif mutation == "identity":
        saved["before"]["identity"]["device_id"] = float(saved["before"]["identity"]["device_id"])
    elif mutation == "incomplete":
        saved["before"]["complete"] = False
    elif mutation == "policy":
        saved["before"]["policy"]["rails"]["0"]["boost_uv"] += 1
    else:
        raw = bytearray.fromhex(saved["before"]["public"]["0"])
        raw[-1] ^= 1
        saved["before"]["public"]["0"] = raw.hex()
    if restore:
        ctx.registry.values = copy.deepcopy(receipt["after"])
    ctx.kwargs["gpu_factory"] = lambda slot: pytest.fail("Malformed replay triggered GPU GET")
    result = ro.execute(receipt, restore=restore, **ctx.kwargs)
    assert result["state"] == "failed" and not result["ok"]
    assert not ctx.registry.operations and not ctx.restarts and not ctx.writes and not ctx.captures


@pytest.mark.parametrize("marker", [False, True])
def test_nonzero_without_saved_replay_refuses_apply_but_permits_cleanup(monkeypatch, marker):
    ctx = scenario(monkeypatch)
    if marker:
        ctx.receipt["voltage_replay_contracts"] = {}
    else:
        ctx.receipt.pop("voltage_replay_contract_version")
        ctx.receipt.pop("voltage_replay_contracts")
    result = ro.execute(ctx.receipt, **ctx.kwargs)
    assert result["state"] == "failed" and not ctx.registry.operations and not ctx.restarts
    ctx.registry.values = copy.deepcopy(ctx.receipt["after"])
    # Legacy cleanup is allowed but cannot claim exact nonzero request recovery.
    result = ro.execute(ctx.receipt, restore=True, **ctx.kwargs)
    assert result["state"] == "recovery-required"
    assert not ctx.registry.values and not ctx.writes and not ctx.captures


def test_receipt_roundtrip_including_compact_events_stays_below_existing_size_limit(monkeypatch, tmp_path):
    ctx = scenario(monkeypatch)
    gpu = prepare_setup(monkeypatch, tmp_path, ctx)
    prepared = ro.prepare(gpu, ctx.receipt["changes"], registry=ctx.registry)
    ctx.receipt = prepared
    path = Path(prepared["path"])
    ctx.kwargs["save"] = ro.startup.atomic_json
    original_apply = ctx.apply

    def apply(target, saved, image_hash):
        # These bulky worker observations must not be copied into journal events.
        result = original_apply(target, saved, image_hash)
        result["before"] = copy.deepcopy(saved)
        result["native_diagnostics"] = copy.deepcopy(ctx.before["equations"])
        return result

    ctx.kwargs["voltage_replay"] = apply

    def reload_exact(receipt):
        assert path.stat().st_size < 8 * 1024 * 1024
        assert ro.load_receipt(path) == receipt
        encoded_events = json.dumps(receipt["events"], allow_nan=False)
        assert len(encoded_events) < 10000
        assert "info_hex" not in encoded_events and "control_hex" not in encoded_events
        assert "native_diagnostics" not in encoded_events and "public" not in encoded_events
        return ro.load_receipt(path)

    prepared = reload_exact(prepared)
    # The fixture's event assertion refers to the executing receipt object.
    ctx.receipt = prepared
    result = ro.execute(prepared, **ctx.kwargs)
    assert result["state"] == "active", result["message"]
    active = reload_exact(result)
    assert ro.active_receipt(ro.Target(**active["target"])) == active
    ctx.receipt = active
    result = ro.execute(active, restore=True, **ctx.kwargs)
    assert result["state"] == "restored", result["message"]
    reload_exact(result)
    assert ro.active_receipt(ro.Target(**result["target"])) is None
    assert ctx.snapshot["voltage_ceiling"] == ctx.before and not ctx.registry.values
