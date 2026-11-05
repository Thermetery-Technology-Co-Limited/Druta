# Intermediate voltage-policy delay: current-machine result

The examined RTX 5080 / 617.42 configuration does not expose either of the
voltage-policy subtypes whose host handlers implement an intermediate-switch
delay. This is a closed negative result for this interface and captured
configuration, not an implementation-ready control or a universal restriction.

The intermediate policy delay is separate from Druta's working PWM
voltage-device switch-delay control. The NVIDIA-authored policy header names a
16-bit microsecond `interSwitchDelayus` field for single-rail and split-rail
multi-step policies. The current host constructor/getter/setter traces bind
these to policy types 4 and 2, respectively. This is a request parameter, not
a physical settling-time measurement.

## What the current adapter reported

Root issued one named `0x2080B209` INFO GET with the 524-byte parameter size
read independently from this host driver's export descriptor. Transport,
completion and RM status were 0, 1 and 0. The guarded request kept its paired
GPU identity and command/envelope geometry.

The returned policy mask was `0x00000001`: only policy 0 was present. Its
16-byte INFO row was `05050000030000000000000000000000`. Source tracing
establishes a 12-byte INFO header, 16-byte row stride, 32 slots, a mask at
offset 4 and duplicated subtype bytes at row offsets 0 and 1. Both subtype
bytes were 5. There was no type 2 or type 4 entry in this capture.

The current host type-5 constructor retains the base CONTROL setter, which
validates pointers and type without copying either delay field. It does not
install the delay-specific overrides used by types 2 and 4. We therefore did
not issue CONTROL GET `0x2080B211` or SET `0x2080F212`, and did not add a slider.
This does not prove that every routed implementation behaves identically or
that no other interface could ever offer such a control.

## Source and request evidence

Host image SHA-256:
`db407b964c11441642a1642a0922c35990de974c267a08537c3432765365f84b`.
Addresses below identify evidence in that image; they are not compatibility
gates in product code.

- Export records: B209 at `E6A190`, handler `4E9A50`, 524 bytes; B211 at
  `E6A1D0`, handler `4E9950`, 520 bytes; F212 at `E6AE30`, handler `4E9CD0`,
  520 bytes. GET flags are `0x48`; SET flags are `0x44`.
- Group construction `873C50` binds INFO mapper `873670`, which computes
  `payload + 12 + 16 * ordinal` for an ordinal below 32. Common INFO
  `7C0200` supplies the mask destination at payload offset 4.
- Factory `8741A0`, jump table `8742E0`: type 5 selects constructor
  `BBD600`. Its complete body through `BBD72D` leaves CONTROL slots
  `+28/+30` inherited from `8737C0` and `685CF0`. The retained setter is
  `685F00`; the INFO getter chain `BBD380 -> 873A40 -> 7C05C0` supplies
  the duplicated type bytes.
- Type 4 binds CONTROL getter/setter `BBC870/BBCA90`, moving the delay
  between row `+6` and object `+78`. Type 2 binds `BBCC60/BBCE10`, moving
  it between row `+12` and object `+80` alongside the inherited split fields.

Root reproduced the important type-5 and INFO layout traces independently of
the delegated source review. The [audit receipt](policy-intermediate-delay-5080-61742-20261105.json)
records the INFO result, hashes and source boundary. All compared native
clock-control bytes, power requests, voltage boost and V/F lock readback
matched before and after the query. No SET, registry change, restart, workload
or firmware analysis was performed in this investigation.

Raw readback, the reviewed INFO-only harness and root source receipts are in
`registry-overrides/policy-delay-root-20261105/`; the independent host source
trace is in `registry-overrides/policy-delay-host-terra-20261105/`. Other
adapters and configurations remain untested. Their availability must be
determined from their own inventory and understood handlers.
