# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
import json
from pathlib import Path
import tempfile
import unittest

from druta import timingprofiles, timings


def table():
    return timings.FieldTable(fields=[
        timings.Field("RC", "CONFIG0", 0, 7, 255, "row cycle", "row cycle"),
        timings.Field("FAW", "CONFIG0", 8, 15, 255, "four activate", "four activate"),
        timings.Field("OFFSET0", "CONFIG1", 0, 3, 15, "phase", "phase [structural]", structural=True),
        timings.Field("RFCSBA", "CONFIG2", 0, 7, 255, "inferred", "inferred", inferred=True),
    ], aliases={"t0": "FAW"})


def capture(*, divergent=False, codename="GP102", band=True):
    ft = table()
    snap = timings.Snapshot(ok=True, codename=codename, slot="0000:01:00.0", pci_id="10de:1b02",
                            boot0="42", chipset="GP102", aperture="broadcast", mem_before=7001,
                            mem_after=7001, mem_states=[405, 7001], pstate_before=0, pstate_after=0,
                            registers={"broadcast": {"CONFIG0": 0x182D, "CONFIG1": 2, "CONFIG2": 9},
                                       "fbpa0": {"CONFIG0": 0x182D, "CONFIG1": 2, "CONFIG2": 9}},
                            scopes=["fbpa0"],
                            mem_offset=0, readings=[timings.Reading(ft.fields[0], 45),
                                                    timings.Reading(ft.fields[1], 24),
                                                    timings.Reading(ft.fields[2], 2),
                                                    timings.Reading(ft.fields[3], 9)])
    if not band:
        snap.pstate_before = snap.pstate_after = 8
    if divergent:
        snap.registers["fbpa0"]["CONFIG0"] = 0x192D
    return snap


class TimingProfileTests(unittest.TestCase):
    def test_legacy_slot_metadata_is_not_silently_discarded(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "legacy.json"
            path.write_text(json.dumps({"fields": {"RC": 46}, "slot": "0000:02:00.0"}))
            self.assertEqual(timingprofiles.read_profile(path, table())["slot"], "0000:02:00.0")

    def test_round_trip_preserves_capture_metadata_and_staged_values(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tuned.json"
            saved = timingprofiles.save(path, capture(), table(), {"RC": 46})
            self.assertEqual(saved["fields"], {"FAW": 24, "RC": 46})
            self.assertEqual(saved["capture"]["source"], "nvtune decoded broadcast capture")
            self.assertEqual(saved["capture"]["pending_modifications"], ["RC"])
            self.assertEqual(timingprofiles.read_profile(path, table()), saved)
            self.assertEqual(timingprofiles.load(path, table()), {"FAW": 24, "RC": 46})
            self.assertFalse(list(Path(folder).glob(".*.tmp")))

    def test_flat_nvtune_fields_loads_and_alias_canonicalizes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "flat.json"
            path.write_text(json.dumps({"fields": {"RC": 46, "t0": 25}}), encoding="utf-8")
            document = timingprofiles.read_profile(path, table())
            self.assertEqual(document["schema"], "nvtune-fields")
            self.assertEqual(document["fields"], {"FAW": 25, "RC": 46})

    def test_upstream_profile_preserves_slot_metadata_and_uppercase_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "upstream.json"
            path.write_text(json.dumps({"_format": "nvtune-profile-1", "name": "safe",
                                        "slot": "0000:01:00.0", "fields": {"RC": 46, "FAW": 25}}),
                            encoding="utf-8")
            document = timingprofiles.read_profile(path, table())
            self.assertEqual(document["slot"], "0000:01:00.0")
            self.assertEqual(document["capture"]["gpu"]["slot"], "0000:01:00.0")
            self.assertEqual(document["fields"], {"FAW": 25, "RC": 46})

    def test_upstream_profile_rejects_scopes_unknown_format_and_lowercase_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad-upstream.json"
            cases = (({"_format": "nvtune-profile-1", "name": "bad", "slot": "0000:01:00.0",
                       "fbpa": {}, "fields": {"RC": 46}}, "fbpa-scoped"),
                     ({"_format": "nvtune-profile-2", "name": "bad", "slot": "0000:01:00.0",
                       "fields": {"RC": 46}}, "unsupported nvtune profile format"),
                     ({"_format": "nvtune-profile-1", "name": "bad", "slot": "0000:01:00.0",
                       "fields": {"rc": 46}}, "must be uppercase"))
            for payload, message in cases:
                with self.subTest(payload=payload):
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaisesRegex(timingprofiles.TimingProfileError, message):
                        timingprofiles.read_profile(path, table())

    def test_raw_snapshot_is_never_treated_as_an_apply_profile(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "capture.json"
            path.write_text(json.dumps({"registers": {"broadcast": {"CONFIG0": "0x1"}}}), encoding="utf-8")
            with self.assertRaisesRegex(timingprofiles.TimingProfileError, "raw nvtune register snapshot"):
                timingprofiles.load(path, table())

    def test_save_rejects_unknown_decoder_idle_or_partition_difference(self):
        for kwargs, text in (({"codename": "UNKNOWN_1B3"}, "raw register snapshots"),
                             ({"band": False}, "top memory band"),
                             ({"divergent": True}, "differs across framebuffer partitions")):
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(timingprofiles.TimingProfileError, text):
                    timingprofiles.make_profile(capture(**kwargs), table())

    def test_save_rejects_incomplete_decoded_capture(self):
        snap = capture()
        snap.readings.pop(1)
        with self.assertRaisesRegex(timingprofiles.TimingProfileError, "missing decoded values"):
            timingprofiles.make_profile(snap, table())
        with self.assertRaisesRegex(timingprofiles.TimingProfileError, "staged fields were not decoded"):
            timingprofiles.make_profile(snap, table(), {"FAW": 25})

    def test_save_checks_partition_registers_not_only_precomputed_divergence(self):
        snap = capture()
        snap.registers["fbpa0"].pop("CONFIG0")
        with self.assertRaisesRegex(timingprofiles.TimingProfileError, "partition 'fbpa0'.*missing CONFIG0"):
            timingprofiles.make_profile(snap, table())

    def test_inconsistent_field_geometry_cannot_be_imported_or_exported(self):
        ft = table()
        ft.fields[0] = timings.Field("RC", "CONFIG0", 0, 7, 127, "row cycle", "row cycle")
        with self.assertRaisesRegex(timingprofiles.TimingProfileError, "inconsistent bit geometry"):
            timingprofiles.make_profile(capture(), ft)
        with self.assertRaisesRegex(timingprofiles.TimingProfileError, "inconsistent bit geometry"):
            timingprofiles.validate_fields({"RC": 45}, ft)

    def test_invalid_assignments_are_rejected_without_coercion_or_filtering(self):
        cases = ({"TYPO": 1}, {"OFFSET0": 1}, {"RFCSBA": 1}, {"RC": 256},
                 {"RC": True}, {"RC": 1.0}, {"RC": 45, "rc": 45}, {"FAW": 24, "t0": 25})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad.json"
            for fields in cases:
                with self.subTest(fields=fields):
                    path.write_text(json.dumps({"fields": fields}), encoding="utf-8")
                    with self.assertRaises(timingprofiles.TimingProfileError):
                        timingprofiles.load(path, table())

    def test_duplicate_json_key_and_unknown_native_version_fail(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad.json"
            path.write_text('{"fields":{"RC":45,"RC":46}}', encoding="utf-8")
            with self.assertRaisesRegex(timingprofiles.TimingProfileError, "duplicate JSON key"):
                timingprofiles.load(path, table())
            path.write_text(json.dumps({"schema": timingprofiles.SCHEMA, "version": 99,
                                        "capture": {}, "fields": {"RC": 45}}), encoding="utf-8")
            with self.assertRaisesRegex(timingprofiles.TimingProfileError, "unsupported timing profile version"):
                timingprofiles.load(path, table())


if __name__ == "__main__":
    unittest.main()
