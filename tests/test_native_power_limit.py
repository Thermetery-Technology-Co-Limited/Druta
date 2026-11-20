# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native board power limit, increment A8: nvbackend.GPU.POWER_LIMIT_XOC_MAX_MW,
_normal_power_bounds_mw, _power_request_bounds_mw, power_limit_bounds_mw, _read_power_limit_nvml,
read_power_limit_mw, set_power_limit_mw and _set_power_limit_nvml against the real Python methods.

native.Gpu's methods run dc_pl_*; the Python originals are the real nvbackend.GPU methods on the
nvbackend.GPU twin of the same fake driver. Two rigs:

1. NVML only (PowerLimitReadbackTests of tests/test_power_limit_readback.py): the PowerDriver model
   (a configured request distinct from a lagging enforced limit, a setter that may fail or not
   echo) served as NVML exports through dc_set_export_resolver; the twin is the identity rig's
   nvbackend.GPU over the same exports (its nvml.dll is the export table), NVAPI down on both
   sides, statics {"pl_min_mw": 100000, "pl_max_mw": 400000} on both.
2. The live rig (tests/test_live_power_backend.py's BackendGpu): the metadata rig of
   tests/test_native_client_power_metadata.py (MetaRig: one of the Python tests' own wire models as
   the responder of the fake gdi32 escape, each side with a fresh fake) plus NVML's configured-limit
   getter and setter over the same fake, as BackendGpu's nvml_get / nvml_set. BackendGpu replaces
   _reapply_power_policy_pins with a Mock; here the REAL pin re-application runs on BOTH sides (the
   B3 original on the twin, the B3 port in the core). With no pins (the default) it returns before
   any read, as the Mock's (True, ""); cases that pin a policy read the policy table through the
   current-limit transport the fake serves (no layout choice is seeded on either side, so both
   probe it) and, with PeerCoupledGpu, the writer's SET.

Every case compares on both sides: the outcome (value, (ok, message) with its full text, or the
exception), the NVML log (each getter read and each setter write with its value), the gdi escape
log, the fake's own command log and live request, the live bookkeeping (_live_power_last,
_live_power_error, _live_power_error_kind, _board_policy), the static pl_* keys and the pins.

Recorded divergences (asserted below as the binding's behaviour where reachable):
- A static pl_* value that is not equal to an int64 is None to the core (B2's _cl_statics_fill);
  Python compares or returns it as given (AbiTests.test_a_non_integer_static_is_none).
- The live back-fill comes from the core's capability store (A6's contract): where that store
  already held an NVML value and the dict held None, the store's value is used, Python takes the
  live binding's. These rigs export no NVML constraints, so both are the binding's.
- _set_power_limit_nvml takes only an int within int64 (TypeError / ValueError), and a dead NVML or
  a missing setter export is AttributeError before any call.
- The stock_* cases of tests/test_live_power_backend.py belong to increment B4 and are not here.

Uncovered paths (no rig reaches them on both sides):
- The IndexError escape of the pin re-application through set_power_limit_mw (dc_pl_set's
  DC_E_RANGE, the binding's IndexError), on the NVML and the live route: it needs a row walk outside
  a policy block, which no table the core accepts produces (B3's measured layouts), so it is
  defensive code. The mutation that survives: dropping `if (!pins.in_bounds)` (both routes) in
  PowerLimit::set.
"""
import ctypes
import math
import struct
import unittest

from druta import client_power_runtime as cp
from druta import native
from druta import nvbackend as NV
from tests.test_client_power_runtime import FakeGpu, put
from tests.test_native_client_power_metadata import MODERN, MetadataCase, MetaNative, MetaRig, commands, store
from tests.test_native_client_power_set import COUPLED_ACCEPTED, PeerCoupledGpu, SetRig
from tests.test_native_identity import CDECL, write_u32
from tests.test_native_live_power import LivePython
from tests.test_native_target_binding import Card
from tests.test_native_transport import install

if not native.available():
    raise unittest.SkipTest("native core not built (native/build.sh linux)")

PL = ("pl_min_mw", "pl_def_mw", "pl_max_mw")
GET, SET_LIMIT, ENFORCED = ("nvmlDeviceGetPowerManagementLimit", "nvmlDeviceSetPowerManagementLimit",
                            "nvmlDeviceGetEnforcedPowerLimit")
INVALID = "power limit must be a finite number in whole milliwatts"
BACKEND_STATIC = {"pl_min_mw": 200000, "pl_def_mw": 321000, "pl_max_mw": 550000}


def outcome(call):
    try:
        return ("ok", call())
    except Exception as exc:  # noqa: BLE001 - compared on both sides
        return (type(exc).__name__, str(exc))


def py_g(value):
    return f"{value:g}"


# ---- NVML's configured limit over a driver model ----------------------------------------------------

class NvmlPower:
    """NVML's configured-limit getter and setter (and the enforced getter) as exports. `model()` is the
    current driver state: an object with .requested (and _update(value) for the live fakes). The log
    holds ("get", status) for each getter read and ("set", value) for each write."""

    def __init__(self, model, *, enforced=None):
        self.model = model
        self.log = []
        self.read_status = self.after_status = self.write_status = 0
        self.echo = True
        self.writes = 0

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def get(_device, pointer):
            status, value = self.read()
            if not status:
                write_u32(pointer, value)
            return status

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
        def put_limit(_device, value):
            return self.write(int(getattr(value, "value", value)))

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def get_enforced(_device, pointer):
            self.log.append(("enforced",))
            write_u32(pointer, enforced)
            return 0

        self.exports = {GET: get, SET_LIMIT: put_limit}
        if enforced is not None:
            self.exports[ENFORCED] = get_enforced

    def read(self):
        """(status, value) of one getter read, logged."""
        status = self.after_status if self.writes else self.read_status
        self.log.append(("get", status))
        return status, (0 if status else self.model().requested & 0xFFFFFFFF)

    def write(self, value):
        """The setter's status for one write of value, logged."""
        self.log.append(("set", value))
        self.writes += 1
        if not self.write_status and self.echo:
            fake = self.model()
            if hasattr(fake, "_update"):
                fake._update(value)
            else:
                fake.requested = value
        return self.write_status

    def python_dll(self):
        """The same getter and setter as nvbackend's ctypes calls them (byref(c_uint), c_uint)."""
        def get(_device, pointer):
            status, value = self.read()
            if not status:
                pointer._obj.value = value
            return status
        return {GET: get, SET_LIMIT: lambda _device, value: self.write(int(value.value))}

    def reset(self):
        self.log, self.writes = [], 0


# ---- rig 1: NVML only (tests/test_power_limit_readback.py) -----------------------------------------

class PowerDriver:
    """tests/test_power_limit_readback.PowerDriver's state."""

    def __init__(self):
        self.requested = 360000


READBACK_STATIC = {"pl_min_mw": 100000, "pl_max_mw": 400000}


def set_static(gpu, static):
    """The pl_* keys exactly as `static` has them (absent keys removed)."""
    gpu.static.get("pl_min_mw")  # the binding merges its capability keys on first use
    for key in PL:
        gpu.static.pop(key, None)
    gpu.static.update(static)


def static_of(gpu):
    return {key: gpu.static[key] for key in PL if key in gpu.static}


class NvmlOnlyCase(unittest.TestCase):
    def tearDown(self):
        native.set_export_resolver(None)

    def both(self, action, *, setup=None, remove=()):
        """action(gpu) on the twin, then on the core, each over a fresh PowerDriver and NVML table."""
        runs = []
        for kind in ("python", "native"):
            driver = PowerDriver()
            power = NvmlPower(lambda driver=driver: driver, enforced=349200)
            card = Card(with_nvapi=False)
            card.nvml.exports.update(power.exports)
            for name in remove:
                card.nvml.exports.pop(name, None)
            if setup:
                setup(power, card)
            install(card.nvml, None, None)
            opened = native.Gpu()
            try:
                if kind == "python":
                    gpu = card.python_twin(opened)
                    gpu.static = dict(READBACK_STATIC)
                    for name in ("PowerTopo", "PowerPolStatus", "VoltRailsStatus"):
                        setattr(gpu.nvapi, name, None)
                else:
                    gpu = opened
                    set_static(gpu, READBACK_STATIC)
                power.reset()
                result = action(gpu)
                runs.append((result, list(power.log), driver.requested, static_of(gpu), bookkeeping(gpu)))
            finally:
                opened.close()
        self.assertEqual(runs[0], runs[1])
        return runs[0]


def bookkeeping(gpu):
    """The live bookkeeping (_live_power_last, _live_power_error, _live_power_error_kind, _board_policy)."""
    return tuple(getattr(gpu, name, None) for name in ("_live_power_last", "_live_power_error",
                                                       "_live_power_error_kind", "_board_policy"))


def sweep_power(gpu):
    """pl_requested_mw / pl_now_mw of one telemetry tick."""
    if isinstance(gpu, native.Gpu):
        d = gpu.read()
    else:
        d = {}
        gpu._read_power(d)
    return {key: d[key] for key in ("pl_requested_mw", "pl_now_mw") if key in d}


class PowerLimitReadbackTests(NvmlOnlyCase):
    def test_telemetry_preserves_requested_and_enforced_as_distinct_values(self):
        result, log, *_ = self.both(sweep_power)
        self.assertEqual(result, {"pl_requested_mw": 360000, "pl_now_mw": 349200})

    def test_configured_getter_never_falls_back_to_enforced_on_error(self):
        def status(power, _card):
            power.read_status = 4

        def zero(power, _card):
            power.model = lambda: type("Zero", (), {"requested": 0})()

        def action(gpu):
            return (gpu.read_power_limit_mw(), gpu._read_power_limit_nvml(), sweep_power(gpu),
                    outcome(lambda: gpu.set_power_limit_mw(350000)))
        for name, setup, remove in (("status", status, ()), ("missing", None, (GET,)), ("zero", zero, ())):
            with self.subTest(failure=name):
                (value, nvml, swept, (_, (ok, message))), log, requested, *_ = self.both(action, setup=setup,
                                                                                    remove=remove)
                self.assertIsNone(value)
                self.assertIsNone(nvml)
                self.assertEqual(swept, {"pl_now_mw": 349200})
                self.assertFalse(ok)
                self.assertEqual(message, "configured power-limit readback is unavailable; no write issued")
                self.assertNotIn("set", [entry[0] for entry in log])

    def test_exact_configured_request_verifies_despite_lagging_enforced_limit(self):
        for request in (350000, 349200, 349000, 355000, 350001, 100000, 400000):
            with self.subTest(request=request):
                (ok, message), log, requested, *_ = self.both(lambda gpu, r=request: gpu.set_power_limit_mw(r))
                self.assertTrue(ok, message)
                self.assertEqual(requested, request)
                self.assertEqual(message, f"power limit configured to {request/1000:g} W")
                self.assertEqual([e for e in log if e[0] == "set"], [("set", request)])

    def test_failed_or_unverified_write_never_reports_requested_value_as_applied(self):
        for setting, value, expected in (
                ("write_status", 4, "power limit failed: 4"),
                ("after_status", 4, "configured power-limit readback is unavailable; no write issued"),
                ("echo", False, "power limit requested 350 W, but the driver reports a configured limit of 360 W")):
            with self.subTest(setting=setting):
                def setup(power, _card, setting=setting, value=value):
                    setattr(power, setting, value)
                (ok, message), log, *_ = self.both(lambda gpu: gpu.set_power_limit_mw(350000), setup=setup)
                self.assertFalse(ok)
                if setting == "after_status":  # the readback failed: NVML-only, no live fallback
                    expected = "power limit was sent, but configured-limit readback failed"
                self.assertEqual(message, expected)
                self.assertEqual([e for e in log if e[0] == "set"], [("set", 350000)])

    def test_invalid_or_out_of_range_inputs_do_not_write(self):
        values = (None, True, False, "350000", float("nan"), float("inf"), -float("inf"), 10**1000, 350000.5,
                  99999, 400001, 0, -1, 1 << 32, (1 << 32) - 1, 2.0**32, 1e300, 10**30)
        for value in values:
            with self.subTest(value=repr(value)[:40]):
                (ok, message), log, requested, *_ = self.both(lambda gpu, v=value: gpu.set_power_limit_mw(v))
                self.assertFalse(ok)
                self.assertNotIn("set", [entry[0] for entry in log])
                self.assertEqual(requested, 360000)

    def test_a_whole_float_is_written_as_its_int(self):
        (ok, message), log, requested, *_ = self.both(lambda gpu: gpu.set_power_limit_mw(350000.0))
        self.assertTrue(ok, message)
        self.assertEqual(requested, 350000)

    def test_set_power_limit_nvml_direct(self):
        def action(gpu):
            return [outcome(lambda v=v: gpu._set_power_limit_nvml(v)) for v in (350000, 549999, 1999000)]
        result, log, *_ = self.both(action)
        self.assertEqual(result[0], ("ok", (True, "power limit configured to 350 W", 350000)))
        self.assertEqual(result[1], ("ok", (True, "power limit configured to 549.999 W", 549999)))

    def test_unreadable_getter_with_nvapi_down_leaves_the_live_state_untouched(self):
        def status(power, _card):
            power.read_status = 4
        result, log, requested, static, kept = self.both(
            lambda gpu: (gpu.read_power_limit_mw(), sweep_power(gpu)), setup=status)
        self.assertEqual(result, (None, {"pl_now_mw": 349200}))
        self.assertEqual(kept, (None, None, None, None))  # NVAPI is down: no live read was tried
        self.assertEqual(static, READBACK_STATIC)

    def test_set_power_limit_nvml_at_and_above_2_31(self):
        # nvmlDeviceSetPowerManagementLimit takes a u32: ctypes packs mw modulo 2**32, and the
        # configured read-back is compared with the caller's mw (an int64 here, Python's int there)
        values = (1 << 31, (1 << 31) + 5, (1 << 32) - 1, (1 << 32) + 350000, (1 << 33) + (1 << 31))

        def action(gpu):
            return [outcome(lambda v=v: gpu._set_power_limit_nvml(v)) for v in values]
        result, log, requested, *_ = self.both(action)
        self.assertEqual(result[0], ("ok", (True, "power limit configured to 2.14748e+06 W", 1 << 31)))
        self.assertEqual(result[1], ("ok", (True, "power limit configured to 2.14748e+06 W", (1 << 31) + 5)))
        self.assertEqual(result[2], ("ok", (True, "power limit configured to 4.29497e+06 W", (1 << 32) - 1)))
        self.assertEqual(result[3], ("ok", (False, "power limit requested 4.29532e+06 W, but the driver reports a "
                                                   "configured limit of 350 W", 350000)))
        self.assertEqual(result[4][1][2], 1 << 31)
        self.assertFalse(result[4][1][0])
        self.assertEqual([v for kind, v in log if kind == "set"], [v & 0xFFFFFFFF for v in values])

    def test_bounds_without_nvapi(self):
        def action(gpu):
            return (gpu._normal_power_bounds_mw(), gpu.power_limit_bounds_mw(), gpu.power_limit_bounds_mw(xoc=True),
                    getattr(gpu, "_live_power_error_kind", None), getattr(gpu, "_live_power_last", None))
        result, log, *_ = self.both(action)
        self.assertEqual(result[0], (100000, 400000))
        self.assertEqual(result[1], (100000, 400000))  # pl_def_mw missing: a live read is tried and fails


# ---- rig 2: the live rig (tests/test_live_power_backend.py) ----------------------------------------

def live_state(gpu):
    return {"last": getattr(gpu, "_live_power_last", None), "error": getattr(gpu, "_live_power_error", None),
            "kind": getattr(gpu, "_live_power_error_kind", None), "board": getattr(gpu, "_board_policy", None),
            "static": static_of(gpu), "pins": dict(getattr(gpu, "_power_policy_pins", None) or {})}


def faults(*events):
    def configure(fake):
        for key, event in events:
            fake.faults[key] = event
    return configure


def chain(*configures):
    def configure(fake):
        for c in configures:
            if c:
                c(fake)
    return configure


def limits(minimum, rated, maximum):
    """BackendGpu.limits on the fake's rows (the static side is the case's)."""
    def configure(fake):
        at = cp.INFO_BASE + fake.client_index * cp.INFO_STRIDE
        native_at = cp.NATIVE_INFO_BASE + fake.native_index * cp.NATIVE_INFO_STRIDE
        struct.pack_into("<III", fake.info, at + 4, minimum, rated, maximum)
        struct.pack_into("<III", fake.native_info, native_at + 8, minimum, rated, maximum)
    return configure


class LiveRig(MetaRig):
    pass


class LiveSetRig(SetRig):
    pass


class LiveCase(MetadataCase):
    def run_both(self, rig, action, *, static=BACKEND_STATIC, xoc=False, pins=None, nvml=None, remove=()):
        """action(side, power) on the twin, then on the core, each with a fresh fake. pins: the session
        pins (none by default: the re-application returns before any read, as BackendGpu's Mock)."""
        runs = []
        for kind in ("python", "native"):
            rig.fresh()
            rig.calls = []
            power = NvmlPower(lambda: rig.fake)
            exports = dict(rig.card.nvml.exports)
            rig.card.nvml.exports.update(power.exports)
            for name in remove:
                rig.card.nvml.exports.pop(name, None)
            if nvml:
                nvml(power)
            install(rig.card.nvml, rig.card.nvapi, rig.gdi())
            opened = native.Gpu()
            try:
                peer = rig.fake.native_peer
                session_pins = {} if pins is None else dict(pins(rig.fake) if callable(pins) else pins)
                if kind == "python":
                    twin = rig.card.python_twin(opened)
                    twin.static = dict(static)
                    twin.voltage_xoc_enabled = xoc
                    twin._power_policy_pins = session_pins
                    for name in ("PowerTopo", "PowerPolStatus", "VoltRailsStatus"):
                        setattr(twin.nvapi, name, None)
                    if rig.recapture:
                        twin.nvapi.PowerPolInfo = rig.getter
                    side = LivePython(twin)
                    windll, winfunctype = rig.python_patches()
                    with windll, winfunctype:
                        store(side, rig, choice=False)
                        result = action(side, power)
                else:
                    rig.probe = lambda: native.hook_test_state()["patched"]
                    rig.enter = native.hook_test_entry
                    set_static(opened, static)
                    opened.voltage_xoc_enabled = xoc
                    opened._power_policy_pins = session_pins
                    side = MetaNative(opened)
                    store(side, rig, choice=False)
                    result = action(side, power)
                runs.append((result, list(power.log), list(rig.calls), list(rig.fake.calls), rig.fake.requested,
                             dict(live_state(side.gpu), counts=dict(rig.fake.counts))))
            finally:
                opened.close()
                rig.card.nvml.exports.clear()
                rig.card.nvml.exports.update(exports)
            self.assertEqual(rig.errors, [], f"the {kind} run raised inside the fake driver")
        for name, python, core in zip(("result", "nvml log", "gdi calls", "fake calls", "requested", "state"),
                                      *runs, strict=True):
            self.assertEqual(python, core, f"Python and native {name} differ")
        result, nvml_log, _, fake_calls, requested, state = runs[0]
        return result, nvml_log, fake_calls, requested, state


def nvml_writes(log):
    return [value for kind, value in log if kind == "set"]


def sets(fake_calls):
    return [request for command, request in fake_calls if command == cp.SET]


def has_command(fake_calls, command):
    return any(c == command for c, _ in fake_calls)


def steps(*calls):
    def action(side, power):
        return [outcome(lambda c=c: c(side.gpu, power)) for c in calls]
    return action


def set_mw(value):
    return lambda gpu, _power: gpu.set_power_limit_mw(value)


def bounds(**kwargs):
    return lambda gpu, _power: gpu.power_limit_bounds_mw(**kwargs)


def read_limit(gpu, _power):
    return gpu.read_power_limit_mw()


def read_live(gpu, _power):
    return gpu.read_live_power_request()


def live_rig(factory=FakeGpu, configure=None, recapture=False, accepted=MODERN):
    return LiveRig(factory, configure=configure, accepted=accepted, recapture=recapture)


class NormalRouteTests(LiveCase):
    def test_normal_requests_keep_nvml_and_pin_reapplication_without_live_probe(self):
        for requested in (200000, 321000, 549999, 550000):
            with self.subTest(requested=requested):
                result, log, fake_calls, now, state = self.run_both(
                    live_rig(), steps(set_mw(requested)), pins=lambda fake: {fake.native_peer: 87654})
                self.assertEqual(result, [("ok", (True, f"power limit configured to {requested/1000:g} W"))])
                self.assertEqual(nvml_writes(log), [requested])
                self.assertFalse(has_command(fake_calls, cp.INFO) or sets(fake_calls))  # no live probe
                self.assertEqual(now, requested)
                # the real re-application read the table and stored the board candidate
                self.assertTrue(has_command(fake_calls, cp.NATIVE_CONTROL))
                self.assertEqual(state["board"], 31)

    def test_a_moved_pin_is_reapplied_by_the_real_writer_on_both_sides(self):
        # FakeGpu does not answer the policy writer's SET, so the note reports the failure
        result, log, fake_calls, now, state = self.run_both(live_rig(), steps(set_mw(322000)),
                                                            pins=lambda fake: {fake.native_peer: 87000})
        (status, (ok, message)), = result
        self.assertFalse(ok)
        self.assertTrue(message.startswith("power limit configured to 322 W; the driver recalculated policies 2"),
                        message)
        self.assertEqual(nvml_writes(log), [322000])

    def test_failed_extension_probe_keeps_ordinary_nvml(self):
        for event in ({"rm": 0x56}, {"rm": 0x55}, {"guard": "after"}):
            with self.subTest(event=event):
                rig = live_rig(configure=faults(((cp.INFO, 1), event), ((cp.INFO, 2), event)))
                result, log, fake_calls, *_ = self.run_both(rig, steps(bounds(xoc=True), set_mw(322000)))
                self.assertEqual(result[0], ("ok", (200000, 550000)))
                self.assertTrue(result[1][1][0], result[1])
                self.assertEqual(nvml_writes(log), [322000])
                self.assertFalse(sets(fake_calls))

    def test_partial_static_set_uses_actual_range_before_route_selection(self):
        rig = live_rig(configure=limits(180123, 321000, 680456))
        result, log, fake_calls, now, state = self.run_both(rig, steps(set_mw(600000)), static={})
        self.assertTrue(result[0][1][0], result)
        self.assertEqual(nvml_writes(log), [600000])
        self.assertFalse(sets(fake_calls))
        self.assertEqual(state["static"], {"pl_min_mw": 180123, "pl_def_mw": 321000, "pl_max_mw": 680456})


class BoundsTests(LiveCase):
    def test_bounds_default_to_existing_xoc_flag_and_override_does_not_change_it(self):
        def action(side, power):
            gpu = side.gpu
            out = [gpu.power_limit_bounds_mw(), gpu.power_limit_bounds_mw(xoc=True), gpu.voltage_xoc_enabled]
            gpu.voltage_xoc_enabled = True
            out += [gpu.power_limit_bounds_mw(), gpu.power_limit_bounds_mw(xoc=False), gpu.voltage_xoc_enabled]
            return out
        result, _, fake_calls, *_ = self.run_both(live_rig(), action)
        self.assertEqual(result, [(200000, 550000), (1, 1999000), False, (1, 1999000), (200000, 550000), True])

    def test_software_xoc_cap_never_shrinks_a_larger_normal_range(self):
        rig = live_rig(configure=limits(180001, 321000, 2200123))
        result, *_ = self.run_both(rig, steps(bounds(xoc=True), bounds(xoc=False)),
                                   static={"pl_min_mw": 180001, "pl_def_mw": 321000, "pl_max_mw": 2200123})
        self.assertEqual(result, [("ok", (1, 2200123)), ("ok", (180001, 2200123))])

    def test_transient_live_read_returns_error_not_stale_observation_but_retains_range(self):
        # INFO 3 / 4 (the second read's pair) and INFO 5 / 6 (the third's) fail transiently
        events = [((cp.INFO, k), {"nt": -1}) for k in (3, 4, 5, 6)]
        rig = live_rig(configure=faults(*events), recapture=True)
        result, _, _, _, state = self.run_both(rig, steps(bounds(xoc=True), read_live, bounds(xoc=True)))
        self.assertEqual(result[0], ("ok", (1, 1999000)))
        self.assertIsNone(result[1][1][0])
        self.assertEqual(result[2], ("ok", (1, 1999000)))
        self.assertEqual(state["kind"], "transient")
        self.assertIsNotNone(state["last"])

    def test_transient_bounds_keep_a_readable_nvml_request(self):
        # the last live observation stands in for the request only when NVML has none: here NVML
        # reads the moved request and the live read fails transiently
        def move(_gpu, power):
            power.model()._update(600000)
        rig = live_rig(lambda: FakeGpu(requested=800000),
                       configure=faults(((cp.INFO, 3), {"nt": -1}), ((cp.INFO, 4), {"nt": -1})), recapture=True)
        result, log, fake_calls, now, state = self.run_both(rig, steps(bounds(), move, bounds()))
        self.assertEqual(result, [("ok", (200000, 800000)), ("ok", None), ("ok", (200000, 600000))])
        self.assertEqual(state["kind"], "transient")
        self.assertEqual(state["last"]["requested_mw"], 800000)

    def test_bounds_only_retains_observed_extended_endpoint_during_transient_loss_of_both_getters(self):
        rig = live_rig(lambda: FakeGpu(requested=800000),
                       configure=faults(*[((cp.INFO, k), {"nt": -1}) for k in (3, 4, 5, 6)]), recapture=True)

        def lose(_gpu, power):
            power.read_status = 9
        result, log, fake_calls, *_ = self.run_both(
            rig, steps(bounds(xoc=False), lose, bounds(xoc=False), read_limit))
        self.assertEqual(result[0], ("ok", (200000, 800000)))
        self.assertEqual(result[2], ("ok", (200000, 800000)))
        self.assertEqual(result[3], ("ok", None))
        self.assertFalse(nvml_writes(log) or sets(fake_calls))

    def test_unrestorable_fe_keeps_normal_bounds_and_prevents_extended_set(self):
        for prior in (0, cp.MISSING):
            with self.subTest(prior=prior):
                rig = live_rig(lambda prior=prior: FakeGpu(requested=prior))
                result, log, fake_calls, *_ = self.run_both(rig, steps(bounds(), set_mw(600000)), xoc=True)
                self.assertEqual(result[0], ("ok", (200000, 550000)))
                ok, message = result[1][1]
                self.assertFalse(ok)
                self.assertIn("no write", message)
                self.assertFalse(nvml_writes(log) or sets(fake_calls))

    def test_successful_live_binding_supplies_missing_static_range_and_default(self):
        for static in ({}, {"pl_min_mw": None, "pl_max_mw": None}, {"pl_def_mw": 321000}):
            with self.subTest(static=static):
                rig = live_rig(lambda: FakeGpu(client_index=63, native_index=29),
                               configure=limits(180123, 321000, 680456))
                result, log, fake_calls, _, state = self.run_both(rig, steps(bounds(xoc=False)), static=static)
                self.assertEqual(result, [("ok", (180123, 680456))])
                self.assertEqual(state["static"], {"pl_min_mw": 180123, "pl_def_mw": 321000, "pl_max_mw": 680456})
                self.assertFalse(nvml_writes(log) or sets(fake_calls))

    def test_live_metadata_does_not_replace_existing_normal_static_values(self):
        result, _, _, _, state = self.run_both(live_rig(), steps(read_live, bounds()),
                                               static=dict(BACKEND_STATIC, pl_min_mw=0))
        self.assertEqual(state["static"]["pl_min_mw"], 0)
        self.assertEqual(result[1], ("ok", (1, 550000)))  # low = max(1, 0)

    def test_refresh_releases_sticky_power_capability_without_a_write(self):
        def refresh(gpu, _power):
            if not isinstance(gpu, native.Gpu):
                gpu._refresh_volt_rail_capabilities = lambda: None
            gpu.refresh_capabilities()
            return live_state(gpu)["last"]
        result, log, fake_calls, *_ = self.run_both(live_rig(), steps(bounds(xoc=True), refresh, bounds(xoc=True)))
        self.assertEqual(result[1], ("ok", None))
        self.assertEqual(result[2], ("ok", (1, 1999000)))
        self.assertFalse(nvml_writes(log) or sets(fake_calls))


class LiveRouteTests(LiveCase):
    def test_transient_first_probe_recovers_and_optional_status_does_not_gate_extension(self):
        events = [((cp.INFO, 1), {"nt": -1})] + [((cp.NATIVE_STATUS, k), {"rm": 0x56}) for k in range(1, 9)]
        rig = live_rig(configure=faults(*events), recapture=True)
        result, log, fake_calls, now, state = self.run_both(rig, steps(bounds(), set_mw(600001)), xoc=True)
        self.assertEqual(result[0], ("ok", (1, 1999000)))
        self.assertTrue(result[1][1][0], result[1])
        self.assertIsNone(state["last"]["arbitration"])
        self.assertTrue(state["last"]["arbitration_error"])
        self.assertEqual(now, 600001)
        self.assertFalse(nvml_writes(log))

    def test_xoc_extended_request_changes_only_source_and_never_reapplies_pins(self):
        for requested in (1, 199999, 550001, 999000, 1000000, 1999000):
            with self.subTest(requested=requested):
                def configure(fake):
                    fake.lower_pmu = 450000
                rig = live_rig(configure=configure)
                result, log, fake_calls, now, state = self.run_both(
                    rig, steps(set_mw(requested)), xoc=True, pins=lambda fake: {fake.native_peer: 87000})
                self.assertEqual(result, [("ok", (True, f"power request configured to {requested/1000:g} W; "
                                                        "downstream power/current limits can be lower"))])
                self.assertEqual(now, requested)
                self.assertEqual(len(sets(fake_calls)), 1)
                self.assertFalse(nvml_writes(log))
                self.assertEqual(state["pins"], {2: 87000})  # never re-applied
                self.assertFalse(has_command(fake_calls, 0x2080E61B))

    def test_v30_live_request_reapplies_user_pins_and_reports_failure(self):
        # the definition is a v30 role (ROLE_INFO byte 4 == 0x30): the real writer re-applies the
        # pinned peers after the board SET (the fake recalculates them); a refused writer SET is a
        # failure note
        for name, event in (("restored", {}), ("refused", {"rm": 0x55})):
            with self.subTest(name):
                rig = LiveSetRig(PeerCoupledGpu, accepted=COUPLED_ACCEPTED, recapture=True,
                                 configure=faults(((0x2080E61B, 1), event)) if event else None)
                pins = lambda fake: {p: base for p, (base, _) in fake.peer_requests.items()}  # noqa: E731
                result, log, fake_calls, now, state = self.run_both(
                    rig, steps(set_mw(600000)), xoc=True, pins=pins,
                    static={"pl_min_mw": 200000, "pl_def_mw": 321000, "pl_max_mw": 550000})
                (_, (ok, message)), = result
                self.assertTrue(message.startswith("power request configured to 600 W; the driver may recalculate "
                                                   "other policies; downstream power/current limits can be lower"),
                                message)
                self.assertEqual(ok, name == "restored", message)
                self.assertIn("the driver recalculated policies", message)
                self.assertEqual(now, 600000)
                self.assertFalse(nvml_writes(log))
                self.assertEqual(state["board"], 29)

    def test_v30_reapplication_is_given_the_requested_board_limit(self):
        # board_mw=mw: the re-application finds the board policy as the one mW row whose request
        # equals the new limit, stores it and drops a pin on it (the write itself set the board)
        rig = LiveSetRig(PeerCoupledGpu, accepted=COUPLED_ACCEPTED, recapture=True)
        board = {}

        def pins(fake):
            board["policy"] = fake.native_index
            return {**{p: base for p, (base, _) in fake.peer_requests.items()}, fake.native_index: 555000}
        result, log, fake_calls, now, state = self.run_both(
            rig, steps(set_mw(600000)), xoc=True, pins=pins,
            static={"pl_min_mw": 200000, "pl_def_mw": 321000, "pl_max_mw": 550000})
        (_, (ok, message)), = result
        self.assertEqual(now, 600000)
        self.assertEqual(state["board"], board["policy"])
        self.assertNotIn(board["policy"], state["pins"])  # the board pin was dropped, never re-written
        self.assertTrue(state["pins"])
        self.assertFalse(nvml_writes(log))

    def test_missing_client_value_is_refused_where_the_bounds_would_allow_it(self):
        # pl_max_mw above 2**32 and xoc off: the live bounds reach past 0xFFFFFFFF, so only the
        # missing-client check refuses 0xFFFFFFFF on the live route (no NVML setter)
        static = {"pl_min_mw": 200000, "pl_def_mw": 321000, "pl_max_mw": 1 << 33}
        result, log, fake_calls, now, _ = self.run_both(live_rig(), steps(bounds(), set_mw(0xFFFFFFFF)),
                                                        static=static, remove=(SET_LIMIT,))
        self.assertEqual(result[0], ("ok", (200000, 1 << 33)))
        self.assertEqual(result[1], ("ok", (False, "power request 4.29497e+06 W out of [200..8.58993e+06] W; "
                                                   "extended requests require XOC")))
        self.assertFalse(sets(fake_calls) or nvml_writes(log))
        self.assertEqual(now, 321000)

    def test_first_extension_requires_existing_xoc_opt_in(self):
        for requested in (199999, 550001, 999000, 1999000):
            with self.subTest(requested=requested):
                result, log, fake_calls, *_ = self.run_both(live_rig(), steps(set_mw(requested)))
                (_, (ok, message)), = result
                self.assertFalse(ok)
                self.assertEqual(message, f"power request {requested/1000:g} W out of [200..550] W; "
                                          "extended requests require XOC")
                self.assertFalse(nvml_writes(log) or sets(fake_calls))

    def test_requests_above_software_envelope_do_not_dispatch(self):
        for requested in (1999001, 0xFFFFFFFE, 0xFFFFFFFF):
            with self.subTest(requested=requested):
                result, log, fake_calls, *_ = self.run_both(live_rig(), steps(set_mw(requested)), xoc=True)
                self.assertFalse(result[0][1][0])
                self.assertFalse(nvml_writes(log) or sets(fake_calls))

    def test_malformed_requests_make_no_calls(self):
        for value in (None, True, "600000", float("inf"), float("nan"), 600000.5, 0, -1):
            with self.subTest(value=value):
                result, log, fake_calls, *_ = self.run_both(live_rig(), steps(set_mw(value)))
                self.assertEqual(result, [("ok", (False, INVALID))])
                self.assertEqual(log, [])
                self.assertEqual(fake_calls, [])

    def test_xoc_off_carryover_allows_only_staying_or_moving_toward_normal(self):
        for current, expected, allowed, denied in ((800000, (200000, 800000), 700000, 800001),
                                                   (1999000, (200000, 1999000), 1500000, 1999001),
                                                   (100000, (100000, 550000), 150000, 99999)):
            with self.subTest(current=current):
                rig = live_rig(lambda current=current: FakeGpu(requested=current))
                result, log, fake_calls, *_ = self.run_both(rig, steps(
                    bounds(), set_mw(denied), set_mw(current), set_mw(allowed), bounds(), set_mw(321000), bounds(),
                    set_mw(322000)))
                self.assertEqual(result[0], ("ok", expected))
                self.assertFalse(result[1][1][0])
                self.assertTrue(result[2][1][0] and result[3][1][0])
                self.assertEqual(result[4], ("ok", (min(200000, allowed), max(550000, allowed))))
                self.assertTrue(result[5][1][0])
                self.assertEqual(result[6], ("ok", (200000, 550000)))
                self.assertTrue(result[7][1][0], result[7])
                self.assertEqual(len(sets(fake_calls)), 3)
                self.assertEqual(nvml_writes(log), [322000])

    def test_live_return_to_normal_does_not_depend_on_nvml_getter_or_setter(self):
        def nvml(power):
            power.read_status = 9
        rig = live_rig(lambda: FakeGpu(requested=800000))
        result, log, fake_calls, now, _ = self.run_both(rig, steps(read_limit, set_mw(321000)), nvml=nvml,
                                                        remove=(SET_LIMIT,))
        self.assertEqual(result[0], ("ok", 800000))
        self.assertTrue(result[1][1][0], result[1])
        self.assertEqual(len(sets(fake_calls)), 1)
        self.assertEqual(now, 321000)

    def test_normal_request_uses_live_path_when_nvml_capability_is_partial(self):
        for missing in ("getter", "setter", "both"):
            with self.subTest(missing=missing):
                remove = {"getter": (GET,), "setter": (SET_LIMIT,), "both": (GET, SET_LIMIT)}[missing]
                result, log, fake_calls, now, state = self.run_both(
                    live_rig(), steps(set_mw(322001)), remove=remove, pins=lambda fake: {fake.native_peer: 87000})
                self.assertTrue(result[0][1][0], result)
                self.assertEqual(now, 322001)
                self.assertEqual(len(sets(fake_calls)), 1)
                self.assertFalse(nvml_writes(log))
                self.assertFalse(has_command(fake_calls, 0x2080E61B))  # no pin re-application

    def test_live_failure_uses_one_compensation_and_never_falls_back_to_nvml(self):
        for event in ({"ignore": True}, {"rm": 0x55}, {"raise": RuntimeError("partial SET")}):
            with self.subTest(event=repr(event)):
                rig = LiveSetRig(FakeGpu, accepted=MODERN, recapture=True, configure=faults(((cp.SET, 1), event)))
                result, log, fake_calls, now, state = self.run_both(rig, steps(set_mw(600000), read_limit), xoc=True)
                (_, (ok, message)), read = result
                self.assertFalse(ok)
                self.assertTrue(message.startswith("live power request failed: "), message)
                self.assertIn("Original request restored and verified", message)
                self.assertEqual(len(sets(fake_calls)), 2)
                self.assertFalse(nvml_writes(log))
                self.assertEqual(now, 321000)
                self.assertEqual(state["last"]["requested_mw"], 321000)  # the verified readback

    def test_failed_live_rollback_is_reported_without_an_extra_nvml_write(self):
        rig = LiveSetRig(FakeGpu, accepted=MODERN, recapture=True,
                         configure=faults(((cp.SET, 1), {"rm": 0x55}), ((cp.SET, 2), {"ignore": True, "rm": 0x56})))
        result, log, fake_calls, now, state = self.run_both(rig, steps(set_mw(600000), read_limit), xoc=True)
        (_, (ok, message)), read = result
        self.assertFalse(ok)
        self.assertIn("restoration was not verified", message)
        self.assertEqual(len(sets(fake_calls)), 2)
        self.assertFalse(nvml_writes(log))
        self.assertEqual(read, ("ok", 600000))

    def test_transient_write_preflight_does_not_use_cached_capability_as_a_request(self):
        rig = live_rig(configure=faults(((cp.INFO, 3), {"nt": -1}), ((cp.INFO, 4), {"nt": -1})), recapture=True)
        result, log, fake_calls, *_ = self.run_both(rig, steps(bounds(), set_mw(600000)), xoc=True)
        self.assertEqual(result[0], ("ok", (1, 1999000)))
        (_, (ok, message)) = result[1]
        self.assertFalse(ok)
        self.assertTrue(message.startswith("live power request not available; no write issued: "), message)
        self.assertFalse(nvml_writes(log) or sets(fake_calls))

    def test_missing_nvml_getter_and_failed_live_probe_are_not_retried_by_route_selection(self):
        for target in (322000, 600000):
            with self.subTest(target=target):
                rig = live_rig(configure=faults(((cp.INFO, 1), {"nt": -1}), ((cp.INFO, 2), {"nt": -1})),
                               recapture=True)
                result, log, fake_calls, _, state = self.run_both(rig, steps(set_mw(target)), xoc=True,
                                                                 remove=(GET,))
                (_, (ok, message)), = result
                self.assertFalse(ok)
                self.assertIn("no write issued", message)
                # one live probe (its bounded transport recovery included), not retried by route selection
                self.assertEqual(state["counts"][cp.INFO], 2)
                self.assertFalse(nvml_writes(log) or sets(fake_calls))


class TelemetryTests(LiveCase):
    """nvbackend's _read_power calls read_power_limit_mw inside the sweep (NVML up): the live fallback,
    its RM escapes and its bookkeeping run there, on both sides."""

    def test_the_sweep_falls_back_to_the_live_request(self):
        result, log, fake_calls, _, state = self.run_both(live_rig(lambda: FakeGpu(requested=800000)), steps(tick),
                                                          nvml=failing_getter)
        self.assertEqual(result, [("ok", 800000)])
        self.assertEqual(state["last"]["requested_mw"], 800000)
        self.assertTrue(has_command(fake_calls, cp.INFO))

    def test_the_sweep_uses_nvml_without_an_escape(self):
        result, log, fake_calls, *_ = self.run_both(live_rig(), steps(tick))
        self.assertEqual(result, [("ok", 321000)])
        self.assertEqual(fake_calls, [])

    def test_a_live_sweep_back_fills_static_and_later_calls_read_alike(self):
        # NVML's getter failing, NVAPI live, static pl_* None (or absent): the sweep's live read
        # fills self.static on both sides, so the bounds and the set that follow (getter restored)
        # see the same statics and issue the same reads
        for static in (NONE_STATIC, {}):
            for after in (bounds(), set_mw(322000)):
                with self.subTest(static=static, after=after):
                    result, log, fake_calls, now, state = self.run_both(
                        live_rig(lambda: FakeGpu(requested=321000)), steps(tick, statics_now, heal, after, statics_now),
                        nvml=failing_getter, static=static)
                    self.assertEqual(result[0], ("ok", 321000))
                    self.assertEqual(result[1], ("ok", {"pl_min_mw": 200000, "pl_def_mw": 321000, "pl_max_mw": 550000}))
                    self.assertTrue(result[3][1])
                    self.assertEqual(result[4], result[1])

    def test_a_failed_live_sweep_leaves_static_none(self):
        rig = live_rig(lambda: FakeGpu(requested=321000),
                       configure=faults(((cp.INFO, 1), {"nt": -1}), ((cp.INFO, 2), {"nt": -1})), recapture=True)
        result, log, fake_calls, now, state = self.run_both(rig, steps(tick, statics_now), nvml=failing_getter,
                                                            static=NONE_STATIC)
        self.assertEqual(result, [("ok", None), ("ok", NONE_STATIC)])
        self.assertEqual(state["static"], NONE_STATIC)

    def test_a_readable_getter_neither_reads_live_nor_back_fills(self):
        result, log, fake_calls, now, state = self.run_both(live_rig(), steps(tick, statics_now), static=NONE_STATIC)
        self.assertEqual(result, [("ok", 321000), ("ok", NONE_STATIC)])
        self.assertEqual(fake_calls, [])

    def test_a_key_cleared_after_a_live_sweep_stays_none_through_an_nvml_sweep(self):
        def clear(gpu, _power):
            gpu.static["pl_def_mw"] = None
        result, log, fake_calls, now, state = self.run_both(
            live_rig(lambda: FakeGpu(requested=321000)), steps(tick, clear, heal, tick, statics_now),
            nvml=failing_getter, static=NONE_STATIC)
        self.assertEqual(result[3], ("ok", 321000))
        self.assertEqual(result[4], ("ok", {"pl_min_mw": 200000, "pl_def_mw": None, "pl_max_mw": 550000}))


NONE_STATIC = {"pl_min_mw": None, "pl_def_mw": None, "pl_max_mw": None}


def tick(gpu, _power):
    """One telemetry sweep's pl_requested_mw (nvbackend's _read_power on the twin)."""
    if isinstance(gpu, native.Gpu):
        return gpu.read().get("pl_requested_mw")
    d = {}
    gpu._read_power(d)
    return d.get("pl_requested_mw")


def statics_now(gpu, _power):
    return {key: gpu.static.get(key) for key in PL}


def failing_getter(power):
    power.read_status = 9


def heal(_gpu, power):
    power.read_status = 0


def policies(gpu, _power):
    rows, error = gpu.read_power_policies()
    return [(r["policy"], r["board"], r["maybe_board"], r.get("writable"), r.get("requested")) for r in rows], error


class PowerPoliciesReaderTests(LiveCase):
    """read_power_policies with the REAL read_power_limit_mw on both sides (the native inputs callback
    calls the binding's, which runs dc_pl_read): the board candidate, _board_policy, the rows and
    the statics (back-filled by a live fallback before the rows are built) agree."""

    def test_with_the_real_reader(self):
        for static in (NONE_STATIC, {}, BACKEND_STATIC):
            for name, nvml, remove in (("failing", failing_getter, ()), ("working", None, ()),
                                       ("missing", None, (GET,))):
                with self.subTest(static=static, getter=name):
                    result, log, fake_calls, now, state = self.run_both(
                        live_rig(), steps(policies, statics_now), static=static, nvml=nvml, remove=remove)
                    (status, (rows, error)), (_, static_after) = result
                    self.assertEqual(status, "ok")
                    if name == "working" or static == BACKEND_STATIC:
                        self.assertEqual(static_after, {**{k: None for k in PL}, **static})
                    else:  # the live fallback filled the statics before the rows were built
                        self.assertEqual(static_after, BACKEND_STATIC)
                        self.assertIsNotNone(state["last"])
                    if name == "working":
                        self.assertFalse(has_command(fake_calls, cp.INFO))


# ---- the pure helpers -----------------------------------------------------------------------------

class PureTests(unittest.TestCase):
    """_normal_power_bounds_mw and _power_request_bounds_mw on a bare twin and a bare handle."""

    def setUp(self):
        self.rig = MetaRig(FakeGpu, accepted=MODERN)
        install(self.rig.card.nvml, self.rig.card.nvapi, self.rig.gdi())
        self.native = native.Gpu()
        self.twin = self.rig.card.python_twin(self.native)

    def tearDown(self):
        self.native.close()
        native.set_export_resolver(None)

    def both(self, static, call):
        self.twin.static = dict(static)
        set_static(self.native, static)
        return outcome(lambda: call(self.twin)), outcome(lambda: call(self.native))

    def test_constant(self):
        self.assertEqual(native.Gpu.POWER_LIMIT_XOC_MAX_MW, NV.GPU.POWER_LIMIT_XOC_MAX_MW)

    def test_normal_bounds_fallbacks_are_pythons(self):
        for static in ({}, {"pl_min_mw": None}, {"pl_max_mw": 7}, {"pl_min_mw": 0, "pl_max_mw": 0},
                       {"pl_min_mw": -5, "pl_max_mw": 1 << 40}, BACKEND_STATIC):
            with self.subTest(static=static):
                python, core = self.both(static, lambda g: g._normal_power_bounds_mw())
                self.assertEqual(python, core)

    def test_request_bounds(self):
        currents = (None, 0, 1, 199999, 200000, 800000, 1999001, 0xFFFFFFFE, 0xFFFFFFFF, -1, 1 << 40, 1 << 70,
                    True, 600000.0, "600000")
        for static in ({}, BACKEND_STATIC, {"pl_min_mw": 0, "pl_max_mw": 2200123}, {"pl_min_mw": -9, "pl_max_mw": 1},
                       {"pl_min_mw": 100000, "pl_max_mw": 0xFFFFFFFF}, {"pl_min_mw": 100000, "pl_max_mw": 1 << 40}):
            for current in currents:
                for xoc in (False, True, 0, 1, None, "x"):
                    for live in (False, True):
                        python, core = self.both(static, lambda g, c=current, x=xoc, lv=live:
                                                 g._power_request_bounds_mw(c, xoc=x, live=lv))
                        self.assertEqual(python, core, (static, current, xoc, live))
        # the extended envelope is capped at 0xFFFFFFFE however large the normal maximum
        for high in (0xFFFFFFFF, 1 << 40):
            python, core = self.both({"pl_min_mw": 100000, "pl_max_mw": high},
                                     lambda g: g._power_request_bounds_mw(None, xoc=True, live=True))
            self.assertEqual(core, python)
            self.assertEqual(core, ("ok", (1, 0xFFFFFFFE)))


class FormattingTests(LiveCase):
    def test_out_of_envelope_messages_format_as_pythons_g(self):
        values = (1999001, 2000000, 12345678, 123456789, 4294967294, 2147483647, 99999999, 2345678)
        result, *_ = self.run_both(live_rig(), steps(*[set_mw(v) for v in values]), xoc=True)
        for value, (_, (ok, message)) in zip(values, result, strict=True):
            self.assertFalse(ok)
            self.assertEqual(message, f"power request {value/1000:g} W out of [0.001..1999] W; extended requests require XOC")

    def test_in_range_figures(self):
        values = (549999, 1234567, 100000, 1999000)
        result, *_ = self.run_both(
            live_rig(configure=limits(1, 321000, 4000000)), steps(*[set_mw(v) for v in values]),
            static={"pl_min_mw": 1, "pl_def_mw": 321000, "pl_max_mw": 4000000})
        for value, (_, (ok, message)) in zip(values, result, strict=True):
            self.assertTrue(ok, message)
            self.assertEqual(message, f"power limit configured to {value/1000:g} W")
        self.assertEqual([m for _, (_, m) in result][0], "power limit configured to 549.999 W")
        self.assertEqual([m for _, (_, m) in result][1], "power limit configured to 1234.57 W")


# ---- the binding and the ABI -------------------------------------------------------------------------

class AbiTests(unittest.TestCase):
    def tearDown(self):
        native.set_export_resolver(None)

    def open(self):
        rig = MetaRig(FakeGpu, accepted=MODERN)
        install(rig.card.nvml, rig.card.nvapi, rig.gdi())
        return native.Gpu()

    def test_argument_refusals(self):
        lib = native.load()
        statics = native._ClStatics()
        low, high, has, mw = ctypes.c_int64(), ctypes.c_int64(), ctypes.c_int32(), ctypes.c_uint32()
        self.assertEqual(lib.dc_pl_normal_bounds(ctypes.byref(statics), ctypes.byref(low), ctypes.byref(high)),
                         native.DC_E_ARGUMENT)  # size unset
        statics.size = ctypes.sizeof(statics)
        statics.pl_min_state = 7
        self.assertEqual(lib.dc_pl_normal_bounds(ctypes.byref(statics), ctypes.byref(low), ctypes.byref(high)),
                         native.DC_E_ARGUMENT)
        statics.pl_min_state = 0
        self.assertEqual(lib.dc_pl_normal_bounds(None, ctypes.byref(low), ctypes.byref(high)), native.DC_E_ARGUMENT)
        self.assertEqual(lib.dc_pl_normal_bounds(ctypes.byref(statics), None, ctypes.byref(high)), native.DC_E_ARGUMENT)
        self.assertEqual(lib.dc_pl_read_nvml(None, ctypes.byref(has), ctypes.byref(mw)), native.DC_E_ARGUMENT)
        with self.open() as gpu:
            ok, needed = ctypes.c_int32(), ctypes.c_size_t()
            self.assertEqual(lib.dc_pl_set(gpu._handle, None, 300000, 0, ctypes.byref(ok), None, 0,
                                           ctypes.byref(needed)), native.DC_E_ARGUMENT)
            self.assertEqual(lib.dc_pl_set(gpu._handle, ctypes.byref(statics), 300000, 0, None, None, 0,
                                           ctypes.byref(needed)), native.DC_E_ARGUMENT)
            error = native._Error()
            self.assertEqual(lib.dc_pl_read(gpu._handle, ctypes.byref(statics), ctypes.byref(has), ctypes.byref(mw),
                                            ctypes.byref(error)), native.DC_E_ARGUMENT)  # error size unset
            self.assertEqual(lib.dc_pl_bounds(gpu._handle, ctypes.byref(statics), 0, ctypes.byref(low), None, None),
                             native.DC_E_ARGUMENT)

    def test_core_range_check_and_the_kept_message(self):
        # the binding refuses first; the core refuses the same values itself with Python's text
        lib = native.load()
        with self.open() as gpu:
            statics = gpu._pl_statics()
            ok, needed = ctypes.c_int32(), ctypes.c_size_t()
            for value in (0, -1, 1 << 32, -(1 << 63)):
                buffer = ctypes.create_string_buffer(8)
                self.assertEqual(lib.dc_pl_set(gpu._handle, ctypes.byref(statics), value, 0, ctypes.byref(ok), buffer,
                                               len(buffer), ctypes.byref(needed)), native.DC_OK)
                self.assertEqual((ok.value, needed.value), (0, len(INVALID) + 1))
                self.assertEqual(buffer.value, INVALID[:7].encode())  # truncated, NUL-terminated
                full = ctypes.create_string_buffer(needed.value)
                self.assertEqual(lib.dc_pl_message_get(gpu._handle, full, len(full), ctypes.byref(needed)), native.DC_OK)
                self.assertEqual(full.value.decode(), INVALID)

    def test_set_power_limit_nvml_refusals(self):
        with self.open() as gpu:
            for value, error in ((1.5, TypeError), ("1", TypeError), (1 << 63, ValueError)):
                with self.subTest(value=value), self.assertRaises(error):
                    gpu._set_power_limit_nvml(value)
            with self.assertRaises(AttributeError):  # the card exports no setter
                gpu._set_power_limit_nvml(300000)

    def test_a_non_integer_static_is_none(self):
        # recorded divergence: Python returns the float itself as the bound
        with self.open() as gpu:
            set_static(gpu, {"pl_min_mw": 150000.5, "pl_max_mw": 400000.0})
            self.assertEqual(gpu._normal_power_bounds_mw(), (50000, 400000))

    def test_the_sweep_without_the_live_layer_is_unchanged(self):
        # a handle whose NVML request reads: no escape in the sweep (the reader is NVML first)
        with self.open() as gpu:
            self.assertNotIn("pl_requested_mw", gpu.read())  # this card exports no NVML limit getter


if __name__ == "__main__":
    unittest.main()
