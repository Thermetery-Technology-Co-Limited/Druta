# Druta - hardware-free tests for preserving paged VRM register state.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later

from pathlib import Path
import tomllib
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from druta import railctl


def make_rail(*, guarded=True, original=0xA005, bits="7:0"):
    # Synthetic register layout: this tests the generic writer, and makes no
    # claim about the still-investigated MP29816 offset register encoding.
    data = {
        "profile": {"format": 1, "name": "test controller", "rail": "test",
                    "runtime_checks": guarded},
        "bus": {"port": 2, "addr7": 0x30},
        "identity": [{"reg": 0, "bytes": 1, "equals": 0},
                     {"reg": 0x29, "bytes": 2, "mask": 0x1C00, "value": 0x400}],
        "telemetry": [{"key": "vout_mv", "reg": 0x8B, "bytes": 2,
                       "encoding": "uint", "scale": 1}],
        "write": [{"key": "offset_mv", "reg": 0x23, "bytes": 2,
                   "bits": bits, "encoding": "int", "lsb_mv": 5,
                   "raw_min": -100, "raw_max": 100}],
        "verify": {"rungs_mv": [10], "min_loaded_vout_mv": 800},
    }
    rail = railctl.Rail(railctl.Profile(data), SimpleNamespace(ok=True))
    state = {(0, 1): 0, (0x29, 2): 0x420, (0x23, 2): original, (0x8B, 2): 1000}
    rail.read = Mock(side_effect=lambda reg, width: state.get((reg, width)))

    def write(reg, value, width):
        state[(reg, width)] = value
        return True

    rail._raw_write = Mock(side_effect=write)
    return rail, state


class RegisterPreservationTests(unittest.TestCase):
    def test_positive_and_negative_offsets_preserve_factory_bits(self):
        for offset, expected in ((50, 0xA00A), (-50, 0xA0F6)):
            rail, state = make_rail()
            self.assertTrue(rail.set_offset_mv(offset, acknowledged=True)[0])
            self.assertEqual(state[(0x23, 2)], expected)
            rail._raw_write.assert_called_once_with(0x23, expected, 2)

    def test_shifted_field_preserves_bits_on_both_sides(self):
        rail, state = make_rail(original=0xA05B, bits="11:4")
        self.assertTrue(rail.set_offset_mv(-50, acknowledged=True)[0])
        self.assertEqual(state[(0x23, 2)], 0xAF6B)

    def test_dry_run_never_changes_word(self):
        rail, state = make_rail()
        self.assertTrue(rail.plan(50)[0])
        self.assertEqual(state[(0x23, 2)], 0xA005)
        rail._raw_write.assert_not_called()

    def test_mismatch_outside_offset_field_restores_exact_original(self):
        rail, state = make_rail()

        def lose_factory_bit(reg, value, width):
            state[(reg, width)] = value ^ 0x1000 if rail._raw_write.call_count == 1 else value
            return True

        rail._raw_write.side_effect = lose_factory_bit
        ok, message = rail.set_offset_mv(50, acknowledged=True)
        self.assertFalse(ok)
        self.assertIn("restored original", message)
        self.assertEqual(state[(0x23, 2)], 0xA005)
        self.assertEqual([c.args[1] for c in rail._raw_write.call_args_list], [0xA00A, 0xA005])

    def test_rejected_or_exceptional_dispatch_rolls_back(self):
        for raises in (False, True):
            rail, state = make_rail()

            def uncertain_dispatch(reg, value, width):
                state[(reg, width)] = value
                if rail._raw_write.call_count == 1:
                    if raises:
                        raise OSError("transport interrupted after dispatch")
                    return False
                return True

            rail._raw_write.side_effect = uncertain_dispatch
            ok, _ = rail.set_offset_mv(50, acknowledged=True)
            self.assertFalse(ok)
            self.assertEqual(state[(0x23, 2)], 0xA005)
            self.assertEqual(rail._raw_write.call_count, 2)

    def test_exception_after_successful_write_rolls_back(self):
        rail, state = make_rail()
        ordinary = rail.read.side_effect
        raised = False

        def failing_read(reg, width):
            nonlocal raised
            if reg == 0x23 and rail._raw_write.call_count == 1 and not raised:
                raised = True
                raise OSError("lost readback")
            return ordinary(reg, width)

        rail.read.side_effect = failing_read
        self.assertFalse(rail.set_offset_mv(50, acknowledged=True)[0])
        self.assertEqual(state[(0x23, 2)], 0xA005)

    def test_rejected_unchanged_write_needs_no_restore_dispatch(self):
        rail, state = make_rail()
        rail._raw_write.side_effect = None
        rail._raw_write.return_value = False
        ok, message = rail.set_offset_mv(50, acknowledged=True)
        self.assertFalse(ok)
        self.assertIn("already reads back exactly", message)
        self.assertNotIn("RESTORE FAILED", message)
        self.assertEqual(state[(0x23, 2)], 0xA005)
        rail._raw_write.assert_called_once_with(0x23, 0xA00A, 2)

    def test_changed_page_or_scale_prevents_write(self):
        for key, value in (((0, 1), 1), ((0x29, 2), 0x820)):
            rail, state = make_rail()
            state[key] = value
            self.assertFalse(rail.set_offset_mv(50, acknowledged=True)[0])
            rail._raw_write.assert_not_called()

    def test_changed_word_before_dispatch_is_not_overwritten(self):
        rail, state = make_rail()
        ordinary = rail.read.side_effect
        word_reads = 0

        def concurrent_change(reg, width):
            nonlocal word_reads
            if reg == 0x23:
                word_reads += 1
                if word_reads == 2:
                    state[(reg, width)] = 0xB006
            return ordinary(reg, width)

        rail.read.side_effect = concurrent_change
        ok, message = rail.set_offset_mv(50, acknowledged=True)
        self.assertFalse(ok)
        self.assertIn("word changed", message)
        self.assertEqual(state[(0x23, 2)], 0xB006)
        rail._raw_write.assert_not_called()

    def test_changed_page_after_dispatch_prevents_wrong_page_restore(self):
        rail, state = make_rail()

        def page_changes(reg, value, width):
            state[(reg, width)] = value
            state[(0, 1)] = 1
            return True

        rail._raw_write.side_effect = page_changes
        ok, message = rail.set_offset_mv(50, acknowledged=True)
        self.assertFalse(ok)
        self.assertIn("RESTORE FAILED", message)
        self.assertEqual(rail._raw_write.call_count, 1)

    def test_restore_does_not_depend_on_voltage_envelope_or_vout(self):
        rail, state = make_rail()
        rail.p.env_max = -1
        rail.p.ceiling = 1
        state[(0x8B, 2)] = None
        self.assertTrue(rail._restore_word(0xA005)[0])
        self.assertEqual(state[(0x23, 2)], 0xA005)

    def test_writable_runtime_checks_discard_changed_telemetry(self):
        for method, expected in (("read_vout", None), ("telemetry", {})):
            rail, state = make_rail()
            ordinary = rail.read.side_effect

            def changed_scale(reg, width):
                value = ordinary(reg, width)
                if reg == 0x8B:
                    state[(0x29, 2)] = 0x820
                return value

            rail.read.side_effect = changed_scale
            self.assertEqual(getattr(rail, method)(), expected)

    def test_legacy_profile_retains_encoding_and_polling_behavior(self):
        path = Path(__file__).resolve().parents[1] / "i2c/rtx2080ti-mp2888a.toml"
        with path.open("rb") as stream:
            profile = railctl.Profile(tomllib.load(stream))
        self.assertFalse(profile.runtime_checks)
        rail, state = make_rail(guarded=False, original=0)
        rail.p = profile
        rail.present = Mock(return_value=True)
        self.assertTrue(rail.set_offset_mv(-12.5, acknowledged=True)[0])
        self.assertEqual(state[(0x23, 2)], 0xFE)
        self.assertEqual(rail.present.call_count, 1)


class VerificationRestoreTests(unittest.TestCase):
    def test_success_requires_exact_full_entry_word_restore(self):
        rail, state = make_rail()
        rail._sample = Mock(side_effect=[(0, 0), (10, 0)])
        with patch.object(railctl.time, "sleep"):
            ok, message, _ = rail.verify(acknowledged=True, ref=lambda: 1000)
        self.assertTrue(ok, message)
        self.assertEqual(state[(0x23, 2)], 0xA005)
        self.assertEqual([c.args[1] for c in rail._raw_write.call_args_list], [0xA007, 0xA005])

    def test_failed_restore_cannot_return_success(self):
        for mismatch in (False, True):
            rail, state = make_rail()
            rail._sample = Mock(side_effect=[(0, 0), (10, 0)])

            def reject_restore(reg, value, width):
                if rail._raw_write.call_count == 2:
                    if mismatch:
                        state[(reg, width)] = value ^ 0x1000
                        return True
                    return False
                state[(reg, width)] = value
                return True

            rail._raw_write.side_effect = reject_restore
            with patch.object(railctl.time, "sleep"):
                ok, message, _ = rail.verify(acknowledged=True, ref=lambda: 1000)
            self.assertFalse(ok)
            self.assertIn("RESTORE FAILED", message)
            self.assertIn("cannot unlock", message)

    def test_sample_exception_restores_entry_word_and_reports_failure(self):
        rail, state = make_rail()
        rail._sample = Mock(side_effect=[(0, 0), OSError("telemetry disappeared")])
        with patch.object(railctl.time, "sleep"):
            ok, message, _ = rail.verify(acknowledged=True, ref=lambda: 1000)
        self.assertFalse(ok)
        self.assertIn("telemetry disappeared", message)
        self.assertEqual(state[(0x23, 2)], 0xA005)


if __name__ == "__main__":
    unittest.main()
