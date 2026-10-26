"""Offline fresh-worker protocol, durable attempts, and uncertain outcomes."""

import copy
import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from druta import nvbackend, startup
from druta import registry_overrides as ro
from druta import voltage_replay_worker as worker
from druta import voltage_request_replay as replay
from tests.test_voltage_request_replay import Gpu

IMAGE = b"synthetic currently running image"
IMAGE_HASH = hashlib.sha256(IMAGE).hexdigest()


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(ro, "state_directory", lambda: tmp_path)
    monkeypatch.setattr(replay.voltage, "read", lambda gpu: gpu.policy())
    template = Gpu()
    ident = template.identity
    target = ro.Target(
        ident["slot"],
        f"PCI\\VEN_10DE&DEV_{ident['device_id']:04X}&SUBSYS_{ident['subsystem_id']:08X}&REV_01\\UNIQUE",
        r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\0472",
    )
    calls, made, images = [], [], []

    def factory(slot):
        assert slot == target.slot
        gpu = Gpu()
        made.append(gpu)
        return gpu

    def image(gpu):
        images.append(gpu)
        return IMAGE

    monkeypatch.setattr(nvbackend, "GPU", factory)
    monkeypatch.setattr(ro, "bind_target", lambda gpu: target)
    monkeypatch.setattr(ro, "read_vbios", image)

    def launch(command, **kwargs):
        calls.append((command, kwargs))
        code = worker.run_worker(command[-2], command[-1])
        return SimpleNamespace(returncode=code, stdout=b"", stderr=b"")

    monkeypatch.setattr(worker.subprocess, "run", launch)
    state = replay.read_checked(template)
    return SimpleNamespace(
        target=target, state=state, calls=calls, made=made, images=images, launch=launch, directory=tmp_path
    )


def request(env, mode="apply", *, job_id="a" * 32):
    data = {"schema": 1, "job_id": job_id, "mode": mode, "target": worker._target_dict(env.target)}
    if mode == "apply":
        data.update(saved=copy.deepcopy(env.state), expected_image_sha256=IMAGE_HASH)
    paths = worker._paths(job_id)
    worker._write_new(paths["request"], data)
    return data, paths


def test_capture_is_fresh_complete_and_strictly_readonly(env):
    first, second = worker.capture(env.target), worker.capture(env.target)
    assert first == second == env.state
    assert len(env.made) == len(env.calls) == 2
    assert env.made[0] is not env.made[1]
    assert not env.images and not any(gpu.set_calls for gpu in env.made)
    assert len(list(env.directory.glob("*.request.json"))) == 2
    assert not list(env.directory.glob("*.attempt.json"))
    assert env.calls[0][1] == {
        "capture_output": True,
        "timeout": 55,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }


@pytest.mark.parametrize("frozen", [False, True])
def test_source_and_frozen_launcher_use_current_installation(env, monkeypatch, frozen):
    monkeypatch.setattr(startup.sys, "frozen", frozen, raising=False)
    if frozen:
        monkeypatch.setattr(startup.sys, "executable", str(env.directory / "portable Druta.exe"))
    worker.capture(env.target)
    command = env.calls[0][0]
    assert "--voltage-replay-worker" in command
    if frozen:
        assert command[0] == str((env.directory / "portable Druta.exe").resolve())
        assert len(command) == 4
    else:
        assert command[1].endswith("druta.py")
    assert Path(command[-2]).parent == Path(command[-1]).parent == env.directory


def test_apply_checks_image_and_sets_once_after_durable_attempt(env, monkeypatch):
    original = replay.replay
    observed = []

    def checked(gpu, state):
        markers = list(env.directory.glob("*.attempt.json"))
        assert len(markers) == 1
        marker, _ = worker._read_record(markers[0])
        assert marker["state"] == "attempted" and marker["expected_image_sha256"] == IMAGE_HASH
        observed.append(marker)
        return original(gpu, state)

    monkeypatch.setattr(replay, "replay", checked)
    desired = replay.project(
        env.state, boost_percent=41, offsets_uv={"1": [-33333, 22222, 11111, -44444, 55555, -66666]}
    )
    result = worker.apply(env.target, desired, IMAGE_HASH)
    assert result["verified"] is True and replay._requested(result["after"]) == replay._requested(desired)
    assert len(observed) == len(env.images) == len(env.made[0].set_calls) == 1
    assert set(result["worker_paths"]) == {"request", "attempt", "result"}
    assert all(Path(path).is_file() for path in result["worker_paths"].values())


@pytest.mark.parametrize("kind", ["unsupported", "transient", "malformed", "identity"])
def test_capture_preserves_typed_failure(env, monkeypatch, kind):
    def fail(gpu):
        raise replay.ReplayError(kind, "bounded capture failure")

    monkeypatch.setattr(replay, "read_checked", fail)
    with pytest.raises(worker.WorkerError) as raised:
        worker.capture(env.target)
    assert raised.value.kind == kind
    assert raised.value.attempted is raised.value.uncertain is False
    assert not env.images and len(env.calls) == 1


def test_incomplete_capture_is_unsupported_without_any_set(env, monkeypatch):
    partial = replay.read_checked(Gpu(extra=(7,)))
    monkeypatch.setattr(replay, "read_checked", lambda gpu: partial)
    with pytest.raises(worker.WorkerError, match="every independently present") as raised:
        worker.capture(env.target)
    assert raised.value.kind == "unsupported" and not raised.value.attempted
    assert not any(gpu.set_calls for gpu in env.made)


@pytest.mark.parametrize("which", ["slot", "device_id", "subsystem_id"])
def test_saved_identity_must_match_target_before_launch(env, which):
    saved = copy.deepcopy(env.state)
    replacement = "0000:75:1a.5" if which == "slot" else 1
    saved["identity"][which] = saved["policy"]["identity"][which] = replacement
    with pytest.raises(worker.WorkerError) as raised:
        worker.apply(env.target, saved, IMAGE_HASH)
    assert raised.value.kind == "identity" and not env.calls


@pytest.mark.parametrize("bad", [None, "", "ab", "A" * 64, "g" * 64, 1, True])
def test_image_hash_is_strict_before_launch(env, bad):
    with pytest.raises(worker.WorkerError):
        worker.apply(env.target, env.state, bad)
    assert not env.calls and not list(env.directory.glob("*.request.json"))


def test_fresh_image_mismatch_is_prewrite_failure(env, monkeypatch):
    monkeypatch.setattr(ro, "read_vbios", lambda gpu: b"changed image")
    with pytest.raises(worker.WorkerError, match="running image differs") as raised:
        worker.apply(env.target, env.state, IMAGE_HASH)
    assert raised.value.kind == "identity"
    assert not raised.value.attempted and not raised.value.uncertain
    assert not env.made[0].set_calls and not list(env.directory.glob("*.attempt.json"))


def test_fresh_target_mismatch_prevents_capture_or_write(env, monkeypatch):
    monkeypatch.setattr(
        ro,
        "bind_target",
        lambda gpu: ro.Target(env.target.slot, env.target.instance_id, env.target.driver_key[:-4] + "0473"),
    )
    with pytest.raises(worker.WorkerError) as raised:
        worker.apply(env.target, env.state, IMAGE_HASH)
    assert raised.value.kind == "identity" and not raised.value.attempted
    assert not env.images and not env.made[0].set_calls


@pytest.mark.parametrize("mode", ["capture", "apply"])
def test_timeout_is_not_retried_and_retains_journal_paths(env, monkeypatch, mode):
    calls = []

    def timeout(command, **kwargs):
        calls.append(command)
        raise subprocess.TimeoutExpired(command, 55)

    monkeypatch.setattr(worker.subprocess, "run", timeout)
    with pytest.raises(worker.WorkerError) as raised:
        worker.capture(env.target) if mode == "capture" else worker.apply(env.target, env.state, IMAGE_HASH)
    error = raised.value
    assert error.kind == "timeout" and error.uncertain is (mode == "apply")
    assert error.attempted is False and len(calls) == 1
    assert Path(error.paths["request"]).is_file()


def test_timeout_after_attempt_is_uncertain(env, monkeypatch):
    def timeout(command, **kwargs):
        value, checksum = worker._read_record(Path(command[-2]))
        worker._write_new(worker._paths(value["job_id"])["attempt"], worker._attempt(value, checksum))
        raise subprocess.TimeoutExpired(command, 55)

    monkeypatch.setattr(worker.subprocess, "run", timeout)
    with pytest.raises(worker.WorkerError) as raised:
        worker.apply(env.target, env.state, IMAGE_HASH)
    assert raised.value.attempted and raised.value.uncertain


@pytest.mark.parametrize("damage", ["missing", "invalid_json", "oversized", "duplicate"])
def test_missing_or_corrupt_result_never_claims_success(env, monkeypatch, damage):
    def fail(command, **kwargs):
        path = Path(command[-1])
        if damage != "missing":
            content = {"invalid_json": "{", "oversized": " " * (worker.MAX_BYTES + 1), "duplicate": '{"a":1,"a":2}'}[
                damage
            ]
            path.write_text(content)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(worker.subprocess, "run", fail)
    with pytest.raises(worker.WorkerError) as raised:
        worker.apply(env.target, env.state, IMAGE_HASH)
    assert raised.value.uncertain and raised.value.paths


@pytest.mark.parametrize(
    "damage", ["verified", "after_request", "desired", "target", "request_hash", "attempted", "schema"]
)
def test_parent_revalidates_success_semantics(env, monkeypatch, damage):
    def corrupt(command, **kwargs):
        outcome = env.launch(command, **kwargs)
        path = Path(command[-1])
        data = json.loads(path.read_text())
        if damage == "verified":
            data["payload"]["verified"] = False
        elif damage == "after_request":
            data["payload"]["after"] = replay.project(data["payload"]["after"], boost_percent=99)
        elif damage == "desired":
            data["payload"]["desired"] = replay.project(data["payload"]["desired"], boost_percent=99)
        elif damage == "target":
            data["target"]["driver_key"] = "wrong"
        elif damage == "request_hash":
            data["request_sha256"] = "0" * 64
        elif damage == "attempted":
            data["attempted"] = False
        else:
            data["schema"] = True
        path.write_text(json.dumps(data))
        return outcome

    monkeypatch.setattr(worker.subprocess, "run", corrupt)
    with pytest.raises(worker.WorkerError) as raised:
        worker.apply(env.target, env.state, IMAGE_HASH)
    assert raised.value.uncertain and len(env.made[0].set_calls) == 1


def test_failed_set_keeps_partial_readback_and_never_retries(env, monkeypatch):
    original = nvbackend.GPU

    def gpu_factory(slot):
        gpu = original(slot)

        def fail(gpu, data):
            gpu.apply(data, first_only=True)
            return -7

        gpu.set_fault = fail
        return gpu

    monkeypatch.setattr(nvbackend, "GPU", gpu_factory)
    desired = replay.project(env.state, boost_percent=99, offsets_uv={"0": [11] * 6, "1": [22] * 6})
    with pytest.raises(worker.WorkerError) as raised:
        worker.apply(env.target, desired, IMAGE_HASH)
    assert raised.value.kind == "partial" and raised.value.attempted and raised.value.uncertain
    assert raised.value.result["after"] is not None
    assert raised.value.result["verified"] is False and len(env.made[0].set_calls) == 1


def test_attempt_durability_failure_prevents_set(env, monkeypatch):
    original = worker._write_new

    def fail(path, value):
        if path.name.endswith(".attempt.json"):
            raise OSError("cannot flush attempt")
        return original(path, value)

    monkeypatch.setattr(worker, "_write_new", fail)
    with pytest.raises(worker.WorkerError) as raised:
        worker.apply(env.target, env.state, IMAGE_HASH)
    assert not raised.value.attempted and not raised.value.uncertain
    assert len(env.made) == 1 and not env.made[0].set_calls


@pytest.mark.parametrize("kind", ["attempt", "result"])
def test_child_rejects_reused_job_without_opening_gpu(env, kind):
    _, paths = request(env)
    worker._write_new(paths[kind], {"sentinel": True})
    with pytest.raises(worker.WorkerError, match="already been used"):
        worker.run_worker(paths["request"], paths["result"])
    assert not env.made
    assert json.loads(paths[kind].read_text()) == {"sentinel": True}


def test_child_rejects_cross_job_and_external_paths(env, tmp_path):
    _, paths = request(env)
    with pytest.raises(worker.WorkerError, match="assigned job"):
        worker.run_worker(paths["request"], tmp_path / "outside.json")
    assert not env.made


def test_record_creation_never_overwrites(env):
    path = env.directory / "sentinel.json"
    worker._write_new(path, {"saved": 1})
    with pytest.raises(FileExistsError):
        worker._write_new(path, {"saved": 2})
    assert json.loads(path.read_text()) == {"saved": 1}


@pytest.mark.parametrize(
    "mutate", [lambda v: v.update(schema=True), lambda v: v.update(job_id="b" * 32), lambda v: v.update(unexpected=1)]
)
def test_malformed_child_request_rejected_before_gpu(env, mutate):
    value, paths = request(env)
    mutate(value)
    paths["request"].write_text(json.dumps(value))
    with pytest.raises(worker.WorkerError):
        worker.run_worker(paths["request"], paths["result"])
    assert not env.made


def test_cli_uses_the_same_bounded_protocol(env):
    _, paths = request(env, mode="capture")
    assert worker.worker_cli([str(paths["request"]), str(paths["result"])]) == 0
    assert not env.images and not env.made[0].set_calls


def test_spawn_failure_is_known_prewrite_failure(env, monkeypatch):
    def fail(*args, **kwargs):
        raise FileNotFoundError("installation executable disappeared")

    monkeypatch.setattr(worker.subprocess, "run", fail)
    with pytest.raises(worker.WorkerError) as raised:
        worker.apply(env.target, env.state, IMAGE_HASH)
    assert not raised.value.attempted and not raised.value.uncertain
    assert Path(raised.value.paths["request"]).is_file()


def test_parent_interruption_retains_uncertain_journal_paths(env, monkeypatch):
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(worker.subprocess, "run", interrupt)
    with pytest.raises(KeyboardInterrupt) as raised:
        worker.apply(env.target, env.state, IMAGE_HASH)
    assert raised.value.uncertain and not raised.value.attempted
    assert Path(raised.value.paths["request"]).is_file()


def test_child_interruption_keeps_partial_result(env, monkeypatch):
    original = nvbackend.GPU

    def gpu_factory(slot):
        gpu = original(slot)

        def interrupt(gpu, data):
            gpu.apply(data, first_only=True)
            raise KeyboardInterrupt()

        gpu.set_fault = interrupt
        return gpu

    monkeypatch.setattr(nvbackend, "GPU", gpu_factory)
    desired = replay.project(env.state, boost_percent=99)
    with pytest.raises(worker.WorkerError) as raised:
        worker.apply(env.target, desired, IMAGE_HASH)
    assert raised.value.attempted and raised.value.uncertain
    assert raised.value.result["after"] is not None and len(env.made[0].set_calls) == 1


def test_fsync_failure_of_attempt_prevents_set_even_with_partial_file(env, monkeypatch):
    original = worker.os.fsync

    def fail(fd):
        if list(env.directory.glob("*.attempt.json")) and not list(env.directory.glob("*.result.json")):
            raise OSError("durability failed")
        return original(fd)

    monkeypatch.setattr(worker.os, "fsync", fail)
    with pytest.raises(worker.WorkerError) as raised:
        worker.apply(env.target, env.state, IMAGE_HASH)
    assert not env.made[0].set_calls
    assert raised.value.attempted is False and raised.value.uncertain is False
    assert list(env.directory.glob("*.attempt.json"))


def test_result_durability_failure_after_set_is_uncertain(env, monkeypatch):
    original = worker._write_new

    def fail(path, data):
        if path.name.endswith(".result.json"):
            raise OSError("disk full after SET")
        return original(path, data)

    monkeypatch.setattr(worker, "_write_new", fail)
    with pytest.raises(worker.WorkerError) as raised:
        worker.apply(env.target, env.state, IMAGE_HASH)
    assert raised.value.attempted and raised.value.uncertain
    assert len(env.made[0].set_calls) == 1


def test_parent_rejects_corrupt_attempt_marker(env, monkeypatch):
    def corrupt(command, **kwargs):
        outcome = env.launch(command, **kwargs)
        value = json.loads(Path(command[-2]).read_text())
        worker._paths(value["job_id"])["attempt"].write_text("{}")
        return outcome

    monkeypatch.setattr(worker.subprocess, "run", corrupt)
    with pytest.raises(worker.WorkerError, match="attempt marker") as raised:
        worker.apply(env.target, env.state, IMAGE_HASH)
    assert raised.value.attempted and raised.value.uncertain


def test_child_records_replay_success_before_late_target_mismatch(env, monkeypatch):
    def bind(gpu):
        return (
            ro.Target(env.target.slot, env.target.instance_id, env.target.driver_key[:-4] + "0473")
            if gpu.set_calls
            else env.target
        )

    monkeypatch.setattr(ro, "bind_target", bind)
    with pytest.raises(worker.WorkerError) as raised:
        worker.apply(env.target, env.state, IMAGE_HASH)
    assert raised.value.attempted and raised.value.uncertain
    assert raised.value.result["verified"] is True


def test_capture_never_reads_image_even_if_image_getter_is_unavailable(env, monkeypatch):
    def forbidden(gpu):
        pytest.fail("read-only request capture must not require BIOS capability")

    monkeypatch.setattr(ro, "read_vbios", forbidden)
    assert worker.capture(env.target) == env.state


def test_request_durability_failure_is_prelaunch_and_never_retried(env, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("request storage unavailable")

    monkeypatch.setattr(worker, "_write_new", fail)
    with pytest.raises(worker.WorkerError) as raised:
        worker.apply(env.target, env.state, IMAGE_HASH)
    assert not raised.value.attempted and not raised.value.uncertain and raised.value.paths
    assert not env.calls and not env.made
