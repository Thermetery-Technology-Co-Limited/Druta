# First native-backend live qualification: RTX 3070 Ti

Date: 2026-11-11. This records request/readback/restoration testing on one
adapter, not complete G4 sign-off or a performance/stability qualification.
The default backend remains Python.

## Configuration

- Baseline: `cpp/core`, `015605d948e5caa6589700fe5f3235a8f0f3d395`.
- NVIDIA GeForce RTX 3070 Ti, PCI `0000:01:00.0`, device/subsystem
  `2482/880e1043`, VBIOS `94.04.5a.00.56`, NVIDIA driver `595.97`.
- Windows 10 Enterprise 19045 x64; administrator session.
- CPython 3.14.7, Dear PyGui 2.3.1, MSVC 19.44.35230.0, Release x64.
- Forced native backend, ABI 2, independently paired NVAPI/NVML identities.
- Tested packaged DLL SHA-256:
  `4295cca1b2a13623a64b1298b50b2a603280e96d22954a09f1ef4cc4769bf860`.

These identifiers describe the experiment; none becomes a compatibility gate.
Each write uses a fresh baseline and a single hardware writer. Test values are
small bounded changes, followed by independent readback and restoration through
the existing understood setters. Opaque captured packets are compared, never
replayed. The owner changed the memory request between sessions; the later runs
preserve their freshly captured **32 MHz true / 512 transport-unit** baseline.

## Problems fixed during qualification

MSVC truncated the high telemetry-mask enum values and rejected the build with
warnings treated as errors. The C header now uses unsigned 64-bit constants,
with compile-time checks across bits 31–34. The probe uses the MSVC secure file
opener and direct assignment for its fallback name. A packaging-test fixture
now canonicalizes Windows temporary paths as production does.

The memory slider could request 26.9375 MHz true while this driver retained
26.875, then leave the unapplied number displayed. Dragging now stages whole
displayed MHz; typing retains transport precision. Failed Apply rereads and
displays the actual configured request. If that read fails, the staged number
remains with an explicit message. Strict backend mismatch detection and Undo
remain intact; there is no automatic retry or device-specific step assumption.

Four live Dear PyGui callback cases cover two drags, an exact fractional typed
request, and a driver-quantized typed request. All show the expected UI behavior
and restore their captured original request and 22 nontelemetry state sections.

## Live results

| Area | Exercise and result |
| --- | --- |
| Ordinary controls | Core 0→−15→0 MHz; memory 0→−10→0 MHz true; board power 310→305→310 W; both fans automatic→40%→automatic. All pass. |
| Private clocks | Crossbar, additional memory, SYS, Video and LTC requests each 0→−15→0 MHz. All five pass; modes and other rows remain unchanged. |
| V/F and locks | One GPU curve point decreased by one 15 MHz bin and restored; temporary point lock; enumerated 825 MHz frequency lock. All three pass with released ownership and exact requested-state restoration. |
| Voltage boost/current | Boost 0→1→0%; current policy 13: 243→242→243 A, including effective-current readback. Pass. |
| Power policies | All 15 independently testable entries (1, 3–10, 12, 14–18) each decreased by 1,000 native units and restored, including pin intent. Pass. Policies 0/11 were excluded because their requested and effective values differ; board policy 2 and current policy 13 were tested separately. |
| Rail limits | REL, ALT-reliability, overvoltage and VMIN each tested independently: raw delta 0→−6,250→0 µV. All four request/preservation checks pass. Session references are estimates, not verified factory defaults. |
| NVVDD offset | Stored request 0→+1→0 mV passes. Only the selected request word changes. Reported GPU V/F voltage coordinates shift uniformly by +6.25 mV during the request and restore exactly. Physical voltage response was not independently measured. |
| MSVDD offset | Both native and Python return driver status −1 for +1 mV. The request and entire clock-control packet remain unchanged. **Refused on this configuration; not qualified.** |
| Advanced runtime controls | 13 trials pass: ELPG/ELCG/BLCG/SLCG holds and releases, individual GR/MS gating, V/F monotonicity and smoothing, voltage-request gate, and device 0/1 switching delays 128→129→128 µs, including ELCG/SLCG enable paths. |
| Scoped memory profile | Actual manual save/change/load/Undo/Undo callbacks pass, with isolated profile files and exact final restoration. |
| Full profile and Reset controls | Stage core −15 MHz, memory 31 MHz true, power 305 W and fans 40%. Explicit full-profile load restores baseline; Undo restores the staged requests. Separately stage all four, confirm Reset controls, verify eight named reset results, then restore the original full profile including memory 32 MHz. Both workflows pass. |
| Hold Headroom | One 6.25 mV REL raise while holding a validated 1081.25 mV point: native apply/live-check/release succeed. Independent audit verifies all 29 request/ownership witnesses restored. Strict harness result remains failed because a reported non-GPU V/F voltage coordinate changed; see below. |
| Concurrency | 180 seconds of telemetry polling with repeated memory-profile loads and scoped read-only MP2888 discovery: 326 samples, five repeated profile cycles, no diagnostics or read failures, GPU edge 41–42 °C. |
| UI/read-only comparison | Source and packaged native/Python comparisons complete; identities and configured requests agree. Reviewed live/derived readings and native-only diagnostics differ. Both packaged UI captures complete all 27 stages. |
| Second window | Two native EXE windows coexist, both responsive, each loading the qualified packaged DLL. This checks coexistence, not simultaneous tuning by two writers. |

The advanced controllers retain Python policy/lifecycle code and use the native
backend's paired companion transport. Their results do not claim that those
consumers have been rewritten in C++.

Hold Headroom's non-GPU V/F row 127 reported voltage changed from 650 to
643.75 mV, while its reported 405 MHz value stayed unchanged. Fresh Python and
native reads agree. The cause and physical memory-voltage effect are unproven.
Full Pstates20, the 24,996-byte clock-control packet, policy CONTROL data, all
requested rail fields, GPU deltas, fans, boost and exact prior point target
restore. The conservative harness retained its marker; after independent audit
and fresh pairing/readback, the exact dead-owner marker was archived and cleared
through the production cleanup method. No opaque replay or voltage write was
used to force the reported coordinate back. This qualifies the bounded request
lifecycle, not unchanged derived readings or the usual margin under GPU load.

The owner separately reported successful manual checks of **Max it**, the
**1999 W configured power limit**, and **BIOS override**. These are owner-reported
results, not scripted proof of delivered power or independent electrical limits.

## Evidence and reproducibility

The opt-in `native_*_3070ti.py` harnesses beside this record default to read-only
inspection. Set `DRUTA_NATIVE_LIB` to the reviewed build and pass a fresh output
path and PCI slot. `--write` is explicit. They are supervised experiments, not
startup tasks, benchmark tools, or a portable device qualification promise.

```powershell
python experiments/native_live_smoke_3070ti.py --slot 0000:01:00.0 --output smoke.json
python experiments/native_clock_live_3070ti.py --slot 0000:01:00.0 --output clocks.json
python experiments/native_limits_live_3070ti.py --slot 0000:01:00.0 --output limits.json
python experiments/native_advanced_inventory_3070ti.py --slot 0000:01:00.0 --output inventory.json
python experiments/native_advanced_live_3070ti.py --slot 0000:01:00.0 --output advanced.json
python experiments/native_full_profile_reset_live_3070ti.py --slot 0000:01:00.0 --output profiles.json
python experiments/native_headroom_live_3070ti.py --slot 0000:01:00.0 --output headroom.json
```

Raw local evidence is retained under `validation-20261111/`, with the later
batch in `remaining-controls/` and UI fix in `memory-slider/`. Large snapshots,
private saved profiles, and build products are not committed. The accompanying
evidence manifest records hashes and conclusions for the principal artifacts.

Original failed evidence is retained: an early Windows report-file replacement
collision, an I2C log-callback signature error, and overstrict checks treating
derived V/F coordinates as unchanged requests. Successful reruns or independent
audits are identified separately; failed artifacts are not relabeled.

## Automated/build validation

- Release DLL, probe and selftest build with warnings treated as errors.
- CTest: 1/1 passed.
- Initial focused native/frontend/packaging suite: 214 passed.
- Memory UI/precision/profile/backend regression suites: 115 passed.
- Pre-PR rerun of changed UI/packaging behavior and telemetry masks: 72 passed.
- Final source-distribution checks, including the new record and harnesses: 9 passed.
- Frozen EXE, matching source manifest and ZIP built successfully; the packaged
  DLL is the same binary used by the live harnesses.

## Remaining scope

Sustained GPU workload, stability and performance have not been tested. The
formal restart/relaunch sequence and multi-adapter switching remain separate
qualification items. A successful owner-initiated PnP restart was observed; no
tuning was automatically replayed across it.

I2C coverage is discovery/telemetry for one fingerprinted MP2888 route, not
direct regulator-voltage writes or physical-rail identification. MSVDD remains
driver-refused. DIFR and controls whose current layouts or required BIOS fields
are unavailable remain unqualified; unknown VCRIT/power-arbitration geometry was
not forced. Legacy P0 requires Kepler/Maxwell hardware, absent here. Other GPUs,
drivers, operating systems and 32-bit builds need their own evidence.
