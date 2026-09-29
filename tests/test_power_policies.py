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
from druta.druta import Druta

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
                words[r:r + 2] = [s["type"], self.request[p]]
        elif command == A619:
            words[1] = self.mask
            for p, s in self.spec.items():
                st = (STATUS[0] + p * STATUS[1]) // 4
                words[st:st + 3] = [s["type"], self.effective(p), 1000 + p]
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
        self.assertEqual(state["power_policy_pins"], {"4": 150000})     # not the derived ones
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

    def test_saved_values_need_the_same_card_and_must_fit_its_table(self):
        self.gpu.set_power_policy(4, 150000)
        state = json.loads(json.dumps(profiles.capture(self.view())))
        other = copy.deepcopy(state)
        other["device"]["uuid"] = "GPU-OTHER"
        self.assertIsNotNone(profiles.preflight(self.gpu, other))
        for pins in ({"4": 168000}, {"6": 1000}, {"2": 300000}, {"4": -1}, {"x": 1}):
            with self.subTest(pins=pins):
                bad = copy.deepcopy(state)
                bad["power_policy_pins"] = pins
                self.assertIsNotNone(profiles.preflight(self.gpu, bad))
        self.assertIsNone(profiles.preflight(self.gpu, state))


class PolicyNameStoreTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = folder.name

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


class PolicyUiTests(unittest.TestCase):
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
        self.assertIn("(yours)", dpg.get_value("live_pp4"))

    def test_a_typed_name_is_saved_for_this_card_and_carried_by_profiles(self):
        self.build()
        self.app.rename_power_policy(4, "  8-pin #1  ")
        self.app.save_power_policy_names(force=True)
        self.assertEqual(policynames.load("GPU-TEST"), {4: {"name": "8-pin #1", "channel": 4, "type": 3}})
        self.assertEqual(self.app.gpu.power_policy_names[4]["name"], "8-pin #1")
        dpg.destroy_context()
        dpg.create_context()
        self.build()                                             # a fresh session starts with it
        self.assertEqual(dpg.get_value("name_pp4"), "8-pin #1")

    def test_a_loaded_profiles_names_become_this_cards_names(self):
        self.build()
        self.app.apply_profile_policy_names(
            {"power_policy_names": {"7": {"name": "slot 12 V", "channel": 3, "type": 4},
                                    "8": {"name": "wrong channel", "channel": 9, "type": 4}}})
        self.assertEqual(dpg.get_value("name_pp7"), "slot 12 V")
        self.assertEqual(dpg.get_value("name_pp8"), "")
        self.app.save_power_policy_names()
        self.assertEqual(list(policynames.load("GPU-TEST")), [7])


if __name__ == "__main__":
    unittest.main()
