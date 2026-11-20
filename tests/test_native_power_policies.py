# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native full power-policy list, increment B2: nvbackend.GPU._policy_amount,
_power_policy_rows, power_policy_unwritable_reason and read_power_policies against the core.

Both sides answer from tests.test_power_policies.PolicyCard (the TITAN RTX table as measured,
and alternate tables built on the same model): the Python twin is that module's policy_gpu, the
core reaches the same card through tests.test_native_current_limits.CLRig. Every case builds a
FRESH card per side, runs the same setup and action, and compares

- the outcome: (rows, error), or the exception type and text;
- the driver log (each escape's size, header and parameter words, fields, NTSTATUS, RM);
- GPU._board_policy afterwards (the storage a unique board candidate is remembered in);
- GPU._last_policy_table afterwards (the cache read_power_policies relies on).

The board reading is read_power_limit_mw. Since increment A8 the binding's inputs callback calls
the native one (dc_pl_read: NVML, else the live request); this card serves no client-power
commands, so here the twin keeps its own (NVML answers from the card) and the core's is replaced
by the same reading of the card; cases that need NVML's limit unreadable, or a different one,
replace it on both sides alike. The real reader on both sides, NVML failing, working or missing,
runs on the live rig: tests.test_native_power_limit.PowerPoliciesReaderTests.

The derived clamp inside read_power_policies (get_current_limits' read_live_power_request branch)
runs on the A6 rig of tests.test_native_current_limit_state instead, whose fixture serves the
client-power commands in front of a current-limit table the confirmation can match.

Recorded divergences (asserted below as the binding's behaviour unless noted, not hidden):

- power_policy_unwritable_reason refuses what Python would compare or format loosely: bounds that
  are not plain ints within int64 (floats, bools, 2**64 + 5: TypeError / OverflowError, where
  Python compares them as given), a maybe_board that is not a str, holds a NUL or does not fit
  dc_policy_row's 96 bytes (ValueError, where Python formats it). It reads every key it passes
  (board, named_current, unit, minimum, maximum, consistent; maybe_board with .get as Python), in
  Python's order, so a missing key raises KeyError even where Python returns from an earlier check.
- read_power_policies and _power_policy_rows take a static pl_* value or a board reading whose
  int() or == raises as unequal to every driver word; Python raises from the comparison.
- _power_policy_rows on a layout with a negative offset: Python indexes from the end of the block,
  dc_power_layout's words are unsigned and the core reports IndexError.
- an allocation failure inside the core (the ABI guard) is a MemoryError with the core's fixed
  text from read_power_policies and _power_policy_rows; not exercised here.
"""
import unittest
from types import SimpleNamespace

import tests.test_native_current_limit_state as state
import tests.test_native_current_limits as base
from druta import native, nvbackend
from tests.test_power_policies import A619, BOARD, COUPLED, STATUS, TITAN, PolicyCard, policy_gpu

GPU = nvbackend.GPU
FIELDS = base.FIELDS
VOLTA = 5  # no current-limit policies: the profile is unsupported

UNREADABLE = "NVML's power limit is unreadable right now"
AMBIGUOUS = "its request equals the Power limit, as another policy's does"
STATICS = {"pl_min_mw": 100000, "pl_def_mw": 260000, "pl_max_mw": 320000}


class Side:
    """One side on a fresh PolicyCard: the Python twin or the core."""

    def __init__(self, kind, table=TITAN, coupled=COUPLED, board=BOARD, arch=GPU.ARCH_TURING, card=None):
        self.kind = kind
        self.card = (card or PolicyCard)(table, coupled, board)
        twin = policy_gpu(self.card, arch=arch)
        self.header = list(twin._capture_current_limit_transport.return_value[0])
        self.driver = base.Driver(self.card.escape)
        self.rig = None
        if kind == "python":
            twin._capture_current_limit_transport.return_value = (list(self.header), dict(FIELDS))
            twin._legacy_clk_escape.side_effect = self.driver
            self.gpu = twin
        else:
            self.rig = base.CLRig(arch, self.driver)
            self.gpu = self.rig.native_gpu()
            self.gpu.capabilities()
            self.gpu.static.get("pl_min_mw")  # merge the capability keys, then take the twin's
            self.gpu.static.update(STATICS)
            card = self.card
            self.gpu.read_power_limit_mw = lambda: card.request[card.board]
        self.gpu._current_limit_transport = (list(self.header), dict(FIELDS))

    def cache(self):
        return {"board_policy": getattr(self.gpu, "_board_policy", None),
                "table": getattr(self.gpu, "_last_policy_table", None)}

    def close(self):
        if self.kind == "native":
            self.gpu.close()


def outcome(fn):
    try:
        return ("ok", fn())
    except Exception as exc:  # noqa: BLE001
        return (type(exc).__name__, str(exc))


def chain(*steps):
    def run(side):
        for step in steps:
            step(side)
    return run


def statics(**values):
    """Set pl_* keys on both sides; the value ABSENT removes the key."""
    def setup(side):
        for key, value in values.items():
            if value is ABSENT:
                side.gpu.static.pop(key, None)
            else:
                side.gpu.static[key] = value
    return setup


ABSENT = object()
NO_STATICS = statics(pl_min_mw=None, pl_def_mw=None, pl_max_mw=None)


def board_mw(value):
    """read_power_limit_mw on both sides returns value (or raises it when it is an exception)."""
    def setup(side):
        def read():
            if isinstance(value, Exception):
                raise value
            return value
        side.gpu.read_power_limit_mw = read
    return setup


def established(*policies):
    def setup(side):
        side.gpu._current_limit_established = set(policies)
    return setup


def pins(mapping):
    def setup(side):
        side.gpu._power_policy_pins = dict(mapping)
    return setup


def read_twice(gpu):
    return gpu.read_power_policies()


def seen(policy):
    """GPU._board_policy as an earlier read (or the live power read) left it."""
    def setup(side):
        side.gpu._board_policy = policy
    return setup


class StatusTypeCard(PolicyCard):
    """PolicyCard whose status block can carry another type word for a policy (status_type), as
    control_type does for the control block."""

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.status_type = {}

    def escape(self, packet, fields):
        result = super().escape(packet, fields)
        if packet[14] == A619:
            for policy, word in self.status_type.items():
                packet[17 + (STATUS[0] + policy * STATUS[1]) // 4] = word
        return result


class PolicyCase(base.ParityCase):
    def side(self, kind, setup=None, **kw):
        side = Side(kind, **kw)
        self.addCleanup(side.close)
        if setup is not None:
            setup(side)
        side.driver.log.clear()
        return side

    def run_side(self, side, action):
        result = outcome(lambda: action(side))
        if side.rig is not None:
            self.assertEqual(side.rig.errors, [])
        return result, side.driver.log, side.cache()

    def both(self, action=lambda side: side.gpu.read_power_policies(), setup=None, **kw):
        runs = [self.run_side(self.side(kind, setup, **kw), action) for kind in ("python", "native")]
        for name, python, core in zip(("outcome", "driver log", "cache"), *runs):
            self.assertEqual(python, core, f"Python and native {name} differ")
        return runs[0]

    def rows(self, run):
        result = run[0]
        self.assertEqual(result[0], "ok", result)
        rows, error = result[1]
        self.assertIsNone(error)
        return {row["policy"]: row for row in rows}


# ---- _policy_amount ------------------------------------------------------------------------------

class PolicyAmountTests(unittest.TestCase):
    def test_edge_values_match_python(self):
        for value in (0, 1, 999, 1000, 1001, 1001000, 5001000, 20000, 1 << 32, -1, -999, -1000, -1001000,
                      (1 << 53), -(1 << 53)):
            for unit in ("mW", "mA", None, "W", 2):
                with self.subTest(value=value, unit=unit):
                    self.assertEqual(native.Gpu._policy_amount(value, unit), GPU._policy_amount(value, unit))

    def test_texts(self):
        self.assertEqual(native.Gpu._policy_amount(5001000, "mW"), "5001 W")
        self.assertEqual(native.Gpu._policy_amount(1, "mA"), "0.001 A")
        self.assertEqual(native.Gpu._policy_amount(0, None), "0 ?")

    def test_beyond_exact_doubles_is_refused(self):
        with self.assertRaises(ValueError):
            native.Gpu._policy_amount((1 << 53) + 1, "mW")
        with self.assertRaises(ValueError):
            native.Gpu._policy_amount(1.5, "mW")


# ---- read_power_policies on the measured table ---------------------------------------------------

class ReadTests(PolicyCase):
    def test_titan_table(self):
        run = self.both()
        rows = self.rows(run)
        self.assertEqual(sorted(rows), [p for p, *_ in TITAN])
        self.assertTrue(rows[2]["board"])
        self.assertTrue(rows[13]["named_current"])
        self.assertEqual({p for p, r in rows.items() if r["writable"]}, {0, 1, 3, 4, 5, 7, 8, 9, 14, 15})
        self.assertEqual(rows[2]["limit"], 268125)
        self.assertIsNone(run[2]["board_policy"])          # the statics told it apart: nothing stored
        self.assertIsNotNone(run[2]["table"])

    def test_second_read_reuses_the_state_read_and_issues_the_same_escapes(self):
        run = self.both(setup=lambda side: side.gpu.read_power_policies())
        self.assertEqual([entry[1][14] for entry in run[1]], [base.INFO, base.CONTROL, base.STATUS])

    def test_partial_mask(self):
        table = [row for row in TITAN if row[0] in (2, 4, 9, 13)]
        rows = self.rows(self.both(table=table, coupled=(4, 9, 13)))
        self.assertEqual(sorted(rows), [2, 4, 9, 13])
        self.assertTrue(rows[2]["board"])

    def test_partial_mask_without_the_named_policy(self):
        table = [row for row in TITAN if row[0] in (2, 4, 7)]
        run = self.both(table=table, coupled=(4, 7))
        rows = self.rows(run)
        self.assertEqual(sorted(rows), [2, 4, 7])
        self.assertFalse(any(r["named_current"] for r in rows.values()))

    def test_non_coupled_board(self):
        rows = self.rows(self.both(coupled=()))
        self.assertTrue(rows[2]["board"])

    def test_pinned_rows(self):
        rows = self.rows(self.both(setup=pins({4: 150000, 8: 20000, 31: 7})))
        self.assertEqual((rows[4]["pinned"], rows[8]["pinned"], rows[5]["pinned"]), (150000, 20000, None))

    def test_unit_two_is_none_and_read_only(self):
        table = TITAN + [(16, 0x07, 2, 2, 1, 50, 100)]
        rows = self.rows(self.both(table=table))
        self.assertIsNone(rows[16]["unit"])
        self.assertFalse(rows[16]["writable"])
        self.assertEqual(native.Gpu.power_policy_unwritable_reason(rows[16]),
                         "its unit is not one Druta understands")

    def test_inconsistent_type(self):
        def setup(side):
            side.card.control_type[4] = 0x05
        rows = self.rows(self.both(setup=setup))
        self.assertFalse(rows[4]["consistent"])
        self.assertFalse(rows[4]["writable"])

    def test_status_type_word(self):
        # the status half of `consistent`: status[state] & 255 == type (policy 4 is type 0x03)
        for name, word, consistent in (("differs, control matching", 0x04, False),
                                       ("high bits over the right type byte", 0x103, True),
                                       ("high bits over another type byte", 0x104, False)):
            with self.subTest(name):
                def setup(side, word=word):
                    side.card.status_type[4] = word
                rows = self.rows(self.both(setup=setup, card=StatusTypeCard))
                self.assertEqual(rows[4]["consistent"], consistent)
                self.assertEqual(rows[4]["writable"], consistent)

    def test_out_of_range_request(self):
        def setup(side):
            side.card.request[4] = 200000
            side.card.request[5] = 0
        rows = self.rows(self.both(setup=setup))
        self.assertFalse(rows[4]["in_range"] or rows[5]["in_range"])
        self.assertFalse(rows[4]["writable"] or rows[5]["writable"])


# ---- telling the board apart ---------------------------------------------------------------------

class BoardTests(PolicyCase):
    def test_pl_min_absent_none_equal_and_unequal(self):
        # absent counts as equal: the statics identify the board and nothing is remembered
        run = self.both(setup=statics(pl_min_mw=ABSENT))
        self.assertTrue(self.rows(run)[2]["board"])
        self.assertIsNone(run[2]["board_policy"])
        # None and an unequal value do not: the unique candidate at NVML's limit is remembered
        for value in (None, 99999):
            with self.subTest(pl_min_mw=value):
                run = self.both(setup=statics(pl_min_mw=value))
                self.assertTrue(self.rows(run)[2]["board"])
                self.assertEqual(run[2]["board_policy"], 2)
        run = self.both(setup=statics(pl_min_mw=100000))
        self.assertIsNone(run[2]["board_policy"])

    def test_pl_def_and_pl_max_absent_or_none_are_unequal(self):
        for key in ("pl_def_mw", "pl_max_mw"):
            for value in (None, ABSENT):
                with self.subTest(key=key, value=value):
                    run = self.both(setup=chain(statics(**{key: value}), board_mw(None)))
                    rows = self.rows(run)
                    self.assertFalse(any(r["board"] for r in rows.values()))
                    self.assertEqual({p for p, r in rows.items() if r["maybe_board"]},
                                     {p for p, r in rows.items() if r["unit"] == "mW"})

    def test_unique_candidate_is_remembered_and_matched_by_policy_next_read(self):
        def action(side):
            first = side.gpu.read_power_policies()
            # the request moves away from NVML's reading: the next read matches policy 2 by index
            side.card.request[2] = 300000
            return first, side.gpu.read_power_policies()
        run = self.both(action, setup=chain(NO_STATICS, board_mw(260000)))
        self.assertEqual(run[0][0], "ok")
        first, second = run[0][1]
        for rows, _error in (first, second):
            board = [r["policy"] for r in rows if r["board"]]
            self.assertEqual(board, [2])
            self.assertFalse(any(r["maybe_board"] for r in rows))
        self.assertEqual(run[2]["board_policy"], 2)

    def test_a_policy_already_seen_is_the_board(self):
        def setup(side):
            side.gpu._board_policy = 4
        run = self.both(setup=chain(NO_STATICS, board_mw(260000), setup))
        rows = self.rows(run)
        self.assertEqual([p for p, r in rows.items() if r["board"]], [4])
        self.assertEqual(run[2]["board_policy"], 4)

    def test_two_equal_requests_are_both_maybe_board(self):
        run = self.both(setup=chain(NO_STATICS, board_mw(5001000)))
        rows = self.rows(run)
        self.assertEqual({p for p, r in rows.items() if r["maybe_board"]}, {0, 14, 15})
        self.assertEqual({r["maybe_board"] for r in rows.values() if r["maybe_board"]}, {AMBIGUOUS})
        self.assertFalse(any(r["board"] or (r["writable"] and r["maybe_board"]) for r in rows.values()))
        self.assertIsNone(run[2]["board_policy"])
        self.assertEqual(native.Gpu.power_policy_unwritable_reason(rows[14]),
                         GPU.power_policy_unwritable_reason(rows[14]))

    def test_board_mw_unreadable_makes_every_mw_row_maybe_board(self):
        for reading in (None, RuntimeError("NVML gone")):
            with self.subTest(reading=reading):
                run = self.both(setup=chain(NO_STATICS, board_mw(reading)))
                rows = self.rows(run)
                mw = {p for p, r in rows.items() if r["unit"] == "mW"}
                self.assertEqual({p for p, r in rows.items() if r["maybe_board"] == UNREADABLE}, mw)
                self.assertFalse(any(r["writable"] for p, r in rows.items() if p in mw))
                self.assertIsNone(run[2]["board_policy"])

    def test_no_candidate_at_all(self):
        run = self.both(setup=chain(NO_STATICS, board_mw(123)))
        rows = self.rows(run)
        self.assertFalse(any(r["board"] or r["maybe_board"] for r in rows.values()))
        self.assertIsNone(run[2]["board_policy"])

    def test_float_and_bool_statics_alone_decide_the_board(self):
        # NVML's reading (the card's request, 260000) has a unique candidate, policy 2: a static
        # taken as unequal would remember it, so board_policy None shows the statics decided
        for name, values, board in (
                ("float pl_def", {"pl_def_mw": 260000.0}, 2),
                ("float pl_max", {"pl_max_mw": 320000.0}, 2),
                ("float pl_min", {"pl_min_mw": 100000.0}, 2),
                ("all three float", {"pl_min_mw": 100000.0, "pl_def_mw": 260000.0, "pl_max_mw": 320000.0}, 2),
                # policy 1 is (1, 1001000, 1001000) mW: True == 1 is its minimum
                ("bool pl_min", {"pl_min_mw": True, "pl_def_mw": 1001000, "pl_max_mw": 1001000}, 1),
                # policy 11 is (0, 0, 0) mW: False == 0 throughout
                ("bool statics", {"pl_min_mw": False, "pl_def_mw": False, "pl_max_mw": 0.0}, 11)):
            with self.subTest(name):
                run = self.both(setup=statics(**values))
                rows = self.rows(run)
                self.assertEqual([p for p, r in rows.items() if r["board"]], [board])
                self.assertIsNone(run[2]["board_policy"])

    def test_seen_policy_is_ignored_while_the_statics_identify_the_board(self):
        run = self.both(setup=seen(4))
        rows = self.rows(run)
        self.assertEqual([p for p, r in rows.items() if r["board"]], [2])
        self.assertTrue(rows[4]["writable"])
        self.assertEqual(run[2]["board_policy"], 4)  # left as it was

    def test_seen_policy_missing_from_the_mask_falls_through_to_the_candidate(self):
        run = self.both(setup=chain(NO_STATICS, board_mw(260000), seen(20)))
        rows = self.rows(run)
        self.assertEqual([p for p, r in rows.items() if r["board"]], [2])
        self.assertFalse(any(r["maybe_board"] for r in rows.values()))
        self.assertEqual(run[2]["board_policy"], 2)  # overwritten by the candidate pass

    def test_reader_that_changes_statics_or_the_seen_policy(self):
        # read_power_limit_mw's live fallback (read_live_power_request) can back-fill the pl_* keys
        # and set _board_policy; _power_policy_rows reads both only after it returns
        def filling(side):
            def read():
                side.gpu.static.update(STATICS)
                return 260000
            side.gpu.read_power_limit_mw = read

        def seeing(side):
            def read():
                side.gpu._board_policy = 4
                return 260000
            side.gpu.read_power_limit_mw = read
        for name, reader, board, remembered in (("fills the statics", filling, [2], None),
                                                ("sets the seen policy", seeing, [4], 4)):
            with self.subTest(name):
                run = self.both(setup=chain(NO_STATICS, reader))
                rows = self.rows(run)
                self.assertEqual([p for p, r in rows.items() if r["board"]], board)
                self.assertFalse(any(r["maybe_board"] for r in rows.values()))
                self.assertEqual(run[2]["board_policy"], remembered)

    def test_comparison_that_raises_is_unequal_natively(self):
        """RECORDED DIVERGENCE: a static pl_* value (or NVML's reading) whose == raises makes
        nvbackend's _power_policy_rows raise from the comparison; the binding takes it as unequal
        to every driver word, so the core decides the board without it."""
        class Unorderable:
            def __eq__(self, other):
                raise RuntimeError("compared")
            __hash__ = object.__hash__

        python = self.side("python", statics(pl_def_mw=Unorderable()))
        with self.assertRaisesRegex(RuntimeError, "compared"):
            python.gpu.read_power_policies()
        core = self.side("native", chain(statics(pl_def_mw=Unorderable()), board_mw(260000)))
        rows, error = core.gpu.read_power_policies()
        self.assertIsNone(error)
        self.assertEqual([r["policy"] for r in rows if r["board"]], [2])  # NVML's reading's candidate
        self.assertEqual(core.gpu._board_policy, 2)

    def test_non_integer_statics_and_reading(self):
        run = self.both(setup=chain(statics(pl_def_mw=260000.0, pl_max_mw="320000"), board_mw(260000.0)))
        rows = self.rows(run)
        self.assertTrue(rows[2]["board"])
        self.assertEqual(run[2]["board_policy"], 2)


# ---- refusals and failed reads -------------------------------------------------------------------

class FailureTests(PolicyCase):
    def test_profile_unsupported(self):
        run = self.both(arch=VOLTA)
        self.assertEqual(run[0], ("ok", ([], "current policies are not validated for this GPU generation")))
        self.assertEqual(run[1], [])

    def test_empty_mask(self):
        run = self.both(table=[], coupled=(), board=None)
        self.assertEqual(run[0], ("ok", ([], "the driver's policy mask is empty")))

    def test_table_read_failure_text(self):
        def setup(side):
            side.driver.refuse = lambda packet: 0x57 if packet[14] == base.INFO else None
        run = self.both(setup=setup)
        self.assertEqual(run[0][0], "ok")
        rows, error = run[0][1]
        self.assertEqual(rows, [])
        self.assertTrue(error)

    def test_named_from_established_only_after_a_failed_current_read(self):
        def setup(side):
            side.gpu._power_policy_table()  # the layout choice is made
            side.gpu._current_limit_established = {13}

        def action(side):
            # get_current_limits' info GET is refused once (an RM refusal keeps the transport, which
            # the core's rig could not capture again); read_power_policies' own table read succeeds
            refused = []

            def refuse(packet):
                if packet[14] == base.INFO and not refused:
                    refused.append(True)
                    return 0x57
                return None
            side.driver.refuse = refuse
            return side.gpu.read_power_policies()
        run = self.both(action, setup=setup)
        self.assertEqual([entry[1][14] for entry in run[1]][:4], [base.INFO, base.INFO, base.CONTROL, base.STATUS])
        rows = self.rows(run)
        self.assertTrue(rows[13]["named_current"])
        self.assertFalse(rows[13]["writable"])


# ---- _power_policy_rows on a given table, and the unwritable reasons -----------------------------

class RowsTests(PolicyCase):
    def table(self):
        side = self.side("python")
        side.gpu._power_policy_table()
        return side.gpu._last_policy_table

    def test_rows_of_a_given_table(self):
        layout, info, control, status = self.table()
        for named, reading, setup in ((frozenset(), None, None), ({13, 40, -1}, 260000, None),
                                      ({2}, None, NO_STATICS), (set(), 5001000, NO_STATICS),
                                      ((), 260000, NO_STATICS)):
            with self.subTest(named=named, reading=reading, statics=setup is not None):
                run = self.both(lambda side: side.gpu._power_policy_rows(layout, info, control, status, named,
                                                                         reading), setup=setup)
                self.assertEqual(run[0][0], "ok")
                self.assertEqual(run[1], [])

    def test_short_blocks_raise_index_error(self):
        layout, info, control, status = self.table()
        for blocks in ((info[:1], control, status), (info, control[:10], status), (info, control, status[:10])):
            with self.subTest(sizes=[len(block) for block in blocks]):
                run = self.both(lambda side, blocks=blocks: side.gpu._power_policy_rows(layout, *blocks))
                self.assertEqual(run[0], ("IndexError", "list index out of range"))
                self.assertIsNone(run[2]["board_policy"])

    @staticmethod
    def words(layout, policy):
        """(info meta, status state, control record) word indices of policy in layout."""
        return tuple((layout[name][0] + policy * layout[name][1]) // 4 for name in ("info", "status", "control"))

    def test_short_info_slice_none_blocks_and_no_sizes(self):
        layout, info, control, status = self.table()
        meta = self.words(layout, 0)[0]  # policy 0 is the first one the mask selects
        cases = [(f"info[:meta + {k}]", (layout, info[:meta + k], control, status),
                  ("ValueError", f"not enough values to unpack (expected 3, got {k - 2})")) for k in (2, 3, 4)]
        cases += [("control None", (layout, info, None, status),
                   ("TypeError", "'NoneType' object is not subscriptable")),
                  ("status None", (layout, info, control, None),
                   ("TypeError", "'NoneType' object is not subscriptable"))]
        for name, args, expected in cases:
            with self.subTest(name):
                run = self.both(lambda side, args=args: side.gpu._power_policy_rows(*args))
                self.assertEqual(run[0], expected)
                self.assertIsNone(run[2]["board_policy"])
        # _power_policy_rows never reads the layout's "sizes"
        bare = {key: value for key, value in layout.items() if key != "sizes"}
        with self.subTest("layout without sizes"):
            run = self.both(lambda side: side.gpu._power_policy_rows(bare, info, control, status))
            self.assertEqual(run[0][0], "ok")
            full = self.both(lambda side: side.gpu._power_policy_rows(layout, info, control, status))
            self.assertEqual(run[0], full[0])

    def test_short_block_past_a_unique_candidate_stores_nothing(self):
        # NO_STATICS and NVML's limit at policy 2's request: policy 2's row is read before policy
        # 13's record fails, so remembering would be reachable past the bounds check
        layout, info, control, status = self.table()
        meta, state_word, record = self.words(layout, 13)
        blocks = {"info[:meta + 1]": (info[:meta + 1], control, status),
                  "info[:meta + 4]": (info[:meta + 4], control, status),
                  "control[:record + 1]": (info, control[:record + 1], status),
                  "status[:state + 1]": (info, control, status[:state_word + 1]),
                  "status[:state + 2]": (info, control, status[:state_word + 2]),
                  "status None": (info, control, None)}
        full = self.both(lambda side: side.gpu._power_policy_rows(layout, info, control, status, (), 260000),
                         setup=NO_STATICS)
        self.assertEqual(full[2]["board_policy"], 2)  # the control: the full table remembers policy 2
        for name, args in blocks.items():
            with self.subTest(name):
                run = self.both(lambda side, args=args: side.gpu._power_policy_rows(layout, *args, (), 260000),
                                setup=NO_STATICS)
                self.assertIn(run[0][0], ("IndexError", "ValueError", "TypeError"))
                self.assertIsNone(run[2]["board_policy"])

    def test_negative_layout_offset_diverges(self):
        """RECORDED DIVERGENCE: a control offset of -4 * len(control) words reaches the same words
        from the end of the block in Python; the core's layout words are unsigned."""
        layout, info, control, status = self.table()
        shifted = dict(layout, control=(layout["control"][0] - 4 * len(control), layout["control"][1]))
        python = self.side("python")
        self.assertEqual(python.gpu._power_policy_rows(shifted, info, control, status),
                         python.gpu._power_policy_rows(layout, info, control, status))
        core = self.side("native")
        self.assertEqual(outcome(lambda: core.gpu._power_policy_rows(shifted, info, control, status)),
                         ("IndexError", "list index out of range"))

    def test_core_refuses_a_short_block_without_storing(self):
        # The binding walks the blocks first, so the core's own bounds check (DC_E_RANGE) is its
        # defensive fallback: called directly, it stores no candidate either.
        layout, info, control, status = self.table()
        record = self.words(layout, 13)[2]
        side = self.side("native", NO_STATICS)

        def words(block):
            return (native.ctypes.c_uint32 * len(block))(*block), len(block)
        (i, ni), (c, nc), (s, ns) = words(info), words(control[:record + 1]), words(status)
        out, count = side.gpu._policy_rows_buffer(), native.ctypes.c_uint32()
        result = native.load().dc_cl_policy_rows(
            side.gpu._handle, native.ctypes.byref(native._layout_struct(layout)), i, ni, c, nc, s, ns, 0,
            native.ctypes.byref(side.gpu._cl_statics_fill(native._ClStatics())), 1, 260000, out, len(out),
            native.ctypes.byref(count))
        self.assertEqual((result, count.value), (native.DC_E_RANGE, 0))
        self.assertIsNone(side.gpu._board_policy)

    def test_named_by_python_membership(self):
        layout, info, control, status = self.table()
        for named in ({True}, {13.0}, {1, 13.0}, [False, 2.0], frozenset({4.0})):
            with self.subTest(named=named):
                run = self.both(lambda side, named=named: side.gpu._power_policy_rows(layout, info, control,
                                                                                       status, named))
                self.assertEqual(run[0][0], "ok")
        rows = {r["policy"]: r for r in run[0][1]}
        self.assertTrue(rows[4]["named_current"])
        self.assertFalse(rows[4]["writable"])
        for named in ("x", "13"):
            with self.subTest(named=named):
                run = self.both(lambda side, named=named: side.gpu._power_policy_rows(layout, info, control,
                                                                                       status, named))
                self.assertEqual(run[0], ("TypeError", "'in <string>' requires string as left operand, not int"))
        # a generator is consumed by each `p in named` in turn, as in Python: 0 and 1 are found, 2
        # is found past 13, and the rest find it exhausted
        run = self.both(lambda side: side.gpu._power_policy_rows(layout, info, control, status,
                                                                 (p for p in (0, 1, 13, 2))))
        rows = {r["policy"]: r for r in run[0][1]}
        self.assertEqual({p for p, r in rows.items() if r["named_current"]}, {0, 1, 2})

    def test_unwritable_reasons_in_python_order(self):
        rows = self.rows(self.both(setup=chain(pins({4: 150000}), lambda side: side.card.control_type.update({5: 1}))))
        variants = []
        for row in rows.values():
            variants.append(row)
            for key, value in (("board", True), ("maybe_board", AMBIGUOUS), ("maybe_board", UNREADABLE),
                               ("named_current", True), ("unit", None), ("minimum", row["maximum"]),
                               ("consistent", False), ("unit", "mA")):
                variants.append({**row, key: value})
            for consistent in (True, False):
                variants.append({**row, "minimum": 2000, "maximum": 1000, "consistent": consistent})
        for row in variants:
            with self.subTest(row=row):
                self.assertEqual(native.Gpu.power_policy_unwritable_reason(row),
                                 GPU.power_policy_unwritable_reason(row))
        plain = {"board": False, "maybe_board": "", "named_current": False, "unit": "mW", "minimum": 2000,
                 "maximum": 1000, "consistent": False}
        self.assertEqual(native.Gpu.power_policy_unwritable_reason(plain), "it has no range to write")
        self.assertEqual(GPU.power_policy_unwritable_reason(plain), "it has no range to write")

    def test_unwritable_reason_missing_keys(self):
        plain = {"board": False, "maybe_board": "", "named_current": False, "unit": "mW", "minimum": 1,
                 "maximum": 2, "consistent": True}
        # maybe_board is read with .get on both sides; a key Python reaches raises KeyError on both
        for key in ("maybe_board", "board", "named_current", "unit", "minimum", "maximum", "consistent"):
            row = {k: v for k, v in plain.items() if k != key}
            with self.subTest(missing=key):
                self.assertEqual(outcome(lambda row=row: native.Gpu.power_policy_unwritable_reason(row)),
                                 outcome(lambda row=row: GPU.power_policy_unwritable_reason(row)))
        # RECORDED DIVERGENCE: a key Python never reaches (it returns from the board check first)
        row = {"board": True}
        self.assertEqual(GPU.power_policy_unwritable_reason(row), "the board limit is set with the Power limit slider")
        self.assertEqual(outcome(lambda: native.Gpu.power_policy_unwritable_reason(row)),
                         ("KeyError", "'named_current'"))

    def test_unwritable_reason_refuses_what_it_cannot_carry(self):
        """RECORDED DIVERGENCES: the binding refuses rows Python answers loosely.

        Python (nvbackend.GPU.power_policy_unwritable_reason) compares the bounds as given and
        formats any truthy maybe_board: minimum 1.2 / maximum 1.8 with consistent False gives "its
        record type differs between the driver's blocks"; minimum 2**64 + 5 / maximum 10 gives "it
        has no range to write"; maybe_board "\x00x" gives "it may be the board limit (\x00x)". dc_policy_row
        holds int64 bounds and a 96-byte NUL-terminated text, so the binding refuses instead of
        truncating the floats, wrapping the int or dropping the text."""
        plain = {"board": False, "maybe_board": "", "named_current": False, "unit": "mW", "minimum": 1,
                 "maximum": 10, "consistent": True}
        python = {"float bounds": "its record type differs between the driver's blocks",
                  "bool minimum": "its request lies outside its own range",
                  "minimum 2**64 + 5": "it has no range to write",
                  "maximum -2**63 - 1": "it has no range to write",
                  "NUL-led maybe_board": "it may be the board limit (\x00x)",
                  "96-byte maybe_board": "it may be the board limit (" + "y" * 96 + ")",
                  "non-str maybe_board": "it may be the board limit (5)"}
        cases = {"float bounds": ({"minimum": 1.2, "maximum": 1.8, "consistent": False}, "TypeError"),
                 "bool minimum": ({"minimum": True}, "TypeError"),
                 "minimum 2**64 + 5": ({"minimum": 2 ** 64 + 5}, "OverflowError"),
                 "maximum -2**63 - 1": ({"maximum": -(2 ** 63) - 1}, "OverflowError"),
                 "NUL-led maybe_board": ({"maybe_board": "\x00x"}, "ValueError"),
                 "96-byte maybe_board": ({"maybe_board": "y" * 96}, "ValueError"),
                 "non-str maybe_board": ({"maybe_board": 5}, "ValueError")}
        for name, (change, refusal) in cases.items():
            row = {**plain, **change}
            with self.subTest(name):
                self.assertEqual(GPU.power_policy_unwritable_reason(row), python[name])
                self.assertEqual(outcome(lambda row=row: native.Gpu.power_policy_unwritable_reason(row))[0], refusal)
        # the limits themselves are carried: int64 bounds, a 95-byte text
        for change in ({"minimum": -(2 ** 63), "maximum": 2 ** 63 - 1}, {"maybe_board": "y" * 95}):
            row = {**plain, **change}
            with self.subTest(change=sorted(change)):
                self.assertEqual(native.Gpu.power_policy_unwritable_reason(row),
                                 GPU.power_policy_unwritable_reason(row))


# ---- the derived clamp inside read_power_policies (the A6 rig) -----------------------------------

class DerivedClampReadTests(state.StateCase):
    """read_power_policies where get_current_limits' derived-clamp branch runs: policy 13's request
    above its maximum (Turing 390000, Ampere 270000) with the effective limit saturated, the live power
    read served identically to both sides by tests.test_native_current_limit_state's PowerDriver.
    Compared: the outcome (rows, error), the driver log, the fixture, the transport cache (with
    _last_policy_table), the getter bookkeeping (with the established set) and the live-power state
    (with _board_policy). NVML's limit is unreadable on both sides (the reader returns None)."""

    CASES = state.DerivedClampTests.CASES

    def run_side(self, side, action, rig_errors=()):
        return (*super().run_side(side, action, rig_errors), state.live_attrs(side.gpu))

    def run_both(self, setup, kw):
        runs = [self.run_side(self.side(kind, setup, **kw), lambda g: g.read_power_policies())
                for kind in ("python", "native")]
        for name, python, core in zip(("outcome", "driver log", "fixture state", "cache", "attributes",
                                       "live power"), *runs, strict=True):
            self.assertEqual(python, core, f"Python and native {name} differ")
        return runs[0]

    def setup(self, kw, maximum, requested, *steps, **power):
        def unreadable(side):
            side.gpu.read_power_limit_mw = lambda: None
        return state.chain(state.DerivedClampTests.clamp(self, kw, maximum, requested, **power), *steps, unreadable)

    def rows(self, run):
        self.assertEqual(run[0][0], "ok", run[0])
        rows, error = run[0][1]
        self.assertIsNone(error)
        return {row["policy"]: row for row in rows}

    def test_confirmed_clamp_names_policy_13(self):
        for name, (kw, maximum, requested, _) in self.CASES.items():
            with self.subTest(name):
                run = self.run_both(self.setup(kw, maximum, requested), kw)
                rows = self.rows(run)
                # the confirmation accepted the clamp: 13 has its current-limit slider
                self.assertEqual(state.captures(run[1]), 2)
                self.assertEqual(run[4]["_current_limit_established"], {13})
                self.assertEqual([r["policy"] for r in run[4]["_current_limit_last_rows"]], [13])
                self.assertTrue(rows[13]["named_current"])
                self.assertFalse(rows[13]["writable"])
                # the policy row's own range check is Python's minimum <= requested <= maximum
                self.assertEqual((rows[13]["requested"], rows[13]["maximum"], rows[13]["in_range"]),
                                 (requested, maximum, False))
                self.assertEqual(run[5]["board"], state.BOARD)  # the live read's binding
                self.assertIsNotNone(run[3]["table"])

    def test_refused_clamp_leaves_policy_13_unnamed(self):
        for name, (kw, maximum, requested, _) in self.CASES.items():
            # the live read fails (a 0x31 table), or succeeds with the FE request not above its maximum
            for reason, power, calls in (("definition version 0x31", {"version": 0x31}, 1),
                                         ("request not above the maximum",
                                          {"power_request": state.POWER_LIMITS[2]}, 2)):
                with self.subTest(name=name, reason=reason):
                    run = self.run_both(self.setup(kw, maximum, requested, **power), kw)
                    rows = self.rows(run)
                    self.assertEqual(state.captures(run[1]), calls)
                    self.assertEqual(run[4]["_current_limit_established"], set())
                    self.assertEqual([u["policy"] for u in run[4]["_current_limit_unavailable"]], [13])
                    self.assertFalse(rows[13]["named_current"])
                    self.assertFalse(rows[13]["writable"])
                    self.assertEqual(run[5]["last"] is None, calls == 1)

    def test_unserved_live_read_refuses_alike_given_the_same_pairing(self):
        # The live read not served: with the twin given the paired identity the core's CLRig has,
        # both sides refuse at the transport envelope with the same text. (Without that identity the
        # twin refuses earlier, "GPU binding could not be read", which is the rig, not the port.)
        def paired(side):
            if side.kind == "python":
                twin = side.gpu
                twin.pairing_error, twin.requested_slot = "", ""
                twin.nvapi.gpu = 929
                twin.nvapi.selected.update(handle=929, devid=0x1E02, subsys=0x12A010DE)
                twin.nvml = SimpleNamespace(ok=False, selected=None)
        for name, (kw, maximum, requested, _) in self.CASES.items():
            with self.subTest(name):
                run = self.run_both(self.setup(kw, maximum, requested, paired, serve=False), kw)
                rows = self.rows(run)
                self.assertFalse(rows[13]["named_current"])
                self.assertEqual((run[5]["error"], run[5]["kind"]),
                                 ("Client-power transport envelope is not understood", "malformed"))


if __name__ == "__main__":
    unittest.main()
