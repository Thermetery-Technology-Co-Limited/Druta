# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Hardware-free current-policy guards, preservation and effective readback."""
import ctypes
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from druta import nvbackend as n
from tests.fixture_current_47212 import TITAN_INFO


def fixture(arch=10, newer=False, legacy=False):
    g = n.GPU.__new__(n.GPU)
    g._lock = threading.RLock()
    g.static = {"driver": "580.97", "vbios": "98.03.3b.c0.6f"}
    g.nvapi = SimpleNamespace(ok=True, selected={"devid": 0x2C02,
                                                "slot": "0000:01:00.0"})
    g.arch = Mock(return_value=arch)
    g.voltage_xoc_enabled = False
    generation = {
        10: (0x3FFFF, ((13, 0x12, 13, 300000, 5001000),
                       (14, 0x12, 12, 120000, 5001000))),
        n.GPU.ARCH_TURING: (0xEBBF, ((13, 0x0B, 19, 350780, 390000),)),
        n.GPU.ARCH_PASCAL: (0xEDBF, ((13, 0x0B, 11, 204700, 218000),)),
    }
    mask, policies = generation.get(arch, (0, ()))
    packet_bytes, param_bytes = (54420, 54352) if newer else (11748, 11680)
    info_bytes, status_bytes, control_bytes = ((20000, 396024, 13840) if newer
                                              else (8620, 172080, 4432))
    info_base, info_stride = (0xCC, 0xFC) if newer else (0x58, 0xE4)
    status_base, status_stride = (0x9C, 0x1720) if newer else (0x70, 0x1454)
    control_base, control_stride = (0x14, 0xC4) if newer else (0x14, 0x7C)
    status_mask_word = 0 if legacy else 1
    wire = {c: c for c in (0x2080A618, 0x2080A619, 0x2080A61A, 0x2080E61B)}
    if legacy:
        packet_bytes, param_bytes = 3548, 3480
        info_bytes, status_bytes, control_bytes = 5520, 69148, 2124
        info_base, info_stride = 0x3C, 0x84
        status_base, status_stride = 0x64, 0x828
        control_base, control_stride = 0x14, 0x34
        wire = dict(zip(wire, (0x20802618, 0x20802619, 0x2080261A, 0x2080261B)))
    info = [0] * (info_bytes // 4)
    info[1] = mask
    control = [(i * 17) & 0xFFFFFFFF for i in range(control_bytes // 4)]
    control[:5] = [0, 0, 0, 255, mask]
    dynamic = [0] * (status_bytes // 4)
    dynamic[status_mask_word] = mask
    for policy, type_id, channel, default, maximum in policies:
        meta = (info_base + policy * info_stride) // 4
        state = (status_base + policy * status_stride) // 4
        record = (control_base + policy * control_stride) // 4
        info[meta + 1:meta + 5] = [type_id | channel << 8 | 1 << 16,
                                   1, default, maximum]
        control[record:record + 2] = [type_id, default]
        dynamic[state:state + 3] = [type_id, default, default // 2]
    state = SimpleNamespace(info=info, control=control, dynamic=dynamic,
                            writes=[], store_only=False, refused=False,
                            break_get_once=False, break_restore=False,
                            mutate_other=False, failed_after_set=False)
    header = [0] * 17
    header[2], header[14], header[15] = packet_bytes, 0x2080A612, param_bytes
    if legacy:
        header[14] = 0x20802612
    header[12:14] = [1234, 5678]
    fields = dict(hAdapter=3, hDevice=0, Type=0, Flags=8, hContext=0)
    g._capture_current_limit_transport = Mock(return_value=(header, fields))

    def escape(packet, observed_fields):
        assert observed_fields == fields
        assert list(packet[12:14]) == [1234, 5678]
        assert packet[2] == ctypes.sizeof(packet)
        assert packet[15] == (len(packet) - 17) * 4
        command = next(c for c, actual in wire.items() if actual == packet[14])
        if command == 0x2080E61B:
            params = list(packet[17:])
            state.writes.append(params)
            if state.refused or (state.break_restore and len(state.writes) > 1):
                packet[16] = 31
                return 0
            policy = {1 << 13: 13, 1 << 14: 14}[params[4]]
            record = (control_base + policy * control_stride) // 4
            end = record + control_stride // 4
            state.control[record:end] = params[record:end]
            if not state.store_only:
                offset = (status_base + policy * status_stride) // 4
                state.dynamic[offset + 1] = params[record + 1]
            if state.mutate_other:
                state.control[10] += 1
            if state.failed_after_set and len(state.writes) == 1:
                raise OSError("transport failed after dispatch")
            return 0
        if state.break_get_once and state.writes:
            state.break_get_once = False
            raise OSError("readback disconnected")
        data = {0x2080A618: state.info, 0x2080A619: state.dynamic,
                0x2080A61A: state.control}[command]
        if command == 0x2080A618:
            assert not any(packet[17:])
        elif command == 0x2080A619:
            live_mask = state.info[1]
            assert list(packet[17:19]) == ([live_mask, 0] if legacy else [0, live_mask])
            assert not any(packet[19:])
        else:
            assert list(packet[17:22]) == [0, 0, 0, 0, state.info[1]]
            assert not any(packet[22:])
        packet[17:] = data
        return 0

    g._legacy_clk_escape = Mock(side_effect=escape)
    return g, state


class CurrentLimitTests(unittest.TestCase):
    def test_original_47212_titan_info_capture_decodes_current_descriptor(self):
        g, state = fixture(n.GPU.ARCH_TURING, legacy=True)
        state.info = list(TITAN_INFO)
        row = g.get_current_limits()[0]
        self.assertEqual((row['policy'], row['channel'], row['minimum_ma'],
                          row['default_ma'], row['maximum_ma']),
                         (13, 19, 1000, 350780, 390000))
        self.assertEqual(state.writes, [])

    def test_legacy_turing_layout_reads_and_writes_one_current_word(self):
        g, state = fixture(n.GPU.ARCH_TURING, legacy=True)
        original = list(state.control)
        self.assertEqual(g.get_current_limits()[0]['limit_ma'], 350780)
        self.assertEqual(state.writes, [])
        ok, message = g.set_current_limit_ma(13, 349780)
        self.assertTrue(ok, message)
        expected = list(original)
        expected[(0x14 + 13 * 0x34) // 4 + 1] = 349780
        self.assertEqual(state.control, expected)
        expected[4] = 1 << 13
        self.assertEqual(state.writes, [expected])
        self.assertEqual(len(expected) * 4, 2124)

    def test_legacy_uncertain_set_restores_and_checks_effective_limit(self):
        for flag in ('store_only', 'failed_after_set', 'break_get_once'):
            with self.subTest(flag=flag):
                g, state = fixture(n.GPU.ARCH_TURING, legacy=True)
                original = list(state.control)
                setattr(state, flag, True)
                ok, message = g.set_current_limit_ma(13, 349780)
                self.assertFalse(ok)
                self.assertIn('restored and verified', message)
                self.assertEqual(state.control, original)
                self.assertEqual(len(state.writes), 2)

    def test_legacy_status_mask_position_and_command_geometry_are_required(self):
        g, state = fixture(n.GPU.ARCH_TURING, legacy=True)
        state.dynamic[1], state.dynamic[0] = state.dynamic[0], 0
        self.assertEqual(g.get_current_limits(), [])
        self.assertFalse(g.set_current_limit_ma(13, 349780)[0])
        self.assertEqual(state.writes, [])
        g, state = fixture(n.GPU.ARCH_TURING, legacy=True)
        g._capture_current_limit_transport.return_value[0][14] = 0x2080A612
        self.assertEqual(g.get_current_limits(), [])
        g._legacy_clk_escape.assert_not_called()

    def test_new_geometry_reads_each_generation_independently_of_identity(self):
        for arch, defaults in ((n.GPU.ARCH_TURING, [350780]),
                               (n.GPU.ARCH_PASCAL, [204700]),
                               (10, [300000, 120000])):
            with self.subTest(arch=arch):
                g, state = fixture(arch, newer=True)
                # A deliberately stale driver label must not select the ABI.
                g.static["driver"] = "580.97"
                g.nvapi.selected["devid"] = 0xFFFF
                self.assertEqual([r["limit_ma"] for r in g.get_current_limits()],
                                 defaults)
                self.assertEqual(state.writes, [])

    def test_new_geometry_preserves_full_getter_buffer_and_changes_one_word(self):
        for arch, policy, target in ((n.GPU.ARCH_TURING, 13, 349780),
                                     (10, 14, 180000)):
            with self.subTest(arch=arch, policy=policy):
                g, state = fixture(arch, newer=True)
                original = list(state.control)
                ok, message = g.set_current_limit_ma(policy, target)
                self.assertTrue(ok, message)
                word = (0x14 + policy * 0xC4) // 4 + 1
                expected = list(original)
                expected[word] = target
                self.assertEqual(state.control, expected)
                expected[4] = 1 << policy
                self.assertEqual(len(expected) * 4, 13840)
                self.assertEqual(state.writes, [expected])
                self.assertEqual(g.get_current_limits()[-1]["limit_ma"], target)

    def test_new_geometry_verification_and_uncertain_writes_restore_full_record(self):
        for flag in ("store_only", "break_get_once", "failed_after_set"):
            with self.subTest(flag=flag):
                g, state = fixture(n.GPU.ARCH_TURING, newer=True)
                original = list(state.control)
                setattr(state, flag, True)
                ok, message = g.set_current_limit_ma(13, 349780)
                self.assertFalse(ok)
                self.assertIn("restored and verified", message)
                self.assertEqual(state.control, original)
                restore = list(original)
                restore[4] = 1 << 13
                self.assertEqual(state.writes[-1], restore)
                self.assertEqual(len(state.writes), 2)

    def test_unknown_or_mixed_transport_geometry_never_dispatches(self):
        for packet_bytes, param_bytes, command in (
                (54424, 54356, 0x2080A612),
                (54420, 11680, 0x2080A612),
                (11748, 54352, 0x2080A612),
                (54420, 54352, 0x20802612)):
            with self.subTest(packet_bytes=packet_bytes, param_bytes=param_bytes,
                              command=command):
                g, state = fixture(n.GPU.ARCH_TURING, newer=True)
                header, _ = g._capture_current_limit_transport.return_value
                header[2], header[14], header[15] = packet_bytes, command, param_bytes
                self.assertEqual(g.get_current_limits(), [])
                self.assertFalse(g.set_current_limit_ma(13, 349780)[0])
                g._legacy_clk_escape.assert_not_called()

    def test_new_transport_rejects_legacy_getter_sizes_and_record_offsets(self):
        for block in ("info", "control", "dynamic"):
            g, state = fixture(n.GPU.ARCH_TURING, newer=True)
            _, legacy = fixture(n.GPU.ARCH_TURING)
            responses = [state.info, state.control, state.dynamic]
            responses[("info", "control", "dynamic").index(block)] = getattr(legacy, block)
            g._current_limit_rm = Mock(side_effect=responses)
            self.assertEqual(g.get_current_limits(), [])
        g, state = fixture(n.GPU.ARCH_TURING, newer=True)
        _, legacy = fixture(n.GPU.ARCH_TURING)
        legacy_request = list(legacy.control)
        legacy_request[4] = 1 << 13
        with self.assertRaisesRegex(ValueError, "buffer size"):
            g._current_limit_rm(0x2080E61B, legacy_request)
        g._legacy_clk_escape.assert_not_called()
        # Valid lengths with a descriptor only at the old position also fail.
        state.info[2:] = [0] * (len(state.info) - 2)
        state.info[2:len(legacy.info)] = legacy.info[2:]
        self.assertEqual(g.get_current_limits(), [])
        self.assertFalse(g.set_current_limit_ma(13, 349780)[0])
        self.assertEqual(state.writes, [])

    def test_reads_both_live_currents_and_reuses_only_this_client_transport(self):
        g, state = fixture()
        rows = g.get_current_limits()
        self.assertEqual([(r["policy"], r["limit_ma"], r["value_ma"])
                          for r in rows], [(13, 300000, 150000),
                                          (14, 120000, 60000)])
        self.assertEqual([r["normal_maximum_ma"] for r in rows], [500000, 200000])
        self.assertEqual([r["maximum_ma"] for r in rows], [5001000, 5001000])
        state.dynamic[(0x70 + 14 * 0x1454) // 4 + 2] = 12345
        self.assertEqual(g.get_current_limits()[1]["value_ma"], 12345)
        self.assertEqual(g._capture_current_limit_transport.call_count, 1)
        self.assertEqual(state.writes, [])

    def test_unknown_generation_or_unavailable_nvapi_never_opens_transport(self):
        for field, value in (("arch", 8), ("ok", False)):
            g, state = fixture()
            if field == "ok":
                g.nvapi.ok = value
            else:
                g.arch.return_value = value
            self.assertEqual(g.get_current_limits(), [])
            self.assertFalse(g.set_current_limit_ma(13, 400000)[0])
            g._capture_current_limit_transport.assert_not_called()
            g._legacy_clk_escape.assert_not_called()

    def test_identity_fields_never_gate_a_supported_generation(self):
        for field, value in (("devid", 0xFFFF), ("driver", "different"),
                             ("vbios", "different")):
            g, _ = fixture()
            if field == "devid":
                g.nvapi.selected[field] = value
            else:
                g.static[field] = value
            self.assertTrue(g.get_current_limits())

    def test_pascal_and_turing_descriptors_use_returned_ranges_and_masks(self):
        for arch, mask, default, maximum in (
                (n.GPU.ARCH_TURING, 0xEBBF, 350780, 390000),
                (n.GPU.ARCH_PASCAL, 0xEDBF, 204700, 218000)):
            g, state = fixture(arch)
            rows = g.get_current_limits()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["policy"], 13)
            self.assertEqual(rows[0]["default_ma"], default)
            self.assertEqual(rows[0]["maximum_ma"], maximum)
            self.assertEqual(rows[0]["normal_maximum_ma"], maximum)
            self.assertEqual(state.info[1], mask)
            self.assertTrue(g.set_current_limit_ma(13, default + 1000)[0])
            self.assertFalse(g.set_current_limit_ma(14, 1)[0])

    def test_board_channel_and_higher_api_maximum_do_not_hide_current_policy(self):
        for arch in (n.GPU.ARCH_PASCAL, n.GPU.ARCH_TURING, 10):
            with self.subTest(arch=arch):
                g, state = fixture(arch)
                meta = (0x58 + 13 * 0xE4) // 4
                state.info[meta + 1] = (state.info[meta + 1] & ~0xFF00) | (22 << 8)
                state.info[meta + 4] = 6_000_000
                row = g.get_current_limits()[0]
                self.assertEqual(row['maximum_ma'], 6_000_000)
                self.assertEqual(row['normal_maximum_ma'], 500_000)
                self.assertEqual(row['channel'], 22)
                self.assertIn('channel 22', row['label'])
                self.assertFalse(g.set_current_limit_ma(13, 600_000)[0])
                g.voltage_xoc_enabled = True
                self.assertTrue(g.set_current_limit_ma(13, 600_000)[0])
                self.assertEqual(g.get_current_limits()[0]['limit_ma'], 600_000)
                self.assertFalse(g.set_current_limit_ma(13, 6_000_001)[0])

    def test_malformed_current_record_does_not_hide_other_valid_policy(self):
        meta = (0x58 + 13 * 0xE4) // 4
        cases = [("info", 1, 0), ("dynamic", 1, 0), ("control", 4, 0),
                 ("control", 3, 0),
                 ("info", meta + 1, 0x00000D12),
                 ("info", meta + 1, 0x00010D11),
                 ("info", meta + 2, 0), ("info", meta + 3, 0),
                 ("info", meta + 4, 0),
                 ("control", (0x14 + 13 * 0x7C) // 4, 0x11),
                 ("dynamic", (0x70 + 13 * 0x1454) // 4, 0x11),
                 ("dynamic", (0x70 + 13 * 0x1454) // 4 + 1, 0),
                 ("dynamic", (0x70 + 13 * 0x1454) // 4 + 1, 5001001)]
        for name, index, value in cases:
            with self.subTest(name=name, index=index, value=value):
                g, state = fixture()
                getattr(state, name)[index] = value
                global_failure = (name, index) in (("info", 1), ("dynamic", 1),
                                                    ("control", 4), ("control", 3))
                self.assertEqual([r["policy"] for r in g.get_current_limits()],
                                 [] if global_failure else [14])
                self.assertFalse(g.set_current_limit_ma(13, 400000)[0])
                self.assertEqual(state.writes, [])
                if not global_failure:
                    self.assertTrue(g.set_current_limit_ma(14, 150000)[0])
                    self.assertTrue(g.current_limit_diagnostics()["unavailable_policies"][0]["present"])
        g, state = fixture()
        g._current_limit_rm = Mock(side_effect=[state.info[:-1], state.control,
                                               state.dynamic])
        self.assertEqual(g.get_current_limits(), [])

    def test_single_current_rail_and_extra_policies_use_full_abi_mask(self):
        for legacy, newer in ((True, False), (False, False), (False, True)):
            for surviving in (13, 14):
                with self.subTest(legacy=legacy, newer=newer, surviving=surviving):
                    g, state = fixture(legacy=legacy, newer=newer)
                    mask = (1 << surviving) | (1 << 31)
                    state.info[1] = state.control[4] = mask
                    state.dynamic[0 if legacy else 1] = mask
                    original = list(state.control)
                    rows = g.get_current_limits()
                    self.assertEqual([row["policy"] for row in rows], [surviving])
                    self.assertEqual(g._current_limit_error, "")
                    absent, = g.current_limit_diagnostics()["unavailable_policies"]
                    self.assertFalse(absent["present"])
                    self.assertTrue(g.set_current_limit_ma(surviving, 150000)[0])
                    changes = [i for i, (a, b) in enumerate(zip(original, state.control))
                               if a != b]
                    self.assertEqual(len(changes), 1)
                    self.assertEqual(state.writes[0][4], 1 << surviving)
                    self.assertEqual(state.control[4], mask)

    def test_policy_mask_capacity_is_derived_from_all_known_buffers(self):
        for layout in n.GPU._CURRENT_LIMIT_LAYOUTS.values():
            self.assertEqual(n.GPU._current_limit_capacity(layout), 32)
        g, state = fixture()
        state.info[1] |= 1 << 32
        g._current_limit_rm = Mock(return_value=state.info)
        self.assertEqual(g.get_current_limits(), [])
        self.assertIn("ABI capacity", g._current_limit_error)
        self.assertEqual(g._current_limit_rm.call_count, 1)

    def test_no_known_currents_is_absence_not_a_global_read_failure(self):
        for mask in (0, 1 << 31):
            g, state = fixture()
            state.info[1] = state.control[4] = state.dynamic[1] = mask
            self.assertEqual(g.get_current_limits(), [])
            self.assertEqual(g._current_limit_error, "")
            unavailable = g.current_limit_diagnostics()["unavailable_policies"]
            self.assertEqual({r["policy"] for r in unavailable}, {13, 14})
            self.assertTrue(all(not r["present"] for r in unavailable))
            self.assertFalse(g.set_current_limit_ma(13, 400000)[0])
            self.assertEqual(state.writes, [])

    def test_other_current_disagreement_does_not_suppress_selected_policy_write(self):
        for fail_after_dispatch in (False, True):
            g, state = fixture()
            state.dynamic[(0x70 + 14 * 0x1454) // 4 + 1] = 110000
            state.failed_after_set = fail_after_dispatch
            ok, message = g.set_current_limit_ma(13, 400000)
            self.assertEqual(ok, not fail_after_dispatch, message)
            if fail_after_dispatch:
                self.assertIn("restored and verified", message)
            self.assertEqual(g.get_current_limits()[1]["limit_ma"], 110000)

    def test_only_selected_limit_changes_and_effective_readback_is_required(self):
        for policy, target in ((13, 400000), (14, 175000)):
            g, state = fixture()
            original = list(state.control)
            ok, message = g.set_current_limit_ma(policy, target)
            self.assertTrue(ok, message)
            word = (0x14 + policy * 0x7C) // 4 + 1
            self.assertEqual([i for i, pair in enumerate(zip(original, state.control))
                              if pair[0] != pair[1]], [word])
            self.assertEqual(state.writes[0][4], 1 << policy)
            self.assertEqual(state.control[word], target)
            expected = list(original)
            expected[4], expected[word] = 1 << policy, target
            self.assertEqual(state.writes, [expected])

    def test_normal_limits_and_xoc_inclusive_api_bound(self):
        for policy, maximum in ((13, 500000), (14, 200000)):
            g, state = fixture()
            self.assertFalse(g.set_current_limit_ma(policy, maximum + 1)[0])
            self.assertEqual(state.writes, [])
            self.assertTrue(g.set_current_limit_ma(policy, maximum)[0])
            g.voltage_xoc_enabled = True
            self.assertTrue(g.set_current_limit_ma(policy, 5001000)[0])
            count = len(state.writes)
            self.assertFalse(g.set_current_limit_ma(policy, 5001001)[0])
            self.assertEqual(len(state.writes), count)

    def test_leaving_xoc_preserves_high_value_and_allows_only_lowering(self):
        g, state = fixture()
        g.voltage_xoc_enabled = True
        self.assertTrue(g.set_current_limit_ma(13, 700000)[0])
        g.voltage_xoc_enabled = False
        self.assertTrue(g.set_current_limit_ma(13, 700000)[0])
        self.assertEqual(len(state.writes), 1)
        self.assertFalse(g.set_current_limit_ma(13, 700001)[0])
        self.assertTrue(g.set_current_limit_ma(13, 600000)[0])
        self.assertFalse(g.set_current_limit_ma(13, 600001)[0])
        self.assertTrue(g.set_current_limit_ma(13, 300000)[0])

    def test_large_current_messages_preserve_milliamp_precision(self):
        g, state = fixture()
        g.voltage_xoc_enabled = True
        ok, message = g.set_current_limit_ma(13, 4999999)
        self.assertTrue(ok, message)
        self.assertIn("4999.999 A", message)
        ok, message = g.set_current_limit_ma(13, 4999999)
        self.assertTrue(ok, message)
        self.assertIn("already at 4999.999 A", message)
        g.voltage_xoc_enabled = False
        ok, message = g.set_current_limit_ma(13, 5000000)
        self.assertFalse(ok)
        self.assertIn("0.001 and 4999.999 A", message)

    def test_invalid_inputs_never_write(self):
        for policy, value in ((12, 1), (15, 1), (True, 1), ("13", 1),
                              (13, True), (13, "400000"), (13, 1.5),
                              (13, -1), (13, 0), (13, float("nan")),
                              (14, 1 << 32)):
            g, state = fixture()
            self.assertFalse(g.set_current_limit_ma(policy, value)[0])
            self.assertEqual(state.writes, [])

    def test_stored_only_success_restores_and_reports_failure(self):
        g, state = fixture()
        original = list(state.control)
        state.store_only = True
        ok, message = g.set_current_limit_ma(13, 400000)
        self.assertFalse(ok)
        self.assertIn("effective", message)
        self.assertIn("restored and verified", message)
        self.assertEqual(state.control, original)
        self.assertEqual(len(state.writes), 2)

    def test_get_or_dispatch_exception_does_not_skip_restoration(self):
        for flag in ("break_get_once", "failed_after_set"):
            g, state = fixture()
            original = list(state.control)
            setattr(state, flag, True)
            ok, message = g.set_current_limit_ma(14, 180000)
            self.assertFalse(ok)
            self.assertIn("restored and verified", message)
            self.assertEqual(state.control, original)
            self.assertEqual(len(state.writes), 2)

    def test_failed_restore_is_never_reported_as_success(self):
        g, state = fixture()
        state.store_only = True
        state.break_restore = True
        ok, message = g.set_current_limit_ma(14, 180000)
        self.assertFalse(ok)
        self.assertIn("restoration could not be verified", message)
        self.assertIn("RM 0x1F", message)

    def test_unrelated_control_change_is_detected_and_not_overwritten(self):
        g, state = fixture()
        original = list(state.control)
        state.mutate_other = True
        ok, message = g.set_current_limit_ma(13, 400000)
        self.assertFalse(ok)
        self.assertIn("restoration could not be verified", message)
        self.assertNotEqual(state.control[10], original[10])
        self.assertEqual(state.control[409], original[409])
        self.assertTrue(all(w[4] == 1 << 13 for w in state.writes))

    def test_selected_policy_disagreement_blocks_a_new_write(self):
        g, state = fixture()
        state.dynamic[(0x70 + 13 * 0x1454) // 4 + 1] = 295000
        ok, message = g.set_current_limit_ma(13, 400000)
        self.assertFalse(ok)
        self.assertIn("nothing written", message)
        self.assertEqual(state.writes, [])

    def test_transport_rejects_unlisted_commands_wrong_sizes_and_broad_write_masks(self):
        for command, params in ((0x2080A612, None), (0x2080E61B, None),
                                (0x2080E61B, [0] * 1108),
                                (0x2080E61B, [0] * 1107)):
            g, state = fixture()
            with self.assertRaises(ValueError):
                g._current_limit_rm(command, params)
            g._capture_current_limit_transport.assert_not_called()

    def test_ntstatus_failure_discards_client_transport(self):
        g, state = fixture()
        self.assertTrue(g.get_current_limits())
        g._legacy_clk_escape.side_effect = lambda packet, fields: -1
        self.assertEqual(g.get_current_limits(), [])
        self.assertIsNone(g._current_limit_transport)

    def test_reset_all_restores_reported_defaults_only_when_changed(self):
        g, state = fixture()
        self.assertTrue(g.set_current_limit_ma(13, 400000)[0])
        self.assertTrue(g.set_current_limit_ma(14, 180000)[0])
        g.static["pl_def_mw"] = 360000
        g.nvapi.VoltCtrlGet = None
        g.nvapi.BoostTableSet = None
        # This fixture models current policy transport, not voltage rail state.
        g._volt_rail_profile = Mock(return_value=None)
        for name in ("set_clock_offset", "set_power_limit_mw", "_reset_gpu_clocks",
                     "reset_fan"):
            setattr(g, name, Mock(return_value=(True, "ok")))
        for name in ("clkdom_ok", "volt_rail_limits_supported", "_vf_lock_available"):
            setattr(g, name, Mock(return_value=False))
        results = g.reset_all()
        self.assertTrue(all(result[0] for result in results))
        self.assertEqual([r["limit_ma"] for r in g.get_current_limits()],
                         [300000, 120000])
        count = len(state.writes)
        g.reset_all()
        self.assertEqual(len(state.writes), count)
        g._legacy_clk_escape.side_effect = OSError("GPU disconnected")
        results = g.reset_all()
        self.assertTrue(any(not result[0] and "cannot read current limits" in result[1]
                            for result in results))


class CurrentTransportCaptureTests(unittest.TestCase):
    def exercise(self, *, fail=False, owner=True, newer=False, mismatch=False):
        g, _ = fixture()
        del g._capture_current_limit_transport
        g.nvapi.gpu = object()
        g.nvapi.ver = lambda typ, version: ctypes.sizeof(typ) | version << 16
        code = ctypes.create_string_buffer(b"original bytes")
        original_code = bytes(code)
        address = ctypes.addressof(code)
        callback_storage = ctypes.create_string_buffer(14)
        architecture = (n.u32 * 33)()
        architecture[2], architecture[14], architecture[15] = 132, 0x20800111, 64
        packet_bytes, param_bytes = (54420, 54352) if newer else (11748, 11680)
        power = (n.u32 * (packet_bytes // 4))()
        power[2], power[14], power[15] = packet_bytes, 0x2080A612, param_bytes
        if mismatch:
            power[2] += 4
        power[12:14] = [9988, 7766]
        packets = [architecture, power]
        escapes = [n.GPU._Escape(hAdapter=987, hDevice=0, Type=0, Flags=8,
                                hContext=0, pPrivateDriverData=ctypes.addressof(p),
                                PrivateDriverDataSize=ctypes.sizeof(p)) for p in packets]
        before = [bytes(p) for p in packets]
        seen = []

        def real(pointer):
            self.assertEqual(bytes(code), original_code)
            seen.append(pointer)
            return 0

        def prototype(result_type, argument_type):
            def bind(target):
                if callable(target):
                    def getter(handle, block):
                        self.assertIs(handle, g.nvapi.gpu)
                        for index, escape in enumerate(escapes):
                            target(ctypes.addressof(escape))
                            if index == 0:
                                self.assertNotEqual(bytes(code), original_code)
                            if fail:
                                raise OSError("getter interrupted")
                        return 0
                    g.nvapi.PowerPolInfo = getter
                    return ctypes.c_void_p(ctypes.addressof(callback_storage))
                self.assertEqual(target, address)
                return real
            return bind

        def protect(pointer, size, flags, previous):
            previous._obj.value = 0x20
            return 1

        kernel = SimpleNamespace(
            VirtualProtect=Mock(side_effect=protect),
            GetCurrentThreadId=Mock(side_effect=[731, 731 if owner else 999,
                                                731 if owner else 999]),
            GetCurrentProcess=Mock(return_value=42),
            FlushInstructionCache=Mock(return_value=1))
        g._legacy_clk_gdi = Mock(return_value=SimpleNamespace(
            D3DKMTEscape=ctypes.c_void_p(address)))
        g.nvapi.PowerPolInfo = Mock()
        with patch("druta.nvbackend.ctypes.WinDLL", return_value=kernel), \
                patch("druta.nvbackend.ctypes.WINFUNCTYPE", new=prototype, create=True):
            if fail:
                with self.assertRaisesRegex(OSError, "getter interrupted"):
                    g._capture_current_limit_transport()
                result = None
            else:
                result = g._capture_current_limit_transport()
        self.assertEqual(bytes(code), original_code)
        self.assertEqual([bytes(p) for p in packets], before)
        self.assertEqual(kernel.VirtualProtect.call_count, 2)
        self.assertEqual(kernel.VirtualProtect.call_args.args[2], 0x20)
        self.assertEqual(len(seen), 1 if fail else 2)
        return result

    def test_capture_only_reads_and_restores_bytes_before_each_call(self):
        for newer in (False, True):
            with self.subTest(newer=newer):
                header, fields = self.exercise(newer=newer)
                self.assertEqual(header[12:14], [9988, 7766])
                self.assertEqual(fields["hAdapter"], 987)
                self.assertIsNone(self.exercise(newer=newer, mismatch=True))

    def test_exception_and_wrong_thread_restore_hook_without_a_transport(self):
        self.assertIsNone(self.exercise(fail=True))
        self.assertIsNone(self.exercise(owner=False))


if __name__ == "__main__":
    unittest.main()
