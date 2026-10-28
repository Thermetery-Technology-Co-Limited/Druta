# Turing live client-power qualification: Gigabyte RTX 2080 SUPER

On 2026-10-28 the local Gigabyte AORUS Xtreme RTX 2080 SUPER (TU104,
PCI device `10DE:1E81`, subsystem `1458:3FF4`, address `0000:0a:00.0`)
ran driver 581.42 and VBIOS `90.04.7a.40.3d`. Its reported ordinary power
range was 105/250/350 W minimum/rated/maximum. The captured initial request
was **350 W**, not the 250 W rated value. Every test restored that initial
request and the complete captured client and native definitions/controls.
No driver update was needed for the tested live operations.

The owner reports an imperfect 16 GB VRAM modification whose P5/P8 BIOS voltage
and timing settings are unstable. An existing domain-6 V/F point hold at
1062.5 mV kept the board in P0. Qualification requires that hold and P0;
the full hold readback remained unchanged. No driver reset or low-power
transition was requested. This condition is specific to the test board.

## Read-only mapping evidence

Two identical reads of each accepted INFO response established the local
geometry. Larger candidates returned RM `0x1F` (INVALID_ARGUMENT): client
INFO 2340 bytes, role INFO 264 bytes, and native INFO 20512, 20000 and 8632
bytes. Accepted packets were:

| RM command | Meaning | Parameter bytes | Row mapping |
| --- | --- | ---: | --- |
| `2080A630` | Client INFO | 1160 | `8 + 36*i`, 32-bit mask at +4 |
| `2080A632` / `2080E633` | Client CONTROL GET / SET | 392 | `8 + 12*i`, request at row+4, client at row+8 |
| `2080A61C` | Role INFO | 232 | Role selector 1; 228-byte definition at +4 |
| `2080A618` | Native INFO | 8620 | `88 + 228*i`, 32-bit mask at +4 |
| `2080A61A` | Native CONTROL GET | 4432 | `20 + 124*i`, request at row+4, mask at +16 |
| `2080A619` | Native STATUS GET | 172080 | `112 + 5204*i`, effective value at row+4 |

The unmodified power getter supplied a paired A612 transport with 11748-byte
packet / 11680-byte parameter geometry. Role 1 uniquely bound client row 0 to
native policy 2. The client row reported type `0x57`, role 1 and milliwatts;
the role/native definition reported subclass `0x30`, native type 0 and channel
9, with identical bounds. These row indices and identities are observations,
not support allowlists. The live SET changes the existing FE (`0xFE`) request.
Unknown fields, opaque tails and unrelated control bytes remain preserved.

This is the RM packet mapping used for live power control. It does not
establish a persistent Windows registry/VBIOS override mapping. The Ampere
implementation's 6356-byte native CONTROL and 184-byte rows cannot be reused
for this interface. Druta now tries the understood smaller native INFO only
after explicit size rejection; transient errors do not select another ABI.

## Reversible idle trials

The [sanitized report](turing-tu104-live-power-20261028.json) contains buffer
hashes, all occupied policy requests, sample telemetry and restoration results.
Raw buffers and durable undo records remain outside the repository/package.

| Board request | Settled native board status | Derived core current request/effective | Full restoration |
| ---: | ---: | ---: | --- |
| 349 W | 359.906 W | 359.640 A | Verified |
| 351 W | 361.969 W | 360.360 A | Verified |
| 400 W | 412.500 W | 378.000 A | Verified |
| 351 W, production backend | 361.969 W | 360.360 A | Verified |

NVML's configured getter retained each requested board value. The native board
status above is a distinct software field; it is not measured physical input
power. The current policy's advertised maximum was 360 A. Its derived value
above that maximum remains visible only with matching paired native INFO and
CONTROL snapshots and equal requested/effective current. Independent current
requests remain bounded by the adapter's advertised range.

The fourth trial used `GPU.set_power_limit_mw`, applied a 350 A current pin,
repeated the 351 W board request, and verified that the pin was re-applied
after the driver recalculated coupled policies. An independent 360.001 A
request was refused without changing the captured state. The complete baseline
was restored after each trial. An independent hidden 30-second watchdog with
durable undo state was armed for every trial; parent restoration succeeded,
so the timed watchdog did not need to intervene in these four trials.

The desktop remained at P0 with low GPU utilization, about 44 W reported power
and 43 C edge temperature. No workload, voltage change, registry write, driver
restart or firmware flash was performed. These tests establish acceptance,
readback, current-control retention, pin replay and restoration on this one
configuration. They do not establish physical enforcement under load, all
Turing board behavior, four-digit requests, or support on every driver.
The inherited 1999 W software XOC envelope is not a newly measured Turing
physical ceiling. Detailed arbitration and original-BIOS coloring remain
unavailable with the smaller layout.

## Reproduction

Install the pinned requirements, then use read-only mode first:

```powershell
python experiments/validate_turing_live_power.py --slot 0000:0a:00.0 --output local-read.json
```

For the same 105/250/350 W configuration, the experiment's explicit
`--execute --private-dir C:\path\outside-repository` option runs these idle
trials with a restoration watchdog. Its test points and idle bounds are local
qualification conditions, not product compatibility gates. The script refuses
to use those fixed points against a different ordinary maximum.
