# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native PERF diagnostics reader, increment P1 (the group layouts, _transport and _get)
against perf_diagnostics.PerfDiagnosticsReader.

druta.native_perf_diagnostics runs dc_perf_layouts / dc_perf_transport / dc_perf_get /
dc_perf_reset. The Python original runs on an nvbackend.GPU twin built as in
tests/test_native_target_binding.py (GPU.__new__ with the identity records the native handle
exposes, so _binding, slot() and arch() are the real methods), and its
_current_limit_transport_layouts and _legacy_clk_escape are the real nvbackend ones reaching the
same fake gdi32 the core resolves (tests/test_native_transport.py). A fixture transport is stored
on both sides through the _current_limit_transport attribute / setter, as
tests/test_native_rm_transport.py does; the fresh-capture cases use its HookRig.

Every case compares the result or the exception ((PerfDiagnosticError kind, message, status) or
the ValueError text), the gdi call log including the packet bytes the escape received, and the
transport cache afterwards (the layout choice is a dict on the twin and an index on the core, so
only whether one is set is compared).

Documented divergence (shared with dc_rm_power_layouts, tests/test_native_rm_transport.py): when
gdi32 lacks an escape export the core's capture message names the missing export after Python's
text; the kind is the same.

Unreachable natively, and so covered only by tests/test_perf_diagnostics.py: a cached transport
that is not a (header, fields) pair, header words that are not 32-bit ints and an escape-field set
other than the five fields (the core's types hold these by construction), a transport header
longer than 17 words passed to _get, and a command or size that is not an int but compares equal
to an allowed one (the binding passes 0, refused as not allowed)."""
import ctypes
import struct
from types import SimpleNamespace
import unittest
from dataclasses import asdict

from druta import native, nvbackend
from druta.perf_diagnostics import LAYOUTS, PROBE_ARCHITECTURES, _GET_COMMANDS, PerfDiagnosticError, PerfDiagnosticsReader
from tests.test_native_rm_transport import ARCH_STEP, FIELDS as HOOK_FIELDS, HookCase, HookRig, header as rm_header
from tests.test_native_rm_transport import owner, power_packet
from tests.test_native_target_binding import Card
from tests.test_native_transport import ENUM2, ESCAPE, FIELDS, KMT, NTSTATUS_INVALID_PARAMETER, TransportRig

if not native.available():
    raise unittest.SkipTest("native core not built (native/build.sh linux)")

from druta import native_perf_diagnostics as perf  # noqa: E402

NV = nvbackend
u32 = ctypes.c_uint32
A612, P2612 = 0x2080A612, 0x20802612
PAYLOAD_MARK = 0xC0DE0000


def header(packet_bytes=54420, parameter_bytes=54352, *, command=A612, marks=(9988, 7766)):
    """A captured power-GET header (words 0..16) with recognisable words elsewhere."""
    words = [0x11110000 | k for k in range(17)]
    words[2], words[14], words[15], words[16] = packet_bytes, command, parameter_bytes, 0
    words[12], words[13] = marks
    return words


MODERN = (header(), dict(FIELDS))


def echo(words):
    """The driver's answer: the request geometry echoed, RM status 0, a payload pattern."""
    for k in range(17, len(words)):
        words[k] = PAYLOAD_MARK | (k & 0xFFFF)


def expected_payload(size):
    return b"".join(struct.pack("<I", PAYLOAD_MARK | (k & 0xFFFF)) for k in range(17, 17 + size // 4))


def expected_packet(command, size, mask=None, base=None):
    """What the escape must receive: the header with words 2/14/15/16 set and the mask at byte 72."""
    words = list(base or MODERN[0]) + [0] * (size // 4)
    words[2], words[14], words[15], words[16] = 68 + size, command, size, 0
    data = bytearray(struct.pack(f"<{len(words)}I", *words))
    if mask is not None:
        data[72:72 + len(mask)] = mask
    return bytes(data)


def escape_call(command, size, mask=None, base=None, handle=111):
    return ("Escape", handle, FIELDS["hDevice"], FIELDS["Type"], FIELDS["Flags"], FIELDS["hContext"], 68 + size,
            expected_packet(command, size, mask, base))


ENUM_AND_QUERY = [("EnumAdapters2",)] + [("QueryAdapterInfo", h, 6, 12) for h in (111, 222, 333)]
CLOSES = [("CloseAdapter", h) for h in (111, 222, 333)]


def gdi_round(command, size, mask=None, base=None):
    return ENUM_AND_QUERY + [escape_call(command, size, mask, base)] + CLOSES


class PerfRig(TransportRig):
    """TransportRig whose escape logs the whole packet and then lets `respond(words)` answer."""

    def __init__(self, *, respond=echo, **kw):
        self.respond, self.errors = respond, []
        super().__init__(**kw)

    def _fake_gdi(self):
        table = super()._fake_gdi()

        @KMT
        def escape(pointer):
            try:
                e = NV.GPU._Escape.from_address(pointer)
                size = e.PrivateDriverDataSize
                self.calls.append(("Escape", e.hAdapter, e.hDevice, e.Type, e.Flags, e.hContext, size,
                                   ctypes.string_at(e.pPrivateDriverData, size)))
                self.respond((u32 * (size // 4)).from_address(e.pPrivateDriverData))
            except BaseException as exc:  # an exception would vanish inside the callback
                self.errors.append(repr(exc))
            return self.escape_status
        table[ESCAPE] = escape
        return table


def python_reader(gpu):
    reader = PerfDiagnosticsReader(gpu)
    return SimpleNamespace(transport=reader._transport, get=reader._get, fresh=lambda: python_reader(gpu))


def native_reader(gpu):
    def fresh():
        perf.reset(gpu)
        return native_reader(gpu)
    return SimpleNamespace(transport=lambda: perf.transport(gpu),
                           get=lambda *args, **kwargs: perf.get(gpu, *args, **kwargs), fresh=fresh)


def outcome(call):
    try:
        return ("ok", call())
    except PerfDiagnosticError as exc:
        return ("PerfDiagnosticError", exc.kind, str(exc), exc.status)
    except (ValueError, TypeError) as exc:
        return (type(exc).__name__, str(exc))


def normalized(transport):
    if transport is None:
        return None
    header_words, fields = transport
    return list(header_words), dict(fields)


def cache(gpu):
    return (normalized(getattr(gpu, "_current_limit_transport", None)),
            getattr(gpu, "_current_limit_layout_choice", None) is not None)


def seed_choice(gpu):
    gpu._current_limit_layout_choice = {"seeded": True} if isinstance(gpu, NV.GPU) else 0


class PerfCase(unittest.TestCase):
    def tearDown(self):
        native.set_export_resolver(None)

    def both(self, rig, action, *, assign=MODERN, choice=True):
        """action(reader) on the Python twin, then on the core, from one fake state:
        (outcome, gdi call log, cache afterwards), asserted equal."""
        opened = rig.native_gpu()
        runs = []
        try:
            twin = Card.python_twin(rig, opened)
            for gpu in (twin, opened):
                rig.reset()
                if assign is not None:
                    gpu._current_limit_transport = assign
                if choice:
                    seed_choice(gpu)
                if gpu is twin:
                    with rig.python_windll():
                        result = outcome(lambda: action(python_reader(gpu)))
                else:
                    result = outcome(lambda: action(native_reader(gpu)))
                runs.append((result, list(rig.calls), cache(gpu)))
        finally:
            opened.close()
        self.assertEqual(rig.errors, [])
        self.assertEqual(runs[0], runs[1], "Python and native runs differ")
        return runs[0]


def get(command, size, mask=None, transport=MODERN, **kwargs):
    if mask is not None:
        kwargs["mask"] = mask
    return lambda reader: reader.get(command, size, transport, **kwargs)


def info_cases():
    return [(name, layout.info_command, layout.info_size) for name, layout in LAYOUTS.items()]


def control_cases():
    return [(name, layout.control_command, layout.control_size, bytes(range(1, layout.mask_bytes + 1)))
            for name, layout in LAYOUTS.items()]


# ---- the pure tables ------------------------------------------------------------------------------

class TableTests(unittest.TestCase):
    def test_layouts_are_the_python_layouts(self):
        table = perf.layouts()
        self.assertEqual(list(table), list(LAYOUTS))
        self.assertEqual(table, {name: asdict(layout) for name, layout in LAYOUTS.items()})

    def test_probe_architectures_and_get_commands(self):
        self.assertEqual(perf.probe_architectures(), PROBE_ARCHITECTURES)
        self.assertEqual(perf.get_commands(), _GET_COMMANDS)
        self.assertEqual(len(_GET_COMMANDS), 6)


# ---- _transport -----------------------------------------------------------------------------------

UNVALIDATED = ("PerfDiagnosticError", "unsupported", "unvalidated current-policy transport geometry", None)
NOT_MODERN = ("PerfDiagnosticError", "unsupported", "PERF diagnostics need the validated modern RM transport", None)


class TransportTests(PerfCase):
    def test_the_modern_fixture_is_returned_and_kept(self):
        result, calls, stored = self.both(PerfRig(), lambda r: r.transport())
        self.assertEqual(result, ("ok", (tuple(MODERN[0]), MODERN[1])))
        self.assertEqual(calls, [])  # a stored transport is validated, never re-captured
        self.assertEqual(stored, ((MODERN[0], MODERN[1]), True))  # nor is the layout choice touched

    def test_a_short_header_or_inconsistent_size_word_is_unsupported_and_dropped(self):
        for label, value in (("16 words", (header()[:16], dict(FIELDS))),
                             ("word 2", (header(54424, 54352), dict(FIELDS)))):
            with self.subTest(label):
                result, calls, stored = self.both(PerfRig(), lambda r: r.transport(), assign=value)
                self.assertEqual(result, UNVALIDATED)
                self.assertEqual((calls, stored), ([], (None, False)))  # _rm_power_transport drops both

    def test_an_unknown_a612_geometry_keeps_the_transport(self):
        value = (header(1068, 1000), dict(FIELDS))
        result, _, stored = self.both(PerfRig(), lambda r: r.transport(), assign=value)
        self.assertEqual(result, UNVALIDATED)
        self.assertEqual(stored, ((value[0], value[1]), False))  # kept for other callers; only the choice goes

    def test_an_rm26_transport_is_explicitly_unsupported(self):
        value = (header(3548, 3480, command=P2612), dict(FIELDS))
        result, calls, stored = self.both(PerfRig(), lambda r: r.transport(), assign=value)
        self.assertEqual(result, NOT_MODERN)
        self.assertEqual((calls, stored), ([], ((value[0], value[1]), True)))  # a valid power transport is kept

    def test_nothing_captured_is_a_transient_failure_with_the_layouts_text(self):
        # No PowerPolInfo getter: neither side can capture, and the retryable ValueError becomes transient.
        nothing = ("PerfDiagnosticError", "transient",
                   "unsupported current-policy transport: no recognized power GET captured", None)
        for rig in (PerfRig(), PerfRig(gdi=False), PerfRig(missing={ESCAPE})):
            with self.subTest(gdi=rig.with_gdi, missing=sorted(rig.missing)):
                result, calls, stored = self.both(rig, lambda r: r.transport(), assign=None)
                self.assertEqual(result, nothing)
                self.assertEqual((calls, stored), ([], (None, False)))


class CaptureTests(HookCase):
    """A fresh capture through the hook (tests/test_native_rm_transport.py's HookRig)."""

    @staticmethod
    def transport(gpu):
        reader = PerfDiagnosticsReader(gpu)._transport if isinstance(gpu, NV.GPU) else (lambda: perf.transport(gpu))
        try:
            return ("ok", reader())
        except PerfDiagnosticError as exc:
            return ("PerfDiagnosticError", exc.kind, str(exc), exc.status)

    def test_a_captured_modern_transport_is_returned(self):
        rig = HookRig([ARCH_STEP, owner(power_packet())])
        (_, result), _, _ = self.both(rig, self.transport)
        self.assertEqual(result, ("ok", (tuple(rm_header(54420, 54352)), dict(HOOK_FIELDS))))

    def test_a_captured_rm26_transport_is_unsupported(self):
        rig = HookRig([ARCH_STEP, owner(power_packet(3548, 3480, command=P2612))])
        (_, result), _, _ = self.both(rig, self.transport)
        self.assertEqual(result, NOT_MODERN)

    def test_missing_gdi32_exports_keep_the_kind(self):
        # Documented divergence shared with dc_rm_power_layouts: the core names the missing export.
        rig = HookRig([ARCH_STEP, owner(power_packet())], missing={ENUM2})
        python, core = self.sides(rig, self.transport)
        text = "unsupported current-policy transport: no recognized power GET captured"
        self.assertEqual(python, ("PerfDiagnosticError", "transient", text, None))
        self.assertEqual(core, ("PerfDiagnosticError", "transient", text + " (gdi32 lacks D3DKMTEnumAdapters2)", None))


# ---- _get -----------------------------------------------------------------------------------------

class GetTests(PerfCase):
    def test_every_info_get_without_a_mask(self):
        for name, command, size in info_cases():
            with self.subTest(name):
                result, calls, stored = self.both(PerfRig(), get(command, size))
                self.assertEqual(result, ("ok", expected_payload(size)))
                self.assertEqual(len(result[1]), size)
                self.assertEqual(calls, gdi_round(command, size))
                self.assertEqual(stored, ((MODERN[0], MODERN[1]), True))

    def test_every_control_get_with_its_exact_mask(self):
        for name, command, size, mask in control_cases():
            with self.subTest(name):
                result, calls, _ = self.both(PerfRig(), get(command, size, mask))
                self.assertEqual(result, ("ok", expected_payload(size)))
                self.assertEqual(calls, gdi_round(command, size, mask))

    def test_the_header_words_come_from_the_given_transport(self):
        altered = (header(marks=(1, 2)), dict(FIELDS, hDevice=5, hContext=6))
        command, size = LAYOUTS["cf_policy"].info_command, LAYOUTS["cf_policy"].info_size
        result, calls, stored = self.both(PerfRig(), get(command, size, transport=altered))
        self.assertEqual(result, ("ok", expected_payload(size)))
        packet = expected_packet(command, size, base=altered[0])
        self.assertEqual(calls[4], ("Escape", 111, 5, 0, 8, 6, 68 + size, packet))
        self.assertEqual(stored, ((MODERN[0], MODERN[1]), True))  # the cache is not the argument

    def test_value_errors_before_the_driver_is_touched(self):
        cf = LAYOUTS["cf_policy"]
        mask = bytes(cf.mask_bytes)
        refused = "Only understood read-only PERF commands are allowed"
        control = "PERF CONTROL GET requires an exact INFO mask"
        info = "PERF INFO GET does not accept a request mask"
        cases = [
            (get(0x2080A612, cf.info_size), refused),
            (get(cf.info_command, cf.info_size + 4), refused),
            (get(cf.info_command, cf.control_size), refused),
            (get(-1, cf.info_size), refused),
            (get(cf.control_command, cf.control_size), control),
            (get(cf.control_command, cf.control_size, mask + b"\0"), control),
            (get(cf.control_command, cf.control_size, mask[:-1]), control),
            (get(cf.control_command, cf.control_size, bytearray(mask)), control),
            (get(cf.info_command, cf.info_size, mask), info),
            (get(cf.info_command, cf.info_size, b""), info),
            (get(cf.info_command, cf.info_size, bytearray()), info),
        ]
        for action, text in cases:
            with self.subTest(text):
                result, calls, stored = self.both(PerfRig(nvml_devid=0x1B02), action)  # pairing refused too
                self.assertEqual(result, ("ValueError", text))
                self.assertEqual((calls, stored), ([], ((MODERN[0], MODERN[1]), True)))

    def test_a_binding_refusal_comes_before_the_escape(self):
        cf = LAYOUTS["cf_policy"]
        result, calls, stored = self.both(PerfRig(nvml_devid=0x1B02), get(cf.info_command, cf.info_size))
        self.assertEqual(result[:2], ("PerfDiagnosticError", "identity"))
        self.assertIn("NVAPI and NVML disagree", result[2])
        self.assertIsNone(result[3])
        self.assertEqual((calls, stored), ([], ((MODERN[0], MODERN[1]), True)))

    def test_a_failed_escape_drops_the_transport_and_choice(self):
        cf = LAYOUTS["cf_policy"]
        rig = PerfRig(escape_status=NTSTATUS_INVALID_PARAMETER)
        result, calls, stored = self.both(rig, get(cf.info_command, cf.info_size))
        self.assertEqual(result, ("PerfDiagnosticError", "transient",
                                  f"PERF GET transport failed (NTSTATUS {NTSTATUS_INVALID_PARAMETER})",
                                  NTSTATUS_INVALID_PARAMETER))
        self.assertEqual(calls, gdi_round(cf.info_command, cf.info_size))
        self.assertEqual(stored, (None, False))

    def test_an_escape_that_cannot_be_issued(self):
        # nvbackend._legacy_clk_escape returns None: Python's `None != 0` takes the transport-failure path.
        cf = LAYOUTS["cf_policy"]
        none = ("PerfDiagnosticError", "transient", "PERF GET transport failed (NTSTATUS None)", None)
        for rig, log in ((PerfRig(missing={ESCAPE}), []), (PerfRig(gdi=False), []),
                         (PerfRig(adapters=[(7, 2, 0, 0)]), [("EnumAdapters2",), ("QueryAdapterInfo", 7, 6, 12),
                                                             ("CloseAdapter", 7)])):
            with self.subTest(adapters=rig.adapters, gdi=rig.with_gdi, missing=sorted(rig.missing)):
                result, calls, stored = self.both(rig, get(cf.info_command, cf.info_size))
                self.assertEqual(result, none)
                self.assertEqual((calls, stored), (log, (None, False)))

    def test_rm_rejections(self):
        cf = LAYOUTS["cf_controller"]
        for rm, kind in ((0x1F, "unsupported"), (0x56, "unsupported"), (0x77, "transient"), (0x1, "transient")):
            def respond(words, rm=rm):
                echo(words)
                words[16] = rm
            with self.subTest(rm=hex(rm)):
                result, calls, stored = self.both(PerfRig(respond=respond), get(cf.info_command, cf.info_size))
                self.assertEqual(result, ("PerfDiagnosticError", kind,
                                          f"PERF GET 0x{cf.info_command:08X} rejected (RM 0x{rm:X})", rm))
                self.assertEqual(calls, gdi_round(cf.info_command, cf.info_size))
                self.assertEqual(stored, ((MODERN[0], MODERN[1]), True))  # a rejection keeps the transport

    def test_echo_changes_are_malformed(self):
        cf = LAYOUTS["limits"]
        mask = bytes(range(64))
        for index, delta in ((2, 4), (14, 1), (15, 4), (2, -4)):
            def respond(words, index=index, delta=delta):
                echo(words)
                words[index] = (words[index] + delta) & 0xFFFFFFFF
            with self.subTest(word=index, delta=delta):
                result, calls, _ = self.both(PerfRig(respond=respond), get(cf.control_command, cf.control_size, mask))
                self.assertEqual(result, ("PerfDiagnosticError", "malformed",
                                          "PERF response changed its command or parameter geometry", None))
                self.assertEqual(calls, gdi_round(cf.control_command, cf.control_size, mask))

    def test_rm_status_is_judged_before_the_echo(self):
        cf = LAYOUTS["cf_policy"]

        def respond(words):
            words[16], words[14] = 0x56, 0
        result, _, _ = self.both(PerfRig(respond=respond), get(cf.info_command, cf.info_size))
        self.assertEqual(result[:2], ("PerfDiagnosticError", "unsupported"))

    def test_none_transport_is_refused_after_the_binding(self):
        cf = LAYOUTS["cf_policy"]
        result, calls, _ = self.both(PerfRig(), get(cf.info_command, cf.info_size, transport=None))
        self.assertEqual(result, ("TypeError", "cannot unpack non-iterable NoneType object"))
        self.assertEqual(calls, [])

    def test_a_fresh_reader_binds_again(self):
        cf = LAYOUTS["cf_policy"]

        def action(reader):
            first = reader.get(cf.info_command, cf.info_size, MODERN)
            again = reader.fresh().get(cf.info_command, cf.info_size, MODERN)
            return first == again == expected_payload(cf.info_size), reader.transport()
        result, calls, _ = self.both(PerfRig(), action)
        self.assertEqual(result, ("ok", (True, (tuple(MODERN[0]), MODERN[1]))))
        self.assertEqual(calls, gdi_round(cf.info_command, cf.info_size) * 2)


if __name__ == "__main__":
    unittest.main()
