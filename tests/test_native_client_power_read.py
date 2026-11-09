# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native client-power controller, increment A5 (the full read) against
client_power_runtime.ClientPowerController._once, _arbitration, _read_locked and read.

native.Gpu._cp_read runs dc_cpc_read. The Python original runs the real
ClientPowerController(twin).read() on the nvbackend.GPU twin. Both sides run on the metadata rig of
tests/test_native_client_power_metadata.py (MetaRig: one of the Python tests' own wire models as the
responder of the fake gdi32 escape, each side with a fresh fake), and every case compares the read's
dict (key order included, nested dicts too) or the exception (kind, message, rm_status), the gdi
call log (the escape fields and packet bytes), the controller state, the transport cache and the
fake's own command log.

The models: FakeGpu (modern, with the NATIVE_STATUS packet it serves), ModernCoupledGpu (modern
buffers with a v30 definition), LegacyGpu, TuringGpu with the 228-byte record (policy124) and the
216-byte record (INFO216), Rm26Gpu, ExtendedRm26Gpu, and the metadata rig's CompactGpu (which
serves no CONTROL). The legacy and RM26-record groups never send STATUS. payload() is the rig's:
it changes a response after the model has answered, so it applies on every model alike.

LockTests checks the GPU lock read() holds on both sides: a second thread's read_metadata waits
until the first thread's read, blocked inside an escape, has returned.

Not reachable natively or through these fakes, and so covered only by the Python tests:
  - the target identity changing during a capture ("Client-power target changed during capture"):
    the fake identity cannot change while one core call runs. An identity failure during STATUS is
    reachable through the transport key changing on the retry's fresh capture, and covered;
  - Python's slice growth, OverflowError or struct.error from a CONTROL slot or native row outside
    its packet (every size group's rows lie inside its packets; the core refuses such a geometry);
  - a bare ValueError during the STATUS exchange (the size table cannot change once the transport
    is keyed, so the payload always fits it);
  - power_arbitration.decode refusing the layout or the INFO / CONTROL lengths (_LAYOUT is fixed
    and STATUS is read only for the modern group, whose packets have exactly its sizes).
Not reachable at all: dc_cpc_read's DC_E_BUFFER (every size group's payloads fit
dc_cpc_observation); AbiTests checks the binding's answer to it with a stubbed export.
"""
import ctypes
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

from druta import client_power_runtime as cp
from druta import native, native_power_arbitration
from tests.test_client_power_driver_compat import ModernCoupledGpu
from tests.test_client_power_legacy import LegacyGpu
from tests.test_client_power_rm26 import Rm26Gpu
from tests.test_client_power_rm26_extended import ExtendedRm26Gpu
from tests.test_client_power_runtime import FakeGpu, put
from tests.test_client_power_turing import TuringGpu
from tests.test_native_client_power_controller import outcome
from tests.test_native_client_power_metadata import (
    A612_KEY, MODERN, TURING_KEY, CompactGpu, MetadataCase, MetaPython, MetaRig, commands, legacy_accepted,
    ordered, payload, rm_error, state, store, u32)
from tests.test_native_transport import install

if not native.available():
    raise unittest.SkipTest("native core not built (native/build.sh linux)")

RM26_KEY, RM26_EXTENDED_KEY = (0x20802612, 2524, 2456), (0x20802612, 3548, 3480)
UNAVAILABLE = "Power arbitration diagnostics are unavailable for this policy layout"
NO_TRANSPORT = "unsupported current-policy transport: no recognized power GET captured"
CP = cp.CONTROL_BASE + 37 * cp.CONTROL_STRIDE  # FakeGpu's client CONTROL slot
NP = cp.NATIVE_CONTROL_BASE + 31 * cp.NATIVE_CONTROL_STRIDE  # FakeGpu's native CONTROL row
READ_KEYS = ["schema", "stable_reads", "identity", "binding", "info_hex", "role_info_hex", "native_info_hex",
             "requested_mw", "control_hex", "native_control_hex", "arbitration", "arbitration_error"]
METADATA = [(cp.INFO, 0x924), (cp.ROLE_INFO, 0x108), (cp.NATIVE_INFO, 20512)]
CAPTURE = METADATA + [(cp.CONTROL, 0x328), (cp.NATIVE_CONTROL, 13876)]
STATUS = (cp.NATIVE_STATUS, 397048)


def full_read(side):
    """read() on the side: the Python controller's, or the core's."""
    if isinstance(side, MetaPython):
        return side.controller.read()
    return side.gpu._cp_read()


def read_once(rig, choice=True):
    def action(side):
        store(side, rig, choice)
        return outcome(lambda: full_read(side))
    return action


def word(offset, value):
    return lambda raw: put(raw, offset, value)


def byte(offset, value):
    return lambda raw: raw.__setitem__(offset, value)


class ReadCase(MetadataCase):
    def read(self, rig, action=None):
        return self.both(rig, action or read_once(rig))


# ---- the happy path on every model --------------------------------------------------------------

class HappyPathTests(ReadCase):
    def check(self, rig, expected_state, *, arbitration):
        result, _, final, cache, fake_calls = self.read(rig)
        self.assertEqual(result[0], "ok", result)
        got = result[1]
        self.assertEqual(list(got), READ_KEYS)
        self.assertEqual(final, expected_state)
        self.assertEqual(cache, ((rig.header, rig.fields), True))
        if arbitration:
            self.assertTrue(got["arbitration"]["available"])
            self.assertEqual(list(got["arbitration"]), ["schema", "available", "reason", "policies", "mask"])
            self.assertIsNone(got["arbitration_error"])
        else:
            self.assertEqual((got["arbitration"], got["arbitration_error"]), (None, UNAVAILABLE))
            self.assertNotIn(cp.NATIVE_STATUS, [command for command, _ in fake_calls])
        self.assertEqual(got["requested_mw"], 321000)
        return got, fake_calls

    def test_modern_with_arbitration(self):
        rig = MetaRig(FakeGpu, accepted=MODERN)
        got, fake_calls = self.check(rig, state(transport_key=A612_KEY), arbitration=True)
        self.assertEqual(commands(fake_calls), CAPTURE * 2 + [STATUS])
        self.assertEqual(got["arbitration"]["mask"], (1 << 31) | (1 << 2))
        selected = [row for row in got["arbitration"]["policies"] if row["policy"] == 31]
        self.assertEqual(selected[0]["requested"], 321000)
        # the STATUS request carries INFO's mask word and nothing else
        status_request = fake_calls[-1][1]
        self.assertEqual(status_request[4:8], bytes.fromhex(got["native_info_hex"])[4:8])
        self.assertFalse(any(status_request[:4]) or any(status_request[8:]))

    def test_modern_with_a_v30_definition(self):
        rig = MetaRig(ModernCoupledGpu, accepted=MODERN)
        got, _ = self.check(rig, state(transport_key=A612_KEY), arbitration=True)
        self.assertEqual(got["binding"]["client_index"], 63)

    def test_modern_with_arbitration_unavailable(self):
        rig = MetaRig(FakeGpu, configure=payload(cp.NATIVE_STATUS, 1, word(4, 0)), accepted=MODERN)
        result, _, final, cache, fake_calls = self.read(rig)
        self.assertEqual(result[0], "ok", result)
        self.assertEqual((result[1]["arbitration"], result[1]["arbitration_error"]),
                         (None, "The INFO, CONTROL and STATUS occupancy masks or control header disagree."))
        self.assertEqual(commands(fake_calls), CAPTURE * 2 + [STATUS])  # a decode refusal is not retried
        self.assertEqual(cache, ((rig.header, rig.fields), True))

    def test_legacy(self):
        rig = MetaRig(LegacyGpu, accepted=legacy_accepted())
        got, fake_calls = self.check(rig, state(sizes="LEGACY_SIZES", legacy=True, transport_key=A612_KEY),
                                     arbitration=False)
        self.assertEqual(commands(fake_calls)[-2:], [(cp.CONTROL, 392), (cp.NATIVE_CONTROL, 6356)])
        self.assertEqual(got["binding"]["layout"], "legacy32")

    def test_turing_policy124(self):
        rig = MetaRig(TuringGpu)
        got, fake_calls = self.check(rig, state(sizes="TURING_SIZES", legacy=True, native_policy124=True,
                                                transport_key=TURING_KEY), arbitration=False)
        self.assertEqual(commands(fake_calls)[-2:], [(cp.CONTROL, 392), (cp.NATIVE_CONTROL, 4432)])

    def test_info216(self):
        rig = MetaRig(lambda: TuringGpu(record=216))
        got, _ = self.check(rig, state(sizes="INFO216_SIZES", legacy=True, role_info216=True,
                                       transport_key=TURING_KEY), arbitration=False)
        self.assertEqual(got["binding"]["native_info_stride"], 216)

    def test_rm26(self):
        rig = MetaRig(Rm26Gpu)
        _, fake_calls = self.check(rig, state(sizes="RM26_SIZES", rm26=True, rm26_records=True,
                                              transport_key=RM26_KEY), arbitration=False)
        self.assertEqual(commands(fake_calls)[-2:], [(0x20802632, 388), (0x2080261A, 2108)])

    def test_rm26_extended(self):
        rig = MetaRig(ExtendedRm26Gpu)
        _, fake_calls = self.check(rig, state(sizes="RM26_EXTENDED_SIZES", rm26=True, rm26_records=True,
                                              rm26_extended=True, transport_key=RM26_EXTENDED_KEY),
                                   arbitration=False)
        self.assertEqual(commands(fake_calls)[-2:], [(0x20802632, 388), (0x2080261A, 2124)])

    def test_the_a612_compact_group_has_no_control(self):
        # CompactGpu answers only the metadata GETs: its CONTROL is refused for its size
        rig = MetaRig(CompactGpu)
        result, _, final, cache, fake_calls = self.read(rig)
        self.assertEqual(result, rm_error(cp.CONTROL, 0x1F))
        self.assertEqual(final, state(sizes="A612_COMPACT_SIZES", rm26_records=True, rm26_extended=True,
                                      transport_key=A612_KEY))
        self.assertEqual(cache, ((rig.header, rig.fields), True))
        self.assertEqual(commands(fake_calls)[-1], (cp.CONTROL, 388))

    def test_control_requests_carry_the_selected_row(self):
        rig = MetaRig(FakeGpu, accepted=MODERN)
        result, _, _, _, fake_calls = self.read(rig)
        info = bytes.fromhex(result[1]["info_hex"])
        control_request = fake_calls[3][1]
        expected = bytearray(0x328)
        expected[:4] = info[:4]
        expected[4:cp.INFO_BASE] = (1 << 37).to_bytes(32, "little")
        expected[CP:CP + 2] = info[cp.INFO_BASE + 37 * cp.INFO_STRIDE:][:2]
        expected[CP + 8] = cp.CLIENT
        self.assertEqual(control_request, bytes(expected))
        native_request = fake_calls[4][1]
        self.assertEqual(native_request, bytes(16) + info_mask(result[1]) + bytes(13876 - 20))

    def test_two_reads_on_one_controller(self):
        rig = MetaRig(FakeGpu, accepted=MODERN)

        def action(side):
            store(side, rig)
            return outcome(lambda: full_read(side)), outcome(lambda: full_read(side))
        (first, second), *_ = self.read(rig, action)
        self.assertEqual((first[0], second[0]), ("ok", "ok"))
        self.assertNotEqual(first[1]["arbitration"], second[1]["arbitration"])  # FakeGpu's STATUS counter


def info_mask(got):
    return bytes.fromhex(got["native_info_hex"])[4:8]


# ---- every _once refusal --------------------------------------------------------------------------

MALFORMED, UNSUPPORTED = "malformed", "unsupported"
GROUP = "Client CONTROL group or selected mask differs"
ROW_TYPE = "Client CONTROL changed the selected row type or client"
NATIVE_HEADER = "Native CONTROL header or inventory differs from INFO"
ONCE_REFUSALS = [
    ("group byte", payload(cp.CONTROL, 1, byte(0, 0x5A)), MALFORMED, GROUP),
    ("mask bit", payload(cp.CONTROL, 1, byte(4 + 37 // 8, 0)), MALFORMED, GROUP),
    ("mask, another bit", payload(cp.CONTROL, 1, byte(4, 1)), MALFORMED, GROUP),
    ("mask, last byte", payload(cp.CONTROL, 1, byte(cp.INFO_BASE - 1, 0x80)), MALFORMED, GROUP),
    ("row type", payload(cp.CONTROL, 1, byte(CP, 84)), MALFORMED, ROW_TYPE),
    ("row type discriminant", payload(cp.CONTROL, 1, byte(CP + 1, 84)), MALFORMED, ROW_TYPE),
    ("client", payload(cp.CONTROL, 1, byte(CP + 8, 0xFD)), MALFORMED, ROW_TYPE),
    ("native header", payload(cp.NATIVE_CONTROL, 1, word(12, 254)), MALFORMED, NATIVE_HEADER),
    ("native header word 0", payload(cp.NATIVE_CONTROL, 1, word(0, 1)), MALFORMED, NATIVE_HEADER),
    ("native inventory", payload(cp.NATIVE_CONTROL, 1, word(16, 1 << 31)), MALFORMED, NATIVE_HEADER),
    ("native type", payload(cp.NATIVE_CONTROL, 1, word(NP, 34)), MALFORMED,
     "Bound native policy CONTROL type differs from INFO"),
    ("native request", payload(cp.NATIVE_CONTROL, 1, word(NP + 4, 321001)), "unstable",
     "Client and native current requests disagree"),
]


class OnceRefusalTests(ReadCase):
    def test_refusals(self):
        for name, configure, kind, message in ONCE_REFUSALS:
            with self.subTest(name):
                rig = MetaRig(FakeGpu, configure=configure, accepted=MODERN)
                result, _, final, cache, fake_calls = self.read(rig)
                self.assertEqual(result, ("ClientPowerError", kind, message, None))
                self.assertEqual(final, state(transport_key=A612_KEY))
                self.assertEqual(cache, ((rig.header, rig.fields), True))  # not transient: no reset
                self.assertNotIn(STATUS, commands(fake_calls))

    def test_a_missing_or_zero_fe_request(self):
        for requested, message in ((cp.MISSING, "The selected FE client has no existing restorable request"),
                                   (0, "A zero FE input has uncharacterized removal semantics")):
            with self.subTest(requested=requested):
                rig = MetaRig(lambda requested=requested: FakeGpu(requested=requested), accepted=MODERN)
                result, _, _, _, fake_calls = self.read(rig)
                self.assertEqual(result, ("ClientPowerError", UNSUPPORTED, message, None))
                self.assertEqual(commands(fake_calls), CAPTURE[:4])  # no native CONTROL

    def test_rm26_echo_and_short_native_header(self):
        cases = (
            (Rm26Gpu, 0x20802632, word(0, 1 << 3), GROUP),
            (Rm26Gpu, 0x2080261A, word(0, 1), NATIVE_HEADER),
            (Rm26Gpu, 0x2080261A, word(4 + 31 * 52, 9), "Bound native policy CONTROL type differs from INFO"),
            (ExtendedRm26Gpu, 0x2080261A, word(12, 0), NATIVE_HEADER),
            (ExtendedRm26Gpu, 0x2080261A, word(0, 1), NATIVE_HEADER),
            (ExtendedRm26Gpu, 0x20802632, byte(4 + 11 * 12 + 8, 0xF8), ROW_TYPE),
        )
        for factory, command, change, message in cases:
            with self.subTest(factory=factory.__name__, command=hex(command), message=message):
                result, *_ = self.read(MetaRig(factory, configure=payload(command, 1, change)))
                self.assertEqual(result, ("ClientPowerError", MALFORMED, message, None))

    def test_the_short_rm26_header_ignores_the_long_header_words(self):
        # words 1..4 are the native rows' area in the short packet, not a header
        def configure(fake):
            for count in (1, 2):
                fake.faults[(0x2080261A, count)] = {"payload": word(12, 7)}
        result, *_ = self.read(MetaRig(Rm26Gpu, configure=configure))
        self.assertEqual(result[0], "ok", result)

    def test_the_modern_group_byte_is_info_s(self):
        # Only byte 0 of the group is checked against INFO; bytes 1..3 may differ.
        def configure(fake):
            for count in (1, 2):
                fake.faults[(cp.CONTROL, count)] = {"payload": byte(1, 0)}
        result, *_ = self.read(MetaRig(FakeGpu, configure=configure, accepted=MODERN))
        self.assertEqual(result[0], "ok", result)

    def test_a_transient_control_failure_retries_a_fresh_pair(self):
        for command in (cp.CONTROL, cp.NATIVE_CONTROL):
            with self.subTest(command=hex(command)):
                def configure(fake, command=command):
                    fake.faults[(command, 2)] = {"rm": 0x99}
                rig = MetaRig(FakeGpu, configure=configure, accepted=MODERN, recapture=True)
                result, _, _, cache, fake_calls = self.read(rig)
                self.assertEqual(result[0], "ok", result)
                self.assertEqual(cache, ((rig.header, rig.fields), False))
                self.assertEqual(commands(fake_calls).count((cp.INFO, 0x924)), 4)

    def test_requests_changing_between_captures_are_unstable(self):
        rig = MetaRig(FakeGpu, configure=payload(cp.NATIVE_CONTROL, 2, byte(13875, 0)), accepted=MODERN)
        result, _, _, cache, fake_calls = self.read(rig)
        self.assertEqual(result, ("ClientPowerError", "unstable",
                                  "Client-power definitions or requests changed between fresh reads", None))
        self.assertEqual(commands(fake_calls), CAPTURE * 2)
        self.assertEqual(cache, ((rig.header, rig.fields), True))


UNSTABLE = ("ClientPowerError", "unstable", "Client-power definitions or requests changed between fresh reads", None)
OPAQUE = 36  # CONTROL's client_opaque area: past the mask (which ends at 0x24), before CONTROL_BASE (0x28)
NATIVE_INFO_LAST = cp.SIZES[cp.NATIVE_INFO] - 1  # past every native INFO row FakeGpu binds


def flip(offset):
    return lambda raw: raw.__setitem__(offset, raw[offset] ^ 1)


def both_captures(command, change):
    def configure(fake):
        for count in (1, 2):
            payload(command, count, change)(fake)
    return configure


class StabilityTests(ReadCase):
    """_stable_locked's whole-observation comparison: captures that pass every _once check and
    differ in one part only."""

    def check_unstable(self, configure):
        rig = MetaRig(FakeGpu, configure=configure, accepted=MODERN)
        result, _, final, cache, fake_calls = self.read(rig)
        self.assertEqual(result, UNSTABLE)
        self.assertEqual(commands(fake_calls), CAPTURE * 2)  # unstable is not retried; no STATUS
        self.assertEqual(final, state(transport_key=A612_KEY))
        self.assertEqual(cache, ((rig.header, rig.fields), True))

    def check_stable(self, configure):
        result, *_ = self.read(MetaRig(FakeGpu, configure=configure, accepted=MODERN))
        self.assertEqual(result[0], "ok", result)
        return result[1]

    def test_captures_differing_only_in_control(self):
        self.check_unstable(payload(cp.CONTROL, 2, byte(OPAQUE, 0)))
        # the byte is checked by no _once step: the same change in both captures reads
        got = self.check_stable(both_captures(cp.CONTROL, byte(OPAQUE, 0)))
        self.assertEqual(bytes.fromhex(got["control_hex"])[OPAQUE], 0)

    def test_captures_differing_only_in_metadata(self):
        self.check_unstable(payload(cp.NATIVE_INFO, 2, byte(NATIVE_INFO_LAST, 7)))
        got = self.check_stable(both_captures(cp.NATIVE_INFO, byte(NATIVE_INFO_LAST, 7)))
        self.assertEqual(bytes.fromhex(got["native_info_hex"])[NATIVE_INFO_LAST], 7)


# ---- _metadata_once failing inside _once, through read() -------------------------------------------

def faults(*events):
    def configure(fake):
        for key, event in events:
            fake.faults[key] = event
    return configure


class MetadataInReadTests(ReadCase):
    def test_a_definite_info_failure(self):
        rig = MetaRig(FakeGpu, configure=faults(((cp.INFO, 1), {"rm": 0x56})), accepted=MODERN)
        result, _, final, cache, fake_calls = self.read(rig)
        self.assertEqual(result, rm_error(cp.INFO, 0x56))
        self.assertEqual(commands(fake_calls), METADATA[:1])
        self.assertEqual(cache, ((rig.header, rig.fields), True))

    def test_a_transient_info_failure_retries_a_fresh_pair(self):
        for count, infos in ((1, 3), (2, 4)):
            with self.subTest(count=count):
                rig = MetaRig(FakeGpu, configure=faults(((cp.INFO, count), {"rm": 0x99})), accepted=MODERN,
                              recapture=True)
                result, _, final, cache, fake_calls = self.read(rig)
                self.assertEqual(result[0], "ok", result)
                self.assertEqual(final, state(transport_key=A612_KEY))
                self.assertEqual(cache, ((rig.header, rig.fields), False))  # captured again after the reset
                self.assertEqual(commands(fake_calls).count((cp.INFO, 0x924)), infos)
                self.assertEqual(commands(fake_calls)[-len(CAPTURE) * 2 - 1:], CAPTURE * 2 + [STATUS])

    def test_a_transient_info_failure_without_a_capturable_transport(self):
        rig = MetaRig(FakeGpu, configure=faults(((cp.INFO, 2), {"rm": 0x99})), accepted=MODERN)
        result, _, _, cache, fake_calls = self.read(rig)
        self.assertEqual(result, ("ClientPowerError", "transient", NO_TRANSPORT, None))
        self.assertEqual(commands(fake_calls), CAPTURE + METADATA[:1])
        self.assertEqual(cache, (None, False))

    def test_a_failed_role_or_native_info_read(self):
        for position, command in ((1, cp.ROLE_INFO), (2, cp.NATIVE_INFO)):
            for count, before in ((1, []), (2, CAPTURE)):
                with self.subTest(command=hex(command), count=count):
                    rig = MetaRig(FakeGpu, configure=faults(((command, count), {"rm": 0x56})), accepted=MODERN)
                    result, _, final, cache, fake_calls = self.read(rig)
                    self.assertEqual(result, rm_error(command, 0x56))
                    self.assertEqual(commands(fake_calls), before + METADATA[:position + 1])
                    self.assertEqual(final, state(transport_key=A612_KEY))
                    self.assertEqual(cache, ((rig.header, rig.fields), True))

    def test_a_failed_metadata_check(self):
        row = cp.INFO_BASE + 37 * cp.INFO_STRIDE  # FakeGpu's client INFO row
        for count, before in ((1, []), (2, CAPTURE)):
            with self.subTest(count=count):
                rig = MetaRig(FakeGpu, configure=payload(cp.INFO, count, flip(row)), accepted=MODERN)
                result, _, final, cache, fake_calls = self.read(rig)
                self.assertEqual(result, ("ClientPowerError", MALFORMED,
                                          "Selected client INFO type discriminants disagree", None))
                self.assertEqual(commands(fake_calls), before + METADATA[:1])  # checked before ROLE_INFO
                self.assertEqual(final, state(transport_key=A612_KEY))
                self.assertEqual(cache, ((rig.header, rig.fields), True))


# ---- _once refusals in the legacy, Turing, INFO216 and RM26 groups ---------------------------------

NATIVE_TYPE = "Bound native policy CONTROL type differs from INFO"
LEGACY_CP, TURING_CP, RM26_CP = 8 + 3 * 12, 8 + 5 * 12, 4 + 11 * 12  # each model's client CONTROL slot
LEGACY_NP, TURING_NP = 20 + 19 * 184, 20 + 17 * 124  # each model's bound native CONTROL row
RM26_CONTROL, RM26_NATIVE_CONTROL = 0x20802632, 0x2080261A


GROUP_REFUSALS = [
    # LegacyGpu: the group byte is INFO's byte 0 and the mask is the 4 bytes at [4:8]
    ("legacy group byte", LegacyGpu, cp.CONTROL, byte(0, 0x5A), GROUP),
    ("legacy mask, another bit", LegacyGpu, cp.CONTROL, byte(4, 0x10), GROUP),
    ("legacy mask, last byte", LegacyGpu, cp.CONTROL, byte(7, 0x80), GROUP),
    ("legacy row type", LegacyGpu, cp.CONTROL, flip(LEGACY_CP), ROW_TYPE),
    ("legacy client", LegacyGpu, cp.CONTROL, byte(LEGACY_CP + 8, 0xF8), ROW_TYPE),
    ("legacy native header", LegacyGpu, cp.NATIVE_CONTROL, word(12, 254), NATIVE_HEADER),
    ("legacy native type", LegacyGpu, cp.NATIVE_CONTROL, word(LEGACY_NP, 9), NATIVE_TYPE),
    # TuringGpu (policy124): the long native header (0, 0, 0, 255, mask) and 124-byte native rows
    ("policy124 group byte", TuringGpu, cp.CONTROL, byte(0, 0x5A), GROUP),
    ("policy124 mask", TuringGpu, cp.CONTROL, byte(5, 0x01), GROUP),
    ("policy124 row type", TuringGpu, cp.CONTROL, flip(TURING_CP + 1), ROW_TYPE),
    ("policy124 native header word 0", TuringGpu, cp.NATIVE_CONTROL, word(0, 1), NATIVE_HEADER),
    ("policy124 native header 255", TuringGpu, cp.NATIVE_CONTROL, word(12, 254), NATIVE_HEADER),
    ("policy124 native inventory", TuringGpu, cp.NATIVE_CONTROL, word(16, 1), NATIVE_HEADER),
    ("policy124 native type", TuringGpu, cp.NATIVE_CONTROL, word(TURING_NP, 9), NATIVE_TYPE),
    ("policy124 native request", TuringGpu, cp.NATIVE_CONTROL, word(TURING_NP + 4, 321001), "unstable"),
    # TuringGpu with the 216-byte INFO record: the native CONTROL rows keep their 124-byte stride
    ("INFO216 native header", "INFO216", cp.NATIVE_CONTROL, word(12, 254), NATIVE_HEADER),
    ("INFO216 native inventory", "INFO216", cp.NATIVE_CONTROL, word(16, 1), NATIVE_HEADER),
    ("INFO216 native type", "INFO216", cp.NATIVE_CONTROL, word(TURING_NP, 9), NATIVE_TYPE),
    # ExtendedRm26Gpu: CONTROL echoes the request's 4-byte group word
    ("rm26 extended group echo", ExtendedRm26Gpu, RM26_CONTROL, word(0, 1 << 3), GROUP),
    ("rm26 extended group, unrelated bit", ExtendedRm26Gpu, RM26_CONTROL, word(0, (1 << 11) | (1 << 7)), GROUP),
    # Rm26Gpu: the short records' row type and client
    ("rm26 row type", Rm26Gpu, RM26_CONTROL, flip(RM26_CP), ROW_TYPE),
    ("rm26 row type discriminant", Rm26Gpu, RM26_CONTROL, flip(RM26_CP + 1), ROW_TYPE),
    ("rm26 client", Rm26Gpu, RM26_CONTROL, byte(RM26_CP + 8, 0xF8), ROW_TYPE),
]


def group_rig(model, configure):
    if model is LegacyGpu:
        return MetaRig(LegacyGpu, configure=configure, accepted=legacy_accepted())
    if model == "INFO216":
        return MetaRig(lambda: TuringGpu(record=216), configure=configure)
    return MetaRig(model, configure=configure)


class GroupRefusalTests(ReadCase):
    def test_refusals(self):
        for name, model, command, change, message in GROUP_REFUSALS:
            with self.subTest(name):
                rig = group_rig(model, payload(command, 1, change))
                result, _, final, cache, fake_calls = self.read(rig)
                if message == "unstable":
                    expected = ("ClientPowerError", "unstable", "Client and native current requests disagree", None)
                else:
                    expected = ("ClientPowerError", MALFORMED, message, None)
                self.assertEqual(result, expected)
                self.assertEqual(commands(fake_calls)[-1][0], command)  # refused at the changed response
                self.assertEqual(cache, ((rig.header, rig.fields), True))

    def test_the_unchanged_responses_read(self):
        # the cases above refuse because of the rig's change, not the model
        for model in (LegacyGpu, TuringGpu, "INFO216", ExtendedRm26Gpu, Rm26Gpu):
            with self.subTest(model=getattr(model, "__name__", model)):
                result, *_ = self.read(group_rig(model, None))
                self.assertEqual(result[0], "ok", result)

    def test_the_rig_changes_legacy_and_turing_responses(self):
        # LegacyGpu ignores its own "payload" faults and TuringGpu keys them by size; the rig applies
        # payload() itself, so a change both captures carry reads through into the result.
        for model, offset in ((LegacyGpu, cp.LEGACY_SIZES[cp.NATIVE_CONTROL] - 1), (TuringGpu, 4431),
                              ("INFO216", 4431)):
            with self.subTest(model=getattr(model, "__name__", model)):
                result, *_ = self.read(group_rig(model, both_captures(cp.NATIVE_CONTROL, byte(offset, 0xC3))))
                self.assertEqual(result[0], "ok", result)
                self.assertEqual(bytes.fromhex(result[1]["native_control_hex"])[offset], 0xC3)


# ---- _arbitration -------------------------------------------------------------------------------

def status_faults(*events):
    def configure(fake):
        for count, event in enumerate(events, 1):
            fake.faults[(cp.NATIVE_STATUS, count)] = event
    return configure


class ArbitrationTests(ReadCase):
    def test_a_transient_then_success_resets_the_transport(self):
        rig = MetaRig(FakeGpu, configure=status_faults({"rm": 0x99}), accepted=MODERN, recapture=True)
        result, calls, final, cache, fake_calls = self.read(rig)
        self.assertEqual(result[0], "ok", result)
        self.assertTrue(result[1]["arbitration"]["available"])
        self.assertIsNone(result[1]["arbitration_error"])
        self.assertEqual(commands(fake_calls), CAPTURE * 2 + [STATUS] * 2)
        self.assertEqual(final, state(transport_key=A612_KEY))
        self.assertEqual(cache, ((rig.header, rig.fields), False))  # captured again after the reset

    def test_two_transients_report_the_second(self):
        rig = MetaRig(FakeGpu, configure=status_faults({"rm": 0x99}, {"rm": 0x98}), accepted=MODERN,
                      recapture=True)
        result, _, _, cache, fake_calls = self.read(rig)
        self.assertEqual(result[0], "ok", result)
        self.assertEqual((result[1]["arbitration"], result[1]["arbitration_error"]),
                         (None, "Client-power command 0x2080a619 rejected (RM 0x98)"))
        self.assertEqual(commands(fake_calls), CAPTURE * 2 + [STATUS] * 2)
        self.assertEqual(cache, (None, False))  # reset after the second transient too

    def test_a_transient_retry_without_a_capturable_transport(self):
        rig = MetaRig(FakeGpu, configure=status_faults({"rm": 0x99}), accepted=MODERN)
        result, _, _, cache, fake_calls = self.read(rig)
        self.assertEqual(result[0], "ok", result)
        self.assertEqual((result[1]["arbitration"], result[1]["arbitration_error"]), (None, NO_TRANSPORT))
        self.assertEqual(commands(fake_calls), CAPTURE * 2 + [STATUS])
        self.assertEqual(cache, (None, False))

    def test_a_non_transient_failure_is_not_retried(self):
        cases = (({"rm": 0x56}, "Client-power command 0x2080a619 rejected (RM 0x56)"),
                 ({"rm": 0x1F}, "Client-power command 0x2080a619 rejected (RM 0x1f)"),
                 ({"header": (12, 7)}, "Client-power response changed its transport envelope"),
                 ({"guard": "after"}, "Clock GET exceeded the guarded allocation"))
        for event, message in cases:
            with self.subTest(message):
                rig = MetaRig(FakeGpu, configure=status_faults(event), accepted=MODERN, recapture=True)
                result, _, _, cache, fake_calls = self.read(rig)
                self.assertEqual(result[0], "ok", result)
                self.assertEqual((result[1]["arbitration"], result[1]["arbitration_error"]), (None, message))
                self.assertEqual(commands(fake_calls), CAPTURE * 2 + [STATUS])
                self.assertEqual(cache, ((rig.header, rig.fields), True))

    def test_a_transient_then_a_definite_failure_reports_the_second(self):
        rig = MetaRig(FakeGpu, configure=status_faults({"completion": 0}, {"rm": 0x40}), accepted=MODERN,
                      recapture=True)
        result, *_ = self.read(rig)
        self.assertEqual(result[1]["arbitration_error"], "Client-power command 0x2080a619 rejected (RM 0x40)")

    def test_a_transient_then_an_unavailable_decode(self):
        rig = MetaRig(FakeGpu, configure=status_faults({"rm": 0x99}, {"payload": word(4, 1)}), accepted=MODERN,
                      recapture=True)
        result, *_ = self.read(rig)
        self.assertEqual((result[1]["arbitration"], result[1]["arbitration_error"]),
                         (None, "The INFO, CONTROL and STATUS occupancy masks or control header disagree."))

    def test_an_identity_failure_during_status_is_raised(self):
        rig = MetaRig(FakeGpu, accepted=MODERN, recapture=True)
        stored = list(rig.header)

        def change(_fake):  # the driver's power GET now has another size
            rig.header[2], rig.header[15] = 11748, 11680

        def configure(fake):
            fake.faults[(cp.NATIVE_STATUS, 1)] = {"rm": 0x99, "mutate": change}
        rig.configure = configure

        def action(side):
            store(side, rig)
            try:
                return outcome(lambda: full_read(side))
            finally:
                rig.header[:] = stored
        result, _, final, cache, fake_calls = self.read(rig, action)
        self.assertEqual(result, ("ClientPowerError", "identity",
                                  "Client-power transport changed; create a fresh controller", None))
        self.assertEqual(final, state(transport_key=A612_KEY))
        changed = list(stored)
        changed[2], changed[15] = 11748, 11680
        self.assertEqual(cache, ((changed, rig.fields), False))  # captured again, then refused
        self.assertEqual(commands(fake_calls), CAPTURE * 2 + [STATUS])

    def test_the_decoded_report_matches_the_native_decoder(self):
        # The binding's rebuild of the report is native_power_arbitration.decode's for the same packets.
        rig = MetaRig(FakeGpu, accepted=MODERN)
        result, *_ = self.read(rig)
        got = result[1]
        fake = FakeGpu()
        fake.counts[cp.NATIVE_STATUS] = 1  # the one STATUS the read sent
        status = fake._status()
        words = cp._words
        expected = native_power_arbitration.decode(cp._LAYOUT, words(bytes.fromhex(got["native_info_hex"])),
                                                   words(bytes.fromhex(got["native_control_hex"])),
                                                   words(bytes(status)))
        self.assertEqual(ordered(got["arbitration"]), ordered(expected))


# ---- the GPU lock read() holds -------------------------------------------------------------------

class BlockingRig(MetaRig):
    """MetaRig whose escape, on the reading thread's first NATIVE_CONTROL, signals `entered` and
    waits for `release`; every escape is logged with the thread that issued it."""

    def fresh(self):
        super().fresh()
        self.reader, self.escapes = None, []
        self.entered, self.release = threading.Event(), threading.Event()

    def respond(self, address, size, fields):
        command = int((u32 * (size // 4)).from_address(address)[14])
        me = threading.get_ident()
        self.escapes.append((me, command))
        if me == self.reader and command == cp.NATIVE_CONTROL and not self.entered.is_set():
            self.entered.set()
            if not self.release.wait(30):
                raise AssertionError("the blocked read was never released")
        return super().respond(address, size, fields)


class LockTests(ReadCase):
    def test_read_holds_the_gpu_lock_for_the_whole_read(self):
        # Python: read() holds gpu._lock, so read_metadata() on another thread waits for it. The core:
        # dc_cpc_read holds the handle's mutex, so dc_cpc_read_metadata on another thread waits too.
        rig = BlockingRig(FakeGpu, accepted=MODERN)
        timeout = 30

        def action(side):
            store(side, rig)
            results = {}

            def reader():
                rig.reader = threading.get_ident()
                results["read"] = outcome(lambda: full_read(side))

            def other():
                results["metadata"] = outcome(side.read)  # read_metadata on both sides
            first = threading.Thread(target=reader, daemon=True)
            second = threading.Thread(target=other, daemon=True)
            try:
                first.start()
                self.assertTrue(rig.entered.wait(timeout), "the read never reached NATIVE_CONTROL")
                second.start()
                second.join(0.5)  # bounded: it must still be waiting for the lock
                waiting = second.is_alive()
                early = [command for thread, command in rig.escapes if thread == second.ident]
            finally:
                rig.release.set()
            first.join(timeout)
            second.join(timeout)
            self.assertFalse(first.is_alive() or second.is_alive(), "a thread did not return")
            order = ["read" if thread == first.ident else "metadata" for thread, _ in rig.escapes]
            return results["read"], results["metadata"], waiting, early, order

        (read, metadata, waiting, early, order), _, final, cache, fake_calls = self.read(rig, action)
        self.assertEqual((read[0], metadata[0]), ("ok", "ok"), (read, metadata))
        self.assertTrue(waiting)
        self.assertEqual(early, [])
        reads = len(CAPTURE) * 2 + 1
        self.assertEqual(order, ["read"] * reads + ["metadata"] * len(METADATA) * 2)
        self.assertEqual(commands(fake_calls), CAPTURE * 2 + [STATUS] + METADATA * 2)
        self.assertEqual(final, state(transport_key=A612_KEY))


# ---- the C ABI ----------------------------------------------------------------------------------

class AbiTests(unittest.TestCase):
    def tearDown(self):
        native.set_export_resolver(None)

    def test_arguments(self):
        lib = native.load()
        rig = MetaRig(FakeGpu, accepted=MODERN)
        rig.fresh()
        install(rig.card.nvml, rig.card.nvapi, rig.gdi())
        gpu = native.Gpu()
        rig.calls = []
        observation = native.Gpu._cp_observation_type()
        try:
            before = gpu._cp_state()
            o, error = observation(), native._Error()
            ctypes.memset(ctypes.addressof(o), 0xA5, ctypes.sizeof(o))

            def sizes(outer=0, metadata=0, report=0):
                o.size = ctypes.sizeof(o) + outer
                o.metadata.size = ctypes.sizeof(native.Gpu._CpcMetadata) + metadata
                o.arbitration.size = ctypes.sizeof(o.arbitration) + report
            sizes()
            error.size, error.kind = ctypes.sizeof(error), 99
            sentinel = bytes(o)

            def call(handle=gpu._handle, out=ctypes.addressof(o), err=ctypes.byref(error)):
                return lib.dc_cpc_read(handle, out, err)

            def untouched():
                self.assertEqual(error.kind, 99)
                self.assertEqual(bytes(o), sentinel)
            self.assertEqual(call(handle=None), native.DC_E_ARGUMENT)
            untouched()
            self.assertEqual(call(out=None), native.DC_E_ARGUMENT)
            untouched()
            for change in ({"outer": -1}, {"outer": 1}, {"metadata": 1}, {"metadata": -1}, {"report": -4},
                           {"report": 4}):
                with self.subTest(change):
                    sizes(**change)
                    sentinel = bytes(o)
                    self.assertEqual(call(), native.DC_E_ARGUMENT)
                    untouched()
                    sizes()
            sentinel = bytes(o)
            for size in (0, ctypes.sizeof(error) - 4, ctypes.sizeof(error) + 4):
                with self.subTest(error_size=size):
                    error.size = size
                    error_before = bytes(error)
                    self.assertEqual(call(), native.DC_E_ARGUMENT)
                    self.assertEqual(bytes(o), sentinel)
                    self.assertEqual(bytes(error), error_before)  # kind 99 included
                    self.assertEqual(error.kind, 99)
            self.assertEqual(rig.calls, [])
            self.assertEqual(gpu._cp_state(), before)
            self.assertEqual(gpu._current_limit_transport, None)
            # A NULL error is allowed: the failure (no transport) is still DC_E_FAILED, *out untouched.
            self.assertEqual(call(err=None), native.DC_E_FAILED)
            self.assertEqual(bytes(o), sentinel)
            # With an error out the failure is reported there, *out still untouched.
            error.size, error.kind, error.rm_status = ctypes.sizeof(error), 99, 0x77
            self.assertEqual(call(), native.DC_E_FAILED)
            self.assertEqual(bytes(o), sentinel)
            self.assertEqual((error.size, error.kind, error.rm_status, error.message.decode()),
                             (ctypes.sizeof(error), native.DC_ERR_TRANSIENT, 0, NO_TRANSPORT))
            # The success path fills the whole struct over the 0xA5 fill and clears a stale *error.
            gpu._current_limit_transport = (list(rig.header), dict(rig.fields))
            error.size, error.kind, error.rm_status, error.message = ctypes.sizeof(error), 99, 0x56, b"stale"
            error.fixed, error.has_ntstatus, error.ntstatus = 1, 1, -5
            self.assertEqual(call(), native.DC_OK)
            self.assertEqual((o.size, o.metadata.size, o.arbitration.size),
                             (ctypes.sizeof(o), ctypes.sizeof(native.Gpu._CpcMetadata),
                              ctypes.sizeof(o.arbitration)))
            self.assertEqual((o.requested_mw, o.control_size, o.native_control_size), (321000, 0x328, 13876))
            self.assertEqual((o.has_arbitration, o.has_arbitration_error, o.arbitration_error),
                             (1, 0, b""))
            self.assertEqual((o.metadata.schema, o.metadata.units, o.arbitration.available), (1, b"mW", 1))
            raw = bytes(o)
            error_field = observation.arbitration_error
            self.assertEqual(raw[error_field.offset:error_field.offset + error_field.size], bytes(error_field.size))
            cleared = native._Error()
            cleared.size = ctypes.sizeof(cleared)
            self.assertEqual(bytes(error), bytes(cleared))  # kind, rm_status, ntstatus, message all reset
        finally:
            gpu.close()

    def test_a_buffer_refusal_is_a_client_power_error(self):
        # Unreachable in the core (every group fits dc_cpc_observation); the binding's answer to it.
        gpu = native.Gpu.__new__(native.Gpu)
        gpu._handle = None
        stub = SimpleNamespace(dc_cpc_read=Mock(return_value=native.DC_E_BUFFER))
        with patch.object(native, "load", return_value=stub):
            with self.assertRaises(cp.ClientPowerError) as caught:
                gpu._cp_read()
        self.assertEqual(caught.exception.kind, "malformed")
        self.assertIn("DC_E_BUFFER", str(caught.exception))
        self.assertIsNone(caught.exception.rm_status)

    def test_the_payload_arrays_hold_every_group(self):
        o = native.Gpu._cp_observation_type()
        for name in ("SIZES", "LEGACY_SIZES", "TURING_SIZES", "INFO216_SIZES", "RM26_SIZES", "RM26_EXTENDED_SIZES",
                     "A612_COMPACT_SIZES"):
            sizes = getattr(cp, name)
            self.assertLessEqual(sizes[cp.CONTROL], o.control.size)
            self.assertLessEqual(sizes[cp.NATIVE_CONTROL], o.native_control.size)
        self.assertEqual((o.control.size, o.native_control.size),
                         (cp.SIZES[cp.CONTROL], cp.SIZES[cp.NATIVE_CONTROL]))
        self.assertIs(dict(o._fields_)["arbitration"], native_power_arbitration._Report)


if __name__ == "__main__":
    unittest.main()
