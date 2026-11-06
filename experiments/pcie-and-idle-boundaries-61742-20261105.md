# PCIe and second-threshold follow-up on 617.42

This follow-up adds no runtime control. It distinguishes one directly tested
unsupported operation from two unresolved paths. The working manual PCIe speed
control remains separate. The [receipt](pcie-and-idle-boundaries-61742-20261105.json)
contains the original width trial, exact metadata bytes and source-capture hashes.

## Link width: documented request returned unsupported

On the local RTX 5080 / 617.42, root sent one
`NV2080_CTRL_CMD_BUS_SET_PCIE_LINK_WIDTH` (`0x20801804`) request with the
documented eight-byte payload `[8, 0]`. It returned NT status zero, completion
one and RM `0x56` (`NV_ERR_NOT_SUPPORTED`). NVIDIA defines the second word's
failure-reason flags only for generic errors; its value is not assigned a
meaning for this unsupported result.

Two baseline and five subsequent observations retained register `0x85008 =
0x80011060`, both endpoints at Gen 1 x16, and identical complete captured
endpoint capability/control/status words. No restoration SET or restart was
needed. This is an unsupported public request on this configuration, not proof
that every adapter, driver path or alternative interface lacks width control.

The saved host handler `48EDB0` calls `317E70`. A request matching current width
returns without changing it; a different power-of-two width from 1 to 16 calls
bus virtual `+470`. The traced Blackwell selection points to the `92D80`
return-`0x56` stub. Root independently checked the selection, stub bytes and
request/readback receipt. However, the command export has flags `0x44`, including
`ROUTE_TO_PHYSICAL`: the local stub alone was insufficient to rule out a
forwarded implementation. The bounded live trial settled the public result
without establishing which internal route ran.

## LTR: valid encoding, actual route still unknown

The earlier latency request used 10-bit values 64/96 and legal scale 2/2.
The traced local packing would produce `0x14305080` after its trigger cleared;
the saved register remained zero. Ordinary masking of those values does not
explain that mismatch. No new LTR request was issued.

Root checked the pinned GET/SET exports: `0x20801817`, size 12, handler
`48DFA0`; `0x20801818`, size 8, handler `48EE40`. Both have flags `0x48`,
including `NON_PRIVILEGED` and `ROUTE_TO_PHYSICAL`. NVIDIA's public resource
prologue permits conditional forwarding that bypasses the local handler.
Neither that runtime decision nor the local register-helper sanity/filter
decisions were observed. Consequently, a local register-writer trace does not
prove that the earlier trial reached it. No supported read-only discriminator
was identified; additional unchanged GETs would not resolve the cause.

## Second idle threshold: setter is not a functional definition

The host E802 type-3 dispatch maps attribute `0x3B` to `738B88`. It constructs
the pair `[0, caller_value]` and uses the same scaling helper as the shipped
first field. The second scaled/raw readbacks are independently identified.
This establishes a numerical request path, not the current field's purpose.
The downstream meaning of a zero first word is also unproven for this operation.
An older GK20A post-powerup-threshold name is not bound to the current register
or protocol. No second-field write was made and no named control is exposed.

All source work in this follow-up examined host code or public definitions;
no firmware analysis, workload, physical measurement or registry change occurred.
Driver versions identify evidence only and do not become product gates.

Primary definitions: [NVIDIA bus commands](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/615.71.09/src/common/sdk/nvidia/inc/ctrl/ctrl2080/ctrl2080bus.h),
[routing flags](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/ca2d03e6fafd4d4b56b383497d931c25b283bfcf/src/nvidia/inc/kernel/rmapi/control.h),
and [conditional resource forwarding](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/ca2d03e6fafd4d4b56b383497d931c25b283bfcf/src/nvidia/src/kernel/rmapi/resource.c).
