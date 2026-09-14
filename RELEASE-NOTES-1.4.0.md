# Druta 1.4.0

Druta 1.4.0 corrects I2C voltage-response verification, adds Kepler/Maxwell
controls and current-limit support across three driver layouts, and removes
compatibility restrictions derived from individual development boards. It also
includes the package reorganization and tuning/recovery fixes from PR #19.

## I2C verification correction

**Published versions 1.2.0 and 1.3.0 used an invalid generic verification
calculation: controller VOUT minus NVAPI voltage.** Driver voltage is not an
independent measurement of an I2C-induced physical change. A previous pass or
failure from that verifier is not reliable evidence of the write path.

Verification now holds an observed P0 operating point and samples the selected
controller's direct VOUT. It requires a rise above measured noise, exact
restoration of the original register state, a downward response, and return to
the baseline band. The fixed 800 mV entry threshold is removed. Unstable or
unproven responses remain inconclusive; they no longer diagnose an incorrect
board merely because no sufficient movement was observed.

Kepler NCP4206 verification uses direct VMON and the same response/reversal
method. It never used the generic NVAPI subtraction. Temporary P0 requests are
released after restoration; an existing Druta hold is preserved. Cancellation,
partial writes and cleanup failures retain recovery state, revoke stale
verification, and prevent another GPU from receiving unfinished cleanup.

## Controls follow the current adapter

- **Core current limit** is available on Pascal, Turing and Blackwell when the
  generation-understood policy and runtime interface validate. Blackwell also
  supports **Other rail current limit**. The layouts exercised with 472.12,
  580.97 and 610.88 are selected by packet structure, not driver-version lists.
- Policies validate independently. An absent second current rail or unrelated
  extra policy no longer removes a valid control.
  Unavailable expected policies show diagnostics. Normal current envelopes are
  500 A core / 200 A other, intersected with API limits; XOC uses the reported
  maximum. These are request bounds, not physical component ratings.
- Voltage-limit references come from the current adapter's first stable paired
  reads. Displayed absolute limits are labelled estimates from driver data.
  **Initial** restores exact captured deltas, including tuning already present
  at launch; it does not claim factory settings. Profiles preserve raw deltas.
- NVVDD and MSVDD limits work independently. **MSVDD requested offset** is an
  explicit XOC experiment with stored-request readback, profile/undo support,
  and a Zero action. Acceptance does not prove physical voltage movement.
- Known clock controls no longer require optional telemetry pairing. Memory
  offsets use their transport's representable units and report actual readback
  mismatch instead of inheriting one RTX 5080's precision rule.
- **Device > Refresh capabilities** retries discovery without discarding
  settings, owned holds or first-read restoration values.

## Kepler, Maxwell and controller discovery

- **Lock P0 and max fan** is offered to Kepler and Maxwell through generation and
  runtime capability checks. P0 and settled top memory must be observed before
  requesting 100% fan. Fan-only Undo restores the prior fan policy while
  preserving the separately owned hold; **Release P0** releases it.
- Timing reads recognize these holds. Kepler omits incompatible V/F controls;
  Maxwell attempts its runtime V/F interface without a GTX 745 blacklist.
  Legacy clock-domain identities and diagnostics are corrected.
- NCP4206, MP2888A and MP29816 discovery searches ports 0–7 and addresses
  0x08–0x77 using controller identity and understood registers, without GPU-ID
  filters. Ambiguous candidates require selection. Controller identity alone
  does not establish which physical rail is wired to it.
- MP29816 discovery binds the current page and scale without changing PAGE.
  Documented scales provide telemetry; the sourced offset operation is offered
  only in its understood 5 mV mode and still requires session verification.

## Precision, recovery and packaging

Fractional power/voltage requests survive Apply, reapplication and profile
replay. Graphics offsets display their encoded clock bins; Pascal/Turing curve
units no longer depend on the current tune. Dynamic clock-list enumeration
handles larger Blackwell tables. Failed lock readbacks retain ownership and
recovery; queued callbacks cannot tune a newly selected GPU.

Timing writes recheck Unlock and the current performance band immediately
before commit. Helper failures and missing readbacks remain failures. Preview
uses the helper's advertised dry-run convention, and unknown layouts retain
raw captures without authorizing decoded writes.

Application code now lives in `src/druta`, with tests in `tests`. Source and
installed-package entry points, existing profile locations, portable recipes,
and startup launch compatibility are retained.

## Download and validation

Download [Druta-1.4.0-win64.zip](https://github.com/Thermetery-Technology-Co-Limited/Druta/releases/download/1.4.0/Druta-1.4.0-win64.zip),
extract it, and keep the entire `Druta` folder together. Run `Druta.exe`; Python
is not required. Matching source, tests, build files and SHA-256 records are
included under `source`. Save a fresh profile to capture the new raw fields.

The clean 1.4.0 release source passed **898 automated tests**, including
launcher, wheel/source packaging, profile replay, capability and recovery tests.
Historical hardware checks cover GTX 770/690/745, TITAN Xp/RTX and RTX 5080;
their exact scope is recorded with the source. Revised direct verification
passed repeated local TITAN RTX and GTX 770 runs. It remains unconfirmed on
TITAN V and the remote TITAN RTX/HOF PCB. MP29816 telemetry was observed, but
the tested driver rejected offset writes. Discovery is not write validation.

See the [compatibility audit](https://github.com/Thermetery-Technology-Co-Limited/Druta/blob/1.4.0/COMPATIBILITY-GATING-AUDIT.md)
and [I2C validation record](https://github.com/Thermetery-Technology-Co-Limited/Druta/blob/1.4.0/experiments/I2C-VOUT-VALIDATION.md).

**Full changelog:** https://github.com/Thermetery-Technology-Co-Limited/Druta/compare/1.3.0...1.4.0
