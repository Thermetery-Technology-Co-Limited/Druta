# Druta - parity tests for the native voltage-rail reference capture and profile.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native rail reference and the readers over it (increment R3) against nvbackend.GPU.

druta.native_rails runs dc_rails_limits / dc_rails_profile / dc_rails_diagnostics /
dc_rails_limits_supported / dc_rails_limit_fields / dc_rails_stock_limit_mv / dc_rails_limits_mv.
The originals, GPU.read_volt_rail_limits, _volt_rail_profile, volt_rail_diagnostics,
volt_rail_limits_supported, volt_rail_limit_fields, stock_limit_mv and volt_rail_limits_mv, run on
the identity twin of tests/test_native_rails.py, whose arch() is the real method over the same fake
nvmlDeviceGetArchitecture (or NvAPI_GPU_GetArchInfo) the core reads.

The rig is the one of tests/test_native_rails.py: one Fake serves both rail getters to both sides.
Its records reproduce tests/test_volt_rails.py::FakeRailAPI (control deltas over per-card bases,
the live record [type, 800000, bases + deltas, min of the first three, vmin] that sync_live
writes) and the R470 getter of tests/test_volt_rails_legacy.py (V1 only, an empty success for an
absent rail). A hook on the Fake changes the driver's answers between the two samples of one read.
Every operation records its result (compared with dict key order and value types, NaN equal to
NaN), the getter call log, the four caches, the number of architecture reads it caused, and the
profile afterwards (the reference and the first-read record as _volt_rail_profile shows them).
The fixture values are inputs to the read logic, not claims about any board.

Not reachable natively, and covered only by the Python rail tests: an unhashable rail argument
(the binding raises TypeError before any read; Python after its reads, and not at all where its
profile is None) and the writer's diagnostics keys (last_write_error, observed_packets,
write_protocol), which belong to the rail writer that is not ported yet."""
import copy
import ctypes
import math
import unittest

from druta import native, nvbackend as NV
from tests.test_native_rails import CACHES, CDECL, V1, V2, NativeSide, PythonSide, Rig
from tests.test_native_target_binding import GET_ARCH_INFO, Card

if not native.available():
    raise unittest.SkipTest("native core not built (native/build.sh linux)")

from druta import native_rails as nr  # noqa: E402

FIELDS = ("reliability", "alt_reliability", "overvoltage", "vmin")
LIVE_TYPE = {0: 1, 1: 3}
BASES = {"turing": (1068750, 1093750, 1125000, 650000),
         "pascal": (1068750, 1093750, 1200000, 650000),
         "blackwell": (1040000, 1060000, 1200000, 800000)}
ARCH = {"pascal": 4, "turing": 6, "ampere": 7, "blackwell": 10}
TEXT = {
    1: "GPU architecture could not be detected. Retry detection.",
    2: "Rail limit control is not yet understood for this GPU architecture.",
    3: "The driver did not return this rail's control settings. Retry detection.",
    4: "The driver did not return a valid rail status. Retry detection.",
    5: "Waiting for stable voltage readings to establish this rail's reference.",
}
NO_ARCH = {"arch_status": 1}  # NVML refuses, no NVAPI architecture interface: arch() is None


class CountingCard(Card):
    """A Card that counts its architecture reads (both sides read the same fake exports)."""

    reads = 0

    @property
    def arch(self):
        self.reads += 1
        return self._arch

    @arch.setter
    def arch(self, value):
        self._arch = value

    @property
    def nvapi_arch(self):
        self.reads += 1
        return self._nvapi_arch

    @nvapi_arch.setter
    def nvapi_arch(self, value):
        self._nvapi_arch = value


class ArchInfoV1Card(CountingCard):
    """A CountingCard whose NvAPI_GPU_GetArchInfo understands only the V1 struct: V2 answers -9
    (NVAPI_INCOMPATIBLE_STRUCT_VERSION) and V1 answers self.nvapi_arch."""

    def __init__(self, **options):
        super().__init__(**options)

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def arch_info(handle, pointer):
            info = NV._GpuArchInfo.from_address(pointer)
            if info.version != NV.NvAPI.ver(NV._GpuArchInfo, 1):
                return -9
            info.architecture = self.nvapi_arch
            return 0
        self._arch_info_v1 = arch_info
        self.nvapi.interfaces[GET_ARCH_INFO] = arch_info


def live_record(rail, bases, deltas):
    """FakeRailAPI.sync_live's record for one rail."""
    reliability, alternate, overvoltage, vmin = [b + d for b, d in zip(bases, deltas)]
    return [LIVE_TYPE[rail], 800000, reliability, alternate, overvoltage,
            min(reliability, alternate, overvoltage), vmin]


def sync(fake, bases):
    """FakeRailAPI.sync_live: the live limits follow the control deltas (type and live stay)."""
    for rail, deltas in fake.control.items():
        if rail in fake.live:
            fake.live[rail][2:] = live_record(rail, bases, deltas)[2:]


def config(kind="turing", *, rails=None, bases=None, control=None, architecture=None, legacy=False):
    """Rig options for tests/test_volt_rails.py::fake_gpu(kind) (legacy: legacy_gpu(kind))."""
    rails = rails or ((0, 1) if kind == "blackwell" else (0,))
    if control is None:
        control = {r: [0, 0, 0, 0] for r in rails}
        if kind == "blackwell" and 1 in control:
            control[1][0] = -50000
    bases = bases or BASES[kind]
    live = {r: live_record(r, bases, control[r]) for r in rails if r in control}
    card = {"arch": ARCH[kind] if architecture is None else architecture}
    options = {"control": control, "live": live, "card": card}
    if legacy:
        if kind == "pascal":  # R470's V1 control block uses the 1068.75 mV reliability base
            live[0][2] = 1068750
            live[0][5] = min(live[0][2:5])
        options.update(accept={"control": {V1}}, control_type={0: 1}, empty_absent=True)
    return options


def shape(value):
    """value with dict key order and leaf types made comparable, NaN equal to NaN."""
    if isinstance(value, dict):
        return ("dict", [(shape(k), shape(v)) for k, v in value.items()])
    if isinstance(value, (list, tuple)):
        return (type(value).__name__, [shape(v) for v in value])
    if isinstance(value, float) and math.isnan(value):
        return ("float", "nan")
    return (type(value).__name__, value)


class RefSide:
    """Records (operation, result, getter calls, caches, architecture reads, profile afterwards)."""

    def _record(self, operation, call):
        self.fake.calls.clear()
        card = self.rig.card
        before = card.reads
        try:
            result = copy.deepcopy(call())
        except Exception as exc:  # the exception is the observable
            result = ("raises", type(exc).__name__, str(exc))
        reads = card.reads - before
        calls = list(self.fake.calls)
        self.trace.append((operation, result, calls, self.caches(), reads, copy.deepcopy(self._profile())))
        return result

    def limits(self):
        return self._record(("limits",), self._limits)

    def profile(self):
        return self._record(("profile",), self._profile)

    def diagnostics(self):
        return self._record(("diagnostics",), self._diagnostics)

    def supported(self, *rail):
        return self._record(("supported",) + rail, lambda: self._supported(*rail))

    def fields(self, rail):
        return self._record(("fields", rail), lambda: self._fields(rail))

    def stock(self, rail, key):
        return self._record(("stock", rail, key), lambda: self._stock(rail, key))

    def limits_mv(self):
        return self._record(("limits_mv",), self._limits_mv)


class RefPythonSide(RefSide, PythonSide):
    def __init__(self, gpu, rig):
        super().__init__(gpu, rig)
        for name in ("_arch_cache", "_arch_source"):  # never detected yet
            if hasattr(gpu, name):
                delattr(gpu, name)
        gpu._volt_rail_reference = {}  # as GPU.__init__ sets them
        gpu._volt_rail_initial_uv = {}
        gpu.static = {key: rig.native.static[key] for key in ("slot", "name", "driver", "vbios")}

    def _limits(self):
        return self.gpu.read_volt_rail_limits()

    def _profile(self):
        return self.gpu._volt_rail_profile()

    def _diagnostics(self):
        return self.gpu.volt_rail_diagnostics()

    def _supported(self, *rail):
        return self.gpu.volt_rail_limits_supported(*rail)

    def _fields(self, rail):
        return self.gpu.volt_rail_limit_fields(rail)

    def _stock(self, rail, key):
        return self.gpu.stock_limit_mv(rail, key)

    def _limits_mv(self):
        return self.gpu.volt_rail_limits_mv()


class RefNativeSide(RefSide, NativeSide):
    def _limits(self):
        return nr.read_volt_rail_limits(self.gpu)

    def _profile(self):
        return nr.volt_rail_profile(self.gpu)

    def _diagnostics(self):
        return nr.volt_rail_diagnostics(self.gpu)

    def _supported(self, *rail):
        return nr.volt_rail_limits_supported(self.gpu, *rail)

    def _fields(self, rail):
        return nr.volt_rail_limit_fields(self.gpu, rail)

    def _stock(self, rail, key):
        return nr.stock_limit_mv(self.gpu, rail, key)

    def _limits_mv(self):
        return nr.volt_rail_limits_mv(self.gpu)


class RefRig(Rig):
    card_class = CountingCard
    python_side = RefPythonSide
    native_side = RefNativeSide


class ArchInfoV1Rig(RefRig):
    card_class = ArchInfoV1Card


def after_live(change):
    """A hook running change(fake) once, on the first control call after the first live call:
    the driver's answers change between the first sample and the second."""
    seen = {"live": False, "done": False}

    def hook(fake, kind):
        if kind == "live":
            seen["live"] = True
        elif seen["live"] and not seen["done"]:
            seen["done"] = True
            change(fake)
    return hook


def masks(trace):
    """(getter kind, version word, mask word) of every getter call in trace."""
    return [(call[0], call[2], call[3]) for step in trace for call in step[2]]


def row(trace, step, rail=0):
    return trace[step][1][rail]


class ParityTest(unittest.TestCase):
    def parity(self, scenario, rig_class=RefRig, **options):
        rig = rig_class(**options)
        self.addCleanup(rig.close)
        python, core = rig.run(scenario)
        self.assertEqual(len(python), len(core))
        for step, (p, c) in enumerate(zip(python, core)):
            with self.subTest(step=step, operation=p[0]):
                self.assertEqual(p[0], c[0])
                p_result, c_result = p[1], c[1]
                if p[0] == ("diagnostics",):
                    # the getters cache: equal contents (the binding rebuilds it in sorted order)
                    p_result, c_result = dict(p_result), dict(c_result)
                    self.assertEqual(p_result.pop("getters"), c_result.pop("getters"), "getters")
                self.assertEqual(shape(p_result), shape(c_result), "result")
                self.assertEqual(p[2], c[2], "getter calls")
                self.assertEqual(p[3], c[3], "caches")
                self.assertEqual(p[4], c[4], "architecture reads")
                self.assertEqual(shape(p[5]), shape(c[5]), "profile afterwards")
        self.assertTrue(all(mask in (1, 2) for _, _, mask in masks(python)))
        return python


FIRST_READ = [("control", V2, 1), ("control", V2, 2), ("control", V2, 2),
              ("live", V1, 1), ("live", V1, 2), ("live", V1, 2)]
SECOND_SAMPLE = [("control", V2, 1), ("live", V1, 1)]


class ReferenceCaptureTests(ParityTest):
    def test_paired_sample_captures_the_reference_and_first_read_record(self):
        def scenario(side, fake, clock):
            side.limits()
            side.limits()

        trace = self.parity(scenario, **config("turing"))
        first = row(trace, 0)
        self.assertEqual(list(first), ["type", *FIELDS, "_base_mv", "_headroom_mv", "_absolute_ok", "_boost_mv",
                                       "_effective_mv", "_reference_kind"])
        self.assertEqual(first["_base_mv"], dict(zip(FIELDS, (1068.75, 1093.75, 1125.0, 650.0))))
        self.assertIs(first["_absolute_ok"], True)
        self.assertEqual(first["_effective_mv"], 1068.75)
        self.assertEqual(first["_reference_kind"], "first-read estimate (driver status is quantized)")
        # the paired second sample: rail 1 is backing off, so only rail 0 is asked again
        self.assertEqual(masks(trace[:1]), FIRST_READ + SECOND_SAMPLE)
        self.assertEqual(trace[0][4], 1)
        self.assertEqual(trace[0][5]["poweron"], {0: (0, 0, 0, 0)})
        # a referenced rail needs no second sample
        self.assertEqual(masks(trace[1:]), SECOND_SAMPLE)
        self.assertEqual(row(trace, 1)["_base_mv"], first["_base_mv"])

    def test_bases_subtract_the_deltas_and_the_boost_from_reliability_only(self):
        def scenario(side, fake, clock):
            side.limits()

        control = {0: [23000, -12000, 9100, 47000], 1: [-77000, 5500, -19000, 31500]}
        options = config("blackwell", control=control, bases=(987625, 1087125, 1173375, 612500))
        trace = self.parity(scenario, boost=6250, **options)
        self.assertEqual(row(trace, 0)["_base_mv"], {"reliability": 987.625 - 6.25, "alt_reliability": 1087.125,
                                                     "overvoltage": 1173.375, "vmin": 612.5})
        self.assertEqual(row(trace, 0)["_boost_mv"], 6.25)
        self.assertEqual(trace[0][5]["poweron"], {0: (23000, -12000, 9100, 47000), 1: (-77000, 5500, -19000, 31500)})

    def test_reliability_base_is_the_absolute_minus_the_delta_then_minus_the_boost(self):
        """absolute - delta - boost, evaluated left to right: the fixture is one where
        absolute - (delta + boost) and absolute - boost - delta give three different doubles."""
        def scenario(side, fake, clock):
            side.limits()

        absolute, delta, boost = 1126894, 59236, 49956  # microvolts
        a, d, b = absolute / 1000.0, delta / 1000.0, boost / 1000.0
        self.assertEqual(len({a - d - b, a - (d + b), a - b - d}), 3)
        options = config("turing", control={0: [delta, 0, 0, 0]}, bases=(absolute - delta, 1093750, 1125000, 650000))
        trace = self.parity(scenario, boost=boost, **options)
        self.assertEqual(options["live"][0][2], absolute)
        self.assertEqual(row(trace, 0)["_base_mv"]["reliability"], a - d - b)
        self.assertEqual(trace[0][5]["bases"][0]["reliability"], a - d - b)

    def test_first_read_record_rounds_each_delta_back_to_its_microvolts(self):
        """The first-read record is round(delta / 1000.0 * 1000), the nearest integer, on both sides.

        i32 / 1000.0 * 1000 is not always the integer it came from: 1001 and 2026 come back a hair
        below it (truncation and floor both lose a microvolt), -65526 a hair above it (truncation
        toward zero loses one) and -2007 a hair below it (floor loses one); 2007 lands a hair above
        and 7, -3 and 999999 round-trip exactly. Whether
        the nearest-integer rounding breaks ties to even is not observable on this path: an int32
        divided by 1000.0 and multiplied by 1000 never lands on x.5, so no fixture can tell
        round-half-even from round-half-away."""
        def scenario(side, fake, clock):
            side.limits()

        cases = (
            ("blackwell", {0: [1001, -65526, 2026, 7], 1: [-3, 999999, -2007, 2007]}, None),
            ("turing", {0: [-2147483648, 2147483647, 1, -1]}, [1068750, 1093750, 1125000, 1068750, 650000]),
        )
        for kind, control, live in cases:
            with self.subTest(control=control):
                options = config(kind, control=control)
                if live:  # the extremes would leave the u32 live record: keep the stock one
                    options["live"][0][2:] = live
                trace = self.parity(scenario, **options)
                expected = {r: tuple(round(v / 1000.0 * 1000) for v in values) for r, values in control.items()}
                self.assertEqual(expected, {r: tuple(values) for r, values in control.items()})
                self.assertEqual(trace[0][5]["poweron"], expected)
        # the fixture separates nearest from truncation and from floor, on both signs
        values = [v for _, control, _ in cases for record in control.values() for v in record]
        self.assertEqual([v for v in values if int(v / 1000.0 * 1000) != v], [1001, -65526, 2026])
        self.assertEqual([v for v in values if math.floor(v / 1000.0 * 1000) != v], [1001, 2026, -2007])

    def test_a_second_control_sample_differing_in_any_field_leaves_no_reference(self):
        for index in range(-1, 4):  # -1: the type word
            with self.subTest(index=index):
                def change(fake, index=index):
                    if index < 0:
                        fake.control_type[0] = 7
                    else:
                        fake.control[0][index] += 1

                def scenario(side, fake, clock):
                    fake.hook = after_live(change)
                    side.limits()
                    fake.hook = None
                    side.limits()  # stable now: captured from the changed deltas

                trace = self.parity(scenario, **config("turing"))
                self.assertEqual(row(trace, 0)["_base_mv"], {})
                self.assertIsNone(trace[0][5]["bases"].get(0))
                self.assertEqual(trace[0][5]["poweron"], {})
                self.assertEqual(masks(trace[:1]), FIRST_READ + SECOND_SAMPLE)
                self.assertTrue(row(trace, 1)["_base_mv"])
                self.assertEqual(masks(trace[1:]), SECOND_SAMPLE * 2)

    def test_the_state_comparison_covers_the_four_fields_the_boost_and_the_type_only(self):
        # live record words: 0 type, 1 live, 2..4 the first three fields, 5 effective, 6 vmin
        for index, compared in ((0, True), (1, False), (2, True), (3, True), (4, True), (5, False), (6, True)):
            with self.subTest(index=index):
                def change(fake, index=index):
                    fake.live[0][index] += 1000

                def scenario(side, fake, clock):
                    fake.hook = after_live(change)
                    side.limits()

                trace = self.parity(scenario, **config("turing"))
                self.assertEqual(bool(row(trace, 0)["_base_mv"]), not compared)

    def test_a_boost_change_between_the_samples_leaves_no_reference(self):
        def scenario(side, fake, clock):
            fake.hook = after_live(lambda fake: setattr(fake, "boost", 6250))
            side.limits()
            fake.hook = None
            side.limits()

        trace = self.parity(scenario, **config("turing"))
        self.assertEqual(row(trace, 0)["_base_mv"], {})
        self.assertEqual(row(trace, 0)["_boost_mv"], 0.0)
        self.assertEqual(row(trace, 1)["_base_mv"]["reliability"], 1068.75 - 6.25)

    def test_a_rail_missing_from_the_second_state_or_control_read_is_not_referenced(self):
        for kind in ("live", "control"):
            with self.subTest(missing=kind):
                def scenario(side, fake, clock, kind=kind):
                    fake.hook = after_live(lambda fake: getattr(fake, kind).pop(1))
                    side.limits()

                trace = self.parity(scenario, **config("blackwell"))
                self.assertTrue(row(trace, 0, 0)["_base_mv"])
                self.assertEqual(row(trace, 0, 1)["_base_mv"], {})
                self.assertIs(row(trace, 0, 1)["_absolute_ok"], True)  # the first state had it
                self.assertEqual(list(trace[0][5]["bases"]), [0])

    def test_first_read_record_is_set_once_and_kept_across_reads_and_refresh(self):
        """Rail 0's record and reference survive a changed delta and a refresh; rail 1 records its own.

        Not counted as coverage here, because R3 cannot observe them: setdefault versus assignment
        of the first-read record (a reference and its record are always set together and neither
        is ever cleared, so a rail with a record is never asked to capture again), and the boost's
        finite/non-negative check (the boost is a u32 / 1000.0, always finite and non-negative)."""
        def scenario(side, fake, clock):
            saved = fake.control.pop(1)
            fake.live.pop(1)
            side.supported(0)
            fake.control[0][0] = 12500
            fake.control[1] = saved
            fake.live[1] = [3, 800000, 0, 0, 0, 0, 0]
            sync(fake, BASES["blackwell"])
            side.limits()     # rail 1 still backing off
            side.refresh()
            side.supported(1)
            side.limits()
            side.profile()

        trace = self.parity(scenario, **config("blackwell"))
        self.assertIs(trace[0][1], True)
        self.assertEqual(trace[0][5]["poweron"], {0: (0, 0, 0, 0)})
        self.assertIs(trace[3][1], True)
        self.assertEqual(trace[-1][1]["poweron"], {0: (0, 0, 0, 0), 1: (-50000, 0, 0, 0)})
        # sync() keeps live = base + delta, so a recapture from the changed delta would give the same
        # 1040.0 base: this value cannot show a recapture. A recapture is caught by the getter-call
        # comparison of parity() (a capture takes a second sample) and by the unchanged record above.
        self.assertEqual(row(trace, 4)["_base_mv"]["reliability"], 1040.0)
        self.assertEqual(trace[2][3], {name: {} for name in CACHES})
        self.assertTrue(trace[2][5]["bases"])

    def test_profile_dicts_keep_the_order_the_rails_were_referenced_in(self):
        def scenario(side, fake, clock):
            fake.live[0][0] = 3  # rail 0's state is invalid: only rail 1 is referenced
            side.limits()
            fake.live[0][0] = 1
            side.limits()
            side.profile()

        trace = self.parity(scenario, **config("blackwell"))
        self.assertEqual(list(trace[0][5]["bases"]), [1])
        for key in ("bases", "headroom_mv", "poweron", "fields"):
            self.assertEqual(list(trace[-1][1][key]), [1, 0])

    def test_unsupported_or_unknown_architecture_takes_no_second_sample(self):
        for card in ({"arch": 9}, {"arch": 5}, NO_ARCH):
            with self.subTest(card=card):
                def scenario(side, fake, clock):
                    side.limits()
                    side.limits()
                    side.profile()

                options = config("turing")
                options["card"] = card
                trace = self.parity(scenario, **options)
                self.assertEqual(masks(trace[:1]), FIRST_READ)
                self.assertEqual(row(trace, 0)["_base_mv"], {})
                self.assertEqual(row(trace, 0)["reliability"], 0.0)
                self.assertIsNone(trace[-1][1])
                # an unrecognized architecture is read again each time (never cached)
                self.assertEqual(trace[1][4], 1 if card is NO_ARCH else 0)

    def test_the_architecture_is_read_only_when_a_rail_needs_a_reference(self):
        def scenario(side, fake, clock):
            fake.live[0][0] = 3
            side.limits()   # invalid state: nothing needs a reference
            fake.live[0][0] = 1
            side.limits()
            fake.control.pop(0)
            clock[0] = 103.0
            side.limits()   # nothing read

        options = config("turing")
        options["card"] = NO_ARCH
        trace = self.parity(scenario, **options)
        self.assertEqual([step[4] for step in trace], [0, 1, 0])
        self.assertIsNone(trace[2][1])

    def test_v1_control_drops_rails_absent_from_the_state(self):
        def scenario(side, fake, clock):
            side.limits()
            side.limits_mv()

        trace = self.parity(scenario, **config("turing", legacy=True))
        self.assertEqual(set(trace[0][1]), {0})
        self.assertEqual(trace[0][3]["_volt_rail_masks_cache"]["VoltRailsCtlGet"], (0, 1))

    def test_v2_keeps_a_controlled_rail_without_state_with_placeholders(self):
        def scenario(side, fake, clock):
            fake.live.pop(1)
            side.limits()
            side.limits_mv()

        trace = self.parity(scenario, **config("blackwell"))
        missing = row(trace, 0, 1)
        self.assertEqual((missing["_absolute_ok"], missing["_boost_mv"], missing["_effective_mv"], missing["_base_mv"]),
                         (False, 0.0, None, {}))
        self.assertTrue(math.isnan(trace[1][1][1]["reliability"]))

    def test_v1_control_drops_a_referenced_rail_once_it_leaves_the_state_and_keeps_an_invalid_one(self):
        def scenario(side, fake, clock):
            side.limits()              # both rails referenced under V1
            saved = fake.live.pop(1)
            side.limits()              # rail 1 is not in the state: dropped although referenced
            fake.live[1] = saved
            fake.live[1][0] = 42       # in the state again, with an invalid type: kept
            side.limits()
            side.supported(1)

        trace = self.parity(scenario, **config("blackwell", legacy=True))
        self.assertEqual(trace[0][3]["_volt_rail_read_versions"]["VoltRailsCtlGet"], V1)
        self.assertEqual(list(trace[0][5]["bases"]), [0, 1])
        self.assertEqual(list(trace[0][1]), [0, 1])
        self.assertEqual(list(trace[1][1]), [0])
        self.assertEqual(list(trace[2][1]), [0, 1])
        self.assertTrue(row(trace, 2, 1)["_base_mv"])
        self.assertIs(row(trace, 2, 1)["_absolute_ok"], True)
        self.assertIs(trace[3][1], False)

    def test_v2_empty_success_for_an_absent_rail_keeps_a_zero_row_without_state(self):
        """An empty success outside V1 is a present control record of zero deltas: no V1 presence
        filter applies, so rail 1 stays as a row whose absolute figures are placeholders."""
        def scenario(side, fake, clock):
            side.limits()
            side.diagnostics()
            side.fields(1)
            side.limits_mv()

        trace = self.parity(scenario, empty_absent=True, **config("turing"))
        self.assertEqual(trace[0][3]["_volt_rail_read_versions"]["VoltRailsCtlGet"], V2)
        missing = row(trace, 0, 1)
        self.assertEqual({k: missing[k] for k in ("type", *FIELDS)}, {"type": 0, **{k: 0.0 for k in FIELDS}})
        self.assertEqual((missing["_absolute_ok"], missing["_base_mv"], missing["_effective_mv"]), (False, {}, None))
        self.assertEqual(trace[1][1]["rails"][1], {"available": False, "reason": TEXT[4]})
        self.assertEqual(trace[2][1], ())
        self.assertTrue(all(math.isnan(v) for v in trace[3][1][1].values()))


class ProfileTests(ParityTest):
    def test_profile_shape_before_and_after_the_first_read(self):
        def scenario(side, fake, clock):
            side.profile()
            side.limits()
            side.profile()

        trace = self.parity(scenario, **config("turing"))
        self.assertEqual(trace[0][1], {"bases": {}, "headroom_mv": {}, "poweron": {}, "fields": {},
                                       "control_version": V2, "settle_vmin": False, "min_mv": 650.0,
                                       "max_mv": 1200.0})
        self.assertEqual(trace[0][2], [])
        self.assertEqual(trace[2][1]["fields"], {0: FIELDS})
        self.assertEqual(trace[2][1]["headroom_mv"], {0: None})

    def test_settle_vmin_needs_pascal_and_the_v1_control_layout(self):
        for kind, legacy, settle in (("pascal", True, True), ("turing", True, False), ("pascal", False, False)):
            with self.subTest(kind=kind, legacy=legacy):
                def scenario(side, fake, clock):
                    side.limits()
                    side.profile()

                trace = self.parity(scenario, **config(kind, legacy=legacy))
                self.assertIs(trace[1][1]["settle_vmin"], settle)
                self.assertEqual(trace[1][1]["control_version"], V1 if legacy else V2)

    def test_per_card_bases_stock_values_and_absolute_view(self):
        expected = {"turing": [1068.75, 1093.75, 1125.0, 650.0], "pascal": [1068.75, 1093.75, 1200.0, 650.0],
                    "blackwell": [1040.0, 1060.0, 1200.0, 800.0]}
        for kind, values in expected.items():
            with self.subTest(card=kind):
                def scenario(side, fake, clock):
                    side.limits()
                    for key in FIELDS:
                        side.stock(0, key)
                    side.limits_mv()

                trace = self.parity(scenario, **config(kind))
                self.assertEqual([step[1] for step in trace[1:5]], values)
                self.assertEqual(trace[5][1][0], dict(zip(FIELDS, values)))
                self.assertIsNone(row(trace, 0)["_headroom_mv"])
                self.assertEqual([step[2] for step in trace[1:5]], [[]] * 4)  # the reference exists

    def test_ampere_is_a_supported_architecture(self):
        def scenario(side, fake, clock):
            side.limits()
            side.supported(0)
            side.supported(1)
            side.fields(0)

        trace = self.parity(scenario, **config("turing", architecture=ARCH["ampere"]))
        self.assertEqual([step[1] for step in trace[1:]], [True, False, FIELDS])

    def test_unknown_generation_gives_nan_limits(self):
        def scenario(side, fake, clock):
            side.limits()
            side.limits_mv()

        trace = self.parity(scenario, **config("turing", architecture=9))
        self.assertTrue(all(math.isnan(v) for v in trace[1][1][0].values()))

    def test_only_present_rails_expose_fields(self):
        for kind in ("turing", "pascal", "blackwell"):
            with self.subTest(card=kind):
                def scenario(side, fake, clock):
                    side.fields(0)
                    side.fields(1)
                    side.fields(2)

                trace = self.parity(scenario, **config(kind))
                self.assertEqual([step[1] for step in trace],
                                 [FIELDS, FIELDS if kind == "blackwell" else (), ()])

    def test_second_card_records_its_own_first_read(self):
        def scenario(side, fake, clock):
            side.limits()
            side.stock(1, "reliability")

        trace = self.parity(scenario, **config("blackwell"))
        self.assertEqual(set(trace[0][5]["poweron"]), {0, 1})
        self.assertEqual(trace[1][1], 990.0)


class QueryTests(ParityTest):
    def test_limits_supported_for_any_rail_and_each_key(self):
        keys = (0, 1, 1.0, True, False, 0.0, 2, -1, "1", 1.5, 2 ** 32, 2 ** 32 + 1)

        def scenario(side, fake, clock):
            side.supported()
            for key in keys:
                side.supported(key)

        trace = self.parity(scenario, **config("blackwell", rails=(0, 1)))
        self.assertEqual([step[1] for step in trace],
                         [True] * 7 + [False] * 6)
        # once referenced, each call reads the limits (deltas, state) and then the state again
        self.assertEqual(masks(trace[1:2]),
                         [("control", V2, 1), ("control", V2, 2)] + [("live", V1, 1), ("live", V1, 2)] * 2)

    def test_limits_supported_reads_nothing_without_a_profile(self):
        def scenario(side, fake, clock):
            side.supported()
            side.supported(0)

        trace = self.parity(scenario, **config("turing", architecture=9))
        self.assertEqual([step[1] for step in trace], [False, False])
        self.assertEqual(masks(trace), [])

    def test_limits_supported_needs_a_reference_and_a_valid_state(self):
        def scenario(side, fake, clock):
            fake.hook = after_live(lambda fake: fake.live[0].__setitem__(2, fake.live[0][2] + 1000))
            side.supported(0)            # the pair differed: no reference
            fake.hook = None
            side.supported(0)            # referenced now
            fake.live[1][0] = 42
            side.supported(1)            # valid reference, invalid state now
            side.supported()

        trace = self.parity(scenario, **config("blackwell"))
        self.assertEqual([step[1] for step in trace], [False, True, False, True])

    def test_limits_supported_for_any_rail_when_only_rail_one_is_supported(self):
        def scenario(side, fake, clock):
            fake.live[0][0] = 3  # rail 0's state type is invalid: only rail 1 is referenced
            side.supported()
            side.supported(0)
            side.supported(1)

        trace = self.parity(scenario, **config("blackwell"))
        self.assertEqual([step[1] for step in trace], [True, False, True])
        self.assertEqual(list(trace[0][5]["bases"]), [1])

    def test_limit_fields_are_empty_for_a_controlled_rail_without_a_reference(self):
        def scenario(side, fake, clock):
            fake.hook = after_live(lambda fake: fake.control[1].__setitem__(0, fake.control[1][0] + 1))
            side.fields(1)   # rail 1's control delta changed between the samples: not referenced
            fake.hook = None
            side.fields(0)
            side.fields(1)

        trace = self.parity(scenario, **config("blackwell"))
        self.assertEqual([step[1] for step in trace], [(), FIELDS, ()])
        self.assertEqual(trace[0][3]["_volt_rail_masks_cache"]["VoltRailsCtlGet"], (0, 1))
        self.assertEqual(list(trace[0][5]["bases"]), [0])

    def test_limit_fields_read_the_limits_only_while_no_rail_has_a_reference(self):
        def scenario(side, fake, clock):
            side.fields(1)
            side.fields(0)
            side.fields(1.0)
            side.fields("0")

        trace = self.parity(scenario, **config("turing"))
        self.assertEqual([step[1] for step in trace], [(), FIELDS, (), ()])
        self.assertEqual(masks(trace[:1]), FIRST_READ + SECOND_SAMPLE)
        self.assertEqual(masks(trace[1:]), [])

    def test_limit_fields_keep_reading_while_no_reference_is_captured(self):
        def scenario(side, fake, clock):
            side.fields(0)
            side.fields(0)

        trace = self.parity(scenario, **config("turing", architecture=9))
        self.assertEqual([step[1] for step in trace], [(), ()])
        self.assertEqual(masks(trace[1:]), SECOND_SAMPLE)

    def test_stock_limit_mv_for_referenced_unreferenced_and_bad_keys(self):
        def scenario(side, fake, clock):
            side.stock(1, "reliability")   # reads first; rail 1 has no first-read record
            side.stock(0, "vmin")
            side.stock(True, "alt_reliability")
            side.stock(0, "bogus")         # KeyError, after the profile and record checks
            side.stock(1, "bogus")         # None: the record check comes first
            side.stock(0, ["vmin"])        # TypeError of the unhashable key
            side.stock(2, "vmin")
            side.stock(0.0, "overvoltage")

        control = {0: [-12500, 6250, 0, 25000]}
        options = config("turing", control=control)
        trace = self.parity(scenario, **options)
        self.assertEqual([step[1] for step in trace],
                         [None, 675.0, None, ("raises", "KeyError", "'bogus'"), None,
                          ("raises", "TypeError", "unhashable type: 'list'"), None, 1125.0])
        self.assertEqual(masks(trace[:1]), FIRST_READ + SECOND_SAMPLE)
        self.assertEqual(masks(trace[1:]), [])

    def test_stock_limit_mv_adds_the_first_read_delta_to_the_base(self):
        def scenario(side, fake, clock):
            for key in FIELDS:
                side.stock(0, key)
            fake.control[0] = [0, 0, 0, 0]  # a later change is not the first read
            sync(fake, (1000000, 1050000, 1150000, 600000))
            side.limits()
            side.stock(0, "reliability")

        control = {0: [-12500, 6250, 9100, -47000]}
        trace = self.parity(scenario, **config("turing", control=control, bases=(1000000, 1050000, 1150000, 600000)))
        # the first-read absolute value: the estimated base plus the first-read delta
        self.assertEqual([step[1] for step in trace[:4]], [987.5, 1056.25, 1159.1, 553.0])
        self.assertEqual(trace[5][1], 987.5)

    def test_stock_limit_mv_divides_the_first_read_microvolts_by_a_thousand(self):
        """base + v / 1000.0, not base + v * 0.001: for these small first-read values the two give
        different doubles once added to a small base (near 1 V the difference is below one ulp)."""
        for deltas, bases in (((9, 13, 18, 26), (0, 1, 2, 0)), ((-9, -13, -18, -26), (10, 14, 19, 27))):
            with self.subTest(deltas=deltas):
                def scenario(side, fake, clock):
                    for key in FIELDS:
                        side.stock(0, key)

                trace = self.parity(scenario, **config("turing", control={0: list(deltas)}, bases=bases))
                base = [(b + v) / 1000.0 - v / 1000.0 for b, v in zip(bases, deltas)]
                expected = [x + v / 1000.0 for x, v in zip(base, deltas)]
                self.assertEqual([step[1] for step in trace], expected)
                self.assertTrue(all(x + v * 0.001 != e for x, v, e in zip(base, deltas, expected)))

    def test_stock_limit_mv_without_profile_is_none_even_for_a_bad_key(self):
        def scenario(side, fake, clock):
            side.stock(0, "bogus")
            side.stock(0, "vmin")

        trace = self.parity(scenario, **config("turing", architecture=9))
        self.assertEqual([step[1] for step in trace], [None, None])
        self.assertEqual(masks(trace[:1]), FIRST_READ)
        self.assertEqual(masks(trace[1:]), SECOND_SAMPLE)

    def test_limits_mv_is_none_without_rows_and_nan_without_a_base(self):
        def scenario(side, fake, clock):
            fake.hook = after_live(lambda fake: fake.control[1].__setitem__(3, 1))
            side.limits_mv()
            fake.hook = None
            fake.control.clear()
            clock[0] = 103.0
            fake.script["control"] = [-1] * 4
            side.limits_mv()

        trace = self.parity(scenario, **config("blackwell"))
        self.assertEqual(trace[0][1][0], {"reliability": 1040.0, "alt_reliability": 1060.0, "overvoltage": 1200.0,
                                          "vmin": 800.0})
        self.assertTrue(all(math.isnan(v) for v in trace[0][1][1].values()))
        self.assertIsNone(trace[1][1])


class DiagnosticsTests(ParityTest):
    def diagnose(self, options, prepare=None):
        def scenario(side, fake, clock):
            if prepare:
                prepare(fake)
            side.diagnostics()
            fake.hook = None

        return self.parity(scenario, **options)[0][1]

    def test_each_reason_in_precedence_order(self):
        def unknown(kind="turing", **changes):
            options = config(kind)
            options["card"] = changes.pop("card", options["card"])
            options.update(changes)
            return options

        def invalid(fake):
            fake.live[0][0] = 3

        def unstable(fake):
            fake.hook = after_live(lambda fake: fake.control[0].__setitem__(0, 1))

        cases = (
            (unknown(card=NO_ARCH, with_control=False), invalid, 1),
            (unknown(card={"arch": 9}, with_control=False), invalid, 2),
            (unknown(card={"arch": 5}), None, 2),
            (unknown(with_control=False), invalid, 3),
            (unknown(), invalid, 4),
            (unknown(), unstable, 5),
            (unknown(), None, 0),
        )
        for options, prepare, code in cases:
            with self.subTest(code=code, prepare=prepare and prepare.__name__):
                result = self.diagnose(options, prepare)
                self.assertEqual(result["rails"][0], {"available": code == 0, "reason": TEXT.get(code, "")})
                self.assertEqual((result["available"], result["reason"]), (code == 0, TEXT.get(code, "")))
                self.assertEqual(list(result), ["available", "reason", "architecture", "rails",
                                                "architecture_source", "adapter", "getters", "last_write_error",
                                                "observed_packets", "write_protocol"])

    def test_rails_are_rail_zero_and_every_rail_read(self):
        def nothing(fake):
            fake.control.clear()
            fake.live.clear()

        result = self.diagnose(config("blackwell"), nothing)
        self.assertEqual(result["rails"], {0: {"available": False, "reason": TEXT[3]}})
        self.assertEqual(result["reason"], TEXT[3])

        def only_rail_one(fake):
            fake.control.pop(0)
            fake.live.pop(0)

        result = self.diagnose(config("blackwell"), only_rail_one)
        self.assertEqual(list(result["rails"]), [0, 1])
        self.assertEqual(result["rails"][0]["reason"], TEXT[3])
        self.assertTrue(result["rails"][1]["available"])
        self.assertEqual((result["available"], result["reason"]), (True, ""))

        def state_only(fake):
            fake.control.pop(1)

        result = self.diagnose(config("blackwell"), state_only)
        self.assertEqual(result["rails"][1]["reason"], TEXT[3])

    def test_architecture_source_and_adapter(self):
        for card, source, architecture in (({"arch": 6}, "NVML", 6),
                                           ({"arch_export": False, "nvapi_arch": 0x160}, "NVAPI V2", 6),
                                           (NO_ARCH, None, None)):
            with self.subTest(source=source):
                options = config("turing")
                options["card"] = card
                result = self.diagnose(options)
                self.assertEqual((result["architecture_source"], result["architecture"]), (source, architecture))
                self.assertEqual(result["adapter"], {"slot": "0000:01:00.0", "name": "TITAN RTX",
                                                     "driver": result["adapter"]["driver"],
                                                     "vbios": "90.02.0B.00.01"})
                self.assertEqual((result["last_write_error"], result["observed_packets"], result["write_protocol"]),
                                 (None, [], None))
                self.assertEqual(set(result["getters"]), {"VoltRailsCtlGet", "VoltRailsAbs"})

    def test_architecture_source_names_the_nvapi_v1_struct(self):
        def scenario(side, fake, clock):
            side.diagnostics()
            side.diagnostics()

        options = config("turing")
        options["card"] = {"arch_export": False, "nvapi_arch": 0x160}
        trace = self.parity(scenario, rig_class=ArchInfoV1Rig, **options)
        for step in trace:
            self.assertEqual((step[1]["architecture_source"], step[1]["architecture"]), ("NVAPI V1", 6))
        self.assertTrue(trace[0][1]["available"])
        self.assertEqual([step[4] for step in trace], [1, 0])  # recognized, then cached

    def test_an_architecture_recognized_later_captures_the_reference_then(self):
        def scenario(side, fake, clock):
            card = side.rig.card
            card.arch_status = 1  # the card is shared by both sides: set its state at the start
            side.limits()
            side.diagnostics()
            card.arch_status = 0  # NVML starts answering
            side.limits()
            side.diagnostics()
            side.diagnostics()

        options = config("turing")
        options["card"] = {"arch": ARCH["turing"], "arch_status": 1}
        trace = self.parity(scenario, **options)
        self.assertEqual(masks(trace[:1]), FIRST_READ)  # unknown: no second sample
        self.assertEqual(row(trace, 0)["_base_mv"], {})
        self.assertIsNone(trace[0][5])
        self.assertEqual((trace[1][1]["architecture"], trace[1][1]["architecture_source"]), (None, None))
        self.assertEqual(trace[1][1]["reason"], TEXT[1])
        self.assertTrue(row(trace, 2)["_base_mv"])
        self.assertEqual(masks(trace[2:3]), SECOND_SAMPLE * 2)
        self.assertEqual(list(trace[2][5]["bases"]), [0])
        self.assertEqual((trace[3][1]["architecture"], trace[3][1]["architecture_source"], trace[3][1]["available"]),
                         (ARCH["turing"], "NVML", True))
        self.assertEqual([step[4] for step in trace], [1, 2, 1, 0, 0])

    def test_diagnostics_reads_the_architecture_then_the_limits_then_the_state(self):
        def scenario(side, fake, clock):
            side.diagnostics()
            side.diagnostics()

        options = config("turing")
        options["card"] = NO_ARCH
        trace = self.parity(scenario, **options)
        # arch() once itself and once inside read_volt_rail_limits (rail 0 needs a reference)
        self.assertEqual([step[4] for step in trace], [2, 2])
        self.assertEqual(masks(trace[:1]), FIRST_READ + [("live", V1, 1)])


class LegacyTests(ParityTest):
    def test_profile_bases_follow_the_runtime_version(self):
        for kind, overvoltage in (("turing", 1125.0), ("pascal", 1200.0)):
            with self.subTest(kind=kind):
                def scenario(side, fake, clock):
                    side.supported()
                    side.limits()
                    side.profile()

                trace = self.parity(scenario, **config(kind, legacy=True))
                self.assertIs(trace[0][1], True)
                self.assertEqual(row(trace, 1)["_base_mv"], {"reliability": 1068.75, "alt_reliability": 1093.75,
                                                             "overvoltage": overvoltage, "vmin": 650.0})
                self.assertEqual(row(trace, 1)["type"], 1)
                self.assertEqual(trace[2][1]["control_version"], V1)

    def test_v1_success_for_an_absent_rail_does_not_expose_it(self):
        def scenario(side, fake, clock):
            side.limits()
            side.state()
            side.fields(1)

        trace = self.parity(scenario, **config("turing", legacy=True))
        self.assertEqual(set(trace[0][1]), {0})
        self.assertEqual(set(trace[1][1]), {0})
        self.assertEqual(trace[2][1], ())
        self.assertIn(("control", V1, 2), masks(trace[:1]))

    def test_the_fallback_version_is_remembered(self):
        def scenario(side, fake, clock):
            side.limits()
            side.limits()

        trace = self.parity(scenario, **config("turing", legacy=True))
        self.assertIn(("control", V2, 1), masks(trace[:1]))
        self.assertTrue(all(version == V1 for kind, version, _ in masks(trace[1:]) if kind == "control"))


class RuntimeReferenceTests(ParityTest):
    def test_different_board_bases_and_nonzero_initial_values_are_not_stock(self):
        def scenario(side, fake, clock):
            side.limits()
            side.stock(1, "reliability")

        control = {0: [23000, -12000, 9100, 47000], 1: [-77000, 5500, -19000, 31500]}
        trace = self.parity(scenario, **config("blackwell", control=control,
                                               bases=(987625, 1087125, 1173375, 612500)))
        self.assertEqual(row(trace, 0)["_base_mv"]["reliability"], 987.625)
        self.assertEqual(trace[1][1], 910.625)

    def test_initial_floor_below_the_software_default(self):
        def scenario(side, fake, clock):
            side.stock(0, "vmin")

        trace = self.parity(scenario, **config("turing", bases=(1000000, 1050000, 1150000, 600000)))
        self.assertEqual(trace[0][1], 600.0)

    def test_an_unrelated_bad_rail_does_not_hide_the_other(self):
        def scenario(side, fake, clock):
            fake.live[1][0] = 42
            side.supported(0)
            side.supported(1)
            side.diagnostics()

        trace = self.parity(scenario, **config("blackwell"))
        self.assertEqual([trace[0][1], trace[1][1]], [True, False])
        self.assertEqual(trace[2][1]["rails"][1]["reason"], TEXT[4])

    def test_telemetry_skew_after_capture_does_not_rebase(self):
        def scenario(side, fake, clock):
            side.limits()
            fake.live[0][2] += 6250
            side.limits()

        trace = self.parity(scenario, **config("turing"))
        self.assertEqual(row(trace, 1)["_base_mv"], row(trace, 0)["_base_mv"])
        self.assertEqual(masks(trace[1:]), SECOND_SAMPLE)

    def test_a_status_change_during_pairing_creates_no_reference(self):
        def scenario(side, fake, clock):
            def hook(fake, kind):
                if kind == "live":
                    fake.live[0][2] += 1000
            fake.hook = hook
            side.limits()
            side.limits()

        trace = self.parity(scenario, **config("turing"))
        self.assertEqual(row(trace, 0)["_base_mv"], {})
        self.assertEqual(trace[-1][5]["poweron"], {})
        self.assertEqual(masks(trace[1:]), SECOND_SAMPLE * 2)


class AbiArgumentTests(unittest.TestCase):
    def setUp(self):
        self.rig = RefRig(**config("turing"))
        self.addCleanup(self.rig.close)
        self.lib = nr.load()
        self.handle = self.rig.native._handle

    def test_refusals_leave_the_output_untouched(self):
        lib, h, byref, E = self.lib, self.handle, ctypes.byref, native.DC_E_ARGUMENT
        structs = ((nr._LimitSet, lib.dc_rails_limits), (nr._Profile, lib.dc_rails_profile),
                   (nr._Diagnosis, lib.dc_rails_diagnostics), (nr._LimitsMv, lib.dc_rails_limits_mv))
        for struct, entry in structs:
            with self.subTest(entry=entry.__name__):
                self.assertEqual(entry(None, byref(nr._new(struct))), E)
                self.assertEqual(entry(h, None), E)
                for size in (0, ctypes.sizeof(struct) - 1, ctypes.sizeof(struct) + 1):
                    value = struct()
                    ctypes.memset(byref(value), 0xA5, ctypes.sizeof(struct))
                    value.size = size
                    before = bytes(value)
                    self.assertEqual(entry(h, byref(value)), E)
                    self.assertEqual(bytes(value), before)
        flag, count, has, mv = ctypes.c_int32(7), ctypes.c_uint32(7), ctypes.c_int32(7), ctypes.c_double(7.0)
        self.assertEqual(lib.dc_rails_limits_supported(None, 0, 0, byref(flag)), E)
        self.assertEqual(lib.dc_rails_limits_supported(h, 0, 0, None), E)
        self.assertEqual(lib.dc_rails_limit_fields(None, 0, byref(count)), E)
        self.assertEqual(lib.dc_rails_limit_fields(h, 0, None), E)
        self.assertEqual(lib.dc_rails_stock_limit_mv(None, 0, 0, byref(has), byref(mv)), E)
        self.assertEqual(lib.dc_rails_stock_limit_mv(h, 0, 0, None, byref(mv)), E)
        self.assertEqual(lib.dc_rails_stock_limit_mv(h, 0, 0, byref(has), None), E)
        self.assertEqual((flag.value, count.value, has.value, mv.value), (7, 7, 7, 7.0))
        # a refused call reads nothing and captures nothing
        self.assertEqual(self.rig.fake.calls, [])
        self.assertEqual(nr.volt_rail_profile(self.rig.native)["bases"], {})

    def test_unhashable_rail_raises_type_error_before_any_read(self):
        for call in (lambda: nr.volt_rail_limits_supported(self.rig.native, [0]),
                     lambda: nr.volt_rail_limit_fields(self.rig.native, [0]),
                     lambda: nr.stock_limit_mv(self.rig.native, [0], "vmin")):
            with self.assertRaises(TypeError):
                call()
        self.assertEqual(self.rig.fake.calls, [])
        # Python raises it too, after its reads
        twin = self.rig.twin
        twin._volt_rail_masks_cache = {}
        with self.assertRaises(TypeError):
            twin.volt_rail_limit_fields([0])


if __name__ == "__main__":
    unittest.main()
