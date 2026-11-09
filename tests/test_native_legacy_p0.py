# Druta - parity tests for the native legacy P0 hold and the verification holds.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native legacy P0 hold and verification holds (increment G2) against nvbackend.GPU.

druta.native_legacy_p0 runs dc_legacy_p0_supported / _owned / _hold / _release / _request and the
V/F lock accessors dc_gpu_vf_lock_recovery_get / _set and dc_gpu_vf_lock_entry_get. The originals,
GPU.legacy_p0_supported, legacy_p0_owned, hold_legacy_p0, release_legacy_p0,
verification_legacy_p0 and verification_p0, run on an nvbackend.GPU twin whose read() is the real
GPU.read over the telemetry fake of tests/test_native_telemetry.py (Rig), and whose ForcePstate
(0x025BFB10) is bound by the real NvAPI binder from the same QueryInterface table the core resolves
it from. The twin carries the identity the native handle exposes (NVAPI ok, handle, pairing error),
as tests/test_native_target_binding.py's Card.python_twin does.

Each scenario runs once per side from the same fake state. Every observable is compared: the
result ((ok, message), or the exception type and text), one event log holding each ForcePstate call
(handle and both u32 arguments), each telemetry sample and each wait (the core's sleep hook,
Python's patched time.sleep, both in milliseconds), and ownership before and after. The samples are
scripted per read (pstate from CurrentPstate, core and memory from the public clock getter, the P0
memory maximum from nvmlDeviceGetMinMaxClockOfPState; the private clock getter and Pstates20 are
absent so nothing else supplies them). verification_p0 on a non-Kepler card runs on the stateful
lock table of tests/test_native_vflock.py (LockRig, Turing-class) and compares, in addition, every
getter and setter call with the whole table each write carried, the table afterwards and the
recovery record. The loaded voltage comes from the real readers on both sides (GPU.read_vcore_mv and
native.Gpu.read_vcore_mv) over a VoltRailsStatus the rigs serve and log (handle and struct version),
so whether and when it is read is compared too; only the V/F curve is supplied per side. The
fixture values are inputs to the logic, not claims about any board.

Not reachable natively, and so covered only by tests/test_legacy_p0.py and
tests/test_kepler_verification_p0.py: a ForcePstate that raises (Python's "P0 request failed:
{exc}", "P0 release failed: {exc}" and the transport-exception rollback in hold_legacy_p0 and
verification_legacy_p0; the core does not guard a faulting export), a telemetry read that raises
("P0 verification failed: {exc}"; Telemetry::read() does not throw), and non-finite or non-int
memory readings (the core's samples are uint32, finite by construction). The developer-device
profile table (GPU.LEGACY_P0_PROFILES, legacy_p0_profile) is deliberately not ported.
"""
import copy
import ctypes
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from druta import native, nvbackend as NV
from tests.test_native_identity import FakeNvapi, TITAN, write_u32
from tests.test_native_telemetry import Rig, u32_callback
from tests.test_native_vflock import LockRig, entry

if not native.available():
    raise unittest.SkipTest("native core not built (native/build.sh linux)")

from druta import native_legacy_p0 as nl  # noqa: E402

CDECL = ctypes.CFUNCTYPE
FORCE_PSTATE = 0x025BFB10
VOLT_RAILS_STATUS = 0x465F9BCF
VOLT_STATUS_V1 = NV.NvAPI.ver(NV._VoltStatus, 1)
ALL_CLOCKS, ALL_CLOCKS_PRIV, CURRENT_PSTATE, PSTATES20 = 0xDCB616C3, 0x1BD69F49, 0x927DA4F6, 0x6FF81213
KEPLER, MAXWELL, TURING = 2, 3, 6
GOOD = {"pstate": 0, "core": 705, "mem": 3004, "top": 3004}
P8 = dict(GOOD, pstate=8, mem=324)

HELD = "P0 held; core {core} MHz, memory {mem} MHz (not a maximum-core lock)"
NOT_OBSERVED = "P0 and its top memory band were not observed"
RELEASED = "P0 request released; automatic performance states restored"
UNAVAILABLE = "legacy P0 hold is unavailable on this GPU/driver"


def serve_vcore(rig, log, value_uv=1043750, status=0):
    """Serve VoltRailsStatus from the rig: log(handle, version) on each call, then status, and on
    status 0 value_uv (rig.vcore, when the rig carries one, overrides both)."""
    @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
    def volt(handle, pointer):
        vs = NV._VoltStatus.from_address(pointer)
        log(handle, vs.version)
        code, value = getattr(rig, "vcore", (status, value_uv))
        if code:
            return code
        vs.value_uV = value
        return 0
    rig._volt_keep = volt
    rig.names["VoltRailsStatus"] = rig.nvapi.interfaces[VOLT_RAILS_STATUS] = volt


def attempt(call):
    try:
        return ("ok", call())
    except Exception as exc:  # noqa: BLE001 - the type and text are what is compared
        return ("raise", type(exc).__name__, str(exc))


class P0Rig(Rig):
    """Rig with scripted telemetry samples and a ForcePstate that logs into one event list.

    samples: one dict per read (pstate, core, mem, top; None leaves that value out); the last one
             repeats. force: the statuses ForcePstate returns, in order (0 once exhausted).
    """

    def __init__(self, *, arch=KEPLER, with_force=True, nvapi_init=0, pairing=False):
        super().__init__()
        if nvapi_init or pairing:  # the same interfaces behind a failing or mismatched NVAPI
            old = self.nvapi
            self.nvapi = FakeNvapi([dict(TITAN, devid=0x1B02) if pairing else TITAN], init_status=nvapi_init)
            for ident, fn in old.interfaces.items():
                self.nvapi.interfaces.setdefault(ident, fn)
        i, n, e = self.nvapi.interfaces, self.names, self.nvml.exports
        if arch is None:
            e.pop("nvmlDeviceGetArchitecture", None)
        else:
            e["nvmlDeviceGetArchitecture"] = u32_callback(arch)
        i.pop(ALL_CLOCKS_PRIV, None)
        n["AllClocksPriv"] = None

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def pstates(handle, pointer):  # absent table: no Pstates20 fallback for the P0 maxima
            return -9
        n["Pstates20Get"] = i[PSTATES20] = pstates

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def clocks(handle, pointer):
            self.reads += 1
            self.events.append(("read", self.reads))
            cf = NV._ClkFreqs.from_address(pointer)
            if cf.clockType != 0:
                return 1
            s = self.sample()
            for domain, key in ((0, "core"), (4, "mem")):
                if s[key] is not None:
                    cf.domain[domain].present, cf.domain[domain].frequency = 1, s[key] * 1000
            return 0
        n["AllClocks"] = i[ALL_CLOCKS] = clocks

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def pstate(handle, pointer):
            value = self.sample()["pstate"]
            if value is None:
                return 1
            write_u32(pointer, value)
            return 0
        n["CurrentPstate"] = i[CURRENT_PSTATE] = pstate

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p)
        def minmax(device, ctype, pstate_index, low, high):
            value = {0: 2100, 2: self.sample()["top"]}.get(ctype) if pstate_index == 0 else None
            if value is None:
                return 3
            write_u32(low, 0)
            write_u32(high, value)
            return 0
        e["nvmlDeviceGetMinMaxClockOfPState"] = minmax

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32)
        def force(handle, pstate_index, flags):
            self.events.append(("force", handle, pstate_index, flags))
            return self.force.pop(0) if self.force else 0
        self._force_keep = force
        if with_force:
            i[FORCE_PSTATE] = force
        # every VoltRailsStatus call (each telemetry sample's, and read_vcore_mv's) in its own log
        serve_vcore(self, lambda handle, version: self.vcore_calls.append((handle, version)))
        self.reset()

    def reset(self, samples=(GOOD,), force=()):
        self.samples, self.force = [dict(s) for s in samples], list(force)
        self.events, self.reads, self.vcore_calls = [], 0, []

    def sample(self):
        return self.samples[min(max(self.reads, 1), len(self.samples)) - 1]

    def twin(self, opened):
        """An nvbackend.GPU reading this fake, carrying the identity of the native handle."""
        gpu = self.python_gpu()
        gpu.nvapi.ok, gpu.nvapi.gpu = opened.nvapi.ok, opened.nvapi.gpu
        gpu.pairing_error = opened.pairing_error
        binder = SimpleNamespace(_qi=self.nvapi.query)
        gpu.nvapi.ForcePstate = NV.NvAPI._i(binder, FORCE_PSTATE, NV.PTR, NV.u32, NV.u32)  # as NvAPI binds it
        gpu.nvapi.GetArchInfo = None
        gpu._legacy_p0_owned = False  # GPU.__init__
        gpu._vf_lock_recovery = None
        return gpu


class PythonSide:
    def __init__(self, gpu):
        self.gpu = gpu

    def supported(self):
        return self.gpu.legacy_p0_supported()

    def owned(self):
        return self.gpu.legacy_p0_owned()

    def hold(self):
        return self.gpu.hold_legacy_p0()

    def release(self):
        return self.gpu.release_legacy_p0()

    def verification_legacy_p0(self):
        return self.gpu.verification_legacy_p0()

    def verification_p0(self, voltage_mv=None):
        return self.gpu.verification_p0(voltage_mv)

    def refused_request(self):
        """Python's refusal of the request: verification_legacy_p0 raises before its request, in the
        shape of the core's (acquired, ok, message). Only for cards the request refuses."""
        try:
            with self.gpu.verification_legacy_p0():
                raise AssertionError("the request was not refused")
        except RuntimeError as exc:
            return False, False, str(exc)

    def recovery(self):
        return copy.deepcopy(getattr(self.gpu, "_vf_lock_recovery", None))

    def set_recovery(self, record):
        self.gpu._vf_lock_recovery = copy.deepcopy(record)


class NativeSide(PythonSide):
    def supported(self):
        return nl.legacy_p0_supported(self.gpu)

    def owned(self):
        return nl.legacy_p0_owned(self.gpu)

    def hold(self):
        return nl.hold_legacy_p0(self.gpu)

    def release(self):
        return nl.release_legacy_p0(self.gpu)

    def verification_legacy_p0(self):
        return nl.verification_legacy_p0(self.gpu)

    def verification_p0(self, voltage_mv=None):
        return nl.verification_p0(self.gpu, voltage_mv)

    def refused_request(self):
        return nl._request_legacy_p0(self.gpu)  # dc_legacy_p0_request's own refusal, no wrapper first

    def recovery(self):
        return nl.vf_lock_recovery(self.gpu)

    def set_recovery(self, record):
        nl.set_vf_lock_recovery(self.gpu, record)


class P0Parity(unittest.TestCase):
    def tearDown(self):
        native.set_sleep_hook(None)
        native.set_export_resolver(None)

    def both(self, rig, scenario, *, samples=(GOOD,), force=(), owned=False, on_sleep=None):
        """scenario(side) on the twin, then on the core, from the same fake state.

        owned: both sides first take a successful hold (all-good samples), so the scenario starts
        from a session-owned request; that priming is not part of the compared log.
        on_sleep(side): called from inside each wait (Python's time.sleep, the core's sleep hook)."""
        runs = []
        opened = rig.native_gpu()
        try:
            for side in (PythonSide(rig.twin(opened)), NativeSide(opened)):
                native_side = isinstance(side, NativeSide)

                def wait(ms, side=side):
                    rig.events.append(("sleep", ms))
                    if on_sleep:
                        on_sleep(side)
                with mock.patch.object(NV.time, "sleep", side_effect=lambda s: wait(round(s * 1000))):
                    native.set_sleep_hook(wait)
                    if owned:
                        rig.reset()
                        assert side.hold()[0], "priming hold failed"
                    rig.reset(samples, force)
                    before = side.owned()
                    result = attempt(lambda: scenario(side))
                    after = side.owned()
                    native.set_sleep_hook(None)
                runs.append({"result": result, "events": list(rig.events), "reads": rig.reads,
                             "owned": (before, after), "vcore": list(rig.vcore_calls)})
                if not native_side:
                    rig.reset()
        finally:
            opened.close()
        self.assertEqual(runs[0], runs[1], "Python and native runs differ")
        return runs[0]

    @staticmethod
    def forced(run):
        return [event[1:] for event in run["events"] if event[0] == "force"]


class HoldTests(P0Parity):
    def test_kepler_and_maxwell_hold_on_the_third_good_sample(self):
        for arch in (KEPLER, MAXWELL):
            with self.subTest(arch=arch):
                run = self.both(P0Rig(arch=arch), lambda s: s.hold())
                self.assertEqual(run["result"], ("ok", (True, HELD.format(core=705, mem=3004))))
                self.assertEqual(run["events"], [("force", 0x2000, 0, 2), ("read", 1), ("sleep", 100),
                                                 ("read", 2), ("sleep", 100), ("read", 3)])
                self.assertEqual(run["owned"], (False, True))
                self.assertEqual(run["vcore"], [(0x2000, VOLT_STATUS_V1)] * 3)  # one per telemetry sample

    def test_p8_that_never_settles_reads_twenty_times_and_releases(self):
        run = self.both(P0Rig(), lambda s: s.hold(), samples=[P8])
        self.assertEqual(run["result"], ("ok", (False, NOT_OBSERVED + "; " + RELEASED)))
        self.assertEqual(run["reads"], 20)
        self.assertEqual([e for e in run["events"] if e[0] == "sleep"], [("sleep", 100)] * 20)
        self.assertEqual(self.forced(run), [(0x2000, 0, 2), (0x2000, 16, 2)])
        self.assertEqual(run["owned"], (False, False))

    def test_a_bad_sample_resets_the_consecutive_count(self):
        samples = [P8, GOOD, GOOD, dict(GOOD, mem=405), GOOD, GOOD, dict(GOOD, core=535)]
        run = self.both(P0Rig(), lambda s: s.hold(), samples=samples)
        self.assertEqual(run["result"], ("ok", (True, HELD.format(core=535, mem=3004))))
        self.assertEqual((run["reads"], run["owned"]), (7, (False, True)))

    def test_a_nonzero_pstate_at_the_top_memory_band_is_not_p0(self):
        for pstate in (1, 2):
            with self.subTest(pstate=pstate):
                top_band = dict(GOOD, pstate=pstate)  # memory at the top band, but not P0
                run = self.both(P0Rig(), lambda s: s.hold(), samples=[top_band])
                self.assertEqual(run["result"], ("ok", (False, NOT_OBSERVED + "; " + RELEASED)))
                self.assertEqual((run["reads"], run["owned"]), (20, (False, False)))
                # and it resets the consecutive count: the hold completes only three samples later
                samples = [GOOD, GOOD, top_band, GOOD, GOOD, dict(GOOD, core=640)]
                run = self.both(P0Rig(), lambda s: s.hold(), samples=samples)
                self.assertEqual(run["result"], ("ok", (True, HELD.format(core=640, mem=3004))))
                self.assertEqual((run["reads"], run["owned"]), (6, (False, True)))

    def test_the_handle_lock_is_free_across_the_waits(self):
        # From inside the first wait, a second thread runs a lock-taking entry on the same handle
        # (dc_legacy_p0_owned); it must finish while the hold is still waiting. Python's hold takes
        # no lock, so the probe completes there too.
        probes = []

        def on_sleep(side):
            if any(p[0] is side for p in probes):  # the first wait of each side only
                return
            done, seen = threading.Event(), []

            def probe():
                seen.append(side.owned())
                done.set()
            thread = threading.Thread(target=probe, daemon=True)
            thread.start()
            finished = done.wait(5)
            if finished:
                thread.join(5)
            probes.append((side, finished, list(seen)))
        run = self.both(P0Rig(), lambda s: s.hold(), on_sleep=on_sleep)
        self.assertEqual(run["result"], ("ok", (True, HELD.format(core=705, mem=3004))))
        self.assertEqual([p[1:] for p in probes], [(True, [True]), (True, [True])])

    def test_two_good_samples_then_a_bad_one_do_not_hold(self):
        samples = [GOOD, GOOD, P8] * 6 + [GOOD, GOOD]
        run = self.both(P0Rig(), lambda s: s.hold(), samples=samples)
        self.assertEqual(run["result"], ("ok", (False, NOT_OBSERVED + "; " + RELEASED)))
        self.assertEqual(run["reads"], 20)

    def test_already_owned_hold_that_fails_retains_the_existing_hold(self):
        run = self.both(P0Rig(), lambda s: s.hold(), samples=[P8], owned=True)
        self.assertEqual(run["result"], ("ok", (False, NOT_OBSERVED + "; existing session P0 hold retained")))
        self.assertEqual(self.forced(run), [])
        self.assertEqual((run["reads"], run["owned"]), (20, (True, True)))

    def test_already_owned_hold_that_succeeds_makes_no_request(self):
        run = self.both(P0Rig(), lambda s: s.hold(), owned=True)
        self.assertEqual(run["result"], ("ok", (True, HELD.format(core=705, mem=3004))))
        self.assertEqual(self.forced(run), [])

    def test_nonzero_request_status_clears_ownership_and_reads_nothing(self):
        run = self.both(P0Rig(), lambda s: s.hold(), force=[-104])
        self.assertEqual(run["result"], ("ok", (False, "P0 request failed (NVAPI status -104)")))
        self.assertEqual((run["events"], run["owned"]), ([("force", 0x2000, 0, 2)], (False, False)))

    def test_failed_verification_with_a_failed_release_keeps_ownership(self):
        run = self.both(P0Rig(), lambda s: (s.hold(), s.owned(), s.release()), samples=[P8], force=[0, -5])
        failed = (False, NOT_OBSERVED + "; RELEASE FAILED: P0 release failed (NVAPI status -5)")
        self.assertEqual(run["result"], ("ok", (failed, True, (True, RELEASED))))
        self.assertEqual(self.forced(run), [(0x2000, 0, 2), (0x2000, 16, 2), (0x2000, 16, 2)])
        self.assertEqual(run["owned"], (False, False))

    def test_release(self):
        nothing = self.both(P0Rig(), lambda s: s.release())
        self.assertEqual(nothing["result"], ("ok", (True, "this session owns no legacy P0 hold")))
        self.assertEqual(nothing["events"], [])
        owned = self.both(P0Rig(), lambda s: s.release(), owned=True)
        self.assertEqual(owned["result"], ("ok", (True, RELEASED)))
        self.assertEqual((owned["events"], owned["owned"]), ([("force", 0x2000, 16, 2)], (True, False)))
        refused = self.both(P0Rig(), lambda s: s.release(), owned=True, force=[-6])
        self.assertEqual(refused["result"], ("ok", (False, "P0 release failed (NVAPI status -6)")))
        self.assertEqual(refused["owned"], (True, True))

    def test_other_generations_and_an_absent_architecture_are_unsupported(self):
        for arch in (None, 1, 4, 5, 6, 7, 8, 9, 10):
            with self.subTest(arch=arch):
                run = self.both(P0Rig(arch=arch), lambda s: (s.supported(), s.hold()))
                self.assertEqual(run["result"], ("ok", (False, (False, UNAVAILABLE))))
                self.assertEqual((run["events"], run["owned"]), ([], (False, False)))

    def test_nvapi_down_interface_absent_or_pairing_error_are_unsupported(self):
        for options in (dict(nvapi_init=-1), dict(with_force=False), dict(pairing=True),
                        dict(arch=MAXWELL, pairing=True)):
            with self.subTest(**options):
                rig = P0Rig(**options)
                if options.get("pairing"):
                    with rig.native_gpu() as opened:
                        self.assertTrue(opened.pairing_error)
                run = self.both(rig, lambda s: (s.supported(), s.hold(), s.release()))
                self.assertEqual(run["result"],
                                 ("ok", (False, (False, UNAVAILABLE), (True, "this session owns no legacy P0 hold"))))
                self.assertEqual(run["events"], [])

    def test_memory_maximum_absent_or_zero_never_confirms(self):
        for top in (None, 0):
            with self.subTest(top=top):
                run = self.both(P0Rig(), lambda s: s.hold(), samples=[dict(GOOD, top=top)])
                self.assertEqual(run["result"], ("ok", (False, NOT_OBSERVED + "; " + RELEASED)))
                self.assertEqual(run["reads"], 20)

    def test_memory_threshold_is_ninety_seven_percent_of_the_top_band(self):
        cases = [(1000, 969, False), (1000, 970, True), (3505, 3399, False), (3505, 3400, True)]
        for top, mem, holds in cases:
            with self.subTest(top=top, mem=mem):
                run = self.both(P0Rig(), lambda s: s.hold(), samples=[dict(GOOD, top=top, mem=mem)])
                expected = (True, HELD.format(core=705, mem=mem)) if holds else \
                    (False, NOT_OBSERVED + "; " + RELEASED)
                self.assertEqual(run["result"], ("ok", expected))

    def test_memory_or_pstate_absent_never_confirms(self):
        for key in ("mem", "pstate"):
            with self.subTest(key=key):
                run = self.both(P0Rig(), lambda s: s.hold(), samples=[dict(GOOD, **{key: None})])
                self.assertEqual(run["result"], ("ok", (False, NOT_OBSERVED + "; " + RELEASED)))

    def test_absent_core_clock_prints_a_question_mark(self):
        run = self.both(P0Rig(), lambda s: s.hold(), samples=[dict(GOOD, core=None)])
        self.assertEqual(run["result"], ("ok", (True, HELD.format(core="?", mem=3004))))


class VerificationLegacyP0Tests(P0Parity):
    @staticmethod
    def verify(body=lambda side, check: check(), *, kepler_route=False):
        def scenario(side):
            manager = side.verification_p0(800.0) if kepler_route else side.verification_legacy_p0()
            with manager as check:
                inside = side.owned()
                body(side, check)
            return inside
        return scenario

    def test_acquire_and_release(self):
        for kepler_route in (False, True):
            with self.subTest(through_verification_p0=kepler_route):
                run = self.both(P0Rig(), self.verify(kepler_route=kepler_route))
                self.assertEqual(run["result"], ("ok", True))
                self.assertEqual(self.forced(run), [(0x2000, 0, 2), (0x2000, 16, 2)])
                self.assertEqual((run["reads"], run["owned"]), (0, (False, False)))

    def test_existing_session_hold_is_preserved_without_a_call(self):
        run = self.both(P0Rig(), self.verify(), owned=True)
        self.assertEqual(run["result"], ("ok", True))
        self.assertEqual((run["events"], run["owned"]), ([], (True, True)))

    def test_nonzero_request_status_still_releases(self):
        run = self.both(P0Rig(), self.verify(), force=[-3])
        self.assertEqual(run["result"], ("raise", "RuntimeError", "Kepler P0 request failed (NVAPI status -3)"))
        self.assertEqual(self.forced(run), [(0x2000, 0, 2), (0x2000, 16, 2)])
        self.assertEqual(run["owned"], (False, False))

    def test_nonzero_request_and_release_statuses_leave_ownership_set(self):
        run = self.both(P0Rig(), self.verify(), force=[-3, -4])
        self.assertEqual(run["result"], ("raise", "RuntimeError",
                                         "verification P0 hold RELEASE FAILED: P0 release failed (NVAPI status -4)"))
        self.assertEqual(run["owned"], (False, True))

    def test_release_failure_raises(self):
        run = self.both(P0Rig(), self.verify(), force=[0, -7])
        self.assertEqual(run["result"], ("raise", "RuntimeError",
                                         "verification P0 hold RELEASE FAILED: P0 release failed (NVAPI status -7)"))
        self.assertEqual(run["owned"], (False, True))

    def test_released_hold_fails_the_check(self):
        run = self.both(P0Rig(), self.verify(lambda side, check: (side.release(), check())))
        self.assertEqual(run["result"], ("raise", "RuntimeError", "verification's Kepler P0 request was released"))
        self.assertEqual(self.forced(run), [(0x2000, 0, 2), (0x2000, 16, 2)])

    def test_body_exception_releases(self):
        def body(side, check):
            raise ValueError("controller")
        run = self.both(P0Rig(), self.verify(body))
        self.assertEqual(run["result"], ("raise", "ValueError", "controller"))
        self.assertEqual(run["owned"], (False, False))

    def test_body_exception_with_a_failed_release_raises_release_failed(self):
        def body(side, check):
            raise ValueError("controller")
        run = self.both(P0Rig(), self.verify(body), force=[0, -7])
        self.assertEqual(run["result"], ("raise", "RuntimeError",
                                         "verification P0 hold RELEASE FAILED: P0 release failed (NVAPI status -7)"))
        self.assertEqual(self.forced(run), [(0x2000, 0, 2), (0x2000, 16, 2)])
        self.assertEqual(run["owned"], (False, True))

    def test_maxwell_and_unsupported_cards_are_refused_before_any_call(self):
        refusal = ("raise", "RuntimeError", "Kepler P0 request is unavailable; verification cannot write")
        for options in (dict(arch=MAXWELL), dict(arch=None), dict(with_force=False), dict(nvapi_init=-1)):
            with self.subTest(**options):
                run = self.both(P0Rig(**options), self.verify())
                self.assertEqual((run["result"], run["events"]), (refusal, []))

    def test_the_core_request_refuses_on_its_own(self):
        # dc_legacy_p0_request called directly, with no wrapper refusing first, against Python's
        # refusal in verification_legacy_p0.
        refusal = (False, False, "Kepler P0 request is unavailable; verification cannot write")
        for options in (dict(arch=MAXWELL), dict(arch=None), dict(pairing=True), dict(with_force=False),
                        dict(nvapi_init=-1)):
            with self.subTest(**options):
                run = self.both(P0Rig(**options), lambda s: s.refused_request())
                self.assertEqual(run["result"], ("ok", refusal))
                self.assertEqual((run["events"], run["owned"]), ([], (False, False)))

    def test_kepler_verification_p0_ignores_a_pending_vf_recovery(self):
        # Kepler delegates to the P0 request before the V/F recovery check; the record is untouched.
        record = {"domain": 6, "previous": (0, 0), "requested": (3, 737500)}

        def scenario(side):
            side.set_recovery(record)
            with side.verification_p0() as check:
                inside = side.owned()
                check()
            return inside, side.recovery()
        run = self.both(P0Rig(), scenario)
        self.assertEqual(run["result"], ("ok", (True, record)))
        self.assertEqual(self.forced(run), [(0x2000, 0, 2), (0x2000, 16, 2)])
        self.assertEqual((run["vcore"], run["owned"]), ([], (False, False)))  # no loaded voltage read

    def test_an_unsupported_kepler_card_is_refused_through_verification_p0(self):
        def scenario(side):
            with side.verification_p0():
                pass
        run = self.both(P0Rig(with_force=False), scenario)
        self.assertEqual(run["result"], ("raise", "RuntimeError",
                                         "Kepler P0 request is unavailable; verification cannot write"))
        self.assertEqual((run["events"], run["vcore"], run["owned"]), ([], [], (False, False)))


class RequestEntryFailureTests(unittest.TestCase):
    """dc_legacy_p0_request failing after the core took ownership (DC_E_FAILED from an exception
    after request() set it): the wrapper still learns of the ownership and releases it."""

    def tearDown(self):
        native.set_export_resolver(None)

    def test_ownership_taken_before_a_failed_entry_is_released(self):
        rig = P0Rig()
        library = nl.load()

        class FailingAfterTheRequest:
            def __getattr__(self, name):
                return getattr(library, name)

            @staticmethod
            def dc_legacy_p0_request(handle, acquired, outcome):
                assert library.dc_legacy_p0_request(handle, acquired, outcome) == native.DC_OK
                return native.DC_E_FAILED  # as if building the outcome had thrown
        with rig.native_gpu() as gpu:
            rig.reset()
            with mock.patch.object(nl, "load", return_value=FailingAfterTheRequest()):
                def run():
                    with nl.verification_legacy_p0(gpu):
                        raise AssertionError("the body ran")
                self.assertRaisesRegex(ValueError, "the operation failed", run)
            self.assertEqual([e for e in rig.events if e[0] == "force"],
                             [("force", 0x2000, 0, 2), ("force", 0x2000, 16, 2)])
            self.assertFalse(nl.legacy_p0_owned(gpu))


class VerificationP0Tests(unittest.TestCase):
    """verification_p0 on a Turing-class lock table. The V/F curve is supplied; the loaded voltage
    is read by the real readers from the rig's VoltRailsStatus, whose calls are in rig.calls."""

    CURVE = [{"volt_mv": 700.0}, {"volt_mv": 737.5}, {"volt_mv": 750.0}]
    VCORE = ("VoltRailsStatus", 0x2000, VOLT_STATUS_V1)

    def tearDown(self):
        native.set_export_resolver(None)

    def both(self, rig, scenario, *, curve=CURVE, curve_error=None, vcore=(0, 743000), prepare=None):
        """vcore: (status, value_uV) VoltRailsStatus answers; curve_error: read_vf_curve's error."""
        runs = []
        rig.vcore = vcore
        serve_vcore(rig, lambda handle, version: rig.calls.append(("VoltRailsStatus", handle, version)))
        opened = rig.native_gpu()
        set_status = rig.set_status  # not reset by rig.reset(); a body may change it
        try:
            for side in (PythonSide(rig.python_gpu()), NativeSide(opened)):
                rig.reset()
                rig.set_status = set_status
                gpu = side.gpu
                if isinstance(side, PythonSide):
                    gpu._vf_lock_recovery = None

                def read_vf_curve():
                    rig.calls.append(("curve",))
                    if prepare:
                        prepare(rig)
                    return copy.deepcopy(curve), curve_error
                gpu.read_vf_curve = read_vf_curve
                result = attempt(lambda: scenario(side, rig))
                runs.append({"result": result, "calls": list(rig.calls), "state": rig.state(),
                             "recovery": side.recovery()})
            # the shared fake is stateful: the class attributes are restored for the next run
        finally:
            opened.close()
        self.assertEqual(runs[0], runs[1], "Python and native runs differ")
        return runs[0]

    @staticmethod
    def verify(body=lambda side, rig, check: check(), voltage_mv=None):
        def scenario(side, rig):
            with side.verification_p0(voltage_mv) as check:
                inside = side.gpu.read_vf_lock_status(domain=6)
                body(side, rig, check)
            return inside
        return scenario

    @staticmethod
    def writes(run):
        return [call[4] for call in run["calls"] if call[0] == "VfLockSet"]

    def test_the_exact_table_is_restored(self):
        rig = LockRig(arch=TURING)
        original = copy.deepcopy(rig.table)
        run = self.both(rig, self.verify())
        lock = {"domain": 6, "lockMode": 3, "volt_uV": 737500, "volt_mv": 737.5, "count": 7}
        self.assertEqual(run["result"], ("ok", (lock, None)))
        held = copy.deepcopy(original)
        held[6].update(lockMode=3, volt_uV=737500)
        self.assertEqual(self.writes(run), [held, original])  # every byte of both writes
        self.assertEqual((run["state"][0], run["recovery"]), (original, None))
        # the loaded voltage is read once, by the real reader, before the curve
        self.assertEqual(run["calls"][:3], [("BoostLock",), self.VCORE, ("curve",)])
        self.assertEqual(run["calls"].count(self.VCORE), 1)

    def test_an_off_target_with_a_stored_voltage_is_restored_exactly(self):
        def off_812500(rig):
            reset = rig.reset

            def stored():
                reset()
                rig.table[6] = entry(6, 0, 812500)  # OFF, but carrying a voltage
            rig.reset = stored
            return rig
        rig = off_812500(LockRig(arch=TURING))
        rig.reset()
        original = copy.deepcopy(rig.table)
        run = self.both(rig, self.verify())
        held = copy.deepcopy(original)
        held[6].update(lockMode=3, volt_uV=737500)
        self.assertEqual(self.writes(run), [held, original])
        self.assertEqual((run["state"][0], run["recovery"]), (original, None))

        def body(side, rig, check):
            rig.set_status = -1  # the restoring write is refused: the record stays pending
        run = self.both(off_812500(LockRig(arch=TURING)), lambda side, rig: attempt(
            lambda: self.verify(body)(side, rig)))
        self.assertEqual(run["recovery"], {"domain": 6, "previous": (0, 812500), "requested": (3, 737500),
                                           "verification": True})

        # the entry accessor against the twin's own table entry
        rig = off_812500(LockRig(arch=TURING))
        rig.reset()
        twin = rig.python_gpu()
        raw = twin._vf_lock_read_raw()
        e = next(e for e in twin._vf_lock_entries(raw) if e.domain == 6)
        with rig.native_gpu() as gpu:
            point = nl._PointLock()
            self.assertEqual(nl.load().dc_gpu_vf_lock_entry_get(gpu._handle, 6, ctypes.byref(point)), native.DC_OK)
            self.assertEqual((point.domain, point.lock_mode, point.volt_uv, point.count),
                             (e.domain, e.lockMode, e.volt_uV, raw.count))
            self.assertEqual((point.domain, point.lock_mode, point.volt_uv, point.count), (6, 0, 812500, 7))
            self.assertEqual(nl._vf_lock_entry(gpu, 6), (0, 812500))

    def test_the_explicit_voltage_wins_over_the_reader(self):
        run = self.both(LockRig(arch=TURING), self.verify(voltage_mv=749.9))
        self.assertEqual(run["result"][1][0]["volt_uV"], 737500)
        self.assertNotIn(self.VCORE, run["calls"])
        run = self.both(LockRig(arch=TURING), self.verify(voltage_mv=750.0))
        self.assertEqual(run["result"][1][0]["volt_uV"], 750000)
        self.assertNotIn(self.VCORE, run["calls"])

    def test_an_explicit_zero_or_nan_voltage_is_refused_without_a_read(self):
        for voltage_mv, message in ((0.0, "no V/F point at or below the loaded voltage"),
                                    (float("nan"), "cannot select a loaded V/F point for verification")):
            with self.subTest(voltage_mv=voltage_mv):
                run = self.both(LockRig(arch=TURING), self.verify(voltage_mv=voltage_mv))
                self.assertEqual((run["result"], self.writes(run)), (("raise", "RuntimeError", message), []))
                self.assertNotIn(self.VCORE, run["calls"])

    def test_a_curve_returned_with_an_error_is_not_used(self):
        run = self.both(LockRig(arch=TURING), self.verify(), curve_error="curve unreadable")
        self.assertEqual((run["result"], self.writes(run)),
                         (("raise", "RuntimeError", "cannot select a loaded V/F point for verification"), []))
        self.assertEqual(run["recovery"], None)

    def test_a_point_hold_on_another_domain_is_checked_on_that_domain(self):
        rig = LockRig(arch=TURING)
        reset = rig.reset

        def held():
            reset()
            rig.table[0] = entry(0, 3, 900000)
        rig.reset = held
        run = self.both(rig, self.verify())
        self.assertEqual(run["result"], ("ok", (None, None)))  # domain 6 itself holds nothing
        self.assertEqual((self.writes(run), run["recovery"]), ([], None))
        self.assertNotIn(self.VCORE, run["calls"])

    def test_an_existing_hold_that_changes_raises_without_a_restore(self):
        rig = LockRig(arch=TURING)
        reset = rig.reset

        def held():
            reset()
            rig.table[6] = entry(6, 3, 900000)
        rig.reset = held

        def body(side, rig, check):
            rig.table[6]["volt_uV"] = 875000  # another tuner moved it; this session owns nothing
            check()
        run = self.both(rig, self.verify(body))
        self.assertEqual(run["result"], ("raise", "RuntimeError", "verification V/F hold changed or became unreadable"))
        self.assertEqual((self.writes(run), run["recovery"]), ([], None))
        self.assertEqual(run["state"][0][6]["volt_uV"], 875000)

    def test_a_body_exception_is_propagated_after_the_restore(self):
        rig = LockRig(arch=TURING)
        original = copy.deepcopy(rig.table)

        def body(side, rig, check):
            raise ValueError("controller")
        run = self.both(rig, self.verify(body))
        self.assertEqual(run["result"], ("raise", "ValueError", "controller"))
        self.assertEqual(len(self.writes(run)), 2)
        self.assertEqual((run["state"][0], run["recovery"]), (original, None))

    def test_a_failed_restore_replaces_the_body_exception(self):
        def body(side, rig, check):
            rig.set_status = -1
            raise ValueError("controller")
        run = self.both(LockRig(arch=TURING), self.verify(body))
        self.assertEqual(run["result"], ("raise", "RuntimeError",
                                         "verification P0 hold RESTORE FAILED: V/F lock recovery write failed (status -1)"))
        self.assertEqual(run["recovery"], {"domain": 6, "previous": (0, 0), "requested": (3, 737500),
                                           "verification": True})

    def test_a_target_beyond_the_reported_count_is_unavailable(self):
        rig = LockRig(arch=TURING)
        reset = rig.reset

        def short():
            reset()
            rig.count = 6  # domain 6 sits at index 6, past the entries the driver reports
        rig.reset = short
        run = self.both(rig, self.verify())
        self.assertEqual((run["result"], self.writes(run)),
                         (("raise", "RuntimeError", "verification V/F target is unavailable or already in use"), []))

    def test_an_existing_point_hold_is_never_written(self):
        rig = LockRig(arch=TURING)
        reset = rig.reset

        def held():
            reset()
            rig.table[6] = entry(6, 3, 900000)
        rig.reset = held
        run = self.both(rig, self.verify())
        self.assertEqual(run["result"][1][0]["volt_uV"], 900000)
        self.assertEqual((self.writes(run), run["recovery"]), ([], None))
        self.assertNotIn(("curve",), run["calls"])
        self.assertNotIn(self.VCORE, run["calls"])  # no loaded voltage is read for an existing hold

    def test_a_set_that_cannot_be_rolled_back_marks_the_record_verification(self):
        rig = LockRig(arch=TURING, fail_get_after_set=True)
        run = self.both(rig, self.verify())
        self.assertEqual(run["result"], ("raise", "RuntimeError",
                                         "V/F lock write returned OK but its verification read failed; RECOVERY "
                                         "REQUIRED: V/F lock recovery getter failed; refusing to write blind"))
        self.assertEqual(run["recovery"], {"domain": 6, "previous": (0, 0), "requested": (3, 737500),
                                           "verification": True})

    def test_a_refused_set_leaves_nothing_pending(self):
        run = self.both(LockRig(arch=TURING, set_status=-5), self.verify())
        self.assertEqual(run["result"][0], "raise")
        self.assertIn("V/F lock write failed (status -5)", run["result"][2])
        self.assertIsNone(run["recovery"])

    def test_a_restore_failure_stays_pending_and_retryable(self):
        rig = LockRig(arch=TURING)
        original = copy.deepcopy(rig.table)

        def scenario(side, rig):
            def body(side, rig, check):
                rig.set_status = -1  # the restoring write is refused
            outcome = attempt(lambda: self.verify(body)(side, rig))
            pending = side.recovery()
            rig.set_status = 0
            return outcome, pending, side.gpu.recover_vf_lock(), side.gpu.vf_lock_recovery_pending()
        run = self.both(rig, scenario)
        outcome, pending, retried, still = run["result"][1]
        self.assertEqual(outcome, ("raise", "RuntimeError",
                                   "verification P0 hold RESTORE FAILED: V/F lock recovery write failed (status -1)"))
        self.assertEqual(pending, {"domain": 6, "previous": (0, 0), "requested": (3, 737500), "verification": True})
        self.assertEqual((retried, still), ((True, "previous V/F lock state restored and verified"), False))
        self.assertEqual((run["state"][0], run["recovery"]), (original, None))

    def test_a_changed_hold_fails_the_check_and_the_concurrent_request_is_kept(self):
        def body(side, rig, check):
            rig.table[6]["volt_uV"] = 850000
            check()
        run = self.both(LockRig(arch=TURING), self.verify(body))
        self.assertEqual(run["result"], ("raise", "RuntimeError",
                                         "verification P0 hold RESTORE FAILED: V/F lock target changed "
                                         "concurrently; leaving its current request untouched"))
        self.assertEqual(run["state"][0][6]["volt_uV"], 850000)
        self.assertEqual(run["recovery"]["verification"], True)

    def test_unreadable_entry_non_off_entry_or_absent_domain_write_nothing(self):
        unavailable = ("raise", "RuntimeError", "verification V/F target is unavailable or already in use")

        def stop_answering(rig):
            rig.get_status = -1  # the curve read comes between the status read and the entry read
        run = self.both(LockRig(arch=TURING), self.verify(), prepare=stop_answering)
        self.assertEqual((run["result"], self.writes(run)), (unavailable, []))

        for row in (entry(6, 9, 0), entry(6, 2, 1500000), entry(5)):
            with self.subTest(row=row):
                rig = LockRig(arch=TURING)
                reset = rig.reset

                def changed(row=row):
                    reset()
                    rig.table[6] = row
                rig.reset = changed
                run = self.both(rig, self.verify())
                self.assertEqual((run["result"], self.writes(run)), (unavailable, []))

    def test_an_unreadable_table_refuses_with_the_status_error(self):
        rig = LockRig(arch=TURING)
        reset = rig.reset

        def failing():
            reset()
            rig.get_status = -1
        rig.reset = failing
        run = self.both(rig, self.verify())
        self.assertEqual(run["result"], ("raise", "RuntimeError", "V/F lock getter failed; the current lock is unknown"))

    def test_no_curve_point_at_or_below_the_voltage(self):
        run = self.both(LockRig(arch=TURING), self.verify(), vcore=(0, 699000))
        self.assertEqual((run["result"], self.writes(run)),
                         (("raise", "RuntimeError", "no V/F point at or below the loaded voltage"), []))
        # an empty curve, a refused rail read, a zero rail reading
        for curve, vcore in (([], (0, 743000)), (CURVE, (-5, 743000)), (CURVE, (0, 0))):
            with self.subTest(curve=curve, vcore=vcore):
                run = self.both(LockRig(arch=TURING), self.verify(), curve=curve, vcore=vcore)
                self.assertEqual((run["result"], self.writes(run)), (("raise", "RuntimeError",
                                                 "cannot select a loaded V/F point for verification"), []))
                self.assertEqual(run["calls"].count(self.VCORE), 1)

    def test_a_pending_recovery_refuses_before_any_read(self):
        record = {"domain": 6, "previous": (0, 0), "requested": (3, 737500)}

        def scenario(side, rig):
            side.set_recovery(record)
            outcome = attempt(lambda: self.verify()(side, rig))
            kept = side.recovery()
            side.set_recovery(None)
            return outcome, kept
        run = self.both(LockRig(arch=TURING), scenario)
        self.assertEqual(run["result"], ("ok", (("raise", "RuntimeError", "V/F lock recovery is pending"), record)))
        self.assertEqual(run["calls"], [])


CURVE = VerificationP0Tests.CURVE


class AbiArgumentTests(unittest.TestCase):
    """NULL and size checks leave the caller's struct untouched."""

    def tearDown(self):
        native.set_export_resolver(None)

    def test_null_and_size_checks(self):
        library = nl.load()
        rig = LockRig(arch=TURING)
        with rig.native_gpu() as gpu:
            r = nl._Recovery()
            r.size, r.pending = 0, 7
            self.assertEqual(library.dc_gpu_vf_lock_recovery_get(gpu._handle, ctypes.byref(r)), native.DC_E_ARGUMENT)
            self.assertEqual((r.size, r.pending), (0, 7))
            self.assertEqual(library.dc_gpu_vf_lock_recovery_set(gpu._handle, ctypes.byref(r)), native.DC_E_ARGUMENT)
            self.assertIsNone(nl.vf_lock_recovery(gpu))
            self.assertEqual(library.dc_gpu_vf_lock_recovery_get(gpu._handle, None), native.DC_E_ARGUMENT)
            self.assertEqual(library.dc_gpu_vf_lock_entry_get(gpu._handle, 6, None), native.DC_E_ARGUMENT)
            self.assertEqual(library.dc_legacy_p0_owned(gpu._handle, None), native.DC_E_ARGUMENT)
            self.assertEqual(library.dc_legacy_p0_supported(None, ctypes.byref(ctypes.c_int32())), native.DC_E_ARGUMENT)
            self.assertEqual(library.dc_legacy_p0_hold(gpu._handle, None), native.DC_E_ARGUMENT)
            self.assertEqual(library.dc_legacy_p0_release(gpu._handle, None), native.DC_E_ARGUMENT)
            self.assertEqual(library.dc_legacy_p0_request(gpu._handle, None, None), native.DC_E_ARGUMENT)
            e = nl._PointLock(9, 9, 9, 9)
            self.assertEqual(library.dc_gpu_vf_lock_entry_get(gpu._handle, 42, ctypes.byref(e)), native.DC_E_UNAVAILABLE)
            self.assertEqual((e.domain, e.lock_mode, e.volt_uv, e.count), (9, 9, 9, 9))
            self.assertEqual(nl._vf_lock_entry(gpu, 6), (0, 0))

    def test_recovery_record_round_trip(self):
        rig = LockRig(arch=TURING)
        with rig.native_gpu() as gpu:
            for record in ({"domain": 6, "previous": (3, 875000), "requested": (3, 900000)},
                           {"domain": -1, "previous": (0, 0), "requested": (3, 0xFFFFFFFF), "verification": True}):
                nl.set_vf_lock_recovery(gpu, record)
                self.assertEqual(nl.vf_lock_recovery(gpu), record)
                self.assertTrue(gpu.vf_lock_recovery_pending())
            nl.set_vf_lock_recovery(gpu, None)
            self.assertIsNone(nl.vf_lock_recovery(gpu))
            self.assertFalse(gpu.vf_lock_recovery_pending())


if __name__ == "__main__":
    unittest.main()
