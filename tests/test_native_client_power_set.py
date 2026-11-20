# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native client-power controller, increment A7 (the request SET with its compensating
restoration) against client_power_runtime.ClientPowerController.set_request and _rollback.

native.Gpu._cp_set_request runs dc_cpc_set_request. The Python original runs the real
ClientPowerController(twin).set_request() on the nvbackend.GPU twin, whose _write_policy_limit is the
real B3 original. Both sides run on the read rig of tests/test_native_client_power_read.py (MetaRig:
one of the Python tests' own wire models as the responder of the fake gdi32 escape, each side with a
fresh fake), and every case compares

- the result ({"before", "after"}, key order included, nested dicts too) or the exception: its type,
  kind, message, rm_status and the rollback dict ("attempted", "verified", "error", and "readback"
  with its whole read() dict, key order included);
- the gdi call log (the escape fields and packet bytes: every GET and SET, so the SET payloads and
  their count, and the policy writer's own GETs and SETs on the peer path);
- the controller state and the transport cache (reset before every compensation, so the
  compensation captures the transport again);
- the fake's own command log, its counts, its live request and the peer writes it received;
- the pins, the board policy, the policy mask and the last policy table the writer left behind.

Every failure after the SET is dispatched needs the transport captured again, so those rigs carry
the PowerPolInfo getter (recapture). The fakes' scripted events are FakeGpu's: {"ignore"},
{"mutate"}, {"rm"}, {"nt"}, {"raise"}, {"header"}, {"guard"} and {"payload"}, applied by the same
fake on both sides at the same count of the same command.

The peer path: ModernCoupledGpu's board SET recalculates two v30 peers. PeerCoupledGpu (below)
also answers the policy writer's own commands, the native policy GETs at the writer's layout
(found by the writer's read-only info probe: the rig refuses the other candidate sizes with RM 0x1F)
and its SET (0x2080E61B), which writes the selected policy's request word. With pinned peers, a
failed board write is compensated by replaying the captured board request (which the fake answers
with the unpinned baselines), then by one writer call per differing peer, then by a final read.

Recorded divergences and unreachable paths:

- {"raise": RuntimeError(...)} on the SET: Python's own tests call the fake directly, so its
  exception reaches _exchange as "Client-power transport failed: partial write". Here the fake
  answers through the gdi escape on BOTH sides, and an exception cannot cross that callback: the
  rig turns the fake's raise into the escape's NTSTATUS (STATUS_UNSUCCESSFUL), after the fake has
  applied the SET, so both sides report "Client-power escape failed (NT -1073741823, completion
  1)". The native escape has no exception path at all (as A3 records); the parity is exact.
- The target identity cannot change inside one core call through the public fakes (the binding's
  identity is fixed when the handle opens; see tests/test_native_client_power_controller.py), and no
  Python test reaches _rollback's per-peer lookup either. The lookup before each peer write is
  therefore reached through a test seam, dc_hook_test_cpc_binding (Gpu._cp_test_lookup): it makes
  the controller's nth target lookup fail with the sticky reader's identity refusal or return a
  target whose subsystem id differs, and the Python side patches the controller's _binding on the
  same call. PeerLookupTests covers both, the changed identity ("GPU binding changed before peer
  restoration") and the failed lookup (the reader's own "GPU binding changed; create a fresh PERF
  reader"). The identity change after the SET is also reachable through the transport key:
  IdentityTests changes the driver's power GET during the SET, so the compensation's fresh capture
  is refused as "Client-power transport changed; create a fresh controller" before it dispatches,
  with the exact rollback dict Python builds.
- Python's BaseException branch (a KeyboardInterrupt during the SET: add_note and .rollback, then
  re-raised) is not reachable natively: nothing interrupts the core call.
- The RM26 packet family's peer restoration runs the writer on the RM26 policy layout, which
  Rm26Gpu does not serve (no STATUS); it stays covered by tests/test_client_power_rm26.py.
- The outer error's "transient" default (getattr(exc, "kind", "transient") for an inner exception
  that is no ClientPowerError) is unreachable: after the dispatch, every failure set_request
  compensates for is a ClientPowerError (the helpers' exceptions need a malformed reading _once has
  already refused); the branch mirrors Python and is left untested.
- dc_cpc_set_request's DC_E_BUFFER is unreachable (every size group fits dc_cpc_observation);
  AbiTests checks the binding's answer to it with a stubbed export.
"""
import ctypes
from types import SimpleNamespace
import struct
import threading
import unittest
from unittest.mock import Mock, patch

from druta import client_power_runtime as cp
from druta import native
from tests.test_client_power_driver_compat import ModernCoupledGpu, modern_v30_gpu
from tests.test_client_power_legacy import LegacyGpu
from tests.test_client_power_rm26 import Rm26Gpu
from tests.test_client_power_rm26_extended import ExtendedRm26Gpu
from tests.test_client_power_runtime import FakeGpu, put
from tests.test_client_power_turing import TuringGpu
from tests.test_native_client_power_metadata import (
    A612_KEY, MODERN, TURING_KEY, MetadataCase, MetaPython, MetaRig, commands, legacy_accepted, ordered, store)
from tests.test_native_client_power_read import NO_TRANSPORT, RM26_EXTENDED_KEY, RM26_KEY, faults, payload, word
from tests.test_native_transport import install

if not native.available():
    raise unittest.SkipTest("native core not built (native/build.sh linux)")

POLICY_SET = 0x2080E61B
STATUS_UNSUCCESSFUL = -1073741823  # 0xC0000001: the escape status a raising fake stands for
INVALID = "Power request must be positive whole uint32 milliwatts below the missing-client sentinel"
MISMATCH = "Client-power request or preserved definitions did not read back exactly"
NOT_RESTORED = "Original client-power definitions and requests were not restored"
VERIFIED = "Original request restored and verified"
UNVERIFIED = "Original request restoration was not verified"
CHANGED_TRANSPORT = "Client-power transport changed; create a fresh controller"
SET_ECHO = "Client-power SET changed the requested packet"
ENVELOPE = "Client-power response changed its transport envelope"
GUARD = "Clock GET exceeded the guarded allocation"
FE = cp.CONTROL_BASE + 37 * cp.CONTROL_STRIDE + 4  # FakeGpu's client request word
COUPLED_ACCEPTED = {**MODERN, POLICY_SET: {cp.SIZES[cp.NATIVE_CONTROL]}}


def rejected(rm, command=cp.SET):
    return f"Client-power command {command:#x} rejected (RM {rm:#x})"


def escape_failed(nt, completion=1):
    return f"Client-power escape failed (NT {nt}, completion {completion})"


# ---- the rig ------------------------------------------------------------------------------------

class SetRig(MetaRig):
    """MetaRig whose fake may raise its scripted RuntimeError: the escape then returns
    STATUS_UNSUCCESSFUL (an exception cannot cross the escape callback on either side)."""

    def fresh(self):
        super().fresh()
        self.raised = []

    def respond(self, address, size, fields):
        try:
            return super().respond(address, size, fields)
        except RuntimeError as exc:
            self.raised.append(str(exc))
            return STATUS_UNSUCCESSFUL


class PeerCoupledGpu(ModernCoupledGpu):
    """ModernCoupledGpu that also answers the policy writer's SET (0x2080E61B at the native CONTROL
    size): the selected policy (the only bit of control word 4) takes the request word at its row.
    Its events are FakeGpu's, counted per command: {"rm"}, {"nt"} and {"ignore"}."""

    def __init__(self, *, pinned=False):
        super().__init__(pinned=pinned)
        self.peer_writes = []

    def _legacy_clk_escape(self, packet, fields):
        if int(packet[14]) != POLICY_SET:
            return super()._legacy_clk_escape(packet, fields)
        assert self._lock.held
        size = int(packet[15])
        assert size == cp.SIZES[cp.NATIVE_CONTROL] and packet[2] == size + 68
        assert fields == {"hAdapter": 12, "hDevice": 0, "Type": 0, "Flags": 1, "hContext": 0}
        request = bytes(packet)[68:]
        self.calls.append((POLICY_SET, request))
        self.counts[POLICY_SET] += 1
        event = self.faults.get((POLICY_SET, self.counts[POLICY_SET]), {})
        selector = cp._u32(request, 16)
        assert selector and not selector & (selector - 1), "a policy SET selects exactly one policy"
        policy = selector.bit_length() - 1
        at = cp.NATIVE_CONTROL_BASE + policy * cp.NATIVE_CONTROL_STRIDE + 4
        self.peer_writes.append((policy, cp._u32(request, at)))
        if not event.get("ignore"):
            put(self.native_control, at, cp._u32(request, at))
        packet[5], packet[11], packet[16] = 1, 1, event.get("rm", 0)
        return event.get("nt", 0)


def set_request(side, mw):
    if isinstance(side, MetaPython):
        return side.controller.set_request(mw)
    return side.gpu._cp_set_request(mw)


def set_outcome(call):
    try:
        return ("ok", call())
    except cp.ClientPowerError as exc:
        return ("ClientPowerError", exc.kind, str(exc), exc.rm_status, exc.rollback)
    except (ValueError, TypeError, AttributeError, IndexError) as exc:
        return (type(exc).__name__, str(exc))


def bookkeeping(side, fake):
    """What the request and the policy writer leave on the GPU and in the fake driver."""
    gpu = side.gpu
    return {"pins": dict(getattr(gpu, "_power_policy_pins", None) or {}),
            "board_policy": getattr(gpu, "_board_policy", None),
            "policy_mask": getattr(gpu, "_power_policy_mask", None) or 0,
            "table": getattr(gpu, "_last_policy_table", None),
            "requested": fake.requested,
            "counts": dict(fake.counts),
            "peer_writes": list(getattr(fake, "peer_writes", ()))}


def set_once(rig, *requests, choice=True):
    """Store the fake's transport, then one set_request per value on one controller."""
    requests = requests or (551000,)

    def action(side):
        store(side, rig, choice)
        results = [set_outcome(lambda mw=mw: set_request(side, mw)) for mw in requests]
        return results, bookkeeping(side, rig.fake), list(rig.raised)
    return action


class SetCase(MetadataCase):
    def run_set(self, rig, *requests, action=None, choice=True):
        """(the outcome of each request, bookkeeping, the fake's raises), gdi calls, state, cache,
        fake calls - identical on both sides (MetadataCase.both)."""
        (results, books, raised), calls, final, cache, fake_calls = self.both(
            rig, action or set_once(rig, *requests, choice=choice))
        return results, books, raised, calls, final, cache, fake_calls

    def failed(self, result, kind, message):
        """The rollback of a failed request, after checking its exception."""
        self.assertEqual(result[:4], ("ClientPowerError", kind, message, None), result)
        return result[4]


def rig_of(factory, configure=None, **kw):
    kw.setdefault("recapture", True)
    return SetRig(factory, configure=configure, **kw)


def modern(configure=None, **kw):
    return rig_of(FakeGpu, configure, accepted=MODERN, **kw)


def set_payloads(fake_calls, command=cp.SET):
    return [request for c, request in fake_calls if c == command]


def recaptured(rig):
    """The transport cache after a compensation captured it again (the layout choice forgotten)."""
    return (rig.header, rig.fields), False


# ---- the request refused before anything is read ------------------------------------------------

class RefusalTests(SetCase):
    def test_a_bad_request_reads_and_writes_nothing(self):
        for wanted in (None, True, False, "361000", 361000.0, 0, -1, 0xFFFFFFFF, 0x100000000, -(1 << 70)):
            with self.subTest(wanted=wanted):
                rig = modern()
                results, books, _, calls, final, cache, fake_calls = self.run_set(rig, wanted)
                self.assertEqual(results, [("ClientPowerError", "invalid", INVALID, None, None)])
                self.assertEqual((calls, fake_calls), ([], []))
                self.assertEqual(cache, ((rig.header, rig.fields), True))

    def test_an_int_subclass_is_a_bad_request(self):
        class Watts(int):
            pass
        results, *_ = self.run_set(modern(), Watts(361000))
        self.assertEqual(results, [("ClientPowerError", "invalid", INVALID, None, None)])


MALFORMED, UNSUPPORTED = "malformed", "unsupported"
NATIVE_ROW = cp.NATIVE_CONTROL_BASE + 31 * cp.NATIVE_CONTROL_STRIDE
CLIENT_ROW = cp.INFO_BASE + 37 * cp.INFO_STRIDE


def setitem(offset, value):
    return lambda raw: raw.__setitem__(offset, value)


def pack(offset, value):
    return lambda raw: put(raw, offset, value)


# test_client_power_runtime.test_mask_type_unit_binding_and_geometry_faults_prevent_set
PRE_SET_FAULTS = [
    (cp.INFO, setitem(12, 1), MALFORMED),
    (cp.INFO, setitem(CLIENT_ROW + 1, 99), MALFORMED),
    (cp.INFO, setitem(CLIENT_ROW + 3, 1), UNSUPPORTED),
    (cp.INFO, setitem(CLIENT_ROW + 2, 7), UNSUPPORTED),
    (cp.INFO, pack(CLIENT_ROW + 4, 600000), MALFORMED),
    (cp.CONTROL, setitem(0, 8), MALFORMED),
    (cp.CONTROL, setitem(4, 2), MALFORMED),
    (cp.CONTROL, setitem(FE - 4, 7), MALFORMED),
    (cp.CONTROL, setitem(FE + 4, 253), MALFORMED),
    (cp.ROLE_INFO, setitem(0, 2), MALFORMED),
    (cp.ROLE_INFO, setitem(30, 99), MALFORMED),
    (cp.NATIVE_INFO, pack(4, 1 << 2), MALFORMED),
    (cp.NATIVE_CONTROL, pack(16, 0), MALFORMED),
    (cp.NATIVE_CONTROL, pack(12, 0), MALFORMED),
    (cp.NATIVE_CONTROL, pack(NATIVE_ROW, 11), MALFORMED),
    (cp.NATIVE_CONTROL, pack(NATIVE_ROW + 4, 321001), "unstable"),
]


class PreSetTests(SetCase):
    """Every failure before the SET is dispatched is raised as it is, with no rollback."""

    def check_no_set(self, rig, kind, *, message=None, rm=None):
        results, books, _, _, final, cache, fake_calls = self.run_set(rig)
        result = results[0]
        self.assertEqual(result[:2], ("ClientPowerError", kind), result)
        if message is not None:
            self.assertEqual(result[2], message)
        self.assertEqual((result[3], result[4]), (rm, None))
        self.assertEqual(set_payloads(fake_calls), [])
        return result, books, final, cache, fake_calls

    def test_mask_type_unit_binding_and_geometry_faults(self):
        for command, change, kind in PRE_SET_FAULTS:
            with self.subTest(command=hex(command), kind=kind):
                self.check_no_set(modern(payload(command, 1, change)), kind)

    def test_a_missing_or_unrestorable_prior_request(self):
        for prior in (0, cp.MISSING):
            with self.subTest(prior=prior):
                rig = rig_of(lambda prior=prior: FakeGpu(requested=prior), accepted=MODERN)
                self.check_no_set(rig, UNSUPPORTED)

    def test_requests_changing_between_fresh_reads(self):
        rig = modern(faults(((cp.INFO, 2), lambda g, _: g._update(322000))))
        self.check_no_set(rig, "unstable",
                          message="Client-power definitions or requests changed between fresh reads")

    def test_a_definite_rm_refusal_keeps_its_rm_status(self):
        rig = modern(faults(((cp.ROLE_INFO, 1), {"rm": 0x56})))
        self.check_no_set(rig, UNSUPPORTED, message=rejected(0x56, cp.ROLE_INFO), rm=0x56)

    def test_a_bad_captured_transport_fails_before_any_command(self):
        rig = modern()
        stored = list(rig.header)

        def action(side):
            rig.header[0] = 0
            try:
                return set_once(rig)(side)
            finally:
                rig.header[:] = stored
        (results, *_), calls, _, _, fake_calls = self.both(rig, action)
        self.assertEqual(results[0], ("ClientPowerError", MALFORMED,
                                      "Client-power transport envelope is not understood", None, None))
        self.assertEqual(fake_calls, [])

    def test_a_failure_of_the_set_before_its_dispatch_is_not_compensated(self):
        # Two transient STATUS failures leave the transport reset and, without a getter, uncapturable:
        # the read still succeeds (STATUS is diagnostic), and the SET's own transport capture fails
        # before anything is handed to the escape.
        rig = modern(faults(((cp.NATIVE_STATUS, 1), {"rm": 0x99}), ((cp.NATIVE_STATUS, 2), {"rm": 0x98})),
                     recapture=False)
        _, _, final, cache, fake_calls = self.check_no_set(rig, "transient", message=NO_TRANSPORT)
        self.assertEqual(cache, (None, False))
        self.assertEqual(final["transport_key"], A612_KEY)
        self.assertEqual(commands(fake_calls)[-1], (cp.NATIVE_STATUS, cp.SIZES[cp.NATIVE_STATUS]))


# ---- the happy path on every model ----------------------------------------------------------------

class HappyPathTests(SetCase):
    def check(self, rig, wanted, expected_state):
        results, books, _, calls, final, cache, fake_calls = self.run_set(rig, wanted)
        self.assertEqual(results[0][0], "ok", results[0])
        got = results[0][1]
        self.assertEqual(list(got), ["before", "after"])
        self.assertEqual(got["after"]["requested_mw"], wanted)
        self.assertEqual(cp._state(got["after"]), cp._project(got["before"], wanted)
                         if not cp._coupled_requests(got["before"]) else cp._state(got["after"]))
        self.assertTrue(cp._matches(cp._project(got["before"], wanted), got["after"]))
        self.assertEqual(final, expected_state)
        self.assertEqual(cache, ((rig.header, rig.fields), True))  # nothing reset
        self.assertEqual(len(set_payloads(fake_calls, self.set_command(final))), 1)
        self.assertEqual(books["peer_writes"], [])
        return got, fake_calls

    @staticmethod
    def set_command(final):
        return 0x20802633 if final["rm26"] else cp.SET

    def test_modern_below_minimum_above_maximum_and_independent_pmu_output(self):
        def lower(fake):
            fake.lower_pmu = 180000
        for wanted in (1, 199000, 321000, 551000, 0xFFFFFFFE):
            with self.subTest(wanted=wanted):
                got, fake_calls = self.check(modern(lower), wanted, dict(STATE, transport_key=A612_KEY))
                self.assertEqual(got["before"]["requested_mw"], 321000)
                sent = set_payloads(fake_calls)[0]
                self.assertEqual(sent, bytes.fromhex(cp._project(got["before"], wanted)["control_hex"]))
                row = [r for r in got["after"]["arbitration"]["policies"] if r["policy"] == 31]
                self.assertEqual((row[0]["requested"], row[0]["effective"]), (wanted, 180000))

    def test_ordinary_limits_are_not_admission_gates(self):
        for minimum, maximum in ((0, 550000), (200000, 0xFFFFFFFF)):
            def limits(fake, minimum=minimum, maximum=maximum):
                native_at = cp.NATIVE_INFO_BASE + fake.native_index * cp.NATIVE_INFO_STRIDE
                put(fake.info, CLIENT_ROW + 4, minimum)
                put(fake.info, CLIENT_ROW + 12, maximum)
                put(fake.native_info, native_at + 8, minimum)
                put(fake.native_info, native_at + 16, maximum)
            with self.subTest(minimum=minimum, maximum=maximum):
                got, _ = self.check(modern(limits), 551000, dict(STATE, transport_key=A612_KEY))
                self.assertEqual((got["after"]["binding"]["minimum_mw"], got["after"]["binding"]["maximum_mw"]),
                                 (minimum, maximum))

    def test_an_unrelated_native_type_disagreement_is_preserved(self):
        def peer_type(fake):
            put(fake.native_control, cp.NATIVE_CONTROL_BASE + fake.native_peer * cp.NATIVE_CONTROL_STRIDE, 121)
        got, _ = self.check(modern(peer_type), 551000, dict(STATE, transport_key=A612_KEY))
        peer = cp.NATIVE_CONTROL_BASE + 2 * cp.NATIVE_CONTROL_STRIDE
        self.assertEqual(bytes.fromhex(got["after"]["native_control_hex"])[peer:peer + 4], struct.pack("<I", 121))

    def test_legacy(self):
        got, _ = self.check(rig_of(LegacyGpu, accepted=legacy_accepted()), 551000,
                            dict(STATE, sizes="LEGACY_SIZES", legacy=True, transport_key=A612_KEY))
        self.assertEqual(got["after"]["binding"]["layout"], "legacy32")

    def test_turing_policy124_and_info216(self):
        for factory, expected in (
                (TuringGpu, dict(STATE, sizes="TURING_SIZES", legacy=True, native_policy124=True,
                                 transport_key=TURING_KEY)),
                (lambda: TuringGpu(record=216), dict(STATE, sizes="INFO216_SIZES", legacy=True, role_info216=True,
                                                     transport_key=TURING_KEY))):
            with self.subTest(expected["sizes"]):
                self.check(rig_of(factory), 551000, expected)

    def test_rm26_and_rm26_extended(self):
        for factory, expected in (
                (Rm26Gpu, dict(STATE, sizes="RM26_SIZES", rm26=True, rm26_records=True, transport_key=RM26_KEY)),
                (ExtendedRm26Gpu, dict(STATE, sizes="RM26_EXTENDED_SIZES", rm26=True, rm26_records=True,
                                       rm26_extended=True, transport_key=RM26_EXTENDED_KEY))):
            with self.subTest(expected["sizes"]):
                got, _ = self.check(rig_of(factory), 1999000, expected)
                self.assertEqual(got["after"]["binding"]["layout"], "rm26")

    def test_modern_v30_accepts_derived_peer_votes(self):
        for pinned in (False, True):
            with self.subTest(pinned=pinned):
                rig = rig_of(lambda pinned=pinned: PeerCoupledGpu(pinned=pinned), accepted=COUPLED_ACCEPTED)
                got, _ = self.check(rig, 551000, dict(STATE, transport_key=A612_KEY))
                after = bytes.fromhex(got["after"]["native_control_hex"])
                for index, raised in ((2, 60000), (7, 90000)):  # the peers' recalculated votes
                    at = cp.NATIVE_CONTROL_BASE + index * cp.NATIVE_CONTROL_STRIDE + 4
                    self.assertEqual(cp._u32(after, at), raised)


STATE = {"sizes": "SIZES", "legacy": False, "native_policy124": False, "role_info216": False, "rm26": False,
         "rm26_records": False, "rm26_extended": False, "transport_key": None}


# ---- a dispatched SET that fails, compensated exactly once ----------------------------------------

def set_fault(*events, command=cp.SET):
    """configure(fake): events[n] on the (n + 1)-th SET."""
    return faults(*(((command, n), event) for n, event in enumerate(events, 1) if event is not None))


class CompensationTests(SetCase):
    def compensated(self, rig, kind, message, *, verified=True, sets=2, requested=321000, released=False):
        results, books, raised, calls, final, cache, fake_calls = self.run_set(rig)
        detail = VERIFIED if verified else UNVERIFIED
        rollback = self.failed(results[0], kind, f"{message}. {detail}")
        self.assertEqual(list(rollback), ["attempted", "verified", "error", "readback"])
        self.assertEqual((rollback["attempted"], rollback["verified"]), (True, verified))
        sent = set_payloads(fake_calls)
        self.assertEqual(len(sent), sets)
        self.assertEqual(cp._u32(sent[-1], FE), 321000)  # the replay carries the captured request
        self.assertEqual(books["requested"], requested)
        # A rollback read that fails after its bounded retry leaves the transport released; every
        # other path ends on a fresh capture taken by the reset before (or inside) the rollback.
        self.assertEqual(cache, (None, False) if released else recaptured(rig))
        return rollback, books, raised, fake_calls

    def test_an_ignored_or_failed_set_compensates_exactly_once(self):
        cases = (
            ({"ignore": True}, "readback", MISMATCH),
            ({"mutate": lambda g: g._update(550000)}, "readback", MISMATCH),
            ({"rm": 0x55}, "transient", rejected(0x55)),
            ({"nt": -4}, "transient", escape_failed(-4)),
            ({"raise": RuntimeError("partial write")}, "transient", escape_failed(STATUS_UNSUCCESSFUL)),
            ({"header": (15, 4)}, MALFORMED, ENVELOPE),
            ({"guard": "after"}, MALFORMED, GUARD),
            ({"payload": setitem(37, 4)}, MALFORMED, SET_ECHO),
        )
        for event, kind, message in cases:
            with self.subTest(event=sorted(event)):
                rollback, _, raised, _ = self.compensated(modern(set_fault(event)), kind, message)
                self.assertIsNone(rollback["error"])
                self.assertEqual(rollback["readback"]["requested_mw"], 321000)
                self.assertEqual(raised, ["partial write"] if "raise" in event else [])

    def test_an_unsupported_set_still_compensates_because_it_was_dispatched(self):
        rollback, *_ = self.compensated(modern(set_fault({"ignore": True, "rm": 0x56})), UNSUPPORTED,
                                        rejected(0x56))
        self.assertTrue(rollback["verified"])

    def test_peer_or_opaque_mutation_is_reported_unverified(self):
        surfaces = {
            "client_info": lambda g: g.info.__setitem__(-1, g.info[-1] ^ 1),
            "native_info": lambda g: g.native_info.__setitem__(-1, g.native_info[-1] ^ 1),
            "native_control": lambda g: g.native_control.__setitem__(-1, g.native_control[-1] ^ 1),
            "client_opaque": lambda g: setattr(g, "client_opaque", b"lost"),
        }
        for surface, mutate in surfaces.items():
            with self.subTest(surface):
                rollback, *_ = self.compensated(modern(set_fault({"mutate": mutate})), "readback", MISMATCH,
                                                verified=False)
                self.assertEqual(rollback["error"], f"ClientPowerError: {NOT_RESTORED}")
                self.assertIsNotNone(rollback["readback"])

    def test_a_failed_rollback_is_reported_without_a_third_set(self):
        rig = modern(set_fault({"rm": 0x55}, {"ignore": True, "rm": 0x1F}))
        rollback, *_ = self.compensated(rig, "transient", rejected(0x55), verified=False, requested=551000)
        self.assertEqual(rollback["error"], f"ClientPowerError: {rejected(0x1F)}; ClientPowerError: {NOT_RESTORED}")
        self.assertEqual(rollback["readback"]["requested_mw"], 551000)

    def test_a_failed_rollback_that_still_restores_is_not_verified(self):
        rig = modern(set_fault({"rm": 0x55}, {"rm": 0x99}))
        rollback, *_ = self.compensated(rig, "transient", rejected(0x55), verified=False)
        self.assertEqual(rollback["error"], f"ClientPowerError: {rejected(0x99)}")
        self.assertEqual(rollback["readback"]["requested_mw"], 321000)

    def test_a_transient_verification_get_recovers_without_repeating_the_set(self):
        rig = modern(faults(((cp.INFO, 3), {"nt": -1})))
        results, books, _, _, _, cache, fake_calls = self.run_set(rig)
        self.assertEqual(results[0][0], "ok", results[0])
        self.assertEqual(results[0][1]["after"]["requested_mw"], 551000)
        self.assertEqual((len(set_payloads(fake_calls)), books["counts"][cp.INFO]), (1, 5))
        self.assertEqual(cache, recaptured(rig))  # the stable read's own reset

    def test_a_failed_verification_get_compensates_once_and_reads_the_restoration(self):
        rig = modern(faults(((cp.INFO, 3), {"nt": -1}), ((cp.INFO, 4), {"nt": -1})))
        rollback, *_ = self.compensated(rig, "transient", escape_failed(-1))
        self.assertEqual(rollback["readback"]["requested_mw"], 321000)
        self.assertIsNone(rollback["error"])

    def test_a_transient_rollback_read_recovers_without_another_compensation(self):
        rig = modern(faults(((cp.SET, 1), {"rm": 0x55}), ((cp.INFO, 3), {"nt": -1})))
        rollback, *_ = self.compensated(rig, "transient", rejected(0x55))
        self.assertIsNone(rollback["error"])

    def test_a_failed_rollback_read_leaves_no_readback(self):
        rig = modern(faults(((cp.SET, 1), {"rm": 0x55}), ((cp.INFO, 3), {"nt": -1}), ((cp.INFO, 4), {"nt": -2})))
        rollback, *_ = self.compensated(rig, "transient", rejected(0x55), verified=False, released=True)
        self.assertEqual(rollback["error"], f"ClientPowerError: {escape_failed(-2)}")
        self.assertIsNone(rollback["readback"])

    def test_every_model_compensates(self):
        cases = (
            (rig_of(LegacyGpu, set_fault({"rm": 0x55}), accepted=legacy_accepted()), cp.SET),
            (rig_of(TuringGpu, faults(((cp.SET, 392, 1), {"rm": 0x55}))), cp.SET),
            (rig_of(Rm26Gpu, set_fault({"rm": 0x55}, command=0x20802633)), 0x20802633),
            (rig_of(ExtendedRm26Gpu, set_fault({"rm": 0x55}, command=0x20802633)), 0x20802633),
        )
        for rig, command in cases:
            with self.subTest(rig.factory.__name__):
                results, books, _, _, _, cache, fake_calls = self.run_set(rig)
                result = results[0]
                self.assertEqual(result[:4], ("ClientPowerError", "transient", f"{rejected(0x55, command)}. {VERIFIED}",
                                              None), result)
                self.assertEqual((result[4]["attempted"], result[4]["verified"]), (True, True))
                self.assertEqual(len(set_payloads(fake_calls, command)), 2)
                self.assertEqual(cache, recaptured(rig))


# ---- the target changing after the SET ------------------------------------------------------------

class IdentityTests(SetCase):
    def test_a_changed_transport_after_the_set_is_never_written(self):
        # The SET is refused and changes the driver's power GET: the compensation's fresh capture has
        # another key, which _transport refuses as an identity change before anything is dispatched.
        rig = modern()
        stored = list(rig.header)

        def change(_fake):
            rig.header[2], rig.header[15] = 11748, 11680
        rig.configure = set_fault({"rm": 0x55, "mutate": change})

        def action(side):
            try:
                return set_once(rig)(side)
            finally:
                rig.header[:] = stored
        (results, books, _), _, final, cache, fake_calls = self.both(rig, action)
        rollback = self.failed(results[0], "transient", f"{rejected(0x55)}. {UNVERIFIED}")
        self.assertEqual(rollback, {"attempted": False, "verified": False,
                                    "error": f"ClientPowerError: {CHANGED_TRANSPORT}", "readback": None})
        self.assertEqual(len(set_payloads(fake_calls)), 1)
        self.assertEqual(final["transport_key"], A612_KEY)
        changed = list(stored)
        changed[2], changed[15] = 11748, 11680
        self.assertEqual(cache, ((changed, rig.fields), False))
        self.assertEqual(books["requested"], 551000)  # the refused SET was applied; nothing undid it

    def test_an_identity_failure_reading_back_is_compensated_with_its_kind(self):
        # The read-back's transient INFO resets the transport; its fresh capture has another key.
        rig = modern()
        stored = list(rig.header)

        def change(_fake):
            rig.header[2], rig.header[15] = 11748, 11680
        rig.configure = faults(((cp.INFO, 3), {"nt": -1, "mutate": change}))

        def action(side):
            try:
                return set_once(rig)(side)
            finally:
                rig.header[:] = stored
        (results, books, _), *_ = self.both(rig, action)
        rollback = self.failed(results[0], "identity", f"{CHANGED_TRANSPORT}. {UNVERIFIED}")
        self.assertEqual(rollback, {"attempted": False, "verified": False,
                                    "error": f"ClientPowerError: {CHANGED_TRANSPORT}", "readback": None})


# ---- the v30 policy table (tests/test_client_power_driver_compat.py) ---------------------------------

def v30(client=37, native=31, slot="0000:72:1d.3", device=0xABCD, subsystem=0x87654321):
    return lambda: modern_v30_gpu(client=client, native=native, slot=slot, device=device, subsystem=subsystem)


class V30Tests(SetCase):
    def test_a_v30_definition_writes_and_restores_the_exact_state(self):
        for client, native_index in ((37, 31), (63, 29), (0, 2)):
            for requested in (550001, 1999000):
                with self.subTest(client=client, native=native_index, requested=requested):
                    rig = rig_of(v30(client, native_index), accepted=MODERN)
                    results, books, _, _, _, cache, fake_calls = self.run_set(rig, requested, 321000)
                    first, second = results
                    self.assertEqual((first[0], second[0]), ("ok", "ok"), results)
                    self.assertEqual(first[1]["after"]["requested_mw"], requested)
                    self.assertEqual(cp._state(second[1]["after"]), cp._state(first[1]["before"]))
                    self.assertEqual(len(set_payloads(fake_calls)), 2)
                    self.assertEqual(cache, ((rig.header, rig.fields), True))

    def test_a_failed_v30_write_restores_opaque_bytes_and_requests(self):
        for event, kind, message in (({"rm": 0x55}, "transient", rejected(0x55)),
                                     ({"nt": -1}, "transient", escape_failed(-1)),
                                     ({"ignore": True}, "readback", MISMATCH)):
            with self.subTest(event=event):
                rig = rig_of(v30(63, 29), set_fault(event), accepted=MODERN)
                results, books, *_ = self.run_set(rig)
                rollback = self.failed(results[0], kind, f"{message}. {VERIFIED}")
                self.assertEqual((rollback["attempted"], rollback["verified"], rollback["error"]), (True, True, None))
                self.assertEqual(books["requested"], 321000)

    def test_metadata_validation_comes_before_the_set(self):
        native_at = cp.NATIVE_INFO_BASE + 31 * cp.NATIVE_INFO_STRIDE

        def corrupt(name):
            def configure(fake):
                if name == "unknown_table_version":
                    fake.native_info[native_at] = 0x31
                elif name == "client_units":
                    fake.info[CLIENT_ROW + 3] = 1
                elif name == "native_units":
                    fake.native_info[native_at + 6] = 1
                elif name == "unordered_client_bounds":
                    put(fake.info, CLIENT_ROW + 4, 400000)
                elif name == "native_bounds_differ":
                    put(fake.native_info, native_at + 16, 550001)
                elif name == "role_selector":
                    fake.faults[cp.ROLE_INFO, 1] = {"payload": lambda raw: put(raw, 0, 2)}
                elif name == "role_definition":
                    fake.faults[cp.ROLE_INFO, 1] = {"payload": lambda raw: raw.__setitem__(204, 0xD7)}
                elif name == "unoccupied_native_target":
                    put(fake.native_info, 4, 1 << fake.native_peer)
                elif name == "duplicate_native_definition":
                    peer_at = cp.NATIVE_INFO_BASE + fake.native_peer * cp.NATIVE_INFO_STRIDE
                    fake.native_info[peer_at:peer_at + 260] = fake.native_info[native_at:native_at + 260]
            return configure
        for name, kind in (("unknown_table_version", MALFORMED), ("client_units", UNSUPPORTED),
                           ("native_units", MALFORMED), ("unordered_client_bounds", MALFORMED),
                           ("native_bounds_differ", MALFORMED), ("role_selector", MALFORMED),
                           ("role_definition", MALFORMED), ("unoccupied_native_target", MALFORMED),
                           ("duplicate_native_definition", MALFORMED)):
            with self.subTest(name):
                results, books, _, _, _, _, fake_calls = self.run_set(rig_of(v30(), corrupt(name), accepted=MODERN))
                self.assertEqual(results[0][:2], ("ClientPowerError", kind), results[0])
                self.assertIsNone(results[0][4])
                self.assertEqual(set_payloads(fake_calls), [])
                self.assertEqual(books["requested"], 321000)


def coupled(configure=None, *, pinned=False, **kw):
    return rig_of(lambda: PeerCoupledGpu(pinned=pinned), configure, accepted=COUPLED_ACCEPTED, **kw)


class PeerRestorationTests(SetCase):
    """The v30 peer restoration with the policy writer (tests/test_client_power_driver_compat.py)."""

    def test_a_failed_board_write_restores_preexisting_peer_pins(self):
        for event, kind, message in (({"rm": 0x55}, "transient", rejected(0x55)),
                                     ({"nt": -1}, "transient", escape_failed(-1)),
                                     ({"ignore": True}, "readback", MISMATCH)):
            with self.subTest(event=event):
                rig = coupled(set_fault(event), pinned=True)
                results, books, _, _, final, cache, fake_calls = self.run_set(rig)
                rollback = self.failed(results[0], kind, f"{message}. {VERIFIED}")
                self.assertEqual((rollback["attempted"], rollback["verified"], rollback["error"]), (True, True, None))
                # the writer restored each pinned peer, in index order, once
                self.assertEqual(books["peer_writes"], [(2, 51000), (7, 81000)])
                self.assertEqual(len(set_payloads(fake_calls)), 2)
                self.assertEqual(len(set_payloads(fake_calls, POLICY_SET)), 2)
                readback = bytes.fromhex(rollback["readback"]["native_control_hex"])
                for index, pinned in ((2, 51000), (7, 81000)):
                    at = cp.NATIVE_CONTROL_BASE + index * cp.NATIVE_CONTROL_STRIDE + 4
                    self.assertEqual(cp._u32(readback, at), pinned)
                self.assertEqual(books["pins"], {})  # the writer pins nothing
                self.assertIsNone(books["board_policy"])
                self.assertEqual(books["policy_mask"], (1 << 29) | (1 << 7) | (1 << 2))
                self.assertIsNotNone(books["table"])
                # the writer's layout choice stays; nothing reset it after the compensation's capture
                self.assertEqual(cache[0], (rig.header, rig.fields))

    def test_unpinned_peers_need_no_writer(self):
        rig = coupled(set_fault({"rm": 0x55}))
        results, books, *_ = self.run_set(rig)
        rollback = self.failed(results[0], "transient", f"{rejected(0x55)}. {VERIFIED}")
        self.assertTrue(rollback["verified"])
        self.assertEqual((books["peer_writes"], books["table"]), ([], None))

    def test_a_failed_peer_write_is_reported_unverified(self):
        # The first peer's policy SET is refused; the writer replays the original record (a second
        # policy SET) and reports. The rollback stops at that peer.
        rig = coupled(faults(((cp.SET, 1), {"rm": 0x55}), ((POLICY_SET, 1), {"rm": 0x1})), pinned=True)
        results, books, *_ = self.run_set(rig)
        rollback = self.failed(results[0], "transient", f"{rejected(0x55)}. {UNVERIFIED}")
        self.assertEqual(rollback["error"],
                         "ClientPowerError: Policy 2 restoration failed: policy 2: policy request failed "
                         "(NTSTATUS 0, RM 0x1); original limit restored and verified")
        self.assertEqual(books["peer_writes"], [(2, 51000), (2, 50000)])
        # the readback is the first one: the second read is never reached
        readback = bytes.fromhex(rollback["readback"]["native_control_hex"])
        self.assertEqual(cp._u32(readback, cp.NATIVE_CONTROL_BASE + 2 * cp.NATIVE_CONTROL_STRIDE + 4), 50000)

    def test_an_ignored_peer_write_is_reported_unverified(self):
        rig = coupled(faults(((cp.SET, 1), {"rm": 0x55}), ((POLICY_SET, 1), {"ignore": True})), pinned=True)
        results, books, *_ = self.run_set(rig)
        rollback = self.failed(results[0], "transient", f"{rejected(0x55)}. {UNVERIFIED}")
        self.assertTrue(rollback["error"].startswith(
            "ClientPowerError: Policy 2 restoration failed: policy 2: control read-back disagrees; "), rollback)

    def test_a_failed_second_read_keeps_the_first_readback(self):
        # INFO 1/2 before, 3/4 the compensation's first read, 5/6 the read after the peer writes.
        rig = coupled(faults(((cp.SET, 1), {"rm": 0x55}), ((cp.INFO, 5), {"nt": -1}), ((cp.INFO, 6), {"nt": -2})),
                      pinned=True)
        results, books, *_ = self.run_set(rig)
        rollback = self.failed(results[0], "transient", f"{rejected(0x55)}. {UNVERIFIED}")
        self.assertEqual(rollback["error"], f"ClientPowerError: {escape_failed(-2)}")
        self.assertEqual(books["peer_writes"], [(2, 51000), (7, 81000)])
        readback = bytes.fromhex(rollback["readback"]["native_control_hex"])
        self.assertEqual(cp._u32(readback, cp.NATIVE_CONTROL_BASE + 2 * cp.NATIVE_CONTROL_STRIDE + 4), 50000)

    def test_a_recalculated_unknown_peer_is_rejected_and_compensated(self):
        info_at = cp.NATIVE_INFO_BASE + 2 * cp.NATIVE_INFO_STRIDE
        control_at = cp.NATIVE_CONTROL_BASE + 2 * cp.NATIVE_CONTROL_STRIDE
        corruptions = {
            "units": lambda f: f.native_info.__setitem__(info_at + 6, 9),
            "table_version": lambda f: f.native_info.__setitem__(info_at, 0x31),
            "type": lambda f: put(f.native_control, control_at, 0xFE),
            "bounds": lambda f: put(f.native_info, info_at + 16, 0),
        }
        for name, corrupt in corruptions.items():
            with self.subTest(name):
                results, books, *_ = self.run_set(coupled(corrupt))
                rollback = self.failed(results[0], "readback", f"{MISMATCH}. {VERIFIED}")
                self.assertTrue(rollback["verified"])
                self.assertEqual(books["peer_writes"], [])

    def test_opaque_changes_fail_without_claiming_complete_restoration(self):
        for name, offset in (("peer_opaque", cp.NATIVE_CONTROL_BASE + 2 * cp.NATIVE_CONTROL_STRIDE + 13),
                             ("selected_opaque", cp.NATIVE_CONTROL_BASE + 29 * cp.NATIVE_CONTROL_STRIDE + 13),
                             ("tail", -1)):
            def flip(fake, offset=offset):
                fake.native_control[offset] ^= 1
            with self.subTest(name):
                results, books, *_ = self.run_set(coupled(set_fault({"mutate": flip})))
                rollback = self.failed(results[0], "readback", f"{MISMATCH}. {UNVERIFIED}")
                self.assertEqual(rollback["error"], f"ClientPowerError: {NOT_RESTORED}")
                self.assertEqual(books["peer_writes"], [])
                self.assertEqual(books["requested"], 321000)

    def test_a_v40_definition_does_not_inherit_v30_peer_permission(self):
        def v40(fake):
            fake.native_info[cp.NATIVE_INFO_BASE + fake.native_index * cp.NATIVE_INFO_STRIDE] = 0x40
        results, books, *_ = self.run_set(coupled(v40))
        rollback = self.failed(results[0], "readback", f"{MISMATCH}. {VERIFIED}")
        self.assertTrue(rollback["verified"])
        self.assertEqual(books["peer_writes"], [])


# ---- the C ABI ----------------------------------------------------------------------------------

PEER_IDENTITY = "GPU binding changed before peer restoration"
READER_CHANGED = "GPU binding changed; create a fresh PERF reader"


class PeerLookupTests(SetCase):
    """The target lookup _rollback makes before each peer write, made to change or fail before the
    SECOND of two peer writes (pinned peers 2 and 7 on the v30 coupled rig). The Python side patches
    the controller's _binding on that call (the first after one peer write); the native side arms the
    test seam for the same call number, which the Python run found."""

    def lookup(self, mode):
        rig = coupled(set_fault({"rm": 0x55}), pinned=True)
        found = {}

        def action(side):
            store(side, rig, True)
            if isinstance(side, MetaPython):
                controller, original, calls = side.controller, side.controller._binding, []

                def binding():
                    calls.append(len(rig.fake.peer_writes))
                    if len(rig.fake.peer_writes) == 1 and "nth" not in found:
                        found["nth"] = len(calls)
                        if mode == "fail":  # _binding's conversion of the reader's PerfDiagnosticError
                            raise cp.ClientPowerError("identity", READER_CHANGED)
                        identity = dict(original())
                        identity["subsystem_id"] ^= 1
                        return identity
                    return original()
                with patch.object(controller, "_binding", binding):
                    results = [set_outcome(lambda: set_request(side, 551000))]
                found["python_calls"] = len(calls)
            else:
                side.gpu._cp_test_lookup(found["nth"], mode)
                results = [set_outcome(lambda: set_request(side, 551000))]
            return results, bookkeeping(side, rig.fake), list(rig.raised)
        results, books, _, _, _, _, fake_calls = self.run_set(rig, action=action)
        rollback = self.failed(results[0], "transient", f"{rejected(0x55)}. {UNVERIFIED}")
        # exactly one peer write, then nothing: no second policy write and no second read-back
        self.assertEqual(books["peer_writes"], [(2, 51000)])
        self.assertEqual(len(set_payloads(fake_calls, POLICY_SET)), 1)
        self.assertEqual(found["python_calls"], found["nth"])  # the lookup that failed was the last one
        self.assertEqual(books["counts"][cp.INFO], 4)  # INFO 1/2 before the SET, 3/4 the first read-back
        self.assertEqual((rollback["attempted"], rollback["verified"]), (True, False))
        readback = bytes.fromhex(rollback["readback"]["native_control_hex"])  # the first read-back kept
        self.assertEqual(cp._u32(readback, cp.NATIVE_CONTROL_BASE + 2 * cp.NATIVE_CONTROL_STRIDE + 4), 50000)
        return rollback

    def test_a_changed_identity_before_a_peer_write_stops_the_restoration(self):
        rollback = self.lookup("changed")
        self.assertEqual(rollback["error"], f"ClientPowerError: {PEER_IDENTITY}")

    def test_a_failed_lookup_before_a_peer_write_stops_the_restoration(self):
        rollback = self.lookup("fail")
        self.assertEqual(rollback["error"], f"ClientPowerError: {READER_CHANGED}")

    def test_the_seam_is_inert_unless_armed_and_disarms(self):
        lib = native.load()
        self.assertEqual(lib.dc_hook_test_cpc_binding(None, 1, 1), native.DC_E_ARGUMENT)
        rig = coupled(set_fault({"rm": 0x55}), pinned=True)

        def action(side):
            store(side, rig, True)
            if not isinstance(side, MetaPython):
                for mode in (-1, 3):
                    self.assertEqual(lib.dc_hook_test_cpc_binding(side.gpu._handle, 1, mode), native.DC_E_ARGUMENT)
                side.gpu._cp_test_lookup(1, "fail")
                side.gpu._cp_test_lookup(0, None)  # disarmed: the full restoration runs
            results = [set_outcome(lambda: set_request(side, 551000))]
            return results, bookkeeping(side, rig.fake), list(rig.raised)
        results, books, *_ = self.run_set(rig, action=action)
        rollback = self.failed(results[0], "transient", f"{rejected(0x55)}. {VERIFIED}")
        self.assertEqual((rollback["verified"], books["peer_writes"]), (True, [(2, 51000), (7, 81000)]))


class RollbackTextTests(SetCase):
    """dc_cpc_rollback_error_get on the text a real rollback left, and the binding's fetch of a long
    text under self._lock."""

    def test_the_real_getter_returns_the_kept_text_until_the_next_request(self):
        # 1: SET refused, its restoring SET refused too (a rollback error); 2: succeeds; 3: SET refused,
        # its restoring SET ignored and refused (another rollback error).
        rig = modern(set_fault({"rm": 0x55}, {"rm": 0x99}, None, {"rm": 0x56}, {"ignore": True, "rm": 0x1F}))
        lib, probes = native.load(), []

        def fetch(gpu):
            needed = ctypes.c_size_t()
            status = lib.dc_cpc_rollback_error_get(gpu._handle, None, 0, ctypes.byref(needed))
            full = ctypes.create_string_buffer(needed.value)
            self.assertEqual(lib.dc_cpc_rollback_error_get(gpu._handle, full, len(full), ctypes.byref(needed)),
                             native.DC_OK)
            short = ctypes.create_string_buffer(b"\xaa" * max(needed.value - 1, 1), max(needed.value - 1, 1))
            before = bytes(short)
            short_status = (lib.dc_cpc_rollback_error_get(gpu._handle, short, needed.value - 1, ctypes.byref(needed))
                            if needed.value > 1 else None)
            return status, needed.value, full.value.decode(), short_status, bytes(short) == before

        def action(side):
            store(side, rig, True)
            results = []
            for mw in (551000, 551000, 321000):
                results.append(set_outcome(lambda mw=mw: set_request(side, mw)))
                if not isinstance(side, MetaPython):
                    probes.append(fetch(side.gpu))
            return results, bookkeeping(side, rig.fake), list(rig.raised)
        results, *_ = self.run_set(rig, action=action)
        first, second, third = results
        self.assertEqual(second[0], "ok", second)
        texts = [first[4]["error"], "", third[4]["error"]]
        self.assertEqual(texts[0], f"ClientPowerError: {rejected(0x99)}")
        self.assertEqual(texts[2], f"ClientPowerError: {rejected(0x1F)}; ClientPowerError: {NOT_RESTORED}")
        for (status, needed, text, short_status, untouched), expected in zip(probes, texts):
            with self.subTest(expected=expected):
                # capacity 0 never holds the NUL: DC_E_BUFFER with the size, then the whole text
                self.assertEqual((status, needed, text), (native.DC_E_BUFFER, len(expected) + 1, expected))
                if expected:  # one byte short: refused, nothing written
                    self.assertEqual((short_status, untouched), (native.DC_E_BUFFER, True))

    def test_a_long_text_is_fetched_under_the_lock_and_again_when_it_grows(self):
        # The inline room lowered to 0, so the binding fetches even this short text; the kept text
        # outgrows the buffer once (as another call replacing it would make it): the fetch runs again
        # with the larger size, every fetch with self._lock held, and the request is not repeated.
        rig = modern(set_fault({"rm": 0x55}, {"rm": 0x99}))
        library, fetches = native.load(), []
        real = library.dc_cpc_rollback_error_get

        def growing(handle, out, capacity, needed):
            held = []

            def probe():
                acquired = self.gpu_lock.acquire(blocking=False)
                if acquired:
                    self.gpu_lock.release()
                held.append(not acquired)
            thread = threading.Thread(target=probe)
            thread.start()
            thread.join()
            status = real(handle, out, capacity, needed)
            fetches.append((capacity, needed._obj.value, held[0]))
            if len(fetches) == 1:
                needed._obj.value += 100
                return native.DC_E_BUFFER
            return status

        def action(side):
            store(side, rig, True)
            if not isinstance(side, MetaPython):
                side.gpu._CP_ROLLBACK_INLINE = 0
                self.gpu_lock = side.gpu._lock
                with patch.object(library, "dc_cpc_rollback_error_get", growing):
                    results = [set_outcome(lambda: set_request(side, 551000))]
            else:
                results = [set_outcome(lambda: set_request(side, 551000))]
            return results, bookkeeping(side, rig.fake), list(rig.raised)
        results, books, _, _, _, _, fake_calls = self.run_set(rig, action=action)
        text = results[0][4]["error"]
        self.assertEqual(text, f"ClientPowerError: {rejected(0x99)}")
        self.assertEqual([capacity for capacity, _, _ in fetches], [len(text) + 1, len(text) + 101])
        self.assertEqual([held for _, _, held in fetches], [True, True])
        self.assertEqual(len(set_payloads(fake_calls)), 2)  # the SET and its one compensation


class AbiTests(unittest.TestCase):
    def tearDown(self):
        native.set_export_resolver(None)

    def open(self):
        rig = SetRig(FakeGpu, accepted=MODERN)
        rig.fresh()
        install(rig.card.nvml, rig.card.nvapi, rig.gdi())
        rig.calls = []
        return rig, native.Gpu()

    def result(self):
        r = native.Gpu._cp_set_result_type()()
        r.size = ctypes.sizeof(r)
        for o in (r.before, r.after, r.rollback.readback):
            o.size, o.metadata.size = ctypes.sizeof(o), ctypes.sizeof(native.Gpu._CpcMetadata)
            o.arbitration.size = ctypes.sizeof(o.arbitration)
        r.rollback.size = ctypes.sizeof(r.rollback)
        return r

    def test_arguments(self):
        lib = native.load()
        rig, gpu = self.open()
        try:
            r, error = self.result(), native._Error()
            error.size, error.kind = ctypes.sizeof(error), 99

            def call(handle=gpu._handle, mw=551000, out=ctypes.addressof(r), err=ctypes.byref(error)):
                return lib.dc_cpc_set_request(handle, mw, out, err)
            sentinel = bytes(r)
            self.assertEqual(call(handle=None), native.DC_E_ARGUMENT)
            self.assertEqual(call(out=None), native.DC_E_ARGUMENT)
            for field in ("size", "before.size", "after.metadata.size", "rollback.size", "rollback.readback.size",
                          "rollback.readback.arbitration.size"):
                with self.subTest(field):
                    *path, name = field.split(".")
                    target = r
                    for part in path:
                        target = getattr(target, part)
                    good = getattr(target, name)
                    setattr(target, name, good + 4)
                    self.assertEqual(call(), native.DC_E_ARGUMENT)
                    setattr(target, name, good)
            self.assertEqual(bytes(r), sentinel)
            error.size = 0
            self.assertEqual(call(), native.DC_E_ARGUMENT)
            self.assertEqual((error.kind, bytes(r)), (99, sentinel))
            self.assertEqual((rig.calls, gpu._cp_state()["transport_key"]), ([], None))
            # the core's own range check, before any read
            error.size = ctypes.sizeof(error)
            for mw in (0, -1, 0xFFFFFFFF, 1 << 40, -(1 << 63)):
                with self.subTest(mw=mw):
                    self.assertEqual(call(mw=mw), native.DC_E_FAILED)
                    self.assertEqual((error.kind, error.rm_status, error.message.decode()),
                                     (native.DC_ERR_INVALID, 0, INVALID))
                    self.assertEqual(r.has_rollback, 0)
            self.assertEqual(rig.calls, [])
            # A NULL error is allowed: the failure (no transport) is DC_E_FAILED.
            self.assertEqual(call(err=None), native.DC_E_FAILED)
        finally:
            gpu.close()

    def test_the_rollback_error_getter(self):
        lib = native.load()
        rig, gpu = self.open()
        try:
            needed = ctypes.c_size_t()
            self.assertEqual(lib.dc_cpc_rollback_error_get(None, None, 0, ctypes.byref(needed)), native.DC_E_ARGUMENT)
            self.assertEqual(lib.dc_cpc_rollback_error_get(gpu._handle, None, 4, ctypes.byref(needed)),
                             native.DC_E_ARGUMENT)
            self.assertEqual(lib.dc_cpc_rollback_error_get(gpu._handle, None, 0, ctypes.byref(needed)),
                             native.DC_E_BUFFER)
            self.assertEqual(needed.value, 1)  # nothing yet: ""
            buffer = ctypes.create_string_buffer(1)
            self.assertEqual(lib.dc_cpc_rollback_error_get(gpu._handle, buffer, 1, ctypes.byref(needed)), native.DC_OK)
            self.assertEqual(buffer.value, b"")
        finally:
            gpu.close()

    def test_a_refused_call_never_shows_a_previous_rollback(self):
        # *out is cleared before anything runs: a struct still holding a rollback and its text comes
        # back empty from a call the core refuses for mw = 0.
        lib = native.load()
        rig, gpu = self.open()
        try:
            r, error = self.result(), native._Error()
            error.size = ctypes.sizeof(error)
            r.has_rollback, r.rollback.attempted, r.rollback.has_error, r.rollback.has_readback = 1, 1, 1, 1
            r.rollback.error, r.rollback.error_length = b"ClientPowerError: stale", 23
            r.before.requested_mw = r.after.requested_mw = r.rollback.readback.requested_mw = 551000
            self.assertEqual(lib.dc_cpc_set_request(gpu._handle, 0, ctypes.addressof(r), ctypes.byref(error)),
                             native.DC_E_FAILED)
            self.assertEqual(error.message.decode(), INVALID)
            self.assertEqual((r.has_rollback, r.rollback.attempted, r.rollback.has_error, r.rollback.has_readback,
                              r.rollback.error_length, r.rollback.error), (0, 0, 0, 0, 0, b""))
            self.assertEqual((r.before.requested_mw, r.after.requested_mw, r.rollback.readback.requested_mw), (0, 0, 0))
            self.assertEqual((r.size, r.rollback.size, r.rollback.readback.size), (ctypes.sizeof(r),
                             ctypes.sizeof(r.rollback), ctypes.sizeof(r.rollback.readback)))
        finally:
            gpu.close()

    def test_a_long_rollback_error_is_fetched_whole(self):
        # The binding reads the getter when error_length does not fit the struct's text.
        gpu = native.Gpu.__new__(native.Gpu)
        gpu._handle = None
        text = "ClientPowerError: " + "x" * 5000

        def getter(handle, out, capacity, needed):
            needed._obj.value = len(text) + 1
            ctypes.memmove(out, text.encode() + b"\0", len(text) + 1)
            return native.DC_OK
        rollback = SimpleNamespace(error_length=len(text), error=text[:4095].encode())
        with patch.object(native, "load", return_value=SimpleNamespace(dc_cpc_rollback_error_get=getter)):
            self.assertEqual(gpu._cp_rollback_error(rollback), text)

    def test_a_buffer_refusal_is_a_client_power_error(self):
        gpu = native.Gpu.__new__(native.Gpu)
        gpu._handle, gpu._lock = None, threading.RLock()
        stub = SimpleNamespace(dc_cpc_set_request=Mock(return_value=native.DC_E_BUFFER))
        with patch.object(native, "load", return_value=stub):
            with self.assertRaises(cp.ClientPowerError) as caught:
                gpu._cp_set_request(551000)
        self.assertEqual((caught.exception.kind, caught.exception.rollback, caught.exception.rm_status),
                         ("malformed", None, None))
        self.assertIn("DC_E_BUFFER", str(caught.exception))

    def test_the_struct_layout(self):
        r = native.Gpu._cp_set_result_type()
        fields = dict(r._fields_)
        self.assertIs(fields["before"], native.Gpu._cp_observation_type())
        rollback = fields["rollback"]
        self.assertEqual([name for name, _ in rollback._fields_],
                         ["size", "attempted", "verified", "has_error", "error_length", "error", "has_readback",
                          "readback"])
        self.assertEqual(dict(rollback._fields_)["error"].size if hasattr(dict(rollback._fields_)["error"], "size")
                         else ctypes.sizeof(dict(rollback._fields_)["error"]), 4096)


if __name__ == "__main__":
    unittest.main()
