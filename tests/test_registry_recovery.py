# SPDX-License-Identifier: GPL-3.0-or-later
"""Crash recovery preserves outside edits and restores only owned names."""

import copy
import itertools
import unittest

from druta.registry_recovery import RecoveryConflict, restore_state


def binary(value):
    return {"type": 3, "value": value}


def dword(value):
    return {"type": 4, "value": value}


class RegistryRecovery(unittest.TestCase):
    def test_every_partial_write_prefix_restores_exact_original(self):
        before = {"EnableGpuFirmware": dword(16), "romOverride00": binary("aabb"), "vbios": binary("00")}
        after = {"EnableGpuFirmware": dword(17), "romOverride00": binary("aabb"), "romOverride01": binary("ccdd")}
        owned = ("EnableGpuFirmware", "romOverride01", "vbios")
        for chosen in itertools.product((False, True), repeat=len(owned)):
            current = copy.deepcopy(before)
            for key, applied in zip(owned, chosen):
                if applied:
                    if key in after:
                        current[key] = copy.deepcopy(after[key])
                    else:
                        current.pop(key, None)
            with self.subTest(chosen=chosen):
                self.assertEqual(restore_state(before, after, current), before)

    def test_preserves_external_additions_and_unowned_changes(self):
        before = {"romOverride00": binary("aabb"), "vbios": binary("00")}
        after = {**before, "romOverride01": binary("ccdd")}
        current = {**after, "vbios": binary("ff"), "romOverride02": binary("eeee")}
        expected = {"romOverride00": binary("aabb"), "vbios": binary("ff"), "romOverride02": binary("eeee")}
        self.assertEqual(restore_state(before, after, current), expected)

    def test_preserves_unowned_external_deletion(self):
        before = {"vbios": binary("00")}
        after = {**before, "EnableGpuFirmware": dword(17)}
        self.assertEqual(restore_state(before, after, {"EnableGpuFirmware": dword(17)}), {})

    def test_conflict_is_only_for_owned_key_and_changes_nothing(self):
        before = {"EnableGpuFirmware": dword(16), "vbios": binary("aa")}
        after = {"EnableGpuFirmware": dword(17), "vbios": binary("aa")}
        current = {"EnableGpuFirmware": dword(99), "vbios": binary("bb")}
        saved = copy.deepcopy((before, after, current))
        with self.assertRaises(RecoveryConflict) as raised:
            restore_state(before, after, current)
        self.assertEqual(raised.exception.conflicts, ("EnableGpuFirmware",))
        self.assertEqual((before, after, current), saved)

    def test_owned_deletion_by_outside_writer_conflicts(self):
        with self.assertRaises(RecoveryConflict):
            restore_state({"EnableGpuFirmware": dword(16)}, {"EnableGpuFirmware": dword(17)}, {})

    def test_external_addition_of_transaction_deleted_key_conflicts(self):
        with self.assertRaises(RecoveryConflict):
            restore_state({"vbios": binary("aa")}, {}, {"vbios": binary("bb")})

    def test_case_insensitive_names_preserve_current_spelling(self):
        before = {"EnableGpuFirmware": dword(16)}
        after = {"EnableGpuFirmware": dword(17), "romOverride00": binary("aa")}
        current = {"ENABLEGPUFIRMWARE": dword(17), "ROMOVERRIDE00": binary("aa"), "RomOverride01": binary("bb")}
        self.assertEqual(
            restore_state(before, after, current), {"ENABLEGPUFIRMWARE": dword(16), "RomOverride01": binary("bb")}
        )

    def test_restores_original_spelling_when_key_is_absent(self):
        self.assertEqual(restore_state({"vbios": binary("aa")}, {}, {}), {"vbios": binary("aa")})

    def test_case_only_name_change_does_not_claim_ownership(self):
        self.assertEqual(
            restore_state({"vbios": binary("aa")}, {"VBIOS": binary("aa")}, {"VBIOS": binary("bb")}),
            {"VBIOS": binary("bb")},
        )

    def test_duplicate_aliases_are_invalid(self):
        for where in range(3):
            values = [{}, {}, {}]
            values[where] = {"vbios": binary("aa"), "VBIOS": binary("bb")}
            with self.subTest(where=where), self.assertRaises(ValueError):
                restore_state(*values)

    def test_result_and_inputs_do_not_share_mutable_values(self):
        before = {"a": {"type": 7, "value": ["one"]}}
        after = {"a": {"type": 7, "value": ["two"]}}
        current = {**after, "b": {"type": 7, "value": ["three"]}}
        result = restore_state(before, after, current)
        result["a"]["value"].append("changed")
        result["b"]["value"].append("changed")
        self.assertEqual(before["a"]["value"], ["one"])
        self.assertEqual(current["b"]["value"], ["three"])


if __name__ == "__main__":
    unittest.main()
