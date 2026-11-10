# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The shared fake driver (tests/fake_driver.py) proven at the backend level.

Every case runs one session on a fresh card of the parametrized kind, opened through
druta.gpu_backend.open_gpu, and checks the outcome against literal anchors. Under the "native"
parameter the same session also runs on the Python backend, on a fresh card built with the same
options, and the two outcomes must be EQUAL: the results, the driver-call log of what the session
did, the card's state afterwards, every rig's own write log and the fake's dispatch errors.

What is compared is what the session did after the card was opened. Construction is compared in
its own cases (ConstructionTests), because the native object reads the capability keys of
`static` lazily (below); every other case first merges them on the native object, so both kinds
start the compared part from the same calls.

Recorded divergences, asserted where they show and never compared away silently:
- construction: nvbackend.GPU reads the capability keys of `static` in __init__; native.Gpu reads
  them on their first use (src/druta/native.py, class _Static). The native construction log plus
  the log of the first capability-key access equals the Python construction log.
- `nvapi.selected` / `nvml.selected`: the native records lack Python's `enum_index` and `name`
  (native/DESIGN-FRONTEND.md section (c), "Pairing").
- read(): the core does not provide the Turing legacy clock-lock fallback yet (src/druta/native.py,
  Gpu.read docstring: "Not yet provided by the core ...: the Turing legacy clock-lock fallback").
  On the modern (Turing-class) card nvbackend tries it on every tick; its in-memory capture loads
  kernel32 (served by the fake as a refused VirtualProtect, see tests/fake_driver._Kernel32) and
  gives up, so the Python log carries one attempt, exactly K32, right after the BoostLock read and
  no driver call. python_log() removes only whole attempts in that position (anything else fails),
  test_read_ticks pins one attempt per modern tick and none elsewhere, and no kernel32 entry may
  appear on the native kind or in any construction log.

Errors inside the fake (an exception in any fake callback, which ctypes would otherwise swallow, or
a tripped clock guard) land in driver.errors on both kinds; every case requires it empty for both
kinds' runs.

Differences found here that no document records are pinned as FINDINGs (the exact difference is
asserted, so a change in either backend fails the case): see test_read_ticks and
TestConstruction.
"""
import ctypes
import gc
import os
import sys
import threading
import time
import weakref

import pytest

from druta import gpu_backend, native, nvbackend
from tests.fake_driver import (
    CLOCK_GUARD_READS,
    FAR_FUTURE_S,
    NOT_PROVIDED,
    SECOND_EDGE_C,
    SECOND_POWER_MW,
    SECOND_SLOT,
    TITAN,
    XP,
    FakeDriver,
    card,
)

NATIVE_BUILT = native.available()
KINDS = ["python", pytest.param("native", marks=pytest.mark.skipif(
    not NATIVE_BUILT, reason="native core not built (native/build.sh linux)"))]
ARCH = ("nvml", "nvmlDeviceGetArchitecture")
BOOST_LOCK = ("nvapi", "BoostLock")
# One attempt of nvbackend's legacy clock-lock capture on the fake (module docstring).
K32 = [("kernel32", name) for name in ("GetCurrentThreadId", "GetCurrentProcess", "VirtualProtect")]


def attempt(call):
    try:
        return ("ok", call())
    except Exception as exc:  # noqa: BLE001 - the type and text are what is compared
        return ("raise", type(exc).__name__, str(exc))


def python_log(log):
    """(the Python log without nvbackend's legacy clock-lock capture attempts, how many there were).

    Each attempt must be exactly K32, right after a BoostLock read of the same card (module
    docstring); a kernel32 entry anywhere else fails here."""
    out, attempts, at = [], 0, 0
    while at < len(log):
        if log[at][0] != "kernel32":
            out.append(log[at])
            at += 1
            continue
        assert log[at:at + len(K32)] == K32, log[at:at + len(K32)]
        assert out and out[-1][:2] == BOOST_LOCK, out[-1:]
        attempts += 1
        at += len(K32)
    return out, attempts


def native_log(log):
    """The native log, which never carries a kernel32 entry (nothing on the core's path loads one)."""
    assert all(entry[0] != "kernel32" for entry in log), log
    return log


def kind_log(kind, log):
    return python_log(log)[0] if kind == "python" else native_log(log)


def merge_static(session):
    """Merge the native capability keys of `static` (module docstring); a no-op for Python."""
    if session.kind == "native":
        session.gpu.static.get("pl_min_mw")


def run(kind, scenario, *, prime_arch=True, **options):
    """One session of `kind` on a fresh FakeDriver(**options): scenario(session) and everything
    the fake saw while it ran."""
    with card(kind, **options) as s:
        merge_static(s)
        if prime_arch:
            s.gpu.arch()  # cached on both kinds; see test_read_ticks for the first tick without it
        s.driver.clear_part_logs()
        mark = s.driver.mark()
        result = scenario(s)
        log = s.driver.since(mark)
        return {"result": result, "log": kind_log(kind, log),
                "state": s.driver.state(), "parts": s.driver.part_logs(), "errors": list(s.driver.errors)}


def both(kind, scenario, **options):
    """run() on `kind`; under native also on Python, and the two outcomes must be equal."""
    mine = run(kind, scenario, **options)
    assert mine["errors"] == []
    if kind == "native":
        theirs = run("python", scenario, **options)
        assert theirs["errors"] == []
        for key in ("result", "log", "state", "parts", "errors"):
            assert theirs[key] == mine[key], key
    return mine


def rails(session):
    """The rail helpers each backend offers today: nvbackend.GPU's methods, druta.native_rails'
    functions over the native object (it has no such methods yet)."""
    if session.kind == "python":
        return session.gpu
    from druta import native_rails

    class Rails:
        def __getattr__(self, name):
            function = getattr(native_rails, name)
            return lambda *a, **k: function(session.gpu, *a, **k)
    return Rails()


def legacy_p0(session):
    """hold / release / owned / supported: nvbackend.GPU's methods, druta.native_legacy_p0 for native."""
    if session.kind == "python":
        return session.gpu
    from druta import native_legacy_p0

    class P0:
        def __getattr__(self, name):
            function = getattr(native_legacy_p0, name)
            return lambda *a, **k: function(session.gpu, *a, **k)
    return P0()


def identity(gpu):
    """The identity surface both kinds share (selected records cut to the native keys)."""
    out = {"available": gpu.available(), "status": gpu.status_line(), "pairing": gpu.pairing_error}
    for half in ("nvapi", "nvml"):
        api = getattr(gpu, half)
        selected = api.selected
        out[half] = (api.ok, api.err_detail, None if selected is None else {
            key: selected[key] for key in ("slot", "devid", "subsys", "handle", "nvml_index", "uuid")
            if key in selected})
    out["nvml dll"] = getattr(gpu.nvml, "dll_path", None)
    return out


def construction(kind, slot=None, **options):
    """Construction of `kind`: (identity, static after the merge, construction log, merge log,
    selected records). The fake's errors must be empty and no kernel32 entry may appear."""
    with card(kind, slot, **options) as s:
        built = s.driver.since(0)
        ident = identity(s.gpu)
        mark = s.driver.mark()
        static = dict(s.gpu.static) if kind == "python" else (merge_static(s), dict(s.gpu.static))[1]
        selected = {half: getattr(s.gpu, half).selected for half in ("nvapi", "nvml")}
        merged = s.driver.since(mark)
        assert s.driver.errors == []
        assert all(entry[0] != "kernel32" for entry in built + merged)
        return ident, static, built, merged, selected


# ---- construction ------------------------------------------------------------------------------ #

class TestConstruction:
    CASES = {
        "modern": {},
        "kepler": {"architecture": "kepler"},
        "no nvapi": {"with_nvapi": False},
        "no nvml": {"with_nvml": False},
        "transient name": {"transient": {"nvmlDeviceGetName": None}},
        "transient constraints": {"transient": {"nvmlDeviceGetPowerManagementLimitConstraints": None}},
    }

    @pytest.mark.parametrize("kind", KINDS)
    @pytest.mark.parametrize("case", list(CASES))
    def test_identity_static_and_log(self, kind, case):
        options = self.CASES[case]
        ident, static, built, merged, selected = construction(kind, **options)
        assert ident["available"] is True
        assert static["name"] == TITAN["name"] and static["slot"] == "0000:01:00.0"
        if case == "no nvapi":
            assert ident["nvapi"][:2] == (False, f"nvapi64.dll not loadable: {NOT_PROVIDED}")
            assert ident["status"].startswith("NVAPI: FAIL (")
        if case == "no nvml":
            assert ident["nvml"][0] is False and NOT_PROVIDED in ident["nvml"][1]
            assert static["uuid"] == ""
        if case == "transient constraints":
            # the first (and only) constraints read failed: unknown bounds, never invented ones
            assert static.get("pl_min_mw") is None and static.get("pl_max_mw") is None
        elif case != "no nvml":
            assert (static["pl_min_mw"], static["pl_max_mw"], static["pl_def_mw"]) == (100000, 320000, 280000)
        if kind == "native":
            p_ident, p_static, p_built, p_merged, p_selected = construction("python", **options)
            assert p_ident == ident
            assert p_static == static
            # Recorded (native.py _Static): Python reads the capability keys in __init__.
            assert p_merged == []
            assert p_built == built + merged
            # Recorded (DESIGN-FRONTEND.md (c), "Pairing"): the native selected records lack
            # enum_index and name. FINDING (unrecorded): the native NVML selected record also lacks
            # Python's raw `dev` (the NVML device handle); DESIGN-FRONTEND.md (c) does not say so.
            for half, extra in (("nvapi", {"enum_index", "name"}), ("nvml", {"dev", "name"})):
                if p_selected[half] is None:
                    assert selected[half] is None
                    continue
                assert set(p_selected[half]) - set(selected[half]) == extra
                assert set(selected[half]) <= set(p_selected[half])


# ---- telemetry --------------------------------------------------------------------------------- #

def ticks(session):
    """Three read() ticks (the dict, the raw log); the fake's power reading changes after the first."""
    out = []
    for tick in range(3):
        mark = session.driver.mark()
        out.append((session.gpu.read(), session.driver.since(mark)))
        if tick == 0:
            session.driver.answer_u32("nvmlDeviceGetPowerUsage", 123456)
    return out


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("options", [{}, {"with_nvapi": False}, {"with_nvml": False},
                                     {"architecture": "kepler"}], ids=["modern", "no-nvapi", "no-nvml", "kepler"])
def test_read_ticks(kind, options):
    with card(kind, **options) as s:
        merge_static(s)
        mine = ticks(s)
        assert s.driver.errors == []
    powers = [tick[0].get("power_w") for tick in mine]
    if options.get("with_nvml", True):
        assert powers[0] == 180.5 and powers[1:] == [123.456, 123.456]
    # The recorded kernel32 divergence (module docstring): on the Turing-class card with both
    # libraries (the fallback needs NVAPI and NVML's architecture), one capture attempt per tick,
    # as the tick's last calls; on any other card none.
    turing = options.get("architecture", "modern") == "modern" and options.get("with_nvapi", True) \
        and options.get("with_nvml", True)
    for _, raw in mine:
        if kind == "python":
            assert python_log(raw)[1] == (1 if turing else 0)
            if turing:
                assert raw[-len(K32) - 1:] == [BOOST_LOCK] + K32
        else:
            native_log(raw)
    if kind != "native":
        return
    with card("python", **options) as s:
        theirs = ticks(s)
        assert s.driver.errors == []
    for _, raw in theirs:
        assert python_log(raw)[1] == (1 if turing else 0)
    for tick, ((p_dict, p_log), (n_dict, n_log)) in enumerate(zip(theirs, mine, strict=True)):
        # FINDING (unrecorded): native read() adds the raw private-clock payload as "priv_clocks"
        # (288 words) when the private getter answered; nvbackend.GPU.read() keeps it local.
        extra = set(n_dict) - set(p_dict)
        assert extra == ({"priv_clocks"} if n_dict.get("clk_domains") is not None
                         and options.get("architecture") != "kepler" else set()), extra
        assert p_dict == {key: value for key, value in n_dict.items() if key != "priv_clocks"}
        p_log = python_log(p_log)[0]
        if tick == 0 and ARCH in n_log:
            # FINDING (unrecorded): on the first tick, when the architecture is not cached yet,
            # nvbackend reads it inside read() before the temperatures (clkdom_is_blackwell),
            # the native binding after the core's telemetry call (Gpu.read's clkdom_is_blackwell
            # at its end). Same calls, different order: the same number of reads on both kinds,
            # and moving Python's first one to the end gives the native log.
            assert p_log.count(ARCH) == n_log.count(ARCH)
            assert p_log.index(ARCH) < n_log.index(ARCH) and n_log[-1] == ARCH
            p_log = list(p_log)
            p_log.append(p_log.pop(p_log.index(ARCH)))
        assert p_log == n_log, tick


# ---- power limit ------------------------------------------------------------------------------- #

def power_limit(s):
    g = s.gpu
    out = [attempt(g.read_power_limit_mw), attempt(g.power_limit_bounds_mw),
           attempt(lambda: g.set_power_limit_mw(250000)), attempt(g.read_power_limit_mw),
           attempt(lambda: g.set_power_limit_mw(999999))]
    s.driver.fail_next("nvmlDeviceSetPowerManagementLimit")
    out += [attempt(lambda: g.set_power_limit_mw(260000)), attempt(g.read_power_limit_mw)]
    return out


@pytest.mark.parametrize("kind", KINDS)
def test_power_limit(kind):
    """Success, out of range and a failing SET, on the card that serves the client-power wire model
    (live_power): a request outside NVML's static bounds is checked against the live request's
    bounds, which needs that transport."""
    out = both(kind, power_limit, live_power=True)
    r = out["result"]
    assert r[0] == ("ok", 260000) and r[1] == ("ok", (100000, 320000))
    assert r[2] == ("ok", (True, "power limit configured to 250 W")) and r[3] == ("ok", 250000)
    # 321 W: the live request of the wire model (FakeGpu's requested=321000), which the live bounds
    # admit; 999.999 W is outside them and nothing is written.
    assert r[4] == ("ok", (False, "power request 999.999 W out of [100..321] W; extended requests require XOC"))
    assert r[5] == ("ok", (False, "power limit failed: Unknown Error")) and r[6] == ("ok", 250000)
    assert out["log"].count(("nvml", "nvmlDeviceSetPowerManagementLimit")) == 2  # 250 W and the failed 260 W
    assert [call for call in out["parts"]["base"] if call[0] == "SetPowerManagementLimit"] == [
        ("SetPowerManagementLimit", 250000)]
    assert out["parts"]["live"] and out["state"]["live request"] == 321000  # read, never written


@pytest.mark.parametrize("kind", KINDS)
def test_power_limit_outside_static_bounds_without_client_power_transport(kind):
    """Without the client-power envelope the request outside NVML's static bounds cannot be checked
    against the live request: refused before any write, on both kinds."""
    out = both(kind, lambda s: attempt(lambda: s.gpu.set_power_limit_mw(999999)))
    assert out["result"] == ("ok", (False, "live power request not available; no write issued: "
                                           "Client-power transport envelope is not understood"))
    assert ("nvml", "nvmlDeviceSetPowerManagementLimit") not in out["log"]
    assert [call for call in out["parts"]["base"] if call[0] == "SetPowerManagementLimit"] == []


# ---- current limits and policies --------------------------------------------------------------- #

def current_limits(s):
    g = s.gpu
    return [attempt(g.get_current_limits), attempt(g.read_power_policies),
            attempt(lambda: g.set_current_limit_ma(13, 200000)), attempt(g.get_current_limits)]


@pytest.mark.parametrize("kind", KINDS)
def test_current_limits_and_policies(kind):
    out = both(kind, current_limits)
    before, policies, written, after = out["result"]
    assert before[1][0]["policy"] == 13 and before[1][0]["limit_ma"] == before[1][0]["default_ma"] == 350780
    assert policies[0] == "ok" and len(policies[1][0]) > 1
    assert written == ("ok", (True, "Core current set to 200 A; effective limit verified"))
    assert after[1][0]["limit_ma"] == after[1][0]["requested_ma"] == 200000
    assert out["parts"]["escapes"]  # the write went through the fake gdi32 escape


# ---- clock offsets and the clock-domain rows --------------------------------------------------- #

def clocks(s):
    g = s.gpu

    def offsets():
        d = g.read()
        return {key: d.get(key) for key in ("core_off", "mem_off", "clk_domains", "clk_domains_err")}
    return [attempt(offsets), attempt(lambda: g.set_clock_offset(0, 100)), attempt(lambda: g.set_clock_offset(2, 500)),
            attempt(offsets), attempt(g.read_clk_domain_offsets), attempt(lambda: g.set_clk_domain_offset(0, 50)),
            attempt(g.read_clk_domain_offsets)]


@pytest.mark.parametrize("kind", KINDS)
def test_clock_offsets_and_domains(kind):
    out = both(kind, clocks)
    first, core, mem, second, rows, gpc, rows_after = out["result"]
    assert (first[1]["core_off"], first[1]["mem_off"]) == (0, 0)
    assert first[1]["clk_domains"] and first[1]["clk_domains"][0]["name"] == "GPC"
    assert core[1][0] is True and mem[1][0] is True
    # The fake's inputs: core requests land on its 15 MHz-bin V/F rows (+105 written and read back,
    # "V/F shape kept"); the memory request is written in the units nvbackend converts it to (4000)
    # and read() reports NVML's stored value. The same on both kinds.
    assert core[1] == (True, "core offset set to +105 MHz; V/F shape kept")
    assert (second[1]["core_off"], second[1]["mem_off"]) == (105, 4000)
    assert {t: o["off"] for t, o in out["state"]["clock"][2].items()} == {0: 105, 2: 4000}
    assert rows[1][0][0]["freq_khz"] == 0
    assert gpc == ("ok", (True, "GPC offset +0 -> +50 MHz (requested; the driver floors to whole clock bins)"))
    assert rows_after[1][0][0]["freq_khz"] == 50000


# ---- fan --------------------------------------------------------------------------------------- #

def fan(s):
    g = s.gpu
    state = g.read_fan_control_state()
    return [state, attempt(lambda: g.set_fan(60)), attempt(g.read_fan_control_state),
            attempt(lambda: g.restore_fan_control_state(state)), attempt(g.read_fan_control_state)]


@pytest.mark.parametrize("kind", KINDS)
def test_fan(kind):
    out = both(kind, fan)
    state, manual, during, restored, after = out["result"]
    assert manual == ("ok", (True, "fans set to manual 60%"))
    assert all(f["manual"] and f["level"] == 60 for f in during[1]["fans"])
    assert restored[1][0] is True
    assert [(f["manual"], f["policy"]) for f in after[1]["fans"]] == [(f["manual"], f["policy"]) for f in state["fans"]]


# ---- V/F curve --------------------------------------------------------------------------------- #

def vf_curve(s):
    g = s.gpu
    return [attempt(g.read_vf_curve), attempt(lambda: g.apply_vf_deltas({7: 15000, 8: 30000})),
            attempt(g.read_vf_curve), attempt(g.reset_vf_curve), attempt(g.read_vf_curve)]


@pytest.mark.parametrize("kind", KINDS)
def test_vf_curve(kind):
    out = both(kind, vf_curve)
    before, written, after, reset, last = out["result"]
    assert before[0] == "ok" and all(p["delta_khz"] == 0 for p in before[1][0])
    assert written == ("ok", (True, "VF delta table written (2 points changed)"))
    assert [p["delta_khz"] for p in after[1][0][7:9]] == [15000, 30000]
    assert reset[1][0] is True and all(p["delta_khz"] == 0 for p in last[1][0])


# ---- rails ------------------------------------------------------------------------------------- #

def rail_reads(s):
    r = rails(s)
    return [attempt(r.read_volt_rail_limits), attempt(r.read_volt_rail_state),
            attempt(r.volt_rail_limits_supported)]


@pytest.mark.parametrize("kind", KINDS)
def test_rail_limits_and_state(kind):
    out = both(kind, rail_reads)
    limits, state, supported = out["result"]
    assert limits[1][0]["_base_mv"] == {"reliability": 1068.75, "alt_reliability": 1093.75,
                                        "overvoltage": 1125.0, "vmin": 650.0}
    assert state[1][0]["live"] == 800.0 and supported == ("ok", True)


# ---- reset all --------------------------------------------------------------------------------- #

def moved_then_reset(s):
    g = s.gpu
    moved = [g.set_power_limit_mw(250000), g.set_clock_offset(0, 100), g.set_clock_offset(2, 500),
             g.set_current_limit_ma(13, 200000), g.set_fan(60), g.set_voltage_boost(50),
             g.set_clk_domain_offset(0, 50), g.apply_vf_deltas({7: 15000})]
    steps = attempt(lambda: [(step.name,) + tuple(step) for step in g.reset_all()])
    return [moved, steps, g.read_power_limit_mw(), g.read_voltage_boost()]


@pytest.mark.parametrize("kind", KINDS)
def test_reset_all_after_a_session(kind):
    out = both(kind, moved_then_reset)
    moved, steps, power, boost = out["result"]
    assert all(ok for ok, _ in moved), moved
    assert steps[0] == "ok"
    names = [step[0] for step in steps[1]]
    assert names[:4] == ["core offset", "mem offset", "GPC offset", "core current"]
    assert {"power limit", "clock lock", "v/f point lock", "fan", "voltage boost", "vf curve"} <= set(names)
    assert all(step[1] for step in steps[1]), steps
    assert power == 280000 and boost == 0
    assert out["state"]["policies"][2] == 280000


# ---- legacy P0 (Kepler-class card) ------------------------------------------------------------- #

def p0_session(s):
    p0 = legacy_p0(s)
    return [p0.legacy_p0_supported(), attempt(p0.hold_legacy_p0), p0.legacy_p0_owned(),
            attempt(p0.release_legacy_p0), p0.legacy_p0_owned()]


@pytest.mark.parametrize("kind", KINDS)
def test_kepler_hold_and_release_legacy_p0(kind):
    out = both(kind, p0_session, architecture="kepler")
    assert out["result"] == [
        True, ("ok", (True, "P0 held; core 705 MHz, memory 3004 MHz (not a maximum-core lock)")), True,
        ("ok", (True, "P0 request released; automatic performance states restored")), False]
    forces = [event for event in out["parts"]["p0"] if event[0] == "force"]
    assert [event[2:] for event in forces] == [(0, 2), (16, 2)]
    # The waits on the card's clock (nvbackend's own `time` name on Python, the sleep hook native):
    # the same entries as when the Python kind patched the time module itself.
    assert [entry for entry in out["log"] if entry[0] == "sleep"] == [("sleep", 100), ("sleep", 100)]


@pytest.mark.parametrize("kind", KINDS)
def test_modern_card_has_no_legacy_p0(kind):
    out = both(kind, p0_session)
    assert out["result"][:3] == [False, ("ok", (False, "legacy P0 hold is unavailable on this GPU/driver")), False]


# ---- partial and transient --------------------------------------------------------------------- #

def partial(s):
    g = s.gpu
    d = g.read()
    return [{key: d.get(key) for key in ("core", "power_w", "pl_requested_mw", "fans")},
            attempt(g.read_power_limit_mw), attempt(g.get_current_limits), attempt(lambda: g.set_fan(50))]


@pytest.mark.parametrize("kind", KINDS)
def test_nvapi_absent(kind):
    out = both(kind, partial, with_nvapi=False)
    tick, power, currents, fans = out["result"]
    assert tick["power_w"] == 180.5 and power == ("ok", 260000)
    assert all(entry[0] != "nvapi" for entry in out["log"])
    assert currents == ("ok", [])


@pytest.mark.parametrize("kind", KINDS)
def test_nvml_absent(kind):
    out = both(kind, partial, with_nvml=False)
    tick, power, currents, fans = out["result"]
    assert tick["core"] == 1935 and tick["power_w"] is None
    assert all(entry[0] != "nvml" for entry in out["log"])


def transient_power(s):
    g = s.gpu
    return [attempt(g.read_power_limit_mw), attempt(g.read_power_limit_mw), attempt(g.power_limit_bounds_mw),
            attempt(lambda: g.set_power_limit_mw(250000)), attempt(g.read_power_limit_mw)]


@pytest.mark.parametrize("kind", KINDS)
def test_transient_power_limit_reader(kind):
    out = both(kind, transient_power, transient={"nvmlDeviceGetPowerManagementLimit": None})
    first, second, bounds, written, back = out["result"]
    assert first == ("ok", None)  # the failed read is unknown, not a value
    assert second == ("ok", 260000) and bounds == ("ok", (100000, 320000))
    assert written == ("ok", (True, "power limit configured to 250 W")) and back == ("ok", 250000)


@pytest.mark.parametrize("kind", KINDS)
def test_transient_rail_getter(kind):
    """The control getter fails once: the bounded read-only retry inside the reader absorbs it and
    the rail control stays supported."""
    out = both(kind, rail_reads, transient={"VoltRailsCtlGet": None})
    limits, state, supported = out["result"]
    assert limits[0] == "ok" and 0 in limits[1] and supported == ("ok", True)
    assert out["log"].count(("nvapi", "VoltRailsCtlGet")) > 2


@pytest.mark.parametrize("kind", KINDS)
def test_transient_capability_read(kind):
    """The constraints read fails on its only call: the bounds stay unknown in `static`, the power
    limit is still readable and writable."""
    out = both(kind, transient_power, transient={"nvmlDeviceGetPowerManagementLimitConstraints": None})
    first, second, bounds, written, back = out["result"]
    assert first == second == ("ok", 260000)
    assert written[1][0] is True and back == ("ok", 250000)


TWO_CARD_KEYS = ("power_w", "temp_edge", "core")


def second_card(s):
    """Open the second card next to the first and read BOTH: per card, the values and the log of
    its tick (the Python legacy clock-lock attempts removed, module docstring)."""
    other = s.open(SECOND_SLOT)
    other.static.get("pl_min_mw")  # the native capability merge (module docstring); read on Python
    other.arch()  # as run() primes the first card (the first-tick order: test_read_ticks)
    reads = []
    for gpu in (s.gpu, other):
        mark = s.driver.mark()
        d = gpu.read()
        reads.append(({key: d.get(key) for key in TWO_CARD_KEYS}, kind_log(s.kind, s.driver.since(mark))))
    return [identity(s.gpu), identity(other), dict(other.static), reads]


@pytest.mark.parametrize("kind", KINDS)
def test_two_cards(kind):
    """Each card answers with its own values, and every call of the second card's tick that takes a
    device / GPU handle carries the second card's index in the log (handle pairing)."""
    out = both(kind, second_card, second_card=True)
    first, second, _, ((first_values, first_log), (second_values, second_log)) = out["result"]
    assert first["status"].endswith("slot 0000:01:00.0") and second["status"].endswith(f"slot {SECOND_SLOT}")
    assert second["nvml"][2]["uuid"] == XP["uuid"] and first["nvml"][2]["uuid"] == TITAN["uuid"]
    assert first_values == {"power_w": 180.5, "temp_edge": 61, "core": first_values["core"]}
    assert second_values == {"power_w": SECOND_POWER_MW / 1000, "temp_edge": SECOND_EDGE_C,
                             "core": first_values["core"]}
    assert first_log and all(len(entry) == 2 for entry in first_log)  # card 0: no tag (single-card format)
    tagged = [entry for entry in second_log if entry[0] in ("nvml", "nvapi")]
    assert tagged == [entry + (1,) for entry in first_log if entry[0] in ("nvml", "nvapi")]
    assert ("nvml", "nvmlDeviceGetPowerUsage", 1) in tagged and ("nvapi", "ThermalSettings", 1) in tagged
    # Opening the second card addresses it too; nothing anywhere addresses a handle the fake never
    # issued ("handle 0x..." in the log).
    assert {entry[2] for entry in out["log"] if len(entry) == 3 and entry[0] != "sleep"} == {1}


@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
@pytest.mark.parametrize("kind", KINDS)
def test_a_handle_the_fake_never_issued_is_visible(kind):
    """A device / GPU handle no card owns is logged as its own text (the gates are shared by both
    kinds, so they are called directly here: no backend passes such a handle on purpose). The fake
    behind ThermalSettings, handed no structure here, raises: that lands in driver.errors."""
    with card(kind) as s:
        d = s.driver
        power = d._gate("nvml", "nvmlDeviceGetPowerUsage", d.nvml_table)
        thermal = d._gate("nvapi", 0xE3640A56, d.nvapi_table)
        value = ctypes.c_uint32()
        mark = d.mark()
        power(0x1001, ctypes.byref(value))  # card 1's handle on a one-card fake
        power(None, ctypes.byref(value))
        thermal(0x2005, 15, None)
        assert d.since(mark) == [("nvml", "nvmlDeviceGetPowerUsage", "handle 0x1001"),
                                 ("nvml", "nvmlDeviceGetPowerUsage", "handle None"),
                                 ("nvapi", "ThermalSettings", "handle 0x2005")]
        assert [error.split(" (in ")[0] for error in d.errors] == ["TypeError: integer expected"]
        d.errors.clear()


@pytest.mark.parametrize("kind", KINDS)
def test_second_slot_opened_first(kind):
    ident, static, *_ = construction(kind, SECOND_SLOT, second_card=True)
    assert static["name"] == XP["name"] and static["slot"] == SECOND_SLOT
    if kind == "native":
        assert construction("python", SECOND_SLOT, second_card=True)[:2] == (ident, static)


# ---- the card restores everything -------------------------------------------------------------- #

def environment():
    out = {"pin": gpu_backend.choice(), "gate": gpu_backend.surface_gate,
           "windll": getattr(ctypes, "WinDLL", None), "winfunctype": getattr(ctypes, "WINFUNCTYPE", None),
           "nvml loader": nvbackend._load_nvml_library, "sleep": time.sleep, "monotonic": time.monotonic,
           "nvbackend time": nvbackend.time, "unraisable": sys.unraisablehook, "gpu init": native.Gpu.__init__,
           "env": os.environ.get("DRUTA_BACKEND"), "describe": gpu_backend.describe()}
    if NATIVE_BUILT:
        out.update(resolver=native._resolver_keepalive, sleep_hook=native._sleep_keepalive,
                   clock_hook=native._clock_keepalive)
    return out


@pytest.mark.parametrize("kind", KINDS)
def test_close_restores_everything(kind):
    before = environment()
    assert before["pin"] is None  # nothing earlier (another card included) left a kind pinned
    with card(kind) as s:
        assert gpu_backend.choice().kind == kind
        other = "python" if kind == "native" else "native"
        with pytest.raises(gpu_backend.BackendMismatch):
            gpu_backend.open_gpu(kind=other)
        if kind == "native":
            assert native._resolver_keepalive is not None
        s.gpu.read()
        assert s.driver.errors == []
    assert environment() == before
    with pytest.raises(RuntimeError), card(kind):
        raise RuntimeError("a failing test body")
    assert environment() == before


@pytest.mark.skipif(not NATIVE_BUILT, reason="native core not built (native/build.sh linux)")
def test_cards_of_both_kinds_in_turn():
    for kind in ("native", "python", "native"):
        with card(kind) as s:
            assert s.gpu.available() and s.driver.errors == []
    assert gpu_backend.choice() is None


def test_unknown_options_are_refused():
    with pytest.raises(ValueError):
        FakeDriver(architecture="pascal")
    with pytest.raises(KeyError):
        FakeDriver().fail_next("nvmlNoSuchExport")


# ---- errors inside the fake reach the test ----------------------------------------------------- #

def raising_power_reader(s):
    """Serve nvmlDeviceGetPowerUsage from a callback that raises, then read."""
    @ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
    def broken(device, pointer):
        raise RuntimeError("broken fake reader")
    s.driver.replace("nvmlDeviceGetPowerUsage", broken)
    s.gpu.read()


@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
@pytest.mark.parametrize("kind", KINDS)
def test_exception_in_a_fake_callback_fails_the_case(kind):
    """ctypes swallows an exception raised in a callback and answers 0 (success to the backend);
    the card's unraisable hook records it, so the case fails through driver.errors."""
    out = run(kind, raising_power_reader)
    assert len(out["errors"]) == 1 and out["errors"][0].startswith("RuntimeError: broken fake reader")
    with pytest.raises(AssertionError):
        both(kind, raising_power_reader)


@pytest.mark.parametrize("kind", KINDS)
def test_clock_guard_is_recorded_and_ends_the_wait(kind):
    """A backend polling the card's clock CLOCK_GUARD_READS times without a wait trips the guard:
    recorded once in driver.errors and answered with a far-future time (reset_all's waits read the
    clock on both kinds); a wait resets the count."""
    with card(kind) as s:
        s.driver.clock_reads = CLOCK_GUARD_READS  # one read short of tripping: a wait starts over
        s.driver.wait_ms(1)
        assert s.driver.now() == s.driver.clock and s.driver.errors == []
        s.driver.clock_reads = CLOCK_GUARD_READS - 1
        start = s.driver.clock
        list(s.gpu.reset_all())
        assert s.driver.errors == [f"fake driver clock: read {CLOCK_GUARD_READS} times without a wait"]
        assert s.driver.last_now == start + FAR_FUTURE_S


@pytest.mark.parametrize("kind", KINDS)
def test_unraisable_hook_is_restored(kind):
    before = sys.unraisablehook
    with card(kind):
        assert sys.unraisablehook is not before
    assert sys.unraisablehook is before
    with pytest.raises(RuntimeError, match="body"), card(kind):
        raise RuntimeError("body")
    assert sys.unraisablehook is before


# ---- cards do not nest ------------------------------------------------------------------------- #

def read_once(s):
    return s.gpu.read()


@pytest.mark.skipif(not NATIVE_BUILT, reason="native core not built (native/build.sh linux)")
@pytest.mark.parametrize(("outer", "inner"), [("native", "native"), ("native", "python"), ("python", "native")])
def test_nested_card_is_refused(outer, inner):
    def nested(s):
        before = environment()
        with pytest.raises(RuntimeError, match="cards do not nest"), card(inner):
            pass
        assert environment() == before  # refused before anything was touched
        return s.gpu.read()
    assert run(outer, nested) == run(outer, read_once)


# ---- native objects never outlive their card ----------------------------------------------------- #

NATIVE_ONLY = pytest.mark.skipif(not NATIVE_BUILT, reason="native core not built (native/build.sh linux)")


def _closed(gpu):
    """Whether native.Gpu.close() has run on `gpu`: its handle box is marked closed (every entry
    then raises native.NativeClosed; the box frees the handle at its last reference)."""
    return gpu._box.closed


@NATIVE_ONLY
def test_native_object_opened_inside_is_closed_on_exit():
    """The code under test opens its own native object (as every front-end flow does): the card
    tracks it (native.Gpu.__init__ wrapped for the card's lifetime, weak references) and closes it
    on exit, so a later use raises native.NativeClosed (a ValueError) instead of calling through
    the card that is gone (the review's probe g4h1_rv7_probe4)."""
    with card("native") as s:
        other = gpu_backend.open_gpu(kind="native")
        assert other.read()["power_w"] == 180.5
        assert [ref() for ref in s.natives] == [s.gpu, other]
        assert not _closed(other)
    assert _closed(other)
    with pytest.raises(native.NativeClosed) as caught:
        other.read()
    assert isinstance(caught.value, ValueError) and str(caught.value) == "invalid argument"
    with card("native") as t:  # inside the next card it reaches nothing either
        mark = t.driver.mark()
        with pytest.raises(ValueError, match="invalid argument"):
            other.read()
        assert t.driver.since(mark) == []


@NATIVE_ONLY
def test_native_objects_left_open_are_closed_and_dropped_ones_skipped():
    holder = []
    with card("native") as s:
        holder.append(gpu_backend.open_gpu(kind="native"))  # kept by "the code under test", never closed
        dropped = gpu_backend.open_gpu(kind="native")
        dropped_ref = weakref.ref(dropped)
        del dropped
        gc.collect()  # native.Gpu and its _Static refer to each other
        assert dropped_ref() is None and len(s.natives) == 3
    assert _closed(holder[0]) and _closed(s.gpu)


@NATIVE_ONLY
def test_a_failing_close_does_not_stop_the_others():
    before = environment()
    with pytest.raises(RuntimeError, match="injected close failure"), card("native") as s:
        failing, last = gpu_backend.open_gpu(kind="native"), gpu_backend.open_gpu(kind="native")

        def close():
            native.Gpu.close(failing)
            raise RuntimeError("injected close failure")
        failing.close = close
    assert _closed(s.gpu) and _closed(last) and _closed(failing)
    assert gpu_backend.choice() is None
    assert environment() == before
    del failing.close


# ---- changes the two kinds would not see alike are refused ------------------------------------- #

@pytest.mark.parametrize("kind", KINDS)
def test_gdi32_export_removal(kind):
    """On an open card a gdi32 export cannot be removed (the core calls a gdi32 export it resolved
    once without a NULL check); removed before the card opens (`absent`) it is absent on both kinds:
    the current-limit write has no escape to go through."""
    with card(kind) as s:
        with pytest.raises(ValueError, match="cannot be removed from an open card"):
            s.driver.remove("D3DKMTEscape")
        assert s.driver.errors == []
    out = both(kind, current_limits, absent=("D3DKMTEscape",))
    assert out["parts"]["escapes"] == []
    assert all(entry != ("gdi32", "D3DKMTEscape") for entry in out["log"])
    assert out["result"] != run(kind, current_limits)["result"]


@pytest.mark.parametrize("kind", KINDS)
def test_nvapi_interface_absent_at_open_cannot_be_added(kind):
    """nvbackend binds NVAPI interfaces at construction, the core per call: an interface absent at
    open would reach the native kind alone. Replacing one that was present stays allowed."""
    with card(kind, rails=False) as s:
        callback = s.driver.nvapi_table.get(0xE3640A56)
        with pytest.raises(ValueError, match="absent when the card opened"):
            s.driver.replace("VoltRailsCtlGet", callback)
        with pytest.raises(ValueError, match="absent when the card opened"):
            s.driver.answer_u32("VoltRailsCtlGet", 1)
        s.driver.replace("ThermalSettings", callback)
        assert s.gpu.read()["temp_edge"] == 61
        assert s.driver.errors == []


# ---- the Python kind's clock is nvbackend's alone ----------------------------------------------- #

def test_python_card_leaves_the_time_module_real():
    real_sleep, real_monotonic = time.sleep, time.monotonic
    slept = {}

    def worker():
        start = real_monotonic()
        time.sleep(0.05)
        slept["s"] = real_monotonic() - start

    with card("python") as s:
        assert time.sleep is real_sleep and time.monotonic is real_monotonic
        assert nvbackend.time is not time and nvbackend.time.time is time.time  # the proxy
        mark = s.driver.mark()
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        assert slept["s"] >= 0.045
        assert s.driver.since(mark) == []
        nvbackend.time.sleep(0.25)  # the card's clock: logged, not slept
        assert s.driver.since(mark) == [("sleep", 250)]
    assert nvbackend.time is time
