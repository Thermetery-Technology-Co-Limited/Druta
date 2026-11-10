# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native policy writer and the user's values, increment B3: nvbackend.GPU._write_policy_limit,
_pin, set_power_policy and _reapply_power_policy_pins against the core.

The rig is B2's (tests.test_native_power_policies): both sides answer from a FRESH
tests.test_power_policies.PolicyCard (the TITAN RTX table as measured; the driver re-derives the
coupled policies from the board limit on every board write), the Python twin through its mocked
escape and the core through tests.test_native_current_limits.CLRig. Failures are injected into
that same escape (an RM refusal or an NTSTATUS failure of the n-th escape of one command, an
ignored SET, a SET that also changes a non-limit word), identically on both sides. Every case
compares

- the outcome: (ok, message), or the exception type and text;
- the driver log (each escape's size, header and parameter words, fields, NTSTATUS, RM): every GET
  and SET with its words, so the SET sequence and the restoration replay are compared word for word;
- the pins (GPU._power_policy_pins), GPU._board_policy, GPU._last_policy_table and the established
  mask (GPU._current_limit_established) afterwards.

Recorded divergences (asserted below as the binding's behaviour, not hidden):

- _write_policy_limit takes a plain-int value within int64: a float or bool is a TypeError and a
  larger int a ValueError before anything is read (Python compares a float and lets ctypes refuse
  it inside its try). set_power_policy and _reapply_power_policy_pins never pass such a value. An
  int subclass is a value Python writes as its int, and so does the binding.

Most cases run on the TITAN/Turing table (control_mask_word 4); Rm26Card is the short RM26 CONTROL
layout (mask word 0, control records at 4 + 52 * policy) for the writer, its restoration replay and
the re-application on a policy whose selector is bit 31 of word 0. The SET helper reads the mask
word, the control geometry and the wire SET command from the card, never from the Turing constants.
"""
import enum
import threading
import unittest
from unittest import mock

import tests.test_native_current_limit_state as state
import tests.test_native_current_limits as base
import tests.test_native_power_policies as b2
from druta import native
from tests.test_power_policies import A618, A619, A61A, CONTROL, E61B, PolicyCard

INFO_GET, STATUS_GET, CONTROL_GET, SET = base.INFO, base.STATUS, base.CONTROL, base.SET
REFUSED = "policy request failed (NTSTATUS 0, RM 0x1)"
RESTORED = "original limit restored and verified"
UNVERIFIED = "restoration could not be verified: "
NONE_SUBSCRIPT = "'NoneType' object is not subscriptable"


class P(int):
    """An int subclass: Python's `type(x) is int` gates refuse it, its value is still an int."""


class Policy(enum.IntEnum):
    FOUR = 4
    THIRTEEN = 13


class Side(b2.Side):
    def __init__(self, kind, **kw):
        super().__init__(kind, **kw)
        header = getattr(self.card, "HEADER", None)
        if header is not None:  # the card's own power GET: the layout the transport is keyed by
            self.header = [0] * 17
            self.header[14], self.header[2], self.header[15] = header
            if kind == "python":
                self.gpu._capture_current_limit_transport.return_value = (list(self.header), dict(base.FIELDS))
            self.gpu._current_limit_transport = (list(self.header), dict(base.FIELDS))

    def cache(self):
        gpu = self.gpu
        return {"board_policy": getattr(gpu, "_board_policy", None),
                "table": getattr(gpu, "_last_policy_table", None),
                "pins": dict(getattr(gpu, "_power_policy_pins", None) or {}),
                "established": set(getattr(gpu, "_current_limit_established", None) or ())}


class Rm26Card(PolicyCard):
    """PolicyCard on the short RM26 packets (header 0x20802612 / 2524 / 2456): the 26xx commands,
    5008 / 14148 / 2108 / 2108-byte blocks, info records at 52 + 116 p, status at 12 + 372 p,
    control at 4 + 52 p with the policy mask (and the SET's selector) in control word 0. The
    control block's opaque words carry a pattern the writer must preserve."""

    HEADER = (0x20802612, 2524, 2456)
    WIRE = {0x20802618: A618, 0x20802619: A619, 0x2080261A: A61A, 0x2080261B: E61B}
    WIRE_SIZES = {0x20802618: 5008, 0x20802619: 14148, 0x2080261A: 2108, 0x2080261B: 2108}
    INFO, STATUS, CONTROL, MASK_WORD, SET = (52, 116), (12, 372), (4, 52), 0, 0x2080261B

    def escape(self, packet, fields):
        wire, size = packet[14], packet[15]
        if self.WIRE_SIZES.get(wire) != size:
            packet[16] = 0x1F
            return 0
        command, words = self.WIRE[wire], [0] * (size // 4)
        if command == A618:
            words[1] = self.mask
            for p, s in self.spec.items():
                m = (self.INFO[0] + p * self.INFO[1]) // 4
                words[m:m + 5] = [0x30, s["type"] | s["channel"] << 8 | s["unit"] << 16,
                                  s["min"], s["default"], s["max"]]
        elif command == A61A:
            words = [(i * 17 + 31) & 0xFFFFFFFF for i in range(size // 4)]
            words[0] = self.mask
            for p, s in self.spec.items():
                r = (self.CONTROL[0] + p * self.CONTROL[1]) // 4
                words[r:r + 2] = [self.control_type.get(p, s["type"]), self.request[p]]
        elif command == A619:
            words[0] = self.mask
            for p, s in self.spec.items():
                st = (self.STATUS[0] + p * self.STATUS[1]) // 4
                words[st:st + 3] = [s["type"], self.effective(p), self.reading.get(p, 1000 + p)]
        else:
            params = list(packet[17:])
            policy = params[self.MASK_WORD].bit_length() - 1
            r = (self.CONTROL[0] + policy * self.CONTROL[1]) // 4
            self.sets.append((policy, params[r + 1]))
            if self.ignore_next:
                self.ignore_next = False
                return 0
            self.request[policy] = params[r + 1]
            if policy == self.board:
                self.derive()
            return 0
        packet[17:] = words
        return 0


# policy, type, channel, unit, min, default, max: the board (2), the named core current (13) and an
# ordinary mW policy at index 31, whose SET selector is the top bit of word 0
RM26 = [(2, 0x00, 9, 0, 180000, 321000, 550000), (13, 0x0B, 19, 1, 1000, 350780, 390000),
        (31, 0x05, 27, 0, 1000, 63000, 100000)]
RM26_KW = dict(card=Rm26Card, table=RM26, coupled=(), board=2)


def geometry(card):
    """(mask word, control (base, stride), wire SET command) of the card's layout."""
    return (getattr(card, "MASK_WORD", 4), getattr(card, "CONTROL", CONTROL), getattr(card, "SET", SET))


def fail(command, *nths, rm=0x1, ntstatus=None):
    """From now on, the nths (1-based) escapes of command fail: the RM status word rm written back,
    or the NTSTATUS ntstatus returned with the packet untouched."""
    def arm(side):
        inner, seen = side.driver.inner, [0]

        wire = {logical: code for code, logical in getattr(side.card, "WIRE", {}).items()}.get(command, command)

        def escape(packet, fields):
            if packet[14] == wire:
                seen[0] += 1
                if seen[0] in nths:
                    if ntstatus is not None:
                        return ntstatus
                    packet[16] = rm
                    return 0
            return inner(packet, fields)
        side.driver.inner = escape
    return arm


def ignore_sets(*nths):
    """The nths (1-based) SETs from now on are accepted but change nothing."""
    def arm(side):
        inner, seen = side.driver.inner, [0]

        wire_set = geometry(side.card)[2]

        def ignoring(packet, fields):
            if packet[14] == wire_set:
                seen[0] += 1
                if seen[0] in nths:
                    side.card.ignore_next = True
            return inner(packet, fields)
        side.driver.inner = ignoring
    return arm


def after_set(change):
    """change(card) runs after the first SET from now on that the driver accepted (the driver
    changing more than it was asked to)."""
    def arm(side):
        inner, done = side.driver.inner, []

        wire_set = geometry(side.card)[2]

        def changing(packet, fields):
            status = inner(packet, fields)
            if packet[14] == wire_set and not done and status == 0 and packet[16] == 0:
                done.append(True)
                change(side.card)
            return status
        side.driver.inner = changing
    return arm


def requests(**values):
    """The card's requests changed behind Druta's back (the driver recalculating them)."""
    def setup(side):
        for key, value in values.items():
            side.card.request[int(key[1:])] = value
    return setup


def act(*arms, call):
    def action(side):
        for arm in arms:
            arm(side)
        return call(side)
    return action


class WriteCase(b2.PolicyCase):
    def side(self, kind, setup=None, **kw):
        side = Side(kind, **kw)
        self.addCleanup(side.close)
        self.geometry = geometry(side.card)  # identical on both sides: the same kw
        self.wire = dict(getattr(side.card, "WIRE", {}))
        try:
            side.gpu._power_policy_table()  # the layout choice is made: the logs start at the action
        except Exception:                                               # noqa: BLE001 - VOLTA
            pass
        if setup is not None:
            setup(side)
        side.driver.log.clear()
        return side

    def sides(self, action, setup=None, **kw):
        """The runs of both sides, uncompared (for an accepted divergence)."""
        return [self.run_side(self.side(kind, setup, **kw), action) for kind in ("python", "native")]

    def commands(self, run):
        """The logical command of every escape in the log (a card's wire commands mapped back)."""
        return [self.wire.get(entry[1][14], entry[1][14]) for entry in run[1]]

    def sets(self, run):
        """(selected policy, limit word) of every SET in the log, as the driver received them, read
        with the layout of the card the run used (its mask word, control base and stride)."""
        mask_word, (control_base, control_stride), wire_set = self.geometry
        out = []
        for entry in run[1]:
            if entry[1][14] == wire_set:
                params = entry[2]
                policy = params[mask_word].bit_length() - 1
                out.append((policy, params[(control_base + policy * control_stride) // 4 + 1]))
        return out


def write(policy, value):
    return lambda side: side.gpu._write_policy_limit(policy, value)


# ---- _write_policy_limit -------------------------------------------------------------------------

class WritePolicyLimitTests(WriteCase):
    def test_success_without_moved_policies(self):
        run = self.both(write(4, 150000))
        self.assertEqual(run[0], ("ok", (True, "policy 4 set to 150 W; effective limit 150 W")))
        self.assertEqual(self.sets(run), [(4, 150000)])
        self.assertEqual(self.commands(run), [INFO_GET, CONTROL_GET, STATUS_GET, SET,
                                              INFO_GET, CONTROL_GET, STATUS_GET])
        self.assertEqual(run[2]["pins"], {})  # _write_policy_limit never pins

    def test_success_with_moved_policies_listed_in_policy_order(self):
        # the board write moves every coupled policy; the card's coupling order is not the message's
        run = self.both(write(2, 290000), coupled=(13, 9, 3))
        self.assertEqual(run[0], ("ok", (True, "policy 2 set to 290 W; effective limit 299.062 W; "
                                               "the driver also changed policies 3, 9, 13")))
        run = self.both(write(2, 290000))
        self.assertTrue(run[0][1][1].endswith("; the driver also changed policies 3, 4, 5, 7, 8, 9, 13"), run[0])

    def test_already_at_value_issues_no_set(self):
        run = self.both(write(8, 17000))
        self.assertEqual(run[0], ("ok", (True, "policy 8 already at 17 A")))
        self.assertEqual(self.commands(run), [INFO_GET, CONTROL_GET, STATUS_GET])

    def test_ignored_set_is_restored_and_verified(self):
        run = self.both(act(ignore_sets(1), call=write(4, 150000)))
        self.assertEqual(run[0], ("ok", (False, f"policy 4: control read-back disagrees; {RESTORED}")))
        self.assertEqual(self.sets(run), [(4, 150000), (4, 143000)])

    def test_changed_non_limit_word(self):
        def retype(card):
            card.control_type[5] = 0x07
        run = self.both(act(after_set(retype), call=write(4, 150000)))
        self.assertEqual(run[0], ("ok", (False, "policy 4: control read-back changed data outside the limits; "
                                                f"{RESTORED}")))
        self.assertEqual(self.sets(run), [(4, 150000), (4, 143000)])

    def test_moved_limit_word_is_not_outside_the_limits(self):
        def move(card):
            card.request[9] = 20000
        run = self.both(act(after_set(move), call=write(4, 150000)))
        self.assertEqual(run[0], ("ok", (True, "policy 4 set to 150 W; effective limit 150 W; "
                                               "the driver also changed policies 9")))

    def test_refused_set(self):
        run = self.both(act(fail(SET, 1), call=write(4, 150000)))
        self.assertEqual(run[0], ("ok", (False, f"policy 4: {REFUSED}; {RESTORED}")))
        self.assertEqual(self.sets(run), [(4, 150000), (4, 143000)])

    def test_set_escape_failure(self):
        """An NTSTATUS failure drops the transport and the layout choice on both sides. DIVERGENCE
        (accepted, the fixture's capture, as in test_native_current_limits): the Python twin's mocked
        capture supplies the transport again for the replay; the core's rig has nothing to capture
        with, so its replay is refused and the note says the restoration could not be verified."""
        runs = self.sides(act(fail(SET, 1, ntstatus=-1073741823), call=write(4, 150000)))
        (python, python_log, python_cache), (core, core_log, core_cache) = runs
        failed = "policy 4: policy request failed (NTSTATUS -1073741823, RM 0x0); "
        self.assertEqual(python, ("ok", (False, failed + RESTORED)))
        self.assertEqual(core, ("ok", (False, failed + UNVERIFIED + "unsupported current-policy transport: "
                                       "no recognized power GET captured")))
        self.assertEqual(core_log, python_log[:len(core_log)])  # up to the failed SET
        self.assertEqual([entry[1][14] for entry in core_log], [INFO_GET, CONTROL_GET, STATUS_GET, SET])
        self.assertEqual(core_cache["pins"], python_cache["pins"])

    def test_failed_read_back(self):
        run = self.both(act(fail(CONTROL_GET, 2), call=write(4, 150000)))
        self.assertEqual(run[0], ("ok", (False, f"policy 4: {REFUSED}; {RESTORED}")))

    def test_failed_restoration_set(self):
        run = self.both(act(fail(SET, 1, 2), call=write(4, 150000)))
        self.assertEqual(run[0], ("ok", (False, f"policy 4: {REFUSED}; {UNVERIFIED}{REFUSED}")))

    def test_disagreeing_restoration_read_back(self):
        # the SET lands, its read-back fails, the replay is ignored: the limit is still the new one
        run = self.both(act(fail(CONTROL_GET, 2), ignore_sets(2), call=write(4, 150000)))
        self.assertEqual(run[0], ("ok", (False, f"policy 4: {REFUSED}; "
                                                f"{UNVERIFIED}restoration read-back disagrees")))

    def test_failed_restoration_read_back(self):
        run = self.both(act(fail(SET, 1), fail(CONTROL_GET, 2), call=write(4, 150000)))
        self.assertEqual(run[0], ("ok", (False, f"policy 4: {REFUSED}; {UNVERIFIED}{REFUSED}")))

    def test_mask_emptied_by_the_write(self):
        def empty(card):
            card.mask = 0
        # the replay's own mask check then refuses it: the validated table lists no policy
        run = self.both(act(after_set(empty), call=write(4, 150000)))
        self.assertEqual(run[0], ("ok", (False, f"policy 4: {NONE_SUBSCRIPT}; {UNVERIFIED}current-policy write "
                                                "must select one policy the validated table lists")))
        self.assertEqual(self.sets(run), [(4, 150000)])  # the replay never reaches the driver

    def test_refused_set_then_the_restoration_empties_the_mask(self):
        # the first SET is refused, the replay accepted, then the mask is emptied: the restoration's
        # read-back has no control block to index
        def empty(card):
            card.mask = 0
        run = self.both(act(fail(SET, 1), after_set(empty), call=write(4, 150000)))
        self.assertEqual(run[0], ("ok", (False, f"policy 4: {REFUSED}; {UNVERIFIED}{NONE_SUBSCRIPT}")))
        self.assertEqual(self.sets(run), [(4, 150000), (4, 143000)])

    def test_value_beyond_the_limit_word(self):
        # packed into the u32 word as ctypes packs it: the read-back cannot equal the int
        run = self.both(write(4, (1 << 32) + 150000))
        self.assertEqual(run[0], ("ok", (False, f"policy 4: control read-back disagrees; {RESTORED}")))
        self.assertEqual(self.sets(run), [(4, 150000), (4, 143000)])

    def test_table_read_failure(self):
        run = self.both(act(fail(INFO_GET, 1), call=write(4, 150000)))
        self.assertEqual(run[0], ("ok", (False, f"policy 4: the policy table could not be read ({REFUSED}); "
                                                "nothing written")))

    def test_not_in_the_table(self):
        for policy in (6, -1, 32, 1 << 40, 1 << 70, 4.0, True, "4"):
            with self.subTest(policy=policy):
                run = self.both(write(policy, 150000))
                self.assertEqual(run[0], ("ok", (False, f"policy {policy} is not in the driver's table")))
                self.assertEqual(self.commands(run), [INFO_GET, CONTROL_GET, STATUS_GET])
        run = self.both(write(4, 150000), table=[], coupled=(), board=None)
        self.assertEqual(run[0], ("ok", (False, "policy 4 is not in the driver's table")))

    def test_value_that_is_not_an_int64(self):
        """RECORDED DIVERGENCE: refused before the table read (see the module docstring)."""
        core = self.side("native")
        for value, error in ((150000.0, TypeError), (True, TypeError), (1 << 63, ValueError)):
            with self.subTest(value=value):
                with self.assertRaises(error):
                    core.gpu._write_policy_limit(4, value)
        self.assertEqual(core.driver.log, [])

    def test_int_subclass_policy_is_not_in_the_table(self):
        # Python's `type(policy) is not int`, after the table read; nothing is written
        for policy in (P(4), Policy.FOUR):
            with self.subTest(policy=repr(policy)):
                run = self.both(write(policy, 150000))
                self.assertEqual(run[0], ("ok", (False, "policy 4 is not in the driver's table")))
                self.assertEqual(self.commands(run), [INFO_GET, CONTROL_GET, STATUS_GET])

    def test_int_subclass_value_is_written_as_its_int(self):
        # Python's writer has no type gate on the value: it compares, formats and packs the int
        run = self.both(write(4, P(150000)))
        self.assertEqual(run[0], ("ok", (True, "policy 4 set to 150 W; effective limit 150 W")))
        self.assertEqual(self.sets(run), [(4, 150000)])


# ---- set_power_policy ----------------------------------------------------------------------------

def setter(policy, value, **kw):
    return lambda side: side.gpu.set_power_policy(policy, value, **kw)


class SetPowerPolicyTests(WriteCase):
    def test_success_pins_only_with_pin(self):
        run = self.both(setter(4, 150000))
        self.assertEqual(run[0], ("ok", (True, "policy 4 set to 150 W; effective limit 150 W")))
        self.assertEqual(run[2]["pins"], {4: 150000})
        run = self.both(setter(4, 150000, pin=False))
        self.assertEqual(run[0][1][0], True)
        self.assertEqual(run[2]["pins"], {})
        run = self.both(setter(8, 20000), setup=b2.pins({4: 150000}))
        self.assertEqual(run[2]["pins"], {4: 150000, 8: 20000})

    def test_failure_does_not_pin(self):
        run = self.both(act(ignore_sets(1), call=setter(4, 150000)))
        self.assertEqual(run[0], ("ok", (False, f"policy 4: control read-back disagrees; {RESTORED}")))
        self.assertEqual(run[2]["pins"], {})

    def test_already_at_value_pins(self):
        run = self.both(setter(4, 143000))
        self.assertEqual(run[0], ("ok", (True, "policy 4 already at 143 W")))
        self.assertEqual(run[2]["pins"], {4: 143000})

    def test_named_policy(self):
        run = self.both(setter(13, 360000))
        self.assertEqual(run[0], ("ok", (False, "policy 13 is set with its current-limit slider")))
        self.assertNotIn(SET, self.commands(run))
        for value in (360000.0, True, None):
            with self.subTest(value=value):
                run = self.both(setter(13, value, allow_named=True))
                self.assertEqual(run[0], ("ok", (False, "policy 13 must be a whole number of milliamps")))

    def test_named_policy_through_its_slider(self):
        run = self.both(setter(13, 360000, allow_named=True))
        self.assertEqual(run[0][0], "ok")
        self.assertTrue(run[0][1][0], run[0])
        self.assertEqual(run[2]["pins"], {13: 360000})
        self.assertEqual(self.sets(run), [(13, 360000)])
        run = self.both(setter(13, 360000, allow_named=True, pin=False))
        self.assertTrue(run[0][1][0], run[0])
        self.assertEqual(run[2]["pins"], {})
        # the slider's own envelope and texts
        run = self.both(setter(13, 400000, allow_named=True))
        self.assertFalse(run[0][1][0])
        self.assertEqual(run[2]["pins"], {})
        run = self.both(setter(13, 1 << 70, allow_named=True))
        self.assertFalse(run[0][1][0])

    def test_named_from_established(self):
        table = [row for row in b2.TITAN if row[0] in (2, 4, 7)]
        run = self.both(setter(7, 12000), setup=b2.established(7), table=table, coupled=(4, 7))
        self.assertEqual(run[0], ("ok", (False, "policy 7 is set with its current-limit slider")))
        self.assertEqual(run[2]["established"], {7})

    def test_unwritable_rows(self):
        for policy, reason in ((2, "the board limit is set with the Power limit slider"),
                               (11, "it has no range to write")):
            with self.subTest(policy=policy):
                run = self.both(setter(policy, 100000))
                self.assertEqual(run[0], ("ok", (False, f"policy {policy}: {reason}")))
        run = self.both(setter(4, 150000), setup=b2.chain(b2.NO_STATICS, b2.board_mw(None)))
        self.assertEqual(run[0], ("ok", (False, f"policy 4: it may be the board limit ({b2.UNREADABLE})")))

        def retype(side):
            side.card.control_type[4] = 0x05
        run = self.both(setter(4, 150000), setup=retype)
        self.assertEqual(run[0], ("ok", (False, "policy 4: its record type differs between the driver's blocks")))

    def test_out_of_range_and_non_int_values(self):
        for value in (200000, 0, 167001, True, 150000.0, "150000", 1 << 70, -(1 << 70)):
            with self.subTest(value=value):
                run = self.both(setter(4, value))
                self.assertEqual(run[0], ("ok", (False, "policy 4 must be between 1 W and 167 W")))
                self.assertNotIn(SET, self.commands(run))
        run = self.both(setter(8, 40000))
        self.assertEqual(run[0], ("ok", (False, "policy 8 must be between 1 A and 33.75 A")))
        run = self.both(setter(4, 1000))
        self.assertTrue(run[0][1][0])
        run = self.both(setter(4, 167000))
        self.assertTrue(run[0][1][0])

    def test_not_in_the_table(self):
        for policy in (6, 40, -1, 1 << 70):
            with self.subTest(policy=policy):
                run = self.both(setter(policy, 1000))
                self.assertEqual(run[0], ("ok", (False, f"policy {policy} is not in the driver's table")))

    def test_read_error(self):
        run = self.both(act(fail(INFO_GET, *range(1, 10)), call=setter(4, 150000)))
        self.assertEqual(run[0], ("ok", (False, REFUSED)))
        run = self.both(setter(4, 150000), table=[], coupled=(), board=None)
        self.assertEqual(run[0], ("ok", (False, "the driver's policy mask is empty")))
        run = self.both(setter(4, 150000), arch=b2.VOLTA)
        self.assertEqual(run[0], ("ok", (False, "current policies are not validated for this GPU generation")))

    def test_board_candidate_is_remembered_before_the_write(self):
        run = self.both(setter(4, 150000), setup=b2.chain(b2.NO_STATICS, b2.board_mw(260000)))
        self.assertTrue(run[0][1][0], run[0])
        self.assertEqual(run[2]["board_policy"], 2)
        run = self.both(setter(2, 290000), setup=b2.chain(b2.NO_STATICS, b2.board_mw(260000)))
        self.assertEqual(run[0], ("ok", (False, "policy 2: the board limit is set with the Power limit slider")))
        self.assertEqual(run[2]["board_policy"], 2)

    def test_policy_the_abi_cannot_carry(self):
        # Python's steps over the native pieces: 4.0 == 4 finds the row, then the writer refuses it
        for policy, kw, message in ((4.0, {}, "policy 4.0 is not in the driver's table"),
                                    (True, {"value": 2000000}, "policy True must be between 0.001 W and 1001 W"),
                                    (True, {}, "policy True is not in the driver's table"),
                                    (2.0, {}, "policy 2.0: the board limit is set with the Power limit slider"),
                                    (13.0, {}, "policy 13.0 is set with its current-limit slider"),
                                    (13.0, {"allow_named": True}, "not a supported current policy"),
                                    ("4", {}, "policy 4 is not in the driver's table")):
            with self.subTest(policy=policy, kw=kw):
                kw = dict(kw)
                value = kw.pop("value", 150000)
                run = self.both(setter(policy, value, **kw))
                self.assertEqual(run[0], ("ok", (False, message)))
                self.assertEqual(run[2]["pins"], {})

    def test_int_subclasses_are_refused_as_python_refuses_them(self):
        # policy: the row is found (P(4) == 4), then the writer refuses a policy that is not an int;
        # value: `type(value) is not int` is the range refusal (named: the whole-milliamps one)
        for policy, value, kw, message in (
                (P(4), 150000, {}, "policy 4 is not in the driver's table"),
                (Policy.FOUR, 150000, {}, "policy 4 is not in the driver's table"),
                (4, P(150000), {}, "policy 4 must be between 1 W and 167 W"),
                (4, Policy.FOUR, {}, "policy 4 must be between 1 W and 167 W"),
                (P(13), 360000, {"allow_named": True}, "not a supported current policy"),
                (Policy.THIRTEEN, 360000, {"allow_named": True}, "not a supported current policy"),
                (13, P(360000), {"allow_named": True}, "policy 13 must be a whole number of milliamps")):
            with self.subTest(policy=repr(policy), value=repr(value)):
                run = self.both(setter(policy, value, **kw))
                self.assertEqual(run[0], ("ok", (False, message)))
                self.assertNotIn(SET, self.commands(run))
                self.assertEqual(run[2]["pins"], {})


# ---- an exception Python lets escape read_power_policies (the inputs callback) --------------------

def interrupted(side):
    """read_power_limit_mw raises what read_power_policies does not catch."""
    def read():
        raise KeyboardInterrupt("stop")
    side.gpu.read_power_limit_mw = read


def static_without_get(side):
    """A truthy `static` without .get: Python's _power_policy_rows raises AttributeError."""
    side.gpu.static = [1]


def escaping(call):
    """call's outcome with a KeyboardInterrupt caught too (outcome() catches Exception only)."""
    def action(side):
        try:
            return call(side)
        except KeyboardInterrupt as exc:
            return ("KeyboardInterrupt", str(exc))
    return action


class CallerFailureTests(WriteCase):
    """Python raises out of read_power_policies before set_power_policy writes, pins or stores
    anything; the core stops at the callback's failure (DC_E_CALLER) and the binding raises it."""

    FAILURES = (("KeyboardInterrupt", interrupted, ("ok", ("KeyboardInterrupt", "stop"))),
                ("AttributeError", static_without_get, ("AttributeError", "'list' object has no attribute 'get'")))

    def test_set_power_policy_stops_before_any_write(self):
        # Without the stop the write would proceed whatever the inputs then held: the board policy
        # seen earlier (2) makes policy 4 an ordinary writable row
        for name, failure, expected in self.FAILURES:
            with self.subTest(name):
                run = self.both(escaping(setter(4, 150000)),
                                setup=b2.chain(b2.pins({8: 20000}), b2.seen(2), failure))
                self.assertEqual(run[0], expected)
                self.assertEqual(self.commands(run)[-3:], [INFO_GET, CONTROL_GET, STATUS_GET])
                self.assertNotIn(SET, self.commands(run))
                self.assertEqual((run[2]["pins"], run[2]["board_policy"]), ({8: 20000}, 2))
                self.assertIsNotNone(run[2]["table"])

    def test_read_power_policies_stops_alike(self):
        for name, failure, expected in self.FAILURES:
            with self.subTest(name):
                run = self.both(escaping(lambda side: side.gpu.read_power_policies()), setup=failure)
                self.assertEqual(run[0], expected)
                self.assertEqual(self.commands(run)[-3:], [INFO_GET, CONTROL_GET, STATUS_GET])
                self.assertIsNone(run[2]["board_policy"])

    def test_a_named_policy_with_allow_named_stops_too(self):
        run = self.both(escaping(setter(13, 360000, allow_named=True)), setup=interrupted)
        self.assertEqual(run[0], ("ok", ("KeyboardInterrupt", "stop")))
        self.assertNotIn(SET, self.commands(run))
        self.assertEqual(run[2]["pins"], {})

    def test_the_core_reports_the_callbacks_failure(self):
        # the ABI itself: DC_E_CALLER, nothing written to *ok or the buffer, no rows
        side = self.side("native")
        gpu, library = side.gpu, native.load()
        stop, no_confirm = native.CL_POLICY_INPUTS(lambda *_: 1), native.CL_CONFIRM()
        ok, needed = native.ctypes.c_int32(7), native.ctypes.c_size_t(99)
        message = native.ctypes.create_string_buffer(b"untouched", 64)
        status = library.dc_cl_set_power_policy(gpu._handle, no_confirm, None, stop, None, 4, 1, 150000, 1, 0,
                                                native.ctypes.byref(ok), message, len(message),
                                                native.ctypes.byref(needed))
        self.assertEqual((status, ok.value, message.value, needed.value), (native.DC_E_CALLER, 7, b"untouched", 99))
        rows, count, error = gpu._policy_rows_buffer(), native.ctypes.c_uint32(5), native._Error()
        error.size = native.ctypes.sizeof(native._Error)
        status = library.dc_cl_power_policies_read(gpu._handle, no_confirm, None, stop, None, rows, len(rows),
                                                   native.ctypes.byref(count), native.ctypes.byref(error))
        self.assertEqual((status, count.value), (native.DC_E_CALLER, 0))
        self.assertNotIn(SET, self.commands((None, side.driver.log)))
        self.assertEqual(gpu._power_policy_pins, {})


# ---- the confirm callback through set_power_policy (the A6 derived-clamp rig) --------------------

class DerivedClampSetTests(state.StateCase):
    """set_power_policy where read_power_policies' get_current_limits runs its derived-clamp branch:
    policy 13's request above its maximum, the effective limit saturated, the live power read served
    identically to both sides (tests.test_native_power_policies.DerivedClampReadTests' setup).
    Compared: outcome, driver log, fixture state (the SETs written), cache, the getter attributes
    (pins and the established set among them) and the live-power state (with _board_policy)."""

    CASES = b2.DerivedClampReadTests.CASES
    setup = b2.DerivedClampReadTests.setup

    def run_side(self, side, action, rig_errors=()):
        return (*state.StateCase.run_side(self, side, action, rig_errors), state.live_attrs(side.gpu))

    def run_both(self, setup, kw, action):
        runs = [self.run_side(self.side(kind, setup, **kw), action) for kind in ("python", "native")]
        for name, python, core in zip(("outcome", "driver log", "fixture state", "cache", "attributes",
                                       "live power"), *runs, strict=True):
            self.assertEqual(python, core, f"Python and native {name} differ")
        return runs[0]

    def test_confirmed_clamp_is_the_named_policy(self):
        for name, (kw, maximum, requested, target) in self.CASES.items():
            with self.subTest(name):
                run = self.run_both(self.setup(kw, maximum, requested), kw,
                                    lambda g: g.set_power_policy(13, target))
                self.assertEqual(run[0], ("ok", (False, "policy 13 is set with its current-limit slider")))
                self.assertEqual(run[2]["writes"], [])
                self.assertEqual(run[4]["_current_limit_established"], {13})
                self.assertEqual(state.captures(run[1]), 2)

    def test_confirmed_clamp_delegates_with_allow_named(self):
        for name, (kw, maximum, requested, target) in self.CASES.items():
            with self.subTest(name):
                run = self.run_both(self.setup(kw, maximum, requested), kw,
                                    lambda g: g.set_power_policy(13, target, allow_named=True))
                amps = format(target / 1000, ".3f").rstrip("0").rstrip(".")
                self.assertEqual(run[0], ("ok", (True, f"Core current set to {amps} A; effective limit verified")))
                self.assertEqual(len(run[2]["writes"]), 1)
                self.assertEqual(run[4]["_power_policy_pins"], {13: target})
                self.assertEqual(run[4]["_current_limit_established"], {13})

    def test_unconfirmed_clamp_is_an_ordinary_row(self):
        # unnamed, so the policy list's own checks apply: its request lies above its maximum
        for name, (kw, maximum, requested, target) in self.CASES.items():
            for reason, power in (("definition version 0x31", {"version": 0x31}),
                                  ("request not above the maximum", {"power_request": state.POWER_LIMITS[2]})):
                with self.subTest(name=name, reason=reason):
                    run = self.run_both(self.setup(kw, maximum, requested, **power), kw,
                                        lambda g: g.set_power_policy(13, target, allow_named=True))
                    self.assertEqual(run[0], ("ok", (False, "policy 13: its request lies outside its own range")))
                    self.assertEqual(run[2]["writes"], [])
                    self.assertEqual(run[4]["_current_limit_established"], set())
                    self.assertEqual(run[4]["_power_policy_pins"], {})


# ---- _reapply_power_policy_pins ------------------------------------------------------------------

def reapply(board_mw=None):
    return lambda side: side.gpu._reapply_power_policy_pins(board_mw)


class ReapplyTests(WriteCase):
    def test_no_pins(self):
        run = self.both(reapply())
        self.assertEqual(run[0], ("ok", (True, "")))
        self.assertEqual(run[1], [])

    def test_pins_all_matching(self):
        run = self.both(reapply(), setup=b2.pins({4: 143000, 8: 17000, 6: 1, 31: 5}))
        self.assertEqual(run[0], ("ok", (True, "")))
        self.assertEqual(self.commands(run), [INFO_GET, CONTROL_GET, STATUS_GET])
        self.assertEqual(run[2]["pins"], {4: 143000, 8: 17000, 6: 1, 31: 5})

    def test_one_recalculated(self):
        run = self.both(reapply(), setup=b2.chain(b2.pins({4: 150000, 8: 17000}), requests(p4=155000)))
        self.assertEqual(run[0], ("ok", (True, "the driver recalculated policies 4; re-applied your values")))
        self.assertEqual(self.sets(run), [(4, 150000)])
        self.assertEqual(self.commands(run), [INFO_GET, CONTROL_GET, STATUS_GET,
                                              INFO_GET, CONTROL_GET, STATUS_GET, SET,
                                              INFO_GET, CONTROL_GET, STATUS_GET])

    def test_after_a_board_write(self):
        def board_write(side):
            side.card.request[2] = 290000
            side.card.derive()
        run = self.both(reapply(), setup=b2.chain(b2.pins({9: 20000, 4: 150000}), board_write))
        self.assertEqual(run[0], ("ok", (True, "the driver recalculated policies 4, 9; re-applied your values")))
        self.assertEqual(self.sets(run), [(4, 150000), (9, 20000)])

    def test_failures_join_every_message(self):
        run = self.both(act(fail(SET, 1, 3), call=reapply()),
                        setup=b2.chain(b2.pins({4: 150000, 5: 150000, 8: 20000}), requests(p4=155000, p5=155000)))
        self.assertEqual(run[0], ("ok", (False, "the driver recalculated policies 4, 5, 8; re-applying your values "
                                                f"FAILED: policy 4: {REFUSED}; {RESTORED}; "
                                                f"policy 5: {REFUSED}; {RESTORED}")))
        self.assertEqual(self.sets(run), [(4, 150000), (4, 155000), (5, 150000), (5, 155000), (8, 20000)])
        self.assertEqual(run[2]["pins"], {4: 150000, 5: 150000, 8: 20000})

    def test_long_message_is_fetched_whole(self):
        pins = dict.fromkeys((3, 4, 5, 7, 8, 9), 2)

        def small_buffer(side):
            if side.kind == "native":
                side.gpu._POLICY_MESSAGE = 64
        run = self.both(act(small_buffer, fail(SET, *range(1, 40)), call=reapply()), setup=b2.pins(pins))
        self.assertEqual(run[0][0], "ok")
        self.assertGreater(len(run[0][1][1]), 64)
        self.assertTrue(run[0][1][1].endswith(f"policy 9: {REFUSED}; {UNVERIFIED}{REFUSED}"), run[0])

    def test_long_message_fetch_repeats_under_the_lock(self):
        # the kept text outgrows the fetch buffer once (as another call replacing it would make it):
        # the fetch runs again with the larger size, and every fetch runs with self._lock held
        pins = dict.fromkeys((3, 4, 5, 7, 8, 9), 2)
        library, fetches = native.load(), []
        real = library.dc_cl_policy_message_get

        def growing(handle, out, capacity, needed):
            held = []
            probe = threading.Thread(target=lambda: held.append(not self.gpu_lock.acquire(blocking=False)))
            probe.start()
            probe.join()
            status = real(handle, out, capacity, needed)
            fetches.append((capacity, needed._obj.value, held[0]))
            if len(fetches) == 1:
                needed._obj.value += 100
            return status

        def small_buffer(side):
            if side.kind == "native":
                side.gpu._POLICY_MESSAGE = 64
                self.gpu_lock = side.gpu._lock
                patcher = mock.patch.object(library, "dc_cl_policy_message_get", growing)
                patcher.start()
                self.addCleanup(patcher.stop)
        run = self.both(act(small_buffer, fail(SET, *range(1, 40)), call=reapply()), setup=b2.pins(pins))
        full = run[0][1][1]
        self.assertEqual([capacity for capacity, _, _ in fetches], [len(full) + 1, len(full) + 101])
        self.assertEqual([held for _, _, held in fetches], [True, True])
        self.assertTrue(full.endswith(f"policy 9: {REFUSED}; {UNVERIFIED}{REFUSED}"), run[0])

    def test_board_identified_by_board_mw(self):
        run = self.both(reapply(260000), setup=b2.chain(b2.pins({2: 250000, 4: 150000}), requests(p4=155000)))
        self.assertEqual(run[0], ("ok", (True, "the driver recalculated policies 4; re-applied your values")))
        self.assertEqual(run[2]["pins"], {4: 150000})
        self.assertEqual(run[2]["board_policy"], 2)
        self.assertEqual(self.sets(run), [(4, 150000)])
        # a board pin that already matches is dropped too; nothing else to do
        run = self.both(reapply(260000.0), setup=b2.pins({2: 260000}))
        self.assertEqual(run[0], ("ok", (True, "")))
        self.assertEqual((run[2]["pins"], run[2]["board_policy"]), ({}, 2))

    def test_board_stored_without_a_pin_on_it(self):
        # policy 2 is the unique mW row requesting 260000: it is the board whether or not it is pinned
        run = self.both(reapply(260000), setup=b2.pins({4: 150000}))
        self.assertEqual(run[0], ("ok", (True, "the driver recalculated policies 4; re-applied your values")))
        self.assertEqual((run[2]["board_policy"], run[2]["pins"]), (2, {4: 150000}))
        self.assertEqual(self.sets(run), [(4, 150000)])

    def test_two_board_candidates(self):
        run = self.both(reapply(5001000), setup=b2.pins({14: 4000000}))
        self.assertEqual(run[0], ("ok", (True, "the driver recalculated policies 14; re-applied your values")))
        self.assertEqual(run[2]["pins"], {14: 4000000})
        self.assertIsNone(run[2]["board_policy"])

    def test_no_board_candidate(self):
        for reading in (123, "260000", 260000.5):
            with self.subTest(reading=reading):
                run = self.both(reapply(reading), setup=b2.chain(b2.pins({2: 250000}), b2.seen(4)))
                self.assertEqual(run[0], ("ok", (True, "the driver recalculated policies 2; re-applied your values")))
                self.assertEqual((run[2]["pins"], run[2]["board_policy"]), ({2: 250000}, 4))

    def test_table_read_failing(self):
        run = self.both(act(fail(INFO_GET, 1), call=reapply()), setup=b2.pins({4: 150000}))
        self.assertEqual(run[0], ("ok", (False, f"your policy values could not be checked: {REFUSED}")))

    def test_mask_empty(self):
        run = self.both(reapply(260000), setup=b2.pins({4: 150000}), table=[], coupled=(), board=None)
        self.assertEqual(run[0], ("ok", (False, "your policy values could not be checked: the policy mask is empty")))
        self.assertEqual(run[2]["pins"], {4: 150000})


# ---- the short RM26 CONTROL layout: the selector in word 0 ---------------------------------------

class Rm26LayoutTests(WriteCase):
    LIMIT_WORD = (4 + 31 * 52) // 4 + 1

    def raw_sets(self, run):
        return [entry[2] for entry in run[1] if entry[1][14] == Rm26Card.SET]

    def test_writer_selects_policy_31_in_word_0(self):
        run = self.both(write(31, 64000), **RM26_KW)
        self.assertEqual(run[0], ("ok", (True, "policy 31 set to 64 W; effective limit 64 W")))
        self.assertEqual(self.sets(run), [(31, 64000)])
        (params,) = self.raw_sets(run)
        self.assertEqual((params[0], params[self.LIMIT_WORD], len(params) * 4), (1 << 31, 64000, 2108))
        self.assertEqual(params[4], (4 * 17 + 31) & 0xFFFFFFFF)  # word 4 is policy 0's opaque data here
        self.assertEqual(self.commands(run), [INFO_GET, CONTROL_GET, STATUS_GET, SET,
                                              INFO_GET, CONTROL_GET, STATUS_GET])

    def test_restoration_replays_on_word_0(self):
        run = self.both(act(ignore_sets(1), call=write(31, 64000)), **RM26_KW)
        self.assertEqual(run[0], ("ok", (False, f"policy 31: control read-back disagrees; {RESTORED}")))
        self.assertEqual(self.sets(run), [(31, 64000), (31, 63000)])
        self.assertEqual([params[0] for params in self.raw_sets(run)], [1 << 31, 1 << 31])

    def test_set_power_policy_pins_policy_31(self):
        run = self.both(setter(31, 64000), **RM26_KW)
        self.assertEqual(run[0], ("ok", (True, "policy 31 set to 64 W; effective limit 64 W")))
        self.assertEqual((run[2]["pins"], run[2]["board_policy"]), ({31: 64000}, 2))

    def test_reapply_writes_the_pin_on_policy_31(self):
        run = self.both(reapply(), setup=b2.pins({31: 64000}), **RM26_KW)
        self.assertEqual(run[0], ("ok", (True, "the driver recalculated policies 31; re-applied your values")))
        self.assertEqual(self.sets(run), [(31, 64000)])
        self.assertEqual([params[0] for params in self.raw_sets(run)], [1 << 31])
        self.assertEqual(run[2]["pins"], {31: 64000})


if __name__ == "__main__":
    unittest.main()
