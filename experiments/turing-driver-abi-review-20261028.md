# Offline driver ABI comparison: 431.60, 581.42 and 621.01

On 2026-10-28, an offline examination compared the owner-supplied 431.60
driver directory and 621.01 installer with the installed 581.42 user-mode
NVAPI DLL. **581.42 remains the only driver in this comparison with live
qualification on the local TU104 board.** No inspected DLL was loaded, and
neither supplied driver was installed or activated. This comparison issued no
GPU command, changed no registry value, and performed no driver restart,
load/unload or power-state transition. The board's existing 1062.5 mV V/F hold
was outside this offline work.

The [machine-readable evidence](turing-driver-abi-review-20261028.json) records
the exact DLL file versions, lengths, SHA-256 hashes, instruction bytes and
relative virtual addresses. Binary files and driver installers are not
included in Druta's source or release package.

## Method and provenance

The 431.60 `Display.Driver/nvapi64.dl_` was expanded with Windows `expand.exe`.
For 621.01, the installer contains a 7-Zip payload starting at file offset
`0x111824`. Its start-header and next-header CRCs matched. A copy of that
payload was written to scratch storage and WinRAR extracted only `nvapi64.dll`
and `nvapi64_impl.dll`. The installer itself was never executed. Direct
selective extraction from the executable returned WinRAR exit 6; extracting
the same archive payload as a `.7z` file succeeded with exit 0.

PE parsing and x64 instruction decoding located dispatcher cases that put a
parameter byte count in `[rsp+0x28]` and the RM command in `r9d` before a shared
call. The adjacent instructions were decoded individually; the A612 case uses
the same size store shortly after its command assignment. These are concrete
wrapper command/size pairs, rather than sizes inferred from a driver's version
string or the arithmetic of a presumed row count. Addresses below are RVAs
relative to each PE image base, not addresses in a running process.

| Binary | PE file version | Bytes | Role in this comparison |
| --- | --- | ---: | --- |
| 431.60 `nvapi64.dll` | 26.21.14.3160 | 5,087,208 | Older dispatcher |
| Installed `nvapi64.dll` | 32.0.15.8142 | 5,625,544 | 581.42 dispatcher and live-qualified reference |
| 621.01 `nvapi64.dll` | 32.0.16.2101 | 560,200 | Smaller public DLL; no matching instruction pairs found here |
| 621.01 `nvapi64_impl.dll` | 32.0.16.2101 | 5,688,528 | Dispatcher containing the compared command/size pairs |

## Observed command and parameter geometry

Each entry gives the command followed by its parameter bytes. The labels are
used to compare the corresponding operations; they do not establish that the
older driver accepts Druta's current row definitions or selectors.

| Operation | 431.60 | 581.42 | 621.01 implementation |
| --- | --- | --- | --- |
| Power INFO transport | `20802612`, 2456 | `2080A612`, 11680 | `2080A612`, 54352 |
| Client INFO | `20802630`, 644 | `2080A630`, 1160 | `2080A630`, 2340 |
| Client CONTROL GET | `20802632`, 388 | `2080A632`, 392 | `2080A632`, 808 |
| Client CONTROL SET | `20802633`, 388 | `2080E633`, 392 | `2080E633`, 808 |
| Native INFO | `20802618`, 5008 | `2080A618`, 8620 | `2080A618`, 20512 |
| Native STATUS GET | `20802619`, 14148 | `2080A619`, 172080 | `2080A619`, 397048 |
| Native CONTROL GET | `2080261A`, 2108 | `2080A61A`, 4432 | `2080A61A`, 13876 |
| Native CONTROL SET | `2080261B`, 2108 | `2080E61B`, 4432 | `2080E61B`, 13876 |

For example, 431.60's client INFO case at RVA `0x8FC14` stores `0x284`
(644 bytes), then assigns command `0x20802630` at `0x8FC1C`. The equivalent
581.42 instructions at `0x52D61` / `0x52D69` store `0x488` (1160 bytes) and
assign `0x2080A630`. The 621.01 implementation at `0x553B8` / `0x553C0`
stores `0x924` (2340 bytes) and assigns `0x2080A630`. Full paired instruction
evidence for the table is in the JSON.

No literal `2080261C` or `2080A61C` reference was found in executable sections
of these four files. **This does not establish that the role operation is
absent.** The same literal is absent from 581.42 even though its 232-byte role
GET was validated on the local board. A computed command or another route can
avoid a direct immediate. Consequently this review supplies no role packet
size or runtime-support claim for 431.60 or 621.01.

## Implications for Druta

The 581.42 dispatch geometry agrees with the paired live observations in the
[TU104 qualification](turing-tu104-live-power-20261028.md): smaller client
packets together with 8620-byte native INFO and 4432-byte native CONTROL. The
new native row stride of 124 bytes is established by that live qualification,
not by these static byte counts.

621.01's observed client and native sizes exactly match Druta's existing
modern `client_power_runtime.SIZES` candidates. This is a useful candidate for
future qualification, not evidence that the driver will accept those requests
on this board. Its role binding, row layout, live readback, restoration and
physical enforcement remain untested. This comparison provides no reason to
replace the already qualified 581.42 driver during the current work.

431.60 exposes a different command namespace and smaller structures in the
observed dispatcher. Neither the 581.42 `legacy32/policy124` binding nor the
621.01-shaped modern packets should be relabeled as a 431.60 implementation.
Druta's existing transport/geometry checks must continue refusing an
unrecognized interface before a live client SET. Supporting the observed
431.60 route would require its own paired transport capture, row/role/unit
validation, exact request readback and restoration qualification. No driver
version gate or guessed conversion was added from this review.

These observations apply to the exact binaries identified in the evidence.
They do not prove the absence of other private interfaces, establish a
generation-wide layout, identify a persistent registry/VBIOS override mapping,
or measure physical power/current enforcement.
