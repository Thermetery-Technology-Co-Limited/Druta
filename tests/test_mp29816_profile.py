# Druta - hardware-free checks for the measured ASUS Astral MP29816 profile.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later

from pathlib import Path
import tomllib
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from druta import railctl


PROFILE = Path(__file__).resolve().parents[1] / "i2c/asus-rtx5080-astral-mp29816.toml"
RESPONSES = {
    (0xAD, 5): int.from_bytes(bytes.fromhex("0416a80200"), "little"),
    (0x99, 4): int.from_bytes(bytes.fromhex("0353504d"), "little"),
    (0x9A, 7): int.from_bytes(bytes.fromhex("063631383a324d"), "little"),
    (0x9B, 3): int.from_bytes(bytes.fromhex("020100"), "little"),
    (0x00, 1): 0,
    (0x29, 2): 0x0420,
    (0x8B, 2): 230,
    (0x22, 2): 0,
}


def profile(*, read_only=False):
    with PROFILE.open("rb") as stream:
        data = tomllib.load(stream)
    if read_only:
        # Retain regression coverage for supported telemetry-only profiles.
        data.pop("write")
    return railctl.Profile(data, str(PROFILE))


def fake_rail(*, read_only=False):
    rail = railctl.Rail(profile(read_only=read_only), SimpleNamespace(ok=True))
    values = dict(RESPONSES)
    rail.read = Mock(side_effect=lambda register, width: values.get((register, width)))
    rail._raw_write = Mock(side_effect=AssertionError("Unexpected write in read-only test"))
    return rail, values


class MP29816ProfileTests(unittest.TestCase):
    def test_real_board_profile_matches_only_tested_pci_ids(self):
        p = profile()
        self.assertTrue(p.candidate_for(0x2C02, 0x89DE1043))
        self.assertFalse(p.candidate_for(0x2C02, 0x12341043))
        self.assertFalse(p.candidate_for(0x1E02, 0x89DE1043))
        self.assertEqual((p.port, p.addr7), (2, 0x30))
        self.assertFalse(p.weak_id)
        self.assertTrue(p.runtime_checks)

    def test_measured_count_prefixed_identity_is_present(self):
        rail, _ = fake_rail()
        self.assertTrue(rail.present())
        for command in ((0xAD, 5), (0x99, 4), (0x9A, 7), (0x9B, 3), (0x00, 1), (0x29, 2)):
            rail.read.assert_any_call(*command)
        rail._raw_write.assert_not_called()

    def test_every_identity_component_is_required(self):
        for key in ((0xAD, 5), (0x99, 4), (0x9A, 7), (0x9B, 3), (0x00, 1), (0x29, 2)):
            for replacement in (None, 0xFFFF):
                with self.subTest(register=key, replacement=replacement):
                    rail, values = fake_rail()
                    values[key] = replacement
                    self.assertFalse(rail.present())
                    self.assertEqual(rail.telemetry(), {})
                    rail._raw_write.assert_not_called()

    def test_manufacturer_match_without_model_is_not_enough(self):
        rail, values = fake_rail()
        values[(0x9A, 7)] ^= 0x100
        self.assertFalse(rail.present())

    def test_shifted_or_wrong_count_manufacturer_response_rejected(self):
        for wrong in (0x4D5053, 0x4D505302):
            rail, values = fake_rail()
            values[(0x99, 4)] = wrong
            self.assertFalse(rail.present())

    def test_scale_selector_requires_5mv_while_ignoring_other_fields(self):
        for selector in range(8):
            rail, values = fake_rail()
            values[(0x29, 2)] = (selector << 10) | 0x03FF
            self.assertEqual(rail.present(), selector == 1)

    def test_measured_voltage_and_reserved_bits_decode(self):
        rail, values = fake_rail()
        for word, expected in ((230, 1150), (160, 800), (0xA0E6, 1150)):
            values[(0x8B, 2)] = word
            telemetry = rail.telemetry()
            self.assertEqual(telemetry["vout_mv"], expected)
            self.assertEqual(rail.read_vout(), expected)
            self.assertEqual(telemetry["offset_raw"], 0)
            self.assertEqual(telemetry["offset_mv"], 0)
            self.assertNotIn("iout_a", telemetry)
            self.assertNotIn("vrm_temp_c", telemetry)
        rail._raw_write.assert_not_called()

    def test_voltage_bus_error_produces_no_numeric_reading(self):
        rail, values = fake_rail()
        values[(0x8B, 2)] = None
        self.assertNotIn("vout_mv", rail.telemetry())
        self.assertIsNone(rail.read_vout())

    def test_changed_or_missing_page_and_scale_suppress_reads(self):
        for key, replacement in (((0x00, 1), 1), ((0x00, 1), None),
                                 ((0x29, 2), 0x0820), ((0x29, 2), None)):
            for method, expected in (("read_vout", None), ("telemetry", {})):
                with self.subTest(register=key, replacement=replacement, method=method):
                    rail, values = fake_rail()
                    values[key] = replacement
                    self.assertEqual(getattr(rail, method)(), expected)
                    self.assertNotIn(unittest.mock.call(0x8B, 2), rail.read.call_args_list)
                    rail._raw_write.assert_not_called()

    def test_page_or_scale_change_during_voltage_read_discards_sample(self):
        for key, replacement in (((0x00, 1), 1), ((0x00, 1), None),
                                 ((0x29, 2), 0x0820), ((0x29, 2), None)):
            for method, expected in (("read_vout", None), ("telemetry", {})):
                with self.subTest(register=key, replacement=replacement, method=method):
                    rail, values = fake_rail()

                    def change_during_read(register, width):
                        result = values.get((register, width))
                        if register == 0x8B:
                            values[key] = replacement
                        return result

                    rail.read.side_effect = change_during_read
                    self.assertEqual(getattr(rail, method)(), expected)
                    rail.read.assert_any_call(0x8B, 2)
                    rail._raw_write.assert_not_called()

    def test_verify_read_only_refuses_before_sampling_or_bus_access(self):
        for acknowledged in (False, True):
            rail, _ = fake_rail(read_only=True)
            reference = Mock(side_effect=AssertionError("Must not sample a reference"))
            result = rail.verify(acknowledged=acknowledged, ref=reference, allow_idle=True)
            self.assertFalse(result[0])
            self.assertEqual(result[2], [])
            self.assertIn("telemetry", result[1])
            reference.assert_not_called()
            rail.read.assert_not_called()
            rail._raw_write.assert_not_called()

    def test_profile_without_write_has_no_write_path_even_in_xoc(self):
        rail, _ = fake_rail(read_only=True)
        self.assertTrue(rail.p.read_only)
        self.assertIsNone(rail.p.write)
        self.assertEqual(rail.p.writable, set())
        self.assertIsNone(rail.p.hw_min_mv)
        self.assertIsNone(rail.p.hw_max_mv)
        for xoc in (False, True):
            rail.xoc = xoc
            self.assertFalse(rail.plan(25)[0])
            self.assertFalse(rail.set_offset_mv(25, acknowledged=True)[0])
            self.assertFalse(rail.reset()[0])
        rail._raw_write.assert_not_called()

    def test_exact_device_id_count_and_payload_are_required(self):
        for wrong in (0x0002A816, 0x0002A81603, 0x0002A81704):
            rail, values = fake_rail()
            values[(0xAD, 5)] = wrong
            self.assertFalse(rail.present())

    def test_offset_profile_is_experimental_and_whitelists_only_22(self):
        p = profile()
        self.assertIn("write path unverified", p.name)
        self.assertFalse(p.read_only)
        self.assertEqual(p.writable, {0x22})
        self.assertEqual((p.wreg, p.wbytes, p.wbits, p.lsb_mv), (0x22, 2, (7, 0), 5))
        self.assertEqual((p.raw_min, p.raw_max), (-128, 127))
        self.assertEqual((p.env_min, p.env_max), (-50, 50))
        self.assertEqual(p.rungs, (5, 10, 15, 20))
        self.assertTrue(p.runtime_checks)

    def test_mocked_offset_writes_preserve_high_byte_and_encode_both_signs(self):
        for offset, expected in ((5, 0xA001), (-5, 0xA0FF), (50, 0xA00A), (-50, 0xA0F6)):
            rail, values = fake_rail()
            values[(0x22, 2)] = 0xA000

            def write(register, word, width):
                values[(register, width)] = word
                return True

            rail._raw_write.side_effect = write
            ok, message = rail.set_offset_mv(offset, acknowledged=True)
            self.assertTrue(ok, message)
            self.assertEqual(values[(0x22, 2)], expected)
            rail._raw_write.assert_called_once_with(0x22, expected, 2)

    def test_normal_envelope_and_signed_encoding_bounds_reject_without_writes(self):
        for xoc, offset in ((False, -55), (False, 55), (True, -645), (True, 640)):
            rail, _ = fake_rail()
            rail.xoc = xoc
            self.assertFalse(rail.set_offset_mv(offset, acknowledged=True)[0])
            rail._raw_write.assert_not_called()

    def test_wrong_page_scale_or_device_id_prevents_offset_dispatch(self):
        for key, value in (((0, 1), 1), ((0x29, 2), 0x820), ((0xAD, 5), None)):
            rail, values = fake_rail()
            values[key] = value
            self.assertFalse(rail.set_offset_mv(5, acknowledged=True)[0])
            rail._raw_write.assert_not_called()

    def test_read_only_copy_keeps_offset_unknown(self):
        rail, _ = fake_rail(read_only=True)
        telemetry = rail.telemetry()
        self.assertEqual(telemetry["vout_mv"], 1150)
        self.assertIsNone(telemetry["offset_raw"])
        self.assertIsNone(telemetry["offset_mv"])

    def test_discovery_bypasses_legacy_recipe_pci_metadata(self):
        with patch.object(railctl, "load_profiles", return_value=[profile()]), \
                patch('druta.mp29816.discover', return_value=[]) as scanner:
            api = SimpleNamespace(ok=False)
            self.assertIsNone(railctl.find(api, 0x2C02, 0x12341043))
            scanner.assert_called_once()


if __name__ == "__main__":
    unittest.main()
