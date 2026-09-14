# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise the public nvtune releases at the subprocess boundary, without hardware.

Help text is copied from the named upstream tags (tool/src/cli.cpp).
Field rows reproduce cmd_fields formatting of upstream tool/src/regs.cpp.
Those upstream portions are Copyright (C) 2026 Sebastian Marrufo and licensed
GPL-3.0-or-later. Dump register values below are synthetic; no GPU was accessed.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from druta import timings, timingwrite as tw

SLOT = "0000:02:00.0"

HELP_100 = """nvtune - NVIDIA FBPA memory timing tool

usage: nvtune <command> [options]

commands:
  list                      enumerate GPUs, identify the chip, show topology
  fields                    every tunable parameter, register, bit range, limits
  dump                      decode current timings
  get FIELD...              read specific fields
  set FIELD=VALUE...        write fields
  save                      snapshot all timing registers to JSON
  restore                   write a snapshot back
  apply PROFILE.json        apply a JSON profile
  daemon [FIELD=VALUE...]   hold values against driver reprogramming
  probe                     dump or watch raw FBPA words
  clocks                    read + decode clock domains
  peek OFFSET...            read raw dword(s) at FBPA-relative offset
  poke OFFSET VALUE         write a raw dword (needs --yes)
  vbios                     parse the VBIOS Memory Tweak Table

common options:
  -d, --device SLOT     PCI slot, e.g. 0000:08:00.0. Repeatable.
                        Default: all NVIDIA GPUs.
      --fbpa N          target one partition instead of the broadcast aperture
      --all-fbpa        target every active partition individually
      --force           write even if range checks complain
  -o, --output PATH     save destination
  -i, --input PATH      restore source

dump options:      --raw  --optional
daemon options:    --profile PATH  --interval SECONDS  -v/--verbose
probe options:     --start OFF  --length BYTES  --watch SECONDS
vbios options:     --rom FILE  --prom  --full  --columns A,B,C

Writing to memory-controller registers can hang the machine and corrupt VRAM.
A first write snapshots stock values; use 'restore' to roll back.
"""

HELP_101 = """nvtune - NVIDIA FBPA memory timing tool

usage: nvtune <command> [options]

commands:
  list                      enumerate GPUs, identify the chip, show topology
  fields                    every tunable parameter, register, bit range, limits
  dump                      decode current timings
  get FIELD...              read specific fields
  set FIELD=VALUE...        write fields
  save                      snapshot all timing registers to JSON
  restore                   write a snapshot back
  apply PROFILE.json        apply a JSON profile
  daemon [FIELD=VALUE...]   hold values against driver reprogramming
  probe                     dump or watch raw FBPA words
  peek OFFSET...            read raw dword(s) at FBPA-relative offset
  poke OFFSET VALUE         write a raw dword (needs --yes)
  vbios                     parse the VBIOS Memory Tweak Table

common options:
  -d, --device SLOT     PCI slot, e.g. 0000:08:00.0. Repeatable.
                        Default: all NVIDIA GPUs.
      --fbpa N          target one partition instead of the broadcast aperture
      --all-fbpa        target every active partition individually
  -o, --output PATH     save destination
  -i, --input PATH      restore source

dump options:      --raw  --optional
daemon options:    --profile PATH  --interval SECONDS  -v/--verbose
probe options:     --start OFF  --length BYTES  --watch SECONDS
vbios options:     --rom FILE  --prom  --full  --columns A,B,C

Writing to memory-controller registers can hang the machine and corrupt VRAM.
A first write snapshots stock values; use 'restore' to roll back.
"""

HELP_PREVIEW = """nvtune - NVIDIA FBPA memory timing tool

usage: nvtune <command> [options]

commands:
  list                      enumerate GPUs, identify the chip, show topology
  fields                    every tunable parameter, register, bit range, limits
  dump                      decode current timings
  get FIELD...              read specific fields
  set FIELD=VALUE...        write fields (add --dry-run to preview)
  save                      snapshot all timing registers to JSON
  restore                   write a snapshot back
  apply PROFILE.json        apply a JSON profile
  daemon [FIELD=VALUE...]   hold values against driver reprogramming
  probe                     dump or watch raw FBPA words
  clocks                    read + decode clock domains
  peek OFFSET...            read raw dword(s) at FBPA-relative offset
  poke OFFSET VALUE         write a raw dword (needs --yes)
  vbios                     parse the VBIOS Memory Tweak Table

common options:
  -d, --device SLOT     PCI slot, e.g. 0000:08:00.0. Repeatable.
                        Default: all NVIDIA GPUs.
      --fbpa N          target one partition instead of the broadcast aperture
      --all-fbpa        target every active partition individually
      --force           write even if range checks complain
      --dry-run         preview set/apply using read-only access; no writes
      --commit          explicitly commit set/apply/restore (the default)
  -o, --output PATH     save destination
  -i, --input PATH      restore source

dump options:      --raw  --optional
daemon options:    --profile PATH  --interval SECONDS  -v/--verbose
probe options:     --start OFF  --length BYTES  --watch SECONDS
vbios options:     --rom FILE  --prom  --full  --columns A,B,C

--dry-run and --commit cannot be combined. Preview wrappers must always pass
--dry-run explicitly; older versions reject this flag before opening a GPU.
Writing to memory-controller registers can hang the machine and corrupt VRAM.
A first write snapshots stock values; use 'restore' to roll back.
"""

FIELDS = """FIELD         REGISTER  BITS         MAX  DESCRIPTION
----------------------------------------------------------------------------------------------------
RC            CONFIG0   [7:0]        255  Row cycle time: ACT to ACT, same bank
RFC           CONFIG0   [16:8]       511  Refresh cycle time
RAS           CONFIG0   [23:17]      127  Row active time: ACT to PRE
RP            CONFIG0   [30:24]      127  Row precharge time: PRE to ACT
CL            CONFIG1   [6:0]        127  CAS read latency
WL            CONFIG1   [13:7]       127  CAS write latency
RD_RCD        CONFIG1   [19:14]       63  ACT to READ delay
WR_RCD        CONFIG1   [25:20]       63  ACT to WRITE delay
RPRE          CONFIG2   [3:0]         15  Read preamble length
WPRE          CONFIG2   [7:4]         15  Write preamble length
CDLR          CONFIG2   [14:8]       127  CAS to CAS delay, last read (read data turnaround)
WR            CONFIG2   [22:16]      127  Write recovery time: last write data to PRE
W2R_BUS       CONFIG2   [27:24]       15  Write to read bus turnaround
R2W_BUS       CONFIG2   [31:28]       15  Read to write bus turnaround
PDEX          CONFIG3   [4:0]         31  Power-down exit latency
PDEN2PDEX     CONFIG3   [8:5]         15  Power-down entry to power-down exit
FAW           CONFIG3   [16:9]       255  Four-activate window
AOND          CONFIG3   [23:17]      127  ACT to ACT, other bank / ODT delay
CCDL          CONFIG3   [27:24]       15  CAS to CAS delay, long (same bank group)
CCDS          CONFIG3   [31:28]       15  CAS to CAS delay, short (diff bank group)
REFRESH_LO    CONFIG4   [2:0]          7  Refresh interval, low bits  [structural]
REFRESH       CONFIG4   [14:3]      4095  Refresh interval (tREFI)
RRD           CONFIG4   [20:15]       63  Row to row activate delay
DELAY0        CONFIG4   [26:21]       63  Delay0, low bits (see CONFIG5.DELAY0_MSB/_HI)  [structural]
ADR_MIN       CONFIG5   [2:0]          7  Minimum address bus hold  [structural]
WRCRC         CONFIG5   [10:4]       127  Write CRC latency adder
OFFSET0       CONFIG5   [17:12]       63  Training / phase offset 0  [structural]
DELAY0_MSB    CONFIG5   [19:18]        3  Delay0, mid bits  [structural]
OFFSET1       CONFIG5   [23:20]       15  Training / phase offset 1  [structural]
OFFSET2       CONFIG5   [27:24]       15  Training / phase offset 2  [structural]
DELAY0_HI     CONFIG5   [31:28]       15  Delay0, high bits  [structural]
RFCSBA        TIMING22  [9:0]       1023  Same-bank refresh, all-bank (tRFCsb)
RFCSBR        TIMING22  [17:10]      255  Same-bank refresh, per-bank
              TIMING22  INFERRED: Offset inferred from TIMINGn = 0x290 + n*4. Verify before writing.
"""

DUMP = f"""{SLOT}  GP102 (Pascal, GeForce 10)
  [broadcast] base 0x10F000
    CONFIG0 @0x10F290 = 0x1234ED2D
    CONFIG1 @0x10F294 = 0x12345678
    CONFIG2 @0x10F298 = 0x11223344
    CONFIG3 @0x10F29C = 0x2200304A
    CONFIG4 @0x10F2A0 = 0x10203040
    CONFIG5 @0x10F2A4 = 0x99887766
"""
PREVIEW = f"""{SLOT}  GP102 (Pascal)
  [broadcast]
  CONFIG0 @0x10F290 0x1234ED2D -> 0x1234ED2E [would write]
      RC              45 -> 46
  CONFIG3 @0x10F29C 0x2200304A -> 0x2200324A [would write]
      FAW             24 -> 25
dry run complete: no registers written
"""


class NvtuneCompatibilityTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.exe = Path(folder.name) / "nvtune.exe"
        self.exe.write_bytes(b"fake nvtune release; never executable")
        self.help = HELP_101
        self.fields = FIELDS
        self.dump = DUMP
        self.values = {"RC": 45, "FAW": 24}
        self.commands = []
        self.events = []
        self.failures = {}
        self.before_reply = None
        self.commit_reply = None
        for patcher in (
            patch.object(timings, "find_exe", side_effect=lambda *_a, **_kw: str(self.exe)),
            patch.object(tw.subprocess, "run", side_effect=self.execute),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def execute(self, argv, **kwargs):
        self.assertEqual(argv[0], str(self.exe))
        self.commands.append(list(argv))
        command = argv[1]
        self.events.append(command)
        if self.before_reply is not None:
            self.before_reply(command)
        if command in self.failures:
            result = self.failures[command]
            if isinstance(result, BaseException):
                raise result
            return SimpleNamespace(returncode=result[1], stdout=result[0], stderr="")
        if command == "--help":
            out = self.help
        elif command == "fields":
            out = self.fields
        else:
            self.assertEqual(argv[2:4], ["-d", SLOT])
            if command == "dump":
                self.assertIn("--raw", argv)
                out = self.dump
            elif command == "get":
                out = SLOT + "  " + "  ".join(
                    f"{name}={self.values[name]}" for name in argv[4:] if name in self.values)
            elif command == "set":
                if self.help in (HELP_100, HELP_101):
                    self.assertNotIn("--dry-run", argv)
                    self.assertNotIn("--commit", argv)
                if self.help == HELP_101:
                    self.assertNotIn("--force", argv)
                if "--dry-run" in argv:
                    out = PREVIEW
                else:
                    if self.commit_reply is not None:
                        out, rc = self.commit_reply(argv)
                        return SimpleNamespace(returncode=rc, stdout=out, stderr="")
                    for item in argv[4:]:
                        if "=" in item:
                            name, value = item.split("=", 1)
                            self.values[name] = int(value)
                    out = PREVIEW.replace("[would write]", "[write]").replace(
                        "dry run complete: no registers written", "applied and verified")
            elif command == "restore":
                out = SLOT + " restored from saved.json (verified)"
            else:
                self.fail(f"unexpected external command: {argv}")
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    def select_help(self, help_text):
        # Changing the simulated executable changes its fingerprint, just as
        # replacing an installed release would. This also isolates cache tests.
        self.help = help_text
        self.exe.write_bytes(help_text.encode("utf-8"))
        self.commands.clear()
        self.events.clear()

    def assert_no_write(self):
        self.assertTrue(all(command[1] not in {"set", "apply", "restore", "daemon", "poke"}
                            for command in self.commands), self.commands)

    def test_both_public_releases_preview_without_any_set_command(self):
        for help_text in (HELP_100, HELP_101):
            with self.subTest(release="1.0.0" if help_text == HELP_100 else "1.0.1"):
                self.select_help(help_text)
                plan = tw.plan({"RC": 46, "FAW": 25}, SLOT)
                self.assertTrue(plan.ok, plan.error)
                self.assertEqual(set(plan.touches), {"RC", "FAW"})
                self.assertFalse(plan.needs_force)
                registers = {op["reg"]: op for op in plan.ops}
                self.assertEqual(int(registers["CONFIG0"]["old"], 16), 0x1234ED2D)
                self.assertEqual(int(registers["CONFIG0"]["new"], 16), 0x1234ED2E)
                self.assertEqual(int(registers["CONFIG0"]["offset"], 16), 0x10F290)
                self.assertEqual(int(registers["CONFIG3"]["new"], 16), 0x2200324A)
                self.assert_no_write()

    def test_native_preview_helper_keeps_explicit_preview(self):
        self.select_help(HELP_PREVIEW)
        plan = tw.plan({"RC": 46, "FAW": 25}, SLOT)
        self.assertTrue(plan.ok, plan.error)
        self.assertEqual(self.commands[-1], [str(self.exe), "set", "-d", SLOT,
                                           "RC=46", "FAW=25", "--dry-run"])
        self.assertNotIn("dump", self.events)

    def test_native_preview_does_not_imply_a_known_commit_contract(self):
        self.select_help("  --dry-run  preview without writes\n")
        plan, results = tw.apply({"RC": 46}, SLOT)
        self.assertTrue(plan.ok, plan.error)
        self.assertEqual(results[0].outcome, tw.FAILED)
        self.assertIn("commit contract", results[0].detail)
        self.assertTrue(all("--dry-run" in argv for argv in self.commands if argv[1] == "set"))

    def test_noop_local_preview_reports_no_changed_registers(self):
        plan = tw.plan({"RC": 45, "FAW": 24}, SLOT)
        self.assertTrue(plan.ok, plan.error)
        self.assertEqual(plan.ops, [])
        self.assertEqual(plan.touches, [])
        self.assertFalse(plan.needs_force)
        self.assert_no_write()

    def test_apply_translates_commit_and_force_for_each_supported_contract(self):
        for help_text, flags in ((HELP_100, ["--force"]), (HELP_101, []),
                                 (HELP_PREVIEW, ["--commit", "--force"])):
            with self.subTest(flags=flags):
                self.select_help(help_text)
                self.values = {"RC": 45, "FAW": 24}
                plan, results = tw.apply({"RC": 46, "FAW": 25}, SLOT, force=True)
                self.assertTrue(plan.ok, plan.error)
                self.assertEqual([row.outcome for row in results], [tw.LANDED, tw.LANDED])
                writes = [argv for argv in self.commands
                          if argv[1] == "set" and "--dry-run" not in argv]
                self.assertEqual(len(writes), 1)
                self.assertEqual(writes[0][:6], [str(self.exe), "set", "-d", SLOT,
                                                "RC=46", "FAW=25"])
                self.assertCountEqual(writes[0][6:], flags)
                self.assertEqual(self.events.count("--help"), 1)

    def test_restore_translates_commit_for_public_and_preview_releases(self):
        backup = self.exe.parent / "saved.json"
        backup.write_text("{}", encoding="utf-8")
        for help_text, flags in ((HELP_100, []), (HELP_101, []),
                                 (HELP_PREVIEW, ["--commit"])):
            with self.subTest(flags=flags):
                self.select_help(help_text)
                ok, reason = tw.restore(str(backup), SLOT)
                self.assertTrue(ok, reason)
                self.assertEqual(self.commands[-1], [str(self.exe), "restore", "-d", SLOT,
                                                    "-i", str(backup), *flags])

    def test_top_band_guard_is_last_operation_before_direct_write(self):
        def guard():
            self.events.append("guard")
            return True, "still in top band"
        plan, results = tw.apply({"RC": 46}, SLOT, before_commit=guard)
        self.assertTrue(plan.ok, plan.error)
        self.assertEqual(results[0].outcome, tw.LANDED)
        self.assertEqual(self.events[self.events.index("guard") + 1], "set")

    def test_failed_guard_stops_direct_write_after_successful_preview(self):
        guard = Mock(return_value=(False, "memory left top band"))
        plan, results = tw.apply({"RC": 46}, SLOT, before_commit=guard)
        self.assertTrue(plan.ok, plan.error)
        guard.assert_called_once_with()
        self.assertEqual(results[0].outcome, tw.TOOL_REFUSED)
        self.assertIn("memory left", results[0].detail)
        self.assert_no_write()

    def test_no_slot_never_uses_the_helpers_all_gpu_default(self):
        plan = tw.plan({"RC": 46}, None)
        self.assertFalse(plan.ok)
        self.assertFalse(any(argv[1] in {"dump", "get"} for argv in self.commands))
        self.assert_no_write()

    def test_dump_must_identify_one_known_target_and_broadcast_scope(self):
        for dump in (
            DUMP.replace(SLOT, "0000:09:00.0"),
            "\n".join(DUMP.splitlines()[1:]),
            DUMP + DUMP.replace(SLOT, "0000:09:00.0"),
            DUMP.replace("GP102", "UNKNOWN_1B3"),
            DUMP.replace("[broadcast]", "[FBPA0]"),
            DUMP.replace("    CONFIG0 @0x10F290 = 0x1234ED2D\n", ""),
            DUMP.replace("CONFIG0 @0x10F290 = 0x1234ED2D", "CONFIG0 @oops = broken"),
            DUMP.replace("0x1234ED2D", "0x1234ED2D  (INFERRED)"),
        ):
            with self.subTest(dump=dump):
                self.dump = dump
                plan = tw.plan({"RC": 46}, SLOT)
                self.assertFalse(plan.ok, plan.raw)
                self.assert_no_write()

    def test_local_preview_rejects_unknown_structural_inferred_and_out_of_range_fields(self):
        for assignments in ({"TYPO": 1}, {"OFFSET0": 1}, {"RFCSBA": 1},
                            {"RC": -1}, {"RC": 256}, {"RC": 46.5}):
            with self.subTest(assignments=assignments):
                plan = tw.plan(assignments, SLOT)
                self.assertFalse(plan.ok, plan.raw)
                self.assert_no_write()

    def test_local_preview_refuses_bad_or_ambiguous_field_geometry(self):
        rc_row = next(row for row in FIELDS.splitlines() if row.startswith("RC "))
        for fields in (
            FIELDS.replace("[7:0]", "[0:7]", 1),
            FIELDS.replace("[7:0]", "[40:0]", 1),
            FIELDS.replace("[7:0]", "[8:0]", 1),
            FIELDS + rc_row + "\n",
            FIELDS.replace(rc_row, "RC CONFIG0 unparseable row"),
        ):
            with self.subTest(fields=fields):
                self.fields = fields
                plan = tw.plan({"RC": 46}, SLOT)
                self.assertFalse(plan.ok, plan.raw)
                self.assert_no_write()

    def test_each_local_preview_reads_fresh_field_metadata(self):
        self.assertTrue(tw.plan({"RC": 46}, SLOT).ok)
        self.fields = FIELDS.replace("[7:0]", "[8:0]", 1)
        plan = tw.plan({"RC": 46}, SLOT)
        self.assertFalse(plan.ok)
        self.assertEqual(self.events.count("fields"), 2)
        self.assert_no_write()

    def test_local_preview_refuses_overlapping_requested_fields(self):
        # Both fields are individually well-formed; together this table would
        # put the two independently selected values into overlapping bits.
        self.fields = FIELDS.replace("[16:8]", "[8:0]", 1)
        plan = tw.plan({"RC": 46, "RFC": 238}, SLOT)
        self.assertFalse(plan.ok)
        self.assertIn("overlapping", plan.error)
        self.assert_no_write()

    def test_changed_configuration_cannot_switch_helper_mid_apply(self):
        other = self.exe.parent / "different-nvtune.exe"
        other.write_bytes(b"wrong executable must never be launched")
        def resolve(override=None):
            if override:
                return str(override)
            return str(other if "--help" in self.events else self.exe)
        with patch.object(timings, "find_exe", side_effect=resolve):
            plan, results = tw.apply({"RC": 46}, SLOT)
        self.assertTrue(plan.ok, plan.error)
        self.assertEqual(results[0].outcome, tw.LANDED)
        self.assertTrue(all(argv[0] == str(self.exe) for argv in self.commands))

    def test_read_failures_cannot_authorize_direct_apply(self):
        for command, reply in (("--help", (HELP_101, 1)),
                               ("fields", (FIELDS, 1)), ("dump", (DUMP, 1)),
                               ("dump", subprocess.TimeoutExpired("nvtune", 20))):
            with self.subTest(command=command, reply=reply):
                self.select_help(self.help + "\n")
                self.failures = {command: reply}
                plan, results = tw.apply({"RC": 46}, SLOT)
                self.assertFalse(plan.ok)
                self.assertEqual(results[0].outcome, tw.FAILED)
                self.assert_no_write()

    def test_unknown_help_cannot_fall_back_to_immediate_write(self):
        self.select_help("some other tool; set modifies registers")
        plan, results = tw.apply({"RC": 46}, SLOT)
        self.assertFalse(plan.ok)
        self.assertEqual(results[0].outcome, tw.FAILED)
        self.assert_no_write()

    def test_helper_replacement_invalidates_public_release_capabilities(self):
        self.select_help(HELP_101)
        self.assertTrue(tw.plan({"RC": 46}, SLOT).ok)
        old = self.exe.stat()
        self.exe.write_bytes(b"X" * old.st_size)
        os.utime(self.exe, ns=(old.st_atime_ns, old.st_mtime_ns))
        self.help = HELP_PREVIEW
        plan = tw.plan({"RC": 46}, SLOT)
        self.assertTrue(plan.ok, plan.error)
        self.assertEqual(self.events.count("--help"), 2)
        self.assertIn("--dry-run", self.commands[-1])

    def test_helper_replacement_during_help_or_after_preview_stops_write(self):
        for replacement_event in ("--help", "dump"):
            with self.subTest(event=replacement_event):
                self.select_help(HELP_101)
                def replace(command):
                    if command == replacement_event:
                        self.exe.write_bytes(b"unexpected replacement helper")
                self.before_reply = replace
                plan, results = tw.apply({"RC": 46}, SLOT)
                self.assertEqual(results[0].outcome, tw.FAILED)
                self.assertIn("changed", results[0].detail)
                self.assert_no_write()
                self.before_reply = None

    def test_old_release_refusal_preserves_observed_partial_write(self):
        self.select_help(HELP_100)
        def commit(_argv):
            self.values["RC"] = 46
            return ("CONFIG0 @0x10F290 0x1234ED2D -> 0x1234ED2E [write]\n"
                    "  RC 45 -> 46\n  applied and verified\n"
                    "refusing to write with warnings outstanding", 1)
        self.commit_reply = commit
        plan, results = tw.apply({"RC": 46, "FAW": 25}, SLOT)
        self.assertTrue(plan.ok, plan.error)
        self.assertEqual([row.outcome for row in results], [tw.FAILED, tw.FAILED])
        self.assertEqual([row.after for row in results], [46, 24])
        self.assertEqual(self.events[-1], "get")


if __name__ == "__main__":
    unittest.main()
