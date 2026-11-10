# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Stock and Max all over the full policy list, and the native effective board limit, increment B4:
nvbackend.GPU.stock_power_policy, max_all_power_policies and read_native_power_limit_mw against
the core (dc_pl_stock_power_policy, dc_pl_max_all, dc_pl_native_power_limit).

The Python originals are the real nvbackend.GPU methods on a twin of the same fake driver; every
piece they call is real on both sides (read_power_policies, _write_policy_limit,
set_current_limit_ma, set_power_limit_mw, _set_power_limit_nvml, _reapply_power_policy_pins,
read_live_power_request). Two rigs:

1. The policy rig: tests.test_power_policies.PolicyCard (the TITAN RTX table as measured, the
   driver re-deriving the coupled policies from the board limit on every board write) behind the
   current-limit escape, as B2/B3's rig, plus NVML's configured-limit getter and setter over the
   card's board request (tests.test_native_power_limit.NvmlPower) on BOTH sides: here the core
   reads the board limit itself, where B2/B3's rig patches the binding's read_power_limit_mw. The
   twin has no client-power interface, so with XOC on (whose envelope asks for a live read) the
   two driver logs differ by those failed live-read attempts; such a case compares everything
   else (MaxAllPolicyRigTests.test_max_all_with_xoc_raises_a_current_to_its_maximum).
2. The live rig: tests.test_native_power_limit.LiveCase with LiveSetRig(PeerCoupledGpu): the
   client-power wire model (board at native index 29, peer 7 in mW and peer 2 in mA in the same
   native table) with NVML over its request, for the extended board request, the partial NVML
   capability, XOC and read_native_power_limit_mw.

Every policy-rig case compares on both sides the outcome (with its full text, or the exception),
the driver log (each escape's words), the NVML log (each getter read and each setter write), the
pins, GPU._board_policy, GPU._last_policy_table, the static pl_* keys and the card's requests.
Every live-rig case (PolicyLiveCase) compares LiveCase's result, NVML log, GDI calls, driver calls,
board request and live state (last observation and error, GPU._board_policy, static pl_* keys,
pins), and also GPU._last_policy_table and the last current-limit rows. The rows are compared as
`rows or []`: before any current-limit read the twin has no _current_limit_last_rows attribute
(None here) while native.Gpu's property reads the core's empty list; every consumer reads it with
`or []` (nvbackend.stock_power_policy's label lookup, max_all_power_policies' currents).

Recorded divergences (asserted below where reachable):
- A policy that is not a plain int within int64 is matched to the index 0..31 equal to it (else
  -1) for the pin drop and the row lookups; str(policy) is formatted before the call. Its text
  travels as UTF-8 with surrogatepass and every returned text is read by its length, so a NUL or
  a lone surrogate in it comes back as nvbackend formats it (not a divergence; asserted below).
- max_all_power_policies runs Python's steps over the native methods when pl_max_mw is present and
  not a plain int within int64 (the A8 static rule would make it None to the core).
"""
import ctypes
import struct
import unittest

import tests.test_native_current_limits as base
from druta import client_power_runtime as cp
from druta import native
from druta import nvbackend as NV
from tests.test_native_client_power_set import COUPLED_ACCEPTED, PeerCoupledGpu
from tests.test_native_power_limit import (BACKEND_STATIC, GET, PL, SET_LIMIT, LiveCase, LiveSetRig, NvmlPower,
                                           faults, has_command, nvml_writes, set_static, static_of)
from tests.test_native_power_policies import FIELDS, STATICS
from tests.test_native_power_policy_write import Policy, fail, ignore_sets
from tests.test_power_policies import A618, A619, A61A, COUPLED, E61B, TITAN, BOARD, PolicyCard, policy_gpu

if not native.available():
    raise unittest.SkipTest("native core not built (native/build.sh linux)")


def outcome(call):
    try:
        return ("ok", call())
    except Exception as exc:  # noqa: BLE001 - compared on both sides
        return (type(exc).__name__, str(exc))


def table_with(**rows):
    """TITAN with some rows replaced: p13=(type, channel, unit, min, default, max)."""
    out = []
    for row in TITAN:
        key = f"p{row[0]}"
        out.append((row[0], *rows[key]) if key in rows else row)
    return out


# ---- rig 1: the policy card --------------------------------------------------------------------------

class BoardModel:
    """NvmlPower's model over the card's board request (PolicyCard.nvml_set's re-derivation)."""

    def __init__(self, card):
        self.card = card

    @property
    def requested(self):
        return self.card.request[self.card.board]

    def _update(self, value):
        self.card.request[self.card.board] = value
        self.card.derive()  # measured: even for the same value


class PolicySide:
    def __init__(self, kind, table=TITAN, coupled=COUPLED, board=BOARD, static=STATICS):
        self.kind = kind
        self.card = PolicyCard(table, coupled, board)
        self.power = NvmlPower(lambda: BoardModel(self.card))
        twin = policy_gpu(self.card)
        self.header = list(twin._capture_current_limit_transport.return_value[0])
        self.driver = base.Driver(self.card.escape)
        self.rig = None
        if kind == "python":
            twin._capture_current_limit_transport.return_value = (list(self.header), dict(FIELDS))
            twin._legacy_clk_escape.side_effect = self.driver
            dll = type("Dll", (), {})()
            for name, function in self.power.python_dll().items():
                setattr(dll, name, function)
            twin.nvml.dll = dll
            twin.nvml.has = lambda name: hasattr(dll, name)
            twin.nvml.errstr = str  # the core's fake NVML has no error-string export: the number
            twin.static = {**twin.static, **{k: v for k, v in static.items()}}
            for key in PL:
                if key not in static:
                    twin.static.pop(key, None)
            self.gpu = twin
        else:
            self.rig = base.CLRig(NV.GPU.ARCH_TURING, self.driver)
            self.rig.nvml.exports.update(self.power.exports)
            self.gpu = self.rig.native_gpu()
            self.gpu.capabilities()
            set_static(self.gpu, static)
        self.gpu._current_limit_transport = (list(self.header), dict(FIELDS))

    def remove(self, name):
        if self.kind == "python":
            delattr(self.gpu.nvml.dll, name)

    def state(self):
        gpu = self.gpu
        return {"board_policy": getattr(gpu, "_board_policy", None),
                "table": getattr(gpu, "_last_policy_table", None),
                "pins": dict(getattr(gpu, "_power_policy_pins", None) or {}),
                "static": static_of(gpu), "requests": dict(self.card.request), "nvml": list(self.power.log)}

    def close(self):
        if self.kind == "native":
            self.gpu.close()
            native.set_export_resolver(None)


class PolicyCase(unittest.TestCase):
    def side(self, kind, setup, **kw):
        side = PolicySide(kind, **kw)
        self.addCleanup(side.close)
        try:
            side.gpu._power_policy_table()  # the layout choice is made: the logs start at the action
        except Exception:                                               # noqa: BLE001
            pass
        if setup is not None:
            setup(side)
        side.driver.log.clear()
        side.power.reset()
        return side

    def run_side(self, side, action):
        result = outcome(lambda: action(side))
        if side.rig is not None:
            self.assertEqual(side.rig.errors, [])
        return result, side.driver.log, side.state()

    def both(self, action, setup=None, **kw):
        runs = [self.run_side(self.side(kind, setup, **kw), action) for kind in ("python", "native")]
        for name, python, core in zip(("outcome", "driver log", "state"), *runs, strict=True):
            self.assertEqual(python, core, f"Python and native {name} differ")
        return runs[0]


def stock(policy):
    return lambda side: side.gpu.stock_power_policy(policy)


def max_all(side):
    return side.gpu.max_all_power_policies()


def then(*calls):
    """Each call's outcome in order (a failed one does not stop the next)."""
    def action(side):
        return [outcome(lambda c=c: c(side)) for c in calls]
    return action


def board(mw):
    return lambda side: side.gpu.set_power_limit_mw(mw)


def pin(policy, value):
    return lambda side: side.gpu.set_power_policy(policy, value)


def pins(mapping):
    def setup(side):
        side.gpu._power_policy_pins = dict(mapping)
    return setup


def writes(run):
    """(policy, limit word) of every policy SET in the driver log (the Turing control geometry)."""
    out = []
    for entry in run[1]:
        if entry[1][14] == E61B:
            params = entry[2]
            policy = params[4].bit_length() - 1
            out.append((policy, params[(0x14 + policy * 0xC4) // 4 + 1]))
    return out


class StockPolicyRigTests(PolicyCase):
    def test_stock_hands_a_coupled_policy_back_to_the_driver(self):
        run = self.both(then(board(290000), pin(4, 150000), stock(4)))
        self.assertEqual(run[0][1][2], ("ok", (True, "policy 4 is back to the driver's value: 155 W")))
        self.assertEqual(run[2]["requests"][4], 155000)
        self.assertEqual(run[2]["pins"], {})
        # the default is written, then the board limit re-sent through NVML
        self.assertEqual(writes(run)[-1], (4, 143000))
        self.assertEqual(nvml_writes(run[2]["nvml"]), [290000, 290000])

    def test_stock_of_a_policy_the_driver_does_not_derive_is_its_default(self):
        run = self.both(then(pin(14, 5000000), stock(14)))
        self.assertEqual(run[0][1][1], ("ok", (True, "policy 14 is back to the driver's value: 5001 W")))
        self.assertEqual(run[2]["requests"][14], 5001000)

    def test_stock_keeps_the_note_about_re_applied_values(self):
        run = self.both(then(board(290000), pin(4, 150000), pin(5, 150000), stock(4)))
        ok, message = run[0][1][3][1]
        self.assertTrue(ok, message)
        self.assertTrue(message.startswith("policy 4 is back to the driver's value: 155 W; "), message)
        self.assertIn("re-applied your values", message)
        self.assertEqual(run[2]["requests"][5], 150000)
        self.assertEqual(run[2]["pins"], {5: 150000})

    def test_stock_of_the_named_current_uses_its_label(self):
        run = self.both(then(lambda side: side.gpu.set_current_limit_ma(13, 300000), stock(13)))
        ok, message = run[0][1][1][1]
        self.assertTrue(ok, message)
        self.assertRegex(message, r"^\S.* is back to the driver's value: 350\.78 A$")
        self.assertFalse(message.startswith("policy 13"), message)

    def test_refusals(self):
        cases = {
            6: "policy 6 is not in the driver's table",
            40: "policy 40 is not in the driver's table",
            -1: "policy -1 is not in the driver's table",
            2 ** 70: f"policy {2 ** 70} is not in the driver's table",
            11: "policy 11: it has no range to write",
        }
        for policy, expected in cases.items():
            with self.subTest(policy=policy):
                run = self.both(stock(policy))
                self.assertEqual(run[0], ("ok", (False, expected)))
                self.assertEqual(writes(run), [])
                self.assertEqual(nvml_writes(run[2]["nvml"]), [])
        run = self.both(stock(2))  # the board: its own slider
        self.assertFalse(run[0][1][0])
        self.assertTrue(run[0][1][1].startswith("policy 2: "), run[0])

    def test_the_pin_is_dropped_first(self):
        # Python drops the pin before anything is validated: a refused stock still unpins. Pins on
        # policies the table refuses can exist (a profile, an older table): they are dropped too.
        held = {4: 150000, 5: 150000, 8: 20000, 6: 1000, 11: 2000, 2: 300000}
        cases = ((4, "a failed table read", None), (6, "not in the table", "policy 6 is not in the driver's table"),
                 (11, "no range", "policy 11: it has no range to write"), (2, "the board", None),
                 (8, "already at its default", None))
        for policy, reason, refusal in cases:
            with self.subTest(reason=reason):
                action = (then(lambda side: fail(A618, 1, 2, 3, 4)(side), stock(policy)) if policy == 4
                          else stock(policy))
                run = self.both(action, setup=pins(held))
                # after a board re-send the re-application also drops a pin on the board itself
                dropped = (policy, 2) if policy == 8 else (policy,)
                self.assertEqual(run[2]["pins"], {k: v for k, v in held.items() if k not in dropped})
                result = run[0][1][-1][1] if policy == 4 else run[0][1]
                if policy == 8:  # the board re-send re-applies the other pins the driver moved
                    self.assertTrue(result[1].startswith("policy 8 is back to the driver's value: 17 A; "), result)
                    self.assertIn((4, 150000), writes(run))
                    continue
                self.assertFalse(result[0], run[0])
                if refusal:
                    self.assertEqual(result[1], refusal)
                self.assertEqual(writes(run), [])
                self.assertEqual(nvml_writes(run[2]["nvml"]), [])

    def test_a_default_outside_its_own_range_is_refused(self):
        def setup(side):
            side.card.request[14] = 4000000  # the request itself is in range
        run = self.both(stock(14), setup=setup, table=table_with(p14=(0x0B, 11, 0, 1, 6000000, 5001000)))
        self.assertEqual(run[0], ("ok", (False, "policy 14: its default lies outside its own range; nothing written")))
        self.assertEqual(writes(run), [])

    def test_policies_that_are_not_plain_ints(self):
        # Python's lookups compare (4.0 == 4, True == 1) and pins.pop drops the equal key; its writer
        # then refuses the non-int after its own table read. str(policy) names it.
        cases = [(4.0, "4.0", {1: 7}, 2), (Policy.FOUR, "4", {1: 7}, 2), (True, "True", {4: 150000}, 2),
                 (4.5, "4.5", {4: 150000, 1: 7}, 1), ("4", "4", {4: 150000, 1: 7}, 1),
                 (None, "None", {4: 150000, 1: 7}, 1)]
        for policy, name, left, reads in cases:
            with self.subTest(policy=repr(policy)):
                run = self.both(stock(policy), setup=pins({4: 150000, 1: 7}))
                self.assertEqual(run[0], ("ok", (False, f"policy {name} is not in the driver's table")))
                self.assertEqual(run[2]["pins"], left)
                self.assertEqual([entry[1][14] for entry in run[1]], [A618, A61A, A619] * reads)
                self.assertEqual(writes(run), [])
        # Python's pins.pop hashes the policy only when a pin exists
        run = self.both(stock([4]))
        self.assertEqual(run[0], ("ok", (False, "policy [4] is not in the driver's table")))
        run = self.both(stock([4]), setup=pins({5: 150000}))
        self.assertEqual(run[0], ("TypeError", "unhashable type: 'list'"))
        self.assertEqual(run[2]["pins"], {5: 150000})

    def test_names_with_a_nul_or_a_lone_surrogate_come_back_whole(self):
        for policy in ("a\x00b", "\udcff", "x\x00\udcff\u00e9"):
            with self.subTest(policy=repr(policy)):
                run = self.both(stock(policy))
                self.assertEqual(run[0], ("ok", (False, f"policy {policy} is not in the driver's table")))

    def test_a_failed_pin_reapplication_is_reported(self):
        # the default and the board re-send succeed; re-applying pin 5 (the second policy SET) fails
        run = self.both(then(board(290000), pin(4, 150000), pin(5, 150000),
                             lambda side: fail(E61B, 2)(side), stock(4)))
        self.assertEqual(run[0][1][4], ("ok", (False, "policy 4 is back to the driver's value: 155 W; the driver "
                                                      "recalculated policies 5; re-applying your values FAILED: "
                                                      "policy 5: policy request failed (NTSTATUS 0, RM 0x1); "
                                                      "original limit restored and verified")))

    def test_a_failed_policy_read(self):
        run = self.both(then(lambda side: fail(A61A, 1, 2, 3, 4, 5, 6)(side), stock(4)))
        self.assertFalse(run[0][1][1][1][0])
        self.assertEqual(writes(run), [])

    def test_a_failed_default_write_is_reported(self):
        run = self.both(then(board(290000), pin(4, 150000), lambda side: fail(E61B, 1)(side), stock(4)))
        ok, message = run[0][1][3][1]
        self.assertFalse(ok)
        self.assertTrue(message.startswith("policy 4: policy request failed"), message)
        self.assertEqual(nvml_writes(run[2]["nvml"]), [290000])  # no board re-send

    def test_a_failed_board_resend_is_reported(self):
        def failing_write(side):
            side.power.write_status = 4
        run = self.both(then(board(290000), pin(4, 150000), failing_write, stock(4)))
        ok, message = run[0][1][3][1]
        self.assertFalse(ok)
        self.assertTrue(message.startswith("policy 4 is at its default, but re-sending the board limit failed: "
                                           "power limit failed: "), message)

    def test_an_unreadable_final_row(self):
        # the default and the board re-send succeed, the last table read fails
        def arm(side):
            fail(A618, 4, 5, 6, 7, 8, 9)(side)
        run = self.both(then(board(290000), pin(4, 150000), arm, stock(4)))
        self.assertEqual(run[0][1][3], ("ok", (True, "policy 4 is back to the driver's value: unreadable")))


class MaxAllPolicyRigTests(PolicyCase):
    def test_max_all_raises_every_limit_to_its_own_maximum(self):
        run = self.both(then(pin(4, 150000), pin(14, 4000000), max_all))
        status, steps = run[0][1][2]
        self.assertTrue(all(ok for _step, ok, _msg in steps), steps)
        self.assertEqual(steps[0], ("power limit", True, "power limit configured to 320 W"))
        self.assertEqual(run[2]["pins"], {})
        card_rows = {p: (lo, hi) for p, _t, _c, _u, lo, _d, hi in TITAN}
        for p, (lo, hi) in card_rows.items():
            if p not in (0, 11) and lo < hi:
                self.assertEqual(run[2]["requests"][p], hi, p)

    def test_max_all_leaves_read_only_rows_alone(self):
        def setup(side):
            side.card.control_type[14] = 0x07  # a record read-only here
        run = self.both(max_all, setup=setup, table=table_with(p14=(0x0B, 11, 0, 1, 4000000, 5001000)),
                        coupled=(3, 4, 5, 7, 8, 9))
        self.assertEqual(run[2]["requests"][14], 4000000)
        self.assertTrue(all(ok for _step, ok, _msg in run[0][1]), run[0])

    def test_max_all_without_xoc_stops_a_current_at_its_normal_maximum(self):
        # policy 13 with a maximum above the generation's normal cap: the slider's normal maximum
        run = self.both(then(lambda side: side.gpu.set_current_limit_ma(13, 200000), max_all),
                        table=table_with(p13=(0x0B, 19, 1, 1000, 350780, 900000)), coupled=(3, 4, 5, 7, 8, 9))
        status, steps = run[0][1][1]
        self.assertTrue(all(ok for _step, ok, _msg in steps), steps)
        self.assertLess(run[2]["requests"][13], 900000)
        self.assertGreater(run[2]["requests"][13], 200000)

    def test_max_all_with_xoc_raises_a_current_to_its_maximum(self):
        # XOC's envelope asks for a live read the twin cannot answer, so the driver logs differ by
        # those attempts; the outcome and every other state are compared
        def xoc(side):
            side.gpu.voltage_xoc_enabled = True
        runs = []
        for kind in ("python", "native"):
            side = self.side(kind, xoc, table=table_with(p13=(0x0B, 19, 1, 1000, 350780, 900000)),
                             coupled=(3, 4, 5, 7, 8, 9))
            result, _log, state = self.run_side(
                side, then(lambda side: side.gpu.set_current_limit_ma(13, 200000), max_all))
            runs.append((result, state))
        self.assertEqual(runs[0], runs[1], "Python and native differ")
        (_status, (_set, (_ok, steps))), state = runs[0]
        self.assertEqual(steps, [("power limit", True, "power limit configured to 320 W"),
                                 ("Core current", True, "Core current set to 900 A; effective limit verified")])
        self.assertEqual(state["requests"][13], 900000)
        self.assertEqual(state["pins"], {})

    def test_max_all_reaches_a_current_default_above_the_normal_cap(self):
        run = self.both(then(lambda side: side.gpu.set_current_limit_ma(13, 200000), max_all),
                        table=table_with(p13=(0x0B, 19, 1, 1000, 880000, 900000)), coupled=(3, 4, 5, 7, 8, 9))
        status, steps = run[0][1][1]
        self.assertTrue(all(ok for _step, ok, _msg in steps), steps)
        self.assertGreaterEqual(run[2]["requests"][13], 880000)

    def test_a_failed_policy_read_ends_the_steps(self):
        # the board limit is written through NVML; the one table read after it fails
        run = self.both(then(lambda side: fail(A618, *range(1, 10))(side), max_all))
        status, steps = run[0][1][1]
        self.assertEqual(steps[0], ("power limit", True, "power limit configured to 320 W"))
        self.assertEqual(steps[1][:2], ("power policies", False))
        self.assertTrue(steps[1][2], steps)
        self.assertEqual(len(steps), 2)

    def test_failed_and_ignored_writes_are_steps(self):
        # policies 14 and 15 below their maximum (not coupled): written directly, one SET ignored
        # (restored) and one refused
        table = table_with(p14=(0x0B, 11, 0, 1, 4000000, 5001000), p15=(0x0B, 11, 0, 1, 4000000, 5001000))
        run = self.both(then(lambda side: ignore_sets(1)(side), lambda side: fail(E61B, 3)(side), max_all),
                        table=table)
        status, steps = run[0][1][2]
        self.assertEqual([(label, ok) for label, ok, _ in steps],
                         [("power limit", True), ("policy 14", False), ("policy 15", False)])
        self.assertEqual(steps[1][2], "policy 14: control read-back disagrees; original limit restored and verified")
        self.assertTrue(steps[2][2].startswith("policy 15: policy request failed"), steps)
        self.assertEqual(writes(run), [(14, 5001000), (14, 4000000), (15, 5001000), (15, 4000000)])

    def test_static_tops(self):
        # None: no board step; a whole float or a fraction, a bool: Python's steps over the binding
        for top in (None, 0, 320000.0, 300000.5, True, 2 ** 64, -5):
            with self.subTest(top=top):
                static = {**STATICS, "pl_max_mw": top}
                run = self.both(max_all, static=static)
                status, steps = run[0]
                self.assertEqual(status, "ok", run[0])
                if top in (None, 0):
                    self.assertNotEqual(steps[:1] and steps[0][0], "power limit")
        run = self.both(max_all, static={"pl_min_mw": 100000, "pl_def_mw": 260000})
        self.assertNotEqual(run[0][1][0][0], "power limit")



class AbiTests(PolicyCase):
    """The kept texts, the step entries and the argument refusals of the B4 entries."""

    def native(self, **kw):
        side = self.side("native", None, **kw)
        return side, side.gpu

    def test_kept_texts_equal_the_returned_ones(self):
        side, gpu = self.native(table=table_with(p14=(0x0B, 11, 0, 1, 4000000, 5001000)))
        ok, message = gpu.stock_power_policy(4)
        library, needed = native.load(), ctypes.c_size_t()
        buffer = ctypes.create_string_buffer(8192)
        self.assertEqual(library.dc_pl_message_get(gpu._handle, buffer, len(buffer), ctypes.byref(needed)), 0)
        self.assertEqual(buffer.value.decode(), message)
        steps = gpu.max_all_power_policies()
        self.assertEqual([label for label, _ok, _text in steps], ["power limit", "policy 14"])
        for k, (label, _ok, text) in enumerate(steps):
            for field, expected in ((0, label), (1, text)):
                status = library.dc_pl_step_text_get(gpu._handle, k, field, buffer, len(buffer), ctypes.byref(needed))
                self.assertEqual((status, buffer.value.decode(), needed.value),
                                 (0, expected, len(expected.encode()) + 1))
                # the binding's fetch of a text that outgrew the entry's buffer
                self.assertEqual(gpu._pl_step_text(k, field, b"", needed.value, 1), expected)
        self.assertEqual(library.dc_pl_step_text_get(gpu._handle, len(steps), 0, buffer, len(buffer),
                                                     ctypes.byref(needed)), native.DC_E_RANGE)
        self.assertEqual(library.dc_pl_step_text_get(gpu._handle, 0, 2, buffer, len(buffer), ctypes.byref(needed)),
                         native.DC_E_ARGUMENT)
        # the board step inside max-all is set()'s text, kept as dc_pl_set's
        self.assertEqual(library.dc_pl_message_get(gpu._handle, buffer, len(buffer), ctypes.byref(needed)), 0)
        self.assertEqual(buffer.value.decode(), steps[0][2])
        value, error = gpu.read_native_power_limit_mw()
        self.assertIsNone(value)
        self.assertEqual(library.dc_pl_message_get(gpu._handle, buffer, len(buffer), ctypes.byref(needed)), 0)
        self.assertEqual(buffer.value.decode(), error)

    def test_truncated_entries_and_the_count(self):
        side, gpu = self.native(table=table_with(p14=(0x0B, 11, 0, 1, 4000000, 5001000)))
        library = native.load()
        statics = gpu._pl_statics()
        entries = (native._PolicyStep * 1)()
        entries[0].size = ctypes.sizeof(native._PolicyStep)
        count = ctypes.c_uint32()
        status = library.dc_pl_max_all(gpu._handle, ctypes.byref(statics), 0, entries, 1, ctypes.byref(count))
        self.assertEqual((status, count.value), (0, 2))  # the total; only the first entry written
        self.assertEqual(entries[0].label, b"power limit")
        self.assertEqual(entries[0].message_needed, len(entries[0].message) + 1)

    def test_argument_refusals(self):
        side, gpu = self.native()
        library = native.load()
        statics, bad = gpu._pl_statics(), gpu._pl_statics()
        bad.size = 0
        ok, needed, count = ctypes.c_int32(), ctypes.c_size_t(), ctypes.c_uint32()
        has, value = ctypes.c_int32(), ctypes.c_uint32()
        buffer = ctypes.create_string_buffer(64)
        entries = (native._PolicyStep * 2)()
        entries[0].size = ctypes.sizeof(native._PolicyStep)  # the second entry's size is unset
        refusals = [
            library.dc_pl_stock_power_policy(None, ctypes.byref(statics), 4, 1, b"4", 1, ctypes.byref(ok), buffer,
                                             len(buffer), ctypes.byref(needed)),
            library.dc_pl_stock_power_policy(gpu._handle, ctypes.byref(bad), 4, 1, b"4", 1, ctypes.byref(ok), buffer,
                                             len(buffer), ctypes.byref(needed)),
            library.dc_pl_stock_power_policy(gpu._handle, ctypes.byref(statics), 4, 1, None, 1, ctypes.byref(ok),
                                             buffer, len(buffer), ctypes.byref(needed)),
            library.dc_pl_stock_power_policy(gpu._handle, ctypes.byref(statics), 4, 1, b"4", 1, ctypes.byref(ok),
                                             None, 8, ctypes.byref(needed)),
            library.dc_pl_max_all(gpu._handle, ctypes.byref(statics), 0, entries, 2, ctypes.byref(count)),
            library.dc_pl_max_all(gpu._handle, ctypes.byref(statics), 0, None, 1, ctypes.byref(count)),
            library.dc_pl_max_all(gpu._handle, ctypes.byref(bad), 0, entries, 1, ctypes.byref(count)),
            library.dc_pl_native_power_limit(gpu._handle, ctypes.byref(statics), None, ctypes.byref(value), buffer,
                                             len(buffer), ctypes.byref(needed)),
            library.dc_pl_native_power_limit(gpu._handle, ctypes.byref(statics), ctypes.byref(has),
                                             ctypes.byref(value), None, 4, ctypes.byref(needed)),
        ]
        self.assertEqual(refusals, [native.DC_E_ARGUMENT] * len(refusals))
        # nothing was written to the driver or the NVML setter
        self.assertEqual(writes((None, side.driver.log)), [])
        self.assertEqual(nvml_writes(side.power.log), [])


# ---- rig 2: the live rig -----------------------------------------------------------------------------

PEER_MW = 7
PEER_INFO_MAX = 204 + PEER_MW * 260 + 16  # the native INFO max word of peer 7 (cp.NATIVE_INFO geometry)


def coupled(configure=None, **kw):
    return LiveSetRig(PeerCoupledGpu, accepted=COUPLED_ACCEPTED, recapture=True, configure=configure, **kw)


def widen(fake):
    """Peer 7's own maximum above the request the driver derives for a raised board (90 W)."""
    struct.pack_into("<I", fake.native_info, PEER_INFO_MAX, 100000)


def extended(fake):
    widen(fake)
    fake._update(800000)


def chain(*configures):
    def configure(fake):
        for c in configures:
            if c:
                c(fake)
    return configure


def live_steps(*calls):
    def action(side, power):
        return [outcome(lambda c=c: c(side.gpu, power)) for c in calls]
    return action


def live_stock(policy):
    return lambda gpu, _power: gpu.stock_power_policy(policy)


def live_pin(policy, value):
    return lambda gpu, _power: gpu.set_power_policy(policy, value)


def live_max_all(gpu, _power):
    return gpu.max_all_power_policies()


def native_limit(gpu, _power):
    return gpu.read_native_power_limit_mw()


class PolicyLiveCase(LiveCase):
    """LiveCase, also comparing GPU._last_policy_table and the last current-limit rows."""

    def run_both(self, rig, action, **kw):
        tables = []

        def recorded(side, power):
            try:
                return action(side, power)
            finally:
                gpu = side.gpu
                tables.append((getattr(gpu, "_last_policy_table", None),
                               getattr(gpu, "_current_limit_last_rows", None) or []))
        out = super().run_both(rig, recorded, **kw)
        self.assertEqual(len(tables), 2)
        self.assertEqual(tables[0], tables[1], "Python and native policy table or current rows differ")
        return out


def policy_writes(fake_calls):
    return [request for command, request in fake_calls if command == 0x2080E61B]


class StockLiveTests(PolicyLiveCase):
    def test_extended_request_sets_only_the_default_and_preserves_the_board(self):
        result, log, fake_calls, now, state = self.run_both(
            coupled(extended), live_steps(live_pin(PEER_MW, 82000), live_stock(PEER_MW)))
        ok, message = result[1][1]
        self.assertTrue(ok, message)
        self.assertEqual(message, "policy 7 is at its rated default: 80 W; board request preserved at 800 W; "
                                  "host-derived policy limits were not recomputed")
        self.assertEqual(now, 800000)
        self.assertEqual(nvml_writes(log), [])
        self.assertNotIn(PEER_MW, state["pins"])

    def test_extended_request_keeps_another_pin_without_reapplying_it(self):
        result, log, fake_calls, now, state = self.run_both(
            coupled(extended), live_steps(live_pin(PEER_MW, 82000), live_stock(PEER_MW)), pins={2: 52000})
        self.assertEqual(result[1], ("ok", (True, "policy 7 is at its rated default: 80 W; board request preserved "
                                                  "at 800 W; host-derived policy limits were not recomputed")))
        self.assertEqual(state["pins"], {2: 52000})
        self.assertEqual(nvml_writes(log), [])

    def test_the_board_to_preserve_is_the_live_observation(self):
        # NVML's setter missing: the live route. The board request moves before the k-th live read:
        # Python re-takes the board from the live observation where it differs from NVML's value
        cases = {
            1: (True, "policy 7 is at its rated default: 80 W; board request preserved at 330 W; "
                      "host-derived policy limits were not recomputed"),
            2: (False, "policy default not written: live board request is unreadable (Client-power definitions "
                       "or requests changed between fresh reads)"),
            3: (False, "policy 7 default was sent, but the board request was not preserved exactly "
                       "(request changed)"),
        }
        for k, expected in cases.items():
            with self.subTest(k=k):
                def configure(fake, k=k):
                    fake.faults[(cp.INFO, k)] = lambda g, _r: g._update(330000)
                result, log, fake_calls, now, state = self.run_both(
                    coupled(configure), live_steps(live_stock(PEER_MW)), remove=(SET_LIMIT,))
                self.assertEqual(result, [("ok", expected)])
                self.assertEqual(now, 330000)

    def test_a_live_route_stock_back_fills_the_static_keys(self):
        # without NVML's getter the board read itself is live; without its setter the board is read
        # through NVML and the live route's own read is the first live one
        for missing in (GET, SET_LIMIT):
            with self.subTest(missing=missing):
                result, log, fake_calls, now, state = self.run_both(
                    coupled(), live_steps(live_stock(PEER_MW)), static={}, remove=(missing,))
                self.assertTrue(result[0][1][0], result)
                self.assertEqual(state["static"], {"pl_min_mw": 200000, "pl_def_mw": 321000, "pl_max_mw": 550000})

    def test_ordinary_request_resends_nvml(self):
        result, log, fake_calls, now, state = self.run_both(
            coupled(), live_steps(live_pin(PEER_MW, 82000), live_stock(PEER_MW)))
        ok, message = result[1][1]
        self.assertTrue(ok, message)
        self.assertEqual(message, "policy 7 is back to the driver's value: 80 W")
        self.assertEqual(nvml_writes(log), [321000])

    def test_partial_nvml_capability_stocks_without_rederivation(self):
        for missing in (GET, SET_LIMIT):
            with self.subTest(missing=missing):
                result, log, fake_calls, now, state = self.run_both(
                    coupled(), live_steps(live_pin(PEER_MW, 82000), live_stock(PEER_MW)), remove=(missing,))
                ok, message = result[-1][1]
                self.assertIn("rated default", message)
                self.assertIn("not recomputed", message)
                self.assertEqual(nvml_writes(log), [])

    def test_unreadable_extended_board_refuses_before_the_default(self):
        result, log, fake_calls, now, state = self.run_both(
            coupled(chain(extended, faults(((cp.INFO, 1), {"rm": 0x56})))), live_steps(live_stock(PEER_MW)))
        ok, message = result[0][1]
        self.assertFalse(ok)
        self.assertTrue(message.startswith("policy default not written: live board request is unreadable ("), message)
        self.assertEqual(policy_writes(fake_calls), [])

    def test_a_changed_board_request_is_reported(self):
        def configure(fake):
            extended(fake)
            put = fake.native_control
            at = cp.NATIVE_CONTROL_BASE + PEER_MW * cp.NATIVE_CONTROL_STRIDE + 4
            struct.pack_into("<I", put, at, 82000)  # the default write is a real change
            fake.faults[(0x2080E61B, 1)] = {}
        moved = {}

        def change_after_write(side, power):
            fake = power.model()
            seen = fake.counts[cp.INFO]
            moved["at"] = seen
            fake.faults[(cp.INFO, seen + 3)] = lambda g, _request: g._update(700000)
            return side.gpu.stock_power_policy(PEER_MW)
        result, log, fake_calls, now, state = self.run_both(coupled(configure), change_after_write)
        status, (ok, message) = "ok", result
        self.assertFalse(ok)
        self.assertIn("default was sent, but the board request was not preserved exactly", message)
        self.assertEqual(nvml_writes(log), [])


    def test_a_board_that_reappears_without_a_setter_is_pythons_attribute_error(self):
        # no board readable at the start (NVML's getter fails, the live reads fail transiently), so
        # the default is written on the NVML route; the board then reads live, and re-sending it
        # needs the missing setter: Python's attribute access raises (its text names the dll object)
        def failing(power):
            power.read_status = 9

        def action(side, power):
            try:
                return side.gpu.stock_power_policy(PEER_MW)
            except AttributeError:
                return "AttributeError"
        rig = coupled(faults(*[((cp.INFO, k), {"nt": -1}) for k in range(1, 5)]))
        result, log, fake_calls, now, state = self.run_both(rig, action, nvml=failing, remove=(SET_LIMIT,))
        self.assertEqual(result, "AttributeError")
        self.assertEqual(nvml_writes(log), [])


class MaxAllLiveTests(PolicyLiveCase):
    def test_xoc_asks_for_the_live_envelope(self):
        result, log, fake_calls, now, state = self.run_both(coupled(), live_steps(live_max_all), xoc=True)
        status, steps = result[0]
        self.assertEqual(steps[0][0], "power limit")
        self.assertTrue(steps[0][2].startswith("power request configured to 1999 W"), steps[0])
        self.assertEqual(now, 1999000)
        self.assertEqual(state["pins"], {})

    def test_an_unreadable_envelope_keeps_the_nominal_top(self):
        result, log, fake_calls, now, state = self.run_both(
            coupled(faults(((cp.INFO, 1), {"rm": 0x56}), ((cp.INFO, 2), {"rm": 0x56}))),
            live_steps(live_max_all), xoc=True)
        status, steps = result[0]
        self.assertEqual(steps[0], ("power limit", True, "power limit configured to 550 W"))
        self.assertEqual(nvml_writes(log), [550000])

    def test_without_xoc_the_nominal_top(self):
        result, log, fake_calls, now, state = self.run_both(coupled(), live_steps(live_max_all))
        self.assertEqual(result[0][1][0], ("power limit", True, "power limit configured to 550 W"))
        self.assertFalse(has_command(fake_calls, cp.SET))


class NativeLimitTests(PolicyLiveCase):
    def test_the_bound_policys_effective_status(self):
        result, *_ = self.run_both(coupled(), live_steps(native_limit))
        self.assertEqual(result, [("ok", (321000, None))])

    def test_the_read_back_fills_the_static_keys(self):
        result, log, fake_calls, now, state = self.run_both(coupled(), live_steps(native_limit), static={})
        self.assertEqual(result, [("ok", (321000, None))])
        self.assertEqual(state["static"], {"pl_min_mw": 200000, "pl_def_mw": 321000, "pl_max_mw": 550000})

    def test_a_lower_effective_status(self):
        def lower(fake):
            fake.lower_pmu = 300000
        result, *_ = self.run_both(coupled(lower), live_steps(native_limit))
        self.assertEqual(result, [("ok", (300000, None))])

    def test_failures(self):
        def status_word(value):
            def mutate(raw):
                struct.pack_into("<I", raw, 0x9C + 29 * 0x1730 + 4, value)
            return mutate

        def retype(raw):
            raw[0x9C + 29 * 0x1730] ^= 0xFF
        changed = "native power definitions or configured requests changed during status read"
        cases = {
            "unbound": (faults(((cp.INFO, 1), {"rm": 0x56}), ((cp.INFO, 2), {"rm": 0x56})),
                        "Client-power command 0x2080a630 rejected (RM 0x56)"),
            "status refused": (faults(*[((cp.NATIVE_STATUS, k), {"rm": 0x56}) for k in range(1, 9)]),
                               "policy request failed (NTSTATUS 0, RM 0x56)"),
            "malformed": (faults(*[((cp.NATIVE_STATUS, k), {"payload": status_word(0xFFFFFFFF)})
                                   for k in range(1, 9)]),
                          "native effective power status is missing or malformed"),
            "retyped": (faults(*[((cp.NATIVE_STATUS, k), {"payload": retype}) for k in range(1, 9)]),
                        "native effective power status does not match the bound milliwatt policy"),
            # the board request moves during the table read: the CONTROL bytes differ from the live
            # observation's (and its request word too)
            "changed request": (faults(*[((cp.NATIVE_CONTROL, k), (lambda g, _r: g._update(333000)) if k == 3 else {})
                                         for k in range(1, 9)]), changed),
            # an INFO byte outside every record changes: only the byte comparison sees it
            "changed definition": (faults(*[((cp.NATIVE_INFO, k), {"payload": lambda raw: raw.__setitem__(-1, 7)})
                                            for k in range(3, 9)]), changed),
        }
        for name, (configure, expected) in cases.items():
            with self.subTest(name):
                result, *_ = self.run_both(coupled(configure), live_steps(native_limit))
                self.assertEqual(result, [("ok", (None, expected))])


if __name__ == "__main__":
    unittest.main()
