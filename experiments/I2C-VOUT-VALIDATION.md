# Direct I2C VOUT verification — TITAN RTX, 472.12

The earlier verifier subtracted NVAPI voltage from controller VOUT. That is
not a valid physical response measurement unless the reference's behavior is
independently established. Its earlier passing result does not establish
repeatability of the former verifier. Generic offset verification now ignores
the legacy `ref` argument. NVAPI still selects the requested V/F point and
reads state/clocks, but its voltage does not enter the response verdict.

The MP2888A datasheet describes READ_VOUT (0x8B) as sensed output voltage with
1 mV resolution (page 71), and VOUT_OFFSET as a 6.25 mV-step command (page 17).
[MPS datasheet](https://www.monolithicpower.com/en/documentview/productdocument/index/version/2/document_type/Datasheet/lang/en/sku/MP2888A/document_id/4402/).

Initial direct-VOUT tests still failed: the detector required half the commanded
offset as measured gain, and short sampling windows missed baseline variation.
A separate +50 mV trace showed roughly 1063 → 1088 → 1070 mV while the offset
register read 0 → 8 → 0. Matching P0 and clocks does not prove identical load or
temperature, so the shifted final voltage does not by itself identify a cause.

The final detector samples approximately one second of VOUT per measurement,
checks the held operating point during each sample, and requires a response
larger than measured noise and at least one controller offset step. It assumes
no fraction of commanded gain. After exact register restoration, it also
requires a downward response above noise and a return within the baseline
noise/resolution band. Integer telemetry quantization is included in that band.
Unproven responses remain inconclusive, not a diagnosis of an incorrect board.

Three consecutive runs of the production `Druta._i2c_verify_worker` passed on
the local TITAN RTX / 472.12 at P0, core 1965 MHz, memory 7000 MHz. All first
detected movement at +50 mV: 24–25 mV upward and 17–18 mV downward following
restoration. The entry offset word and complete V/F lock table were restored
in every run; no recovery remained pending. See
`i2c-direct-vout-worker-titan-47212-20260909.json` for the complete worker logs.

This validates response detection at that operating point. It does not establish
1:1 voltage gain, constant load, regulator identity, or TITAN V behavior.


## Kepler NCP4206 verification

NCP4206 already read direct VMON; it never used the generic verifier's
NVAPI-voltage subtraction. Its separate verifier still had an 800 mV baseline
floor, fixed response/overshoot allowances, and no voltage-reversal check.
Those checks are now replaced with the direct-response methodology above.

Verification requests legacy P0 on the selected Kepler GPU, requires observed
P0 and settled positive core/memory clocks before a controller write, and
checks the operating point throughout sampling. It does not require CUDA or
an NVAPI voltage reading. This temporary capability path does not change the
persistent P0 UI's measured-board allowlist. An existing session-owned P0 hold
is preserved; a new request is released to automatic states after controller
restoration. The API does not expose another process's force-request ownership.
A failed release remains owned, blocks GPU switching, and is retried through
Release or shutdown.

VMON is read from I2C register 0xD7. Each sample's actual LINEAR11 exponent
supplies its voltage resolution. Complete 25-sample baseline, response, and
restoration windows use measured noise rather than an arbitrary baseline floor
or a one-step maximum noise gate. A pass requires a positive response and a
downward reversal above measured noise and at least one command/telemetry step,
plus a return within the baseline noise/resolution band. Original VOUT_COMMAND
and paired VID modes are restored and independently checked again after the
restoration window. The existing bounded 25/37.5/50 mV absolute-command trials,
normal verification ceiling, encoding limits, and telemetry validity range
remain in force. P0 and stable clocks do not establish constant load.

See the [onsemi NCP4206 datasheet](https://www.onsemi.com/download/data-sheet/pdf/ncp4206-d.pdf),
Table 12: VMON reports the voltage between FB and FBRTN; VID_EN in the paired
VR configuration registers selects the VID source.

Validation for this revision is simulated hardware only. The production UI
worker test uses raw LINEAR11 telemetry at 750 mV, sees an 11.71875 mV rise
for a 25 mV command, and observes the return to 750 mV before releasing P0.
CUDA and NVAPI voltage calls are forbidden in that test. Regression cases
cover noisy baselines, missing telemetry, P-state/clock changes, cancellation,
control readback failures, absent reversal, active original control, and failed
P0 release/recovery. The historical GTX 770 and dual-GPU GTX 690 captures
establish earlier direct-VMON/control observations, not validation of this new
verifier. No Kepler GPU is currently installed; a hardware retest is pending.

## Released generic-verifier provenance

The generic I2C-minus-NVAPI response calculation was introduced by commit
`368fd6378ae5747f17442db59e45dec5d1ba2a76`. Inspection of the published 1.2.0 and
1.3.0 executables confirmed that calculation and the UI voltage-reference
callback were shipped. Release 1.1 did not contain this verifier. NCP4206
support was added after 1.3.0 and was absent from that published executable.
The corrected generic verifier is in commit `9085295` on PR #22; published
1.3.0 is not retroactively fixed by changing this branch.
