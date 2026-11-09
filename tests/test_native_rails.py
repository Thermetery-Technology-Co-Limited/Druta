# Druta - parity tests for the native voltage-rail getters.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native voltage-rail getters (increment R2) against nvbackend.GPU.

druta.native_rails runs dc_rails_blocks / dc_rails_deltas / dc_rails_state / dc_rails_live_mv /
dc_rails_caches_get / dc_rails_refresh. The originals, GPU._read_volt_rail_blocks,
_read_volt_rail_deltas, read_volt_rail_state, read_rail_live_mv and the cache reset of
_refresh_volt_rail_capabilities, run on the nvbackend.GPU identity twin of
tests/test_native_target_binding.py (Card.python_twin), whose nvapi binds VoltRailsCtlGet
(0xA3070DB0) and VoltRailsAbs (0x5D0634EE) through the real NvAPI binder over the same fake
QueryInterface table the core resolves them from. One stateful fake answers both getters in the
record layout of tests/test_volt_rails.py::FakeRailAPI (the control record at
(0x48 + rail * 0x54) // 4 words is [type] + deltas, the live record
[type, live, reliability, alternate, overvoltage, effective, vmin], header word 2 the boost).

Each scenario runs twice from the same fake state, once per side, with one stepped clock (Python's
time.monotonic() patched, the core's dc_set_clock_hook). Every operation's result, the getter call
log it caused (getter, NVAPI handle, version word, mask word and the whole 8192-byte buffer the
getter received) and the four caches afterwards must be identical. The fixture values are inputs
to the read logic, not claims about any board.

Unreachable natively, and so covered only by the Python rail tests: a getter that raises (Python
records status None and str(exc) when ctypes turns a Windows structured exception inside the getter
into OSError; the core has no such guard on any NVAPI call, so the native error is always None), a
getter name other than the two (Python looks any attribute up on its NVAPI object; the ABI takes
DC_RAILS_CONTROL or DC_RAILS_ABS and the binding raises ValueError) and a non-int status. The
binding also refuses, before any call, a non-int requested version (TypeError) and one outside
0..0xFFFFFFFF (ValueError), and raises TypeError for an unhashable live_mv rail before the state
read (Python after it).

The core's NVAPI readiness is fixed when its handle is built, so a scenario that turns NVAPI off
after the caches are filled lowers the twin's `ok` on the Python side and drops both getters from
the QueryInterface table on the native side (Side.nvapi_off)."""
import copy
import ctypes
import random
import unittest
from unittest import mock

from druta import native, nvbackend as NV
from tests.test_native_clkdom import Block
from tests.test_native_target_binding import Card

if not native.available():
    raise unittest.SkipTest("native core not built (native/build.sh linux)")

from druta import native_rails as nr  # noqa: E402

CDECL = ctypes.CFUNCTYPE
CONTROL_ID, ABS_ID = 0xA3070DB0, 0x5D0634EE
V2, V1 = 0x00020AC8, 0x00010AC8
SIZE = 8192
KINDS = {"VoltRailsCtlGet": "control", "VoltRailsAbs": "live"}
CACHES = ("_volt_rail_masks_cache", "_volt_rail_retry_after", "_volt_rail_getter_diagnostics",
          "_volt_rail_read_versions")
STOCK = [1, 800000, 1068750, 1093750, 1125000, 1068750, 650000]
MSVDD = [3, 850000, 1040000, 1060000, 1200000, 1040000, 800000]


class Fake:
    """Both rail getters over one record set.

    control / live: {rail: values}; a rail missing here answers -8 (absent)
    accept:  {kind: versions the getter understands}; any other version answers -9
    script:  {kind: [entry per call]}: None answers normally, an int is returned without answering,
             ("echo", word, value) answers and then overwrites that u32 word
    boost:   the live block's header word 2 (microvolts): one word for every rail, or {rail: word}
    control_type: {rail: the control record's first (signed) word}; 0 for a rail not listed
    empty_absent: the control getter answers 0 without writing a record for a rail missing from
             control (the R470 V1 behaviour of tests/test_volt_rails_legacy.py)
    hook:    hook(fake, kind) runs as each call arrives, before it is answered (state changes
             between the samples of one read)
    """

    def __init__(self, *, control=None, live=None, accept=None, boost=0, control_type=None, empty_absent=False):
        self.control = copy.deepcopy({0: [0, 0, 0, 0]} if control is None else control)
        self.control_type = dict(control_type or {})
        self.live = copy.deepcopy({0: list(STOCK)} if live is None else live)
        self.accept = {"control": {V2}, "live": {V1}}
        self.accept.update(copy.deepcopy(accept or {}))
        self.script = {"control": [], "live": []}
        self.boost = boost
        self.empty_absent = empty_absent
        self.hook = None
        self.calls = []

    def answer(self, kind, handle, pointer):
        words = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32))
        self.calls.append((kind, handle, words[0], words[1], Block(ctypes.string_at(pointer, SIZE))))
        if self.hook is not None:
            self.hook(self, kind)
        entry = self.script[kind].pop(0) if self.script[kind] else None
        if isinstance(entry, int):
            return entry
        if words[0] not in self.accept[kind]:
            return -9
        if words[1] not in (1, 2):
            return -8
        rail = 0 if words[1] == 1 else 1
        records = self.control if kind == "control" else self.live
        if rail not in records:
            return 0 if kind == "control" and self.empty_absent else -8
        values = [self.control_type.get(rail, 0)] + records[rail] if kind == "control" else records[rail]
        signed = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_int32))
        base = (0x48 + rail * 0x54) // 4
        for index, value in enumerate(values):
            if kind == "control":
                signed[base + index] = value
            else:
                words[base + index] = value
        if kind == "live":
            words[2] = self.boost.get(rail, 0) if isinstance(self.boost, dict) else self.boost
        if entry is not None:
            _, word, value = entry
            words[word] = value
        return 0


class Rig:
    """A Card serving both getters from self.fake, the native handle and its Python twin."""

    python_side = native_side = None  # the Side classes run() uses; PythonSide and NativeSide when None
    card_class = None                 # the Card class; Card when None
    fake_class = None                 # the Fake class; Fake when None

    def __init__(self, *, with_control=True, with_live=True, nvapi_init=0, tick=0.0, card=None, **fake):
        self.config = fake
        self.tick = tick  # seconds the clock advances after each sample, on both sides
        self.samples = 0  # clock samples taken by the side that ran last
        self.fake = (self.fake_class or Fake)(**fake)
        self.card = (self.card_class or Card)(nvapi_init=nvapi_init, **(card or {}))
        self.clock = [100.0]

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def control(handle, pointer):
            return self.fake.answer("control", handle, pointer)

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def live(handle, pointer):
            return self.fake.answer("live", handle, pointer)

        self._keep = (control, live)
        self.getters = {}
        if with_control:
            self.getters[CONTROL_ID] = control
        if with_live:
            self.getters[ABS_ID] = live
        self.card.nvapi.interfaces.update(self.getters)
        self.native = self.card.native_gpu()
        self.twin = self.card.python_twin(self.native)
        self.twin_ok = self.twin.nvapi.ok
        self.bind()

    def bind(self):
        """Every side starts with the getters and the NVAPI readiness the rig was built with."""
        for ident in (CONTROL_ID, ABS_ID):
            self.card.nvapi.interfaces.pop(ident, None)
        self.card.nvapi.interfaces.update(self.getters)
        api = self.twin.nvapi
        api.ok = self.twin_ok
        # as NvAPI.__init__ binds them
        api.VoltRailsCtlGet = api._i(CONTROL_ID, NV.PTR, NV.PTR)
        api.VoltRailsAbs = api._i(ABS_ID, NV.PTR, NV.PTR)

    def now(self):
        self.samples += 1
        value = self.clock[0]
        self.clock[0] += self.tick
        return value

    def close(self):
        self.native.close()
        native.set_clock_hook(None)
        native.set_export_resolver(None)

    def run(self, scenario):
        """(python trace, native trace) of scenario(side, fake, clock), each from a fresh fake.

        The Fake and the clock are rebuilt for each side, but the Card (its NVML and NVAPI exports,
        and the architecture answers they give) is ONE object shared by the two sides: the Python
        side runs first and the native side sees whatever card state it left. A scenario that
        changes card state (side.rig.card) must therefore set that state itself at its start, so
        that both sides begin from the same card."""
        traces = []
        self.sample_counts = []
        for side in ((self.python_side or PythonSide)(self.twin, self),
                     (self.native_side or NativeSide)(self.native, self)):
            self.bind()
            self.fake = (self.fake_class or Fake)(**self.config)
            self.clock[0] = 100.0
            self.samples = 0
            side.fake = self.fake
            if isinstance(side, PythonSide):
                with mock.patch("druta.nvbackend.time.monotonic", side_effect=self.now):
                    scenario(side, self.fake, self.clock)
            else:
                native.set_clock_hook(self.now)
                try:
                    scenario(side, self.fake, self.clock)
                finally:
                    native.set_clock_hook(None)
            traces.append(side.trace)
            self.sample_counts.append(self.samples)
        self.bind()
        return traces


class Side:
    """Runs one operation, recording (operation, result, getter calls, caches afterwards)."""

    def __init__(self, rig):
        self.trace = []
        self.fake = None
        self.rig = rig

    def _record(self, operation, call):
        self.fake.calls.clear()
        result = call()
        self.trace.append((operation, result, list(self.fake.calls), self.caches()))
        return result

    def blocks(self, function, version):
        return self._record(("blocks", function, version),
                            lambda: {r: Block(b) for r, b in self._blocks(function, version).items()})

    def deltas(self):
        return self._record(("deltas",), self._deltas)

    def state(self):
        return self._record(("state",), self._state)

    def live_mv(self, rail):
        return self._record(("live_mv", rail), lambda: self._live_mv(rail))

    def refresh(self):
        return self._record(("refresh",), self._refresh)

    def drop_getter(self, function):
        """The getter disappears from this side's NVAPI (the caches stay as they are)."""
        self.rig.card.nvapi.interfaces.pop(CONTROL_ID if function == "VoltRailsCtlGet" else ABS_ID, None)
        self._drop(function)


class PythonSide(Side):
    def __init__(self, gpu, rig):
        super().__init__(rig)
        self.gpu = gpu
        for name in CACHES:  # a fresh GPU: the constructor's {} for the mask cache, nothing else
            if hasattr(gpu, name):
                delattr(gpu, name)
        gpu._volt_rail_masks_cache = {}

    def _blocks(self, function, version):
        return {r: bytes(b) for r, b in self.gpu._read_volt_rail_blocks(function, version).items()}

    def _deltas(self):
        return self.gpu._read_volt_rail_deltas()

    def _state(self):
        return self.gpu.read_volt_rail_state()

    def _live_mv(self, rail):
        return self.gpu.read_rail_live_mv(rail)

    def _refresh(self):
        self.gpu._refresh_volt_rail_capabilities()

    def _drop(self, function):
        setattr(self.gpu.nvapi, function, None)

    def nvapi_off(self):
        self.gpu.nvapi.ok = False

    def caches(self):
        return {name: copy.deepcopy(getattr(self.gpu, name, {})) for name in CACHES}


class NativeSide(Side):
    def __init__(self, gpu, rig):
        super().__init__(rig)
        self.gpu = gpu
        nr.refresh(gpu)

    def _blocks(self, function, version):
        return nr.read_volt_rail_blocks(self.gpu, function, version)

    def _deltas(self):
        return nr.read_volt_rail_deltas(self.gpu)

    def _state(self):
        return nr.read_volt_rail_state(self.gpu)

    def _live_mv(self, rail):
        return nr.read_rail_live_mv(self.gpu, rail)

    def _refresh(self):
        nr.refresh(self.gpu)

    def _drop(self, function):
        pass  # the core resolves the getter through QueryInterface on every read

    def nvapi_off(self):
        # The core's NVAPI readiness is fixed when the handle is built (Nvapi::disable runs only
        # from pairing) and the ABI has no hook to lower it later, so the native side loses both
        # getters instead: the same `!(a.ok && fn)` early return, reached through fn.
        for ident in (CONTROL_ID, ABS_ID):
            self.rig.card.nvapi.interfaces.pop(ident, None)

    def caches(self):
        return nr.caches(self.gpu)


def masks(trace):
    return [(call[0], call[2], call[3]) for _, _, calls, _ in trace for call in calls]


class ParityTest(unittest.TestCase):
    rig_options = {}

    def rig(self, **options):
        rig = Rig(**options)
        self.addCleanup(rig.close)
        return rig

    def parity(self, scenario, **options):
        self.last_rig = rig = self.rig(**options)
        python, core = rig.run(scenario)
        self.assertEqual(len(python), len(core))
        for step, (p, c) in enumerate(zip(python, core)):
            with self.subTest(step=step, operation=p[0]):
                self.assertEqual(p[0], c[0])
                self.assertEqual(p[1], c[1], "result")
                self.assertEqual(p[2], c[2], "getter calls")
                self.assertEqual(p[3], c[3], "caches")
        # no read ever selects both rails at once
        self.assertTrue(all(mask in (1, 2) for _, _, mask in masks(python)))
        return python


class ReadTests(ParityTest):
    def test_zero_control_record_is_present_and_combined_mask_is_never_used(self):
        def scenario(side, fake, clock):
            side.deltas()
            side.blocks("VoltRailsCtlGet", V2)

        trace = self.parity(scenario)
        self.assertEqual(trace[0][1], {0: {"type": 0, "reliability": 0.0, "alt_reliability": 0.0,
                                           "overvoltage": 0.0, "vmin": 0.0}})
        # rail 1 is absent (-8): two attempts, no V1 fallback
        self.assertEqual(masks(trace[:1]), [("control", V2, 1), ("control", V2, 2), ("control", V2, 2)])
        self.assertEqual(trace[0][3]["_volt_rail_masks_cache"], {"VoltRailsCtlGet": (0,)})
        self.assertEqual(trace[0][3]["_volt_rail_retry_after"], {("VoltRailsCtlGet", 1): 102.0})
        self.assertEqual(trace[0][3]["_volt_rail_getter_diagnostics"], {"VoltRailsCtlGet": {
            0: {"version": "0x00020AC8", "status": 0, "valid": True, "attempts": 1, "error": None},
            1: {"version": "0x00020AC8", "status": -8, "valid": False, "attempts": 2, "error": None}}})
        # within the backoff only the known rail is asked again
        self.assertEqual(masks(trace[1:]), [("control", V2, 1)])

    def test_signed_deltas_and_both_rails(self):
        def scenario(side, fake, clock):
            side.deltas()
            fake.control[1][3] = -2147483648
            side.deltas()

        trace = self.parity(scenario, control={0: [6250, -12500, 0, 1], 1: [-50000, 0, 25000, -1]})
        self.assertEqual(trace[0][1][1]["reliability"], -50.0)

    def test_live_rail_uses_its_own_mask_cache_and_positional_identity(self):
        def scenario(side, fake, clock):
            side.state()
            side.deltas()
            side.state()
            side.live_mv(0)
            side.live_mv(1)

        # the type words are swapped: the index still names the rail
        trace = self.parity(scenario, control={0: [0, 0, 0, 0], 1: [0, 0, 0, 0]},
                            live={0: list(MSVDD), 1: list(STOCK)}, boost=6250)
        self.assertEqual(trace[0][1][0]["type"], 3)
        self.assertEqual(trace[0][1][1]["type"], 1)
        self.assertEqual(trace[0][1][0]["_boost_mv"], 6.25)
        self.assertEqual(trace[3][1], 850.0)
        self.assertEqual(trace[4][1], 800.0)
        self.assertEqual(trace[2][3]["_volt_rail_masks_cache"], {"VoltRailsAbs": (0, 1), "VoltRailsCtlGet": (0, 1)})

    def test_all_zero_live_record_is_not_reported(self):
        def scenario(side, fake, clock):
            side.state()
            side.live_mv(1)
            fake.live[1] = [3, 0, 0, 0, 7, 7, 7]  # live and reliability both zero
            side.state()
            fake.live[0] = [1, 0, 0, 0, 0, 0, 0]
            side.state()
            side.live_mv(0)
            fake.live[0] = [1, 0, 5, 0, 0, 0, 0]  # reliability alone keeps the record
            side.state()
            side.live_mv(0)
            side.live_mv(2)
            side.live_mv(-1)

        trace = self.parity(scenario, live={0: list(STOCK), 1: [3, 0, 0, 0, 0, 0, 0]})
        self.assertEqual(set(trace[0][1]), {0})
        self.assertIsNone(trace[1][1])
        self.assertIsNone(trace[3][1])
        self.assertEqual(trace[5][1][0]["live"], 0.0)
        # the zero record was still read: both rails stay in the mask cache
        self.assertEqual(trace[3][3]["_volt_rail_masks_cache"], {"VoltRailsAbs": (0, 1)})

    def test_transient_first_failure_gets_one_immediate_retry(self):
        def scenario(side, fake, clock):
            fake.script["control"] = [-1]
            side.deltas()

        trace = self.parity(scenario)
        self.assertEqual(masks(trace)[:2], [("control", V2, 1), ("control", V2, 1)])
        self.assertEqual(trace[0][3]["_volt_rail_getter_diagnostics"]["VoltRailsCtlGet"][0]["attempts"], 2)
        self.assertEqual(set(trace[0][1]), {0})

    def test_failed_discovery_backs_off_two_seconds_then_recovers(self):
        def scenario(side, fake, clock):
            fake.script["control"] = [-1] * 4
            side.deltas()                 # both rails fail twice
            side.deltas()                 # no call before the retry time
            clock[0] = 101.999
            side.deltas()
            clock[0] = 102.0
            side.deltas()                 # rail 0 recovers, rail 1 is absent
            clock[0] = 103.0
            side.deltas()                 # rail 0 known; rail 1 still backing off
            clock[0] = 104.0
            side.deltas()

        trace = self.parity(scenario)
        self.assertEqual(len(trace[0][2]), 4)
        self.assertEqual(trace[1][2], [])
        self.assertEqual(trace[2][2], [])
        self.assertEqual(trace[3][1], {0: {"type": 0, "reliability": 0.0, "alt_reliability": 0.0,
                                           "overvoltage": 0.0, "vmin": 0.0}})
        self.assertEqual(masks(trace[4:5]), [("control", V2, 1)])
        self.assertEqual(masks(trace[5:]), [("control", V2, 1), ("control", V2, 2), ("control", V2, 2)])

    def test_v1_fallback_after_incompatible_version_only_and_remembered(self):
        def scenario(side, fake, clock):
            side.deltas()
            side.deltas()                 # the remembered V1 is asked first
            side.blocks("VoltRailsCtlGet", V2)

        trace = self.parity(scenario, accept={"control": {V1}})
        self.assertEqual(masks(trace[:1]), [("control", V2, 1), ("control", V1, 1),
                                            ("control", V2, 2), ("control", V1, 2), ("control", V1, 2)])
        self.assertEqual(masks(trace[1:2]), [("control", V1, 1)])
        self.assertEqual(trace[0][3]["_volt_rail_read_versions"], {"VoltRailsCtlGet": V1})
        self.assertEqual(trace[0][3]["_volt_rail_getter_diagnostics"]["VoltRailsCtlGet"][0]["version"], "0x00010AC8")

    def test_no_fallback_after_other_failures(self):
        for status in (-8, -1, -5):
            with self.subTest(status=status):
                def scenario(side, fake, clock, status=status):
                    fake.script["control"] = [status] * 2
                    side.deltas()

                trace = self.parity(scenario)
                self.assertEqual([m for m in masks(trace) if m[2] == 1], [("control", V2, 1)] * 2)

    def test_absolute_getter_never_falls_back(self):
        def scenario(side, fake, clock):
            side.state()
            clock[0] = 103.0
            side.blocks("VoltRailsAbs", 0x00021620)
            side.blocks("VoltRailsCtlGet", 0x00012345)  # an unknown requested version is tried first

        trace = self.parity(scenario, accept={"live": {0x00021620}})
        self.assertIsNone(trace[0][1])
        self.assertEqual(masks(trace[:1]), [("live", V1, 1), ("live", V1, 2)])
        self.assertEqual(masks(trace[1:2]), [("live", 0x00021620, 1)] + [("live", 0x00021620, 2)] * 2)
        self.assertEqual(masks(trace[2:])[:2], [("control", 0x00012345, 1), ("control", V2, 1)])

    def test_preferred_version_is_tried_first_for_the_live_getter(self):
        def scenario(side, fake, clock):
            side.blocks("VoltRailsAbs", 0x00021620)
            clock[0] = 110.0
            side.state()  # remembered 0x00021620 first, then the requested V1

        self.parity(scenario, accept={"live": {0x00021620}})

    def test_previously_read_rail_is_kept_through_transient_failure(self):
        def scenario(side, fake, clock):
            side.deltas()
            fake.script["control"] = [-1] * 2
            side.deltas()                 # nothing read, nothing fabricated; rail 0 stays known
            side.deltas()                 # rail 0 is asked again at once
            fake.control.pop(0)
            fake.script["control"] = []
            side.deltas()

        trace = self.parity(scenario)
        self.assertEqual(trace[1][1], {})
        self.assertEqual(trace[1][3]["_volt_rail_masks_cache"], {"VoltRailsCtlGet": (0,)})
        self.assertEqual(masks(trace[2:3]), [("control", V2, 1)])
        self.assertEqual(trace[3][1], {})

    def test_success_with_wrong_echo_is_not_valid(self):
        for entry in (("echo", 0, V2), ("echo", 1, 3), ("echo", 1, 0), ("echo", 0, 0)):
            with self.subTest(entry=entry):
                def scenario(side, fake, clock, entry=entry):
                    fake.script["live"] = [entry, entry]
                    side.state()
                    fake.script["live"] = [entry]
                    clock[0] = 103.0
                    side.state()

                trace = self.parity(scenario)
                self.assertIsNone(trace[0][1])
                self.assertEqual(trace[0][3]["_volt_rail_getter_diagnostics"]["VoltRailsAbs"][0],
                                 {"version": "0x00010AC8", "status": 0, "valid": False, "attempts": 2,
                                  "error": None})
                self.assertEqual(set(trace[1][1]), {0})

    def test_unavailable_nvapi_or_missing_getter_reads_nothing(self):
        def scenario(side, fake, clock):
            side.deltas()
            side.state()
            side.live_mv(0)
            side.blocks("VoltRailsCtlGet", V2)

        for options in ({"nvapi_init": -1}, {"with_control": False, "with_live": False}):
            with self.subTest(**options):
                trace = self.parity(scenario, **options)
                self.assertEqual([t[1] for t in trace], [{}, None, None, {}])
                self.assertEqual(masks(trace), [])
                self.assertEqual(trace[-1][3], {name: {} for name in CACHES})

    def test_control_type_word_decodes_signed(self):
        def scenario(side, fake, clock):
            side.deltas()

        for types in ({0: -1, 1: -2147483648}, {0: 2147483647, 1: -2}):
            with self.subTest(types=types):
                trace = self.parity(scenario, control={0: [1, 2, 3, 4], 1: [5, 6, 7, 8]}, control_type=types)
                self.assertEqual({rail: row["type"] for rail, row in trace[0][1].items()}, types)
                self.assertEqual(trace[0][1][1]["vmin"], 0.008)

    def test_boost_word_is_read_from_each_rails_own_block(self):
        def scenario(side, fake, clock):
            side.state()
            fake.boost = {0: 0xFFFFFFFF, 1: 0}  # the driver's boost word changes between reads
            side.state()

        trace = self.parity(scenario, live={0: list(STOCK), 1: list(MSVDD)},
                            boost={0: 6250, 1: 0x80000000 | 12500})
        self.assertEqual(trace[0][1][0]["_boost_mv"], 6.25)
        self.assertEqual(trace[0][1][1]["_boost_mv"], (0x80000000 | 12500) / 1000.0)
        self.assertEqual(trace[1][1][0]["_boost_mv"], 0xFFFFFFFF / 1000.0)
        self.assertEqual(trace[1][1][1]["_boost_mv"], 0.0)

    def test_live_record_fields_are_unsigned_and_in_order(self):
        record = [0x80000001, 0x80000000 + 900000, 0xFFFFFFFF, 0x80000000, 0xC0000000, 0x90000007, 0xA0000000]

        def scenario(side, fake, clock):
            side.state()
            side.live_mv(0)

        trace = self.parity(scenario, live={0: record, 1: [3, 875000, 0x80000000 + 1050000, 1, 2, 1040000, 3]})
        row = dict(trace[0][1][0])
        row.pop("_boost_mv")
        self.assertEqual(row, {"type": 0x80000001, "live": (0x80000000 + 900000) / 1000.0,
                               "reliability": 0xFFFFFFFF / 1000.0, "alt_reliability": 0x80000000 / 1000.0,
                               "overvoltage": 0xC0000000 / 1000.0, "effective": 0x90000007 / 1000.0,
                               "vmin": 0xA0000000 / 1000.0})
        self.assertEqual(trace[0][1][1]["reliability"], (0x80000000 + 1050000) / 1000.0)
        self.assertEqual(trace[0][1][1]["effective"], 1040.0)
        self.assertEqual(trace[1][1], (0x80000000 + 900000) / 1000.0)

    def test_retry_time_comes_from_one_clock_sample_before_the_first_call(self):
        # The clock advances 0.5 s after every sample, on both sides: a reader that sampled it per
        # rail, per version or per attempt would store a later retry time.
        def failing(side, fake, clock):
            fake.script["control"] = [-1] * 4  # both rails fail twice
            side.deltas()
            side.state()                       # live rail 1 absent, two attempts

        def fallback(side, fake, clock):
            side.deltas()                      # V2 answers -9 for both rails, V1 then -8 twice each

        cases = ((failing, {}, [{("VoltRailsCtlGet", 0): 102.0, ("VoltRailsCtlGet", 1): 102.0},
                                {("VoltRailsCtlGet", 0): 102.0, ("VoltRailsCtlGet", 1): 102.0,
                                 ("VoltRailsAbs", 1): 102.5}]),
                 (fallback, {"control": {}, "accept": {"control": {V1}}},
                  [{("VoltRailsCtlGet", 0): 102.0, ("VoltRailsCtlGet", 1): 102.0}]))
        for scenario, options, expected in cases:
            with self.subTest(scenario=scenario.__name__):
                trace = self.parity(scenario, tick=0.5, **options)
                self.assertEqual([step[3]["_volt_rail_retry_after"] for step in trace], expected)
                self.assertEqual(self.last_rig.sample_counts, [len(expected)] * 2)
                if scenario is fallback:
                    self.assertEqual(masks(trace), [("control", V2, 1), ("control", V1, 1), ("control", V1, 1),
                                                    ("control", V2, 2), ("control", V1, 2), ("control", V1, 2)])

    def test_failure_then_incompatible_version_still_falls_back(self):
        def control(side, fake, clock):
            fake.script["control"] = [-1, -9]
            side.deltas()

        trace = self.parity(control, accept={"control": {V1, V2}})
        self.assertEqual(masks(trace)[:3], [("control", V2, 1), ("control", V2, 1), ("control", V1, 1)])
        self.assertEqual(set(trace[0][1]), {0})
        self.assertEqual(trace[0][3]["_volt_rail_read_versions"], {"VoltRailsCtlGet": V1})

        def live(side, fake, clock):
            fake.script["live"] = [-1, -9]
            side.state()

        trace = self.parity(live)
        self.assertEqual(trace[0][3]["_volt_rail_getter_diagnostics"]["VoltRailsAbs"][0],
                         {"version": "0x00010AC8", "status": -9, "valid": False, "attempts": 2, "error": None})
        self.assertEqual([m for m in masks(trace) if m[2] == 1], [("live", V1, 1)] * 2)

    def test_live_mv_looks_the_rail_up_as_a_dict_does(self):
        keys = (0, 1, 1.0, True, False, 1.5, "1", 2 ** 32, 2 ** 32 + 1, None, -1, 2)

        def scenario(side, fake, clock):
            for key in keys:
                side.live_mv(key)

        trace = self.parity(scenario, live={0: list(STOCK), 1: list(MSVDD)})
        self.assertEqual([step[1] for step in trace],
                         [800.0, 850.0, 850.0, 850.0, 800.0, None, None, None, None, None, None, None])
        # the state is read for a key that names no rail, as Python reads it before the lookup
        self.assertTrue(all(step[2] for step in trace))

    def test_live_mv_unhashable_rail_raises_type_error(self):
        def scenario(side, fake, clock):
            fake.calls.clear()
            with self.assertRaises(TypeError):
                side._live_mv([1])
            side.trace.append(len(fake.calls))

        python, core = self.rig(live={0: list(STOCK), 1: list(MSVDD)}).run(scenario)
        self.assertGreater(python[0], 0)  # Python raises after the read
        self.assertEqual(core, [0])       # the binding raises before it

    def test_unavailable_nvapi_or_missing_getter_keeps_filled_caches(self):
        def fill(side, fake, clock):
            side.deltas()  # V1 fallback remembered; control rail 1 absent and backing off
            side.state()   # live rail 1 absent and backing off
            clock[0] = 103.0  # past every retry time

        def nvapi_off(side, fake, clock):
            fill(side, fake, clock)
            side.nvapi_off()
            side.deltas()
            side.state()
            side.live_mv(0)
            side.blocks("VoltRailsCtlGet", V2)
            side.blocks("VoltRailsAbs", V1)

        def control_gone(side, fake, clock):
            fill(side, fake, clock)
            side.drop_getter("VoltRailsCtlGet")
            side.deltas()
            side.blocks("VoltRailsCtlGet", V2)

        def live_gone(side, fake, clock):
            fill(side, fake, clock)
            side.drop_getter("VoltRailsAbs")
            side.state()
            side.live_mv(0)
            side.blocks("VoltRailsAbs", V1)

        for scenario in (nvapi_off, control_gone, live_gone):
            with self.subTest(scenario=scenario.__name__):
                trace = self.parity(scenario, accept={"control": {V1}})
                filled = trace[1][3]
                self.assertTrue(all(filled[name] for name in CACHES))
                for step in trace[2:]:
                    self.assertIn(step[1], ({}, None))
                    self.assertEqual(step[2], [])
                    self.assertEqual(step[3], filled)

    def test_missing_control_getter_leaves_the_live_getter(self):
        def scenario(side, fake, clock):
            side.deltas()
            side.state()

        trace = self.parity(scenario, with_control=False)
        self.assertEqual(trace[0][1], {})
        self.assertEqual(set(trace[1][1]), {0})

    def test_refresh_clears_the_caches_and_retries_at_once(self):
        def scenario(side, fake, clock):
            fake.script["control"] = [-9, 0]  # V1 fallback, remembered
            side.deltas()
            side.state()
            side.refresh()
            side.deltas()                     # back at V2 first; rail 1 retried despite the backoff
            side.state()

        trace = self.parity(scenario, accept={"control": {V1, V2}})
        self.assertEqual(trace[2][3], {name: {} for name in CACHES})
        self.assertEqual(masks(trace[3:4])[0], ("control", V2, 1))
        self.assertIn(("control", V2, 2), masks(trace[3:4]))

    def test_random_scripts(self):
        rng = random.Random(0x5D0634EE)
        choices = [None, None, None, 0, -1, -5, -8, -9, ("echo", 0, V1), ("echo", 1, 3), ("echo", 0, V2)]
        for case in range(60):
            seed = rng.random()
            accept = {"control": set(rng.sample([V1, V2, 0x00012345], rng.randint(0, 2))),
                      "live": set(rng.sample([V1, 0x00021620], rng.randint(0, 2)))}
            control = {r: [rng.randint(-60000, 60000) for _ in range(4)] for r in (0, 1) if rng.random() < 0.7}
            live = {r: [rng.choice((1, 3)), rng.choice((0, 900000)), rng.choice((0, 1050000)), 1, 2, 3, 4]
                    for r in (0, 1) if rng.random() < 0.7}

            def scenario(side, fake, clock, seed=seed):
                local = random.Random(seed)
                for _ in range(10):
                    for kind in ("control", "live"):
                        fake.script[kind] = [local.choice(choices) for _ in range(local.randint(0, 6))]
                    clock[0] += local.choice((0.0, 0.5, 1.999, 2.0, 3.0))
                    operation = local.randrange(7)
                    if operation == 0:
                        side.deltas()
                    elif operation == 1:
                        side.state()
                    elif operation == 2:
                        side.live_mv(local.choice((0, 1, 2)))
                    elif operation == 3:
                        side.blocks("VoltRailsCtlGet", local.choice((V1, V2, 0x00012345)))
                    elif operation == 4:
                        side.blocks("VoltRailsAbs", local.choice((V1, 0x00021620)))
                    elif operation == 5 and local.random() < 0.3:
                        side.refresh()
                    else:
                        side.deltas()

            with self.subTest(case=case):
                self.parity(scenario, accept=accept, control=control, live=live, boost=rng.choice((0, 6250)))


class AbiArgumentTests(unittest.TestCase):
    def setUp(self):
        self.rig = Rig()
        self.addCleanup(self.rig.close)
        self.lib = nr.load()
        self.handle = self.rig.native._handle

    def new(self, struct, size=None):
        value = struct()
        value.size = ctypes.sizeof(struct) if size is None else size
        return value

    def test_refusals(self):
        lib, h, byref = self.lib, self.handle, ctypes.byref
        E = native.DC_E_ARGUMENT
        blocks = self.new(nr._BlockSet)
        self.assertEqual(lib.dc_rails_blocks(None, 0, V2, byref(blocks)), E)
        self.assertEqual(lib.dc_rails_blocks(h, 0, V2, None), E)
        for function in (-1, 2, 0x7FFFFFFF):
            self.assertEqual(lib.dc_rails_blocks(h, function, V2, byref(blocks)), E)
        for struct, entry in ((nr._BlockSet, lib.dc_rails_blocks), (nr._DeltaSet, lib.dc_rails_deltas),
                              (nr._StateSet, lib.dc_rails_state), (nr._Caches, lib.dc_rails_caches_get)):
            for size in (0, ctypes.sizeof(struct) - 1, ctypes.sizeof(struct) + 1):
                value = self.new(struct, size)
                args = (h, 0, V2, byref(value)) if entry is lib.dc_rails_blocks else (h, byref(value))
                self.assertEqual(entry(*args), E)
        self.assertEqual(lib.dc_rails_deltas(None, byref(self.new(nr._DeltaSet))), E)
        self.assertEqual(lib.dc_rails_deltas(h, None), E)
        self.assertEqual(lib.dc_rails_state(None, byref(self.new(nr._StateSet))), E)
        self.assertEqual(lib.dc_rails_state(h, None), E)
        self.assertEqual(lib.dc_rails_caches_get(None, byref(self.new(nr._Caches))), E)
        self.assertEqual(lib.dc_rails_caches_get(h, None), E)
        has, mv = ctypes.c_int32(), ctypes.c_double()
        self.assertEqual(lib.dc_rails_live_mv(None, 0, byref(has), byref(mv)), E)
        self.assertEqual(lib.dc_rails_live_mv(h, 0, None, byref(mv)), E)
        self.assertEqual(lib.dc_rails_live_mv(h, 0, byref(has), None), E)
        self.assertEqual(lib.dc_rails_refresh(None), E)
        # a refused call touches neither the driver nor the caches
        self.assertEqual(self.rig.fake.calls, [])
        self.assertEqual(nr.caches(self.rig.native), {name: {} for name in CACHES})
        with self.assertRaises(ValueError):
            nr.read_volt_rail_blocks(self.rig.native, "VoltRailsStatus", V2)
        self.assertEqual(self.rig.fake.calls, [])

    def test_out_of_range_or_non_int_version_is_refused_before_any_call(self):
        for function in nr.FUNCTIONS:
            for version, error in ((float(V1), TypeError), (-1, ValueError), (2 ** 32, ValueError)):
                with self.subTest(function=function, version=version):
                    with self.assertRaises(error):
                        nr.read_volt_rail_blocks(self.rig.native, function, version)
                    self.assertEqual(self.rig.fake.calls, [])
                    self.assertEqual(nr.caches(self.rig.native), {name: {} for name in CACHES})
        # the bounds themselves are accepted
        for version in (0, 0xFFFFFFFF):
            nr.refresh(self.rig.native)  # past the backoff the first read leaves
            nr.read_volt_rail_blocks(self.rig.native, "VoltRailsAbs", version)
        self.assertEqual({call[2] for call in self.rig.fake.calls}, {0, 0xFFFFFFFF})
        # Python refuses a float too, once it builds a request
        with self.assertRaises(TypeError):
            self.rig.twin._read_volt_rail_blocks("VoltRailsAbs", float(V1))

    def test_struct_sizes_match_the_header(self):
        # The ctypes mirrors are accepted by the size checks, so they match the C layout's sizes.
        lib, h, byref = self.lib, self.handle, ctypes.byref
        self.assertEqual(lib.dc_rails_blocks(h, 0, V2, byref(self.new(nr._BlockSet))), native.DC_OK)
        self.assertEqual(lib.dc_rails_deltas(h, byref(self.new(nr._DeltaSet))), native.DC_OK)
        self.assertEqual(lib.dc_rails_state(h, byref(self.new(nr._StateSet))), native.DC_OK)
        self.assertEqual(lib.dc_rails_caches_get(h, byref(self.new(nr._Caches))), native.DC_OK)


if __name__ == "__main__":
    unittest.main()
