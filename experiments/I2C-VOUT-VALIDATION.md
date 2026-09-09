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
