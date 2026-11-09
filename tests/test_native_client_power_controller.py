# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native client-power controller, increment A3 (the paired transport check and the guarded
single-command exchange) against client_power_runtime.ClientPowerController._transport,
_exchange and _reset_transport.

native.Gpu._cp_transport / _cp_exchange / _cp_reset_transport / _cp_reset / _cp_state run
dc_cpc_*. The Python original runs on an nvbackend.GPU twin built like the Card fixture of
tests/test_native_target_binding.py (GPU.__new__, so slot(), arch(), _rm_power_transport and
_legacy_clk_escape are the real methods) from the identity records the native handle exposes.

One fake driver serves both sides: the Card's NVML / NVAPI callbacks and a fake gdi32 (Python
through a patched ctypes.WinDLL, the core through the resolver seam, as tests/test_native_transport.py
does). The fake escape logs the escape fields and the packet bytes it received, then answers with a
scripted response. A fixture transport is stored on both sides through the _current_limit_transport
setter, as tests/test_native_rm_transport.py does. Every case compares the outcome (the result, or
the ClientPowerError kind, message and rm_status), the gdi call log, write.dispatched, the
controller state and the transport cache.

AbiTests call dc_cpc_* through ctypes directly: argument and capacity handling, *dispatched on
every return, the core's own geometry refusals and error kinds, and the caller stops.

Not reachable natively, and so covered only by tests/test_client_power_runtime.py:
  - the identity changing between two _binding() calls inside one _transport or _exchange, and so
    the guards-before-binding and binding-before-status order of _exchange (the fake identity
    cannot change while one core call runs: a recognised architecture is cached on both sides);
  - the sticky "GPU binding changed" refusal and its clearing by a fresh controller (as in
    tests/test_native_target_binding.py, the public fakes cannot change a bound identity);
  - "Client-power transport failed: {exc}" (an exception raised by the escape itself; the core's
    escape reports every failure as "not issued", which Python's nvbackend returns as None);
  - "Paired power transport could not be captured: {exc}" (a non-ValueError from the getter);
  - a header that is not a tuple/list of 32-bit ints or fields that are not exactly the five
    escape fields (the core's transport is a u32 array and a five-field struct by construction).
"""
import ctypes
import enum
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from druta import client_power_runtime as cp
from druta import native, nvbackend
from tests.test_native_target_binding import Card
from tests.test_native_transport import CLOSE, ENUM2, ESCAPE, KMT, QUERY, Enum2, Query, install

if not native.available():
    raise unittest.SkipTest("native core not built (native/build.sh linux)")

NV = nvbackend
u32 = ctypes.c_uint32
A612, P2612 = 0x2080A612, 0x20802612
FIELDS = dict(hAdapter=987, hDevice=0x11, Type=0x22, Flags=8, hContext=0x44)
NTSTATUS_INVALID_PARAMETER = -1073741811
GROUPS = ("SIZES", "LEGACY_SIZES", "TURING_SIZES", "INFO216_SIZES", "RM26_SIZES", "RM26_EXTENDED_SIZES",
          "A612_COMPACT_SIZES")
MALFORMED_GEOMETRY = ("ClientPowerError", "malformed", "Client-power command or packet geometry is not understood",
                      None)


def header(packet_bytes=54420, parameter_bytes=54352, command=A612, **words):
    h = list(range(100, 117))
    for index, value in cp.clock._ENVELOPE.items():
        h[index] = value
    h[2], h[14], h[15], h[16] = packet_bytes, command, parameter_bytes, 0
    h[5] = h[11] = 0
    for name, value in words.items():
        h[int(name[1:])] = value
    return h


RM26 = header(2524, 2456, P2612)
RM26_EXTENDED = header(3548, 3480, P2612)


def payload_of(size, seed=0x30):
    return bytes((seed + k) & 0xFF for k in range(size))


# ---- the fake driver ----------------------------------------------------------------------------

def respond(*, completion=1, rm=0, status=0, words=None, response=None, overrun=False, underrun=False,
            echo_change=False):
    """The fake escape's answer: the words and bytes it writes, and the NTSTATUS it returns."""
    def answer(address, size):
        view = (u32 * (size // 4)).from_address(address)
        if response is not None:
            ctypes.memmove(address + 68, response, len(response))
        if echo_change is not False:
            data = (ctypes.c_ubyte * size).from_address(address)
            data[68 if echo_change is True else echo_change] ^= 0xFF
        view[11], view[16] = completion, rm
        for index, value in (words or {}).items():
            view[index] = value
        if overrun:
            ctypes.c_ubyte.from_address(address + size).value = 0x00  # one byte into the trailing guard
        if underrun:
            ctypes.c_ubyte.from_address(address - 1).value = 0x00  # one byte into the leading guard
        return status
    return answer


class Rig:
    """A Card plus a fake gdi32 with the adapter at the card's slot (bus 1, device 0)."""

    def __init__(self, *, arch=7, adapters=((111, 1, 0, 0), (222, 2, 0, 0)), missing=(), answer=None, **card):
        self.card = Card(arch=arch, **card)
        self.adapters = list(adapters)
        self.missing = set(missing)
        self.answer = answer or respond()
        self.calls = []
        self._callbacks = self._fake_gdi()

    def _fake_gdi(self):
        @KMT
        def enum2(pointer):
            self.calls.append(("EnumAdapters2",))
            request = Enum2.from_address(pointer)
            capacity = request.count
            request.count = len(self.adapters)
            for k, (handle, *_) in enumerate(self.adapters[:capacity]):
                request.items[k].handle = handle
            return 0

        @KMT
        def query(pointer):
            request = Query.from_address(pointer)
            self.calls.append(("QueryAdapterInfo", request.handle))
            hit = [a for a in self.adapters if a[0] == request.handle]
            if not hit or request.kind != 6 or request.size != 12:
                return -1
            (u32 * 3).from_address(request.data)[:] = hit[0][1:]
            return 0

        @KMT
        def close(pointer):
            self.calls.append(("CloseAdapter", u32.from_address(pointer).value))
            return 0

        @KMT
        def escape(pointer):
            e = NV.GPU._Escape.from_address(pointer)
            size = e.PrivateDriverDataSize
            self.calls.append(("Escape", e.hAdapter, e.hDevice, e.Type, e.Flags, e.hContext, size,
                               ctypes.string_at(e.pPrivateDriverData, size)))
            return self.answer(e.pPrivateDriverData, size)

        return {ENUM2: enum2, QUERY: query, CLOSE: close, ESCAPE: escape}

    def gdi(self):
        return {name: fn for name, fn in self._callbacks.items() if name not in self.missing}

    def open(self):
        install(self.card.nvml, self.card.nvapi, self.gdi())
        return native.Gpu()


# ---- the two sides ------------------------------------------------------------------------------

def outcome(call):
    try:
        return ("ok", call())
    except cp.ClientPowerError as exc:
        return ("ClientPowerError", exc.kind, str(exc), exc.rm_status)
    except (ValueError, TypeError, AttributeError) as exc:
        return (type(exc).__name__, str(exc))


def group_name(sizes):
    names = [name for name in GROUPS if getattr(cp, name) is sizes]
    assert len(names) == 1, "sizes is not one of the module's tables"
    return names[0]


class PythonSide:
    def __init__(self, twin):
        self.gpu = twin
        self.controller = cp.ClientPowerController(twin)

    def store(self, words, choice=True):
        self.gpu._current_limit_transport = (list(words), dict(FIELDS))
        self.gpu._current_limit_layout_choice = {"seeded": True} if choice else None

    def transport(self):
        header_words, fields = self.controller._transport()
        return tuple(header_words), dict(fields)

    def exchange(self, command, payload, write=None):
        return self.controller._exchange(command, payload, write=write)

    def reset_transport(self):
        self.controller._reset_transport()

    def reset(self):
        self.controller = cp.ClientPowerController(self.gpu)

    def state(self):
        c = self.controller
        return {"sizes": group_name(c.sizes), "legacy": c._legacy, "native_policy124": c._native_policy124,
                "role_info216": c._role_info216, "rm26": c._rm26, "rm26_records": c._rm26_records,
                "rm26_extended": c._rm26_extended,
                "transport_key": None if c._transport_key is None else tuple(c._transport_key)}

    def cache(self):
        transport = getattr(self.gpu, "_current_limit_transport", None)
        if transport is not None:
            transport = (list(transport[0]), dict(transport[1]))
        return transport, getattr(self.gpu, "_current_limit_layout_choice", None) is not None


class NativeSide:
    def __init__(self, gpu):
        self.gpu = gpu

    def store(self, words, choice=True):
        self.gpu._current_limit_transport = (list(words), dict(FIELDS))
        self.gpu._current_limit_layout_choice = 0 if choice else None

    def transport(self):
        return self.gpu._cp_transport()

    def exchange(self, command, payload, write=None):
        return self.gpu._cp_exchange(command, payload, write=write)

    def reset_transport(self):
        self.gpu._cp_reset_transport()

    def reset(self):
        self.gpu._cp_reset()

    def state(self):
        s = self.gpu._cp_state()
        return dict(s, sizes=group_name(s["sizes"]))

    def cache(self):
        return self.gpu._current_limit_transport, self.gpu._current_limit_layout_choice is not None


class ParityCase(unittest.TestCase):
    def tearDown(self):
        native.set_export_resolver(None)

    def both(self, rig, action):
        """action(side) on the Python original, then on the core, from the same fake state. Returns
        what action returned, the gdi call log, and the final controller state and cache."""
        runs = []
        for kind in ("python", "native"):
            rig.calls = []
            opened = rig.open()
            try:
                if kind == "python":
                    side = PythonSide(rig.card.python_twin(opened))
                    table = rig.gdi()
                    with patch.object(NV.ctypes, "WinDLL", create=True, return_value=SimpleNamespace(**table)):
                        result = action(side)
                else:
                    side = NativeSide(opened)
                    result = action(side)
                runs.append((result, list(rig.calls), side.state(), side.cache()))
            finally:
                opened.close()
        self.assertEqual(runs[0], runs[1], "Python and native runs differ")
        return runs[0]


def exchange_once(command, payload, *, transport=None, write=False, choice=True, first=None):
    """Store transport, optionally run first(side), then one exchange; returns (outcome, dispatched)."""
    def action(side):
        side.store(header() if transport is None else transport, choice)
        if first is not None:
            first(side)
        w = cp._Write() if write else None
        return outcome(lambda: side.exchange(command, payload, w)), (w.dispatched if w else None)
    return action


def packet(command, size, payload, h=None, wire=None):
    """The bytes the escape receives: the header with the exchange's words, then the payload."""
    words = list(header() if h is None else h)
    words[2], words[14], words[15], words[16] = 68 + size, command if wire is None else wire, size, 0
    words[5] = words[11] = 0
    return bytes((u32 * 17)(*words)) + payload


def escape_log(command, payload, h=None, wire=None, handle=111):
    size = len(payload)
    return ([("EnumAdapters2",), ("QueryAdapterInfo", 111), ("QueryAdapterInfo", 222),
             ("Escape", handle, 0x11, 0x22, 8, 0x44, 68 + size, packet(command, size, payload, h, wire))]
            + [("CloseAdapter", 111), ("CloseAdapter", 222)])


FRESH = {"sizes": "SIZES", "legacy": False, "native_policy124": False, "role_info216": False, "rm26": False,
         "rm26_records": False, "rm26_extended": False, "transport_key": None}
A612_KEY = (A612, 54420, 54352)


def keyed(key=A612_KEY, **changes):
    return dict(FRESH, transport_key=key, **changes)


# ---- the exchange -------------------------------------------------------------------------------

class ExchangeTests(ParityCase):
    def test_get_returns_the_response_payload(self):
        size = cp.SIZES[cp.INFO]
        response = payload_of(size, 0x77)
        rig = Rig(answer=respond(response=response))
        (result, dispatched), calls, state, cache = self.both(rig, exchange_once(cp.INFO, bytes(size)))
        self.assertEqual(result, ("ok", response))
        self.assertIsNone(dispatched)
        self.assertEqual(calls, escape_log(cp.INFO, bytes(size)))
        self.assertEqual(state, keyed())
        self.assertEqual(cache, ((header(), FIELDS), True))

    def test_completion_word_five_may_be_one(self):
        rig = Rig(answer=respond(words={5: 1}))
        (result, _), *_ = self.both(rig, exchange_once(cp.INFO, bytes(cp.SIZES[cp.INFO])))
        self.assertEqual(result, ("ok", bytes(cp.SIZES[cp.INFO])))

    def test_set_with_an_exact_echo_is_dispatched(self):
        payload = payload_of(cp.SIZES[cp.SET])
        (result, dispatched), calls, _, _ = self.both(Rig(), exchange_once(cp.SET, payload, write=True))
        self.assertEqual(result, ("ok", payload))
        self.assertIs(dispatched, True)
        self.assertEqual(calls, escape_log(cp.SET, payload))

    def test_dispatched_is_set_although_the_escape_then_fails(self):
        payload = payload_of(cp.SIZES[cp.SET])
        rig = Rig(answer=respond(status=NTSTATUS_INVALID_PARAMETER))
        (result, dispatched), *_ = self.both(rig, exchange_once(cp.SET, payload, write=True))
        self.assertEqual(result, ("ClientPowerError", "transient",
                                  f"Client-power escape failed (NT {NTSTATUS_INVALID_PARAMETER}, completion 1)", None))
        self.assertIs(dispatched, True)

    def test_dispatched_stays_clear_when_the_transport_fails_first(self):
        payload = payload_of(cp.SIZES[cp.SET])
        (result, dispatched), calls, _, _ = self.both(
            Rig(), exchange_once(cp.SET, payload, write=True, transport=header(**{"w0": 0})))
        self.assertEqual(result[:2], ("ClientPowerError", "malformed"))
        self.assertIs(dispatched, False)
        self.assertEqual(calls, [])

    def test_a_non_write_object_on_a_get_is_marked_as_python_does(self):
        def action(side):
            side.store(header())
            marker = SimpleNamespace(dispatched=False)
            return outcome(lambda: side.exchange(cp.INFO, bytes(cp.SIZES[cp.INFO]), marker)), marker.dispatched
        (result, dispatched), *_ = self.both(Rig(), action)
        self.assertEqual(result[0], "ok")
        self.assertIs(dispatched, True)

    def test_geometry_refusals_touch_nothing(self):
        cases = [
            (0x2080E61E, bytes(12), False),                      # unknown command
            (cp.INFO, b"short", False),                          # wrong payload length
            (cp.SET, bytes(cp.SIZES[cp.SET]), False),           # SET without a write
            (cp.INFO, bytes(cp.SIZES[cp.INFO]), True),          # GET with a write
            (cp.INFO, bytes(cp.RM26_SIZES[cp.INFO]), False),    # a size from another group
        ]
        for command, payload, write in cases:
            with self.subTest(command=hex(command), size=len(payload), write=write):
                (result, dispatched), calls, state, cache = self.both(Rig(), exchange_once(command, payload, write=write))
                self.assertEqual(result, MALFORMED_GEOMETRY)
                self.assertEqual(calls, [])
                self.assertEqual(state, FRESH)
                self.assertEqual(dispatched, False if write else None)

    def test_guard_overrun_is_malformed(self):
        rig = Rig(answer=respond(overrun=True))
        (result, _), calls, _, _ = self.both(rig, exchange_once(cp.INFO, bytes(cp.SIZES[cp.INFO])))
        self.assertEqual(result, ("ClientPowerError", "malformed", "Clock GET exceeded the guarded allocation", None))
        self.assertEqual(len(calls), 6)

    def test_unavailable_escape_is_transient_with_no_status(self):
        expected = ("ClientPowerError", "transient", "Client-power escape failed (NT None, completion 0)", None)
        for rig in (Rig(missing={ESCAPE}), Rig(adapters=[(222, 2, 0, 0)])):
            with self.subTest(missing=sorted(rig.missing), adapters=rig.adapters):
                (result, dispatched), calls, state, _ = self.both(
                    rig, exchange_once(cp.SET, bytes(cp.SIZES[cp.SET]), write=True))
                self.assertEqual(result, expected)
                self.assertIs(dispatched, True)
                self.assertNotIn("Escape", [c[0] for c in calls])
                self.assertEqual(state, keyed())

    def test_escape_status_and_completion(self):
        cases = [
            (respond(status=NTSTATUS_INVALID_PARAMETER),
             f"Client-power escape failed (NT {NTSTATUS_INVALID_PARAMETER}, completion 1)"),
            (respond(status=1, completion=0), "Client-power escape failed (NT 1, completion 0)"),
            (respond(completion=0), "Client-power escape failed (NT 0, completion 0)"),
            (respond(completion=2), "Client-power escape failed (NT 0, completion 2)"),
        ]
        for answer, message in cases:
            with self.subTest(message=message):
                (result, _), *_ = self.both(Rig(answer=answer), exchange_once(cp.INFO, bytes(cp.SIZES[cp.INFO])))
                self.assertEqual(result, ("ClientPowerError", "transient", message, None))

    def test_rm_status(self):
        for rm, kind in ((0x1F, "unsupported"), (0x3A, "unsupported"), (0x56, "unsupported"),
                         (0x40, "unsupported"), (0x99, "transient"), (0x1, "transient")):
            with self.subTest(rm=hex(rm)):
                (result, _), *_ = self.both(Rig(answer=respond(rm=rm)), exchange_once(cp.INFO, bytes(cp.SIZES[cp.INFO])))
                self.assertEqual(result, ("ClientPowerError", kind,
                                          f"Client-power command {cp.INFO:#x} rejected (RM {rm:#x})", rm))

    def test_response_envelope_changes_are_malformed(self):
        for words in ({15: 4}, {0: 0}, {2: 0}, {14: cp.CONTROL}, {12: 7}, {5: 2}):
            with self.subTest(words=words):
                (result, _), *_ = self.both(Rig(answer=respond(words=words)),
                                            exchange_once(cp.INFO, bytes(cp.SIZES[cp.INFO])))
                self.assertEqual(result, ("ClientPowerError", "malformed",
                                          "Client-power response changed its transport envelope", None))

    def test_changed_set_echo_is_malformed(self):
        payload = payload_of(cp.SIZES[cp.SET])
        (result, dispatched), *_ = self.both(Rig(answer=respond(echo_change=True)),
                                             exchange_once(cp.SET, payload, write=True))
        self.assertEqual(result, ("ClientPowerError", "malformed", "Client-power SET changed the requested packet", None))
        self.assertIs(dispatched, True)

    def test_binding_failure_comes_before_the_transport(self):
        rig = Rig(arch=3)  # Maxwell: no modern PERF groups
        (result, _), calls, state, cache = self.both(rig, exchange_once(cp.INFO, bytes(cp.SIZES[cp.INFO])))
        self.assertEqual(result, ("ClientPowerError", "unsupported",
                                  "Modern PERF groups require a Pascal-or-later architecture", None))
        self.assertEqual((calls, state), ([], FRESH))


class CallerObjectTests(ParityCase):
    """Python's own semantics on the caller's command, payload and write objects."""

    def test_payload_elements_are_masked_as_ctypes_stores_them(self):
        size = cp.SIZES[cp.INFO]
        payload = [0x100 + (k & 0xFF) if k % 3 else -1 - (k & 0x7F) for k in range(size)]
        masked = bytes(v & 0xFF for v in payload)
        (result, _), calls, state, _ = self.both(Rig(), exchange_once(cp.INFO, payload))
        self.assertEqual(result, ("ok", masked))  # the fake leaves the payload as the response
        self.assertEqual(calls, escape_log(cp.INFO, masked))
        self.assertEqual(state, keyed())

    def test_a_sequence_set_payload_never_matches_its_bytes_echo(self):
        # `result != payload` compares bytes with the caller's object: a list or tuple always differs,
        # after the dispatch.
        size = cp.SIZES[cp.SET]
        for payload in (list(payload_of(size)), tuple(payload_of(size)), [v + 0x100 for v in payload_of(size)]):
            with self.subTest(type=type(payload).__name__, first=payload[0]):
                (result, dispatched), calls, _, _ = self.both(Rig(), exchange_once(cp.SET, payload, write=True))
                self.assertEqual(result, ("ClientPowerError", "malformed",
                                          "Client-power SET changed the requested packet", None))
                self.assertIs(dispatched, True)
                self.assertEqual(calls, escape_log(cp.SET, bytes(v & 0xFF for v in payload)))

    def test_bytes_like_set_payloads_match_their_echo(self):
        size = cp.SIZES[cp.SET]
        for payload in (bytearray(payload_of(size)), memoryview(payload_of(size))):
            with self.subTest(type=type(payload).__name__):
                (result, dispatched), *_ = self.both(Rig(), exchange_once(cp.SET, payload, write=True))
                self.assertEqual(result, ("ok", payload_of(size)))
                self.assertIs(dispatched, True)

    def test_a_payload_ctypes_cannot_store_fails_after_the_transport(self):
        size = cp.SIZES[cp.INFO]
        for payload in ("a" * size, [1.0] * size, [0] * (size - 1) + ["x"]):
            with self.subTest(kind=type(payload).__name__, last=payload[-1]):
                (result, _), calls, state, _ = self.both(Rig(), exchange_once(cp.INFO, payload))
                self.assertEqual(result[0], "TypeError")
                self.assertIn("cannot be interpreted as an integer", result[1])
                self.assertEqual((calls, state), ([], keyed()))  # transport keyed, nothing dispatched

    def test_an_int_subclass_command_is_the_command(self):
        Command = enum.IntEnum("Command", {"INFO": cp.INFO, "SET": cp.SET})
        payload = payload_of(cp.SIZES[cp.SET])
        for command, data, write in ((Command.INFO, bytes(cp.SIZES[cp.INFO]), False), (Command.SET, payload, True)):
            with self.subTest(command=command.name):
                (result, dispatched), calls, state, _ = self.both(Rig(), exchange_once(command, data, write=write))
                self.assertEqual(result, ("ok", data))
                self.assertEqual(calls, escape_log(int(command), data))
                self.assertEqual(state, keyed())

    def test_a_float_command_binds_and_keys_then_fails_at_the_command_word(self):
        payload = payload_of(cp.SIZES[cp.SET])
        for command, data, write in ((float(cp.INFO), bytes(cp.SIZES[cp.INFO]), False),
                                     (float(cp.SET), payload, True)):
            with self.subTest(command=command):
                (result, dispatched), calls, state, cache = self.both(Rig(), exchange_once(command, data, write=write))
                self.assertEqual(result, ("TypeError", "'float' object cannot be interpreted as an integer"))
                self.assertEqual(dispatched, False if write else None)
                self.assertEqual((calls, state, cache), ([], keyed(), ((header(), FIELDS), True)))

    def test_a_float_command_goes_through_on_rm26_where_the_table_gives_the_wire_command(self):
        response = payload_of(cp.RM26_SIZES[cp.INFO], 0x21)

        def action(side):
            side.store(RM26)
            side.transport()
            return outcome(lambda: side.exchange(float(cp.INFO), bytes(cp.RM26_SIZES[cp.INFO])))
        result, calls, state, _ = self.both(Rig(arch=4, answer=respond(response=response)), action)
        self.assertEqual(result, ("ok", response))
        self.assertEqual(calls, escape_log(cp.INFO, bytes(cp.RM26_SIZES[cp.INFO]), RM26, wire=0x20802630))
        self.assertEqual(state, keyed((P2612, 2524, 2456), sizes="RM26_SIZES", rm26=True, rm26_records=True))

    def test_a_write_object_that_cannot_be_marked_fails_before_the_escape(self):
        for write in (1, object()):
            with self.subTest(write=type(write).__name__):
                def action(side, write=write):
                    side.store(header())
                    return outcome(lambda: side.exchange(cp.INFO, bytes(cp.SIZES[cp.INFO]), write))
                result, calls, state, _ = self.both(Rig(), action)
                self.assertEqual(result[0], "AttributeError")
                self.assertIn("dispatched", result[1])
                self.assertEqual((calls, state), ([], keyed()))

    def test_an_unmarkable_write_object_still_loses_to_an_earlier_transport_failure(self):
        def action(side):
            side.store(header(w0=0))
            return outcome(lambda: side.exchange(cp.INFO, bytes(cp.SIZES[cp.INFO]), 1))
        result, calls, state, _ = self.both(Rig(), action)
        self.assertEqual(result, ("ClientPowerError", "malformed", "Client-power transport envelope is not understood",
                                  None))
        self.assertEqual((calls, state), ([], FRESH))


class OrderTests(ParityCase):
    """Two faults at once: the check Python runs first wins."""

    def test_stored_words_five_eleven_and_sixteen_reach_the_escape_as_zero(self):
        h = header(w5=0x55, w11=0x1111, w16=0x1F)
        size = cp.SIZES[cp.INFO]
        (result, _), calls, state, cache = self.both(Rig(), exchange_once(cp.INFO, bytes(size), transport=h))
        self.assertEqual(result, ("ok", bytes(size)))
        sent = calls[3][-1]
        self.assertEqual([int.from_bytes(sent[4 * i:4 * i + 4], "little") for i in (5, 11, 16)], [0, 0, 0])
        self.assertEqual(calls, escape_log(cp.INFO, bytes(size), h))
        self.assertEqual(cache, ((h, FIELDS), True))

    def test_guard_damage_on_either_side_wins_over_a_failing_status(self):
        overrun = ("ClientPowerError", "malformed", "Clock GET exceeded the guarded allocation", None)
        for answer in (respond(underrun=True), respond(underrun=True, status=NTSTATUS_INVALID_PARAMETER),
                       respond(overrun=True, status=NTSTATUS_INVALID_PARAMETER, completion=0, rm=0x99),
                       respond(underrun=True, words={12: 7})):
            with self.subTest():
                (result, _), calls, _, _ = self.both(Rig(answer=answer), exchange_once(cp.INFO, bytes(cp.SIZES[cp.INFO])))
                self.assertEqual(result, overrun)
                self.assertEqual(len(calls), 6)

    def test_two_faults(self):
        cases = [
            (respond(completion=0, rm=0x1F), ("transient", "Client-power escape failed (NT 0, completion 0)", None)),
            (respond(status=NTSTATUS_INVALID_PARAMETER, words={12: 7}),
             ("malformed", "Client-power response changed its transport envelope", None)),
            (respond(rm=0x56, words={15: 4}), ("malformed", "Client-power response changed its transport envelope", None)),
            (respond(rm=0x56, words={5: 2}), ("malformed", "Client-power response changed its transport envelope", None)),
        ]
        for answer, expected in cases:
            with self.subTest(expected=expected[1]):
                (result, _), *_ = self.both(Rig(answer=answer), exchange_once(cp.INFO, bytes(cp.SIZES[cp.INFO])))
                self.assertEqual(result, ("ClientPowerError", *expected))

    def test_a_changed_set_echo_anywhere_is_malformed(self):
        size = cp.SIZES[cp.SET]
        for index in (68, 68 + size // 2, 68 + size - 1):
            with self.subTest(byte=index - 68):
                (result, dispatched), *_ = self.both(Rig(answer=respond(echo_change=index)),
                                                     exchange_once(cp.SET, payload_of(size), write=True))
                self.assertEqual(result, ("ClientPowerError", "malformed",
                                          "Client-power SET changed the requested packet", None))
                self.assertIs(dispatched, True)

    def test_an_unset_layout_choice_stays_unset(self):
        (result, _), _, state, cache = self.both(Rig(), exchange_once(cp.INFO, bytes(cp.SIZES[cp.INFO]), choice=False))
        self.assertEqual(result[0], "ok")
        self.assertEqual((state, cache), (keyed(), ((header(), FIELDS), False)))


# ---- the transport ------------------------------------------------------------------------------

def transport_once(transport, first=None, choice=True):
    def action(side):
        side.store(transport, choice)
        if first is not None:
            first(side)
        return outcome(side.transport)
    return action


class TransportTests(ParityCase):
    def test_a612_transport(self):
        result, calls, state, cache = self.both(Rig(), transport_once(header()))
        self.assertEqual(result, ("ok", (tuple(header()), FIELDS)))
        self.assertEqual((calls, state, cache), ([], keyed(), ((header(), FIELDS), True)))

    def test_malformed_shapes_a_fixture_can_store(self):
        # _rm_power_transport rejects both shapes first (CurrentLimitUnsupported) and drops the cache.
        for transport in (header()[:16], header(54424, 54352)):
            with self.subTest(words=len(transport), packet_bytes=transport[2]):
                result, _, state, cache = self.both(Rig(), transport_once(transport))
                self.assertEqual(result, ("ClientPowerError", "unsupported",
                                          "unvalidated current-policy transport geometry", None))
                self.assertEqual((state, cache), (FRESH, (None, False)))

    def test_envelope_mismatch_is_malformed_and_not_keyed(self):
        for word, value in ((0, 0), (1, 0x10003), (3, 0), (4, 0x0500002C)):
            with self.subTest(word=word):
                result, _, state, _ = self.both(Rig(), transport_once(header(**{f"w{word}": value})))
                self.assertEqual(result, ("ClientPowerError", "malformed",
                                          "Client-power transport envelope is not understood", None))
                self.assertEqual(state, FRESH)

    def test_transport_key_change_after_a_first_success_is_identity(self):
        def first(side):
            side.exchange(cp.INFO, bytes(cp.SIZES[cp.INFO]))
            side.store(header(11748, 11680))
        result, calls, state, _ = self.both(Rig(), transport_once(header(), first))
        self.assertEqual(result, ("ClientPowerError", "identity",
                                  "Client-power transport changed; create a fresh controller", None))
        self.assertEqual(len(calls), 6)  # the first exchange's escape only
        self.assertEqual(state, keyed())

    def test_rm26_keys_on_pascal_and_turing(self):
        for arch in (4, 6):
            for h, group, extended in ((RM26, "RM26_SIZES", False), (RM26_EXTENDED, "RM26_EXTENDED_SIZES", True)):
                with self.subTest(arch=arch, extended=extended):
                    sizes = getattr(cp, group)
                    response = payload_of(sizes[cp.INFO], 0x11)

                    def action(side, h=h, sizes=sizes):
                        side.store(h)
                        first = outcome(side.transport)
                        middle = side.state()
                        return first, middle, outcome(lambda: side.exchange(cp.INFO, bytes(sizes[cp.INFO])))
                    rig = Rig(arch=arch, answer=respond(response=response))
                    (first, middle, result), calls, state, _ = self.both(rig, action)
                    key = (P2612, h[2], h[15])
                    expected = keyed(key, sizes=group, rm26=True, rm26_records=True, rm26_extended=extended)
                    self.assertEqual(first, ("ok", (tuple(h), FIELDS)))
                    self.assertEqual((middle, state), (expected, expected))
                    self.assertEqual(result, ("ok", response))
                    self.assertEqual(calls, escape_log(cp.INFO, bytes(sizes[cp.INFO]), h, wire=0x20802630))

    def test_rm26_rejection_reports_the_wire_command(self):
        def action(side):
            side.store(RM26)
            side.transport()
            return outcome(lambda: side.exchange(cp.CONTROL, bytes(cp.RM26_SIZES[cp.CONTROL])))
        result, *_ = self.both(Rig(arch=4, answer=respond(rm=0x1F)), action)
        self.assertEqual(result, ("ClientPowerError", "unsupported", "Client-power command 0x20802632 rejected (RM 0x1f)",
                                  0x1F))

    def test_first_exchange_on_an_rm26_transport_switches_sizes_under_the_payload(self):
        # _exchange checks the payload against SIZES, _transport then selects RM26_SIZES, and the
        # payload no longer fits the packet: Python's bare ctypes ValueError, before any dispatch.
        (result, dispatched), calls, state, _ = self.both(
            Rig(arch=4), exchange_once(cp.SET, bytes(cp.SIZES[cp.SET]), transport=RM26, write=True))
        self.assertEqual(result, ("ValueError", "Can only assign sequence of same size"))
        self.assertIs(dispatched, False)
        self.assertEqual(calls, [])
        self.assertEqual(state, keyed((P2612, 2524, 2456), sizes="RM26_SIZES", rm26=True, rm26_records=True))

    def test_rm26_on_ampere_is_unsupported(self):
        for h in (RM26, RM26_EXTENDED):
            with self.subTest(packet_bytes=h[2]):
                result, _, state, _ = self.both(Rig(arch=7), transport_once(h))
                self.assertEqual(result, ("ClientPowerError", "unsupported",
                                          "This client-power RM26 ABI is not qualified for this GPU generation", None))
                self.assertEqual(state, FRESH)

    def test_other_26xx_geometry_is_an_unknown_family(self):
        result, _, state, _ = self.both(Rig(arch=6), transport_once(header(command=P2612)))
        self.assertEqual(result, ("ClientPowerError", "unsupported", "Client-power command family is not understood",
                                  None))
        self.assertEqual(state, FRESH)

    def test_rm26_first_then_a612_is_identity(self):
        changed = ("ClientPowerError", "identity", "Client-power transport changed; create a fresh controller", None)
        # keyed: the key check refuses
        result, _, state, _ = self.both(Rig(arch=6), transport_once(header(), lambda s: (s.store(RM26), s.transport(),
                                                                                         s.store(header()))))
        self.assertEqual(result, changed)
        # not keyed (the RM26 envelope failed after selecting the family): the sticky _rm26 refuses
        bad = header(2524, 2456, P2612, w3=0)
        result, _, state, _ = self.both(Rig(arch=6), transport_once(header(), lambda s: (s.store(bad), outcome(s.transport),
                                                                                         s.store(header()))))
        self.assertEqual(result, changed)
        self.assertEqual(state, dict(FRESH, sizes="RM26_SIZES", rm26=True, rm26_records=True))

    def test_reset_transport_clears_both_cache_attributes(self):
        def action(side):
            side.store(header())
            side.transport()
            before = side.cache()
            side.reset_transport()
            return before, side.cache()
        (before, after), _, state, _ = self.both(Rig(), action)
        self.assertEqual((before, after), (((header(), FIELDS), True), (None, False)))
        self.assertEqual(state, keyed())  # the controller keeps its key

    def test_fresh_controller_forgets_the_key(self):
        def action(side):
            side.store(header())
            side.transport()
            side.reset()
            fresh = side.state()
            side.store(header(11748, 11680))
            return fresh, outcome(side.transport)
        (fresh, result), _, state, _ = self.both(Rig(), action)
        self.assertEqual(fresh, FRESH)
        self.assertEqual(result, ("ok", (tuple(header(11748, 11680)), FIELDS)))
        self.assertEqual(state, keyed((A612, 11748, 11680)))

    def test_fresh_controller_after_rm26_returns_to_sizes(self):
        def action(side):
            side.store(RM26_EXTENDED)
            side.transport()
            side.reset()
            return side.state()
        fresh, *_ = self.both(Rig(arch=4), action)
        self.assertEqual(fresh, FRESH)

    def test_rm26_is_refused_beyond_pascal_and_turing(self):
        for arch in (5, 7, 8, 9, 10):
            for h in (RM26, RM26_EXTENDED):
                with self.subTest(arch=arch, packet_bytes=h[2]):
                    result, _, state, _ = self.both(Rig(arch=arch), transport_once(h))
                    self.assertEqual(result, ("ClientPowerError", "unsupported",
                                              "This client-power RM26 ABI is not qualified for this GPU generation",
                                              None))
                    self.assertEqual(state, FRESH)

    def test_without_a_stored_or_capturable_transport_the_failure_is_transient(self):
        def action(side):
            side.store(header())
            side.transport()
            side.reset_transport()
            return outcome(side.transport), outcome(lambda: side.exchange(cp.INFO, bytes(cp.SIZES[cp.INFO])))
        (transport, exchange), calls, state, cache = self.both(Rig(), action)
        expected = ("ClientPowerError", "transient",
                    "unsupported current-policy transport: no recognized power GET captured", None)
        self.assertEqual((transport, exchange), (expected, expected))
        self.assertEqual((calls, state, cache), ([], keyed(), (None, False)))

    def test_the_key_check_comes_before_the_rm26_switch(self):
        changed = ("ClientPowerError", "identity", "Client-power transport changed; create a fresh controller", None)

        def a612_then_rm26(side):
            side.store(header())
            side.transport()
            side.store(RM26)
            return outcome(side.transport)
        result, _, state, _ = self.both(Rig(arch=4), a612_then_rm26)
        self.assertEqual((result, state), (changed, keyed()))

        def rm26_then_extended(side):
            side.store(RM26)
            side.transport()
            side.store(RM26_EXTENDED)
            return outcome(side.transport)
        result, _, state, _ = self.both(Rig(arch=4), rm26_then_extended)
        self.assertEqual((result, state), (changed, keyed((P2612, 2524, 2456), sizes="RM26_SIZES", rm26=True,
                                                          rm26_records=True)))

    def test_binding_failures_keep_their_kind_and_do_not_stick(self):
        unread = ("ClientPowerError", "transient", "GPU architecture could not be read", None)
        unsupported = ("ClientPowerError", "unsupported", "Modern PERF groups require a Pascal-or-later architecture",
                       None)
        for options, expected in ((dict(arch=7, arch_status=3), unread), (dict(arch=99), unread),
                                  (dict(arch=3), unsupported)):
            with self.subTest(**options):
                rig = Rig(**options)

                def action(side, rig=rig, options=options):
                    rig.card.arch, rig.card.arch_status = options["arch"], options.get("arch_status", 0)
                    side.store(header())
                    first = outcome(side.transport), outcome(lambda: side.exchange(cp.INFO, bytes(cp.SIZES[cp.INFO])))
                    middle = side.state()
                    rig.card.arch, rig.card.arch_status = 7, 0  # the driver now answers
                    second = outcome(side.transport)
                    side.reset()
                    fresh = side.state()
                    return first, middle, second, fresh, outcome(side.transport)
                (first, middle, second, fresh, again), calls, state, _ = self.both(rig, action)
                self.assertEqual(first, (expected, expected))
                self.assertEqual((middle, fresh), (FRESH, FRESH))
                if expected is unread:  # an unread architecture is not cached: the binding recovers
                    self.assertEqual(second, ("ok", (tuple(header()), FIELDS)))
                else:  # a recognised one is cached for the handle: a fresh controller binds it again
                    self.assertEqual(second, unsupported)
                    self.assertEqual(again, unsupported)
                self.assertEqual(calls, [])

    def test_a_fresh_controller_binds_again(self):
        def action(side):
            side.store(header())
            first = outcome(side.transport)
            side.reset()
            return first, side.state(), outcome(side.transport)
        (first, fresh, again), _, state, _ = self.both(Rig(), action)
        self.assertEqual((first, again), (("ok", (tuple(header()), FIELDS)),) * 2)
        self.assertEqual((fresh, state), (FRESH, keyed()))

    def test_state_sizes_is_the_module_table(self):
        opened = Rig().open()
        try:
            self.assertIs(opened._cp_state()["sizes"], cp.SIZES)
        finally:
            opened.close()


# ---- the C ABI directly -------------------------------------------------------------------------

class AbiTests(unittest.TestCase):
    STOP_DISPATCH = b"caller stop: the dispatch"

    def setUp(self):
        self.lib = native.load()
        self.gpu = None
        self.open(Rig(arch=4))

    def open(self, rig):
        if self.gpu is not None:
            self.gpu.close()
        self.rig, self.gpu = rig, rig.open()
        self.gpu._current_limit_transport = (header(), dict(FIELDS))
        self.gpu._current_limit_layout_choice = 0

    def tearDown(self):
        self.gpu.close()
        native.set_export_resolver(None)

    def exchange(self, command, payload, *, write=0, flags=0, hook=None, capacity=None, out=True, dispatched=True,
                 error=True, error_size=None, handle=True, payload_pointer=True):
        capacity = len(payload) if capacity is None else capacity
        buffer = (ctypes.c_ubyte * max(capacity, 1))()
        flag = ctypes.c_int32(7)  # every return overwrites it
        e = native._Error()
        e.size = ctypes.sizeof(native._Error) if error_size is None else error_size
        e.kind = 99  # untouched unless the call reports an error
        status = self.lib.dc_cpc_exchange(
            self.gpu._handle if handle else None, command, payload if payload_pointer else None, len(payload), write,
            flags, ctypes.cast(hook, ctypes.c_void_p) if hook else None, None,
            ctypes.addressof(buffer) if out else None, capacity, ctypes.byref(flag) if dispatched else None,
            ctypes.byref(e) if error else None)
        return status, flag.value, e, bytes(buffer)[:capacity]

    def state(self):
        s = self.gpu._cp_state()
        return dict(s, sizes=group_name(s["sizes"]))

    def test_argument_refusals_clear_dispatched_and_touch_nothing(self):
        info = bytes(cp.SIZES[cp.INFO])
        cases = {
            "handle": dict(handle=False),
            "payload": dict(payload_pointer=False),
            "out": dict(out=False),
            "error size": dict(error_size=4),
            "unknown flag": dict(flags=8),
        }
        for name, options in cases.items():
            with self.subTest(name):
                status, flag, e, _ = self.exchange(cp.INFO, info, **options)
                self.assertEqual((status, flag, e.kind), (native.DC_E_ARGUMENT, 0, 99))
        status, *_ = self.exchange(cp.INFO, info, dispatched=False)
        self.assertEqual(status, native.DC_E_ARGUMENT)
        self.assertEqual((self.rig.calls, self.state()), ([], FRESH))

    def test_a_small_output_is_refused_before_binding_or_dispatch(self):
        payload = payload_of(cp.SIZES[cp.SET])
        for capacity in (0, len(payload) - 1):
            with self.subTest(capacity=capacity):
                status, flag, e, _ = self.exchange(cp.SET, payload, write=1, capacity=capacity, out=capacity > 0)
                self.assertEqual((status, flag, e.kind), (native.DC_E_BUFFER, 0, 99))
        self.assertEqual((self.rig.calls, self.state()), ([], FRESH))  # not even the transport ran
        status, flag, e, out = self.exchange(cp.SET, payload, write=1, capacity=len(payload) + 4)
        self.assertEqual((status, flag, e.kind, out[:len(payload)]), (native.DC_OK, 1, native.DC_ERR_NONE, payload))

    def test_the_core_refuses_bad_geometry_itself(self):
        cases = [
            (cp.SET, bytes(cp.SIZES[cp.SET]), 0),         # SET without a write
            (cp.INFO, bytes(cp.SIZES[cp.INFO]), 1),       # GET flagged as a write
            (0x2080E61E, bytes(12), 0),                    # unknown command
            (cp.INFO, bytes(cp.SIZES[cp.INFO] - 4), 0),   # wrong payload size
        ]
        for command, payload, write in cases:
            with self.subTest(command=hex(command), size=len(payload), write=write):
                status, flag, e, _ = self.exchange(command, payload, write=write)
                self.assertEqual((status, flag, e.kind, native._text(e.message)),
                                 (native.DC_E_FAILED, 0, 5, "Client-power command or packet geometry is not understood"))
        self.assertEqual((self.rig.calls, self.state()), ([], FRESH))

    def test_error_kinds_and_dispatched_on_the_escape_paths(self):
        info = bytes(cp.SIZES[cp.INFO])
        self.open(Rig(arch=4, missing={ESCAPE}))
        status, flag, e, _ = self.exchange(cp.INFO, info)
        self.assertEqual((status, flag, e.kind, e.has_ntstatus, native._text(e.message)),
                         (native.DC_E_FAILED, 1, native.DC_ERR_TRANSIENT, 0,
                          "Client-power escape failed (NT None, completion 0)"))
        self.open(Rig(arch=4))
        for answer, kind, rm in ((respond(rm=0x56), native.DC_ERR_UNSUPPORTED, 0x56),
                                 (respond(rm=0x99), native.DC_ERR_TRANSIENT, 0x99),
                                 (respond(status=NTSTATUS_INVALID_PARAMETER), native.DC_ERR_TRANSIENT, 0),
                                 (respond(words={12: 7}), 5, 0), (respond(overrun=True), 5, 0)):
            with self.subTest(kind=kind, rm=rm):
                self.rig.answer = answer
                status, flag, e, _ = self.exchange(cp.INFO, info)
                self.assertEqual((status, flag, e.kind, e.rm_status), (native.DC_E_FAILED, 1, kind, rm))
        self.gpu._current_limit_transport = (header(w0=0), dict(FIELDS))
        self.gpu._cp_reset()
        status, flag, e, _ = self.exchange(cp.INFO, info)
        self.assertEqual((status, flag, e.kind), (native.DC_E_FAILED, 0, 5))

    def test_a_refusing_dispatch_hook_stops_before_the_escape(self):
        seen = []

        @ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p)
        def refuse(context):
            seen.append("refused")
            return 1
        payload = payload_of(cp.SIZES[cp.SET])
        status, flag, e, _ = self.exchange(cp.SET, payload, write=1, hook=refuse)
        self.assertEqual((status, flag, e.kind, e.message), (native.DC_E_FAILED, 0, native.DC_ERR_INVALID,
                                                              self.STOP_DISPATCH))
        # called once, after the transport keyed the controller, and nothing reached the escape
        self.assertEqual((seen, self.rig.calls, self.state()), (["refused"], [], keyed()))

        @ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p)
        def allow(context):
            seen.append("allowed")
            return 0
        status, flag, *_ = self.exchange(cp.SET, payload, write=1, hook=allow)
        self.assertEqual((status, flag, seen[-1]), (native.DC_OK, 1, "allowed"))

    def test_caller_stops_and_the_caller_echo(self):
        info = bytes(cp.SIZES[cp.INFO])
        status, flag, e, _ = self.exchange(cp.INFO, info, flags=1)
        self.assertEqual((status, flag, e.kind, e.message), (native.DC_E_FAILED, 0, native.DC_ERR_INVALID,
                                                              b"caller stop: the command word"))
        status, flag, e, _ = self.exchange(cp.INFO, info, flags=2)
        self.assertEqual((status, flag, e.message), (native.DC_E_FAILED, 0, b"caller stop: the payload bytes"))
        self.assertEqual(self.rig.calls, [])
        self.rig.answer = respond(echo_change=True)
        payload = payload_of(cp.SIZES[cp.SET])
        status, flag, e, _ = self.exchange(cp.SET, payload, write=1)
        self.assertEqual((status, flag, e.kind), (native.DC_E_FAILED, 1, 5))
        status, flag, e, out = self.exchange(cp.SET, payload, write=1, flags=4)
        self.assertEqual((status, flag), (native.DC_OK, 1))
        self.assertEqual(out, bytes([payload[0] ^ 0xFF]) + payload[1:])

    def test_the_command_stop_is_skipped_where_rm26_gives_the_wire_command(self):
        self.gpu._current_limit_transport = (RM26, dict(FIELDS))
        self.gpu._cp_transport()
        status, flag, *_ = self.exchange(cp.INFO, bytes(cp.RM26_SIZES[cp.INFO]), flags=1)
        self.assertEqual((status, flag), (native.DC_OK, 1))

    def test_transport_and_state_arguments(self):
        t, e = native._RmTransport(), native._Error()
        t.size, e.size = ctypes.sizeof(t), ctypes.sizeof(e)
        lib, handle = self.lib, self.gpu._handle
        self.assertEqual(lib.dc_cpc_transport(None, ctypes.byref(t), ctypes.byref(e)), native.DC_E_ARGUMENT)
        self.assertEqual(lib.dc_cpc_transport(handle, None, ctypes.byref(e)), native.DC_E_ARGUMENT)
        t.size = ctypes.sizeof(t) - 4
        self.assertEqual(lib.dc_cpc_transport(handle, ctypes.byref(t), ctypes.byref(e)), native.DC_E_ARGUMENT)
        t.size, e.size = ctypes.sizeof(t), 8
        self.assertEqual(lib.dc_cpc_transport(handle, ctypes.byref(t), ctypes.byref(e)), native.DC_E_ARGUMENT)
        e.size = ctypes.sizeof(e)
        self.assertEqual(lib.dc_cpc_transport(handle, ctypes.byref(t), None), native.DC_OK)  # error is optional
        s = native.Gpu._CpcState()
        self.assertEqual(lib.dc_cpc_state_get(handle, None), native.DC_E_ARGUMENT)
        self.assertEqual(lib.dc_cpc_state_get(None, ctypes.addressof(s)), native.DC_E_ARGUMENT)
        for size in (0, ctypes.sizeof(s) - 4, ctypes.sizeof(s) + 4):
            s.size, s.sizes = size, 77
            self.assertEqual(lib.dc_cpc_state_get(handle, ctypes.addressof(s)), native.DC_E_ARGUMENT)
            self.assertEqual((s.size, s.sizes), (size, 77))  # untouched
        s.size = ctypes.sizeof(s)
        self.assertEqual(lib.dc_cpc_state_get(handle, ctypes.addressof(s)), native.DC_OK)
        self.assertEqual(tuple(s.transport_key), A612_KEY)
        self.assertEqual(lib.dc_cpc_reset(None), native.DC_E_ARGUMENT)
        self.assertEqual(lib.dc_cpc_reset_transport(None), native.DC_E_ARGUMENT)


if __name__ == "__main__":
    unittest.main()
