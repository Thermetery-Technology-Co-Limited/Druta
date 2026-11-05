# Integral sample-count failure: timeout and driver recovery

The RTX 5080 / 617.42 past-sample request 43 to 44 did not produce a clean
unsupported response. It returned RM `0x65`; subsequent cleanup GETs returned
`0x23`. NVIDIA's pinned [status definitions](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/e6a2e01579fbbd1ca2ed43fef2ee2589e9bd8bd9/src/common/sdk/nvidia/inc/nvstatuscodes.h)
name those values `NV_ERR_TIMEOUT` and `NV_ERR_INVALID_CLIENT` respectively.
The saved System events also record a TDR recovery sequence. No readback
established that count 44 was retained, and the future-count test was not run.

## Recorded sequence

All times below are UTC on November 5, 2026. The failed SET and cleanup calls
have no individual timestamps, so their exact placement within the event
sequence is unknown.

| Recorded time | Observation |
|---|---|
| 09:56:01.345956 | Lower-bound trial restored the complete captured request state exactly; the count trial followed in execution order. |
| 09:56:01.5388375 | Event 153: UCodeReset TDR. |
| 09:56:03.8752490 | Event 153: error on GPUID 100. |
| 09:56:04.3608019 | Event 14: recovery action changed from None to PF FLR. |
| 09:56:08.5227906–09:56:16.8282098 | GpuRcReset, Resetting, Reset and Restarting TDR events. |
| 09:58:09.038267–09:58:10.019885 | Later root-initiated GPU PnP reset record bracket; success, no Windows reboot required. |
| 09:59:08.258108 | Post-reset capture matched the original complete native/client request state. |

Before the manual reset, fresh INFO and CONTROL reads had already recovered
and showed the original counts and bounds. This does not prove that the
failed request was never consumed. The events and failure are associated in
the same captured run; they do not identify the internal fault or prove the
precise causal sequence. No opaque event payload was assigned speculative
meaning. The [machine-readable receipt](power-integral-count-failure-20261105.json)
retains all eight exact event messages and source hashes.

## What the host trace establishes

Root independently reproduced the relevant instructions in the saved 617.42
host binary. The row setter copies the active ten-byte block containing the
counts and ratio bounds together. Its direct helpers check pointers and
compare type metadata; neither they nor this setter body establish a
count-specific admission range or reinitialization operation.

The public NVOC export layout binds command `0x2080E61B`, size 13,876 bytes,
to local handler RVA `0x4E1480`, which calls group routine `0x4DE5A0`. That
routine dispatches selected rows at `packet + 0x14 + index * 0xC4` through
interface slot `+0x30`. Constructor `0xC56BB0` installs the examined setter
`0xC57310` in that slot. On this conditional local path, a later call's error
can be returned after successful row callbacks. This explains why a local
copy alone would not prove completion; it does not locate the observed timeout.

The export also carries `ROUTE_TO_PHYSICAL`. NVIDIA's public resource-control
code permits conditional forwarding that bypasses the local handler. The
actual route, selected runtime interface and downstream handling of this
captured request remain unproved. The receipt pins the public definitions,
metadata bytes, binary hash and independently reproduced disassembly hashes.

## Product decision

Sample counts remain read-only. Neither byte-sized encoding nor the absence
of a check in one host function justifies another live count write. The next
required evidence is the active receiver's count-update contract, including
whether changing history length needs reinitialization. A smaller nearby
value is not established as a valid alternative.

The qualified lower/upper controls and their manual profiles/Undo are
unchanged. This followup performed no GPU call, registry change, restart or
workload; it audited the earlier experiment and static source files only.
