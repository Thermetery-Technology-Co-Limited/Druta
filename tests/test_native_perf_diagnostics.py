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

A second documented divergence (the binding's docstring): escape-field values ctypes refuses are
raised after the binding but before any driver call, where Python raises them inside the escape.

Unreachable natively, and so covered only by tests/test_perf_diagnostics.py: a cached transport
that is not a (header, fields) pair, header words that are not 32-bit ints and an escape-field set
other than the five fields (the core's types hold these by construction). The transport ARGUMENT
of _get is marshalled with Python's own semantics (TransportArgumentTests)."""
import ctypes
import struct
from types import SimpleNamespace
import unittest
from dataclasses import asdict

from druta import native, nvbackend
from druta.perf_diagnostics import LAYOUTS, PROBE_ARCHITECTURES, _GET_COMMANDS, PerfDiagnosticError, PerfDiagnosticsReader
from tests.test_native_rm_transport import ARCH_STEP, FIELDS as HOOK_FIELDS, HookCase, HookRig, header as rm_header
from tests.test_native_rm_transport import owner, power_packet
from tests.test_native_identity import CDECL, write_u32
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
    """TransportRig whose escape logs the whole packet and then lets `respond(words)` answer.
    NVML's architecture answers `self.arch` (restored to `arch` by reset()) and counts its reads."""

    def __init__(self, *, respond=echo, arch=7, **kw):
        self.respond, self.errors = respond, []
        self.initial_arch, self.arch, self.arch_reads = arch, arch, 0
        super().__init__(**kw)

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def architecture(device, pointer):
            self.arch_reads += 1
            write_u32(pointer, self.arch)
            return 0
        self._architecture = architecture
        self.nvml.exports["nvmlDeviceGetArchitecture"] = architecture

    def reset(self):
        super().reset()
        self.arch, self.arch_reads = self.initial_arch, 0

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
    except (ValueError, TypeError, IndexError) as exc:
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

    def both(self, rig, action, *, assign=MODERN, choice=True, same_calls=True):
        """action(reader) on the Python twin, then on the core, from one fake state:
        (outcome, gdi call log, cache afterwards), asserted equal. same_calls=False (a documented
        divergence) compares the outcome and cache only and returns both runs."""
        opened = rig.native_gpu()
        runs, reads = [], []
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
                reads.append(getattr(rig, "arch_reads", None))
        finally:
            opened.close()
        self.arch_reads = reads  # NVML architecture reads during each side's action
        self.assertEqual(rig.errors, [])
        if not same_calls:
            self.assertEqual((runs[0][0], runs[0][2]), (runs[1][0], runs[1][2]), "Python and native runs differ")
            return runs
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
        # PerfRig has no PowerPolInfo getter, so gdi32 is never reached here; CaptureTests covers an
        # absent gdi32 and a missing escape export on the capture path itself.
        result, calls, stored = self.both(PerfRig(), lambda r: r.transport(), assign=None)
        self.assertEqual(result, nothing)
        self.assertEqual((calls, stored), ([], (None, False)))

    def test_transport_does_not_bind(self):
        # Python's _transport never calls _binding: a refused pairing still returns the transport.
        result, calls, stored = self.both(PerfRig(nvml_devid=0x1B02), lambda r: r.transport())
        self.assertEqual(result, ("ok", (tuple(MODERN[0]), MODERN[1])))
        self.assertEqual((calls, stored), ([], ((MODERN[0], MODERN[1]), True)))
        self.assertEqual(self.arch_reads, [0, 0])


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

    def test_an_absent_gdi32_or_escape_export_is_attempted_and_transient(self):
        # PowerPolInfo exists, so both sides reach the capture and find gdi32 unusable before hooking.
        # The kind and Python's text are shared; the core appends its escape-capability detail (the
        # divergence documented above, from the shared capture path in rm_transport).
        text = "unsupported current-policy transport: no recognized power GET captured"
        for rig, suffix in ((HookRig([ARCH_STEP, owner(power_packet())], gdi=False), " (gdi32.dll not loadable: not provided by the test resolver)"),
                            (HookRig([ARCH_STEP, owner(power_packet())], missing={ESCAPE}),
                             " (XPDM: no D3DKMT escape path; driver escapes are not possible on this image)")):
            with self.subTest(gdi=rig.with_gdi, missing=sorted(rig.missing)):
                python, core = self.sides(rig, self.transport)
                self.assertEqual(python, ("PerfDiagnosticError", "transient", text, None))
                self.assertEqual(core, ("PerfDiagnosticError", "transient", text + suffix, None))
                self.assertEqual(rig.getter_calls, 0)  # gdi32 is checked before the hook and the getter


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


    def test_transport_failure_precedes_the_rm_status_and_echo(self):
        cf = LAYOUTS["cf_policy"]
        for label, change in (("RM 0x56", lambda w: w.__setitem__(16, 0x56)),
                              ("word 14", lambda w: w.__setitem__(14, 0))):
            def respond(words, change=change):
                echo(words)
                change(words)
            with self.subTest(label):
                rig = PerfRig(respond=respond, escape_status=NTSTATUS_INVALID_PARAMETER)
                result, calls, stored = self.both(rig, get(cf.info_command, cf.info_size))
                self.assertEqual(result, ("PerfDiagnosticError", "transient",
                                          f"PERF GET transport failed (NTSTATUS {NTSTATUS_INVALID_PARAMETER})",
                                          NTSTATUS_INVALID_PARAMETER))
                self.assertEqual(calls, gdi_round(cf.info_command, cf.info_size))
                self.assertEqual(stored, (None, False))

    def test_refusals_of_every_group_and_cross_group_pairs(self):
        limits, ctl = LAYOUTS["limits"], LAYOUTS["cf_controller"]
        refused = "Only understood read-only PERF commands are allowed"
        control = "PERF CONTROL GET requires an exact INFO mask"
        info = "PERF INFO GET does not accept a request mask"
        mixes = [(limits.info_command, ctl.info_size), (limits.control_command, limits.info_size),
                 (ctl.info_command, limits.info_size), (ctl.control_command, ctl.info_size),
                 (LAYOUTS["cf_policy"].info_command, ctl.control_size)]
        cases = [(get(limits.control_command, limits.control_size, bytes(4)), control),
                 (get(ctl.control_command, ctl.control_size, bytes(64)), control),
                 (get(limits.info_command, limits.info_size, bytes(64)), info),
                 (get(ctl.info_command, ctl.info_size, bytes(4)), info)]
        cases += [(get(command, size), refused) for command, size in mixes]
        for action, text in cases:
            with self.subTest(text):
                result, calls, stored = self.both(PerfRig(), action)
                self.assertEqual(result, ("ValueError", text))
                self.assertEqual((calls, stored), ([], ((MODERN[0], MODERN[1]), True)))
                self.assertEqual(self.arch_reads, [0, 0])  # refused before the binding
        for command, size in mixes:
            self.assertEqual(perf.load().dc_perf_get_allowed(command, size), 0)
            self.assertNotIn((command, size), _GET_COMMANDS)

    def test_the_escape_always_receives_word_16_as_zero(self):
        cf = LAYOUTS["cf_policy"]
        dirty = header()
        dirty[16] = 0x1F
        result, calls, _ = self.both(PerfRig(), get(cf.info_command, cf.info_size, transport=(dirty, dict(FIELDS))))
        self.assertEqual(result, ("ok", expected_payload(cf.info_size)))
        sent = calls[4][-1]
        self.assertEqual(struct.unpack_from("<I", sent, 64)[0], 0)
        self.assertEqual(sent, expected_packet(cf.info_command, cf.info_size, base=dirty))


# ---- _get: the transport argument as Python's packet build sees it -----------------------------------

CF = LAYOUTS["cf_policy"]
ARCH3 = ("PerfDiagnosticError", "unsupported", "Modern PERF groups require a Pascal-or-later architecture", None)


def packet_of(words_given, command, size, mask=None):
    """_get's packet for any header: words stored modulo 2^32 from word 0, then 2/14/15/16 set."""
    words = [0] * (17 + size // 4)
    for index, word in enumerate(words_given):
        words[index] = word & 0xFFFFFFFF
    words[2], words[14], words[15], words[16] = 68 + size, command, size, 0
    data = bytearray(struct.pack(f"<{len(words)}I", *words))
    if mask is not None:
        data[72:72 + len(mask)] = mask
    return bytes(data)


def round_with(packet):
    return (ENUM_AND_QUERY + [("Escape", 111, FIELDS["hDevice"], FIELDS["Type"], FIELDS["Flags"], FIELDS["hContext"],
                               len(packet), packet)] + CLOSES)


class Command(int):
    """An int subclass: equal and hashed as the int, so it is in _GET_COMMANDS."""


class TransportArgumentTests(PerfCase):
    """Python's _get checks the rules, binds, and only then unpacks the transport and builds the
    packet through ctypes. The sticky identity cannot be shown changing on the core: every field of
    it (the NvAPI object and handle, the PCI address, devid, subsys) is read once when the handle
    opens, and a recognised architecture is cached. That the failing GET bound is shown instead by
    the architecture read it caches (a later change of the fake's architecture is not seen)."""

    def test_the_rules_come_before_the_transport(self):
        refused = "Only understood read-only PERF commands are allowed"
        cases = [(get(0x12345678, 4, transport=5), refused),
                 (get(0, 0, transport=([2 ** 32] * 17, dict(FIELDS))), refused),
                 (get(0x12345678, 4, transport=None), refused),
                 (get(CF.control_command, CF.control_size, transport=None), "PERF CONTROL GET requires an exact INFO mask"),
                 (get(CF.info_command, CF.info_size, bytes(4), transport=None), "PERF INFO GET does not accept a request mask")]
        for rig in (PerfRig(), PerfRig(nvml_devid=0x1B02)):
            for action, text in cases:
                with self.subTest(text, refused_pairing=rig.nvml.devices[0]["devid"]):
                    result, calls, stored = self.both(rig, action)
                    self.assertEqual(result, ("ValueError", text))
                    self.assertEqual((calls, stored, self.arch_reads), ([], ((MODERN[0], MODERN[1]), True), [0, 0]))

    def bad_transports(self):
        long_header = header() + [7] * (CF.info_size // 4 + 1)  # one word past the packet
        return [("3-tuple", (header(), dict(FIELDS), None)), ("int", 5), ("None", None),
                ("float word", (header()[:12] + [1.0] + header()[13:], dict(FIELDS))),
                ("str word", (header()[:12] + ["1"] + header()[13:], dict(FIELDS))),
                ("too long", (long_header, dict(FIELDS))), ("fields", (header(), 5))]

    def test_a_binding_refusal_comes_before_the_transport(self):
        for rig, expect in ((PerfRig(arch=3), lambda r: self.assertEqual(r, ARCH3)),
                            (PerfRig(nvml_devid=0x1B02),
                             lambda r: self.assertEqual(r[:2], ("PerfDiagnosticError", "identity")))):
            for label, value in self.bad_transports():
                with self.subTest(label, arch=rig.initial_arch):
                    result, calls, stored = self.both(rig, get(CF.info_command, CF.info_size, transport=value))
                    expect(result)
                    self.assertEqual((calls, stored), ([], ((MODERN[0], MODERN[1]), True)))
        # A float command equal to an allowed one passes the rules and is refused by the binding first.
        result, _, _ = self.both(PerfRig(arch=3), get(float(CF.info_command), CF.info_size))
        self.assertEqual(result, ARCH3)

    def test_marshalling_errors_after_a_valid_binding(self):
        expected = {"int": ("TypeError", "cannot unpack non-iterable int object"),
                    "None": ("TypeError", "cannot unpack non-iterable NoneType object"),
                    "float word": ("TypeError", "'float' object cannot be interpreted as an integer"),
                    "str word": ("TypeError", "'str' object cannot be interpreted as an integer"),
                    "too long": ("IndexError", "invalid index")}
        for label, value in self.bad_transports()[:-1]:
            with self.subTest(label):
                result, calls, stored = self.both(PerfRig(), get(CF.info_command, CF.info_size, transport=value))
                if label == "3-tuple":  # the interpreter's own text, asserted equal on both sides
                    self.assertEqual(result[0], "ValueError")
                    self.assertIn("too many values to unpack", result[1])
                else:
                    self.assertEqual(result, expected[label])
                self.assertEqual((calls, stored), ([], ((MODERN[0], MODERN[1]), True)))
                self.assertEqual(self.arch_reads, [1, 1])  # bound first

    def test_unusable_escape_fields_divergence(self):
        # Documented divergence: Python builds the escape from the fields inside _legacy_clk_escape,
        # after enumerating the adapters (and closing them); the core marshals them with the packet,
        # after the binding and before any driver call. The exception and the cache are the same.
        for label, fields, text in (("not a mapping", 5, "'int' object is not iterable"),
                                    ("float value", dict(FIELDS, hDevice=1.0),
                                     "'float' object cannot be interpreted as an integer")):
            with self.subTest(label):
                runs = self.both(PerfRig(), get(CF.info_command, CF.info_size, transport=(header(), fields)),
                                 same_calls=False)
                self.assertEqual([run[0] for run in runs], [("TypeError", text)] * 2)
                self.assertEqual([run[1] for run in runs], [ENUM_AND_QUERY + CLOSES, []])
                self.assertEqual(runs[0][2], ((MODERN[0], MODERN[1]), True))
                self.assertEqual(self.arch_reads, [1, 1])

    def test_a_failing_get_still_binds(self):
        rig = PerfRig()

        def action(reader):
            first = outcome(lambda: reader.get(CF.info_command, CF.info_size, 5))
            rig.arch = 3  # cached by the binding the failing GET ran: not read again
            return first, reader.get(CF.info_command, CF.info_size, MODERN)
        result, calls, _ = self.both(rig, action)
        self.assertEqual(result, ("ok", (("TypeError", "cannot unpack non-iterable int object"),
                                         expected_payload(CF.info_size))))
        self.assertEqual(calls, gdi_round(CF.info_command, CF.info_size))
        self.assertEqual(self.arch_reads, [1, 1])  # the second GET's two bindings made no driver call

        def unbound(reader):  # the counterpart: nothing bound before the change, so it is seen
            rig.arch = 3
            return reader.get(CF.info_command, CF.info_size, MODERN)
        rig = PerfRig()
        result, calls, _ = self.both(rig, unbound)
        self.assertEqual((result, calls), (ARCH3, []))

    def test_float_command_or_size_is_refused_by_ctypes_after_the_binding(self):
        for label, action, text in (
                ("command", get(float(CF.info_command), CF.info_size),
                 "'float' object cannot be interpreted as an integer"),
                ("size", get(CF.info_command, float(CF.info_size)), "can't multiply sequence by non-int of type 'float'")):
            with self.subTest(label):
                result, calls, stored = self.both(PerfRig(), action)
                self.assertEqual(result, ("TypeError", text))
                self.assertEqual((calls, stored, self.arch_reads), ([], ((MODERN[0], MODERN[1]), True), [1, 1]))

    def test_int_subclass_command_and_size_perform_the_get(self):
        result, calls, _ = self.both(PerfRig(), get(Command(CF.info_command), Command(CF.info_size)))
        self.assertEqual(result, ("ok", expected_payload(CF.info_size)))
        self.assertEqual(calls, gdi_round(CF.info_command, CF.info_size))

    def test_header_words_wrap_modulo_2_32(self):
        words = header()
        words[12], words[13], words[0] = -1, 2 ** 32 + 5, 2 ** 64 + 9
        result, calls, _ = self.both(PerfRig(), get(CF.info_command, CF.info_size, transport=(words, dict(FIELDS))))
        self.assertEqual(result, ("ok", expected_payload(CF.info_size)))
        sent = round_with(packet_of(words, CF.info_command, CF.info_size))
        self.assertEqual(calls, sent)
        self.assertEqual(struct.unpack_from("<I", sent[4][-1], 48), (0xFFFFFFFF,))
        self.assertEqual(struct.unpack_from("<II", sent[4][-1], 52)[0], 5)

    def test_header_length(self):
        mask = bytes(range(1, CF.mask_bytes + 1))
        words_in_control = 17 + CF.control_size // 4
        cases = [("13 words", header()[:13], CF.info_command, CF.info_size, None),
                 ("0 words", [], CF.info_command, CF.info_size, None),
                 ("18 words", header() + [0xABCD1234], CF.info_command, CF.info_size, None),
                 ("the whole CONTROL packet", header() + [0x5A000000 | k for k in range(17, words_in_control)],
                  CF.control_command, CF.control_size, mask)]
        for label, words, command, size, request_mask in cases:
            with self.subTest(label):
                action = get(command, size, request_mask, transport=(words, dict(FIELDS)))
                result, calls, _ = self.both(PerfRig(), action)
                self.assertEqual(result, ("ok", expected_payload(size)))
                self.assertEqual(calls, round_with(packet_of(words, command, size, request_mask)))
        # The 13-word case: words 13 and 16 are 0, 2/14/15 set, 0..12 from the header.
        sent = packet_of(header()[:13], CF.info_command, CF.info_size)
        self.assertEqual(struct.unpack_from("<17I", sent)[13:], (0, CF.info_command, CF.info_size, 0))
        self.assertEqual(struct.unpack_from("<17I", sent)[:2], tuple(header()[:2]))
        # 18 words: word 17 is the first payload word the escape receives.
        self.assertEqual(struct.unpack_from("<I", packet_of(header() + [0xABCD1234], CF.info_command, CF.info_size),
                                            68)[0], 0xABCD1234)
        result, calls, _ = self.both(PerfRig(), get(CF.control_command, CF.control_size, mask,
                                                    transport=(header() + [0] * (words_in_control - 16), FIELDS)))
        self.assertEqual((result, calls), (("IndexError", "invalid index"), []))

    def test_fields_as_pairs_and_header_as_a_tuple(self):
        value = (tuple(header()), list(FIELDS.items()))
        result, calls, _ = self.both(PerfRig(), get(CF.info_command, CF.info_size, transport=value))
        self.assertEqual(result, ("ok", expected_payload(CF.info_size)))
        self.assertEqual(calls, gdi_round(CF.info_command, CF.info_size))

    def test_the_abi_refuses_an_unusable_transport_after_the_binding(self):
        # dc_perf_get directly: more header words than the packet, or no fields (None), is
        # DC_E_ARGUMENT once the rules and the binding pass, with nothing sent; the binding wins.
        library = perf.load()
        words = 17 + CF.info_size // 4
        for rig, expected in ((PerfRig(), -1), (PerfRig(arch=3), native.DC_E_FAILED)):
            gpu = rig.native_gpu()
            try:
                for count, with_fields in ((words + 1, True), (0, False), (words, True)):
                    with self.subTest(count=count, fields=with_fields, arch=rig.initial_arch):
                        rig.reset()
                        header_words = (ctypes.c_uint32 * (words + 1))(*header())
                        fields = native._EscapeFields(**FIELDS)
                        out, error = ctypes.create_string_buffer(CF.info_size), perf._new_error()
                        status = library.dc_perf_get(gpu._handle, CF.info_command, CF.info_size, header_words,
                                                     count, ctypes.byref(fields) if with_fields else None, None, 0,
                                                     perf.DC_PERF_MASK_NONE, out, CF.info_size, ctypes.byref(error))
                        if count == words and expected == -1:
                            self.assertEqual(status, native.DC_OK)  # the packet's own length is accepted
                            self.assertEqual(len(rig.calls), len(gdi_round(CF.info_command, CF.info_size)))
                            continue
                        self.assertEqual(status, expected)
                        self.assertEqual(rig.calls, [])
                        if expected == native.DC_E_FAILED:
                            self.assertEqual(perf._text(error.message), ARCH3[2])
            finally:
                gpu.close()

    def test_a_closed_handle_reports_the_handle_not_the_transport(self):
        rig = PerfRig()
        gpu = rig.native_gpu()
        gpu.close()
        for value in (None, 5, MODERN):
            with self.subTest(transport=value):
                with self.assertRaisesRegex(ValueError, "^invalid argument$"):
                    perf.get(gpu, CF.info_command, CF.info_size, value)
        self.assertEqual(rig.calls, [])


if __name__ == "__main__":
    unittest.main()
