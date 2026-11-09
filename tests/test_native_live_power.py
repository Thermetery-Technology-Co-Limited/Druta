# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native live power request, increment A6: nvbackend.GPU.read_live_power_request and its
bookkeeping (_live_power_last, _live_power_error, _live_power_error_kind, _board_policy, the static
pl_* back-fill and the release in refresh_capabilities) against the real Python method.

native.Gpu.read_live_power_request runs dc_cpc_live_read; its four attributes are properties over
dc_cpc_live_state_get (and dc_cpc_board_policy_set). The Python original is the real
GPU.read_live_power_request (with _get_live_power_controller and refresh_capabilities) on the
nvbackend.GPU twin of the metadata rig (tests/test_native_client_power_metadata.py: MetaRig, one of
the Python tests' own wire models as the responder of the fake gdi32 escape, each side with a fresh
fake). The cases port the read cases of tests/test_live_power_backend.py.

Every case runs one action on both sides and compares what it returns (the (observation, error)
pairs with key order, and after each step the four attributes and static's pl_min_mw / pl_def_mw /
pl_max_mw), the gdi call log, the live controller's state (the twin's _live_power_controller, or a
fresh controller's state while it has none), the transport cache and the fake's own command log.

The twin's static starts as nvbackend.GPU.__init__ would leave it on the same NVML: the Card's NVML
exports no power-limit getters unless a case adds them, and the twin's static then holds the same
values. On the core the store is the handle's capability snapshot (dc_gpu_capabilities_get).

Not reachable natively or through these fakes, and so covered only by tests/test_live_power_backend.py:
  - the sticky "GPU binding changed" refusal (the fake identity cannot change, as in
    tests/test_native_target_binding.py); a failed read after a success is shown with an
    unsupported RM status instead;
  - a failure that is not a ClientPowerError (Python records it as transient): through read() the
    core's only one is the bare ValueError of dc_cpc_exchange, which a read cannot reach, and a C++
    exception (allocation);
  - power_limit_bounds_mw, read_power_limit_mw and set_power_limit_mw, the readers of this
    bookkeeping, which are not ported yet.
"""
import ctypes
import unittest

from druta import client_power_runtime as cp
from druta import native
from tests.test_client_power_runtime import FakeGpu
from tests.test_client_power_turing import TuringGpu
from tests.test_native_client_power_controller import PythonSide, outcome
from tests.test_native_client_power_metadata import (
    A612_KEY,
    MODERN,
    TURING_KEY,
    MetadataCase,
    MetaNative,
    MetaRig,
    commands,
    ordered,
    state,
    payload,
    store,
)
from tests.test_native_client_power_read import NP, byte, word
from tests.test_native_identity import write_u32
from tests.test_native_telemetry import u32_callback
from tests.test_native_transport import install

if not native.available():
    raise unittest.SkipTest("native core not built (native/build.sh linux)")

PL = ("pl_min_mw", "pl_def_mw", "pl_max_mw")
LIMITS = (200000, 321000, 550000)  # FakeGpu's client row: minimum, rated, maximum
TURING_LIMITS = (180000, 321000, 550000)  # TuringGpu's
CONSTRAINTS = "nvmlDeviceGetPowerManagementLimitConstraints"
DEFAULT = "nvmlDeviceGetPowerManagementDefaultLimit"
MODERN_INFO = (cp.INFO, cp.SIZES[cp.INFO])
LEGACY_INFO = (cp.INFO, cp.LEGACY_SIZES[cp.INFO])
TRANSIENT_RM = 0x99  # not a size refusal nor 0x56 / 0x40: a transient rejection
UNSUPPORTED_RM = 0x56


def constraints(minimum, maximum, status=0):
    @ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
    def getter(device, low, high):
        write_u32(low, minimum)
        write_u32(high, maximum)
        return status
    return getter


class LivePython(PythonSide):
    """The twin side: its controller is the twin's own _live_power_controller."""

    def __init__(self, twin):  # noqa: D107 - PythonSide's would build a second controller
        self.gpu = twin

    @property
    def controller(self):
        live = getattr(self.gpu, "_live_power_controller", None)
        return live if live is not None else cp.ClientPowerController(self.gpu)


def bookkeeping(gpu):
    """The attributes nvbackend reads with getattr, and the static range keys."""
    return {"last": getattr(gpu, "_live_power_last", None), "error": getattr(gpu, "_live_power_error", None),
            "kind": getattr(gpu, "_live_power_error_kind", None), "board": getattr(gpu, "_board_policy", None),
            "static": {key: gpu.static.get(key) for key in PL}}


def live(side):
    """One read_live_power_request on the side and the bookkeeping after it."""
    return outcome(side.gpu.read_live_power_request), bookkeeping(side.gpu)


def reads(count, *, rig, before=None):
    def action(side):
        store(side, rig)
        steps = [bookkeeping(side.gpu)]
        for k in range(count):
            if before:
                before(side, k)
            steps.append(live(side))
        return steps
    return action


def faults(*events):
    def configure(fake):
        for key, event in events:
            fake.faults[key] = event
    return configure


class LiveCase(MetadataCase):
    def run_both(self, rig, action, *, nvml=None, static=None):
        """action(side) on the twin, then on the core, each with a fresh fake. nvml: exports added
        to the Card's NVML (both sides); static: the twin's static (what GPU.__init__ would read
        from those exports). Returns the Python run: (result, gdi calls, state, cache, fake calls)."""
        rig.card.nvml.exports.update(nvml or {})
        runs = []
        for kind in ("python", "native"):
            rig.fresh()
            rig.calls = []
            install(rig.card.nvml, rig.card.nvapi, rig.gdi())
            opened = native.Gpu()
            try:
                if kind == "python":
                    twin = rig.card.python_twin(opened)
                    twin.static = dict(static or {})
                    if rig.recapture:
                        twin.nvapi.PowerPolInfo = rig.getter
                    side = LivePython(twin)
                    windll, winfunctype = rig.python_patches()
                    with windll, winfunctype:
                        result = action(side)
                else:
                    rig.probe = lambda: native.hook_test_state()["patched"]
                    rig.enter = native.hook_test_entry
                    side = MetaNative(opened)
                    result = action(side)
                    # one store: the binding's static reflects the core's capability snapshot
                    self.assertEqual({key: opened.capabilities().get(key) for key in PL},
                                     {key: opened.static.get(key) for key in PL})
                runs.append((result, list(rig.calls), side.state(), side.cache(), list(rig.fake.calls)))
            finally:
                opened.close()
            self.assertEqual(rig.errors, [], f"the {kind} run raised inside the fake driver")
        for name, python, core in zip(("result", "gdi calls", "state", "cache", "fake calls"), *runs, strict=True):
            self.assertEqual(python, core, f"Python and native {name} differ")
        self.assertEqual(ordered(runs[0][0]), ordered(runs[1][0]), "Python and native key orders differ")
        return runs[0]


EMPTY = {"last": None, "error": None, "kind": None, "board": None, "static": dict.fromkeys(PL)}
FILLED = dict(zip(PL, LIMITS, strict=True))
TURING_FILLED = dict(zip(PL, TURING_LIMITS, strict=True))


class StickyReadTests(LiveCase):
    def test_success_records_the_observation_board_policy_and_range(self):
        for name, factory, accepted, native_index, filled in (
                ("modern", FakeGpu, MODERN, 31, FILLED), ("turing", TuringGpu, None, 17, TURING_FILLED),
                ("alternate turing", lambda: TuringGpu(client_index=0, native_index=2, architecture=4), None, 2,
                 TURING_FILLED)):
            with self.subTest(name):
                rig = MetaRig(factory, accepted=accepted)
                result, _, final, *_ = self.run_both(rig, reads(1, rig=rig))
                self.assertEqual(result[0], EMPTY)
                (status, (observation, error)), after = result[1]
                self.assertEqual((status, error), ("ok", None))
                self.assertEqual(observation["binding"]["native_index"], native_index)
                self.assertEqual(list(observation)[-2:], ["arbitration", "arbitration_error"])
                self.assertEqual(after, {"last": observation, "error": None, "kind": None, "board": native_index,
                                         "static": filled})
                self.assertIsNotNone(final["transport_key"])

    def test_the_controller_is_sticky(self):
        # The second read reuses the group the first one chose: the modern INFO size is probed once.
        rig = MetaRig(TuringGpu)
        result, _, final, _, fake_calls = self.run_both(rig, reads(2, rig=rig))
        first, second = result[1][0][1][0], result[2][0][1][0]
        self.assertEqual(first, second)
        self.assertEqual(commands(fake_calls).count(MODERN_INFO), 1)
        self.assertEqual(commands(fake_calls).count(LEGACY_INFO), 4)
        self.assertEqual(final, state(sizes="TURING_SIZES", legacy=True, native_policy124=True,
                                      transport_key=TURING_KEY))

    def test_a_definite_failure_keeps_the_controller_and_drops_the_last_observation(self):
        # test_live_api_binds_alternate_adapter_and_reuses_sticky_controller, with an unsupported RM
        # status in place of the binding change the fakes cannot make
        rig = MetaRig(FakeGpu, configure=faults(((cp.INFO, 3), {"rm": UNSUPPORTED_RM})), accepted=MODERN)
        result, _, final, *_ = self.run_both(rig, reads(2, rig=rig))
        (_, (observation, _)), _ = result[1]
        (status, failed), after = result[2]
        message = f"Client-power command {cp.INFO:#x} rejected (RM {UNSUPPORTED_RM:#x})"
        self.assertEqual((status, failed), ("ok", (None, message)))
        self.assertEqual(after, {"last": None, "error": message, "kind": "unsupported",
                                 "board": observation["binding"]["native_index"], "static": FILLED})
        self.assertEqual(final, state(transport_key=A612_KEY))

    def test_an_unstable_or_malformed_failure_after_a_success_drops_the_last_observation(self):
        # the non-transient kinds other than unsupported, each after a successful read: the second
        # read's captures are INFO 3 / 4 and NATIVE_CONTROL 3 / 4. Neither kind is retried, and
        # each drops _live_power_last while _board_policy and the back-filled range stay.
        differ = "Client-power definitions or requests changed between fresh reads"
        for name, configure, kind, message in (
                ("captures differ", payload(cp.NATIVE_CONTROL, 4, byte(13875, 0)), "unstable", differ),
                ("requests disagree", payload(cp.NATIVE_CONTROL, 3, word(NP + 4, 321001)), "unstable",
                 "Client and native current requests disagree"),
                ("malformed", payload(cp.NATIVE_CONTROL, 3, word(12, 254)), "malformed",
                 "Native CONTROL header or inventory differs from INFO")):
            with self.subTest(name):
                rig = MetaRig(FakeGpu, configure=configure, accepted=MODERN)
                result, _, final, _, fake_calls = self.run_both(rig, reads(2, rig=rig))
                (status, (observation, error)), after = result[1]
                self.assertEqual((status, error), ("ok", None))
                self.assertEqual(after["last"], observation)
                (status, failed), after = result[2]
                self.assertEqual((status, failed), ("ok", (None, message)))
                self.assertEqual(after, {"last": None, "error": message, "kind": kind,
                                         "board": observation["binding"]["native_index"], "static": FILLED})
                self.assertEqual(final, state(transport_key=A612_KEY))
                # one STATUS, the first read's: the failed read never reaches the arbitration
                self.assertEqual(commands(fake_calls).count((cp.NATIVE_STATUS, cp.SIZES[cp.NATIVE_STATUS])), 1)
                self.assertEqual(commands(fake_calls).count(MODERN_INFO), 2 + (2 if name == "captures differ" else 1))

    def test_a_transient_failure_keeps_the_last_observation(self):
        # test_transient_live_read_returns_error_not_stale_observation_but_retains_range
        rig = MetaRig(FakeGpu, configure=faults(((cp.INFO, 3), {"rm": TRANSIENT_RM}),
                                                ((cp.INFO, 4), {"rm": TRANSIENT_RM})),
                      accepted=MODERN, recapture=True)
        result, *_ = self.run_both(rig, reads(2, rig=rig))
        (_, (observation, _)), _ = result[1]
        (status, failed), after = result[2]
        message = f"Client-power command {cp.INFO:#x} rejected (RM {TRANSIENT_RM:#x})"
        self.assertEqual((status, failed), ("ok", (None, message)))  # never the stale observation
        self.assertEqual(after, {"last": observation, "error": message, "kind": "transient",
                                 "board": observation["binding"]["native_index"], "static": FILLED})

    def test_a_transient_failure_before_any_success(self):
        rig = MetaRig(FakeGpu, configure=faults(((cp.INFO, 1), {"rm": TRANSIENT_RM})), accepted=MODERN)
        result, *_ = self.run_both(rig, reads(1, rig=rig))
        (status, (observation, error)), after = result[1]
        self.assertEqual((status, observation), ("ok", None))
        self.assertEqual(after, dict(EMPTY, error=error, kind="transient"))
        self.assertIn("no recognized power GET captured", error)  # the retry found no transport to reuse

    def test_success_after_a_failure_clears_the_error(self):
        # a transient INFO failure is retried once (INFO 3 and 4); an unsupported one is not (INFO 3)
        for name, counts, rm in (("transient", (3, 4), TRANSIENT_RM), ("unsupported", (3,), UNSUPPORTED_RM)):
            with self.subTest(name):
                rig = MetaRig(FakeGpu, configure=faults(*[((cp.INFO, k), {"rm": rm}) for k in counts]),
                              accepted=MODERN, recapture=True)
                result, *_ = self.run_both(rig, reads(3, rig=rig))
                (_, (observation, _)), _ = result[3]
                self.assertEqual(result[2][1]["kind"], name)
                self.assertEqual(result[3][1], {"last": observation, "error": None, "kind": None,
                                                "board": observation["binding"]["native_index"],
                                                "static": FILLED})

    def test_transient_first_probe_recovers_with_arbitration_unavailable(self):
        # test_transient_first_probe_recovers_and_optional_status_does_not_gate_extension (the read)
        events = [((cp.INFO, 1), {"rm": TRANSIENT_RM})]
        events += [((cp.NATIVE_STATUS, k), {"rm": UNSUPPORTED_RM}) for k in range(1, 9)]
        rig = MetaRig(FakeGpu, configure=faults(*events), accepted=MODERN, recapture=True)
        result, _, _, _, fake_calls = self.run_both(rig, reads(1, rig=rig))
        (status, (observation, error)), after = result[1]
        self.assertEqual((status, error), ("ok", None))
        self.assertIsNone(observation["arbitration"])
        self.assertEqual(observation["arbitration_error"],
                         f"Client-power command {cp.NATIVE_STATUS:#x} rejected (RM {UNSUPPORTED_RM:#x})")
        self.assertEqual(after["last"], observation)
        self.assertEqual(commands(fake_calls).count(MODERN_INFO), 3)


class StaticBackfillTests(LiveCase):
    """test_successful_live_binding_supplies_missing_static_range_and_default and
    test_live_metadata_does_not_replace_existing_normal_static_values."""

    def backfill(self, nvml, static, expected, rig=None):
        rig = rig or MetaRig(FakeGpu, accepted=MODERN)
        result, *_ = self.run_both(rig, reads(1, rig=rig), nvml=nvml, static=static)
        self.assertEqual(result[0]["static"], dict(dict.fromkeys(PL), **static))
        self.assertEqual(result[1][1]["static"], expected)
        return result

    def test_absent_keys_are_filled_from_the_binding(self):
        self.backfill({}, {}, FILLED)

    def test_failed_getters_leave_keys_to_fill(self):
        # a getter that fails at startup is a None static value, as {"pl_min_mw": None, ...}
        self.backfill({CONSTRAINTS: constraints(1, 2, status=3)}, {}, FILLED)

    def test_present_keys_are_never_replaced(self):
        present = {"pl_min_mw": 100000, "pl_max_mw": 999000, "pl_def_mw": 250000}
        self.backfill({CONSTRAINTS: constraints(100000, 999000), DEFAULT: u32_callback(250000)}, present, present)

    def test_only_the_missing_key_is_filled(self):
        self.backfill({DEFAULT: u32_callback(321000)}, {"pl_def_mw": 321000}, FILLED)
        self.backfill({CONSTRAINTS: constraints(100000, 999000)}, {"pl_min_mw": 100000, "pl_max_mw": 999000},
                      {"pl_min_mw": 100000, "pl_def_mw": 321000, "pl_max_mw": 999000})

    def test_a_zero_value_is_present(self):
        # `static.get(key) is None`, not falsiness: a zero read from NVML is kept
        self.backfill({CONSTRAINTS: constraints(0, 999000)}, {"pl_min_mw": 0, "pl_max_mw": 999000},
                      {"pl_min_mw": 0, "pl_def_mw": 321000, "pl_max_mw": 999000})
        zero = dict.fromkeys(PL, 0)
        self.backfill({CONSTRAINTS: constraints(0, 0), DEFAULT: u32_callback(0)}, zero, zero)

    def test_a_failed_read_fills_nothing(self):
        rig = MetaRig(FakeGpu, configure=faults(((cp.INFO, 1), {"rm": UNSUPPORTED_RM})), accepted=MODERN)
        result = self.backfill({}, {}, dict.fromkeys(PL), rig=rig)
        self.assertEqual(result[1][1]["kind"], "unsupported")

    def test_the_core_capabilities_follow(self):
        # The core's own store is the one filled: dc_gpu_capabilities_get reports the keys after it.
        rig = MetaRig(FakeGpu, accepted=MODERN)
        rig.fresh()
        install(rig.card.nvml, rig.card.nvapi, rig.gdi())
        rig.probe, rig.enter = (lambda: native.hook_test_state()["patched"]), native.hook_test_entry
        with native.Gpu() as gpu:
            self.assertFalse(set(PL) & set(gpu.capabilities()))
            store(MetaNative(gpu), rig)
            self.assertIsNone(gpu.read_live_power_request()[1])
            self.assertEqual({key: gpu.capabilities().get(key) for key in PL}, FILLED)
            self.assertEqual({key: gpu.static[key] for key in PL}, FILLED)


class RefreshTests(LiveCase):
    def refresh(self, side, calls):
        before = list(calls)
        result = side.gpu.refresh_capabilities()
        return result, list(calls) == before  # no escape, enumeration or adapter query

    def test_refresh_releases_the_controller_and_the_observation_without_a_write(self):
        # test_refresh_releases_sticky_power_capability_without_a_write: on the Turing model a fresh
        # controller probes the modern INFO size again, so the release is visible in the fake log.
        rig = MetaRig(TuringGpu)

        def action(side):
            store(side, rig)
            steps = [live(side)]
            controller = side.state()
            refreshed, quiet = self.refresh(side, rig.calls)
            steps.append((refreshed, quiet, bookkeeping(side.gpu), side.state(), controller))
            steps.append(live(side))
            return steps

        result, _, final, _, fake_calls = self.run_both(rig, action)
        (_, (observation, _)), _ = result[0]
        refreshed, quiet, after, fresh, before = result[1]
        self.assertTrue(quiet)
        self.assertEqual(refreshed["retry_pending"], True)
        self.assertEqual(after, {"last": None, "error": None, "kind": None, "board": 17, "static": TURING_FILLED})
        self.assertEqual(fresh, state())  # a fresh controller's state
        self.assertNotEqual(before, fresh)
        (status, (again, error)), after_again = result[2]
        self.assertEqual((again, error), (observation, None))
        self.assertEqual(after_again["last"], observation)
        self.assertEqual(commands(fake_calls).count(MODERN_INFO), 2)  # probed again by the fresh controller
        self.assertEqual(final["transport_key"], TURING_KEY)
        writes = [command for command, _ in fake_calls if command == cp.SET]
        self.assertEqual(writes, [])

    def test_refresh_clears_a_recorded_error(self):
        for name, rm in (("transient", TRANSIENT_RM), ("unsupported", UNSUPPORTED_RM)):
            with self.subTest(name):
                rig = MetaRig(FakeGpu, configure=faults(((cp.INFO, 3), {"rm": rm}), ((cp.INFO, 4), {"rm": rm})),
                              accepted=MODERN, recapture=True)

                def action(side, rig=rig):
                    store(side, rig)
                    steps = [live(side), live(side)]
                    steps.append((self.refresh(side, rig.calls), bookkeeping(side.gpu)))
                    return steps

                result, *_ = self.run_both(rig, action)
                board = result[0][0][1][0]["binding"]["native_index"]
                self.assertEqual(result[1][1]["kind"], name)
                self.assertEqual(result[2][1], {"last": None, "error": None, "kind": None, "board": board,
                                                "static": FILLED})


class BoardPolicyTests(LiveCase):
    def test_set_and_cleared_like_the_attribute(self):
        rig = MetaRig(FakeGpu, accepted=MODERN)

        def action(side):
            store(side, rig)
            steps = []
            for value in (5, None, 0, 31):
                side.gpu._board_policy = value
                steps.append(getattr(side.gpu, "_board_policy", None))
            steps.append(live(side))  # a read takes the bound policy
            side.gpu._board_policy = 3
            steps.append(self_refresh(side))  # refresh keeps it
            return steps

        result, *_ = self.run_both(rig, action)
        self.assertEqual(result[:4], [5, None, 0, 31])
        self.assertEqual(result[4][1]["board"], 31)
        self.assertEqual(result[5], 3)

    def test_native_refusals(self):
        with MetaRig(FakeGpu, accepted=MODERN).card.native_gpu() as gpu:
            for value in (-1, 1 << 31, 2.0, "3", True):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    gpu._board_policy = value
            self.assertIsNone(gpu._board_policy)
            for name in ("_live_power_last", "_live_power_error", "_live_power_error_kind"):
                with self.subTest(name), self.assertRaises(AttributeError):
                    setattr(gpu, name, None)  # only a read writes them: no silent divergence


def self_refresh(side):
    side.gpu.refresh_capabilities()
    return getattr(side.gpu, "_board_policy", None)


class AbiTests(unittest.TestCase):
    def tearDown(self):
        native.set_export_resolver(None)

    def test_argument_refusals_leave_the_bookkeeping(self):
        lib = native.load()
        rig = MetaRig(FakeGpu, accepted=MODERN)
        with rig.card.native_gpu() as gpu:
            gpu._board_policy = 4
            obs = gpu._cp_observation_type()()
            error = native._Error()
            error.size = ctypes.sizeof(error)
            self.assertEqual(lib.dc_cpc_live_read(gpu._handle, None, ctypes.byref(error)), native.DC_E_ARGUMENT)
            self.assertEqual(lib.dc_cpc_live_read(gpu._handle, ctypes.addressof(obs), ctypes.byref(error)),
                             native.DC_E_ARGUMENT)  # obs.size unset
            self.assertEqual(lib.dc_cpc_live_read(None, ctypes.addressof(obs), ctypes.byref(error)),
                             native.DC_E_ARGUMENT)
            self.assertEqual(lib.dc_cpc_board_policy_set(gpu._handle, -2), native.DC_E_ARGUMENT)
            self.assertEqual(lib.dc_cpc_board_policy_set(None, 1), native.DC_E_ARGUMENT)
            s = gpu._cp_live_state_type()()
            self.assertEqual(lib.dc_cpc_live_state_get(gpu._handle, ctypes.addressof(s)), native.DC_E_ARGUMENT)
            s.size = ctypes.sizeof(s)
            self.assertEqual(lib.dc_cpc_live_state_get(gpu._handle, ctypes.addressof(s)), native.DC_E_ARGUMENT)
            self.assertEqual(lib.dc_cpc_live_state_get(gpu._handle, None), native.DC_E_ARGUMENT)
            self.assertEqual((gpu._board_policy, gpu._live_power_last, gpu._live_power_error,
                              gpu._live_power_error_kind), (4, None, None, None))
            self.assertFalse(set(PL) & set(gpu.capabilities()))

    def test_a_failure_with_no_error_out_is_still_recorded(self):
        rig = MetaRig(FakeGpu, accepted=MODERN)
        with rig.card.native_gpu() as gpu:  # no transport stored and none capturable
            obs = gpu._cp_observation_type()()
            obs.size, obs.metadata.size = ctypes.sizeof(obs), ctypes.sizeof(gpu._CpcMetadata)
            obs.arbitration.size = ctypes.sizeof(obs.arbitration)
            self.assertEqual(native.load().dc_cpc_live_read(gpu._handle, ctypes.addressof(obs), None),
                             native.DC_E_FAILED)
            self.assertEqual(gpu._live_power_error_kind, "transient")
            self.assertIn("no recognized power GET captured", gpu._live_power_error)


if __name__ == "__main__":
    unittest.main()
