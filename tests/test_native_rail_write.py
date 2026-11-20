# Druta - parity tests for the native rail write sequence (increment R4).
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native rail write sequence against nvbackend.GPU._write_rail_records and
_verify_rail_records.

druta.native_rails.write_rail_records / verify_rail_records (and the native.Gpu methods
_write_rail_records / _verify_rail_records that delegate to them) run dc_rails_write_records and
dc_rails_verify_records. The Python originals run unmodified on the identity twin of
tests/test_native_rails.py: its read_volt_rail_limits, _volt_rail_profile,
volt_rail_limits_supported, read_voltage_boost and _write_rail_records_locked are the real methods.

The rig joins the R3 reference rig (tests/test_native_rail_reference.RefRig: one Fake serving both
rail getters and the architecture to both sides) with the escape path of
tests/test_native_rail_splice.py (the locked write's parity rig): while the D3DKMTEscape hook is
patched, the VoltRailsCtlGet getter issues the rail GET packet through the thunk (the Python twin's
closure, or native.hook_test_entry), and one fake escape answers it: the GET is filled with the
Fake's current control records, a SET stores its records into the Fake (and the live record follows
the per-card bases, as FakeRailAPI.sync_live does), so a later read-back sees what was written.
NvAPI's VoltCtrlGet answers the Fake's boost. Every getter call, escape (with the packet bytes at
the forward) and boost read lands in one ordered log.

Every operation records its result, that log, the four getter caches, the architecture reads, the
profile afterwards, the writer's three attributes (_volt_rail_write_error,
_volt_rail_write_protocol, _volt_rail_observed_packets; seeded with stale values so "untouched" is
visible), and the Fake's rail state (control records, live records, boost). All of it must be equal
on the two sides. The fixture values are inputs to the logic, not claims about any board.

Recorded divergences (asserted separately, not by parity): a record value outside int32 (or not a
plain int) and a bool rail key (True for rail 1) are refused natively before any escape, with
prepare_write's text; Python issues the GET first and refuses inside the hook, so its
_volt_rail_observed_packets holds that GET's summary where the core's is empty."""
import copy
import ctypes
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from druta import native, nvbackend as NV, rail_transport
from tests.test_native_clkdom import Block
from tests.test_native_rail_reference import BASES, CountingCard, RefNativeSide, RefPythonSide, RefRig, config, shape
from tests.test_native_rail_reference import sync as sync_live
from tests.test_native_rails import CDECL, SIZE, Fake
from tests.test_native_rail_splice import GETS, SETS, rail_packet, unrelated_packet
from tests.test_native_transport import ESCAPE, KMT, install

if not native.available():
    raise unittest.SkipTest("native core not built (native/build.sh linux)")

from druta import native_rails as nr  # noqa: E402

VOLT_CTRL_GET = 0x9DF23CA1
FIELDS = dict(hAdapter=987, hDevice=0, Type=0, Flags=8, hContext=0)
ATTRS = ("_volt_rail_write_error", "_volt_rail_write_protocol", "_volt_rail_observed_packets")
STALE = ("stale error", {"stale": True}, ["stale packet"])
PRESERVE = "Could not preserve the current native rail record: "
INCOMPLETE = "write issued but the rail read-back is incomplete"
BOOST_CHANGED = "rail write changed the voltage boost setting"
LAYOUTS = {layout.packet_size: layout for layout in rail_transport.KNOWN_LAYOUTS}
PROTOCOL = {"get_command": "0x2080B213", "set_command": "0x2080F214", "packet_size": 1104}


class WriteFake(Fake):
    """Fake plus the boost, the escape driver and the SET's effect on the rail state.

    vboost / vboost_status: VoltCtrlGet's percent and status; bases: the per-card bases the live
    record follows after a SET (None: the live record is left alone); get_rm / set_rm / set_status:
    the GET's and SET's RM status words and the SET's NTSTATUS; store: store(rail, values) -> the
    values the driver keeps (a disagreeing driver); set_boost: the boost after a SET; drop_after_set:
    the rail leaves both records after a SET; scripts: per write getter call, the escapes it issues
    ("rail" or "unrelated"; default one rail GET); boost_fail_at: the VoltCtrlGet calls (1-based)
    that fail with -1 (the boost reads None)."""

    def __init__(self, *, vboost=25, vboost_status=0, bases=None, get_rm=0, set_rm=0, set_status=0, store=None,
                 set_boost=None, drop_after_set=False, scripts=None, boost_fail_at=(), **kw):
        super().__init__(**kw)
        self.vboost, self.vboost_status, self.bases = vboost, vboost_status, bases
        self.boost_fail_at, self.boost_reads = set(boost_fail_at), 0  # 1-based VoltCtrlGet calls that fail
        self.get_rm, self.set_rm, self.set_status = get_rm, set_rm, set_status
        self.store, self.set_boost, self.drop_after_set = store, set_boost, drop_after_set
        self.scripts = copy.deepcopy(list(scripts or []))
        self.rig = None
        self.on_read = None  # on_read(fake): runs at each control read while the hook is not patched

    def answer(self, kind, handle, pointer):
        rig = self.rig
        if kind == "control" and rig is not None and rig.probe():
            words = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32))
            self.calls.append(("control-write", handle, words[0], words[1], Block(ctypes.string_at(pointer, SIZE)),
                               threading.current_thread() is threading.main_thread()))
            self.calls.append(("armed", rig.probe()))
            rig.issue(words[1], self.scripts.pop(0) if self.scripts else ["rail"])
            return 0
        if kind == "control" and self.on_read is not None:
            self.on_read(self)
        return super().answer(kind, handle, pointer)


class WriteCard(CountingCard):
    """A CountingCard whose native side also serves gdi32's D3DKMTEscape and NvAPI's VoltCtrlGet."""

    rig = None

    def __init__(self, **options):
        super().__init__(**options)

        @KMT
        def escape(pointer):
            return self.rig.escape(pointer)

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def volt_get(handle, pointer):
            fake = self.rig.fake
            fake.calls.append(("boost",))
            fake.boost_reads += 1
            if fake.vboost_status:
                return fake.vboost_status
            if fake.boost_reads in fake.boost_fail_at:
                return -1
            NV._VoltBoost.from_address(pointer).percent = fake.vboost
            return 0
        self.escape_callback, self.volt_get = escape, volt_get
        self.nvapi.interfaces[VOLT_CTRL_GET] = volt_get

    def native_gpu(self):
        install(self.nvml if self.with_nvml else None, self.nvapi if self.with_nvapi else None,
                {ESCAPE: self.escape_callback})
        return native.Gpu()


class WriteSide:
    """Adds the write and verify operations and the attributes and rail state to each record."""

    def _record(self, operation, call):
        result = super()._record(operation, call)
        fake = self.fake
        self.trace[-1] = self.trace[-1] + (
            tuple(copy.deepcopy(getattr(self.gpu, name, "absent")) for name in ATTRS),
            copy.deepcopy(fake.control), copy.deepcopy(fake.live), fake.vboost)
        return result

    def seed(self):
        for name, value in zip(ATTRS, STALE):
            setattr(self.gpu, name, copy.deepcopy(value))

    def write(self, records):
        self.fake.rig = self.rig
        return self._record(("write", repr(records)), lambda: self._armed(lambda: self._write(records)))

    def verify(self, records, boost):
        self.fake.rig = self.rig
        return self._record(("verify", repr(records), boost), lambda: self._verify(records, boost))


class WritePythonSide(WriteSide, RefPythonSide):
    def _armed(self, call):
        rig = self.rig
        rig.code = ctypes.create_string_buffer(b"original bytes")
        original = bytes(rig.code)
        rig.probe = lambda: bytes(rig.code) != original
        rig.enter = lambda address: rig.py_target(address)
        kernel = SimpleNamespace(VirtualProtect=Mock(side_effect=lambda p, s, f, previous: 1),
                                 GetCurrentThreadId=Mock(side_effect=threading.get_ident))
        storage = ctypes.create_string_buffer(14)

        def load(name, *args, **kwargs):
            if name == "kernel32":
                return kernel
            return SimpleNamespace(D3DKMTEscape=KMT(ctypes.addressof(rig.code)))

        def prototype(restype, argtype):
            def bind(target):
                if callable(target):  # the closure the thunk jumps to
                    rig.py_target = target
                    return ctypes.c_void_p(ctypes.addressof(storage))
                return rig.forward  # proto(address)(pointer): the restored real function
            return bind

        try:
            with patch.object(NV.ctypes, "WinDLL", create=True, side_effect=load), \
                    patch.object(NV.ctypes, "WINFUNCTYPE", new=prototype, create=True):
                return call()
        finally:
            rig.probe = lambda: False

    def _write(self, records):
        return self.gpu._write_rail_records(records)

    def _verify(self, records, boost):
        return self.gpu._verify_rail_records(records, boost)


class WriteNativeSide(WriteSide, RefNativeSide):
    def _armed(self, call):
        rig = self.rig
        rig.probe = lambda: native.hook_test_state()["patched"]
        rig.enter = native.hook_test_entry
        try:
            return call()
        finally:
            rig.probe = lambda: False

    def _write(self, records):
        return self.gpu._write_rail_records(records)

    def _verify(self, records, boost):
        return self.gpu._verify_rail_records(records, boost)


class WriteRig(RefRig):
    card_class = WriteCard
    fake_class = WriteFake
    python_side = WritePythonSide
    native_side = WriteNativeSide

    def __init__(self, **options):
        self.probe = lambda: False
        self.enter = None
        self.py_target = None
        self.packets = []
        super().__init__(**options)
        self.card.rig = self

    def bind(self):
        super().bind()
        self.twin.nvapi.VoltCtrlGet = self.twin.nvapi._i(VOLT_CTRL_GET, NV.PTR, NV.PTR)
        self.twin.nvapi.ver = NV.NvAPI.ver

    def forward(self, address):
        """The unpatched D3DKMTEscape: the fake driver itself."""
        return self.card.escape_callback(address)

    def issue(self, mask, script):
        """The escapes one write getter call issues, through the thunk while it is patched."""
        for item in script:
            make = rail_packet(1104, mask=mask) if item == "rail" else unrelated_packet()
            packet = make()
            escape = NV.GPU._Escape(**FIELDS, pPrivateDriverData=ctypes.addressof(packet),
                                    PrivateDriverDataSize=ctypes.sizeof(packet))
            self.packets.append((packet, escape))
            route = self.enter if self.probe() else self.forward
            route(ctypes.addressof(escape))
            self.fake.calls.append(("armed", self.probe()))

    def escape(self, pointer):
        fake = self.fake
        e = NV.GPU._Escape.from_address(pointer)
        size = e.PrivateDriverDataSize
        fake.calls.append(("escape", size, ctypes.string_at(e.pPrivateDriverData, size), self.probe(),
                           threading.current_thread() is threading.main_thread()))
        words = (ctypes.c_uint32 * (size // 4)).from_address(e.pPrivateDriverData)
        signed = (ctypes.c_int32 * (size // 4)).from_address(e.pPrivateDriverData)
        layout = LAYOUTS.get(size)
        if layout is None or size < 68:
            return 0
        mask = words[layout.mask_word]
        if words[14] in GETS:
            words[16] = fake.get_rm
            if size == 1104:
                words[17] = 0xFF
            for rail in (0, 1):
                if mask >> rail & 1 and rail in fake.control:
                    base = layout.record0 + rail * layout.record_stride
                    signed[base + 1:base + 5] = fake.control[rail]
            return 0
        if words[14] in SETS:
            words[16] = fake.set_rm
            for rail in (0, 1):
                if not mask >> rail & 1:
                    continue
                base = layout.record0 + rail * layout.record_stride
                values = list(signed[base + 1:base + 5])
                fake.control[rail] = fake.store(rail, values) if fake.store else values
                if fake.drop_after_set:
                    fake.control.pop(rail, None)
                    fake.live.pop(rail, None)
            if fake.bases is not None:
                sync_live(fake, fake.bases)
            if fake.set_boost is not None:
                fake.vboost = fake.set_boost
            return fake.set_status
        return 0


def pascal(**changes):
    """tests/test_volt_rails_legacy.py's legacy_gpu("pascal"): R470's V1 control getter on Pascal."""
    return dict(config("pascal", legacy=True), bases=BASES["pascal"], **changes)


def turing(**changes):
    return dict(config("turing"), bases=BASES["turing"], **changes)


def blackwell(**changes):
    return dict(config("blackwell"), bases=BASES["blackwell"], **changes)


def kinds(step):
    """The ordered log of one operation, reduced to call kinds."""
    return [call[0] for call in step[2]]


def sets(step):
    """The SET packets of one operation, as the fake driver received them."""
    return [call[2] for call in step[2] if call[0] == "escape"
            and int.from_bytes(call[2][56:60], "little") in SETS]


class WriteParity(unittest.TestCase):
    def tearDown(self):
        native.hook_test_fail_protect(False)
        self.assertFalse(native.hook_test_state()["installed"])

    def parity(self, scenario, **options):
        rig = WriteRig(**options)
        self.addCleanup(rig.close)
        python, core = rig.run(scenario)
        self.assertEqual(len(python), len(core))
        for step, (p, c) in enumerate(zip(python, core)):
            with self.subTest(step=step, operation=p[0]):
                self.assertEqual(p[0], c[0])
                self.assertEqual(shape(p[1]), shape(c[1]), "result")
                self.assertEqual(p[2], c[2], "driver calls")
                self.assertEqual(p[3], c[3], "caches")
                self.assertEqual(p[4], c[4], "architecture reads")
                self.assertEqual(shape(p[5]), shape(c[5]), "profile afterwards")
                self.assertEqual(shape(p[6]), shape(c[6]), "attributes")
                self.assertEqual(p[7:], c[7:], "rail state")
        return python


class PlainWriteTests(WriteParity):
    def test_a_write_is_one_get_then_one_set_and_sets_the_attributes(self):
        def scenario(side, fake, clock):
            side.seed()
            side.write({0: [-12500, 0, 0, 0]})

        trace = self.parity(scenario, **turing())
        step = trace[0]
        self.assertEqual(step[1], (True, 0))
        self.assertEqual(step[6][0], None)
        self.assertEqual(step[6][1], dict(PROTOCOL, getter_version="0x00020AC8"))
        self.assertEqual(len(step[6][2]), 1)
        self.assertEqual(kinds(step).count("control-write"), 1)
        self.assertEqual(len(sets(step)), 1)
        self.assertEqual(kinds(step).count("boost"), 1)  # the locked write's own read only
        self.assertEqual(step[7][0], [-12500, 0, 0, 0])  # the driver stored the record

    def test_a_refused_set_reports_its_status(self):
        for options in (dict(set_rm=0x57), dict(set_status=-1)):
            with self.subTest(**options):
                def scenario(side, fake, clock):
                    side.seed()
                    side.write({0: [0, 0, 0, 225000]})  # a vmin change on Pascal: no settle without status 0

                trace = self.parity(scenario, **pascal(**options))
                status = 0x57 if "set_rm" in options else 0xFFFFFFFF
                self.assertEqual(trace[0][1], (True, status))
                self.assertEqual(len(sets(trace[0])), 1)
                self.assertEqual(trace[0][6][0], None)

    def test_a_driver_that_emits_no_rail_packet_reports_the_getter_status(self):
        def scenario(side, fake, clock):
            side.seed()
            side.write({0: [-12500, 0, 0, 0]})

        trace = self.parity(scenario, scripts=[["unrelated"]], **turing())
        self.assertEqual(trace[0][1], (False, None))
        self.assertTrue(trace[0][6][0].startswith("The driver did not emit a recognized rail-control packet."))
        self.assertIsNone(trace[0][6][1])
        self.assertEqual(len(trace[0][6][2]), 1)


class PreconditionTests(WriteParity):
    def refused(self, scenario, **options):
        trace = self.parity(scenario, **options)
        last = trace[-1]
        self.assertEqual(last[1], (False, None))
        self.assertNotIn("escape", kinds(last))
        self.assertNotIn("control-write", kinds(last))
        # the error cleared to None at the start; the protocol and the packets untouched
        self.assertEqual(last[6], (None, STALE[1], STALE[2]))
        return trace

    def test_unsupported_architecture(self):
        def scenario(side, fake, clock):
            side.seed()
            side.write({0: [-12500, 0, 0, 0]})

        self.refused(scenario, **turing(card={"arch": 5}))  # Volta is not a rail architecture

    def test_empty_records_foreign_keys_and_unreferenced_rails(self):
        for records in ({}, {"x": [0, 0, 0, 0]}, {2: [0, 0, 0, 0]}, {0: [0, 0, 0, 0], 5: [0, 0, 0, 0]},
                        {1: [0, 0, 0, 0]}):
            with self.subTest(records=records):
                def scenario(side, fake, clock, records=records):
                    side.seed()
                    side.write(records)

                trace = self.refused(scenario, **turing())
                self.assertNotIn("boost", kinds(trace[0]))

    def test_a_referenced_rail_whose_state_is_gone_is_not_supported(self):
        def scenario(side, fake, clock):
            side.limits()  # references both rails
            fake.live.pop(1)
            side.seed()
            side.write({0: [0, 0, 0, 0], 1: [0, 0, 0, 0]})

        trace = self.refused(scenario, **blackwell())
        self.assertNotIn("boost", kinds(trace[-1]))

    def test_a_record_without_four_values(self):
        for values in ([0, 0, 0], [0, 0, 0, 225000, 7], ()):  # a fifth value: values[3] differs from the floor
            with self.subTest(values=values):
                def scenario(side, fake, clock, values=values):
                    side.seed()
                    side.write({0: values})

                trace = self.refused(scenario, **pascal())
                self.assertNotIn("boost", kinds(trace[0]))  # no settle read either: len(values) != 4

    def test_no_boost(self):
        def scenario(side, fake, clock):
            side.seed()
            side.write({0: [-12500, 0, 0, 0]})

        trace = self.refused(scenario, vboost_status=-1, **turing())
        self.assertEqual(kinds(trace[0]).count("boost"), 1)


class SettleTests(WriteParity):
    def test_pascal_floor_change_is_verified_then_resent_identically(self):
        def scenario(side, fake, clock):
            side.seed()
            side.write({0: [0, 0, 0, 225000]})
            side.write({0: [0, 0, 0, 0]})  # restoring the stock floor settles too

        trace = self.parity(scenario, scripts=[["rail"], ["unrelated", "rail"], ["rail"], ["rail"]], **pascal())
        for step in trace:
            self.assertEqual(step[1], (True, 0))
            first, second = sets(step)
            self.assertEqual(first, second)  # the identity re-send carries the same packet
        log = kinds(trace[0])
        # snapshot, the settle boost read, write 1 (its own boost read), verify (read-back, boost), write 2
        self.assertEqual(log.count("control-write"), 2)
        self.assertEqual(log.count("boost"), 4)
        self.assertLess(log.index("boost"), log.index("control-write"))
        # the attributes are the second write's: its getter issued an unrelated escape first
        self.assertEqual(len(trace[0][6][2]), 2)
        self.assertEqual(trace[1][6][2], trace[0][6][2][1:])
        self.assertEqual(trace[0][6][1], dict(PROTOCOL, getter_version="0x00010AC8"))
        self.assertEqual(trace[0][7][0], [0, 0, 0, 225000])

    def test_no_settle_when_the_floor_is_unchanged(self):
        def scenario(side, fake, clock):
            side.write({0: [-12500, 0, 0, 0]})

        trace = self.parity(scenario, **pascal())
        self.assertEqual(trace[0][1], (True, 0))
        self.assertEqual(kinds(trace[0]).count("control-write"), 1)
        self.assertEqual(kinds(trace[0]).count("boost"), 1)

    def test_no_settle_for_a_rail_absent_from_the_snapshot(self):
        def scenario(side, fake, clock):
            side.seed()
            side.write({1: [0, 0, 0, 225000]})

        trace = self.parity(scenario, **pascal())
        self.assertEqual(trace[0][1], (False, None))
        self.assertNotIn("boost", kinds(trace[0]))

    def test_no_settle_without_settle_vmin(self):
        for options in (turing(), pascal(card={"arch": 6}), dict(config("pascal"), bases=BASES["pascal"])):
            with self.subTest(options=options.get("card")):
                def scenario(side, fake, clock):
                    side.write({0: [0, 0, 0, 225000]})

                trace = self.parity(scenario, **options)
                self.assertEqual(trace[0][1], (True, 0))
                self.assertEqual(kinds(trace[0]).count("control-write"), 1)


class VerifyTests(WriteParity):
    def failed_settle(self, records=None, **changes):
        def scenario(side, fake, clock):
            side.seed()
            side.write(records or {0: [0, 0, 0, 225000]})

        trace = self.parity(scenario, scripts=[["unrelated", "rail"], ["rail"]], **pascal(**changes))
        step = trace[0]
        self.assertEqual(step[1], (False, 0))
        self.assertEqual(kinds(step).count("control-write"), 1)  # no re-send
        self.assertEqual(len(step[6][2]), 2)  # the first write's attributes
        self.assertIsNone(step[6][0])
        return trace

    def test_incomplete_read_back(self):
        self.failed_settle(drop_after_set=True)

    def test_a_disagreeing_field(self):
        self.failed_settle(store=lambda rail, values: values[:3] + [values[3] + 6250])

    def test_a_tuple_never_equals_the_read_back(self):
        self.failed_settle(records={0: (0, 0, 0, 225000)})

    def test_a_changed_boost(self):
        self.failed_settle(set_boost=30)

    def test_verify_results_and_texts(self):
        def scenario(side, fake, clock):
            side.limits()
            side.verify({0: [0, 0, 0, 0]}, 25)
            side.verify({0: [0, 0, 0, 225000]}, 25)
            side.verify({0: (0, 0, 0, 0)}, 25)
            side.verify({0: (5,)}, 25)
            side.verify({0: [1, 2, 3]}, 25)
            side.verify({0: [0, 0, 0, -(2 ** 31)]}, 25)
            side.verify({0: [0, 0, 0, 0]}, 30)
            side.verify({0: [0, 0, 0, 0]}, None)
            side.verify({1: [0, 0, 0, 0]}, 25)
            side.verify({"x": [0, 0, 0, 0]}, 25)
            side.verify({}, 25)
            fake.vboost_status = -1
            side.verify({0: [0, 0, 0, 0]}, None)

        trace = self.parity(scenario, **pascal())
        results = [step[1] for step in trace[1:]]
        self.assertEqual(results[0][1], None)
        self.assertEqual(set(results[0][0]), {0})
        self.assertEqual([r[1] for r in results[1:]], [
            "rail 0 read-back disagrees: wanted [0, 0, 0, 225000], got [0, 0, 0, 0]",
            "rail 0 read-back disagrees: wanted (0, 0, 0, 0), got [0, 0, 0, 0]",
            "rail 0 read-back disagrees: wanted (5,), got [0, 0, 0, 0]",
            "rail 0 read-back disagrees: wanted [1, 2, 3], got [0, 0, 0, 0]",
            "rail 0 read-back disagrees: wanted [0, 0, 0, -2147483648], got [0, 0, 0, 0]",
            BOOST_CHANGED, BOOST_CHANGED, INCOMPLETE, INCOMPLETE, None, None])


class DivergenceTests(unittest.TestCase):
    """Values outside int32 and non-plain-int keys: the same result and error text on both sides,
    but the core refuses before any escape (Python after its GET, inside the hook)."""

    def sides(self, records, **options):
        rig = WriteRig(**options)
        self.addCleanup(rig.close)

        def scenario(side, fake, clock):
            side.seed()
            side.write(records)

        return rig.run(scenario)

    def test_out_of_int32_values_and_non_int_keys(self):
        for records, options, message in (
                ({0: [2 ** 31, 0, 0, 0]}, turing(), "rail 0 record values must be signed int32 integers"),
                ({0: [0, 0, 0, 1.0]}, turing(), "rail 0 record values must be signed int32 integers"),
                ({0: [-(2 ** 31) - 1, 0, 0, 0]}, turing(), "rail 0 record values must be signed int32 integers"),
                ({0: [0, 0, 0, 0], 1: [0, 2 ** 31, 0, 0]}, blackwell(),
                 "rail 1 record values must be signed int32 integers"),
                ({True: [0, 0, 0, 0]}, blackwell(), "requested_mask must exactly match the record rails")):
            with self.subTest(records=records):
                python, core = self.sides(records, **options)
                for step in (python[0], core[0]):
                    self.assertEqual(step[1], (False, None))
                    self.assertEqual(step[6][:2], (PRESERVE + message, None))
                    self.assertEqual(sets(step), [])
                self.assertEqual(len(python[0][6][2]), 1)  # Python summarised its GET
                self.assertEqual(core[0][6][2], [])
                self.assertNotIn("escape", kinds(core[0]))
                self.assertIn("escape", kinds(python[0]))
                # everything before the hook is the same
                cut = kinds(python[0]).index("control-write")
                self.assertEqual(python[0][2][:cut], core[0][2])


STORED = [-12500, 6250, 18750, 225000]  # distinct nonzero deltas in all four fields


def one_off(index, delta=1):
    values = list(STORED)
    values[index] += delta
    return values


class DistinctFieldTests(WriteParity):
    def test_verify_texts_with_distinct_fields_one_field_at_a_time(self):
        def scenario(side, fake, clock):
            side.limits()
            side.verify({0: list(STORED)}, 25)
            for index in range(4):
                side.verify({0: one_off(index)}, 25)

        trace = self.parity(scenario, **pascal(control={0: list(STORED)}))
        self.assertIsNone(trace[1][1][1])
        self.assertEqual([step[1][1] for step in trace[2:]], [
            f"rail 0 read-back disagrees: wanted {one_off(index)}, got {STORED}" for index in range(4)])
        self.assertEqual(trace[2][1][1], "rail 0 read-back disagrees: wanted [-12499, 6250, 18750, 225000], "
                                         "got [-12500, 6250, 18750, 225000]")

    def test_settle_with_distinct_fields_verifies_and_resends(self):
        def scenario(side, fake, clock):
            side.write({0: one_off(3, 25000)})

        trace = self.parity(scenario, **pascal(control={0: list(STORED)}))
        self.assertEqual(trace[0][1], (True, 0))
        self.assertEqual(len(sets(trace[0])), 2)
        self.assertEqual(trace[0][7][0], one_off(3, 25000))

    def test_settle_whose_driver_changes_one_field_fails_verification(self):
        for index in range(4):
            with self.subTest(field=index):
                def scenario(side, fake, clock):
                    side.write({0: one_off(3, 25000)})

                store = (lambda i: lambda rail, values: values[:i] + [values[i] + 1000] + values[i + 1:])(index)
                trace = self.parity(scenario, store=store, **pascal(control={0: list(STORED)}))
                self.assertEqual(trace[0][1], (False, 0))
                self.assertEqual(len(sets(trace[0])), 1)


class SettleEdgeTests(WriteParity):
    def test_a_re_send_refused_at_its_preconditions_keeps_the_first_writes_attributes(self):
        def scenario(side, fake, clock):
            side.seed()
            side.write({0: [0, 0, 0, 225000]})

        # boost reads: the settle read, write 1, the verification, then write 2's (fails)
        trace = self.parity(scenario, boost_fail_at={4}, scripts=[["unrelated", "rail"], ["rail"]], **pascal())
        step = trace[0]
        self.assertEqual(step[1], (False, None))
        self.assertEqual(kinds(step).count("boost"), 4)
        self.assertEqual(kinds(step).count("control-write"), 1)
        self.assertEqual(step[6][0], None)
        self.assertEqual(step[6][1], dict(PROTOCOL, getter_version="0x00010AC8"))
        self.assertEqual(len(step[6][2]), 2)  # the first write's two summaries

    def test_a_first_write_that_sets_nothing_is_not_verified(self):
        def scenario(side, fake, clock):
            side.seed()
            side.write({0: [0, 0, 0, 225000]})

        trace = self.parity(scenario, scripts=[["unrelated"]], **pascal())
        step = trace[0]
        self.assertEqual(step[1], (False, None))
        self.assertEqual(kinds(step).count("control-write"), 1)
        self.assertEqual(kinds(step).count("boost"), 2)  # the settle read and the write's; no verification
        self.assertTrue(step[6][0].startswith("The driver did not emit a recognized rail-control packet."))

    def test_a_settle_boost_of_none_skips_everything_after_the_refused_write(self):
        def scenario(side, fake, clock):
            side.seed()
            side.write({0: [0, 0, 0, 225000]})

        trace = self.parity(scenario, vboost_status=-1, **pascal())
        step = trace[0]
        self.assertEqual(step[1], (False, None))
        self.assertEqual(kinds(step).count("boost"), 2)
        self.assertNotIn("control-write", kinds(step))
        self.assertEqual(step[6], (None, STALE[1], STALE[2]))


class KeyAndBoundaryTests(WriteParity):
    def test_a_float_key_raises_pythons_type_error_at_the_rail_mask(self):
        for records, options in (({1.0: [0, 0, 0, 0]}, blackwell()), ({0.0: [0, 0, 0, 225000]}, pascal())):
            with self.subTest(records=records):
                def scenario(side, fake, clock, records=records):
                    side.seed()
                    side.write(records)

                trace = self.parity(scenario, **options)
                step = trace[0]
                self.assertEqual(step[1], ("raises", "TypeError",
                                           "unsupported operand type(s) for <<: 'int' and 'float'"))
                self.assertNotIn("control-write", kinds(step))
                self.assertEqual(step[6], (None, STALE[1], STALE[2]))
                # the settle read (Pascal, the floor changes) and the locked write's boost read both ran
                self.assertEqual(kinds(step).count("boost"), 2 if 0.0 in records else 1)

    def test_int32_boundaries_are_written(self):
        def scenario(side, fake, clock):
            side.write({0: [2 ** 31 - 1, -(2 ** 31), 0, 0]})

        trace = self.parity(scenario, **turing())
        self.assertEqual(trace[0][1], (True, 0))
        (packet,) = sets(trace[0])
        words = list(ctypes.cast(ctypes.create_string_buffer(packet, len(packet)), ctypes.POINTER(ctypes.c_int32))[20:25])
        self.assertEqual(words, [5, 2 ** 31 - 1, -(2 ** 31), 0, 0])
        self.assertEqual(trace[0][7][0], [2 ** 31 - 1, -(2 ** 31), 0, 0])


class BindingContractTests(unittest.TestCase):
    """The native binding's own refusals (documented divergences of druta.native_rails)."""

    def setUp(self):
        self.rig = WriteRig(**pascal())
        self.addCleanup(self.rig.close)
        self.gpu = self.rig.native
        self.fake = self.rig.fake = WriteFake(**self.rig.config)

    def test_verify_refusals_raise_type_error_before_any_read(self):
        for records, boost in (({0: range(4)}, 25), ({0: [0, 0, 0, 0, 0]}, 25), ({0: [0, 0, 0, 0.0]}, 25),
                               ({True: [0, 0, 0, 0]}, 25), ({1.0: [0, 0, 0, 0]}, 25), ({0: 5}, 25),
                               ({0: [0, 0, 0, 0]}, 2 ** 31), ({0: [0, 0, 0, 0]}, 25.0)):
            with self.subTest(records=records, boost=boost):
                self.fake.calls.clear()
                with self.assertRaises(TypeError):
                    self.gpu._verify_rail_records(records, boost)
                self.assertEqual(self.fake.calls, [])

    def test_an_unsized_value_raises_with_the_error_already_cleared(self):
        self.gpu._volt_rail_write_error = "stale"
        self.fake.calls.clear()
        with self.assertRaises(TypeError):
            self.gpu._write_rail_records({0: 5})
        self.assertIsNone(self.gpu._volt_rail_write_error)
        self.assertEqual(self.fake.calls, [])

    def test_verify_refuses_more_than_four_values_at_the_abi(self):
        records = nr._new(nr._Records)
        records.count = 1
        records.records[0].count = 5
        out = nr._new(nr._VerifyResult)
        library = nr.load()
        self.assertEqual(library.dc_rails_verify_records(self.gpu._handle, ctypes.byref(records), 0, 0,
                                                         ctypes.byref(out)), native.DC_E_ARGUMENT)
        self.assertEqual(self.fake.calls, [])
        outcome = nr._new(nr._WriteOutcome)  # the write entry takes it: count != 4 is its precondition refusal
        self.assertEqual(library.dc_rails_write_records(self.gpu._handle, ctypes.byref(records),
                                                        ctypes.byref(outcome)), native.DC_OK)
        self.assertEqual((outcome.ok, outcome.has_status, outcome.attributes), (0, 0, 0))


class HookMutexTests(unittest.TestCase):
    """A second handle's raw locked write, started from inside the sequence, runs only once the
    whole sequence has ended (the process-wide hook mutex is held from the snapshot to the re-send)."""

    def race(self, trigger):
        rig = WriteRig(**pascal())
        self.addCleanup(rig.close)
        nr.refresh(rig.native)
        fake = rig.fake = WriteFake(**rig.config)
        fake.rig = rig
        other = native.Gpu()
        self.addCleanup(other.close)
        state = {"thread": None, "about": threading.Event(), "entered": threading.Event(), "early": None}
        log = fake.calls

        def competitor():
            state["about"].set()  # right before the locked write that must block
            other._rail_write_locked(0x10AC8, 1, {0: [0, 0, 0, 0]}, 25)

        def on_read(fake):
            if state["thread"] is None and trigger(log):
                log.append(("competitor-start",))
                state["thread"] = threading.Thread(target=competitor)
                state["thread"].start()
                self.assertTrue(state["about"].wait(5))  # the competitor is at its call
                # A bounded wait that releases the GIL. Were the hook mutex free here, the
                # competitor's getter would run (and set the event) inside this wait.
                state["early"] = state["entered"].wait(0.5)

        original = rig.issue

        def issue(mask, script):
            if threading.current_thread() is not threading.main_thread():
                state["entered"].set()
            original(mask, script)
        rig.issue = issue
        fake.on_read = on_read
        rig.probe = lambda: native.hook_test_state()["patched"]
        rig.enter = native.hook_test_entry
        try:
            self.assertEqual(rig.native._write_rail_records({0: [0, 0, 0, 225000]}), (True, 0))
        finally:
            if state["thread"] is not None:
                state["thread"].join()
            rig.probe = lambda: False
        self.assertIsNotNone(state["thread"])
        self.assertFalse(state["early"])
        writes = [(index, call[-1]) for index, call in enumerate(log) if call[0] == "control-write"]
        self.assertEqual([main for _, main in writes], [True, True, False])
        start = log.index(("competitor-start",))
        self.assertTrue(start < writes[1][0] < writes[2][0])
        last_main = max(i for i, call in enumerate(log) if call[0] == "escape" and call[-1])
        self.assertGreater(writes[2][0], last_main)
        self.assertEqual(fake.control[0], [0, 0, 0, 0])  # the competitor's record landed last
        return log, start, writes

    def test_a_competitor_started_at_the_snapshot_waits_for_the_whole_sequence(self):
        log, start, writes = self.race(lambda log: True)  # the sequence's first control read
        self.assertLess(start, writes[0][0])
        self.assertEqual(start, 0)  # on_read runs as the snapshot's first control call arrives

    def test_a_competitor_started_at_the_verification_waits_for_the_re_send(self):
        log, start, writes = self.race(sets_in)  # the first control read after the first SET
        self.assertLess(writes[0][0], start)


def sets_in(log):
    return any(call[0] == "escape" and int.from_bytes(call[2][56:60], "little") in SETS for call in log)


if __name__ == "__main__":
    unittest.main()
