# Native driver and GPU issues

Opened November 26, 2026, Asia/Shanghai. This ledger tracks failures and
qualification gaps from the [native driver and GPU regression matrix](native-driver-gpu-regression-20261125.md).
The tested hardware is the GTX 1050 Ti and RTX 3070 Ti. Published Druta
1.10.0 results use the shipped core under the matrix harness.

## Latest matrix rerun

The `rerun-20261126-r1` run completed all 14 supported GPU and driver
combinations. The audit verified 56 tuning round trips, 14 BIOS Apply and
Restore cycles, two extended ClientPower round trips, and 42 gradeable
throughput samples. Each tuning pass required Apply readback and restoration
of the original signed requests. Firmware passes required verification of
the complete edited and original ROMs. The [per-row audit](rerun-20261126-r1-verified-rows.json)
records the outcome, BIOS receipt filename and benchmark count for each pairing.

| GPU | Driver | Max It | V/F | Board power | NVVDD REL | BIOS Apply and Restore |
| --- | --- | --- | --- | --- | --- | --- |
| RTX 3070 Ti | 581.80 | Pass | Pass | Pass | Pass | Pass, `bios-ga104-58180-652d771706.json` |
| GTX 1050 Ti | 581.80 | Pass | Pass | Pass | Pass | Pass, `bios-gp107-58180-55ec992850.json` |
| RTX 3070 Ti | 472.12 | Pass | Pass | Pass | Pass | Pass, `bios-ga104-47212-7e077cf958.json` |
| GTX 1050 Ti | 472.12 | Pass | Pass | Pass | Pass | Pass, `bios-gp107-47212-b616bab94e.json` |
| RTX 3070 Ti | 512.15 | Pass | Pass | Pass | Pass | Pass, `bios-ga104-51215-3491851dfe.json` |
| GTX 1050 Ti | 512.15 | Pass | Pass | Pass | Pass | Pass, `bios-gp107-51215-e82b9b1b79.json` |
| RTX 3070 Ti | 560.94 | Pass | Pass | Pass | Pass | Pass, `bios-ga104-56094-a9954131c3.json` |
| GTX 1050 Ti | 560.94 | Pass | Pass | Pass | Pass | Pass, `bios-gp107-56094-e0f260bbf6.json` |
| RTX 3070 Ti | 595.71 | Pass | Pass | Pass | Pass | Pass, `bios-ga104-59571-7128ac9f62.json` |
| RTX 3070 Ti | 595.97 | Pass | Pass | Pass | Pass | Pass, `bios-ga104-59597-c13727bec3.json` |
| GTX 1050 Ti | 375.63 | Pass | Pass | Pass | Pass | Pass, `bios-gp107-37563-5b4fc8fe92.json` |
| GTX 1050 Ti | 431.36 | Pass | Pass | Pass | Pass | Pass on retest, `bios-gp107-43136-e541ba623b.json` |
| GTX 1050 Ti | 582.28 | Pass | Pass | Pass | Pass | Pass, `bios-gp107-58228-b57003bc88.json` |
| GTX 1050 Ti | 582.78 | Pass | Pass | Pass | Pass | Pass, `bios-gp107-58278-cccd4f23e4.json` |

The first phase completed 10 of 14 rows and restored both adapters to
shared 581.80 before the monitor was moved to the GTX. During the isolated
transition to 375.63, Windows registered the signed GTX package and stalled
during device restart. NVIDIA's runtime query also stalled, and no control
or firmware trial started on that bind. The operator rebooted. The matching
NVIDIA runtime then reported the exact GTX UUID, 375.63, and its original 90 W
request. Windows used Microsoft Basic Display Driver for the local desktop,
with the RTX disabled and the GTX independently paired by the native probes.
The rerun preflight accepts both PCI domain widths and checks the Windows
primary desktop when the isolated NVIDIA adapter reports no active display.

On 431.36 the first BIOS Apply and Remove could not reopen NVAPI or NVML
after the device refresh. The exact registry binding had zero override values
before recovery. The approved reboot recovered the same 431.36 binding, and
the production Verify helper retired the scratch receipt after the full ROM,
INFO and P0 clock rows matched their original values. See
`bios-gp107-43136-56c812b896.json` and
`gp107-43136-rerun-20261126-r1-recovery-proof.json`.

The Pascal verifier now retries transient read failures during clock-image
verification. Restore defers another refresh if exact proof is still
unavailable when the retry window expires. The native suite passed 170 tests
with this change. The 431.36 retest bound the driver from shared 581.80 with
the GTX driving the desktop and completed Apply and Restore using the rebuilt
verifier. The 582.28 and 582.78 firmware round trips used the same verifier and
restored the full original ROM and P0 clock rows. The original and rebuilt
binary hashes are in `rerun-20261126-r1-binaries.json` and
`rerun-20261126-r1-recovery-binaries.json`.

The 431.36 candidate and shipped 1.10.0 runs under
`rerun-20261126-r1-431-retry` started from matching complete curves and
matched every recorded curve row during Apply and after Restore. Both
held 1898 MHz at 1100 mV and restored 1822 MHz, 90 W, and an unowned
hold. The short Max It throughput samples were 546.67 and 546.50 frames/s
while applied on the candidate and shipped cores respectively.

The 581.80 GTX firmware test loaded an 1810 MHz P0 GPC ceiling and restored
the original complete ROM and P0 GPC rows. The scratch receipt retired and
the original-profile receipt was verified unchanged.

Extended ClientPower passed live again on 560.94. The RTX set 310 → 381 →
310 W and the GTX set 90 → 114 → 90 W. The original client and native
CONTROL buffers and requested peer policy values matched after Restore.
The receipts are `ga104-56094-rerun-20261126-r1-extended.json` and
`gp107-56094-rerun-20261126-r1-extended.json`.

Both adapters ended on signed 581.80 with Windows reporting no device
problems and with the original 310 W RTX and 90 W GTX requests. Independent
reads matched both original ROM hashes and found zero managed registry
override values. The GTX was driving the display and the rebuilt Druta was
open on the local desktop. Final evidence uses `rerun-20261126-r1-final-*`.

## Closed or comparison-only findings

| Finding | Evidence |
| --- | --- |
| Elevated BIOS helper on 472.12 | Resolved on the test host. Acronis Active Protection events 6 and 7 identified the Druta executables and the exact blocked NVIDIA display driver registry keys. The first helper had an active Administrators token and Windows ACL access, and its denied attempt left the original image intact. Druta preserves Windows error 5 and now points elevated users to the relevant policy logs. After the administrator uninstalled Acronis, the production helper applied relationship 1 at raw 4095, refreshed the driver, and verified the edited ROM and INFO. Remove restored raw 4096, the original full ROM and INFO hashes and zero override values. Both helper runs exited 0. |
| GTX 1050 Ti Max It clock regression on 431.36 | Fixed. The Max It planner keeps the released fallback when NVML does not measure the grid. Two live comparisons matched the complete curve and 1911 MHz hold, and both Undos verified restoration. |
| RTX 3070 Ti NVVDD control on 512.15 | Fixed for the qualified singleton V1 layout. Two GETs matched the V1 NVVDD record against the completed 972-byte transport. The driver’s direct setter applied an exact −6.25 mV reliability request and Undo restored it. Effective and live NVVDD moved 1081.25 → 1075 → 1081.25 mV. Boost stayed at 0% and the other fields matched. Max It also applied and restored its +25 mV headroom. The stock 1.10.0 core still refused. |
| Board power and Max It on 560.94 | Fixed. The driver accepts 7724-byte INFO, 149928-byte STATUS and 4172-byte CONTROL blocks on both cards. The fallback selects the layout from the discovery packet and successful read. Both occupied policy tables matched across the three blocks. The candidate applied and restored 90 → 89 → 90 W on GTX and 310 → 309 → 310 W on RTX. Max It reached 113 W and 380 W respectively and restored the original settings. |
| Extended ClientPower on 560.94 | Fixed and qualified live on both cards. The `0x2080A61C` role request uses 204 bytes after the exact transport and ordinary size refusals identify the candidate. The 1160-byte client INFO, 204-byte role INFO and 7724-byte native INFO uniquely matched the selected board policy. The client and native CONTROL blocks were 392 and 4172 bytes. The RTX read back 310 → 381 → 310 W and the GTX read back 90 → 114 → 90 W. Both restored all requested policy values and pins and matched both complete CONTROL buffers byte for byte. The driver recalculated coupled peer requests while the extended requests were active. |
| 512.15, 560.94 and 581.80 relationship Apply and Restore | Verified live on the RTX 3070 Ti. The existing 5392-byte RM26 write path qualified 512.15. The exact 7724-byte and 8620-byte A618 layouts now qualify with the Ampere V30 ROM and matching selected policy definition. Production Apply changed relationship 1, target policy 1, from raw 4096 to 4095. The active ROM and INFO matched the one-weight projections. Restore returned to raw 4096, both original full-buffer hashes, and zero managed registry values on all three drivers. |
| 595.71 and 595.97 relationship Apply and Restore | Fixed and verified live on the RTX 3070 Ti. The 8632-byte A618 INFO layout qualifies writes when paired with the Ampere V30 ROM table and an exact match for the selected weight and target policy fields. Both signed drivers serialize the relationship mask at `0x1CD8` and use rows at `0x1CDC` with stride `0x24`. The type-zero row stores its target at byte 1 and Q4.12 weight at byte 6. Production Apply changed relationship 1, target policy 1, from raw 4096 to 4095 and verified the complete edited ROM and INFO after refresh on both drivers. Restore recovered raw 4096, both original full-buffer hashes, and zero registry override values. The fixtures verify target metadata, unrelated bytes, and the trailing masks. |
| Intermittent Max It V/F baseline refusal on 581.80 | Max It now recaptures the curve after applying its own controls and replans if evaluated clocks changed. Fixtures cover evaluated-frequency movement and refusal of an unexpected signed request. A live RTX 3070 Ti run completed the curve and hold steps, and Undo verified restoration. The direct curve editor retains its stale-baseline check. |
| Partial disk Undo coverage on direct V/F edits | A standalone curve edit now saves a scoped curve profile. A fixture disabled unrelated power-policy reads, applied the V/F edit and restored the saved signed requests from the profile. |
| GTX 1050 Ti NVVDD writes on 375.63 | The qualified Pascal V1 path applied and restored the selected rail request. The old 1.10.0 core lacked sufficient device and curve identity for the live comparison. |
| GTX 1050 Ti control coverage on 512.15 | The candidate passed all four live controls. The shipped core passed V/F, blocked power and Max It at preflight and rejected rail Apply. |
| Initial runtime enumeration failures | Both 472.12 rows recovered after a clean boot. The GTX 1050 Ti paired on 512.15, 560.94 and 581.80 on later binds. Isolating the GTX recovered 582.28 and 582.78, with all four live controls restored on both builds. |
| Short throughput and evaluated-table differences | The 472.12 Pascal power and 595.97 Ampere Max It differences narrowed on repeat. Some evaluated V/F tables shifted while signed requests restored exactly. Raw intervals and the operating conditions are recorded in the matrix. |
| Test-rig boot configuration | The saved Boot Manager default pointed to a missing loader. The matrix used one-time Windows boot selections for the test restarts. |

## Follow-up evidence

The run directory is `native/build/clock-discovery/driver-gpu-regression-20261125/`.
The earlier live logs use `nvvdd-v1-972-r1`, `compact-power-fix-r1`, and
`fresh-max-baseline-r1`.
The BIOS diagnostic receipt is `bios-ga104-47212-312c690eac.json` and
has matching before and after ROM and INFO hashes, zero managed
registry records and no Apply refresh request.
The Acronis event and rule correlation is `bios-registry-acronis-evidence.md`.
The passing production Apply and Remove receipt is
`bios-ga104-47212-1f0ea23ea7.json`.
The 560.94 live readback receipts are `remaining-client-power-56094-ga104-live.json`
and `remaining-client-power-56094-gp107-live.json`.

| Driver | INFO bytes | Relationship 1 weight offset | Production Apply and Restore receipt |
| --- | ---: | --- | --- |
| 512.15 | 5392 | `0x1068` | `bios-ga104-51215-b84c0e418d.json` |
| 560.94 | 7724 | `0x1986` | `bios-ga104-56094-c0cd5d9764.json` |
| 581.80 | 8620 | `0x1D06` | `bios-ga104-58180-50bca3b1ee.json` |

The active INFO hashes independently matched the one-weight projections in
`bios-older-write-independent-info-projections.json`. All three trials applied
at 0% GPU use, with the RTX reporting P8 at 48°C on 512.15, P0 at 48°C on
560.94, and P0 at 52°C on 581.80.

The 595 live decoder results are `remaining-weights-59571-ga104-rebuilt.txt`
and `remaining-weights-59597-ga104-rebuilt.txt`.
The 595 production Apply and Restore receipts are
`bios-ga104-59571-01b4ac7c21.json` and `bios-ga104-59597-6145efb22b.json`.
Both runs recorded successful Apply and Remove helper exits and identical
active ROM and INFO hashes across the two drivers.
The post-restart uninstall check is `acronis-post-reboot-verification.json`.
Windows removed the pending `fltsrv` service and completed the queued Acronis
deletions. No Acronis product, program directory, storage driver or minifilter
was present after restart.

The rebuilt native suite passed 170 tests, including the 5392-byte RM26 and
7724-, 8620-, and 8632-byte A618 write fixtures.
The focused Python ClientPower,
power layout, and current-limit suites passed 904 tests.
After the relationship write tests, both GPUs were restored to signed 581.80 with
their original 90 W and 310 W requests. Both Windows display devices reported
no problems, and `bios-older-write-restored-58180.txt` confirms the original RTX
ROM and zero managed firmware override values. The rebuilt Druta UI opened
on the local desktop.
