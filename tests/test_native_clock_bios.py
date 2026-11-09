# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native clock-BIOS reader, increments C1 (the guarded user-clock CONTROL GET and the pure
_control_shape) and C2 (the INFO GET _info_once, the pure _decode and the two-sample read())
against clock_bios_runtime.

druta.native_clock_bios runs dc_clock_bios_control / dc_clock_bios_control_shape /
dc_clock_bios_reset. The Python original, clock_bios_runtime._control_once(twin,
PerfDiagnosticsReader(twin), identity), runs on the nvbackend.GPU identity twin of
tests/test_native_target_binding.py (Card.python_twin: GPU.__new__ with the identity records the
native handle exposes, so _binding, slot(), arch() and clkdom_domains are the real methods), given
the same stateful fake ClkDomCtlGet as the core (tests/test_native_clkdom.py::ClkDomRig).

The control GET (every GET after the one-bit-at-a-time discovery) logs the 24996 request bytes it
received and the 64 bytes before and after them, which must still be the 0xA5 guards: so the core
hands the driver exactly the declared allocation with the version at 0, the mask at 8 and zeros
elsewhere. Each case compares the result or the exception (exact type name, PerfDiagnosticError kind
and status, message) and the whole call log.

The caller's identity is compared by the binding with Python's own `!=` against the identity the
core bound, so identities that only compare equal (complex, Fraction, bool, a mappingproxy, a dict
subclass with its own comparison) behave as in Python, exceptions included.

Unreachable natively, and so covered only by tests/test_clock_bios_runtime.py: a clkdom_domains
inventory that is not a list or tuple, has duplicates, or holds a value that is not an int in 0..31
(ClkDom::domains builds unique probed indices); a non-int GET status; an _identity refusal of the
binding's own values (a device id above 16 bits, a PCI bus/device/function out of range: the core
binding yields neither); the sticky "GPU binding changed" refusal (none of its identity elements can
change under the public fakes, as tests/test_native_target_binding.py notes: the architecture is
cached and the NVAPI handle and ids are fixed at open). Whether a reader holds a sticky identity is
compared instead, through dc_clock_bios_held against reader._identity. A pairing mismatch
disables NVAPI, so the inventory is already unavailable before the binding is reached; the
binding's own identity and transient refusals are covered through the NVML PCI identity and the
architecture read instead.

C2 (DecodeTests, InfoOnceTests, ReadTests, AbiArgumentTests): druta.native_clock_bios runs
dc_clock_bios_decode / dc_clock_bios_info / dc_clock_bios_read. InfoRig joins ClockRig's
ClkDomCtlGet with PerfRig's fake gdi32 (tests/test_native_perf_diagnostics.py), whose escape answers
the INFO GET (words 5 and 11 set to 1, word 16 the RM status, the INFO payload of
tests/test_clock_bios_runtime.py at byte 68) and logs the 64 bytes on either side of the packet. Both
sides store the same A612 fixture transport carrying the paired envelope words, so reader._transport()
succeeds on each, and the twin's _legacy_clk_escape is the real nvbackend one. read() drops the
transport cache after its first attempt; nothing in these fakes can recapture it (no PowerPolInfo
getter), so a rig with recapture=True re-stores the fixture when a side queries the INFO export with
an empty cache (logged as ("recapture",)): both sides query it at the same point, which stands in for
Python's FakeGPU._current_limit_transport_layouts recapture. Each case compares the outcome, the whole
call log and the transport cache afterwards (stored transport, and whether a layout choice is set).
The reader read() uses is fresh per call: neither side's handle reader gains or loses its sticky
identity through read().

Unreachable natively for C2, and so covered only by tests/test_clock_bios_runtime.py: a target
change between the two bindings of one _info_once ("Clock INFO target changed during GET") or of
_control_once inside read() (the identity elements are fixed at open, as above); a transport whose
escape fields are not 32-bit ints (EscapeFields is typed) or whose header is not 17 words (rejected
by reader._transport() first); a non-int escape status other than None; a non-PerfDiagnosticError
from _info_once (Python's `except Exception` branch of read(): only _identity's ClockBiosError could
raise one, and binding() never yields a value it refuses) and every check of _shape(observation) in
read(), each of which re-validates values the core produced itself (the constant layout and
stable_reads, the identity binding() validated, the exact SIZE info bytes and their canonical hex,
the CONTROL dict _control_once already shaped, and rows decoded from the same bytes).

Not mirrored by ncb.decode, which refuses them with a TypeError of its own
(DecodeTests.test_buffers_python_indexes_by_element_are_refused): a 12336-element buffer whose items
are not unsigned single bytes or that has more than one dimension. _decode indexes such a buffer by
element while struct.unpack_from reads it by byte offset: a memoryview cast to 'I' passes the length
check and is then read partly by byte offsets and sliced by element, format 'b' gives signed row
types and 'c' gives bytes objects. Objects without the buffer protocol, a strided view (the
BufferError struct.unpack_from raises) and every one-dimensional unsigned-byte buffer (bytes,
bytearray, array('B'), a byte memoryview, a ctypes c_uint8 array) are compared with _decode."""
import array
import ctypes
import random
import struct
import unittest
from fractions import Fraction
from types import MappingProxyType
from unittest import mock

from druta import clock_bios_runtime as cbr, native, nvbackend as NV
from druta.clock_bios_runtime import _CONTROL_LAYOUT
from druta.perf_diagnostics import PerfDiagnosticsReader
from tests.test_native_clkdom import GET_ID, SIZE, UNKNOWN, VERSION, Block, ClkDomRig
from tests import test_clock_bios_runtime as rp
from tests.test_native_identity import TITAN
from tests.test_native_perf_diagnostics import PerfRig, header as perf_header
from tests.test_native_target_binding import Card
from tests.test_native_transport import ESCAPE, FIELDS, KMT, NTSTATUS_INVALID_PARAMETER

if not native.available():
    raise unittest.SkipTest("native core not built (native/build.sh linux)")

from druta import native_clock_bios as ncb  # noqa: E402

CDECL = ctypes.CFUNCTYPE
u32 = ctypes.c_uint32
GUARD = b"\xa5" * 64
IDENTITY = {"slot": "0000:01:00.0", "architecture": 7, "device_id": TITAN["devid"], "subsystem_id": TITAN["subsys"]}
ACCEPTED = [0, 1, 2, 3, 5, 9]
MASK = sum(1 << d for d in ACCEPTED)
DISCOVERY = [("ClkDomCtlGet", 1 << d) for d in range(32)]
UNAVAILABLE_INVENTORY = ("ClockBiosError", None, None, "User-clock request layout or inventory is unavailable")
CHANGED = ("ClockBiosError", None, None, "User-clock requests changed version or inventory")
TARGET_CHANGED = ("PerfDiagnosticError", "identity", None, "User-clock request target changed")
OVERRUN = ("PerfDiagnosticError", "malformed", None, "Clock GET exceeded the guarded allocation")


def request(mask):
    """The bytes the control GET must receive: version at 0, mask at 8, zeros elsewhere."""
    data = bytearray(SIZE)
    struct.pack_into("<II", data, 0, VERSION, 0)
    struct.pack_into("<I", data, 8, mask)
    return Block(data)


def control_calls(mask):
    return [("control-request", GUARD, request(mask), GUARD), ("ClkDomCtlGet", mask)]


class ClockRig(ClkDomRig):
    """ClkDomRig whose control GET (any GET after discovery) can misbehave after answering.

    control_status: the control GET returns this status
    overrun / underrun: bytes the driver writes just past the end / before the start of the packet
    pokes: byte offsets relative to the packet start the driver writes 0xEE to (single guard bytes)
    words: {word index: value} the driver writes over its answer (the version and mask echoes)
    """

    def __init__(self, *, control_status=0, overrun=0, underrun=0, pokes=(), words=None, arch=7,
                 accepted=ACCEPTED, **kw):
        self.control_status, self.overrun, self.underrun = control_status, overrun, underrun
        self.pokes = tuple(pokes)
        self.words = dict(words or {})
        self.probe_next = 0
        super().__init__(arch=arch, accepted=accepted, **kw)
        base = self.nvapi.interfaces.get(GET_ID)
        if base is None:
            return

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def get(handle, pointer):
            mask = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32))[2]
            if self.probe_next < 32 and mask == 1 << self.probe_next:  # clkdom_domains' probe
                self.probe_next += 1
                return base(handle, pointer)
            self.calls.append(("control-request", ctypes.string_at(pointer - len(GUARD), len(GUARD)),
                               Block(ctypes.string_at(pointer, SIZE)), ctypes.string_at(pointer + SIZE, len(GUARD))))
            status = base(handle, pointer)
            words = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32))
            for index, value in self.words.items():
                words[index] = value
            if self.overrun:
                ctypes.memset(pointer + SIZE, 0xEE, self.overrun)
            if self.underrun:
                ctypes.memset(pointer - self.underrun, 0xEE, self.underrun)
            for offset in self.pokes:
                ctypes.memset(pointer + offset, 0xEE, 1)
            return self.control_status or status
        self.names["ClkDomCtlGet"] = self.nvapi.interfaces[GET_ID] = get

    def reset(self):
        super().reset()
        self.probe_next = 0


def outcome(call):
    """("ok", value), or the exception's exact type name, kind, status and text."""
    try:
        return ("ok", call())
    except Exception as exc:
        return (type(exc).__name__, getattr(exc, "kind", None), getattr(exc, "status", None), str(exc))


def held(side):
    """Whether the side's reader holds a sticky target identity."""
    return side.reader._identity is not None if side.python else ncb.held(side.gpu)


class Side:
    """One side of a run: control_once(identity) through its current reader; fresh() a new reader."""

    def __init__(self, gpu, python):
        self.gpu, self.python = gpu, python
        self.reader = PerfDiagnosticsReader(gpu) if python else None

    def control_once(self, identity=IDENTITY):
        if self.python:
            return cbr._control_once(self.gpu, self.reader, identity)
        return ncb.control_once(self.gpu, identity)

    def fresh(self):
        if self.python:
            self.reader = PerfDiagnosticsReader(self.gpu)
        else:
            ncb.reset(self.gpu)


class ClockCase(unittest.TestCase):
    def tearDown(self):
        native.set_export_resolver(None)

    def both(self, rig, action=lambda side: side.control_once()):
        """action(side) on the Python twin, then on the core, from one fake state: (outcome, call log)."""
        opened = rig.native_gpu()
        runs = []
        try:
            twin = Card.python_twin(rig, opened)
            twin.nvapi.ClkDomCtlGet = rig.names.get("ClkDomCtlGet")
            for side in (Side(twin, True), Side(opened, False)):
                rig.reset()
                runs.append((outcome(lambda: action(side)), list(rig.calls)))
        finally:
            opened.close()
        self.assertEqual(runs[0], runs[1], "Python and native runs differ")
        return runs[0]


class ControlOnceTests(ClockCase):
    def test_happy_path_returns_the_probed_inventory_and_the_driver_bytes(self):
        rig = ClockRig(preload={3: {"mode": 15, "msvdd": -12345}, 9: {"mode": 15, "msvdd": 2100000}})
        (kind, value), calls = self.both(rig)
        self.assertEqual(kind, "ok")
        self.assertEqual(calls, DISCOVERY + control_calls(MASK))
        self.assertEqual(value, {**_CONTROL_LAYOUT, "mask": MASK, "payload_hex": rig.expected_block(MASK).hex()})
        self.assertEqual(cbr._control_shape(value, IDENTITY)[1], {3: -12345, 9: 2100000})
        self.assertEqual(ncb.control_shape(value, IDENTITY)[1], {3: -12345, 9: 2100000})

    def test_single_and_high_domains(self):
        for accepted in ([0], [31], [4, 31]):
            with self.subTest(accepted=accepted):
                mask = sum(1 << d for d in accepted)
                (kind, value), calls = self.both(ClockRig(accepted=accepted))
                self.assertEqual((kind, value["mask"]), ("ok", mask))
                self.assertEqual(calls, DISCOVERY + control_calls(mask))

    def test_getter_withheld_is_refused_without_a_call(self):
        result, calls = self.both(ClockRig(with_get=False))
        self.assertEqual(result, ("ClockBiosError", None, None, "User-clock CONTROL GET is unavailable"))
        self.assertEqual(calls, [])

    def test_empty_inventory(self):
        result, calls = self.both(ClockRig(accepted=[]))
        self.assertEqual(result, UNAVAILABLE_INVENTORY)
        self.assertEqual(calls, DISCOVERY)

    def test_getter_status(self):
        for status in (-1, 7):
            with self.subTest(status=status):
                result, calls = self.both(ClockRig(control_status=status))
                self.assertEqual(result, ("ClockBiosError", None, None, f"User-clock CONTROL GET failed: {status}"))
                self.assertEqual(calls, DISCOVERY + control_calls(MASK))

    def test_guard_overrun_comes_first(self):
        for options in (dict(overrun=1), dict(overrun=64), dict(underrun=4), dict(overrun=4, control_status=-1),
                        dict(underrun=1), dict(underrun=64), dict(pokes=[-64]), dict(pokes=[-1]),
                        dict(pokes=[SIZE + 63]), dict(pokes=[-64], control_status=-1)):
            with self.subTest(**options):
                result, calls = self.both(ClockRig(**options))
                self.assertEqual(result, OVERRUN)
                self.assertEqual(calls, DISCOVERY + control_calls(MASK))
        result, _ = self.both(ClockRig(overrun=4), lambda side: side.control_once(dict(IDENTITY, device_id=1)))
        self.assertEqual(result, OVERRUN)

    def test_a_different_identity_is_refused_after_the_get(self):
        others = [dict(IDENTITY, device_id=0x1B02), dict(IDENTITY, slot="0000:02:00.0"), dict(IDENTITY, architecture=8),
                  dict(IDENTITY, subsystem_id=0), {k: v for k, v in IDENTITY.items() if k != "slot"},
                  dict(IDENTITY, extra=1), dict(IDENTITY, slot=b"0000:01:00.0"), dict(IDENTITY, slot="0" * 40),
                  dict(IDENTITY, device_id=-1), dict(IDENTITY, device_id=2 ** 32), dict(IDENTITY, device_id="7682"),
                  None, "0000:01:00.0", list(IDENTITY.items())]
        for identity in others:
            with self.subTest(identity=identity):
                result, calls = self.both(ClockRig(), lambda side: side.control_once(identity))
                self.assertEqual(result, TARGET_CHANGED)
                self.assertEqual(calls, DISCOVERY + control_calls(MASK))
        result, _ = self.both(ClockRig(control_status=-1), lambda side: side.control_once(None))
        self.assertEqual(result, TARGET_CHANGED)  # the target is compared before the status

    def test_an_equal_identity_of_other_numeric_types_is_accepted(self):
        identity = dict(IDENTITY, architecture=7.0, device_id=float(TITAN["devid"]))
        (kind, value), _ = self.both(ClockRig(), lambda side: side.control_once(identity))
        self.assertEqual((kind, value["mask"]), ("ok", MASK))

    def test_the_identity_is_compared_with_pythons_own_semantics(self):
        class Equal(dict):  # != says equal whatever the values
            def __ne__(self, other):
                return False

        class Unequal(dict):  # != says different whatever the values
            def __ne__(self, other):
                return True

        class EqOnly(dict):  # only __eq__ overridden: dict's own != still decides
            def __eq__(self, other):
                return False

        class Raises(dict):
            def __ne__(self, other):
                raise LookupError("comparison refused")

        class Weird:  # an int-convertible value that does not compare equal
            def __int__(self):
                return 7

        accepted = [dict(IDENTITY, architecture=7 + 0j), dict(IDENTITY, architecture=Fraction(7)),
                    dict(IDENTITY, device_id=Fraction(TITAN["devid"])), MappingProxyType(IDENTITY),
                    Equal(IDENTITY, device_id=1, slot="nonsense"), Equal(), EqOnly(IDENTITY)]
        refused = [Unequal(IDENTITY), dict(IDENTITY, architecture=7 + 1j), dict(IDENTITY, architecture=Weird()),
                   dict(IDENTITY, architecture=7.5), MappingProxyType(dict(IDENTITY, device_id=1)),
                   dict(reversed(list(IDENTITY.items())), extra=None)]
        for identity in accepted:
            with self.subTest(identity=identity):
                (kind, value), calls = self.both(ClockRig(), lambda side: side.control_once(identity))
                self.assertEqual((kind, value["mask"]), ("ok", MASK))
                self.assertEqual(calls, DISCOVERY + control_calls(MASK))
        for identity in refused:
            with self.subTest(identity=identity):
                result, calls = self.both(ClockRig(), lambda side: side.control_once(identity))
                self.assertEqual(result, TARGET_CHANGED)
                self.assertEqual(calls, DISCOVERY + control_calls(MASK))
        # The comparison's own exception propagates, ahead of the status check.
        result, calls = self.both(ClockRig(control_status=-1), lambda side: side.control_once(Raises(IDENTITY)))
        self.assertEqual(result, ("LookupError", None, None, "comparison refused"))
        self.assertEqual(calls, DISCOVERY + control_calls(MASK))
        # The comparison is not reached when the guards or the binding refuse first.
        self.assertEqual(self.both(ClockRig(overrun=1), lambda side: side.control_once(Raises()))[0], OVERRUN)
        self.assertEqual(self.both(ClockRig(arch=UNKNOWN), lambda side: side.control_once(Raises()))[0],
                         ("PerfDiagnosticError", "transient", None, "GPU architecture could not be read"))

    def test_bool_float_fraction_and_complex_equal_a_bound_one(self):
        with mock.patch("tests.test_native_telemetry.TITAN", dict(TITAN, subsys=1)):
            rig = ClockRig()
        for one in (1, True, 1.0, Fraction(1), 1 + 0j):
            with self.subTest(one=one):
                identity = dict(IDENTITY, subsystem_id=one)
                (kind, value), calls = self.both(rig, lambda side: side.control_once(identity))
                self.assertEqual((kind, value["mask"]), ("ok", MASK))
                self.assertEqual(calls, DISCOVERY + control_calls(MASK))
        for other in (False, 0, 1.5, IDENTITY["subsystem_id"]):
            with self.subTest(other=other):
                identity = dict(IDENTITY, subsystem_id=other)
                result, _ = self.both(rig, lambda side: side.control_once(identity))
                self.assertEqual(result, TARGET_CHANGED)

    def test_failures_in_pythons_order_when_two_coincide(self):
        cases = [
            (dict(control_status=-1, words={0: VERSION + 1}),
             ("ClockBiosError", None, None, "User-clock CONTROL GET failed: -1")),
            (dict(control_status=-1, words={2: MASK ^ 2}),
             ("ClockBiosError", None, None, "User-clock CONTROL GET failed: -1")),
            (dict(overrun=4, arch=UNKNOWN), OVERRUN),
            (dict(pokes=[-1], arch=3), OVERRUN),
            (dict(words={2: MASK ^ 2}), CHANGED),
            (dict(words={0: VERSION + 1, 2: MASK ^ 2}), CHANGED),
        ]
        for options, expected in cases:
            with self.subTest(**{k: str(v) for k, v in options.items()}):
                result, calls = self.both(ClockRig(**options))
                self.assertEqual(result, expected)
                self.assertEqual(calls, DISCOVERY + control_calls(MASK))

    def test_the_bindings_own_refusals_keep_their_kind(self):
        cases = [
            (dict(nvml=dict(TITAN, pci_status=3)),
             ("PerfDiagnosticError", "identity", None, "NVML and NVAPI identify different PCI addresses")),
            (dict(nvml=dict(TITAN, domain=1)),
             ("PerfDiagnosticError", "identity", None, "NVAPI handle does not identify the selected PCI address")),
            (dict(arch=UNKNOWN), ("PerfDiagnosticError", "transient", None, "GPU architecture could not be read")),
            (dict(arch=3), ("PerfDiagnosticError", "unsupported", None,
                            "Modern PERF groups require a Pascal-or-later architecture")),
        ]
        for options, expected in cases:
            with self.subTest(**{k: str(v) for k, v in options.items()}):
                device = options.pop("nvml", None)
                rig = ClockRig(control_status=-1, **options)
                if device is not None:
                    rig.nvml.devices[0] = device
                result, calls = self.both(rig)
                self.assertEqual(result, expected)
                self.assertEqual(calls, DISCOVERY + control_calls(MASK))

    def test_a_pairing_mismatch_is_refused_at_the_inventory(self):
        rig = ClockRig()
        rig.nvml.devices[0] = dict(TITAN, devid=0x1B02, subsys=0x119A10DE)
        result, calls = self.both(rig)
        self.assertEqual(result, UNAVAILABLE_INVENTORY)  # NVAPI is disabled, so clkdom_ok is False
        self.assertEqual(calls, [])

    def test_changed_version_or_mask_echo(self):
        for words in ({0: VERSION + 1}, {0: 0}, {2: MASK ^ 1}, {2: MASK | 1 << 31}, {2: 0}):
            with self.subTest(words=words):
                result, calls = self.both(ClockRig(words=words))
                self.assertEqual(result, CHANGED)
                self.assertEqual(calls, DISCOVERY + control_calls(MASK))

    def test_the_sticky_identity_is_held_until_reset(self):
        """dc_clock_bios_reset clears the reader's sticky identity as a fresh PerfDiagnosticsReader does."""
        def states(identity):
            def action(side):
                seen = [held(side)]
                seen.append(outcome(lambda: side.control_once(identity))[0])
                seen.append(held(side))
                side.fresh()
                seen.append(held(side))
                return seen
            return action
        rig = ClockRig()
        self.assertEqual(self.both(rig, states(IDENTITY))[0], ("ok", [False, "ok", True, False]))
        # bound, then the identity comparison refuses: the binding still holds its identity
        self.assertEqual(self.both(rig, states(None))[0], ("ok", [False, "PerfDiagnosticError", True, False]))
        # refused before the binding (no getter; an overrun) or by it: nothing is held
        for options in (dict(with_get=False), dict(overrun=1), dict(arch=UNKNOWN), dict(arch=3)):
            with self.subTest(**{k: str(v) for k, v in options.items()}):
                result = self.both(ClockRig(**options), states(IDENTITY))[0]
                self.assertEqual(result[0], "ok")
                self.assertEqual([result[1][i] for i in (0, 2, 3)], [False, False, False])

    def test_reset_is_a_fresh_reader(self):
        def twice(side):
            side.fresh()
            first = side.control_once()
            side.fresh()
            return first, side.control_once()
        (kind, (first, second)), calls = self.both(ClockRig(), twice)
        self.assertEqual(kind, "ok")
        self.assertEqual(first, second)
        self.assertEqual(calls, DISCOVERY + control_calls(MASK) * 2)  # the inventory is cached on the GPU


# ---- _control_shape ------------------------------------------------------------------------------

def payload(mask, rows=(), *, version=VERSION, echo=None):
    """A CONTROL payload: rows is ((index, row_type, int32 request), ...)."""
    data = bytearray(SIZE)
    struct.pack_into("<I", data, 0, version)
    struct.pack_into("<I", data, 8, mask if echo is None else echo)
    for index, row_type, value in rows:
        pos = 0x124 + index * 0x304
        struct.pack_into("<I", data, pos, row_type)
        struct.pack_into("<i", data, pos + 0x114, value)
        struct.pack_into("<i", data, pos + 0x10C, 0x5A5A5A5A)  # a neighbour that must not be read
    return bytes(data)


def value(mask, raw=None, **changes):
    out = {**_CONTROL_LAYOUT, "mask": mask, "payload_hex": (payload(mask) if raw is None else raw).hex()}
    out.update(changes)
    return out


class ControlShapeTests(unittest.TestCase):
    def shape(self, value):
        results = []
        for function in (cbr._control_shape, ncb.control_shape):
            result = outcome(lambda: function(value, IDENTITY))
            if result[0] == "ok":
                self.assertIsNot(result[1][0], value)
            results.append(result)
        self.assertEqual(results[0], results[1], "Python and native shapes differ")
        return results[0]

    def test_requests_of_type_15_rows_only(self):
        mask = 1 | 1 << 3 | 1 << 5 | 1 << 31
        rows = ((0, 15, 2 ** 31 - 1), (3, 15, -12345), (5, 16, 777), (31, 15, -(2 ** 31)), (7, 15, 99))
        result = self.shape(value(mask, payload(mask, rows)))
        self.assertEqual(result, ("ok", (value(mask, payload(mask, rows)), {0: 2 ** 31 - 1, 3: -12345, 31: -(2 ** 31)})))

    def test_row_types_matching_15_only_in_the_low_byte_are_not_requests(self):
        mask = 1 << 1 | 1 << 2 | 1 << 3 | 1 << 4
        rows = ((1, 0x10F, 11), (2, 0x1000000F, 22), (3, 0xFFFFFF0F, 33), (4, 15, 44), (8, 15, 88), (30, 15, 300))
        result = self.shape(value(mask, payload(mask, rows)))
        self.assertEqual(result, ("ok", (value(mask, payload(mask, rows)), {4: 44})))

    def test_masks_with_bits_over_rows_without_requests(self):
        rows = ((2, 14, 5), (4, 0, 5), (6, 15, -1))
        for mask in (1, 0xFFFFFFFF, 1 << 31, 0b1010100):
            with self.subTest(mask=hex(mask)):
                expected = {6: -1} if mask >> 6 & 1 else {}
                self.assertEqual(self.shape(value(mask, payload(mask, rows)))[1][1], expected)

    def test_mask_range_and_type(self):
        message = ("ClockBiosError", None, None, "User-clock request inventory is invalid")
        for mask in (0, 2 ** 32, -1, 2 ** 64, 2 ** 70, True, 1.0, "1", None):
            with self.subTest(mask=mask):
                self.assertEqual(self.shape(value(mask, payload(1))), message)

    def test_the_inventory_is_checked_before_the_geometry(self):
        inventory = ("ClockBiosError", None, None, "User-clock request inventory is invalid")
        for mask in (0, None, True, 2 ** 32):
            for text in (None, "", "zz", payload(1).hex()[:-2]):
                with self.subTest(mask=mask, text=repr(text)[:12]):
                    self.assertEqual(self.shape(value(mask, payload(1), payload_hex=text)), inventory)

    def test_hex_whitespace_is_pythons_ascii_whitespace(self):
        good = payload(1).hex()
        not_hex = ("ClockBiosError", None, None, "Clock readback is not hexadecimal")
        canonical = ("ClockBiosError", None, None, "Clock readback needs canonical bounded hexadecimal")
        for space in " \t\n\r\x0b\x0c":
            cases = [
                (space * 2 + good[2:], canonical),                         # at the start
                (good[:-2] + space * 2, canonical),                         # at the end
                (good[:100] + space + good[100:-2] + space, canonical),     # between pairs, and at the end
                (space + good[:100] + space + good[100:-2], canonical),     # at the start, and between pairs
                (good[:101] + space + good[101:-1], not_hex),               # inside a pair
            ]
            for text, expected in cases:
                with self.subTest(space=repr(space), text=repr(text)[:16] + repr(text)[-8:]):
                    self.assertEqual(len(text), len(good))
                    self.assertEqual(self.shape(value(1, payload_hex=text)), expected)
        for text in ("\xa0\xa0" + good[2:], good[:-2] + "\xa0\xa0", good[:100] + "\xa0" + good[100:-2] + " ",
                     good[:-2] + "\x1c\x1f", good[:-2] + "\x85\x85", good[:-2] + "\u3000 "):
            with self.subTest(text=repr(text)[-12:]):
                self.assertEqual(self.shape(value(1, payload_hex=text)), not_hex)

    def test_payload_geometry_and_hexadecimal(self):
        good = payload(1).hex()
        geometry = ("ClockBiosError", None, None, "Clock readback has invalid packet geometry")
        not_hex = ("ClockBiosError", None, None, "Clock readback is not hexadecimal")
        canonical = ("ClockBiosError", None, None, "Clock readback needs canonical bounded hexadecimal")
        cases = [
            (good[:-2], geometry), (good + "00", geometry), (good[:-1], geometry), ("", geometry),
            (bytes.fromhex(good), geometry), (None, geometry), (list(good), geometry),
            (good[:-2] + "zz", not_hex), (good[:-2] + "éé", not_hex), (good[:-2] + "€0", not_hex),
            (good[:-2] + "0\x00", not_hex), (good[:-2] + " 0", not_hex), (good[:-3] + "0 0", not_hex),
            (good[:-2] + "  ", canonical), (good[:-4] + " 00 ", canonical), (good[:-2] + "\t\n", canonical),
            (good[:16].upper() + good[16:], canonical), (good[:-2] + "AA", canonical),
        ]
        for text, expected in cases:
            with self.subTest(text=repr(text)[-12:]):
                self.assertEqual(self.shape(value(1, payload_hex=text)), expected)

    def test_changed_version_or_inventory(self):
        for raw in (payload(1, version=VERSION + 1), payload(1, echo=3), payload(1, echo=0)):
            self.assertEqual(self.shape(value(1, raw)), CHANGED)

    def test_dict_shape(self):
        complete = ("ClockBiosError", None, None, "Complete user-clock requests are unavailable")
        layout = ("ClockBiosError", None, None, "User-clock request layout is not understood")
        base = value(1)
        cases = [
            (None, complete), (list(base.items()), complete), ({k: v for k, v in base.items() if k != "mask"}, complete),
            (dict(base, extra=1), complete),
            (dict(base, version=VERSION + 1), layout), (dict(base, capacity=True), layout),
            (dict(base, row_bytes=0x304 * 1.0), layout), (dict(base, header_bytes=0x125), layout),
        ]
        for candidate, expected in cases:
            with self.subTest(candidate=str(candidate)[:40]):
                self.assertEqual(self.shape(candidate), expected)


# ---- C2: the INFO GET (_info_once), _decode and read() -------------------------------------------

INFO_BYTES = 68 + cbr.SIZE
INFO_ROUND_START = [("EnumAdapters2",)] + [("QueryAdapterInfo", h, 6, 12) for h in (111, 222, 333)]
INFO_ROUND_END = [("CloseAdapter", h) for h in (111, 222, 333)]
RECAPTURE = [("recapture",)]
NOTHING_CAPTURED = "unsupported current-policy transport: no recognized power GET captured"


def clock_header():
    """A captured A612 power-GET header carrying the understood paired envelope words."""
    words = perf_header()
    for index, word in cbr._ENVELOPE.items():
        words[index] = word
    return words


TRANSPORT = (clock_header(), dict(FIELDS))


def info_request(base=None):
    """What the INFO escape must receive: the header with words 2/14/15/16/5/11 set, zeros after."""
    words = list(base or TRANSPORT[0]) + [0] * (cbr.SIZE // 4)
    words[2], words[14], words[15], words[16], words[5], words[11] = INFO_BYTES, cbr.COMMAND, cbr.SIZE, 0, 0, 0
    return Block(struct.pack(f"<{len(words)}I", *words))


def info_round(base=None):
    return INFO_ROUND_START + [
        ("info-request", GUARD, info_request(base), GUARD),
        ("Escape", 111, FIELDS["hDevice"], FIELDS["Type"], FIELDS["Flags"], FIELDS["hContext"], INFO_BYTES),
    ] + INFO_ROUND_END


def stable_read_calls(*, discovery=True):
    """One attempt of read() whose two samples both succeed: (INFO, CONTROL) twice."""
    return info_round() + (DISCOVERY if discovery else []) + control_calls(MASK) + info_round() + control_calls(MASK)


def word_action(**writes):
    """An INFO escape action writing packet words over the driver's answer."""
    def action(rig, pointer, words):
        for index, word in writes.items():
            words[int(index[1:])] = word
    return action


def status_action(status):
    def action(rig, pointer, words):
        return status
    return action


def overrun_action(offset):
    """Writes one byte at `offset` from the end of the packet (0: just past it) or before it (negative)."""
    def action(rig, pointer, words):
        at = pointer + INFO_BYTES + offset if offset >= 0 else pointer + offset
        ctypes.memset(at, 0, 1)
    return action


def payload_action(offset, byte):
    def action(rig, pointer, words):
        ctypes.memset(pointer + 68 + offset, byte, 1)
    return action


class _Interfaces(dict):
    """The fake NVAPI interface table: a query of the INFO export lets the rig stand in for a fresh
    power-transport capture (see InfoRig.export_queried)."""

    def __init__(self, rig, items):
        super().__init__(items)
        self.rig = rig

    def get(self, key, default=None):
        if key == cbr.EXPORT:
            self.rig.export_queried()
        return super().get(key, default)


class InfoRig(ClockRig, PerfRig):
    """ClockRig's ClkDomCtlGet plus PerfRig's fake gdi32, answering the INFO escape.

    info: the INFO parameter bytes the driver copies at byte 68 (tests.test_clock_bios_runtime's
    info_payload by default); rm / completion: words 16 / 11 of every answer (word 5 is 1)
    info_script: one action per INFO escape, action(rig, pointer, words) after the answer; a non-None
      return value is that escape's NTSTATUS
    control_script: one status per CONTROL GET (after discovery), in place of control_status
    export: whether NVAPI resolves the INFO GET export 0x57B5A5DF (it is never called)
    recapture: while a side runs, its query of the INFO export re-stores TRANSPORT when the side's
      transport cache is empty (logged as ("recapture",)): both sides query the export at the same
      point of _info_once, after the binding and before reader._transport(), so this stands in for
      the fresh capture of Python's FakeGPU._current_limit_transport_layouts after read() dropped
      the cache. Without it the dropped cache cannot be refilled (no PowerPolInfo getter).
    """

    def __init__(self, *, info=None, rm=0, completion=1, info_script=(), control_script=(), export=True,
                 recapture=True, **kw):
        self.info = bytes(rp.info_payload() if info is None else info)
        self.rm, self.completion = rm, completion
        self.info_script_config, self.control_script_config = list(info_script), list(control_script)
        self.info_script, self.control_script = [], []
        self.recapture, self.side_gpu = recapture, None
        super().__init__(**kw)

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def info_export(handle, pointer):
            self.calls.append(("INFO export called",))
            return -1
        self._info_export = info_export
        interfaces = dict(self.nvapi.interfaces)
        if export:
            interfaces[cbr.EXPORT] = info_export
        self.nvapi.interfaces = _Interfaces(self, interfaces)

    @property
    def control_status(self):
        return self.control_script.pop(0) if self.control_script else self._control_status

    @control_status.setter
    def control_status(self, value):
        self._control_status = value

    def reset(self):
        PerfRig.reset(self)  # TransportRig's log and packet, PerfRig's architecture counter
        if hasattr(self, "accepted_config"):  # not while TransportRig.__init__ runs
            ClockRig.reset(self)
        self.info_script = list(getattr(self, "info_script_config", ()))
        self.control_script = list(getattr(self, "control_script_config", ()))

    def export_queried(self):
        gpu = self.side_gpu
        if gpu is not None and self.recapture and gpu._current_limit_transport is None:
            self.calls.append(("recapture",))
            gpu._current_limit_transport = TRANSPORT

    def _fake_gdi(self):
        table = super()._fake_gdi()

        @KMT
        def escape(pointer):
            try:
                e = NV.GPU._Escape.from_address(pointer)
                size, data = e.PrivateDriverDataSize, e.pPrivateDriverData
                self.calls.append(("info-request", ctypes.string_at(data - len(GUARD), len(GUARD)),
                                   Block(ctypes.string_at(data, size)), ctypes.string_at(data + size, len(GUARD))))
                self.calls.append(("Escape", e.hAdapter, e.hDevice, e.Type, e.Flags, e.hContext, size))
                return self.answer(data, size)
            except BaseException as exc:  # an exception would vanish inside the callback
                self.errors.append(repr(exc))
                return -1
        self._info_escape = escape
        table[ESCAPE] = escape
        return table

    def answer(self, pointer, size):
        words = (u32 * (size // 4)).from_address(pointer)
        ctypes.memmove(pointer + 68, self.info, min(len(self.info), size - 68))
        words[5], words[11], words[16] = 1, self.completion, self.rm
        status = self.escape_status
        action = self.info_script.pop(0) if self.info_script else None
        if action is not None:
            result = action(self, pointer, words)
            if result is not None:
                status = result
        return status


def cache(gpu):
    transport = getattr(gpu, "_current_limit_transport", None)
    if transport is not None:
        transport = (list(transport[0]), dict(transport[1]))
    return transport, getattr(gpu, "_current_limit_layout_choice", None) is not None


class InfoSide(Side):
    def info_once(self):
        if self.python:
            return cbr._info_once(self.gpu, self.reader)
        return ncb.info_once(self.gpu)

    def read(self):
        return cbr.read(self.gpu) if self.python else ncb.read(self.gpu)


def observation_of(rig, *, control=True, error=None):
    """Python's observation dict for the rig's INFO payload and the stable CONTROL block."""
    return {
        **cbr._LAYOUT,
        "identity": IDENTITY,
        "info_hex": rig.info.hex(),
        "user_control": {**_CONTROL_LAYOUT, "mask": MASK, "payload_hex": rig.expected_block(MASK).hex()}
        if control else None,
        "rows": cbr._decode(rig.info),
        "stable_reads": 2,
        "user_control_error": error,
    }


class InfoCase(ClockCase):
    def both(self, rig, action=lambda side: side.read(), *, assign=TRANSPORT, choice=True):
        """action(side) on the Python twin, then on the core, from one fake state and one stored transport:
        (outcome, call log, transport cache afterwards), asserted equal."""
        opened = rig.native_gpu()
        runs = []
        try:
            twin = Card.python_twin(rig, opened)
            twin.nvapi.ClkDomCtlGet = rig.names.get("ClkDomCtlGet")
            for side in (InfoSide(twin, True), InfoSide(opened, False)):
                rig.reset()
                side.gpu._current_limit_transport = assign
                if choice:
                    side.gpu._current_limit_layout_choice = {"seeded": True} if side.python else 0
                rig.side_gpu = side.gpu
                try:
                    if side.python:
                        with rig.python_windll():
                            result = outcome(lambda: action(side))
                    else:
                        result = outcome(lambda: action(side))
                finally:
                    rig.side_gpu = None
                runs.append((result, list(rig.calls), cache(side.gpu)))
        finally:
            opened.close()
        self.assertEqual(rig.errors, [])
        self.assertEqual(runs[0], runs[1], "Python and native runs differ")
        return runs[0]


KEPT = ((TRANSPORT[0], TRANSPORT[1]), True)  # the stored transport and the seeded layout choice
DROPPED = (None, False)


class DecodeTests(unittest.TestCase):
    def same(self, raw):
        results = [outcome(lambda: cbr._decode(raw)), outcome(lambda: ncb.decode(raw))]
        self.assertEqual(results[0], results[1], "Python and native decodes differ")
        if results[0][0] == "ok":
            self.assertEqual(repr(results[0][1]), repr(results[1][1]))  # key order and value types too
        return results[0]

    def test_the_runtime_fixture(self):
        kind, rows = self.same(rp.info_payload())
        self.assertEqual(kind, "ok")
        self.assertEqual(list(rows), ["1", "30"])
        self.assertEqual((rows["1"]["minimum_mhz"], rows["1"]["maximum_mhz"]), (-200, 400))
        self.assertIsNone(rows["30"]["minimum_mhz"])

    def test_masks_over_all_32_rows_typed_and_untyped(self):
        raw = bytearray(cbr.SIZE)
        for index in range(32):
            at = cbr.HEADER + index * cbr.STRIDE
            kinds = [(16, 16), (16, 15), (15, 16), (0, 0), (255, 16)]
            raw[at], raw[at + 1] = kinds[index % len(kinds)]
            struct.pack_into("<I", raw, at + 4, 0x80000000 >> index | index)
            struct.pack_into("<hh", raw, at + 0x26, -32768 + index, 32767 - index)
        for mask in (0, 1, 1 << 31, 0xFFFFFFFF, 0xAAAAAAAA, 0x55555555, 0x80000001):
            with self.subTest(mask=hex(mask)):
                struct.pack_into("<I", raw, 4, mask)
                kind, rows = self.same(bytes(raw))
                self.assertEqual((kind, len(rows)), ("ok", bin(mask).count("1")))

    def test_negative_and_extreme_int16_values(self):
        for low, high in ((-1, 0), (-32768, 32767), (32767, -32768), (-200, -100)):
            with self.subTest(low=low, high=high):
                kind, rows = self.same(rp.info_payload(index=5, minimum=low, maximum=high))
                self.assertEqual((rows["5"]["minimum_mhz"], rows["5"]["maximum_mhz"]), (low, high))

    def test_seeded_random_payloads(self):
        generator = random.Random(0xC10C)
        for _ in range(40):
            raw = bytearray(generator.randbytes(cbr.SIZE))
            for index in range(32):
                if generator.random() < 0.4:
                    at = cbr.HEADER + index * cbr.STRIDE
                    raw[at] = raw[at + 1] = 16
            self.assertEqual(self.same(bytes(raw))[0], "ok")

    def test_the_wrong_size(self):
        refused = ("ClockBiosError", None, None, "Clock INFO geometry is not understood")
        for size in (0, 4, cbr.SIZE - 1, cbr.SIZE + 1, 2 * cbr.SIZE):
            with self.subTest(size=size):
                self.assertEqual(self.same(bytes(size)), refused)
        self.assertEqual(self.same(bytearray(cbr.SIZE))[0], "ok")

    def test_every_unsigned_byte_buffer_decodes_as_its_bytes(self):
        raw = bytearray(rp.info_payload())
        at = cbr.HEADER + 30 * cbr.STRIDE
        raw[at:at + 2] = b"\xff\x80"  # row 30's type bytes above 0x7F (signed for format 'b')
        expected = self.same(bytes(raw))
        self.assertEqual(expected[0], "ok")
        self.assertEqual((expected[1]["30"]["type"], expected[1]["30"]["base_type"]), (0xFF, 0x80))
        for candidate in (bytearray(raw), array.array("B", raw), memoryview(bytes(raw)), memoryview(bytearray(raw)),
                          (ctypes.c_uint8 * cbr.SIZE).from_buffer_copy(raw)):
            with self.subTest(type=type(candidate).__name__):
                self.assertEqual(self.same(candidate), expected)

    def test_type_rules_before_and_after_the_geometry(self):
        for candidate in (None, 5, 1.5, object()):  # no length: len() raises first
            with self.subTest(candidate=type(candidate).__name__):
                self.assertEqual(self.same(candidate)[:2], ("TypeError", None))
        geometry = ("ClockBiosError", None, None, "Clock INFO geometry is not understood")
        for candidate in ([0] * 4, "x" * 4, (), {}, array.array("I", [0] * 4)):  # the length is checked next
            with self.subTest(candidate=repr(candidate)[:30]):
                self.assertEqual(self.same(candidate), geometry)
        for candidate in ([0] * cbr.SIZE, "x" * cbr.SIZE, tuple(range(256)) * (cbr.SIZE // 256 + 1)):
            if len(candidate) != cbr.SIZE:
                candidate = candidate[:cbr.SIZE]
            with self.subTest(candidate=type(candidate).__name__):  # no buffer protocol: struct's own TypeError
                self.assertEqual(self.same(candidate), ("TypeError", None, None,
                                                        f"a bytes-like object is required, not "
                                                        f"'{type(candidate).__name__}'"))
        strided = memoryview(rp.info_payload() * 2)[::2]  # struct.unpack_from refuses a strided view
        self.assertEqual(self.same(strided), ("BufferError", None, None,
                                              "memoryview: underlying buffer is not C-contiguous"))

    def test_buffers_python_indexes_by_element_are_refused(self):
        """Unmirrored: _decode reads these by byte offset and indexes them by element."""
        raw = rp.info_payload()
        for candidate in (memoryview(bytes(cbr.SIZE * 4)).cast("I"), array.array("I", bytes(cbr.SIZE * 4)),
                          memoryview(raw).cast("b"), array.array("b", raw), memoryview(raw).cast("c"),
                          memoryview(bytes(cbr.SIZE * 2)).cast("B", (cbr.SIZE, 2))):
            with self.subTest(format=memoryview(candidate).format, ndim=memoryview(candidate).ndim):
                self.assertEqual(len(candidate), cbr.SIZE)
                kind, _, _, message = outcome(lambda: ncb.decode(candidate))
                self.assertEqual(kind, "TypeError")
                self.assertIn("mirrors _decode for single-byte unsigned buffers only", message)


class InfoOnceTests(InfoCase):
    def test_the_guarded_get_returns_the_identity_and_the_info_bytes(self):
        rig = InfoRig()
        result, calls, stored = self.both(rig, lambda side: side.info_once())
        self.assertEqual(result, ("ok", (IDENTITY, rig.info)))
        self.assertEqual((calls, stored), (info_round(), KEPT))

    def test_the_export_is_checked_after_the_binding_and_before_the_transport(self):
        result, calls, stored = self.both(InfoRig(export=False, recapture=False), lambda side: side.info_once(),
                                          assign=None)
        self.assertEqual(result, ("PerfDiagnosticError", "unsupported", None, "Clock INFO GET export is unavailable"))
        self.assertEqual((calls, stored), ([], (None, True)))
        result, calls, _ = self.both(InfoRig(export=False, nvml_devid=0x1B02), lambda side: side.info_once())
        self.assertEqual(result[:2], ("PerfDiagnosticError", "identity"))
        self.assertEqual(calls, [])

    def test_transport_failures_keep_their_kind(self):
        rm26 = (perf_header(3548, 3480, command=0x20802612), dict(FIELDS))
        for k, word in cbr._ENVELOPE.items():
            rm26[0][k] = word
        cases = [(None, ("PerfDiagnosticError", "transient", None, NOTHING_CAPTURED)),
                 (rm26, ("PerfDiagnosticError", "unsupported", None,
                         "PERF diagnostics need the validated modern RM transport"))]
        for assign, expected in cases:
            with self.subTest(expected[1]):
                result, calls, _ = self.both(InfoRig(recapture=False), lambda side: side.info_once(), assign=assign)
                self.assertEqual((result, calls), (expected, []))

    def test_every_envelope_word_of_the_transport(self):
        refused = ("PerfDiagnosticError", "malformed", None, "Clock INFO needs the understood paired envelope")
        for index in cbr._ENVELOPE:
            with self.subTest(word=index):
                words = clock_header()
                words[index] ^= 1
                result, calls, stored = self.both(InfoRig(), lambda side: side.info_once(),
                                                  assign=(words, dict(FIELDS)))
                self.assertEqual((result, calls), (refused, []))
                self.assertEqual(stored, ((words, dict(FIELDS)), True))
        words = clock_header()  # the other header words are copied into the packet as they are
        words[6], words[12] = 0xDEAD, 0xBEEF
        result, calls, _ = self.both(InfoRig(), lambda side: side.info_once(), assign=(words, dict(FIELDS)))
        self.assertEqual((result[0], calls), ("ok", info_round(words)))

    def test_rm_status(self):
        for rm, kind in ((0x1F, "unsupported"), (0x56, "unsupported"), (0x77, "transient"), (1, "transient"),
                         (0xFFFFFFFF, "transient")):
            with self.subTest(rm=hex(rm)):
                result, calls, _ = self.both(InfoRig(rm=rm), lambda side: side.info_once())
                self.assertEqual((result, calls), (("PerfDiagnosticError", kind, None, f"Clock INFO RM status {rm}"),
                                                   info_round()))

    def test_completion_and_echo(self):
        geometry = ("PerfDiagnosticError", "malformed", None, "Clock INFO changed its envelope or geometry")
        completion = ("PerfDiagnosticError", "transient", None, "Clock INFO completion is unavailable")
        cases = [(word_action(w14=0x2080D01C), geometry), (word_action(w15=1), geometry),
                 (word_action(w2=INFO_BYTES - 4), geometry), (word_action(w0=0), geometry),
                 (word_action(w3=0), geometry), (word_action(w12=1), geometry), (word_action(w5=2), geometry),
                 (word_action(w5=0xFFFFFFFF), geometry), (word_action(w11=0), completion),
                 (word_action(w11=2), completion), (word_action(w5=0), "ok"), (word_action(w17=7), "ok"),
                 (word_action(w14=0x2080D01C, w16=0x56), geometry), (word_action(w11=0, w16=0x56), completion),
                 (word_action(w5=2, w11=0), geometry)]
        for action, expected in cases:
            with self.subTest(expected=expected):
                result, calls, _ = self.both(InfoRig(info_script=[action]), lambda side: side.info_once())
                self.assertEqual(result[0] if expected == "ok" else result, expected)
                self.assertEqual(calls, info_round())

    def test_every_echoed_header_word(self):
        """Words 0-15 must come back as sent, except 5 (0 or 1) and 11 (the completion)."""
        geometry = ("PerfDiagnosticError", "malformed", None, "Clock INFO changed its envelope or geometry")
        for index in range(16):
            if index in (5, 11):
                continue
            for flip in (1, 0x80000000):
                with self.subTest(word=index, flip=hex(flip)):
                    action = lambda r, p, w, i=index, f=flip: w.__setitem__(i, w[i] ^ f)
                    result, calls, _ = self.both(InfoRig(info_script=[action]), lambda side: side.info_once())
                    self.assertEqual((result, calls), (geometry, info_round()))

    def test_the_transport_is_validated_before_its_envelope(self):
        """A transport refused by the current-limit check or by reader._transport() wins over a wrong
        envelope word: Python checks the envelope only on the transport reader._transport() returns."""
        for index in cbr._ENVELOPE:
            geometry = clock_header()
            geometry[index] ^= 1
            geometry[15] = 1000  # refused by the current-limit transport check, which drops the cache
            rm26 = perf_header(3548, 3480, command=0x20802612)  # refused by reader._transport()
            for k, word in cbr._ENVELOPE.items():
                rm26[k] = word
            rm26[index] ^= 1
            for words, expected, stored_after in (
                    (geometry, "unvalidated current-policy transport geometry", None),
                    (rm26, "PERF diagnostics need the validated modern RM transport", (rm26, dict(FIELDS)))):
                with self.subTest(word=index, expected=expected):
                    result, calls, stored = self.both(InfoRig(recapture=False), lambda side: side.info_once(),
                                                      assign=(words, dict(FIELDS)))
                    self.assertEqual((result, calls), (("PerfDiagnosticError", "unsupported", None, expected), []))
                    self.assertEqual(stored[0], stored_after)

    def test_the_escape_status_and_an_escape_that_was_not_issued(self):
        failed = ("PerfDiagnosticError", "transient", None, f"Clock INFO transport failed: {NTSTATUS_INVALID_PARAMETER}")
        result, calls, stored = self.both(InfoRig(escape_status=NTSTATUS_INVALID_PARAMETER),
                                          lambda side: side.info_once())
        self.assertEqual((result, calls, stored), (failed, info_round(), KEPT))  # the cache is not dropped here
        none = ("PerfDiagnosticError", "transient", None, "Clock INFO transport failed: None")
        for rig, log in ((InfoRig(missing={ESCAPE}), []), (InfoRig(gdi=False), []),
                         (InfoRig(adapters=[(7, 2, 0, 0)]), [("EnumAdapters2",), ("QueryAdapterInfo", 7, 6, 12),
                                                             ("CloseAdapter", 7)])):
            with self.subTest(adapters=rig.adapters, gdi=rig.with_gdi, missing=sorted(rig.missing)):
                self.assertEqual(self.both(rig, lambda side: side.info_once()), (none, log, KEPT))

    def test_guards_come_before_the_status_and_the_status_before_the_echo(self):
        overrun = ("PerfDiagnosticError", "malformed", None, "Clock GET exceeded the guarded allocation")
        for offset in (0, 63, -1, -64):
            with self.subTest(offset=offset):
                rig = InfoRig(info_script=[lambda r, p, w, o=offset: (overrun_action(o)(r, p, w), -5)[1]])
                self.assertEqual(self.both(rig, lambda side: side.info_once())[0], overrun)
        rig = InfoRig(info_script=[lambda r, p, w: (word_action(w14=1)(r, p, w), -5)[1]])
        self.assertEqual(self.both(rig, lambda side: side.info_once())[0],
                         ("PerfDiagnosticError", "transient", None, "Clock INFO transport failed: -5"))

    def test_the_sticky_identity_is_held_until_reset(self):
        def states(side):
            seen = [held(side), outcome(side.info_once)[0], held(side)]
            side.fresh()
            seen.append(held(side))
            return seen
        self.assertEqual(self.both(InfoRig(), states)[0], ("ok", [False, "ok", True, False]))
        # the binding comes first, so a GET that then fails still holds the identity; a refused binding does not
        self.assertEqual(self.both(InfoRig(rm=0x56), states)[0], ("ok", [False, "PerfDiagnosticError", True, False]))
        self.assertEqual(self.both(InfoRig(arch=3), states)[0], ("ok", [False, "PerfDiagnosticError", False, False]))


class ReadTests(InfoCase):
    def test_guarded_pair_and_unknown_rows(self):
        rig = InfoRig(preload={1: {"mode": 15, "freq": 1234}})
        (kind, (observation, error)), calls, stored = self.both(rig)
        self.assertEqual((kind, error), ("ok", None))
        self.assertEqual(observation, observation_of(rig))
        self.assertEqual(repr(observation), repr(observation_of(rig)))  # key order and value types
        self.assertEqual(observation["rows"]["1"]["minimum_mhz"], -200)
        self.assertIsNone(observation["rows"]["30"]["minimum_mhz"])
        self.assertEqual((calls, stored), (stable_read_calls(), KEPT))
        self.assertTrue(cbr._shape(observation, writable=True))

    def test_a_transient_info_failure_is_retried_with_the_transport_dropped(self):
        for action, message in ((status_action(NTSTATUS_INVALID_PARAMETER), "transport failed"),
                                (word_action(w11=0), "completion"), (word_action(w16=0x77), "RM status 119")):
            with self.subTest(message):
                rig = InfoRig(info_script=[action])
                (kind, (observation, error)), calls, stored = self.both(rig)
                self.assertEqual((kind, error, observation), ("ok", None, observation_of(rig)))
                self.assertEqual(calls, info_round() + RECAPTURE + stable_read_calls())
                self.assertEqual(stored, (KEPT[0], stored[1]))
                self.assertEqual(sum(call[0] == "Escape" for call in calls), 3)

    def test_a_transient_control_failure_is_retried_independently(self):
        rig = InfoRig(control_script=[-5])
        (kind, (observation, error)), calls, _ = self.both(rig)
        self.assertEqual((kind, error, observation), ("ok", None, observation_of(rig)))
        self.assertEqual(calls, stable_read_calls() + RECAPTURE + stable_read_calls(discovery=False))
        self.assertEqual(sum(call[0] == "Escape" for call in calls), 4)

    def test_a_missing_control_getter_keeps_the_info_diagnostic(self):
        rig = InfoRig(with_get=False)
        (kind, (observation, error)), calls, stored = self.both(rig)
        self.assertEqual((kind, error), ("ok", None))
        self.assertEqual(observation, observation_of(rig, control=False, error="User-clock CONTROL GET is unavailable"))
        self.assertEqual(calls, info_round() * 2 + RECAPTURE + info_round() * 2)
        with self.assertRaises(cbr.ClockBiosError):
            cbr._shape(observation, writable=True)
        self.assertTrue(cbr._shape(observation))

    def test_an_unstable_user_control_is_a_partial_observation(self):
        rig = InfoRig(drift=True)
        (kind, (observation, error)), calls, _ = self.both(rig)
        self.assertEqual((kind, error), ("ok", None))
        self.assertIsNone(observation["user_control"])
        self.assertEqual(observation["user_control_error"], "User-clock requests were not stable")
        self.assertEqual(sum(call[0] == "Escape" for call in calls), 4)

    def test_a_partial_observation_survives_a_failed_second_attempt(self):
        for options, message in ((dict(control_script=[-5], recapture=False), "User-clock requests were not stable"),
                                 (dict(control_script=[0, -7], recapture=False), "User-clock CONTROL GET failed: -7")):
            with self.subTest(message):
                rig = InfoRig(**options)
                (kind, (observation, error)), calls, stored = self.both(rig)
                self.assertEqual((kind, error), ("ok", None))
                self.assertEqual(observation, observation_of(rig, control=False, error=message))
                self.assertEqual((calls, stored), (stable_read_calls(), DROPPED))
        # a non-transient failure in the second attempt discards the partial observation
        rig = InfoRig(control_script=[-5], info_script=[None, None, word_action(w16=0x1F)])
        result, calls, _ = self.both(rig)
        self.assertEqual(result, ("ok", (None, "Clock INFO RM status 31")))
        self.assertEqual(calls, stable_read_calls() + RECAPTURE + info_round())

    def test_the_second_partial_observation_replaces_the_first(self):
        for script, message in (([-5, 0, 0, -7], "User-clock CONTROL GET failed: -7"),
                                ([0, -7, -5, 0], "User-clock requests were not stable")):
            with self.subTest(message):
                rig = InfoRig(control_script=script)
                (kind, (observation, error)), calls, stored = self.both(rig)
                self.assertEqual((kind, error), ("ok", None))
                self.assertEqual(observation, observation_of(rig, control=False, error=message))
                self.assertEqual(calls, stable_read_calls() + RECAPTURE + stable_read_calls(discovery=False))
                self.assertEqual(stored[0], KEPT[0])

    def test_an_info_failure_on_the_second_sample(self):
        first_sample = info_round() + DISCOVERY + control_calls(MASK)
        rig = InfoRig(info_script=[None, status_action(-5)])  # transient: retried with the cache dropped
        (kind, (observation, error)), calls, stored = self.both(rig)
        self.assertEqual((kind, error, observation), ("ok", None, observation_of(rig)))
        self.assertEqual(calls, first_sample + info_round() + RECAPTURE + stable_read_calls(discovery=False))
        self.assertEqual(stored[0], KEPT[0])
        rig = InfoRig(info_script=[None, word_action(w16=0x1F)])  # unsupported: stops, the cache kept
        result, calls, stored = self.both(rig)
        self.assertEqual(result, ("ok", (None, "Clock INFO RM status 31")))
        self.assertEqual((calls, stored), (first_sample + info_round(), KEPT))
        rig = InfoRig(info_script=[None, overrun_action(0)])  # a guard overrun on the second sample stops too
        result, calls, stored = self.both(rig)
        self.assertEqual(result, ("ok", (None, "Clock GET exceeded the guarded allocation")))
        self.assertEqual((calls, stored), (first_sample + info_round(), KEPT))

    def test_a_partial_second_attempt_replaces_a_transient_first(self):
        rig = InfoRig(info_script=[status_action(-5)], control_script=[0, -7])
        (kind, (observation, error)), calls, stored = self.both(rig)
        self.assertEqual((kind, error), ("ok", None))
        self.assertEqual(observation,
                         observation_of(rig, control=False, error="User-clock CONTROL GET failed: -7"))
        self.assertEqual(calls, info_round() + RECAPTURE + stable_read_calls())
        self.assertEqual(stored[0], KEPT[0])

    def test_no_retry_on_unsupported_or_a_corrupt_envelope(self):
        for action, message in ((word_action(w14=0x2080D01C), "Clock INFO changed its envelope or geometry"),
                                (word_action(w15=1), "Clock INFO changed its envelope or geometry"),
                                (word_action(w5=2), "Clock INFO changed its envelope or geometry"),
                                (word_action(w16=0x56), "Clock INFO RM status 86"),
                                (word_action(w16=0x1F), "Clock INFO RM status 31")):
            with self.subTest(message):
                result, calls, stored = self.both(InfoRig(info_script=[action]))
                self.assertEqual(result, ("ok", (None, message)))
                self.assertEqual((calls, stored), (info_round(), KEPT))

    def test_info_and_control_guard_corruption_stop(self):
        overrun = ("ok", (None, "Clock GET exceeded the guarded allocation"))
        result, calls, stored = self.both(InfoRig(info_script=[overrun_action(0)]))
        self.assertEqual((result, calls, stored), (overrun, info_round(), KEPT))
        result, calls, stored = self.both(InfoRig(overrun=1))
        self.assertEqual((result, calls, stored), (overrun, info_round() + DISCOVERY + control_calls(MASK), KEPT))

    def test_pairing_export_and_identity_failures(self):
        result, calls, _ = self.both(InfoRig(nvml_devid=0x1B02))
        self.assertEqual((result[0], result[1][0], calls), ("ok", None, []))
        self.assertIn("NVAPI and NVML disagree", result[1][1])
        result, calls, _ = self.both(InfoRig(export=False))
        self.assertEqual((result, calls), (("ok", (None, "Clock INFO GET export is unavailable")), []))
        for options, message in ((dict(arch=3), "Modern PERF groups require a Pascal-or-later architecture"),
                                 (dict(arch=UNKNOWN), "GPU architecture could not be read")):
            with self.subTest(message):
                result, calls, stored = self.both(InfoRig(**options))
                self.assertEqual(result, ("ok", (None, message)))
                self.assertEqual(calls, [])
                # the architecture refusal is unsupported (stop); the unreadable one is transient, so the
                # cache is dropped and the retry is refused by the binding before anything refills it
                self.assertEqual(stored, KEPT if options["arch"] == 3 else DROPPED)

    def test_a_transport_failure_is_bounded(self):
        result, calls, stored = self.both(InfoRig(recapture=False), assign=None)
        self.assertEqual((result, calls, stored), (("ok", (None, NOTHING_CAPTURED)), [], DROPPED))
        # a transient escape failure drops the cache after the first attempt: nothing refills it
        rig = InfoRig(recapture=False, escape_status=NTSTATUS_INVALID_PARAMETER)
        result, calls, stored = self.both(rig)
        self.assertEqual((result, calls, stored), (("ok", (None, NOTHING_CAPTURED)), info_round(), DROPPED))
        # an unsupported transport stops at once and keeps the cache
        words = clock_header()
        words[15] = 1000
        result, calls, stored = self.both(InfoRig(), assign=(words, dict(FIELDS)))
        self.assertEqual(result, ("ok", (None, "unvalidated current-policy transport geometry")))
        self.assertEqual(calls, [])

    def test_a_persistent_transient_failure_reports_the_last_message(self):
        none = "Clock INFO transport failed: None"
        for rig, message, rounds in (
                (InfoRig(rm=0x77), "Clock INFO RM status 119", info_round()),
                (InfoRig(completion=0), "Clock INFO completion is unavailable", info_round()),
                (InfoRig(missing={ESCAPE}), none, []), (InfoRig(gdi=False), none, [])):
            with self.subTest(message):
                result, calls, _ = self.both(rig)
                self.assertEqual(result, ("ok", (None, message)))
                self.assertEqual(calls, rounds + RECAPTURE + rounds)

    def test_info_that_changes_between_the_two_samples_is_retried(self):
        rig = InfoRig(info_script=[None, payload_action(100, 0x77)])
        (kind, (observation, error)), calls, _ = self.both(rig)
        self.assertEqual((kind, error, observation), ("ok", None, observation_of(rig)))
        self.assertEqual(calls, stable_read_calls() + RECAPTURE + stable_read_calls(discovery=False))
        rig = InfoRig(recapture=False, info_script=[None, payload_action(100, 0x77)])
        self.assertEqual(self.both(rig)[0], ("ok", (None, NOTHING_CAPTURED)))
        rig = InfoRig(info_script=[None, payload_action(100, 0x77)] * 2)
        self.assertEqual(self.both(rig)[0], ("ok", (None, "Clock INFO changed between fresh reads")))

    def test_the_read_uses_a_fresh_reader_and_leaves_the_handles_alone(self):
        def states(side):
            seen = [held(side)]
            seen.append(side.read()[1])
            seen.append(held(side))
            side.info_once()
            seen.append(held(side))
            seen.append(side.read()[1])
            seen.append(held(side))
            return seen
        self.assertEqual(self.both(InfoRig(), states)[0], ("ok", [False, None, False, True, None, True]))


class AbiArgumentTests(unittest.TestCase):
    def tearDown(self):
        native.set_export_resolver(None)

    def test_null_arguments_wrong_sizes_and_a_short_capacity_touch_nothing(self):
        library = ncb.load()
        rig = InfoRig()
        opened = rig.native_gpu()
        try:
            rig.reset()
            handle = opened._handle
            target = native._Target()
            target.size = ctypes.sizeof(native._Target)
            bad_target = native._Target()
            bad_target.size = 1
            payload = ctypes.create_string_buffer(ncb.INFO_SIZE)
            error = ncb._new_error()
            bad_error = native._Error()
            bad_error.size = 3
            rows, bad_rows = ncb._Rows(), ncb._Rows()
            rows.size = ctypes.sizeof(ncb._Rows)
            observation, bad_observation = ncb._Observation(), ncb._Observation()
            observation.size = ctypes.sizeof(ncb._Observation)
            bad_observation.size = observation.size - 1
            byref = ctypes.byref
            info = library.dc_clock_bios_info
            cases = [
                (info(None, byref(target), payload, ncb.INFO_SIZE, byref(error)), native.DC_E_ARGUMENT),
                (info(handle, None, payload, ncb.INFO_SIZE, byref(error)), native.DC_E_ARGUMENT),
                (info(handle, byref(bad_target), payload, ncb.INFO_SIZE, byref(error)), native.DC_E_ARGUMENT),
                (info(handle, byref(target), None, ncb.INFO_SIZE, byref(error)), native.DC_E_ARGUMENT),
                (info(handle, byref(target), payload, ncb.INFO_SIZE, byref(bad_error)), native.DC_E_ARGUMENT),
                (info(handle, byref(target), payload, ncb.INFO_SIZE - 1, byref(error)), native.DC_E_BUFFER),
                (info(handle, byref(target), None, 0, byref(error)), native.DC_E_BUFFER),
                (library.dc_clock_bios_decode(None, 4, byref(rows), byref(error)), native.DC_E_ARGUMENT),
                (library.dc_clock_bios_decode(bytes(cbr.SIZE), cbr.SIZE, None, byref(error)), native.DC_E_ARGUMENT),
                (library.dc_clock_bios_decode(bytes(cbr.SIZE), cbr.SIZE, byref(bad_rows), byref(error)),
                 native.DC_E_ARGUMENT),
                (library.dc_clock_bios_decode(bytes(cbr.SIZE), cbr.SIZE, byref(rows), byref(bad_error)),
                 native.DC_E_ARGUMENT),
                (library.dc_clock_bios_read(None, byref(observation), byref(error)), native.DC_E_ARGUMENT),
                (library.dc_clock_bios_read(handle, None, byref(error)), native.DC_E_ARGUMENT),
                (library.dc_clock_bios_read(handle, byref(bad_observation), byref(error)), native.DC_E_ARGUMENT),
                (library.dc_clock_bios_read(handle, byref(observation), None), native.DC_E_ARGUMENT),
                (library.dc_clock_bios_read(handle, byref(observation), byref(bad_error)), native.DC_E_ARGUMENT),
                (library.dc_clock_bios_reset(None), native.DC_E_ARGUMENT),
                (library.dc_clock_bios_held(None, byref(ctypes.c_int32())), native.DC_E_ARGUMENT),
                (library.dc_clock_bios_held(handle, None), native.DC_E_ARGUMENT),
            ]
            for index, (status, expected) in enumerate(cases):
                with self.subTest(case=index):
                    self.assertEqual(status, expected)
            self.assertEqual(rig.calls, [])
            self.assertFalse(ncb.held(opened))
            # a NULL raw of size 0 is the empty bytes object: refused by _decode itself
            status = library.dc_clock_bios_decode(None, 0, byref(rows), byref(error))
            self.assertEqual((status, native._text(error.message)),
                             (native.DC_E_UNRECOGNIZED, "Clock INFO geometry is not understood"))
        finally:
            opened.close()


if __name__ == "__main__":
    unittest.main()
