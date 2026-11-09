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

Not reachable natively, and so covered only by tests/test_client_power_runtime.py:
  - the identity changing between two _binding() calls inside one _transport or _exchange (the
    fake identity cannot change while one core call runs);
  - "Client-power transport failed: {exc}" (an exception raised by the escape itself; the core's
    escape reports every failure as "not issued", which Python's nvbackend returns as None);
  - "Paired power transport could not be captured: {exc}" (a non-ValueError from the getter);
  - a header that is not a tuple/list of 32-bit ints or fields that are not exactly the five
    escape fields (the core's transport is a u32 array and a five-field struct by construction).
"""
import ctypes
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

def respond(*, completion=1, rm=0, status=0, words=None, response=None, overrun=False, echo_change=False):
    """The fake escape's answer: the words and bytes it writes, and the NTSTATUS it returns."""
    def answer(address, size):
        view = (u32 * (size // 4)).from_address(address)
        if response is not None:
            ctypes.memmove(address + 68, response, len(response))
        if echo_change:
            data = (ctypes.c_ubyte * size).from_address(address)
            data[68] ^= 0xFF
        view[11], view[16] = completion, rm
        for index, value in (words or {}).items():
            view[index] = value
        if overrun:
            ctypes.c_ubyte.from_address(address + size).value = 0x00  # one byte into the trailing guard
        return status
    return answer


class Rig:
    """A Card plus a fake gdi32 with the adapter at the card's slot (bus 1, device 0)."""

    def __init__(self, *, arch=7, adapters=((111, 1, 0, 0), (222, 2, 0, 0)), missing=(), answer=None):
        self.card = Card(arch=arch)
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
    except ValueError as exc:
        return ("ValueError", str(exc))


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

    def test_state_sizes_is_the_module_table(self):
        opened = Rig().open()
        try:
            self.assertIs(opened._cp_state()["sizes"], cp.SIZES)
        finally:
            opened.close()


if __name__ == "__main__":
    unittest.main()
