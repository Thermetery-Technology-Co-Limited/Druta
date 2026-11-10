# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native reset_all (increment G3: dc_gpu_reset_all) against nvbackend.GPU.reset_all.

Both sides run every step for real on the same fakes; nothing on either side is mocked above the
driver except what the existing twins already mock (the current-policy transport capture and the
escape below the adapter match, as tests.test_native_power_policies does). Three rigs reach every
step between them:

- Card (this module): one NVML/NVAPI table assembled from the existing per-knob rigs (the clock
  offsets, NVML lock and V/F curve of tests.test_native_clocks.ClockRig; the V/F point lock and
  voltage boost of tests.test_native_vflock.LockRig; the clock-control block of
  tests.test_native_clkdom.ClkDomRig; the fans of tests.test_native_fan.FanRig) over the policy
  table of tests.test_power_policies.PolicyCard behind tests.test_native_current_limits.CLRig's
  escape, with NVML's power limit answering from that card. It has no rail getters: the rail-limit
  step takes its "unreadable" branch on both sides;
- the rail-setter rig of tests.test_native_rail_setters (rail getters, boost and rail SET escapes
  through the hooked thunk), for the rail-limit step, its writer attributes and the hold record;
- the legacy P0 rig of tests.test_native_legacy_p0 (a Kepler-class ForcePstate), for the P0 step
  and its ownership.

On every rig a step whose knob the rig does not model takes the same "unavailable" branch on both
sides; that is compared like any other step. Each case compares, in order, the full list of
(name, ok, message), every fake's call log (the core's PCI adapter match on the escape transport is
left out: the twin's escape is mocked below that point), the fakes' state, the policy pins and the
clock-lock request afterwards (and, on the other rigs, the hold record, the writer attributes and
the P0 ownership).

Raised kinds: Python's reset_all lets any callee's exception escape. Through the core only the
callbacks can raise (the static keys, read_power_limit_mw's inputs), plus the IndexError Python lets
escape set_power_limit_mw / read_power_policies / _write_policy_limit and MemoryError for the core's
allocation guard; a failing driver call is a returned status on both sides (RaisedCalleeTests).

The live rig of tests.test_native_power_limit (LiveCase) is a fourth rig, for the power-limit step's
live read and the back-fill of the static pl_* keys it drives (BackfillTests).

Recorded divergences (DESIGN-TRANSPORT.md, reset all):
- the static back-fill's order: Python back-fills self.static inside read_live_power_request, before
  set_power_limit_mw writes anything; the core runs set_power_limit_mw to completion and then calls
  the back-fill, so a static mapping whose item assignment raises stops Python before the power-limit
  write and the core after it. The binding's own dict never raises; no case here injects one;
- a truthy pl_def_mw that is not an integer goes to the core as no value ("default unknown") where
  Python calls set_power_limit_mw with it; no writer of the static stores such a value."""
import copy
import ctypes
import re
import unittest
from unittest.mock import Mock, patch

from druta import native, nvbackend as NV

if not native.available():
    raise unittest.SkipTest("native core not built (native/build.sh linux)")

import tests.test_native_current_limits as cl  # noqa: E402
import tests.test_native_power_limit as pl  # noqa: E402
from druta import native_rails as nr  # noqa: E402
from tests.test_native_clkdom import ClkDomRig  # noqa: E402
from tests.test_native_clocks import ClockRig  # noqa: E402
from tests.test_native_fan import FanRig  # noqa: E402
from tests.test_native_legacy_p0 import KEPLER, MAXWELL, P0Parity, P0Rig  # noqa: E402
from tests.test_native_rail_reference import BASES, config  # noqa: E402
from tests.test_native_rail_setters import SetterNativeSide, SetterPythonSide, SetterRig  # noqa: E402
from tests.test_native_rail_write import blackwell, sets, turing  # noqa: E402
from tests.test_native_telemetry import u32_callback  # noqa: E402
from tests.test_native_vflock import LockRig, entry  # noqa: E402
from tests.test_power_policies import A612, BOARD, COUPLED, TITAN, PolicyCard  # noqa: E402

CDECL = ctypes.CFUNCTYPE
GPU = NV.GPU
PASCAL, TURING = 4, 6
# every interface nvbackend.NvAPI binds: the twins leave out the ones their rig does not serve
NVAPI_NAMES = sorted(set(re.findall(r"self\.(\w+)\s*=\s*self\._i\(", open(NV.__file__).read())))
ADAPTER_MATCH = {"EnumAdapters2", "QueryAdapterInfo", "CloseAdapter"}
STATICS = {"pl_min_mw": 100000, "pl_def_mw": 260000, "pl_max_mw": 320000}
POINT = NV.VF_LOCK_MODE_POINT
RAIL_UNREADABLE = ("rail limits", False, "rail limits were not reset: current values unreadable or incomplete")
NO_DEFAULT = ("power limit", False, "power limit: default unknown, left unchanged")

# What each part rig serves on the shared card: (NVML exports, NVAPI interface names).
SERVES = {
    "clock": (("nvmlErrorString", "nvmlDeviceGetSupportedGraphicsClocks", "nvmlDeviceGetClockOffsets",
               "nvmlDeviceSetClockOffsets", "nvmlDeviceSetGpuLockedClocks", "nvmlDeviceResetGpuLockedClocks"),
              ("VfpCurve", "BoostTableGet", "BoostTableSet", "Pstates20Get", "Pstates20Set")),
    "lock": ((), ("BoostLock", "VfLockSet", "VoltCtrlGet", "VoltCtrlSet")),
    "clkdom": ((), ("ClkDomCtlGet", "ClkDomCtlSet")),
    "fan": (("nvmlDeviceGetFanControlPolicy_v2", "nvmlDeviceGetTargetFanSpeed", "nvmlDeviceSetFanSpeed_v2",
             "nvmlDeviceSetDefaultFanSpeed_v2"), ("FanCoolersControl", "FanCoolersStatus", "FanCoolersSetControl")),
}


def fill_nvapi(twin):
    """The interfaces the rig does not serve are None on the twin, as NvAPI binds a missing one."""
    for name in NVAPI_NAMES:
        if not hasattr(twin.nvapi, name):
            setattr(twin.nvapi, name, None)


def steps_of(gpu):
    out = gpu.reset_all()
    assert all(isinstance(step, NV.ResetStep) for step in out), out
    return [(step.name,) + tuple(step) for step in out]


def attempt(call):
    try:
        return ("ok", call())
    except Exception as exc:  # noqa: BLE001 - the type and text are what is compared
        return ("raise", type(exc).__name__, str(exc))


class Card:
    """One card assembled from the per-knob rigs (module docstring); every part keeps its own
    state and call log, and the base CLRig's escape reaches the PolicyCard through a Driver."""

    def __init__(self, *, arch=TURING, clock=None, lock=None, clkdom=None, fan=None, table=None):
        self.arch = arch
        self.table = table
        self.card = PolicyCard(*(table or ()))
        self.header = [0] * 17
        self.header[2], self.header[14], self.header[15] = 54420, A612, 54352
        self.driver = cl.Driver(self.card.escape)
        self.base = cl.CLRig(arch, self.driver)
        self.parts = {"clock": ClockRig(arch=arch, **(clock or {})), "lock": LockRig(arch=arch, **(lock or {})),
                      "clkdom": ClkDomRig(arch=arch, **(clkdom or {})), "fan": FanRig(**(fan or {}))}
        e, i, n = self.base.nvml.exports, self.base.nvapi.interfaces, self.base.names
        for key, part in self.parts.items():
            exports, names = SERVES[key]
            for name in exports:
                if name in part.nvml.exports:
                    e[name] = part.nvml.exports[name]
                else:
                    e.pop(name, None)
            for name in names:
                fn = part.names.get(name)
                n[name] = fn
                for ident in [k for k, v in part.nvapi.interfaces.items() if v is fn and fn is not None]:
                    i[ident] = fn

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def limit_get(device, pointer):
            ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32))[0] = self.card.request[self.card.board]
            return 0

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
        def limit_set(device, mw):
            self.base.calls.append(("SetPowerManagementLimit", mw))
            self.card.request[self.card.board] = mw
            self.card.derive()  # measured: the coupled policies follow the board limit
            return 0
        self._keep = (limit_get, limit_set)
        e["nvmlDeviceGetPowerManagementLimit"] = limit_get
        e["nvmlDeviceSetPowerManagementLimit"] = limit_set
        e["nvmlDeviceGetArchitecture"] = u32_callback(arch)

    def reset(self):
        for part in self.parts.values():
            part.reset()
        self.base.reset()
        self.card.__init__(*(self.table or ()))
        self.driver.log.clear()
        self.driver.fail_at, self.driver.fail, self.driver.refuse = set(), -1, None

    def refuse(self, indexes=None, rm=0x40):
        """The driver answers these escapes of what follows (counted from 0 after the setup; all of
        them when None) with an RM error. Not an NTSTATUS failure: that discards the captured
        transport, which the twin's mocked capture would then hand back and the core cannot."""
        driver = self.driver
        driver.refuse = lambda packet: rm if indexes is None or len(driver.log) - 1 in indexes else None

    def clear_logs(self):
        for part in self.parts.values():
            part.calls.clear()
        self.base.calls.clear()
        self.driver.log.clear()

    def logs(self):
        out = {key: copy.deepcopy(part.calls) for key, part in self.parts.items()}
        out["nvml"] = [c for c in self.base.calls if c[0] not in ADAPTER_MATCH]
        out["escapes"] = copy.deepcopy(self.driver.log)
        return out

    def state(self):
        return {**{key: part.state() for key, part in self.parts.items()}, "policies": dict(self.card.request),
                "policy sets": list(self.card.sets)}

    def python_gpu(self):
        gpu = self.base.python_gpu()
        for part in self.parts.values():
            twin = part.python_gpu()
            gpu.static.update(twin.static)
        for name in NVAPI_NAMES:
            setattr(gpu.nvapi, name, self.base.names.get(name))
        gpu._capture_current_limit_transport = Mock(return_value=(list(self.header), dict(cl.FIELDS)))
        gpu._legacy_clk_escape = self.driver
        gpu._current_limit_transport = (list(self.header), dict(cl.FIELDS))
        gpu._vf_lock_recovery = None
        gpu._legacy_p0_owned = False
        gpu.msvdd_write_enabled = False
        gpu.voltage_xoc_enabled = False
        return gpu

    def native_gpu(self):
        gpu = self.base.native_gpu()
        gpu._current_limit_transport = (list(self.header), dict(cl.FIELDS))
        return gpu


class ResetParity(unittest.TestCase):
    def tearDown(self):
        native.set_export_resolver(None)

    def both(self, rig, setup=None, *, statics=STATICS, action=steps_of, compare=True):
        """setup(gpu) then action(gpu) on the twin, then on the core, each from the same fake state;
        only what the action did is in the compared logs."""
        runs = []
        for kind in ("python", "native"):
            rig.reset()
            gpu = rig.python_gpu() if kind == "python" else rig.native_gpu()
            try:
                if kind == "native":
                    gpu.capabilities()
                    gpu.static.get("pl_min_mw")  # merge the capability keys, then take the case's
                gpu.static.update(statics)
                if setup:
                    setup(gpu)
                rig.clear_logs()
                result = attempt(lambda: action(gpu))
                runs.append({"result": result, "logs": rig.logs(), "state": rig.state(),
                             "pins": dict(gpu._power_policy_pins or {}), "lock": gpu.last_clock_lock_request(),
                             # what the reset remembers: the board policy read_power_policies stores,
                             # the static pl_* keys, the current-limit read error, the live observation
                             "board": getattr(gpu, "_board_policy", None),
                             "static": {key: dict.get(gpu.static, key) for key in STATICS},
                             "read error": getattr(gpu, "_current_limit_read_error", None),
                             "live": getattr(gpu, "_live_power_last", None)})
            finally:
                if kind == "native":
                    gpu.close()
        if compare:
            self.assertEqual(runs[0]["result"], runs[1]["result"], "steps")
            for key in runs[0]:
                self.assertEqual(runs[0][key], runs[1][key], key)
        return runs[0]

    def steps(self, run):
        self.assertEqual(run["result"][0], "ok", run["result"])
        return run["result"][1]

    def named(self, run):
        return {name: (ok, message) for name, ok, message in self.steps(run)}


def set_policy(policy, value):
    def setup(gpu):
        ok, message = gpu.set_power_policy(policy, value)
        assert ok, message
    return setup


def chain(*setups):
    def setup(gpu):
        for step in setups:
            step(gpu)
    return setup


def point_lock(rig):
    """The point lock held on domain 0 (the V/F point lock table's first entry)."""
    original = rig.parts["lock"].reset

    def held():
        original()
        rig.parts["lock"].table[0] = entry(0, POINT, 900000)
    rig.parts["lock"].reset = held


# ---- tests/test_pascal_reset.py ------------------------------------------------------------------ #

class PascalStockResetTests(ResetParity):
    def pascal(self, status=3, arch=PASCAL, **kw):
        rig = Card(arch=arch, clock={"reset_status": status, **kw.pop("clock", {})}, **kw)
        point_lock(rig)
        return rig

    def test_only_reset_all_treats_confirmed_pascal_not_supported_as_noop(self):
        rig = self.pascal()
        run = self.both(rig, action=lambda g: (g.reset_gpu_clocks(), steps_of(g)))
        direct, steps = run["result"][1]
        self.assertEqual(direct, (False, "reset failed: Not Supported"))
        named = {name: (ok, message) for name, ok, message in steps}
        self.assertEqual(named[GPU.LOCK_STEP], (True, "NVML frequency lock is not applicable to Pascal; nothing to reset"))
        self.assertTrue(named[GPU.VF_LOCK_STEP][0], named)
        self.assertEqual([c[0] for c in run["logs"]["lock"]].count("VfLockSet"), 1)
        offsets = [c for c in run["logs"]["clock"] if c[0] == "SetClockOffsets"]
        self.assertEqual([(c[2], c[4]) for c in offsets], [(0, 0), (2, 0)])

    def test_other_errors_and_other_or_unknown_architectures_remain_failures(self):
        for status, arch in ((4, PASCAL), (15, PASCAL), (999, PASCAL), (3, TURING), (3, 0)):
            with self.subTest(status=status, arch=arch):
                run = self.both(self.pascal(status, arch))
                self.assertFalse(self.named(run)[GPU.LOCK_STEP][0])
                self.assertEqual([c[0] for c in run["logs"]["lock"]].count("VfLockSet"), 1)

    def test_missing_export_is_not_treated_as_confirmed_unsupported(self):
        run = self.both(self.pascal(clock={"with_lock": False}))
        self.assertEqual(self.named(run)[GPU.LOCK_STEP], (False, "ResetGpuLockedClocks not available"))

    def test_successful_release_keeps_normal_result(self):
        rig = self.pascal(0, TURING)
        run = self.both(rig, lambda g: g.lock_gpu_clocks(990, 1500))
        self.assertEqual(self.named(run)[GPU.LOCK_STEP], (True, "GPU clock lock released"))
        self.assertIsNone(run["lock"])  # the remembered request went with the release

    def test_point_unlock_failure_is_preserved_alongside_pascal_noop(self):
        run = self.both(self.pascal(lock={"set_status": -1}))
        named = self.named(run)
        self.assertTrue(named[GPU.LOCK_STEP][0])
        self.assertFalse(named[GPU.VF_LOCK_STEP][0])


# ---- tests/test_reset_readback.py ResetReadbackTests --------------------------------------------- #

class ResetReadbackTests(ResetParity):
    def test_unreadable_offsets_report_failure_and_can_be_retried(self):
        rig = Card(clkdom={"preload": {2: {"freq": 25000}, 0: {"nvvdd": 12500}}, "fail_status": -5})
        part = rig.parts["clkdom"]
        original = part.reset

        def healthy():
            original()
            part.fail_after = None
        part.reset = healthy

        def discover(gpu):  # the layout validated; every later GET fails until the fault clears
            gpu.clkdom_layout()
            gpu.clkdom_domains()
            part.fail_after = part.successes

        def retried(gpu):
            first = steps_of(gpu)
            part.fail_after = None
            return first, steps_of(gpu)
        run = self.both(rig, discover, action=retried)
        first, second = ({name: (ok, message) for name, ok, message in steps} for steps in run["result"][1])
        self.assertEqual(first["clock-domain offsets"],
                         (False, "clock-domain offsets were not reset: clock-domain read failed (status -5)"))
        self.assertEqual(first["core rail offset"], (False, "core rail offset was not reset: current value unreadable"))
        self.assertNotIn("MEM offset", first)
        # the same session's next reset reads them and writes both back to zero
        self.assertNotIn("clock-domain offsets", second)
        self.assertTrue(second["MEM offset"][0], second)
        self.assertTrue(second["core rail offset"][0], second)
        self.assertEqual((run["state"]["clkdom"][2]["freq"], run["state"]["clkdom"][0]["nvvdd"]), (0, 0))
        sets_ = [c for c in run["logs"]["clkdom"] if c[0] == "ClkDomCtlSet"]
        self.assertEqual(len(sets_), 2)  # both from the second reset

    def test_partial_or_empty_clock_read_cannot_be_treated_as_stock(self):
        # the read after discovery is rejected (a wrong version or mask echo, a failed GET): no write
        for options in ({"echo_from": 0, "echo_version": 0x1234}, {"echo_from": 0, "echo_mask": False},
                        {"fail_after": 0}):
            with self.subTest(options=options):
                rig = Card(clkdom={"preload": {2: {"freq": 25000}}})

                def discover(gpu, options=options):
                    gpu.clkdom_layout()
                    gpu.clkdom_domains()
                    part = rig.parts["clkdom"]
                    for key, value in options.items():
                        setattr(part, key, part.successes + value if key in ("echo_from", "fail_after") else value)
                run = self.both(rig, discover)
                self.assertFalse(self.named(run)["clock-domain offsets"][0])
                self.assertEqual([c for c in run["logs"]["clkdom"] if c[0] == "ClkDomCtlSet"], [])
                self.assertEqual(run["state"]["clkdom"][2]["freq"], 25000)

    def test_confirmed_zero_offsets_require_no_write_or_failure(self):
        run = self.both(Card())
        named = self.named(run)
        self.assertNotIn("clock-domain offsets", named)
        self.assertNotIn("core rail offset", named)
        self.assertFalse([n for n in named if n.endswith(" offset") and n not in ("core offset", "mem offset")])
        self.assertEqual([c for c in run["logs"]["clkdom"] if c[0] == "ClkDomCtlSet"], [])

    def test_unvalidated_private_layout_is_not_a_failed_supported_control(self):
        for clkdom in ({"with_get": False}, {"get_status": -5}):
            with self.subTest(clkdom=clkdom):
                run = self.both(Card(clkdom=clkdom))
                named = self.named(run)
                self.assertNotIn("clock-domain offsets", named)
                self.assertNotIn("core rail offset", named)
                self.assertEqual([c for c in run["logs"]["clkdom"] if c[0] == "ClkDomCtlSet"], [])

    def test_unreadable_point_lock_is_not_skipped_by_reset(self):
        rig = Card()
        original = rig.parts["lock"].reset

        def unreadable():
            original()
            rig.parts["lock"].get_status = -1
        rig.parts["lock"].reset = unreadable
        run = self.both(rig)
        self.assertFalse(self.named(run)[GPU.VF_LOCK_STEP][0])
        self.assertEqual([c[0] for c in run["logs"]["lock"] if c[0] in ("BoostLock", "VfLockSet")], ["BoostLock"])


class RailLimitSide:
    """The rail-setter sides plus a reset_all operation (its trace carries the writer attributes,
    the fake's rail state and the hold record, as every setter operation's)."""

    def reset_all(self):
        if isinstance(self.gpu, NV.GPU):
            fill_nvapi(self.gpu)
        return self._op(("reset_all",), lambda: attempt(lambda: steps_of(self.gpu)))

    def limits(self):
        read = self.gpu.read_volt_rail_limits if isinstance(self.gpu, NV.GPU) else (
            lambda: nr.read_volt_rail_limits(self.gpu))
        return self._record(("limits",), read)


class RailPythonSide(RailLimitSide, SetterPythonSide):
    pass


class RailNativeSide(RailLimitSide, SetterNativeSide):
    pass


class RailRig(SetterRig):
    python_side = RailPythonSide
    native_side = RailNativeSide


class RailLimitTests(unittest.TestCase):
    """The rail-limit step on the rail-setter rig: the getters, the boost and the hooked rail SET
    escapes, the writer's attributes, the fake's rail state and the hold record, on both sides."""

    def run_both(self, scenario, **options):
        rig = RailRig(**options)
        self.addCleanup(rig.close)
        python, core = rig.run(scenario)
        self.assertEqual(len(python), len(core))
        for step, (p, c) in enumerate(zip(python, core, strict=True)):
            with self.subTest(step=step, operation=p[0]):
                self.assertEqual(p, c)
        return python

    @staticmethod
    def rail_steps(entry_):
        status, steps = entry_[1]
        assert status == "ok", entry_[1]
        return [s for s in steps if s[0] == "rail limits"]

    def test_unknown_rail_profile_never_reads_or_dispatches_another_cards_reset(self):
        for arch in (2, 5):  # Kepler, Volta: no rail profile for these architectures
            with self.subTest(arch=arch):
                def scenario(side, fake, clock):
                    side.seed()
                    side.reset_all()
                trace = self.run_both(scenario, **dict(config("turing", architecture=arch), bases=BASES["turing"]))
                self.assertEqual(self.rail_steps(trace[0]), [])
                self.assertEqual([c for c in trace[0][2] if c[0] in ("control", "live", "control-write")], [])

    def test_known_rail_profile_with_missing_or_incomplete_read_is_a_failure(self):
        def missing(side, fake, clock):
            side.seed()
            side.limits()          # the first read: the session's restore target
            fake.accept["control"] = set()  # the control getter now refuses every version
            side.reset_all()

        def incomplete(side, fake, clock):
            side.seed()
            side.set(0, vmin=700)  # rail 0 off its first read, then rail 1 stops answering
            side.limits()
            fake.control.pop(1)
            side.reset_all()
        for name, scenario in (("missing", missing), ("incomplete", incomplete)):
            with self.subTest(name):
                trace = self.run_both(scenario, **blackwell())
                self.assertEqual(self.rail_steps(trace[-1]), [RAIL_UNREADABLE])
                self.assertEqual(sets(trace[-1]), [])  # rail 0 is off stock, yet nothing is dispatched

    def test_exact_stock_rails_skip_write_and_changed_rails_dispatch_reset(self):
        def scenario(side, fake, clock):
            side.seed()
            side.limits()
            side.reset_all()       # exactly the first read: no step, no write
            side.set(0, vmin=700)
            side.reset_all()       # off it: the session's first-read values are written back
            side.reset_all()

        trace = self.run_both(scenario, **turing())
        self.assertEqual(self.rail_steps(trace[1]), [])
        self.assertEqual(sets(trace[1]), [])
        self.assertEqual(self.rail_steps(trace[3]),
                         [("rail limits", True, "rail limits restored to first-read values (not factory defaults)")])
        self.assertEqual(len(sets(trace[3])), 1)
        self.assertEqual(self.rail_steps(trace[4]), [])

    def test_a_hold_record_is_handled_by_the_rail_reset(self):
        hold = {"rail": 0, "written_uv": {"vmin": 50000}, "prior_uv": {"vmin": 0}}

        def scenario(side, fake, clock):
            side.seed(hold=hold)
            side.set(0, vmin=700)
            side.reset_all()

        trace = self.run_both(scenario, **turing())
        self.assertEqual(len(self.rail_steps(trace[1])), 1)


# ---- the reset_all cases of tests/test_power_policies.py and tests/test_current_limits.py ------- #

class PolicyResetTests(ResetParity):
    def test_reset_all_drops_the_values_and_returns_every_policy_to_default(self):
        setup = chain(set_policy(4, 150000), set_policy(14, 4000000), lambda g: g.set_power_limit_mw(300000))
        run = self.both(Card(), setup)
        steps = self.steps(run)
        self.assertEqual(run["pins"], {})
        self.assertTrue(all(ok for name, ok, _ in steps if name != "rail limits"), steps)
        self.assertIn(("power limit", True, "power limit configured to 260 W"), steps)
        # policy 4 follows the board limit back to its default; 14 is not coupled and is written
        self.assertNotIn("power policy 4", self.named(run))
        self.assertIn("power policy 14", self.named(run))
        card = PolicyCard()
        self.assertEqual(run["state"]["policies"], card.request)  # every policy back at its default

    def test_reset_all_runs_every_step_when_a_policy_write_read_fails(self):
        rig = Card()
        probe = self.both(rig, set_policy(14, 4000000))
        # the escape just before the reset's policy SET: the write's own table read
        write = next(k for k, e in enumerate(probe["logs"]["escapes"]) if e[1][14] == 0x2080E61B)

        def armed(gpu):
            set_policy(14, 4000000)(gpu)
            rig.refuse({write - 1})
        run = self.both(rig, armed)
        steps = self.steps(run)
        self.assertEqual([s[0] for s in steps][-4:], [GPU.LOCK_STEP, GPU.VF_LOCK_STEP, "fan", "voltage boost"][:0] +
                         [GPU.VF_LOCK_STEP, "fan", "voltage boost", "vf curve"])  # the steps after it still ran
        failed = [s for s in steps if s[0] == "power policy 14"]
        self.assertEqual(len(failed), 1)
        self.assertFalse(failed[0][1])
        self.assertIn("could not be read", failed[0][2])

    def test_current_limit_reset_restores_reported_defaults_only_when_changed(self):
        def changed(gpu):
            ok, message = gpu.set_current_limit_ma(13, 360000)
            assert ok, message
        rig = Card()
        run = self.both(rig, changed)
        named = self.named(run)
        rows = [n for n in named if n not in ("core offset", "mem offset", "rail limits", "power limit", "fan",
                                               GPU.LOCK_STEP, GPU.VF_LOCK_STEP, "voltage boost", "vf curve")]
        self.assertEqual(len(rows), 1, named)
        self.assertTrue(named[rows[0]][0], named)
        self.assertEqual(rows[0], rows[0].lower())
        self.assertEqual(run["state"]["policies"][13], PolicyCard().request[13])

        # nothing changed: no current-limit write at all
        run = self.both(rig, action=lambda g: (steps_of(g), steps_of(g)))
        first, second = run["result"][1]
        self.assertEqual(first, second)
        self.assertEqual(run["state"]["policy sets"], [])

        # an unreadable table is a failed step (the Python test's escape raises; here the driver
        # refuses every request, as natively a driver call cannot raise)
        original = rig.reset

        def broken():
            original()
            rig.refuse()
        rig.reset = broken
        run = self.both(rig)
        self.assertTrue(any(not ok and message.startswith("cannot read current limits")
                            for _, ok, message in self.steps(run)))
        self.assertNotIn("power policies", self.named(run))


# ---- the rest of the brief's cases ---------------------------------------------------------------- #

class OrderAndUnionTests(ResetParity):
    def test_step_order_over_a_full_card(self):
        rig = Card(clkdom={"preload": {2: {"freq": 25000}, 0: {"nvvdd": 12500, "msvdd": 20000}}})
        point_lock(rig)
        original = rig.parts["clock"].reset

        def offsets():
            original()
            rig.parts["clock"].offsets[0]["off"] = 60
            rig.parts["clock"].offsets[2]["off"] = 800
            rig.parts["clock"].deltas[5] = 15000

        rig.parts["clock"].reset = offsets
        original_lock = rig.parts["lock"].reset

        def boosted():
            original_lock()
            rig.parts["lock"].vboost["percent"] = 50
        rig.parts["lock"].reset = boosted
        original_fan = rig.parts["fan"].reset

        def manual():
            original_fan()
            rig.parts["fan"].client[5]["mode"] = 1
        rig.parts["fan"].reset = manual

        def setup(gpu):
            set_policy(14, 4000000)(gpu)
            ok, message = gpu.set_current_limit_ma(13, 360000)
            assert ok, message
            gpu.set_power_limit_mw(300000)
            gpu.lock_gpu_clocks(990, 1500)
        run = self.both(rig, setup)
        names = [n for n, _, _ in self.steps(run)]
        current = [n for n in names if n not in ("core offset", "mem offset", "MEM offset", "core rail offset",
                                                  "MSVDD request 0", "rail limits", "power limit",
                                                  "power policy 14", GPU.LOCK_STEP, GPU.VF_LOCK_STEP, "fan",
                                                  "voltage boost", "vf curve")]
        self.assertEqual(len(current), 1, names)
        self.assertEqual(names, ["core offset", "mem offset", "MEM offset", "core rail offset", "MSVDD request 0",
                                 "rail limits", current[0], "power limit", "power policy 14", GPU.LOCK_STEP,
                                 GPU.VF_LOCK_STEP, "fan", "voltage boost", "vf curve"])
        self.assertTrue(all(ok for name, ok, _ in self.steps(run) if name != "rail limits"), self.steps(run))
        self.assertIsNone(run["lock"])
        self.assertEqual(run["pins"], {})

    def test_msvdd_domain_union_in_sorted_order(self):
        # written: 3 (the opt-in on); seen: 5 by an explicit read, 9 read and then refused; the
        # candidate: control 0. Each is read once; a zero needs nothing, an unreadable one fails.
        rig = Card(clkdom={"preload": {0: {"msvdd": 0}, 5: {"msvdd": 20000}, 9: {"msvdd": 30000}}})

        def setup(gpu):
            gpu.msvdd_write_enabled = gpu.voltage_xoc_enabled = True
            ok, message = gpu.set_rail_offset_mv(10, 3, rail=1)
            assert ok, message
            gpu.msvdd_write_enabled = gpu.voltage_xoc_enabled = False
            for domain in (9, 5):
                assert gpu.rail_offset_capability(1, domain)["available"]
            rig.parts["clkdom"].accepted.remove(9)  # its GET now fails
        run = self.both(rig, setup)
        msvdd = [s for s in self.steps(run) if s[0].startswith("MSVDD")]
        self.assertEqual([s[0] for s in msvdd], ["MSVDD request 3", "MSVDD request 5", "MSVDD request 9"])
        self.assertTrue(msvdd[0][1] and msvdd[1][1])
        self.assertEqual(msvdd[2][1:], (False, "stored request could not be read"))
        state = run["state"]["clkdom"]
        self.assertEqual((state[3]["msvdd"], state[5]["msvdd"], state[9]["msvdd"]), (0, 0, 30000))

    def test_power_limit_default_unknown(self):
        for statics in ({"pl_def_mw": None}, {"pl_def_mw": 0}, {}):
            with self.subTest(statics=statics):
                full = dict(STATICS, **statics)
                if not statics:
                    full.pop("pl_def_mw")

                def drop(gpu, statics=statics):
                    if not statics:
                        gpu.static.pop("pl_def_mw", None)
                run = self.both(Card(), drop, statics=full)
                self.assertIn(NO_DEFAULT, self.steps(run))
                self.assertNotIn(("SetPowerManagementLimit",), [c[:1] for c in run["logs"]["nvml"]])

    def test_pins_cleared_first_even_when_a_later_step_fails(self):
        rig = Card()

        def setup(gpu):
            set_policy(4, 150000)(gpu)
            rig.refuse()  # every escape of the reset fails
        run = self.both(rig, setup)
        self.assertEqual(run["pins"], {})
        named = self.named(run)
        self.assertFalse(named["current limits"][0])
        # both reads failed: now the policy read's failure is a step
        self.assertEqual(named["power policies"],
                         (False, "power policies were not reset: policy request failed (NTSTATUS 0, RM 0x40)"))
        self.assertIn("fan", named)

    def test_a_policy_read_failure_is_a_step_only_when_the_current_read_failed(self):
        # read_power_policies re-reads through get_current_limits, so a failed table read sets the
        # read error whenever a named current policy was established. On a table with no named
        # current row, a GET refused for its size (CurrentLimitUnsupported) leaves the read error
        # empty: read_power_policies' own read then fails too, and Python adds no step for it (its
        # gate is the current-limit read error) and writes no policy.
        table = [row for row in TITAN if row[0] != 13]
        coupled = tuple(p for p in COUPLED if p != 13)
        rig = Card(table=(table, coupled))

        def armed(gpu):
            set_policy(14, 4000000)(gpu)
            rig.refuse(rm=NV.RM_CURRENT_POLICY_SIZE_REJECTED)  # every GET of the reset
        run = self.both(rig, armed)
        named = self.named(run)
        self.assertNotIn("current limits", named)
        self.assertNotIn("power policies", named)
        self.assertNotIn("power policy 14", named)
        self.assertIn("fan", named)
        self.assertTrue(run["logs"]["escapes"])  # the reads were attempted


class LegacyP0Tests(P0Parity):
    """tests/test_legacy_p0.py's reset case: the P0 hold is released only when this session owns
    it, a failed release keeps the ownership, and the step sits between the clock lock and the fan."""

    @staticmethod
    def scenario(side):
        gpu = side.gpu
        if isinstance(gpu, NV.GPU):
            fill_nvapi(gpu)
        else:
            gpu.capabilities()
            gpu.static.get("pl_min_mw")
        gpu.static.update(pl_min_mw=None, pl_def_mw=None, pl_max_mw=None)
        return steps_of(gpu)

    def test_reset_releases_only_owned_p0_and_preserves_failure(self):
        for arch in (KEPLER, MAXWELL):
            for owned, rc in ((True, 0), (True, -1), (False, 0)):
                with self.subTest(arch=arch, owned=owned, rc=rc):
                    run = self.both(P0Rig(arch=arch), self.scenario, owned=owned, force=(rc,))
                    status, steps = run["result"]
                    self.assertEqual(status, "ok")
                    named = {name: (ok, message) for name, ok, message in steps}
                    self.assertEqual(GPU.P0_LOCK_STEP in named, owned)
                    if owned:
                        self.assertEqual(named[GPU.P0_LOCK_STEP][0], rc == 0)
                        names = [name for name, _, _ in steps]
                        self.assertEqual(names[names.index(GPU.LOCK_STEP) + 1], GPU.P0_LOCK_STEP)
                        self.assertEqual(names[names.index(GPU.P0_LOCK_STEP) + 1], "fan")
                    self.assertEqual(run["owned"], (owned, owned and rc != 0))
                    self.assertNotIn("vf curve", named)  # Kepler: the curve does not apply


# ---- the review's cases ----------------------------------------------------------------------------- #

class ReviewedGateTests(ResetParity):
    def test_a_zero_mask_read_earlier_keeps_the_policy_block_after_a_failed_read(self):
        # _power_policy_mask stays not None for the session once a mask was read, even 0: a later
        # failed read that clears the cached table must not hide the policy block's failure step
        rig = Card(table=([],))
        original = rig.reset

        def stocked():
            original()
            rig.card.request.setdefault(BOARD, 260000)  # NVML's limit; the empty table has no row for it
            rig.card.derive = lambda: None  # and no coupled policy to follow it
        rig.reset = stocked

        def setup(gpu):
            assert gpu.get_current_limits() == []
            rig.refuse()
            rows, error = gpu.read_power_policies()
            assert rows == [] and error, (rows, error)
        run = self.both(rig, setup)
        self.assertEqual(self.named(run)["power policies"],
                         (False, "power policies were not reset: policy request failed (NTSTATUS 0, RM 0x40)"))

    def test_kepler_with_the_boost_table_setter_gets_no_curve_reset(self):
        rig = Card(arch=2)
        self.assertIsNotNone(rig.base.names.get("BoostTableSet"))  # the setter is exported
        run = self.both(rig)
        self.assertNotIn("vf curve", self.named(run))
        self.assertEqual([c for c in run["logs"]["clock"] if c[0] in ("BoostTableSet", "BoostTableGet")], [])

    def test_a_current_row_whose_request_alone_is_off_default_is_selected(self):
        rig = Card()
        default = PolicyCard().spec[13]["default"]

        def setup(gpu):
            card = rig.card
            base = PolicyCard.effective.__get__(card)
            card.effective = lambda p: default if p == 13 else base(p)  # the limit reads as its default
            card.request[13] = 360000  # the request alone is off it
        run = self.both(rig, setup)
        rows = [s for s in self.steps(run) if s[0] not in ("core offset", "mem offset", "rail limits", "power limit",
                                                         "fan", GPU.LOCK_STEP, GPU.VF_LOCK_STEP, "voltage boost",
                                                         "vf curve")]
        # selected by its request alone; the write's own check then refuses the same disagreement
        self.assertEqual(rows, [("core current", False,
                                 "selected policy's stored and effective current limits disagree; nothing written")])
        self.assertEqual([s for s in run["state"]["policy sets"] if s[0] == 13], [])

    def test_a_policy_whose_default_is_outside_its_range_is_not_written(self):
        # writable and off its default, but the default lies above its maximum or below its minimum
        for side, row in (("above", (14, 0x0B, 11, 0, 1, 6000000, 5001000)),
                          ("below", (14, 0x0B, 11, 0, 3000000, 2000000, 5001000))):
            with self.subTest(default=side):
                table = [row if r[0] == 14 else r for r in TITAN]
                rig = Card(table=(table,))

                def setup(gpu, rig=rig):
                    rig.card.request[14] = 4000000
                run = self.both(rig, setup)
                self.assertNotIn("power policy 14", self.named(run))
                self.assertEqual([s for s in run["state"]["policy sets"] if s[0] == 14], [])
                self.assertEqual(run["state"]["policies"][14], 4000000)

    def test_a_negative_msvdd_request_is_reset(self):
        rig = Card(clkdom={"preload": {0: {"msvdd": -20000}}})
        run = self.both(rig)
        msvdd = [s for s in self.steps(run) if s[0].startswith("MSVDD")]
        self.assertEqual([s[:2] for s in msvdd], [("MSVDD request 0", True)])
        self.assertEqual(run["state"]["clkdom"][0]["msvdd"], 0)

    def test_a_message_past_the_inline_room_is_fetched_whole_under_the_lock(self):
        # the binding's inline room lowered (the struct keeps its 4096 bytes): every message of 16
        # bytes or more goes through dc_gpu_reset_message_get, which must return it whole
        real = native.load()
        fetched = []

        class Library:
            def __getattr__(self, name):
                return getattr(real, name)

        def action(gpu):
            if isinstance(gpu, NV.GPU):
                return steps_of(gpu)
            library = Library()

            def get(handle, index, out, capacity, needed):
                fetched.append((index, gpu._lock._is_owned()))
                return real.dc_gpu_reset_message_get(handle, index, out, capacity, needed)
            library.dc_gpu_reset_message_get = get
            with patch.object(native, "RESET_MESSAGE_MAX", 16), patch.object(native, "load", return_value=library):
                return steps_of(gpu)
        run = self.both(Card(), set_policy(14, 4000000), action=action)
        steps = self.steps(run)
        long_ = [k for k, (_, _, message) in enumerate(steps) if len(message.encode()) >= 16]
        self.assertTrue(long_)
        self.assertEqual(fetched, [(k, True) for k in long_])


class P0AgainstVfLockTests(P0Parity):
    def test_the_p0_step_sits_between_the_clock_lock_and_the_vf_point_lock(self):
        rig = P0Rig(arch=MAXWELL)
        lock = LockRig(arch=MAXWELL)  # its V/F point lock pair served on the P0 card
        rig._lock_rig = lock
        for name in ("BoostLock", "VfLockSet"):
            fn = lock.names[name]
            rig.names[name] = fn
            for ident in [k for k, v in lock.nvapi.interfaces.items() if v is fn]:
                rig.nvapi.interfaces[ident] = fn
        twin = rig.twin

        def served(opened):
            gpu = twin(opened)
            gpu.nvapi.BoostLock, gpu.nvapi.VfLockSet = rig.names["BoostLock"], rig.names["VfLockSet"]
            return gpu
        rig.twin = served
        run = self.both(rig, LegacyP0Tests.scenario, owned=True)
        status, steps = run["result"]
        self.assertEqual(status, "ok", steps)
        names = [name for name, _, _ in steps]
        at = names.index(GPU.LOCK_STEP)
        self.assertEqual(names[at:at + 4], [GPU.LOCK_STEP, GPU.P0_LOCK_STEP, GPU.VF_LOCK_STEP, "fan"])
        self.assertEqual(run["owned"], (True, False))


class BackfillTests(pl.LiveCase):
    def test_the_power_limit_steps_live_read_back_fills_the_missing_static_keys(self):
        static = {"pl_min_mw": None, "pl_def_mw": 321000, "pl_max_mw": None}

        def action(side, _power):
            if isinstance(side.gpu, NV.GPU):
                fill_nvapi(side.gpu)
            return attempt(lambda: steps_of(side.gpu))
        rig = pl.live_rig(configure=pl.limits(180123, 321000, 680456))
        result, *_, state = self.run_both(rig, action, static=static)
        self.assertEqual(result[0], "ok", result)
        self.assertEqual(state["static"], {"pl_min_mw": 180123, "pl_def_mw": 321000, "pl_max_mw": 680456})


# ---- what can raise --------------------------------------------------------------------------------- #

class RaisedCalleeTests(ResetParity):
    def test_a_static_that_raises_stops_the_reset_after_the_steps_before_it(self):
        class Broken(dict):
            def get(self, key, default=None):
                if key == "pl_def_mw":
                    raise KeyError("static unavailable")
                return super().get(key, default)

        rig = Card(clkdom={"preload": {2: {"freq": 25000}}})

        def setup(gpu):
            set_policy(4, 150000)(gpu)
            gpu.static = Broken(gpu.static)  # both sides keep static as an instance attribute
        run = self.both(rig, setup)
        self.assertEqual(run["result"], ("raise", "KeyError", "'static unavailable'"))
        self.assertEqual(run["pins"], {})
        # everything before the power limit was written: the offsets and the clock-domain offset
        self.assertEqual(run["state"]["clkdom"][2]["freq"], 0)
        self.assertEqual(run["logs"]["fan"], [])  # nothing after it ran

    def test_short_buffer_changes_nothing(self):
        rig = Card()
        rig.reset()
        gpu = rig.native_gpu()
        try:
            set_policy(4, 150000)(gpu)
            library = native.load()
            steps = (native._ResetStep * 1)()
            steps[0].size = ctypes.sizeof(native._ResetStep)
            count = ctypes.c_uint32()
            calls = len(rig.driver.log)
            status = library.dc_gpu_reset_all(gpu._handle, gpu._confirm_derived_clamp(), None, gpu._policy_inputs([]), None,
                                              native._reset_statics(gpu, []), None, 0, steps, 1,
                                              ctypes.byref(count), None, None)
            self.assertEqual((status, count.value), (native.DC_E_BUFFER, native.RESET_STEPS_MAX))
            self.assertEqual(len(rig.driver.log), calls)
            self.assertEqual(gpu._power_policy_pins, {4: 150000})
            self.assertEqual(library.dc_gpu_reset_message_get(gpu._handle, 0, None, 0, ctypes.byref(ctypes.c_size_t())),
                             native.DC_E_RANGE)
        finally:
            gpu.close()


if __name__ == "__main__":
    unittest.main()
