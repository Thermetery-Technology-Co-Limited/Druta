# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The full power-policy list: every limit in the driver's table, the values the
user sets kept across board-limit writes, Stock / Max all / Reset, profiles and
names. Runs the real backend on a modelled driver; nothing touches hardware.

The model is the TITAN RTX / 610.88 table as measured (13 policies, the 610.88
layout) with the coupling measured there: the driver sets the slot, 8-pin and
core current limits on a straight line between their default at the default
board limit and their maximum at the maximum, on every board-limit write. The
3070 Ti / 595.97 showed the same rule on its own policies."""
import copy
import ctypes
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import dearpygui.dearpygui as dpg

from druta import nvbackend as n
from druta import policynames, profiles
from druta.druta import SLOT_BANDS, WIRE_BANDS, Druta

A612, A618, A619, A61A, E61B = 0x2080A612, 0x2080A618, 0x2080A619, 0x2080A61A, 0x2080E61B
INFO, STATUS, CONTROL = (0xCC, 0xFC), (0x9C, 0x1720), (0x14, 0xC4)
SIZES = {A618: 20000, A619: 396024, A61A: 13840, E61B: 13840}
# policy, type, channel, unit (0 mW / 1 mA), min, default, max
TITAN = [(0, 0x0B, 11, 0, 20000, 5001000, 5001000), (1, 0x03, 8, 0, 1, 1001000, 1001000),
         (2, 0x00, 9, 0, 100000, 260000, 320000), (3, 0x03, 3, 0, 1000, 63000, 74000),
         (4, 0x03, 4, 0, 1000, 143000, 167000), (5, 0x03, 5, 0, 1000, 143000, 167000),
         (7, 0x04, 3, 1, 1000, 11000, 16500), (8, 0x04, 4, 1, 1000, 17000, 33750),
         (9, 0x04, 5, 1, 1000, 17000, 33750), (11, 0x09, 0, 0, 0, 0, 0),
         (13, 0x0B, 19, 1, 1000, 350780, 390000), (14, 0x0B, 11, 0, 1, 5001000, 5001000),
         (15, 0x0B, 11, 0, 1, 5001000, 5001000)]
COUPLED, BOARD = (3, 4, 5, 7, 8, 9, 13), 2


class PolicyCard:
    """The driver's policy table behind the escape and NVML's power limit."""

    def __init__(self, table=TITAN, coupled=COUPLED, board=BOARD):
        self.spec = {p: dict(type=t, channel=c, unit=u, min=lo, default=d, max=hi)
                     for p, t, c, u, lo, d, hi in table}
        self.mask = sum(1 << p for p in self.spec)
        self.coupled, self.board = coupled, board
        self.request = {p: s["default"] for p, s in self.spec.items()}
        self.sets, self.ignore_next = [], False
        self.reading = {}                    # channel readings by policy
        self.control_type = {}               # a control block's type byte, if it disagrees

    def derive(self):
        b = self.spec[self.board]
        share = (self.request[self.board] - b["default"]) / (b["max"] - b["default"])
        for p in self.coupled:
            s = self.spec[p]
            self.request[p] = s["default"] + round(share * (s["max"] - s["default"]))

    def effective(self, p):
        if p == self.board:
            return int(self.request[p] * 1.03125)
        if p == 0:
            return 20000                    # measured: policy 0 ignores its request
        return self.request[p]

    def escape(self, packet, fields):
        command, size = packet[14], packet[15]
        if command == A618 and size != SIZES[A618]:
            packet[16] = 0x1F
            return 0
        words = [0] * (size // 4)
        if command == A618:
            words[1] = self.mask
            for p, s in self.spec.items():
                m = (INFO[0] + p * INFO[1]) // 4
                words[m + 1] = s["type"] | s["channel"] << 8 | s["unit"] << 16
                words[m + 2:m + 5] = [s["min"], s["default"], s["max"]]
        elif command == A61A:
            words[:5] = [0, 0, 0, 255, self.mask]
            for p, s in self.spec.items():
                r = (CONTROL[0] + p * CONTROL[1]) // 4
                words[r:r + 2] = [self.control_type.get(p, s["type"]), self.request[p]]
        elif command == A619:
            words[1] = self.mask
            for p, s in self.spec.items():
                st = (STATUS[0] + p * STATUS[1]) // 4
                words[st:st + 3] = [s["type"], self.effective(p), self.reading.get(p, 1000 + p)]
        elif command == E61B:
            params = list(packet[17:])
            policy = params[4].bit_length() - 1
            r = (CONTROL[0] + policy * CONTROL[1]) // 4
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

    def nvml_set(self, dev, value):
        self.request[self.board] = value.value
        self.derive()                        # measured: even for the same value
        return 0

    def nvml_get(self, dev, pointer):
        pointer._obj.value = self.request[self.board]
        return 0


def policy_gpu(card, arch=n.GPU.ARCH_TURING, uuid="GPU-TEST"):
    g = n.GPU.__new__(n.GPU)
    g._lock = threading.RLock()
    g.static = {"driver": "610.88", "vbios": "90.02.1e.00.02", "uuid": uuid,
                "name": "NVIDIA TITAN RTX", "slot": "0000:01:00.0",
                "pl_min_mw": 100000, "pl_def_mw": 260000, "pl_max_mw": 320000}
    g.nvapi = SimpleNamespace(ok=True, selected={"devid": 0x1E02, "slot": "0000:01:00.0"})
    g.arch = Mock(return_value=arch)
    g.voltage_xoc_enabled = False
    header = [0] * 17
    header[2], header[14], header[15] = 54420, A612, 54352
    g._capture_current_limit_transport = Mock(return_value=(header, {"hAdapter": 3}))
    g._legacy_clk_escape = Mock(side_effect=card.escape)
    g.nvml = SimpleNamespace(
        ok=True, dev=object(),
        has=lambda name: name in ("nvmlDeviceSetPowerManagementLimit",
                                  "nvmlDeviceGetPowerManagementLimit"),
        dll=SimpleNamespace(nvmlDeviceSetPowerManagementLimit=card.nvml_set,
                            nvmlDeviceGetPowerManagementLimit=card.nvml_get),
        errstr=lambda status: f"status {status}")
    return g


def rows_of(g):
    rows, error = g.read_power_policies()
    assert error is None, error
    return {r["policy"]: r for r in rows}


class PolicyListTests(unittest.TestCase):
    def setUp(self):
        self.card = PolicyCard()
        self.gpu = policy_gpu(self.card)

    def test_every_policy_is_listed_with_the_range_the_card_reports(self):
        rows = rows_of(self.gpu)
        self.assertEqual(sorted(rows), [p for p, *_ in TITAN])
        self.assertEqual((rows[4]["unit"], rows[4]["minimum"], rows[4]["default"], rows[4]["maximum"]),
                         ("mW", 1000, 143000, 167000))
        self.assertEqual(rows[8]["unit"], "mA")
        self.assertTrue(rows[2]["board"])                       # NVML's own range
        self.assertTrue(rows[13]["named_current"])              # the core-current slider's
        self.assertEqual({p for p, r in rows.items() if r["writable"]},
                         {0, 1, 3, 4, 5, 7, 8, 9, 14, 15})      # not 2, 11 (no range), 13
        self.assertEqual(rows[2]["limit"], 268125)              # the board adds 3.125%

    def test_a_value_set_by_hand_survives_a_board_limit_change(self):
        self.assertTrue(self.gpu.set_power_policy(4, 150000)[0])
        ok, message = self.gpu.set_power_limit_mw(290000)
        self.assertTrue(ok, message)
        self.assertIn("re-applied your values", message)
        rows = rows_of(self.gpu)
        self.assertEqual(rows[4]["requested"], 150000)          # yours, kept
        self.assertEqual(rows[5]["requested"], 155000)          # the driver's
        self.assertEqual(rows[4]["pinned"], 150000)

    def test_core_current_set_with_its_own_slider_is_kept_too(self):
        self.assertTrue(self.gpu.set_current_limit_ma(13, 360000)[0])
        self.assertTrue(self.gpu.set_power_limit_mw(300000)[0])
        self.assertEqual(rows_of(self.gpu)[13]["requested"], 360000)

    def test_without_a_value_set_by_hand_the_driver_decides(self):
        self.assertTrue(self.gpu.set_power_limit_mw(290000)[0])
        self.assertEqual(rows_of(self.gpu)[4]["requested"], 155000)
        self.assertEqual(self.card.sets, [])                    # Druta wrote no policy

    def test_stock_hands_a_coupled_policy_back_to_the_driver(self):
        self.gpu.set_power_limit_mw(290000)
        self.gpu.set_power_policy(4, 150000)
        ok, message = self.gpu.stock_power_policy(4)
        self.assertTrue(ok, message)
        row = rows_of(self.gpu)[4]
        self.assertEqual((row["requested"], row["pinned"]), (155000, None))

    def test_stock_of_a_policy_the_driver_does_not_derive_is_its_default(self):
        self.gpu.set_power_policy(14, 5000000)
        self.assertTrue(self.gpu.stock_power_policy(14)[0])
        self.assertEqual(rows_of(self.gpu)[14]["requested"], 5001000)

    def test_max_all_raises_every_limit_to_its_own_maximum(self):
        self.gpu.set_power_policy(4, 150000)
        self.gpu.set_power_policy(14, 4000000)
        steps = self.gpu.max_all_power_policies()
        self.assertTrue(all(ok for _step, ok, _msg in steps), steps)
        rows = rows_of(self.gpu)
        for p, r in rows.items():
            if r["unit"] and r["minimum"] < r["maximum"]:
                self.assertEqual(r["requested"], r["maximum"], p)
        self.assertEqual(self.gpu._power_policy_pins, {})

    def test_the_board_and_named_current_limits_are_not_written_here(self):
        before = list(self.card.sets)
        self.assertIn("Power limit slider", self.gpu.set_power_policy(2, 300000)[1])
        self.assertIn("current-limit slider", self.gpu.set_power_policy(13, 360000)[1])
        self.assertEqual(self.card.sets, before)
        self.assertTrue(self.gpu.set_power_policy(13, 360000, allow_named=True)[0])

    def test_out_of_range_unknown_and_rangeless_policies_write_nothing(self):
        for policy, value, part in ((4, 167001, "between 1 W and 167 W"), (4, 999, "between"),
                                    (6, 1000, "not in the driver's table"),
                                    (11, 0, "no range"), (4, 150000.0, "between")):
            with self.subTest(policy=policy, value=value):
                ok, message = self.gpu.set_power_policy(policy, value)
                self.assertFalse(ok)
                self.assertIn(part, message)
        self.assertEqual(self.card.sets, [])

    def test_a_write_the_driver_ignores_is_reported_and_the_original_verified(self):
        self.card.ignore_next = True
        ok, message = self.gpu.set_power_policy(4, 150000)
        self.assertFalse(ok)
        self.assertIn("read-back disagrees", message)
        self.assertIn("original limit restored and verified", message)
        self.assertEqual(rows_of(self.gpu)[4]["requested"], 143000)
        self.assertNotIn(4, getattr(self.gpu, "_power_policy_pins", None) or {})

    def test_reset_all_drops_the_values_and_returns_every_policy_to_default(self):
        self.gpu.set_power_policy(4, 150000)
        self.gpu.set_power_policy(14, 4000000)
        self.gpu.set_power_limit_mw(300000)
        g = self.gpu
        g.nvapi.VoltCtrlGet = g.nvapi.BoostTableSet = None
        g._volt_rail_profile = Mock(return_value=None)
        for name in ("set_clock_offset", "_reset_gpu_clocks", "reset_fan"):
            setattr(g, name, Mock(return_value=(True, "ok")))
        for name in ("clkdom_ok", "volt_rail_limits_supported", "_vf_lock_available"):
            setattr(g, name, Mock(return_value=False))
        steps = g.reset_all()
        self.assertTrue(all(ok for ok, _ in steps), steps)
        self.assertEqual(g._power_policy_pins, {})
        for p, r in rows_of(g).items():
            self.assertEqual(r["requested"], r["default"], p)

    def test_a_generation_without_current_policies_lists_nothing(self):
        gpu = policy_gpu(self.card, arch=8)
        rows, error = gpu.read_power_policies()
        self.assertEqual(rows, [])
        self.assertIn("not validated", error)


class PolicyProfileTests(unittest.TestCase):
    def setUp(self):
        self.card = PolicyCard()
        self.gpu = policy_gpu(self.card)

    def view(self):
        g = self.gpu

        class View:
            static = g.static

            def read(self):
                return {}

            def mem_offset_scale(self):
                return 2, "MHz eff"

            def __getattr__(self, name):
                return getattr(g, name)
        return View()

    def test_profile_keeps_the_values_set_by_hand_and_the_names(self):
        self.gpu.set_power_limit_mw(290000)
        self.gpu.set_power_policy(4, 150000)
        self.gpu.power_policy_names = {4: {"name": "8-pin #1", "channel": 4, "type": 3}}
        state = json.loads(json.dumps(profiles.capture(self.view())))
        self.assertEqual(state["power_policy_pins"],                     # not the derived ones
                         {"4": {"value": 150000, "channel": 4, "type": 3}})
        self.assertEqual(state["power_policies"]["5"], {"value": 155000, "channel": 5, "type": 3})
        self.assertNotIn("2", state["power_policies"])                   # the board: its slider's
        self.assertNotIn("13", state["power_policies"])                  # core current: its slider's
        self.assertEqual(state["power_policy_names"],
                         {"4": {"name": "8-pin #1", "channel": 4, "type": 3}})
        self.assertIn("power policies set by hand: 4", profiles.summarize(state))

    def test_restore_writes_the_board_first_and_the_values_set_by_hand_last(self):
        self.gpu.set_power_limit_mw(290000)
        self.gpu.set_power_policy(4, 150000)
        saved = json.loads(json.dumps(profiles.capture(self.view())))
        self.gpu.set_power_limit_mw(260000)
        self.gpu._power_policy_pins = {5: 150000}                # this session's, not the profile's
        self.gpu.set_power_policy(5, 150000)
        results = profiles.restore(self.gpu, saved, apply_curve=False)
        self.assertTrue(all(ok for ok, _ in results), results)
        rows = rows_of(self.gpu)
        self.assertEqual(self.gpu._power_policy_pins, {4: 150000})
        self.assertEqual((rows[2]["requested"], rows[4]["requested"], rows[5]["requested"]),
                         (290000, 150000, 155000))
        self.assertIsNone(rows[13]["pinned"])                    # a derived value is not yours

    def test_saved_values_must_fit_this_cards_table_not_its_driver_string(self):
        self.gpu.set_power_limit_mw(290000)
        self.gpu.set_power_policy(4, 150000)
        state = json.loads(json.dumps(profiles.capture(self.view())))
        self.assertIsNone(profiles.preflight(self.gpu, state))
        updated = copy.deepcopy(state)
        updated["device"]["driver"] = "999.99"                     # a driver update alone
        self.assertIsNone(profiles.preflight(self.gpu, updated))
        entry = {"value": 150000, "channel": 4, "type": 3}
        for pins in ({"4": dict(entry, value=150000.0)}, {"x": dict(entry)}, {"4": "150000"}):
            with self.subTest(malformed=pins):
                bad = copy.deepcopy(state)
                bad["power_policy_pins"] = pins
                self.assertIsNotNone(profiles.preflight(self.gpu, bad))
        # a value that no longer fits the live table is reported on its own;
        # the rest of the profile is restored
        for key, pin, part in (("4", dict(entry, value=168000), "outside this card's range"),
                               ("4", dict(entry, channel=5), "channel or record type differs"),
                               ("4", dict(entry, type=4), "channel or record type differs"),
                               ("6", dict(entry), "no such policy"),
                               ("2", dict(entry, channel=9, type=0), "board limit"),
                               ("4", 150000, "earlier build")):
            with self.subTest(key=key, pin=pin):
                self.gpu.set_power_limit_mw(260000)
                self.card.sets.clear()
                bad = copy.deepcopy(state)
                bad["power_policy_pins"] = {key: pin}
                self.assertIsNone(profiles.preflight(self.gpu, bad))
                results = profiles.restore(self.gpu, bad, apply_curve=False)
                self.assertIn((True, "power limit configured to 290 W"), results)
                failed = [m for ok, m in results if not ok]
                self.assertEqual(len(failed), 1, results)
                self.assertIn(f"power policy {key} (set by hand) not restored", failed[0])
                self.assertIn(part, failed[0])
                self.assertNotIn(int(key), [p for p, _ in self.card.sets])
                self.assertEqual(self.gpu._power_policy_pins, {})

    def test_a_limit_max_all_raised_directly_comes_back_with_the_profile(self):
        card = PolicyCard([row if row[0] != 14 else (14, 0x0B, 11, 0, 1, 4000000, 5001000)
                           for row in TITAN])
        self.gpu = gpu = policy_gpu(card)
        gpu.max_all_power_policies()
        self.assertEqual(card.request[14], 5001000)                # not coupled: written directly
        saved = json.loads(json.dumps(profiles.capture(self.view())))
        card.request[14] = 4000000
        gpu.set_power_limit_mw(260000)
        self.assertTrue(all(ok for ok, _ in profiles.restore(gpu, saved, apply_curve=False)))
        self.assertEqual((card.request[14], card.request[2]), (5001000, 320000))

    def test_a_pin_on_core_current_is_restored_through_its_own_slider(self):
        self.gpu.set_current_limit_ma(13, 360000)
        saved = json.loads(json.dumps(profiles.capture(self.view())))
        self.assertEqual(saved["power_policy_pins"]["13"]["value"], 360000)
        self.gpu.set_power_limit_mw(300000)
        with patch.object(type(self.gpu), "set_current_limit_ma", autospec=True,
                          side_effect=n.GPU.set_current_limit_ma) as setter:
            results = profiles.restore(self.gpu, saved, apply_curve=False)
        self.assertTrue(all(ok for ok, _ in results), results)
        self.assertIn(((self.gpu, 13, 360000), {"pin": True}),
                      [(c.args, c.kwargs) for c in setter.call_args_list])
        self.assertEqual(rows_of(self.gpu)[13]["pinned"], 360000)

    def test_an_unreadable_or_absent_table_leaves_the_rest_of_the_profile_standing(self):
        self.gpu.set_power_limit_mw(290000)
        self.gpu.set_power_policy(4, 150000)
        saved = json.loads(json.dumps(profiles.capture(self.view())))
        self.gpu.set_power_limit_mw(260000)
        real, calls = self.gpu._power_policy_table, [0]

        def after_the_board_write():
            calls[0] += 1
            if calls[0] > 1:                                       # the first is the before-read
                raise ValueError("policy request failed (NTSTATUS 0, RM 0x1A)")
            return real()
        self.gpu._power_policy_table = after_the_board_write
        self.assertIsNone(profiles.preflight(self.gpu, saved))
        results = profiles.restore(self.gpu, saved, apply_curve=False)
        self.assertIn((True, "power limit configured to 290 W"), results)
        self.assertIn((False, "power policies NOT restored: policy request failed "
                              "(NTSTATUS 0, RM 0x1A)"), results)
        # a generation this build has no policy table for: the same, not a refusal
        other = policy_gpu(PolicyCard(), arch=8)
        self.assertIsNone(profiles.preflight(other, saved))
        results = profiles.restore(other, saved, apply_curve=False)
        self.assertIn((True, "power limit configured to 290 W"), results)
        self.assertIn((False, "power policies NOT restored: current policies are not validated "
                              "for this GPU generation"), results)

    def test_a_value_the_driver_derives_is_left_to_the_driver_not_written_back(self):
        self.gpu.set_power_limit_mw(290000)
        saved = json.loads(json.dumps(profiles.capture(self.view())))
        self.assertEqual(saved["power_policies"]["4"]["value"], 155000)
        self.gpu.set_power_limit_mw(260000)
        derive = self.card.derive

        def another_drivers_rounding():                            # derives 1 W higher
            derive()
            for p in (3, 4, 5):
                self.card.request[p] += 1000
        self.card.derive = another_drivers_rounding
        self.card.sets.clear()
        results = profiles.restore(self.gpu, saved, apply_curve=False)
        self.assertTrue(all(ok for ok, _ in results), results)
        self.assertEqual(self.card.sets, [])                       # nothing written by number
        self.assertEqual(self.card.request[4], 156000)             # the driver's, for 290 W
        self.assertEqual(self.gpu._power_policy_pins, {})
        self.assertTrue(any("the driver's values for the restored power limit" in m
                            for _, m in results))

    def test_without_the_board_limit_only_the_values_set_by_hand_are_written(self):
        self.gpu.set_power_limit_mw(290000)
        self.gpu.set_power_policy(8, 20000)
        saved = json.loads(json.dumps(profiles.capture(self.view())))
        del saved["power_limit_mw"]                                # readback was unavailable
        self.gpu._power_policy_pins = {}
        self.gpu.set_power_limit_mw(260000)
        self.card.sets.clear()
        results = profiles.restore(self.gpu, saved, apply_curve=False)
        # core current comes back through its own slider's saved value, as before
        self.assertEqual([s for s in self.card.sets if s[0] != 13], [(8, 20000)])
        self.assertEqual((self.card.request[2], self.card.request[4]), (260000, 143000))
        failed = [m for ok, m in results if not ok]
        self.assertEqual(len(failed), 1, results)
        self.assertIn("the power limit was not restored", failed[0])

    def test_restore_pins_the_users_value_not_the_cards(self):
        self.gpu.set_power_limit_mw(290000)
        self.gpu.set_power_policy(4, 150000)
        saved = json.loads(json.dumps(profiles.capture(self.view())))
        saved["power_policies"]["4"]["value"] = 155000             # a re-apply that failed
        self.gpu.set_power_policy(14, 4000000)                     # one the board does not drive
        saved["power_policy_pins"]["14"] = {"value": 4000000, "channel": 11, "type": 0x0B}
        saved["power_policies"]["14"]["value"] = 3000000
        self.gpu.set_power_limit_mw(260000)
        self.gpu.stock_power_policy(14)
        self.card.sets.clear()
        results = profiles.restore(self.gpu, saved, apply_curve=False)
        self.assertTrue(all(ok for ok, _ in results), results)
        self.assertEqual((self.card.request[4], self.gpu._power_policy_pins),
                         (150000, {4: 150000, 14: 4000000}))
        self.assertEqual([s for s in self.card.sets if s[0] == 14], [(14, 4000000)])


class PolicyNameStoreCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = folder.name


class PolicyNameStoreTests(PolicyNameStoreCase):
    def test_names_are_kept_per_card(self):
        names = {4: {"name": "8-pin #1", "channel": 4, "type": 3}}
        self.assertTrue(policynames.save("GPU-A", names, root=self.root)[0])
        self.assertTrue(policynames.save("GPU-B", {7: {"name": "slot A", "channel": 3, "type": 4}},
                                         root=self.root)[0])
        self.assertEqual(policynames.load("GPU-A", root=self.root), names)
        self.assertEqual(list(policynames.load("GPU-B", root=self.root)), [7])
        self.assertEqual(policynames.load("GPU-C", root=self.root), {})
        self.assertFalse(policynames.save("", names, root=self.root)[0])

    def test_a_name_only_follows_the_channel_and_type_it_was_given_for(self):
        rows = [{"policy": 4, "channel": 4, "type": 3}, {"policy": 5, "channel": 9, "type": 3}]
        names = {4: {"name": "8-pin #1", "channel": 4, "type": 3},
                 5: {"name": "8-pin #2", "channel": 5, "type": 3},     # renumbered table
                 6: {"name": "gone", "channel": 6, "type": 3}}
        self.assertEqual(list(policynames.matching(names, rows)), [4])

    def test_malformed_names_and_an_unreadable_store_are_dropped(self):
        self.assertEqual(policynames.clean({"4": {"name": " ", "channel": 4, "type": 3},
                                            "x": {"name": "a", "channel": 1, "type": 1},
                                            "5": {"name": "b", "channel": "5", "type": 3},
                                            "6": {"name": "c" * 200, "channel": 6, "type": 3}}),
                         {6: {"name": "c" * policynames.NAME_MAX, "channel": 6, "type": 3}})
        Path(self.root, "power-policy-names.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(policynames.load("GPU-A", root=self.root), {})


class PolicyUiCase(unittest.TestCase):
    def setUp(self):
        dpg.create_context()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = folder.name
        store = patch("druta.policynames.store_path",
                      side_effect=lambda root=None: Path(self.root) / "names.json")
        store.start()
        self.addCleanup(store.stop)
        self.card = PolicyCard()
        self.app = app = Druta.__new__(Druta)
        app.gpu = policy_gpu(self.card)
        app._slider_ranges, app._knob_decimals = {}, {}
        app._carryover_hi, app._current_limits, app._knob_cb = {}, {}, {}
        app._ctl_widgets = []
        app._xoc_bounds = app._knob_sync = False
        app.log, app.bind = Mock(), Mock()
        app.s = lambda value: value
        app.guard = Mock(return_value=True)
        app._tim_lock = threading.Lock()

    def tearDown(self):
        dpg.destroy_context()

    def build(self):
        with dpg.window():
            with dpg.collapsing_header(label="Power policies", tag="power_policy_header",
                                       default_open=True):
                self.app.build_power_policy_section()


class PolicyUiTests(PolicyUiCase):
    def test_one_slider_per_writable_policy_bounded_by_the_bios_range(self):
        self.build()
        config = dpg.get_item_configuration("sl_pp4")
        self.assertEqual((config["min_value"], config["max_value"]), (1, 167))
        self.assertEqual(dpg.get_value("in_pp8"), 17)
        for policy in (2, 11, 13):                               # read-only rows
            self.assertFalse(dpg.does_item_exist(f"sl_pp{policy}"))
            self.assertTrue(dpg.does_item_exist(f"name_pp{policy}"))
        self.assertTrue(dpg.does_item_exist("pp_max_all"))
        self.assertIn("pp_max_all", self.app._ctl_widgets)

    def test_a_slider_follows_the_driver_unless_a_value_is_staged(self):
        self.build()
        dpg.set_value("sl_pp5", 150)                             # the user stages a value
        self.app.gpu.set_power_limit_mw(290000)
        self.app._policy_refresh_t = 0.0
        self.app.refresh_power_policies()
        self.assertEqual(dpg.get_value("sl_pp4"), 155)           # followed the driver
        self.assertEqual(dpg.get_value("sl_pp5"), 150)           # kept what the user staged

    def test_apply_writes_the_typed_value_and_marks_it_yours(self):
        self.build()
        self.app.apply_power_policy(4, 150.0)
        self.assertEqual(self.card.request[4], 150000)
        self.assertTrue(dpg.get_value("live_pp4").endswith(" *"))

    def test_a_note_belongs_to_the_channel_and_is_saved_for_this_card(self):
        self.build()
        self.app.set_power_policy_note(8, choice="others")          # 8-pin #1 current, channel 4
        self.assertTrue(dpg.is_item_shown("name_pp8"))
        self.app.set_power_policy_note(8, text="  first 8-pin  ")
        self.app.save_power_policy_names(force=True)
        saved = policynames.load("GPU-TEST")
        self.assertEqual({p: e["name"] for p, e in saved.items()}, {4: "first 8-pin", 8: "first 8-pin"})
        self.assertEqual(dpg.get_value("note_pp4"), "others")       # the power row on channel 4
        self.assertEqual(dpg.get_value("name_pp4"), "first 8-pin")
        self.assertEqual(self.app.gpu.power_policy_names[4]["name"], "first 8-pin")
        dpg.destroy_context()
        dpg.create_context()
        self.build()                                                 # a fresh session starts with it
        self.assertEqual((dpg.get_value("note_pp8"), dpg.get_value("name_pp8")),
                         ("others", "first 8-pin"))

    def test_a_connector_note_and_clearing_it(self):
        self.build()
        self.app.set_power_policy_note(4, choice="PCIE 8pin")
        self.assertEqual(dpg.get_value("note_pp8"), "PCIE 8pin")
        self.assertFalse(dpg.is_item_shown("name_pp8"))
        self.assertTrue(dpg.is_item_shown("desc_pp8"))
        self.app.set_power_policy_note(8, choice="-")
        self.assertEqual((dpg.get_value("note_pp4"), self.app._policy_names), ("-", {}))

    def colour(self, tag):
        return tuple(round(c * 255) for c in dpg.get_item_configuration(tag)["color"][:3])

    def test_a_connectors_current_is_shown_and_coloured_per_wire(self):
        self.build()
        self.app.set_power_policy_note(8, choice="PCIE 8pin")        # 3 wires
        for amps, band in ((15.0, 0), (20.0, 1), (36.0, 2), (45.0, 3), (30.0, 1), (30.3, 2)):
            with self.subTest(amps=amps):
                self.card.reading[8] = int(amps * 1000)
                self.app.refresh_power_policies(force=True)
                self.assertIn(f"({amps / 3:.2f}/wire)", dpg.get_value("live_pp8"))
                self.assertEqual(self.colour("live_pp8"), WIRE_BANDS[band][1])
        self.assertIn("(5.67/wire)", dpg.get_value("live_pp8"))       # the 17 A limit per wire
        self.assertNotIn("/wire", dpg.get_value("live_pp4"))           # the power row: watts

    def test_the_limits_band_follows_the_value_on_the_slider(self):
        self.build()
        self.app.set_power_policy_note(8, choice="PCIE 8pin")
        theme = lambda band: self.app.current_theme(WIRE_BANDS[band][1])        # noqa: E731
        self.assertEqual(dpg.get_item_theme("in_pp8"), theme(1))      # 17 A = 5.67/wire
        dpg.set_value("sl_pp8", 33.75)                                 # staged, not applied
        self.app.refresh_power_policies(force=True)
        self.assertEqual(dpg.get_item_theme("in_pp8"), theme(2))      # 11.25/wire
        self.assertEqual(self.card.sets, [])
        dpg.set_value("sl_pp8", 12.0)
        self.app.refresh_power_policies(force=True)
        self.assertEqual(dpg.get_item_theme("in_pp8"), theme(0))
        self.app.set_power_policy_note(8, choice="-")
        self.assertIsNone(dpg.get_item_theme("in_pp8"))

    def test_each_connector_divides_by_its_own_wires(self):
        self.build()
        self.card.reading[8] = 24000
        for note, text in (("PCIE 8pin", "(8.00/wire)"), ("EPS 8pin", "(6.00/wire)"),
                           ("12VHPWR/12V-2x6", "(4.00/wire)")):
            with self.subTest(note=note):
                self.app.set_power_policy_note(8, choice=note)
                self.assertIn(text, dpg.get_value("live_pp8"))
        for note in ("PCIE", None):                                    # the slot's pins; free text
            with self.subTest(note=note):
                if note:
                    self.app.set_power_policy_note(8, choice=note)
                else:
                    self.app.set_power_policy_note(8, text="my cable")
                self.assertNotIn("/wire", dpg.get_value("live_pp8"))

    def test_the_slot_is_judged_as_a_whole_by_its_own_bands(self):
        self.build()
        self.app.set_power_policy_note(7, choice="PCIE")               # slot current, channel 3
        for amps, band in ((5.0, 0), (5.5, 0), (6.0, 1), (6.25, 1), (6.5, 2), (7.0, 2), (7.5, 3)):
            with self.subTest(amps=amps):
                self.card.reading[7] = int(amps * 1000)
                self.app.refresh_power_policies(force=True)
                self.assertEqual(self.colour("live_pp7"), SLOT_BANDS[band][1])
                self.assertNotIn("/wire", dpg.get_value("live_pp7"))
        # the Titan's own 11 A slot limit, shown as it is
        self.assertEqual(dpg.get_item_theme("in_pp7"), self.app.current_theme(SLOT_BANDS[3][1]))
        dpg.set_value("sl_pp7", 6.0)
        self.app.refresh_power_policies(force=True)
        self.assertEqual(dpg.get_item_theme("in_pp7"), self.app.current_theme(SLOT_BANDS[1][1]))
        self.assertEqual(self.card.sets, [])

    def test_a_loaded_profiles_notes_become_this_cards_notes(self):
        self.build()
        self.app.apply_profile_policy_names(
            {"power_policy_names": {"7": {"name": "slot 12 V", "channel": 3, "type": 4},
                                    "8": {"name": "wrong channel", "channel": 9, "type": 4}}})
        self.assertEqual((dpg.get_value("note_pp7"), dpg.get_value("name_pp7")), ("others", "slot 12 V"))
        self.assertEqual(dpg.get_value("name_pp3"), "slot 12 V")       # same channel
        self.assertEqual(dpg.get_value("note_pp8"), "-")
        self.app.save_power_policy_names()
        self.assertEqual(list(policynames.load("GPU-TEST")), [7])

class ReviewFixBackendTests(unittest.TestCase):
    def setUp(self):
        self.card = PolicyCard()
        self.gpu = policy_gpu(self.card)

    def fail_table_read(self, nth):
        """The nth table read from now raises, as a one-off RM error would."""
        real, calls = self.gpu._power_policy_table, [0]

        def flaky():
            calls[0] += 1
            if calls[0] == nth:
                raise ValueError("policy request failed (NTSTATUS 0, RM 0x1A)")
            return real()
        self.gpu._power_policy_table = flaky

    def test_a_failed_table_read_while_writing_is_reported_not_raised(self):
        self.fail_table_read(2)                                    # the write's own read
        ok, message = self.gpu.set_power_policy(4, 150000)
        self.assertFalse(ok)
        self.assertIn("could not be read", message)
        self.assertEqual(self.card.sets, [])

    def test_a_failed_read_while_reapplying_leaves_the_power_limit_call_standing(self):
        self.gpu.set_power_policy(4, 150000)
        self.fail_table_read(2)                                    # re-apply's per-pin write
        ok, message = self.gpu.set_power_limit_mw(290000)
        self.assertFalse(ok)
        self.assertIn("power limit configured to 290 W", message)
        self.assertIn("FAILED", message)

    def test_reset_all_runs_every_step_when_a_policy_read_fails(self):
        self.gpu.set_power_policy(14, 4000000)
        g = self.gpu
        g.nvapi.VoltCtrlGet = g.nvapi.BoostTableSet = None
        g._volt_rail_profile = Mock(return_value=None)
        for name in ("set_clock_offset", "_reset_gpu_clocks", "reset_fan"):
            setattr(g, name, Mock(return_value=(True, "ok")))
        for name in ("clkdom_ok", "volt_rail_limits_supported", "_vf_lock_available"):
            setattr(g, name, Mock(return_value=False))
        real = g._write_policy_limit
        g._write_policy_limit = lambda p, v: (self.fail_table_read(1), real(p, v))[1]
        steps = g.reset_all()
        g.reset_fan.assert_called_once()                           # the steps after it still ran
        self.assertTrue(any(not ok and "could not be read" in message for ok, message in steps))

    def test_the_board_is_found_when_nvmls_range_is_unknown(self):
        for key in ("pl_min_mw", "pl_def_mw", "pl_max_mw"):
            self.gpu.static.pop(key)
        rows = rows_of(self.gpu)
        self.assertTrue(rows[2]["board"])                          # NVML's configured limit
        self.assertFalse(rows[2]["writable"])
        self.assertIn("Power limit slider", self.gpu.set_power_policy(2, 300000)[1])

    def test_while_the_board_cannot_be_told_apart_every_candidate_is_read_only(self):
        for key in ("pl_min_mw", "pl_def_mw", "pl_max_mw"):
            self.gpu.static.pop(key)
        self.card.request[14] = 260000                             # another tool: equal to the board
        rows = rows_of(self.gpu)
        for p in (2, 14):
            self.assertFalse(rows[p]["board"] or rows[p]["writable"], p)
            self.assertIn("as another policy's does", n.GPU.power_policy_unwritable_reason(rows[p]))
        self.assertFalse(self.gpu.set_power_policy(2, 300000)[0])
        self.assertTrue(self.gpu.set_power_limit_mw(280000)[0])    # now only policy 2 matches
        rows = rows_of(self.gpu)
        self.assertTrue(rows[2]["board"] and rows[14]["writable"])
        with patch.object(self.gpu, "read_power_limit_mw", return_value=None):
            self.assertTrue(rows_of(self.gpu)[2]["board"])         # remembered, not re-guessed

    def test_an_unreadable_power_limit_with_the_board_unknown_holds_every_mw_row(self):
        for key in ("pl_min_mw", "pl_def_mw", "pl_max_mw"):
            self.gpu.static.pop(key)
        with patch.object(self.gpu, "read_power_limit_mw", return_value=None):
            rows = rows_of(self.gpu)
        self.assertFalse(any(r["writable"] for r in rows.values() if r["unit"] == "mW"))
        self.assertTrue(rows[8]["writable"])                       # a current row is not a candidate
        self.assertIn("unreadable", n.GPU.power_policy_unwritable_reason(rows[4]))
        self.assertTrue(rows_of(self.gpu)[2]["board"])             # readable again: found

    def test_a_pin_is_never_reapplied_over_the_board(self):
        self.gpu._power_policy_pins = {2: 300000}                  # however it got there
        self.assertTrue(self.gpu.set_power_limit_mw(280000)[0])
        self.assertEqual(self.card.request[2], 280000)
        self.assertNotIn(2, self.gpu._power_policy_pins)

    def test_max_all_leaves_read_only_rows_alone_and_keeps_the_current_envelope(self):
        card = PolicyCard([row if row[0] != 14 else (14, 0x0B, 11, 0, 1, 4000000, 5001000)
                           for row in TITAN], coupled=(3, 4, 5, 7, 8, 9))
        card.control_type[14] = 0x07                               # a record read-only here
        gpu = policy_gpu(card)
        spec = n.GPU.CURRENT_LIMIT_GENERATION_POLICIES[n.GPU.ARCH_TURING][13]
        with patch.dict(spec, normal_maximum_ma=360000):
            steps = gpu.max_all_power_policies()
        self.assertTrue(all(ok for _step, ok, _msg in steps), steps)
        self.assertEqual(card.request[14], 4000000)                # read-only: not written
        self.assertEqual(card.request[13], 360000)                 # its slider's normal maximum

    def test_stock_refuses_a_default_outside_the_policys_own_range(self):
        card = PolicyCard([row if row[0] != 14 else (14, 0x0B, 11, 0, 1, 6000000, 5001000)
                           for row in TITAN])
        card.request[14] = 4000000                                 # the request itself is in range
        gpu = policy_gpu(card)
        ok, message = gpu.stock_power_policy(14)
        self.assertFalse(ok)
        self.assertIn("its default lies outside its own range", message)
        self.assertEqual(card.sets, [])

    def test_core_current_keeps_its_own_slider_through_a_failed_read(self):
        rows_of(self.gpu)                                          # read once: established
        with patch.object(self.gpu, "get_current_limits", return_value=[]):
            row = rows_of(self.gpu)[13]
        self.assertTrue(row["named_current"])
        self.assertFalse(row["writable"])

    def test_stock_keeps_the_note_about_re_applied_values(self):
        self.gpu.set_power_limit_mw(290000)
        self.gpu.set_power_policy(4, 150000)
        self.gpu.set_power_policy(5, 150000)
        ok, message = self.gpu.stock_power_policy(4)
        self.assertTrue(ok, message)
        self.assertIn("back to the driver's value: 155 W", message)
        self.assertIn("re-applied your values", message)           # policy 5, recomputed
        self.assertEqual(self.card.request[5], 150000)


class ReviewFixUiTests(PolicyUiCase):

    def test_typing_in_a_note_box_holds_the_curve_shortcuts(self):
        self.build()
        self.app.set_power_policy_note(4, choice="others")
        with patch("druta.druta.dpg.is_item_focused", side_effect=lambda t: t == "name_pp4"):
            self.assertTrue(self.app.typing())
        self.assertFalse(self.app.typing())

    def test_after_an_apply_only_that_slider_is_resynced(self):
        self.build()
        dpg.set_value("sl_pp8", 20.0)                              # staged elsewhere
        self.app.apply_power_policy(4, 150.0)
        self.assertEqual(dpg.get_value("sl_pp4"), 150)
        self.assertEqual(dpg.get_value("sl_pp8"), 20.0)

    def test_stock_resyncs_only_its_own_slider(self):
        self.build()
        self.app.apply_power_policy(4, 150.0)
        dpg.set_value("sl_pp8", 20.0)                              # staged elsewhere
        self.app.stock_power_policy(4)
        self.assertEqual(dpg.get_value("sl_pp4"), 143)
        self.assertEqual(dpg.get_value("sl_pp8"), 20.0)

    def test_stock_on_the_core_current_slider_keeps_a_staged_value_in_the_list(self):
        self.build()
        self.app.gpu.set_current_limit_ma(13, 360000)
        dpg.set_value("sl_pp8", 20.0)                              # staged elsewhere
        self.app.report = Mock()
        self.app.refresh_current_limits = Mock()
        self.app.stock_knob("current13")
        self.assertEqual(self.card.request[13], 350780)
        self.assertEqual(dpg.get_value("sl_pp8"), 20.0)

    def test_values_set_by_hand_follow_the_card_through_a_switch(self):
        self.build()
        self.app.gpu.set_power_policy(4, 150000)
        first = self.app.gpu
        other = policy_gpu(PolicyCard(), uuid="GPU-OTHER")
        self.app.hand_over_power_policies(other)
        self.app.gpu = other
        self.assertFalse(getattr(other, "_power_policy_pins", None))
        again = policy_gpu(self.card)                              # the first card, reopened
        self.app.hand_over_power_policies(again)
        self.assertEqual(again._power_policy_pins, {4: 150000})
        self.assertIsNot(again, first)

    def test_max_all_moves_the_power_slider_only_when_the_power_limit_took(self):
        with dpg.window():
            dpg.add_slider_float(tag="sl_pl", default_value=260)
        self.build()
        self.app.gpu.max_all_power_policies = Mock(return_value=[("power limit", False, "refused")])
        self.app.max_all_power_policies()
        self.assertEqual(dpg.get_value("sl_pl"), 260)

    def test_a_note_still_pending_is_saved_before_a_rebuild(self):
        self.build()
        self.app.set_power_policy_note(4, text="slot")             # inside the debounce
        dpg.destroy_context()                                      # the old tree goes, as in a rebuild
        dpg.create_context()
        self.build()
        self.assertEqual(policynames.load("GPU-TEST")[4]["name"], "slot")

    def test_a_read_only_row_says_why(self):
        self.card.control_type[14] = 0x07
        self.build()
        texts = [dpg.get_value(i) for i in dpg.get_all_items()
                 if dpg.get_item_type(i) == "mvAppItemType::mvText"]
        self.assertTrue(any("record type differs" in (t or "") for t in texts))


class ReleaseReviewBackendTests(unittest.TestCase):
    """The 1.7.0 release review: state the user set is kept when a profile
    says nothing about it."""

    def setUp(self):
        self.card = PolicyCard()
        self.gpu = policy_gpu(self.card)

    def test_a_profile_without_power_policy_data_keeps_the_values_set_by_hand(self):
        # 1.6.x profiles and sign-in copies carry no power-policy fields
        self.gpu.set_power_limit_mw(290000)
        self.gpu.set_power_policy(4, 150000)
        results = profiles.restore(self.gpu, {"schema": 2, "power_limit_mw": 290000,
                                              "current_limits_ma": {}}, apply_curve=False)
        self.assertTrue(all(ok for ok, _m in results), results)
        self.assertEqual(self.gpu._power_policy_pins, {4: 150000})
        self.assertEqual(self.card.request[4], 150000)            # put back after the board write

    def test_a_profile_with_power_policy_data_replaces_them(self):
        self.gpu.set_power_limit_mw(290000)
        self.gpu.set_power_policy(4, 150000)
        profiles.restore(self.gpu, {"schema": 2, "power_limit_mw": 290000, "current_limits_ma": {},
                                    "power_policies": {}, "power_policy_pins": {}},
                         apply_curve=False)
        self.assertEqual(self.gpu._power_policy_pins, {})
        self.assertNotEqual(self.card.request[4], 150000)         # the driver's value again


class ReleaseReviewUiTests(PolicyUiCase):
    def test_max_all_shows_the_board_limit_that_took_even_when_a_re_apply_failed(self):
        with dpg.window():
            dpg.add_slider_float(tag="sl_pl", default_value=260)
        self.build()
        self.card.request[self.card.board] = 320000                # NVML took the maximum
        self.app.gpu.max_all_power_policies = Mock(return_value=[
            ("power limit", False, "power limit configured to 320 W; re-applying your values FAILED")])
        self.app.max_all_power_policies()
        self.assertEqual(dpg.get_value("sl_pl"), 320)

    def test_the_mark_shows_only_a_value_the_card_still_holds(self):
        self.build()
        self.app.apply_power_policy(4, 150.0)
        self.app.refresh_power_policies(force=True)
        self.assertTrue(dpg.get_value("live_pp4").endswith(" *"))
        self.card.request[4] = 155000                              # another tool's board write
        self.app.refresh_power_policies(force=True)
        self.assertFalse(dpg.get_value("live_pp4").endswith(" *"))
        self.assertEqual(self.app.gpu._power_policy_pins, {4: 150000})   # Druta's next write puts it back

    def test_typing_in_a_value_box_holds_the_curve_shortcuts(self):
        self.build()
        with patch("druta.druta.dpg.is_item_focused", side_effect=lambda t: t == "in_pp4"):
            self.assertTrue(self.app.typing())

    def test_a_card_with_no_uuid_keeps_its_notes_and_values_for_the_session(self):
        self.app.gpu = policy_gpu(self.card, uuid=None)
        self.app.gpu.slot = lambda: "0000:01:00.0"
        self.build()
        self.app.set_power_policy_note(4, text="aux input")
        dpg.destroy_context()                                      # Refresh capabilities rebuilds
        dpg.create_context()
        self.build()
        self.assertEqual(self.app._policy_names[4]["name"], "aux input")
        self.app.set_power_policy_note(4, text="aux input 2")
        self.app.save_power_policy_names(force=True)
        said = [c for c in self.app.log.call_args_list if "no UUID" in str(c)]
        self.assertEqual(len(said), 1)                             # once, not on every pause
        self.app.gpu.set_power_policy(4, 150000)
        other = policy_gpu(PolicyCard(), uuid="GPU-OTHER")
        self.app.hand_over_power_policies(other)
        self.app.gpu = other
        again = policy_gpu(self.card, uuid=None)                   # the first card, by its slot
        again.slot = lambda: "0000:01:00.0"
        self.app.hand_over_power_policies(again)
        self.assertEqual(again._power_policy_pins, {4: 150000})


class ReviewFixNameStoreTests(PolicyNameStoreCase):
    def test_a_hidden_name_survives_a_save(self):
        policynames.save("GPU-A", {9: {"name": "was here", "channel": 5, "type": 4}}, root=self.root)
        rows = [{"policy": 9, "channel": 6, "type": 4}]           # renumbered: hidden now
        policynames.save("GPU-A", {4: {"name": "slot", "channel": 4, "type": 3}}, rows=rows,
                         root=self.root)
        self.assertEqual(sorted(policynames.load("GPU-A", root=self.root)), [4, 9])

    def test_an_unreadable_store_is_set_aside_not_replaced(self):
        path = Path(self.root, "power-policy-names.json")
        path.write_text('{"GPU-B": {"1": {"name": "x", "channel"', encoding="utf-8")
        ok, message = policynames.save("GPU-A", {4: {"name": "slot", "channel": 4, "type": 3}},
                                       root=self.root)
        self.assertTrue(ok)
        self.assertIn("unreadable", message)
        kept = [f for f in Path(self.root).iterdir() if ".unreadable-" in f.name]
        self.assertEqual(len(kept), 1)
        self.assertIn("GPU-B", kept[0].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
