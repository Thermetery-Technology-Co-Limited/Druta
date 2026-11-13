# Twenty-first parity increment: installed-offset inspection

Both skins now offer **Read installed offsets** in the voltage table section.
The production factory validates an existing Python scaffold receipt against
fresh current-adapter evidence and displays recognized signed head requests.
This is read-only inspection. Selected offset Apply, Undo and profiles remain
open, including their independent policy-effect and retained-request checks.

The reader uses Python's exact slot-hashed filename under the per-user Druta
state directory. Its separate 8 MiB limit accommodates the original image and
full equation packets without increasing the ordinary 1 MiB profile limit.
It rejects duplicate keys, excessive nesting, malformed integers/hex, wrong
packet geometry and mismatched target/head bindings. Historical saved values
remain bookkeeping; current displayed values come from loaded equations.

Windows target resolution joins the paired API identity to the selected PCI
instance and current display-driver registry mapping, using read-only APIs.
Missing receipts, transient I/O failures and malformed evidence stay distinct.
The exact UTF-8 receipt digest detects replacement, including changes outside
the parsed head metadata. Directory, Windows mapping and receipt are checked
again before publishing the result.

The pure recognizer reconstructs canonical unused equation slots, the present
variable/constant template, original roots and installed heads. It matches the
owned VFE/rail ROM spans, current ownership/clock roots and every pre-existing
INFO/CONTROL byte after normalizing only added mask bits and new head rows.
The exact mask ends at 0x404; the remaining four header bytes are independent
unknown fields retained in each packet. Signed head values must be exactly
representable whole float32 microvolts.

Loaded recognition preserves the Python distinction from activation checking:
other ROM regions may differ after current ownership is validated. A separate
strict pure check compares the complete planned image. Neither function writes
an image, registry record or GPU request. Source-local ROM type 2/3 root type
correspondence does not qualify their complete runtime graph semantics for the
ordinary voltage provider.

The variable getter validates guarded transport geometry and target binding,
requires two equal reads and retries only transient failures once. Raw equation
capture reuses the existing bounded service GET path with semantic callbacks
disabled. The provider adds no outer retry loop. Construction and ordinary
telemetry perform no receipt/ROM/variable reads. Failed offset inspection keeps
last-good values stale and leaves independent ordinary voltage requests intact.

## Validation

- Integrated Release build passed. All **127 CTest suites passed in 45.96 s**.
- New suites cover the pure recognizer, bounded receipt and Windows mapping,
  guarded variable capture, real provider factory and service integration.
  They use alternate synthetic identities/layouts, signed values, unknown-byte
  drift, stale/forged receipts, changed roots/inventories/variables, malformed
  envelopes, transient failures and changed exact receipt bytes.
- Raw capture tests prove semantic providers are not called and no SET occurs.
  Service tests prove recognized heads cannot enter the existing request writer
  or profile capture, and offset failures preserve ordinary request capability.
- The [live read-only probe](2026-11-13-parity-twenty-first-voltage-read.json)
  reports no installed receipt on `0000:01:00.0`, UUID
  `GPU-8f27b43c-149d-36d3-73cd-b448b4fdbab1`. It retains all six NVVDD request
  fields, 203 raw equation definitions and two independent 128 us device delays,
  with a clean close and no requested hardware writes. The previously recorded
  unsupported ROM selector layout remains explicit. This run does not physically
  qualify an installed scaffold or signed offset changes on this board.

- All **22 live renderer phases pass** in the
  [renderer report](2026-11-13-parity-twenty-first-smoke.json). Explicit Pause and
  default minimization produce zero frames and telemetry reads; Always refresh
  while minimized produces 113 frames and two reads. The voltage request page
  was visually inspected. The four resets are Direct3D renderer resets, not
  GPU-driver restarts.
- GUI and CUDA-worker import-policy checks pass within their documented scope;
  they do not establish Vista/7 execution.

SHA-256 of the tested Release artifacts:

| Artifact | SHA-256 |
| --- | --- |
| `druta-ui.exe` | `3AE41328F29E9918FF18C9E437361567E6D285CE8ECEDDF565629BE40A479554` |
| `drutacore.dll` | `9C1033839A2F89A2A39BEA90CE6CFB8BC08F09A6310D6C45D6E7DEDF5CCD8A05` |
| `druta-ui-voltage-read-check.exe` | `89683C41B38C8CEBD26B512C68F2E5D65DF733A16BAB0DABA58E87C519CBC42A` |

The [backlog](../../../backlog.md) has **18 open / 26 completed** entries.
Independent policy/retained clock/rail evidence and selected signed offset
Apply/readback/Undo/profiles remain the next implementation increment. Persistent
installation/activation is not supplied by this work. No controller, clock,
timing, voltage or registry write, workload launch or PnP restart was requested
during this checkpoint's live validation.
