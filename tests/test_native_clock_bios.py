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
received and the 64 bytes after them, which must still be the 0xA5 guard: so the core hands the
driver exactly the declared allocation with the version at 0, the mask at 8 and zeros elsewhere.
Each case compares the result or the exception (type, PerfDiagnosticError kind and status, message)
and the whole call log.

Unreachable natively, and so covered only by tests/test_clock_bios_runtime.py: a clkdom_domains
inventory that is not a list or tuple, has duplicates, or holds a value that is not an int in 0..31
(ClkDom::domains builds unique probed indices); a non-int GET status; an _identity refusal of the
binding's own values (a device id above 16 bits, a PCI bus/device/function out of range: the core
binding yields neither); the sticky "GPU binding changed" refusal (none of its identity elements can
change under the public fakes, as tests/test_native_target_binding.py notes). A pairing mismatch
disables NVAPI, so the inventory is already unavailable before the binding is reached; the
binding's own identity and transient refusals are covered through the NVML PCI identity and the
architecture read instead."""
import ctypes
import struct
import unittest

from druta import clock_bios_runtime as cbr, native
from druta.clock_bios_runtime import ClockBiosError, _CONTROL_LAYOUT
from druta.perf_diagnostics import PerfDiagnosticError, PerfDiagnosticsReader
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
    return [("control-request", request(mask), GUARD), ("ClkDomCtlGet", mask)]


class ClockRig(ClkDomRig):
    """ClkDomRig whose control GET (any GET after discovery) can misbehave after answering.

    control_status: the control GET returns this status
    overrun / underrun: bytes the driver writes just past the end / before the start of the packet
    words: {word index: value} the driver writes over its answer (the version and mask echoes)
    """

    def __init__(self, *, control_status=0, overrun=0, underrun=0, words=None, arch=7, accepted=ACCEPTED, **kw):
        self.control_status, self.overrun, self.underrun = control_status, overrun, underrun
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
            self.calls.append(("control-request", Block(ctypes.string_at(pointer, SIZE)),
                               ctypes.string_at(pointer + SIZE, len(GUARD))))
            status = base(handle, pointer)
            words = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32))
            for index, value in self.words.items():
                words[index] = value
            if self.overrun:
                ctypes.memset(pointer + SIZE, 0xEE, self.overrun)
            if self.underrun:
                ctypes.memset(pointer - self.underrun, 0xEE, self.underrun)
            return self.control_status or status
        self.names["ClkDomCtlGet"] = self.nvapi.interfaces[GET_ID] = get

    def reset(self):
        super().reset()
        self.probe_next = 0


def outcome(call):
    try:
        return ("ok", call())
    except PerfDiagnosticError as exc:
        return ("PerfDiagnosticError", exc.kind, exc.status, str(exc))
    except ClockBiosError as exc:
        return ("ClockBiosError", None, None, str(exc))


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
        for options in (dict(overrun=1), dict(overrun=64), dict(underrun=4), dict(overrun=4, control_status=-1)):
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
