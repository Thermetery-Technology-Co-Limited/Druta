"""Direct startup REL controls: activation, request preservation, and cleanup."""

import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from druta import registry_overrides as ro
from druta import voltage_startup_runtime as runtime
from tests.test_registry_overrides import transaction
from tests.test_voltage_startup_runtime import observation, policy_offsets


@pytest.fixture(autouse=True)
def fake_policy(monkeypatch):
    monkeypatch.setattr(runtime.requests.voltage, "read", lambda gpu: gpu.policy())
    monkeypatch.setattr(ro.time, "sleep", lambda seconds: None)


def scenario(monkeypatch, tmp_path, *, enabled=True, boost=0, original=None):
    receipt, registry, _, restarts, kwargs = transaction(monkeypatch)
    target = ro.Target(**receipt["target"])
    before = observation(boost=boost, slot=target.slot)
    saved = runtime.from_observation(before, {runtime.KEY: enabled})
    original = {} if original is None else copy.deepcopy(original)
    image_hash = hashlib.sha256(b"abcdef").hexdigest()
    name = next((key for key in original if key.casefold() == runtime.REGISTRY_NAME.casefold()), runtime.REGISTRY_NAME)
    after_registry = {**original, name: {"type": 4, "value": 1 if enabled else 2}}
    registry.values = copy.deepcopy(original)
    snapshot = {"target": target, "image": b"abcdef", "voltage_ceiling": {"equations": before["equations"]}}
    monkeypatch.setattr(ro, "state_directory", lambda: tmp_path)
    receipt.update(
        schema=1,
        id="e" * 32,
        path=str(tmp_path / ("e" * 32 + ".json")),
        changes={},
        plan={"baseline_sha256": image_hash, "expected_sha256": image_hash, "patches": []},
        verify_bios=True,
        before_image_sha256=image_hash,
        after_image_sha256=image_hash,
        before=original,
        after=after_registry,
        startup_changes={runtime.KEY: enabled},
        voltage_startup_contract_version=1,
        voltage_startup_contracts=saved,
    )
    ctx = SimpleNamespace(
        receipt=receipt,
        registry=registry,
        target=target,
        snapshot=snapshot,
        before=before,
        saved=saved,
        kwargs=kwargs,
        restarts=restarts,
        current=copy.deepcopy(before["requests"]),
        captures=[],
        writes=[],
        timeline=[],
    )
    replace = registry.replace

    def replace_recorded(expected, desired):
        ctx.timeline.append("registry")
        replace(expected, desired)

    registry.replace = replace_recorded

    def capture(target):
        assert target == ctx.target
        ctx.timeline.append("capture")
        ctx.captures.append(copy.deepcopy(ctx.current))
        return copy.deepcopy(ctx.current)

    def restart(target):
        ctx.timeline.append("restart")
        restarts.append(target)
        active = registry.values == ctx.receipt["after"]
        configured = saved["after_requests"] if active else before["requests"]
        # Restart can clear current runtime requests. The initializer affects
        # only selected REL words; final replay must preserve the captured rest.
        values = {index: [0] * 6 for index in configured["public"]}
        if active:
            for index in saved["affected_indices"]:
                values[index][0] = -50000 if enabled else 0
        ctx.current = policy_offsets(runtime.requests.project(configured, offsets_uv=values, boost_percent=0))
        snapshot["image"] = b"abcdef"
        snapshot["voltage_ceiling"] = {"equations": copy.deepcopy(before["equations"])}
        return ro.devicereset.RestartResult(True, False, "restarted", 0)

    def replay(target, wanted, image_hash):
        ctx.timeline.append("replay")
        assert target == ctx.target and image_hash == ctx.receipt["before_image_sha256"]
        ctx.writes.append(
            {"desired": copy.deepcopy(wanted), "registry": copy.deepcopy(registry.values), "image": image_hash}
        )
        ctx.current = policy_offsets(copy.deepcopy(wanted))
        return {"verified": True, "status": 0, "after": copy.deepcopy(ctx.current)}

    kwargs.update(
        gpu_factory=lambda slot: snapshot,
        bind=lambda gpu: target,
        reader=lambda gpu: snapshot["image"],
        restart=restart,
        voltage_capture=capture,
        voltage_replay=replay,
    )
    ctx.capture, ctx.restart, ctx.replay = capture, restart, replay
    return ctx


def prepare_setup(monkeypatch, ctx):
    monkeypatch.setattr(ro, "bind_target", lambda gpu: ctx.target)
    monkeypatch.setattr(ro, "read_vbios", lambda gpu: b"abcdef")
    monkeypatch.setattr(ro, "read_model_inputs", lambda gpu: (None, "unrelated model inputs unavailable"))
    monkeypatch.setattr(runtime, "read_checked", lambda gpu: copy.deepcopy(ctx.before))
    return SimpleNamespace(nvapi=SimpleNamespace(_i=lambda *args: object()))


@pytest.mark.parametrize("enabled", [False, True])
def test_prepare_saves_explicit_dword_and_compact_before_after_contract(monkeypatch, tmp_path, enabled):
    ctx = scenario(monkeypatch, tmp_path, enabled=enabled)
    prepared = ro.prepare(
        prepare_setup(monkeypatch, ctx), {}, registry=ctx.registry, startup_changes={runtime.KEY: enabled}
    )
    assert prepared["voltage_startup_contracts"] == ctx.saved
    assert prepared["after"] == {runtime.REGISTRY_NAME: {"type": 4, "value": 1 if enabled else 2}}
    assert prepared["before_image_sha256"] == prepared["after_image_sha256"]
    assert prepared["plan"]["patches"] == []
    assert Path(prepared["path"]).stat().st_size < 4 * 1024 * 1024
    assert ro.load_receipt(prepared["path"]) == prepared
    assert not ctx.registry.operations and not ctx.restarts and not ctx.writes


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("boost", [0, 1])
def test_apply_and_cleanup_first_undo_preserve_all_captured_requests(monkeypatch, tmp_path, enabled, boost):
    ctx = scenario(monkeypatch, tmp_path, enabled=enabled, boost=boost)
    result = ro.execute(ctx.receipt, **ctx.kwargs)
    assert result["state"] == "active", result["message"]
    assert result["runtime_verified_fields"] == [runtime.KEY]
    assert ctx.registry.values == result["after"]
    assert runtime.requests._requested(ctx.current) == runtime.requests._requested(ctx.saved["after_requests"])
    assert ctx.writes[0]["desired"] == ctx.saved["after_requests"]
    assert ctx.current["boost_percent"] == boost
    ctx.kwargs["gpu_factory"] = lambda slot: (
        ctx.snapshot if ctx.registry.values == result["before"] else pytest.fail("Undo read before registry cleanup")
    )
    result = ro.execute(ctx.receipt, restore=True, **ctx.kwargs)
    assert result["state"] == "restored", result["message"]
    assert ctx.current == ctx.before["requests"] and ctx.registry.values == result["before"]
    assert ctx.writes[-1]["desired"] == ctx.before["requests"] and len(ctx.restarts) == 2


def test_ignored_initializer_fails_before_working_set_can_mask_it(monkeypatch, tmp_path):
    ctx = scenario(monkeypatch, tmp_path)

    def restart(target):
        result = ctx.restart(target)
        if ctx.registry.values == ctx.receipt["after"]:
            ctx.current = copy.deepcopy(ctx.before["requests"])
        return result

    ctx.kwargs["restart"] = restart
    result = ro.execute(ctx.receipt, **ctx.kwargs)
    assert result["state"] == "rolled-back", result["message"]
    assert "did not activate before request replay" in result["message"]
    assert ctx.current == ctx.before["requests"] and not ctx.registry.values
    assert len(ctx.writes) == 1 and ctx.writes[0]["registry"] == ctx.receipt["before"]


def test_successful_set_status_with_ignored_requests_fails_exact_readback(monkeypatch, tmp_path):
    ctx = scenario(monkeypatch, tmp_path, boost=1)

    def replay(target, wanted, image_hash):
        if ctx.registry.values == ctx.receipt["after"]:
            ctx.writes.append({"desired": copy.deepcopy(wanted), "registry": copy.deepcopy(ctx.registry.values)})
            return {"verified": True, "status": 0, "after": copy.deepcopy(ctx.current)}
        return ctx.replay(target, wanted, image_hash)

    ctx.kwargs["voltage_replay"] = replay
    result = ro.execute(ctx.receipt, **ctx.kwargs)
    assert result["state"] == "rolled-back", result["message"]
    assert "did not preserve" in result["message"]
    assert ctx.current == ctx.before["requests"] and not ctx.registry.values and len(ctx.writes) == 2


def test_failed_original_offset_restore_keeps_old_registry_and_recovery_pointer(monkeypatch, tmp_path):
    original = {"fixblackscreenissue": {"type": 4, "value": 17}}
    ctx = scenario(monkeypatch, tmp_path, boost=1, original=original)
    ctx.kwargs["save"] = ro.startup.atomic_json
    assert ro.execute(ctx.receipt, **ctx.kwargs)["state"] == "active"

    def ignored(target, wanted, image_hash):
        ctx.writes.append({"desired": copy.deepcopy(wanted), "registry": copy.deepcopy(ctx.registry.values)})
        return {"verified": True, "status": 0, "after": copy.deepcopy(ctx.current)}

    ctx.kwargs["voltage_replay"] = ignored
    ctx.kwargs["gpu_factory"] = lambda slot: (
        ctx.snapshot if ctx.registry.values == original else pytest.fail("Undo read before registry cleanup")
    )
    result = ro.execute(ctx.receipt, restore=True, **ctx.kwargs)
    assert result["state"] == "recovery-required", result["message"]
    assert ctx.registry.values == original and not result["ok"]
    assert all(operation == original for operation in ctx.registry.operations[1:])
    assert len(ctx.restarts) == 2 and ro.active_receipt(ctx.target) == result


@pytest.mark.parametrize(
    "entry", [None, {"type": 4, "value": 17}, {"type": 3, "value": "deadbeef"}, {"type": 1, "value": "inherited"}]
)
def test_exact_original_registry_type_value_or_absence_is_restored(monkeypatch, tmp_path, entry):
    original = {} if entry is None else {"fixblackscreenissue": entry}
    ctx = scenario(monkeypatch, tmp_path, original=original)
    assert ro.execute(ctx.receipt, **ctx.kwargs)["state"] == "active"
    result = ro.execute(ctx.receipt, restore=True, **ctx.kwargs)
    assert result["state"] == "restored", result["message"]
    assert ctx.registry.values == original and ctx.receipt["before"] == original


@pytest.mark.parametrize("failure", ["partial", "transient"])
def test_prepare_capture_failure_stops_before_registry_change(monkeypatch, tmp_path, failure):
    ctx = scenario(monkeypatch, tmp_path)
    gpu = prepare_setup(monkeypatch, ctx)
    if failure == "partial":
        partial = observation(extra=(7,), slot=ctx.target.slot)
        monkeypatch.setattr(runtime, "read_checked", lambda gpu: partial)
    else:

        def unavailable(gpu):
            raise runtime.VoltageStartupError("transient", "Startup GET temporarily unavailable")

        monkeypatch.setattr(runtime, "read_checked", unavailable)
    with pytest.raises(RuntimeError):
        ro.prepare(gpu, {}, registry=ctx.registry, startup_changes={runtime.KEY: True})
    assert not ctx.registry.operations and not ctx.restarts and not ctx.writes


@pytest.mark.parametrize("group", ["changes", "named_changes"])
def test_startup_isolated_from_other_change_groups(monkeypatch, tmp_path, group):
    ctx = scenario(monkeypatch, tmp_path)
    monkeypatch.setattr(ro, "bind_target", lambda gpu: ctx.target)
    monkeypatch.setattr(ro, "read_vbios", lambda gpu: pytest.fail("Mixed prepare performed read"))
    arguments = {"startup_changes": {runtime.KEY: True}, "changes": {}, group: {"other": 1}}
    with pytest.raises(ro.OverrideError, match="separately"):
        ro.prepare(object(), registry=ctx.registry, **arguments)
    assert not ctx.registry.operations and not ctx.restarts


def test_request_changed_after_preview_refuses_before_first_write(monkeypatch, tmp_path):
    ctx = scenario(monkeypatch, tmp_path)
    ctx.current = runtime.requests.project(ctx.current, boost_percent=1)
    result = ro.execute(ctx.receipt, **ctx.kwargs)
    assert result["state"] == "failed" and not result["ok"]
    assert not ctx.registry.operations and not ctx.restarts and not ctx.writes


def test_changed_equation_peer_fails_before_replay_and_restores_original(monkeypatch, tmp_path):
    ctx = scenario(monkeypatch, tmp_path)

    def restart(target):
        result = ctx.restart(target)
        if ctx.registry.values == ctx.receipt["after"]:
            definitions = ctx.snapshot["voltage_ceiling"]["equations"]
            raw = bytearray.fromhex(definitions["control_hex"])
            raw[-1] ^= 1
            definitions["control_hex"] = raw.hex()
        return result

    ctx.kwargs["restart"] = restart
    result = ro.execute(ctx.receipt, **ctx.kwargs)
    assert result["state"] == "rolled-back", result["message"]
    assert "loaded VFE definitions" in result["message"]
    assert all(write["registry"] == ctx.receipt["before"] for write in ctx.writes)
    assert not ctx.registry.values and ctx.current == ctx.before["requests"]


def test_set_failure_rolls_back_and_replays_original_once(monkeypatch, tmp_path):
    ctx = scenario(monkeypatch, tmp_path)

    def replay(target, wanted, image_hash):
        result = ctx.replay(target, wanted, image_hash)
        if ctx.registry.values == ctx.receipt["after"]:
            raise runtime.requests.ReplayError("set", "Partial SET failure", attempted=True, result=result)
        return result

    ctx.kwargs["voltage_replay"] = replay
    result = ro.execute(ctx.receipt, **ctx.kwargs)
    assert result["state"] == "rolled-back", result["message"]
    assert len(ctx.writes) == 2 and len(ctx.restarts) == 2
    assert ctx.current == ctx.before["requests"] and not ctx.registry.values


def test_existing_other_control_remains_independent_of_startup_getters(monkeypatch):
    receipt, registry, _, restarts, kwargs = transaction(monkeypatch)
    monkeypatch.setattr(runtime, "read_checked", lambda gpu: pytest.fail("Unrelated startup GET"))
    kwargs["voltage_capture"] = lambda target: pytest.fail("Unrelated request capture")
    kwargs["voltage_replay"] = lambda *args: pytest.fail("Unrelated request replay")
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "active", result["message"]
    assert registry.values == receipt["after"] and len(restarts) == 1
