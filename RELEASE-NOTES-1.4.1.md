# Druta 1.4.1

Druta 1.4.1 fixes the memory-timing editor's incompatibility with the public
nvtune releases. It includes [PR #24](https://github.com/Thermetery-Technology-Co-Limited/Druta/pull/24)
and retains the controls and compatibility fixes in [1.4.0](RELEASE-NOTES-1.4.0.md).

## Fixed

Druta 1.4.0 required nvtune to advertise a native preview mode. The public
v1.0.0-alpha and v1.0.1-alpha releases instead write immediately on `set` and
reject `--commit`; v1.0.1-alpha also rejects `--force`. Earlier testing used
an intermediate development build whose command interface differed from
the published releases.

- Editing a timing now produces a local, read-only preview for those public
  releases using their field definitions and the selected GPU's raw register
  dump. It sends no `set` command during preview and preserves unrelated bits.
- Apply sends one actual write using the selected helper's supported flags.
  Helpers with advertised native previews continue to use them.
- Restore uses the matching command convention. The helper executable stays
  pinned throughout an operation, and Apply rechecks Unlock and the current
  memory performance band immediately before writing.
- Failed commands and partly refused batches keep their actual readback and
  remain failures. A partial write is not reported as an untouched GPU.
- Preview failures are shown as errors. Local previews explain that checks
  internal to nvtune can still refuse Apply.

## Live validation

Tests ran on a TITAN Xp (GP102) on 2026-09-14, including a driver round trip
from **472.12 to 580.97 and back to 472.12**. Both switches completed without
requiring a reboot.

| Helper | 472.12 before | 580.97 | 472.12 after |
| --- | --- | --- | --- |
| Public nvtune v1.0.0-alpha | Passed | Passed | Passed |
| Public nvtune v1.0.1-alpha | Passed | Passed | Passed |
| Intermediate native-preview helper | Passed | Passed | Passed |

All nine combinations exercised the actual timing-field, Apply and Restore
UI callbacks at held P0. FAW was loosened from 24 to 25, checked in the
broadcast aperture and all six framebuffer partitions, read back four more
times, and restored exactly. Public-helper previews made no writes. Apply
changed only the requested bits. Each test released its temporary V/F hold
and preserved the original fan-control policy.

The full Windows suite passed **920 tests on each driver**. Read-only UI
construction and capability refresh passed without writes or getter errors;
slider availability/ranges, clock controls, current-policy bounds and rail
capabilities matched across both drivers. The packaged PR build opened and
remained responsive on both. The unmodified 1.4.0 code was also confirmed to
reject both actual public helpers.

These measurements cover this TITAN Xp, these helper/driver combinations and
the FAW change tested. They do not establish compatibility for every board,
timing field or other tuning operation. See the
[recorded results](experiments/nvtune-titan-xp-driver-roundtrip-20260914.json).

## Download

Extract `Druta-1.4.1-win64.zip` and run `Druta/Druta.exe`. The bundle includes
matching source, build instructions and a SHA-256 source manifest.
`SHA256SUMS.txt` provides the archive and EXE hashes.

nvtune remains a separate program. Register its executable with
**Device > Locate nvtune…**. Its driver setup and signing requirements are
unchanged by this release.
