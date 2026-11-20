# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native current-limit policy rows, getter, verified setter, diagnostics and pins, increment
6b of the RM escape transport, against nvbackend.GPU._current_limit_state, get_current_limits,
current_limit_diagnostics, set_current_limit_ma / _set_current_limit_ma and _power_policy_pins.

The harness is tests.test_native_current_limits (increment 6a): every case builds a FRESH
tests.test_current_limits.fixture for each side, runs the same action on the Python twin and on
the core, and compares

- the outcome (the value, or the exception type, text and .fixed);
- the driver log (each escape's size, header words, parameter words, fields, NTSTATUS, RM);
- the fixture's writes, rejected info sizes and control block afterwards;
- the transport cache (transport, layout choice, mask, last table);
- the getter/setter bookkeeping afterwards: _current_limit_unavailable, _current_limit_error,
  _current_limit_read_error, _current_limit_last_rows, _current_limit_established and
  _power_policy_pins.
"""
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import tests.test_native_current_limits as base
from druta import client_power_runtime as cp

GPU = base.GPU
INFO, STATUS, CONTROL, SET = base.INFO, base.STATUS, base.CONTROL, base.SET
NEWER, OLDER, SHORT, AMPERE, LEGACY, TURING = (base.NEWER, base.OLDER, base.SHORT, base.AMPERE,
                                               base.LEGACY, base.TURING)
PASCAL_NEWER = dict(arch=GPU.ARCH_PASCAL, newer=True)
VOLTA = base.VOLTA
RM_SIZE = base.RM_SIZE

# The Python twin only creates these attributes when the getter or setter first assigns them, so
# a fresh twin has none: they are read with getattr and these defaults, which are what the core
# reports before anything happened.
ATTRS = (("_current_limit_unavailable", []), ("_current_limit_error", ""), ("_current_limit_read_error", ""),
         ("_current_limit_last_rows", []), ("_current_limit_established", set()), ("_power_policy_pins", {}))

UNSUPPORTED_GENERATION = "NVAPI unavailable or GPU generation unsupported"
NOT_EXPOSED = "not exposed in the driver's policy mask"
EVERY_LAYOUT_REFUSED = ("no known policy-info layout fits this driver ("
                        "20000-byte info: policy info layout rejected (RM 0x1F); "
                        "8632-byte info: policy info layout rejected (RM 0x1F); "
                        "20512-byte info: policy info layout rejected (RM 0x1F))")
FAILED_READ = "policy request failed (NTSTATUS -1, RM 0x0)"
REFUSED_SET = "policy request failed (NTSTATUS 0, RM 0x1F)"
NO_CAPTURE = "unsupported current-policy transport: no recognized power GET captured"


def geometry(kw):
    """(info meta word, status state word, control record word) of policy 13 in a fixture of kw,
    and the same for policy 14 on the newer layout."""
    if kw.get("ampere_layout"):
        info, status, control = (0x58, 0xE4), (0x80, 0x168C), (0x14, 0xB8)
    elif kw.get("legacy"):
        info, status, control = (0x3C, 0x84), (0x64, 0x828), (0x14, 0x34)
    elif kw.get("newer"):
        info, status, control = (0xCC, 0xFC), (0x9C, 0x1720), (0x14, 0xC4)
    elif kw.get("short_status"):
        info, status, control = (0x58, 0xE4), (0x68, 0x1434), (0x14, 0x7C)
    else:
        info, status, control = (0x58, 0xE4), (0x70, 0x1454), (0x14, 0x7C)

    def words(policy):
        return tuple((base + policy * stride) // 4 for base, stride in (info, status, control))
    return words


def attrs(gpu):
    return {name: getattr(gpu, name, default) for name, default in ATTRS}


def outcome(fn):
    try:
        return ("ok", fn())
    except Exception as exc:  # noqa: BLE001 - the predicate divergence raises a plain RuntimeError
        return (type(exc).__name__, str(exc), getattr(exc, "fixed", False))


def invalid(policy, type_id, channel, unit, minimum, default, maximum):
    return (f"policy {policy}: invalid current record (type=0x{type_id:X}, channel={channel}, unit={unit}, "
            f"min/default/max={minimum}/{default}/{maximum})")


def chain(*steps):
    def run(side):
        for step in steps:
            if step is not None:
                step(side)
    return run


def established(*policies):
    def setup(side):
        side.gpu._current_limit_established = set(policies)
    return setup


def xoc(enabled):
    def setup(side):
        side.gpu.voltage_xoc_enabled = enabled
    return setup


def poke(block, value, policy=13, offset=0, kw=NEWER):
    """Set one word of policy `policy`'s info (block 0), status (1) or control (2) record."""
    words = geometry(kw)

    def setup(side):
        target = (side.state.info, side.state.dynamic, side.state.control)[block]
        target[words(policy)[block] + offset] = value
    return setup


def info_header(type_id, channel, unit):
    return type_id | channel << 8 | unit << 16


class StateCase(base.CurrentLimitCase):
    def run_side(self, side, action, rig_errors=()):
        result = outcome(lambda: action(side.gpu))
        if side.rig is not None:
            self.assertEqual(side.rig.errors, list(rig_errors))
        return result, side.driver.log, side.fixture_state(), side.cache(), attrs(side.gpu)

    def both(self, action, setup=None, **kw):
        """action on a fresh twin and a fresh core after setup; every part must be identical.
        Returns the Python run as (outcome, log, fixture state, cache, attributes)."""
        runs = [self.run_side(self.side(kind, setup, **kw), action) for kind in ("python", "native")]
        for name, python, core in zip(("outcome", "driver log", "fixture state", "cache", "attributes"), *runs):
            self.assertEqual(python, core, f"Python and native {name} differ")
        return runs[0]

    def commands(self, log):
        return [entry[1][14] for entry in log]


# ---- 1. the rows on every layout -------------------------------------------------------------------

BLACKWELL_ROWS = [
    {"policy": 13, "label": "Core current", "channel": 13, "minimum_ma": 1, "maximum_ma": 5001000,
     "normal_maximum_ma": 500000, "default_ma": 300000, "limit_ma": 300000, "requested_ma": 300000,
     "value_ma": 150000},
    {"policy": 14, "label": "Other rail current", "channel": 12, "minimum_ma": 1, "maximum_ma": 5001000,
     "normal_maximum_ma": 200000, "default_ma": 120000, "limit_ma": 120000, "requested_ma": 120000,
     "value_ma": 60000},
]


def single_row(channel, default, maximum):
    return [{"policy": 13, "label": "Core current", "channel": channel, "minimum_ma": 1, "maximum_ma": maximum,
             "normal_maximum_ma": min(max(500000, 1, default), maximum), "default_ma": default,
             "limit_ma": default, "requested_ma": default, "value_ma": default // 2}]


class RowsTests(StateCase):
    def test_state_on_blackwell(self):
        result, log, state, cache, after = self.both(lambda g: g._current_limit_state(), **NEWER)
        rows, control = result[1]
        self.assertEqual(rows, BLACKWELL_ROWS)
        self.assertEqual(control, cache["table"][2])
        self.assertEqual(self.commands(log), [INFO, INFO, CONTROL, STATUS])
        self.assertEqual(state["writes"], [])
        # _current_limit_state touches only the unavailable list of the bookkeeping
        self.assertEqual(after, {name: default for name, default in ATTRS})

    def test_get_on_blackwell(self):
        result, log, state, _, after = self.both(lambda g: g.get_current_limits(), **NEWER)
        self.assertEqual(result, ("ok", BLACKWELL_ROWS))
        self.assertEqual(after["_current_limit_last_rows"], BLACKWELL_ROWS)
        self.assertEqual(after["_current_limit_established"], {13, 14})
        self.assertEqual((after["_current_limit_error"], after["_current_limit_read_error"]), ("", ""))
        self.assertEqual(after["_current_limit_unavailable"], [])
        self.assertEqual(state["writes"], [])

    def test_get_on_every_layout(self):
        for name, kw, rows in (("turing", TURING, single_row(19, 350780, 390000)),
                               ("pascal", OLDER, single_row(11, 204700, 218000)),
                               ("short_status", SHORT, single_row(11, 204700, 218000)),
                               ("ampere", AMPERE, single_row(19, 243000, 270000)),
                               ("legacy", LEGACY, single_row(19, 350780, 390000))):
            with self.subTest(name):
                result, _, state, _, after = self.both(lambda g: g.get_current_limits(), **kw)
                self.assertEqual(result, ("ok", rows))
                self.assertEqual(after["_current_limit_established"], {13})
                self.assertEqual(after["_current_limit_last_rows"], rows)
                self.assertEqual(state["writes"], [])

    def test_channel_differing_from_the_spec_is_relabeled(self):
        result, *_ = self.both(lambda g: g.get_current_limits(),
                               setup=poke(0, info_header(0x12, 7, 1), offset=1), **NEWER)
        self.assertEqual([(r["label"], r["channel"]) for r in result[1]],
                         [("Policy 13 current (channel 7)", 7), ("Other rail current", 12)])

    def test_normal_maximum_raised_to_a_default_above_the_cap(self):
        # default 600 A above the 500 A cap: the normal maximum follows the default
        setup = chain(poke(0, 600000, offset=3), poke(1, 600000, offset=1),
                      poke(2, 600000, offset=1))
        result, *_ = self.both(lambda g: g.get_current_limits(), setup=setup, **NEWER)
        self.assertEqual(result[1][0]["normal_maximum_ma"], 600000)


# ---- 2. unavailable entries and the two error strings -------------------------------------------

def setup_mask(*policies):
    def setup(side):
        mask = sum(1 << p for p in policies)
        side.state.info[1] = mask
        side.state.dynamic[1] = mask
        side.state.control[4] = mask
    return setup


class UnavailableTests(StateCase):
    def test_policy_not_in_the_mask(self):
        result, log, _, _, after = self.both(lambda g: g.get_current_limits(), setup=setup_mask(13), **NEWER)
        self.assertEqual(result, ("ok", BLACKWELL_ROWS[:1]))
        self.assertEqual(after["_current_limit_unavailable"],
                         [{"policy": 14, "label": "Other rail current", "present": False, "error": NOT_EXPOSED}])
        self.assertNotIn("unsupported", after["_current_limit_unavailable"][0])
        # not exposed is not a read failure, and not an "unavailable present" policy either
        self.assertEqual((after["_current_limit_error"], after["_current_limit_read_error"]), ("", ""))
        self.assertEqual(after["_current_limit_established"], {13})
        self.assertEqual(self.commands(log), [INFO, INFO, CONTROL, STATUS])

    def test_established_policy_vanished_from_the_mask(self):
        result, _, _, _, after = self.both(lambda g: g.get_current_limits(),
                                           setup=chain(setup_mask(13), established(13, 14)), **NEWER)
        self.assertEqual(result, ("ok", BLACKWELL_ROWS[:1]))
        vanished = f"policy 14 was read earlier this session and is now {NOT_EXPOSED}"
        self.assertEqual((after["_current_limit_error"], after["_current_limit_read_error"]), (vanished, vanished))
        self.assertEqual(after["_current_limit_established"], {13, 14})

    def test_empty_mask(self):
        def action(g):
            return g.get_current_limits(), g._current_limit_state()
        result, log, _, _, after = self.both(action, setup=setup_mask(), **NEWER)
        self.assertEqual(result, ("ok", ([], ([], None))))
        self.assertEqual(after["_current_limit_unavailable"],
                         [{"policy": 13, "label": "Core current", "present": False, "error": NOT_EXPOSED},
                          {"policy": 14, "label": "Other rail current", "present": False, "error": NOT_EXPOSED}])
        self.assertEqual((after["_current_limit_error"], after["_current_limit_read_error"]), ("", ""))
        self.assertEqual(after["_current_limit_established"], set())
        self.assertEqual(self.commands(log), [INFO, INFO, INFO])  # the probe, then one INFO per read

    def invalid_cases(self):
        """(name, setup, record text, understood) for policy 13 on the Blackwell fixture."""
        good = (0x12, 13, 1, 1, 300000, 5001000)

        def text(**change):
            fields = dict(zip(("type_id", "channel", "unit", "minimum", "default", "maximum"), good), **change)
            return invalid(13, **fields)
        return (
            ("wrong type", poke(0, info_header(0x11, 13, 1), offset=1), text(type_id=0x11), False),
            ("wrong unit", poke(0, info_header(0x12, 13, 0), offset=1), text(unit=0), False),
            ("minimum above default", poke(0, 400000, offset=2), text(minimum=400000), False),
            ("zero minimum", poke(0, 0, offset=2), text(minimum=0), False),
            ("default above maximum", poke(0, 6000000, offset=3), text(default=6000000), False),
            ("status type disagrees", poke(1, 0x11), text(), False),
            ("control type disagrees", poke(2, 0x11), text(), False),
            ("requested above maximum", poke(2, 5001001, offset=1), text(), True),
            ("effective below minimum", poke(1, 0, offset=1), text(), True),
        )

    def test_invalid_records(self):
        for name, setup, text, understood in self.invalid_cases():
            with self.subTest(name):
                result, _, state, _, after = self.both(lambda g: g.get_current_limits(), setup=setup, **NEWER)
                self.assertEqual(result, ("ok", BLACKWELL_ROWS[1:]))
                self.assertEqual(after["_current_limit_unavailable"],
                                 [{"policy": 13, "label": "Core current", "present": True,
                                   "unsupported": not understood, "error": text}])
                self.assertEqual(after["_current_limit_error"], text)
                # a record this build does not understand is not a failed read (yet)
                self.assertEqual(after["_current_limit_read_error"], text if understood else "")
                self.assertEqual(after["_current_limit_established"], {14})
                self.assertEqual(state["writes"], [])

    def test_invalid_records_once_established(self):
        for name, setup, text, _ in self.invalid_cases():
            with self.subTest(name):
                result, _, _, _, after = self.both(lambda g: g.get_current_limits(),
                                                   setup=chain(setup, established(13)), **NEWER)
                self.assertEqual(result, ("ok", BLACKWELL_ROWS[1:]))
                self.assertEqual((after["_current_limit_error"], after["_current_limit_read_error"]), (text, text))
                self.assertEqual(after["_current_limit_established"], {13, 14})

    def test_two_invalid_records_join_with_semicolons(self):
        setup = chain(poke(0, info_header(0x11, 13, 1), offset=1), poke(1, 0, policy=14, offset=1))
        result, _, _, _, after = self.both(lambda g: g.get_current_limits(), setup=setup, **NEWER)
        self.assertEqual(result, ("ok", []))
        first = invalid(13, 0x11, 13, 1, 1, 300000, 5001000)
        second = invalid(14, 0x12, 12, 1, 1, 120000, 5001000)
        self.assertEqual(after["_current_limit_error"], f"{first}; {second}")
        self.assertEqual(after["_current_limit_read_error"], second)

    def test_unsupported_generation(self):
        for arch in (VOLTA, None):
            with self.subTest(arch=arch):
                result, log, _, cache, after = self.both(lambda g: g.get_current_limits(), arch=arch)
                self.assertEqual(result, ("ok", []))
                self.assertEqual(log, [])
                self.assertEqual((after["_current_limit_error"], after["_current_limit_read_error"]),
                                 (UNSUPPORTED_GENERATION, UNSUPPORTED_GENERATION))
                self.assertEqual((after["_current_limit_unavailable"], after["_current_limit_last_rows"]), ([], []))

    def test_unsupported_generation_keeps_the_established_set(self):
        result, _, _, _, after = self.both(lambda g: g.get_current_limits(), setup=established(13), arch=VOLTA)
        self.assertEqual(result, ("ok", []))
        self.assertEqual(after["_current_limit_established"], {13})

    def test_every_layout_refused(self):
        result, log, _, cache, after = self.both(lambda g: g.get_current_limits(),
                                                 setup=base.refuse_info(lambda size: RM_SIZE), **NEWER)
        self.assertEqual(result, ("ok", []))
        self.assertEqual((after["_current_limit_error"], after["_current_limit_read_error"]),
                         (EVERY_LAYOUT_REFUSED, ""))
        self.assertEqual(len(log), 3)
        self.assertIsNotNone(cache["transport"])

    def test_every_layout_refused_once_established(self):
        result, _, _, _, after = self.both(lambda g: g.get_current_limits(),
                                           setup=chain(base.refuse_info(lambda size: RM_SIZE), established(13)),
                                           **NEWER)
        self.assertEqual(result, ("ok", []))
        self.assertEqual((after["_current_limit_error"], after["_current_limit_read_error"]),
                         (EVERY_LAYOUT_REFUSED, EVERY_LAYOUT_REFUSED))

    def test_state_raises_what_the_getter_records(self):
        result, *_ = self.both(lambda g: g._current_limit_state(), setup=base.refuse_info(lambda size: RM_SIZE),
                               **NEWER)
        self.assertEqual(result, ("CurrentLimitUnsupported", EVERY_LAYOUT_REFUSED, False))

    def test_failed_read_drops_the_transport(self):
        def setup(side):
            base.setup_table(side)  # the choice is made; the CONTROL GET of the next read fails
            side.driver.fail_at = {1}
        result, log, _, cache, after = self.both(lambda g: g.get_current_limits(), setup=setup, **NEWER)
        self.assertEqual(result, ("ok", []))
        self.assertEqual((after["_current_limit_error"], after["_current_limit_read_error"]),
                         (FAILED_READ, FAILED_READ))
        self.assertEqual(after["_current_limit_last_rows"], [])
        self.assertEqual(self.commands(log), [INFO, CONTROL])
        self.assertEqual((cache["transport"], cache["choice"]), (None, None))

    def test_failed_read_replaces_earlier_rows(self):
        def setup(side):
            side.gpu.get_current_limits()
            side.driver.fail_at = {0}
        result, _, _, _, after = self.both(lambda g: g.get_current_limits(), setup=setup, **NEWER)
        self.assertEqual(result, ("ok", []))
        self.assertEqual(after["_current_limit_last_rows"], [])
        self.assertEqual(after["_current_limit_established"], {13, 14})  # kept across a failed read
        self.assertEqual(after["_current_limit_read_error"], FAILED_READ)


# ---- 3. the setter -------------------------------------------------------------------------------

def set_buffer(side_state_control, word, value, policy, mask_word=4):
    params = list(side_state_control)
    params[word] = value
    params[mask_word] = 1 << policy
    return params


class SetterTests(StateCase):
    record = geometry(NEWER)(13)[2] + 1
    record14 = geometry(NEWER)(14)[2] + 1

    def original_control(self, kw=NEWER, setup=None):
        side = base.Side("python", **kw)
        if setup is not None:
            setup(side)
        return list(side.state.control)

    def test_success(self):
        original = self.original_control()
        result, log, state, cache, after = self.both(lambda g: g.set_current_limit_ma(13, 299000), **NEWER)
        self.assertEqual(result, ("ok", (True, "Core current set to 299 A; effective limit verified")))
        sent = set_buffer(original, self.record, 299000, 13)
        self.assertEqual(state["writes"], [sent])
        self.assertEqual([i for i, (a, b) in enumerate(zip(sent, original)) if a != b], [4, self.record])
        set_entries = [entry for entry in log if entry[1][14] == SET]
        self.assertEqual(len(set_entries), 1)
        self.assertEqual(set_entries[0][2], tuple(sent))
        self.assertEqual((set_entries[0][4], set_entries[0][5]), (0, 0))
        self.assertEqual(self.commands(log), [INFO, INFO, CONTROL, STATUS, SET, INFO, CONTROL, STATUS])
        expected_control = list(original)
        expected_control[self.record] = 299000
        self.assertEqual(state["control"], expected_control)
        self.assertEqual(after["_power_policy_pins"], {13: 299000})
        self.assertIsNotNone(cache["transport"])

    def test_success_on_every_layout(self):
        for name, kw, target in (("turing", TURING, 349780), ("pascal", OLDER, 204000),
                                 ("short_status", SHORT, 204000), ("ampere", AMPERE, 242000),
                                 ("legacy", LEGACY, 349780)):
            with self.subTest(name):
                original = self.original_control(kw)
                result, _, state, _, after = self.both(lambda g: g.set_current_limit_ma(13, target), **kw)
                amps = format(target / 1000, ".3f").rstrip("0").rstrip(".")
                self.assertEqual(result, ("ok", (True, f"Core current set to {amps} A; effective limit verified")))
                self.assertEqual(state["writes"], [set_buffer(original, geometry(kw)(13)[2] + 1, target, 13)])
                self.assertEqual(after["_power_policy_pins"], {13: target})

    def test_policy_14(self):
        original = self.original_control()
        result, _, state, *_ = self.both(lambda g: g.set_current_limit_ma(14, 180000), **NEWER)
        self.assertEqual(result, ("ok", (True, "Other rail current set to 180 A; effective limit verified")))
        self.assertEqual(state["writes"], [set_buffer(original, self.record14, 180000, 14)])

    def test_rows_and_pins_after_success(self):
        def action(g):
            g.set_current_limit_ma(13, 299000)
            return g.get_current_limits()
        result, _, _, _, after = self.both(action, **NEWER)
        self.assertEqual([(r["limit_ma"], r["requested_ma"]) for r in result[1]], [(299000, 299000), (120000, 120000)])
        self.assertEqual(after["_power_policy_pins"], {13: 299000})

    def test_already_at(self):
        result, log, state, _, after = self.both(lambda g: g.set_current_limit_ma(13, 300000), **NEWER)
        self.assertEqual(result, ("ok", (True, "Core current already at 300 A")))
        self.assertEqual(state["writes"], [])
        self.assertNotIn(SET, self.commands(log))
        # an "already at" success is a value the user chose: it is pinned
        self.assertEqual(after["_power_policy_pins"], {13: 300000})

    def test_unpinned_writes(self):
        for name, action in (("_set_current_limit_ma", lambda g: g._set_current_limit_ma(13, 299000)),
                             ("pin=False", lambda g: g.set_current_limit_ma(13, 299000, pin=False))):
            with self.subTest(name):
                result, _, state, _, after = self.both(action, setup=lambda s: setattr(
                    s.gpu, "_power_policy_pins", {14: 110000}), **NEWER)
                self.assertEqual(result, ("ok", (True, "Core current set to 299 A; effective limit verified")))
                self.assertEqual(len(state["writes"]), 1)
                self.assertEqual(after["_power_policy_pins"], {14: 110000})

    def test_pin_merges_with_existing_pins(self):
        result, _, _, _, after = self.both(lambda g: g.set_current_limit_ma(13, 299000), setup=lambda s: setattr(
            s.gpu, "_power_policy_pins", {14: 110000, 13: 1}), **NEWER)
        self.assertEqual(result[1][0], True)
        self.assertEqual(after["_power_policy_pins"], {14: 110000, 13: 299000})

    def test_failure_does_not_pin(self):
        result, _, _, _, after = self.both(lambda g: g.set_current_limit_ma(13, 0), **NEWER)
        self.assertFalse(result[1][0])
        self.assertEqual(after["_power_policy_pins"], {})

    def refused(self, action, message, kw=NEWER, setup=None, escapes=None):
        result, log, state, _, after = self.both(action, setup=setup, **kw)
        self.assertEqual(result, ("ok", (False, message)))
        self.assertEqual(state["writes"], [])
        self.assertEqual(after["_power_policy_pins"], {})
        if escapes is not None:
            self.assertEqual(self.commands(log), escapes)
        return after

    def test_not_a_supported_policy(self):
        for policy in (5, 15, -1, "13", True, 13.0, None):
            with self.subTest(policy=policy):
                self.refused(lambda g: g.set_current_limit_ma(policy, 299000), "not a supported current policy",
                             escapes=[])
        # policy 14 is not a Turing generation policy
        self.refused(lambda g: g.set_current_limit_ma(14, 299000), "not a supported current policy", kw=TURING,
                     escapes=[])

    def test_not_an_integer(self):
        for ma in (1000.0, "1000", None, True, float("nan")):
            with self.subTest(ma=ma):
                self.refused(lambda g: g.set_current_limit_ma(13, ma),
                             "current limit must be an integer number of milliamps", escapes=[])

    def test_unsupported_generation(self):
        for arch in (VOLTA, None):
            with self.subTest(arch=arch):
                # Volta has no generation policies, so the policy check refuses first
                self.refused(lambda g: g.set_current_limit_ma(13, 1000), "not a supported current policy",
                             kw=dict(arch=arch), escapes=[])

    def test_out_of_range(self):
        for name, setup, ma, message in (
                ("zero", None, 0, "Core current must be between 0.001 and 500 A in the current mode"),
                ("negative", None, -5, "Core current must be between 0.001 and 500 A in the current mode"),
                ("above normal", None, 500001, "Core current must be between 0.001 and 500 A in the current mode"),
                ("huge", None, 1 << 70, "Core current must be between 0.001 and 500 A in the current mode"),
                ("huge negative", None, -(1 << 70), "Core current must be between 0.001 and 500 A in the current mode"),
                ("XOC above the API maximum", xoc(True), 5001001,
                 "Core current must be between 0.001 and 5001 A in the current mode"),
                ("XOC huge", xoc(True), 1 << 70, "Core current must be between 0.001 and 5001 A in the current mode"),
                ("policy 14 normal", None, 200001,
                 "Other rail current must be between 0.001 and 200 A in the current mode")):
            with self.subTest(name):
                policy = 14 if name.startswith("policy 14") else 13
                self.refused(lambda g: g.set_current_limit_ma(policy, ma), message, setup=setup,
                             escapes=[INFO, INFO, CONTROL, STATUS])

    def test_xoc_allows_up_to_the_api_maximum(self):
        original = self.original_control()
        result, _, state, _, after = self.both(lambda g: g.set_current_limit_ma(13, 5001000), setup=xoc(True), **NEWER)
        self.assertEqual(result, ("ok", (True, "Core current set to 5001 A; effective limit verified")))
        self.assertEqual(state["writes"], [set_buffer(original, self.record, 5001000, 13)])

    def test_leaving_xoc_keeps_the_raised_limit_as_the_bound(self):
        def setup(side):
            side.gpu.voltage_xoc_enabled = True
            side.gpu.set_current_limit_ma(13, 600000)
            side.gpu.voltage_xoc_enabled = False
        result, _, state, _, after = self.both(lambda g: g.set_current_limit_ma(13, 700000), setup=setup, **NEWER)
        self.assertEqual(result, ("ok", (False, "Core current must be between 0.001 and 600 A in the current mode")))
        self.assertEqual(len(state["writes"]), 1)  # the setup's
        self.assertEqual(after["_power_policy_pins"], {13: 600000})
        result, _, state, *_ = self.both(lambda g: g.set_current_limit_ma(13, 550000), setup=setup, **NEWER)
        self.assertEqual(result, ("ok", (True, "Core current set to 550 A; effective limit verified")))
        self.assertEqual(len(state["writes"]), 2)

    def test_stored_and_effective_disagree(self):
        self.refused(lambda g: g.set_current_limit_ma(13, 250000),
                     "selected policy's stored and effective current limits disagree; nothing written",
                     setup=poke(1, 295000, offset=1), escapes=[INFO, INFO, CONTROL, STATUS])

    def test_unavailable_policy(self):
        text = invalid(13, 0x11, 13, 1, 1, 300000, 5001000)
        self.refused(lambda g: g.set_current_limit_ma(13, 250000), f"policy 13: {text}; nothing written",
                     setup=poke(0, info_header(0x11, 13, 1), offset=1))
        self.refused(lambda g: g.set_current_limit_ma(14, 100000), f"policy 14: {NOT_EXPOSED}; nothing written",
                     setup=setup_mask(13))
        self.refused(lambda g: g.set_current_limit_ma(13, 100000), f"policy 13: {NOT_EXPOSED}; nothing written",
                     setup=setup_mask())

    def test_read_failures_before_any_write(self):
        self.refused(lambda g: g.set_current_limit_ma(13, 250000), EVERY_LAYOUT_REFUSED,
                     setup=base.refuse_info(lambda size: RM_SIZE))

        def fail_control(side):
            base.setup_table(side)
            side.driver.fail_at = {1}
        self.refused(lambda g: g.set_current_limit_ma(13, 250000), FAILED_READ, setup=fail_control,
                     escapes=[INFO, CONTROL])

    def test_setter_leaves_the_getter_bookkeeping(self):
        # the setter's reads refresh _current_limit_unavailable (it runs _current_limit_state) but
        # never the getter's error strings, last rows or established set
        result, _, _, _, after = self.both(lambda g: g.set_current_limit_ma(13, 299000),
                                           setup=poke(0, info_header(0x11, 12, 1), policy=14, offset=1),
                                           **NEWER)
        self.assertTrue(result[1][0])
        self.assertEqual(after["_current_limit_error"], "")
        self.assertEqual(after["_current_limit_last_rows"], [])
        self.assertEqual(after["_current_limit_established"], set())
        self.assertEqual([item["policy"] for item in after["_current_limit_unavailable"]], [14])

    # ---- restoration branches

    def restoring(self, flags, policy, ma, message, writes, kw=NEWER, setup=None):
        def flagged(side):
            if setup is not None:
                setup(side)
            for flag in flags:
                setattr(side.state, flag, True)
        original = self.original_control(kw)
        result, log, state, _, after = self.both(lambda g: g.set_current_limit_ma(policy, ma), setup=flagged, **kw)
        self.assertEqual(result, ("ok", (False, message)))
        self.assertEqual(after["_power_policy_pins"], {})
        word = geometry(kw)(policy)[2] + 1
        expected = [set_buffer(original, word, ma, policy), set_buffer(original, word, original[word], policy)]
        self.assertEqual(len(state["writes"]), writes)
        for sent, wanted in zip(state["writes"], expected):
            if "mutate_other" in flags:
                continue
            self.assertEqual(sent, wanted)
        return state, log, original

    def test_store_only_restores(self):
        state, log, original = self.restoring(
            ("store_only",), 13, 250000,
            "effective current-policy read-back disagrees; original current limit restored and verified", 2)
        self.assertEqual(state["control"], original)
        self.assertEqual(self.commands(log), [INFO, INFO, CONTROL, STATUS, SET, INFO, CONTROL, STATUS,
                                              SET, INFO, CONTROL, STATUS])

    def test_store_only_on_legacy_restores(self):
        state, _, original = self.restoring(
            ("store_only",), 13, 349780,
            "effective current-policy read-back disagrees; original current limit restored and verified", 2,
            kw=LEGACY)
        self.assertEqual(state["control"], original)

    def test_refused_set(self):
        state, log, original = self.restoring(
            ("refused",), 13, 250000, f"{REFUSED_SET}; restoration could not be verified: {REFUSED_SET}", 2)
        self.assertEqual(state["control"], original)
        self.assertEqual([(e[4], e[5]) for e in log if e[1][14] == SET], [(0, 0x1F), (0, 0x1F)])

    def test_mutate_other(self):
        state, _, original = self.restoring(
            ("mutate_other",), 13, 250000,
            "current-policy control read-back disagrees; restoration could not be verified: "
            "restoration read-back disagrees", 2)
        self.assertEqual(state["control"][10], original[10] + 2)  # two SETs, each moved word 10
        self.assertEqual(state["control"][self.record], original[self.record])  # restored
        self.assertTrue(all(w[4] == 1 << 13 for w in state["writes"]))
        # the restoration replays the ORIGINAL buffer: word 10 is not overwritten with the moved value
        self.assertEqual(state["writes"][1][10], original[10])

    def test_break_restore(self):
        state, _, original = self.restoring(
            ("store_only", "break_restore"), 14, 180000,
            f"effective current-policy read-back disagrees; restoration could not be verified: {REFUSED_SET}", 2)
        # the stored half of the first SET is left in place: restoration was refused
        self.assertEqual(state["control"][self.record14], 180000)

    def test_set_failing_after_dispatch(self):
        # The fixture's failed_after_set raises OSError INSIDE the escape after applying the SET;
        # the core's fake escape can only return a status, so both sides get the same driver here:
        # the SET is applied and the escape returns NTSTATUS -1. That drops the transport, and the
        # twin's mocked capture is made to fail as the core's (which has no PowerPolInfo getter in
        # this rig) does, so the restoration cannot even be dispatched.
        def setup(side):
            inner = side.driver.inner

            def apply_then_fail(packet, fields):
                status = inner(packet, fields)
                return -1 if packet[14] == SET else status
            side.driver.inner = apply_then_fail
            if side.kind == "python":
                side.gpu._capture_current_limit_transport.return_value = None
        original = self.original_control()
        result, log, state, cache, after = self.both(lambda g: g.set_current_limit_ma(14, 180000), setup=setup,
                                                     **NEWER)
        self.assertEqual(result, ("ok", (False, f"{FAILED_READ}; restoration could not be verified: {NO_CAPTURE}")))
        self.assertEqual(state["writes"], [set_buffer(original, self.record14, 180000, 14)])
        self.assertEqual(self.commands(log), [INFO, INFO, CONTROL, STATUS, SET])
        self.assertEqual((cache["transport"], cache["choice"]), (None, None))
        self.assertEqual(after["_power_policy_pins"], {})

    def test_readback_failing_after_the_set(self):
        # break_get_once's equivalent as a status: the first GET after the SET fails with -1.
        def setup(side):
            side.driver.fail_at = {5}
            if side.kind == "python":
                side.gpu._capture_current_limit_transport.return_value = None
        original = self.original_control()
        result, log, state, cache, _ = self.both(lambda g: g.set_current_limit_ma(14, 180000), setup=setup, **NEWER)
        self.assertEqual(result, ("ok", (False, f"{FAILED_READ}; restoration could not be verified: {NO_CAPTURE}")))
        self.assertEqual(state["writes"], [set_buffer(original, self.record14, 180000, 14)])
        self.assertEqual(self.commands(log), [INFO, INFO, CONTROL, STATUS, SET, INFO])
        self.assertEqual((cache["transport"], cache["choice"]), (None, None))

    def test_fixture_exceptions_diverge(self):
        # RECORDED DIVERGENCE (harness): failed_after_set / break_get_once raise OSError inside the
        # fixture's escape. The twin's mocked escape lets it propagate (the transport stays, the
        # exception text becomes the message) and restores through the kept transport; the core's
        # fake D3DKMTEscape turns it into NTSTATUS -1, which drops the transport, and the restoration
        # cannot recapture in this rig. Neither side is wrong: a real escape returns a status.
        for flag, text, py_writes in (("failed_after_set", "transport failed after dispatch", 2),
                                      ("break_get_once", "readback disconnected", 2)):
            with self.subTest(flag):
                def setup(side):
                    setattr(side.state, flag, True)
                side_py, side_core = self.side("python", setup, **NEWER), self.side("native", setup, **NEWER)
                py = self.run_side(side_py, lambda g: g.set_current_limit_ma(14, 180000))
                core = self.run_side(side_core, lambda g: g.set_current_limit_ma(14, 180000),
                                     rig_errors=[f"OSError({text!r})"])
                self.assertEqual(py[0], ("ok", (False, f"{text}; original current limit restored and verified")))
                self.assertEqual(core[0], ("ok", (False, f"{FAILED_READ}; restoration could not be verified: "
                                                         f"{NO_CAPTURE}")))
                self.assertEqual(len(py[2]["writes"]), py_writes)
                self.assertEqual(len(core[2]["writes"]), 1)
                self.assertEqual(py[2]["control"], self.original_control())
                self.assertIsNotNone(py[3]["transport"])
                self.assertIsNone(core[3]["transport"])


# ---- 4. diagnostics ------------------------------------------------------------------------------

class DiagnosticsTests(StateCase):
    IDENTITY = {"python": ("580.97", "98.03.3b.c0.6f"), "native": ("581.57", "90.02.0B.00.01")}

    def diagnostics(self, setup, **kw):
        runs = []
        for kind in ("python", "native"):
            side = self.side(kind, setup, **kw)
            result, *_ = self.run_side(side, lambda g: g.current_limit_diagnostics())
            self.assertEqual(result[0], "ok")
            value = dict(result[1])
            # driver and vbios come from each side's own identity (the fixture's static, the rig's card)
            self.assertEqual((value.pop("driver"), value.pop("vbios")), self.IDENTITY[kind])
            runs.append(value)
        self.assertEqual(runs[0], runs[1], "Python and native diagnostics differ")
        return runs[0]

    def test_before_any_read(self):
        value = self.diagnostics(None, **NEWER)
        self.assertEqual(value, {"build": "current-visibility-v3", "generation": 10,
                                 "expected_policies": GPU.CURRENT_LIMIT_GENERATION_POLICIES[10],
                                 "transport": {}, "layout_info_bytes": None, "error": "", "read_error": "",
                                 "policies": [], "unavailable_policies": []})

    def test_after_a_successful_read(self):
        value = self.diagnostics(lambda s: s.gpu.get_current_limits(), **NEWER)
        self.assertEqual(value["policies"], BLACKWELL_ROWS)
        self.assertEqual(value["layout_info_bytes"], 20000)
        self.assertEqual((value["error"], value["read_error"]), ("", ""))

    def test_ampere_layout_bytes(self):
        value = self.diagnostics(lambda s: s.gpu.get_current_limits(), **AMPERE)
        self.assertEqual(value["layout_info_bytes"], 8632)

    def test_after_a_failed_read(self):
        def setup(side):
            base.setup_table(side)
            side.driver.fail_at = {len(side.driver.log) + 1}  # the log is cleared only after setup
            side.gpu.get_current_limits()
        value = self.diagnostics(setup, **NEWER)
        self.assertEqual((value["error"], value["read_error"]), (FAILED_READ, FAILED_READ))
        self.assertEqual(value["policies"], [])
        self.assertIsNone(value["layout_info_bytes"])  # the choice went with the transport

    def test_after_an_invalid_record(self):
        value = self.diagnostics(chain(poke(0, info_header(0x11, 13, 1), offset=1),
                                       lambda s: s.gpu.get_current_limits()), **NEWER)
        self.assertEqual([item["policy"] for item in value["unavailable_policies"]], [13])
        self.assertEqual(value["policies"], BLACKWELL_ROWS[1:])

    def test_volta(self):
        value = self.diagnostics(lambda s: s.gpu.get_current_limits(), arch=VOLTA)
        self.assertEqual((value["generation"], value["expected_policies"]), (VOLTA, {}))
        self.assertEqual((value["error"], value["read_error"]), (UNSUPPORTED_GENERATION, UNSUPPORTED_GENERATION))


# ---- 5. the derived clamp ------------------------------------------------------------------------
#
# The confirmation is the real read_live_power_request on both sides: nvbackend's on the twin, the
# core's (dc_cpc_live_read, reached through _confirm_derived_clamp's callback) natively. PowerDriver
# serves the client-power half of the driver in front of the current-limit fixture: the client INFO,
# ROLE_INFO and CONTROL of the small client group (LEGACY_SIZES) from its own buffers, the native
# INFO and CONTROL from the fixture's very blocks, so the confirmation reads the table the current
# limits read. The board power policy is native policy BOARD, its FE request POWER_REQUEST, above
# its maximum: the case in which the driver derives a current above the current policy's maximum.
# Every case also compares the live-power bookkeeping and static's pl_* keys.

TURING_SMALL = dict(arch=GPU.ARCH_TURING)  # the 11748-byte transport: TURING_SIZES' native blocks
ENVELOPE = {0: 0x4E564441, 1: 0x10002, 3: 0x4E562A2A, 4: 0x0500002B}  # the RM escape's packet words
BOARD, CLIENT_ROW, KIND = 2, 5, 0x0A
POWER_LIMITS = (100000, 300000, 400000)  # the board policy's minimum, rated and maximum (mW)
POWER_REQUEST = 500000
NVML_RANGE = {"pl_min_mw": 100000, "pl_def_mw": 280000, "pl_max_mw": 320000}  # CLRig's NVML getters
PL = tuple(NVML_RANGE)


class PowerDriver:
    """The client-power commands answered in front of the current-limit fixture (inner).

    version: the board definition's table version (0x30 is the one understood); power_request: the FE
    request in the client and native CONTROL; rm: an RM status for the accepted client INFO; change:
    applied to the fixture's state at the first client-power escape (a table changing between the
    current-limit read and the confirmation)."""

    COMMANDS = (cp.INFO, cp.ROLE_INFO, cp.CONTROL)
    SIZES, CONTROL_BASE = cp.LEGACY_SIZES, 8  # the small client group's sizes and client CONTROL base

    def __init__(self, inner, state, *, ampere, version=0x30, power_request=POWER_REQUEST, rm=0, change=None):
        self.inner, self.state, self.rm, self.change = inner, state, rm, change
        self.blocks = {INFO: state.info, CONTROL: state.control, STATUS: state.dynamic}
        self.ampere_info = ampere  # the fixture refuses (and records) other INFO sizes itself
        bit = 1 << BOARD
        state.info[1] |= bit
        state.control[4] |= bit
        state.dynamic[1] |= bit
        record = bytes([version, 0x12, 0x23, 0x34, KIND, 9, 0, 0]) + struct.pack("<III", *POWER_LIMITS)
        self.at = 88 + BOARD * 228  # the small group's native INFO record (the fixture's 0x58 / 0xE4)
        state.info[self.at // 4:(self.at + len(record)) // 4] = struct.unpack(f"<{len(record) // 4}I", record)
        native = (20 + BOARD * (184 if ampere else 124)) // 4
        state.control[native:native + 2] = [KIND, power_request]
        self.info = bytearray(cp.LEGACY_SIZES[cp.INFO])
        self.info[:4] = b"\xff\x17\x28\x39"
        struct.pack_into("<I", self.info, 4, 1 << CLIENT_ROW)
        row = 8 + CLIENT_ROW * 36
        self.info[row:row + 4] = b"\x57\x57\x01\x00"
        struct.pack_into("<III", self.info, row + 4, *POWER_LIMITS)
        self.requested = power_request

    def __call__(self, packet, fields):
        command, size = packet[14], packet[15]
        if command in self.COMMANDS:
            if self.change is not None:
                change, self.change = self.change, None
                change(self.state)
            result = bytearray(bytes(packet)[68:])
            rm = 0x1F if size != self.SIZES[command] else self.rm if command == cp.INFO else 0
            if not rm:
                if command == cp.INFO:
                    result[:] = self.info
                elif command == cp.ROLE_INFO:
                    native = struct.pack(f"<{len(self.state.info)}I", *self.state.info)
                    result[4:] = native[self.at:self.at + size - 4]
                else:
                    struct.pack_into("<I", result, self.CONTROL_BASE + CLIENT_ROW * 12 + 4, self.requested)
            packet[17:] = struct.unpack(f"<{size // 4}I", bytes(result))
            packet[5], packet[11], packet[16] = 1, 1, rm
            return 0
        block = self.blocks.get(command)
        if block is not None and size != len(block) * 4 and not (self.ampere_info and command == INFO):
            packet[5], packet[11], packet[16] = 1, 1, RM_SIZE  # a size this driver's structure is not
            return 0
        status = self.inner(packet, fields)
        if status == 0:
            packet[5] = packet[11] = 1  # completion, as a driver reports it
        return status


# The modern (full-size) client group on the 20512-byte native table: the 54420-byte transport's
# second fallback layout, reached when the 20000- and 8632-byte info GETs are refused for their size
# (as the AD103 entry of _CURRENT_LIMIT_LAYOUT_FALLBACKS records). Its native INFO/CONTROL are the
# client-power SIZES' own (20512 / 13876, record strides 0x104 / 0xC4), so the confirmation reads the
# very table the current limits read, and the modern group accepts board tables 0x30 and 0x40.
MODERN_KEY = (0x2080A612, 54420, 54352)
MODERN_TABLE = GPU._CURRENT_LIMIT_LAYOUT_FALLBACKS[MODERN_KEY][1]
TURING_MODERN = dict(arch=GPU.ARCH_TURING, newer=True)
AMPERE_MODERN = dict(arch=GPU.ARCH_AMPERE, newer=True)


def modern_table(side):
    """Rebuild side's fixture blocks (the 20000-byte newer layout) as MODERN_TABLE's, carrying every
    occupied policy's info, status and control words over to the new geometry."""
    old, sizes = side.state, MODERN_TABLE["sizes"]
    geometry_old = {"info": (0xCC, 0xFC), "status": (0x9C, 0x1720), "control": (0x14, 0xC4)}
    mask = old.info[1]
    info = [0] * (sizes[INFO] // 4)
    control = [(i * 17) & 0xFFFFFFFF for i in range(sizes[CONTROL] // 4)]
    dynamic = [0] * (sizes[STATUS] // 4)
    info[1], control[:5], dynamic[1] = mask, [0, 0, 0, 255, mask], mask
    for policy in range(32):
        if not mask >> policy & 1:
            continue
        for name, new, source, width in (("info", info, old.info, 5), ("status", dynamic, old.dynamic, 3),
                                         ("control", control, old.control, 2)):
            at = (MODERN_TABLE[name][0] + policy * MODERN_TABLE[name][1]) // 4
            was = (geometry_old[name][0] + policy * geometry_old[name][1]) // 4
            new[at:at + width] = source[was:was + width]
    old.info, old.control, old.dynamic = info, control, dynamic


def modern_words(policy):
    """(info meta word, status state word, control record word) of policy in MODERN_TABLE."""
    return tuple((MODERN_TABLE[name][0] + policy * MODERN_TABLE[name][1]) // 4
                 for name in ("info", "status", "control"))


class ModernPowerDriver(PowerDriver):
    """PowerDriver answering the modern client group (client-power SIZES: a 0x924-byte INFO with
    rows at 0x24, a 0x108-byte ROLE_INFO, CONTROL rows at 0x28) on MODERN_TABLE's native blocks."""

    SIZES, CONTROL_BASE = cp.SIZES, cp.CONTROL_BASE

    def __init__(self, inner, state, *, ampere=False, version=0x40, power_request=POWER_REQUEST, rm=0,
                 change=None):
        self.inner, self.state, self.rm, self.change = inner, state, rm, change
        self.blocks = {INFO: state.info, CONTROL: state.control, STATUS: state.dynamic}
        self.ampere_info = False  # every other info size is refused here for its size
        bit = 1 << BOARD
        state.info[1] |= bit
        state.control[4] |= bit
        state.dynamic[1] |= bit
        record = bytes([version, 0x12, 0x23, 0x34, KIND, 9, 0, 0]) + struct.pack("<III", *POWER_LIMITS)
        self.at = cp.NATIVE_INFO_BASE + BOARD * cp.NATIVE_INFO_STRIDE
        state.info[self.at // 4:(self.at + len(record)) // 4] = struct.unpack(f"<{len(record) // 4}I", record)
        native = (cp.NATIVE_CONTROL_BASE + BOARD * cp.NATIVE_CONTROL_STRIDE) // 4
        state.control[native:native + 2] = [KIND, power_request]
        self.info = bytearray(cp.SIZES[cp.INFO])
        self.info[:4] = b"\xff\x17\x28\x39"
        struct.pack_into("<I", self.info, 4, 1 << CLIENT_ROW)
        row = cp.INFO_BASE + CLIENT_ROW * cp.INFO_STRIDE
        self.info[row:row + 4] = b"\x57\x57\x01\x00"
        struct.pack_into("<III", self.info, row + 4, *POWER_LIMITS)
        self.requested = power_request


def serve_power(side, *, ampere, driver=None, **power):
    """PowerDriver (or driver) in front of side's fixture, the transport with its envelope words,
    and (on the twin) the paired identity CLRig gives the core."""
    if driver is None:
        driver = PowerDriver
    side.driver.inner = driver(side.driver.inner, side.state, ampere=ampere, **power)
    header = list(side.header)
    for index, value in ENVELOPE.items():
        header[index] = value
    side.gpu._current_limit_transport = (header, dict(base.FIELDS))
    if side.kind == "python":
        twin = side.gpu
        twin.pairing_error, twin.requested_slot = "", ""
        twin.nvapi.gpu = 929
        twin.nvapi.selected.update(handle=929, devid=0x1E02, subsys=0x12A010DE)  # CLRig's TITAN
        twin.nvml = SimpleNamespace(ok=False, selected=None)


def live_attrs(gpu):
    return {"last": getattr(gpu, "_live_power_last", None), "error": getattr(gpu, "_live_power_error", None),
            "kind": getattr(gpu, "_live_power_error_kind", None), "board": getattr(gpu, "_board_policy", None),
            "static": {key: gpu.static.get(key) for key in PL}}


def captures(log):
    """The client-power captures in a driver log: each begins with the client INFO (one escape, or
    a refused size and the accepted one). A successful confirmation is two."""
    count, previous = 0, None
    for entry in log:
        command = entry[1][14]
        if command == cp.INFO and previous != cp.INFO:
            count += 1
        previous = command
    return count


class DerivedClampTests(StateCase):
    CASES = {"turing": (TURING_SMALL, 390000, 400000, 380000), "ampere": (AMPERE, 270000, 280000, 260000)}

    def clamp(self, kw, maximum, requested, effective=None, serve=True, **power):
        """Requested above the maximum, effective saturated; the client-power driver served."""
        _, state_word, record = geometry(kw)(13)

        def setup(side):
            side.state.control[record + 1] = requested
            side.state.dynamic[state_word + 1] = maximum if effective is None else effective
            if side.kind == "python":
                side.gpu.static.update(NVML_RANGE)  # what GPU.__init__ reads from CLRig's NVML
            if serve:
                serve_power(side, ampere=bool(kw.get("ampere_layout")), **power)
        return setup

    def run_side(self, side, action, rig_errors=()):
        return (*super().run_side(side, action, rig_errors), live_attrs(side.gpu))

    def run_both(self, action, setup, kw):
        runs = [self.run_side(self.side(kind, setup, **kw), action) for kind in ("python", "native")]
        for name, python, core in zip(("outcome", "driver log", "fixture state", "cache", "attributes",
                                       "live power"), *runs, strict=True):
            self.assertEqual(python, core, f"Python and native {name} differ")
        return runs[0][:5], captures(runs[0][1]), runs[0][5]

    def test_the_twin_holds_the_core_nvml_range(self):
        side = self.side("native", None, **TURING_SMALL)
        self.assertEqual({key: side.gpu.capabilities().get(key) for key in PL}, NVML_RANGE)

    def test_confirmed_row_is_present_and_settable(self):
        for name, (kw, maximum, requested, target) in self.CASES.items():
            with self.subTest(name):
                setup = self.clamp(kw, maximum, requested)
                (result, _, _, _, after), calls, live = self.run_both(lambda g: g.get_current_limits(), setup, kw)
                self.assertEqual([(r["limit_ma"], r["requested_ma"], r["maximum_ma"]) for r in result[1]],
                                 [(maximum, requested, maximum)])
                self.assertEqual(after["_current_limit_unavailable"], [])
                self.assertEqual(calls, 2)  # one confirmation: two fresh captures
                self.assertEqual((live["board"], live["error"], live["static"]), (BOARD, None, NVML_RANGE))
                self.assertEqual((live["last"]["requested_mw"], live["last"]["binding"]["maximum_mw"]),
                                 (POWER_REQUEST, POWER_LIMITS[2]))
                self.assertEqual(live["last"]["binding"]["layout"], "legacy32")

                original = self.original(kw, setup)
                (result, _, state, _, after), calls, _ = self.run_both(
                    lambda g: g.set_current_limit_ma(13, target), setup, kw)
                amps = format(target / 1000, ".3f").rstrip("0").rstrip(".")
                self.assertEqual(result, ("ok", (True, f"Core current set to {amps} A; effective limit verified")))
                record = geometry(kw)(13)[2] + 1
                self.assertEqual(state["writes"], [set_buffer(original, record, target, 13)])
                self.assertEqual(after["_power_policy_pins"], {13: target})
                self.assertEqual(calls, 2)  # the read-back is in range: not consulted again

                # the setter's bound is the policy's own maximum, not the derived request
                (result, _, state, *_), _, _ = self.run_both(
                    lambda g: g.set_current_limit_ma(13, maximum + 1), setup, kw)
                self.assertEqual(result[1][0], False)
                self.assertIn("must be between", result[1][1])
                self.assertEqual(state["writes"], [])

    def original(self, kw, setup):
        side = base.Side("python", **kw)
        setup(side)
        return list(side.state.control)

    def test_unconfirmed_is_an_invalid_record(self):
        def flip_last(block):
            def change(state):
                words = getattr(state, block)
                words[-1] ^= 1  # a tail word past every record: the table changed after the current read
            return change

        for name, (kw, maximum, requested, target) in self.CASES.items():
            spec = GPU.CURRENT_LIMIT_GENERATION_POLICIES[GPU.ARCH_TURING if name == "turing" else GPU.ARCH_AMPERE][13]
            text = invalid(13, spec["type"], 19, 1, 1, maximum - (39220 if name == "turing" else 27000), maximum)
            for reason, power, count, kind in (
                    ("definition version 0x31", dict(version=0x31), 1, "malformed"),
                    ("info changed", dict(change=flip_last("info")), 2, None),
                    ("control changed", dict(change=flip_last("control")), 2, None),
                    ("request not above the maximum", dict(power_request=POWER_LIMITS[2]), 2, None),
                    ("failed read", dict(rm=0x56), 1, "unsupported")):
                with self.subTest(name=name, reason=reason):
                    setup = self.clamp(kw, maximum, requested, **power)
                    (result, _, _, _, after), calls, live = self.run_both(
                        lambda g: g.get_current_limits(), setup, kw)
                    self.assertEqual(result, ("ok", []))
                    self.assertEqual(after["_current_limit_unavailable"],
                                     [{"policy": 13, "label": "Core current", "present": True,
                                       "unsupported": False, "error": text}])
                    self.assertEqual((after["_current_limit_error"], after["_current_limit_read_error"]),
                                     (text, text))
                    self.assertEqual(calls, count)
                    self.assertEqual(live["kind"], kind)
                    self.assertEqual(live["last"] is None, kind is not None)
                    (result, _, state, *_), _, _ = self.run_both(lambda g: g.set_current_limit_ma(13, target),
                                                                 setup, kw)
                    self.assertEqual(result, ("ok", (False, f"policy 13: {text}; nothing written")))
                    self.assertEqual(state["writes"], [])

    def test_modern_table_version_0x40_is_an_invalid_record(self):
        # The role-byte clause of the confirmation: on the modern client group the controller itself
        # accepts a 0x40 board table (the small group's v30-only check never runs), so the live read
        # SUCCEEDS on both sides, every other clause holds (FE request above binding.maximum_mw, the
        # same native INFO and CONTROL as the current-limit table) and only role_info_hex[4] == 0x30
        # refuses the derived clamp. Version 0x30 on the same table is the control: confirmed.
        for name, kw, maximum, requested in (("turing", TURING_MODERN, 390000, 400000),
                                             ("ampere", AMPERE_MODERN, 270000, 280000)):
            arch = GPU.ARCH_TURING if name == "turing" else GPU.ARCH_AMPERE
            spec = GPU.CURRENT_LIMIT_GENERATION_POLICIES[arch][13]
            default = 350780 if name == "turing" else 243000
            text = invalid(13, spec["type"], 19, 1, 1, default, maximum)
            for version in (0x30, 0x40):
                with self.subTest(name=name, version=hex(version)):
                    _, state_word, record = modern_words(13)

                    def setup(side, state_word=state_word, record=record, maximum=maximum, requested=requested,
                              version=version):
                        modern_table(side)
                        side.state.control[record + 1] = requested
                        side.state.dynamic[state_word + 1] = maximum
                        if side.kind == "python":
                            side.gpu.static.update(NVML_RANGE)
                        serve_power(side, ampere=False, driver=ModernPowerDriver, version=version)
                    (result, log, _, cache, after), calls, live = self.run_both(
                        lambda g: g.get_current_limits(), setup, kw)
                    # the current-limit read chose the 20512-byte table after two size refusals
                    self.assertEqual([(e[1][15], e[5]) for e in log if e[1][14] == INFO][:3],
                                     [(20000, RM_SIZE), (8632, RM_SIZE), (20512, 0)])
                    self.assertEqual(cache["choice"], 2)
                    self.assertEqual(calls, 2)  # the live read succeeded: two fresh captures
                    self.assertEqual((live["error"], live["kind"], live["board"]), (None, None, BOARD))
                    self.assertNotIn("layout", live["last"]["binding"])  # the modern group
                    self.assertEqual(bytes.fromhex(live["last"]["role_info_hex"])[4], version)
                    self.assertEqual((live["last"]["requested_mw"], live["last"]["binding"]["maximum_mw"]),
                                     (POWER_REQUEST, POWER_LIMITS[2]))
                    if version == 0x30:
                        self.assertEqual([(r["limit_ma"], r["requested_ma"]) for r in result[1]],
                                         [(maximum, requested)])
                        self.assertEqual(after["_current_limit_unavailable"], [])
                        continue
                    self.assertEqual(result, ("ok", []))
                    self.assertEqual(after["_current_limit_unavailable"],
                                     [{"policy": 13, "label": "Core current", "present": True,
                                       "unsupported": False, "error": text}])
                    self.assertEqual((after["_current_limit_error"], after["_current_limit_read_error"]),
                                     (text, text))

    def test_effective_not_saturated_never_consults(self):
        kw, maximum, requested, _ = self.CASES["turing"]
        setup = self.clamp(kw, maximum, requested, effective=maximum - 1)
        (result, *_), calls, live = self.run_both(lambda g: g.get_current_limits(), setup, kw)
        self.assertEqual(result, ("ok", []))
        self.assertEqual(calls, 0)
        self.assertIsNone(live["last"])

    def test_effective_equal_to_the_request_consults(self):
        # effective in (requested, maximum): the request itself counts too
        kw, maximum, requested, _ = self.CASES["turing"]
        setup = self.clamp(kw, maximum, requested, effective=requested)
        (result, *_), calls, _ = self.run_both(lambda g: g.get_current_limits(), setup, kw)
        self.assertEqual(calls, 2)
        self.assertEqual([r["limit_ma"] for r in result[1]], [requested])

    def test_other_generations_never_consult(self):
        for name, kw, maximum in (("pascal", OLDER, 218000), ("pascal newer", PASCAL_NEWER, 218000),
                                  ("blackwell", NEWER, 5001000)):
            with self.subTest(name):
                setup = self.clamp(kw, maximum, maximum + 1000, serve=False)
                (result, log, _, _, after), calls, live = self.run_both(lambda g: g.get_current_limits(), setup, kw)
                self.assertEqual(calls, 0)
                self.assertFalse([entry for entry in log if entry[1][14] in PowerDriver.COMMANDS])
                self.assertNotIn(13, [r["policy"] for r in result[1]])
                self.assertEqual([i["policy"] for i in after["_current_limit_unavailable"]], [13])
                self.assertIsNone(live["last"])

    def test_exception_in_the_predicate_diverges(self):
        # RECORDED DIVERGENCE, reachable only through a substituted reader: the real
        # read_live_power_request never raises (both sides return (None, text) for a failed read)
        # and its role_info_hex is always hex. An exception raised by a substitute (or inside the
        # predicate, e.g. a non-hex role_info_hex) escapes nvbackend's _current_limit_state, so
        # get_current_limits reports its text as the error; the core's callback cannot carry it
        # and refuses, so the record is an invalid one.
        kw, maximum, requested, _ = self.CASES["turing"]
        text = invalid(13, 0x0B, 19, 1, 1, 350780, maximum)
        for reason, observe in (("raises", Mock(side_effect=RuntimeError("boom"))),
                                ("non-hex role", Mock(return_value=({"role_info_hex": "zz"}, None)))):
            with self.subTest(reason):
                clamp = self.clamp(kw, maximum, requested, serve=False)

                def setup(side, clamp=clamp, observe=observe):
                    clamp(side)
                    side.gpu.read_live_power_request = observe  # the substitute
                runs = self.sides(lambda g: (g._current_limit_state(), g.get_current_limits()), setup, **kw)
                py_error = "boom" if reason == "raises" else "non-hexadecimal number found in fromhex() arg at position 0"
                self.assertEqual(runs[0][0], ("RuntimeError", "boom", False) if reason == "raises"
                                 else ("ValueError", py_error, False))
                core = runs[1]
                self.assertEqual(core[0][0], "ok")
                (core_rows, core_control), core_get = core[0][1]
                self.assertEqual((core_rows, core_get), ([], []))
                self.assertEqual(core_control, core[3]["table"][2])
                self.assertEqual(core[4]["_current_limit_error"], text)
                getter = self.sides(lambda g: g.get_current_limits(), setup, **kw)
                self.assertEqual(getter[0][4]["_current_limit_error"], py_error)
                self.assertEqual(getter[1][4]["_current_limit_error"], text)
                self.assertEqual(getter[0][0], getter[1][0])  # both report no rows


# ---- 6. pins and the established set -------------------------------------------------------------

class BookkeepingTests(StateCase):
    def test_round_trips(self):
        for name, attribute, value, expected in (
                ("pins", "_power_policy_pins", {13: 299000, 14: -5, 0: (1 << 63) - 1}, None),
                ("pins cleared", "_power_policy_pins", {}, None),
                ("established", "_current_limit_established", {13, 14, 31, 0}, None),
                ("established cleared", "_current_limit_established", set(), None)):
            with self.subTest(name):
                def action(g):
                    setattr(g, attribute, value)
                    return getattr(g, attribute)
                result, log, *_ = self.both(action, **NEWER)
                self.assertEqual(result, ("ok", value if expected is None else expected))
                self.assertEqual(log, [])

    def test_none_clears_natively(self):
        # The core accepts None for "nothing"; the twin stores None, and its readers treat a falsy
        # value as empty (pins via `or {}`; the established set only through getattr, so a None
        # there would break the twin's `established | {...}`). Only the core is checked here.
        side = self.side("native", **NEWER)
        side.gpu._power_policy_pins = {13: 1}
        side.gpu._power_policy_pins = None
        self.assertEqual(side.gpu._power_policy_pins, {})
        side.gpu._current_limit_established = {13}
        side.gpu._current_limit_established = None
        self.assertEqual(side.gpu._current_limit_established, set())

    def test_native_refusals(self):
        # native-only: the core stores a 32-bit policy mask and int64 pins, so a value it cannot
        # represent is refused (and nothing is changed); the twin accepts any Python object.
        side = self.side("native", **NEWER)
        side.gpu._power_policy_pins = {13: 7}
        side.gpu._current_limit_established = {13}
        for value in ({"13": 1}, {32: 1}, {-1: 1}, {True: 1}, {13.0: 1}, {13: 1.0}, {13: "1"}, {13: 1 << 63},
                      {13: -(1 << 63) - 1}):
            with self.subTest(pins=value):
                with self.assertRaisesRegex(ValueError, "power policy pins map policy indices below 32"):
                    side.gpu._power_policy_pins = value
                self.assertEqual(side.gpu._power_policy_pins, {13: 7})
        for value in ({"13"}, {32}, {-1}, {True}, {13.0}, {1 << 40}):
            with self.subTest(established=value):
                with self.assertRaisesRegex(ValueError, "policy indices below 32"):
                    side.gpu._current_limit_established = value
                self.assertEqual(side.gpu._current_limit_established, {13})
        self.assertEqual(side.rig.errors, [])

    def test_established_survives_reads(self):
        def action(g):
            g._current_limit_established = {31}
            g.get_current_limits()
            return g._current_limit_established
        result, *_ = self.both(action, **NEWER)
        self.assertEqual(result, ("ok", {13, 14, 31}))

    def test_pins_reported_after_a_set_and_unchanged_by_reads(self):
        def action(g):
            g.set_current_limit_ma(14, 110000)
            g.get_current_limits()
            g._set_current_limit_ma(13, 290000)
            return g._power_policy_pins
        result, _, state, *_ = self.both(action, **NEWER)
        self.assertEqual(result, ("ok", {14: 110000}))
        self.assertEqual(len(state["writes"]), 2)


if __name__ == "__main__":
    unittest.main()
