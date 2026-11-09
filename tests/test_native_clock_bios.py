# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native clock-BIOS reader, increment C1 (the guarded user-clock CONTROL GET and the pure
_control_shape) against clock_bios_runtime.

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
architecture read instead."""
import ctypes
import struct
import unittest
from fractions import Fraction
from types import MappingProxyType
from unittest import mock

from druta import clock_bios_runtime as cbr, native
from druta.clock_bios_runtime import _CONTROL_LAYOUT
from druta.perf_diagnostics import PerfDiagnosticsReader
from tests.test_native_clkdom import GET_ID, SIZE, UNKNOWN, VERSION, Block, ClkDomRig
from tests.test_native_identity import TITAN
from tests.test_native_target_binding import Card

if not native.available():
    raise unittest.SkipTest("native core not built (native/build.sh linux)")

from druta import native_clock_bios as ncb  # noqa: E402

CDECL = ctypes.CFUNCTYPE
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


if __name__ == "__main__":
    unittest.main()
