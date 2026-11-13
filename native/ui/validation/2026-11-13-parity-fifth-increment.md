# Advanced feedback, voltage policies and arbitration

Continuation after `1d1ada8`, against the release `fa6ee83` reference and the
[full completion ledger](PARITY-COMPLETION.md). Two Sol workers implemented the
feedback and voltage-policy services/views; Terra implemented arbitration and
backend/view integration tests. Root traced the protocols, integrated the
services and reviewed the failure behavior. This is another checkpoint, not
completion of the full review goal.

## Implemented

- Both skins expose independent Advanced panels for power feedback ratios,
  voltage admission, V/F monotonicity/smoothing, and read-only power arbitration.
  Drawing and ordinary polling do not trigger their reads. The existing worker
  executes explicit actions and publishes immutable view models.
- Feedback reads the understood policy INFO/CONTROL geometry, qualifies each
  row's units/mode/sample counts/bounds, and changes only the selected lower or
  upper U4.12 ratio. The live mask is registered before the existing C ABI SET.
  Fresh target checks, stale-draft comparison and exact packet readback preserve
  unrelated fields. Undo means the preceding edit; captured-initial Restore
  means the field before this session's first edit. Uncertain writes retain
  selected candidates, including chained failed restoration; foreign scalar
  replacements and changed/absent policy bindings remain visible and refused.
- Voltage admission uses A030 GET / A031 SET and its atomic previous Boolean.
  V/F policy edits use the understood empty-domain-mask 901B GET / D01C SET,
  retaining current unknown header bytes and untouched domain data. Reads are
  independently qualified and bounded; unavailable admission does not hide V/F
  flags. Dirty drafts keep their original comparison value across refreshes.
- Review corrected a failure behavior inherited from Python: an issued gate SET
  with no trustworthy atomic previous value cannot claim to restore the earlier
  pre-read. Automatic compensation and Undo then remain unavailable. An explicit
  user request of the captured initial value starts a new qualified transaction;
  it does not reconstruct the unknown prior state. Delayed recovery of a failed
  new edit preserves Undo for the previous successful edit.
- Arbitration acquires the validated table and packet copies under the GPU lock,
  verifies the target, then calls the existing pure decoder. Missing STATUS,
  malformed geometry and transient reads are distinct. Same-target last-good
  reports remain visibly stale. Retry resets only the captured power transport.
  The real decoder's large STATUS geometry is supported without an arbitrary
  small buffer cap. Unknown policy interpretations remain unavailable/raw.
- All new tuning remains session-only. No registry changes, startup application,
  driver-reset replay, or automatic close-time restoration were added. These
  independent policy fields do not acquire or release another control's clocks.

## Validation

Release build and PE/import audit passed. All **54 CTest suites passed**,
including 107 arbitration service checks against both injected responses and the
actual DLL's pure geometry/decoder, 1,301 voltage service checks, feedback
service/view tests, and new backend plus both-skin widget integration suites.
Tests cover alternate identities, partial capabilities, malformed packets,
transient retention/retry, atomic previous-state races, unknown prior state,
failed compensation, stale drafts, foreign scalar replacement, selected-field
preservation, and no implicit diagnostic polling or startup/close writes.

During review, the initial run failed the new unknown-prior recovery regression.
After completing the recovery fix and rebuilding the frozen source, the full
suite passed. No failing test is excluded.

Built-in renderer captures were inspected for Classic Advanced (2100x1400) and
Focused Advanced (1800x1200), locally under
`native/build/ui-integrated/parity-fifth/`. The new section placement and existing
skin styles render correctly. Expanded widgets and real Read-button intents
were exercised by headless ImGui integration tests. Windows desktop capture
failed twice with `window capture timed out: timed out waiting on channel`, so
this checkpoint does not claim live desktop click verification. The fresh EXE
is left visibly running for review.

## Scoped hardware evidence and limits

The read-only `druta-ui-advanced-live-read` utility requires an explicit DLL,
PCI slot and UUID. On this Windows 10 x64 RTX 3070 Ti at `0000:01:00.0` it read
admission, monotonicity and smoothing as available and On. No tuning SET was
issued. Host flag readback is not proof of a physical evaluator effect.

The current adapter exposes policy sizes **8632 / 190272 / 6356 / 6356** bytes,
with INFO base/stride 88/228, STATUS 128/5772 and CONTROL 20/184. That differs
from the traced feedback/arbitration geometry **20512 / 397048 / 13876 / 13876**.
Both native panels refuse this unsupported interpretation. The Python decoder
also refuses this exact layout. This is device-specific evidence, not a device
or driver compatibility gate. Extending that semantic decoder requires protocol
evidence and remains parked; existing ordinary board/current controls remain
independent. No new feedback, voltage-policy or V/F-policy physical write has
been qualified on this card in this checkpoint.

On/Off readback cannot distinguish another program's identical request or a
value changed away and back between reads. The UI describes this limit; it does
not promise exclusive ownership from Boolean observations.

Validated files:

- EXE SHA-256: `E83BC348FCBC86CC32571AEECF896A2A818BF8F16FC77FF9F6FF070E00F86C33`
- DLL SHA-256: `0CB6005074D3266198B5A41BEE3D28900F0EF4953ACFF984BB9E32F8C7B0043D`
- Local build/test/read evidence: `parity-fifth-build-final.log`,
  `parity-fifth-tests-final.log`, `parity-fifth-live-read.json` under
  `native/build/ui-integrated/`.
