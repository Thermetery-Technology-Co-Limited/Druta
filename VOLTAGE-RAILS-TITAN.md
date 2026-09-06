# NVVDD above 1093.75 mV on TITAN RTX and TITAN Xp

Measured on 2026-09-06, Windows driver **580.97**. Both cards report
**1112.5 mV** through both live-voltage getters when the NVVDD reliability
and alternate-reliability ceilings permit it and the V/F curve requests it.
The 1093.75 mV default cap is therefore adjustable through this interface on
these two boards. No I2C writes or changes to the overvoltage ceiling were
needed.

| Card | Board identity | VBIOS | Default cap | Tested raised cap | Live voltage with raised cap |
|---|---|---|---:|---:|---:|
| TITAN RTX / TU102 | PCI 1E02, subsystem 12A310DE, bus 01 | 90.02.1e.00.02 | 1093.75 mV | 1125 mV | **1112.5 mV** |
| TITAN Xp / GP102 | PCI 1B02, subsystem 11DF10DE, bus 02 | 86.02.3d.00.01 | 1093.75 mV | 1125 mV | **1112.5 mV** |

The readings establish driver-reported live voltage, not an independent ADC
or multimeter measurement. This is a short functional test, not a stability
qualification or a search for the highest possible voltage.

## The decisive test

Raising the ceilings alone left both cards at 1081.25 mV: their evaluated
curves had flat frequency bands starting below the cap. An unchanged live
reading in that condition does not show that the raised limit is ignored.

The test temporarily made the **1112.5 mV** point the first point of a higher
frequency band, read the evaluated curve back to confirm this, and held that
point under the existing CUDA bandwidth workload. It then alternated the
ceilings without changing the curve, point lock, boost, memory offsets, power
limit, or rail offset:

| Ceiling configuration | TITAN RTX live | TITAN Xp live |
|---|---:|---:|
| Original 1093.75 mV | 1081.25 mV | 1081.25 mV |
| Raised to 1125 mV | 1112.5 mV | 1112.5 mV |
| Original 1093.75 mV again | 1081.25 mV | 1081.25 mV |
| Raised to 1125 mV again | 1112.5 mV | 1112.5 mV |

Each row contains eight samples, all agreeing. TU102 also moved from 1965 to
1980 MHz. The exact original V/F table and lock bytes were restored, and all
rail control fields and the original 100% voltage boost read back unchanged.

Pascal's raw V/F delta table uses **GPC2CLK** units while the curve reader
returns physical core MHz. The first attempt sent a physical one-bin delta
without doubling it: the delta stored but the evaluated frequency did not
move. Doubling the *increment*, leaving the existing raw delta in its original
units, made the 1112.5 mV point unique and produced the positive result above.
The editor now applies this same conversion when staging and previewing plans.

## Rails and voltage bases

Both TITANs accept singleton mask **1** for NVVDD. Masks **2** (MSVDD) and
**3** (both rails) return NVAPI status **-1** from both getters. Asking for both
rails had hidden valid NVVDD telemetry. There is no confirmed MSVDD control on
either card through this interface, so Druta does not expose one.

The native RM control record has type 2. Nevertheless, the existing
Blackwell-style type-5 records sent through **0x2080F214**, with requested mask
1 and the current voltage-boost field preserved, change all four NVVDD limits.
An all-zero NVAPI control record is valid at stock and must not be discarded.

These are the zero-delta bases, with reliability expressed at **0% boost**:

| Field | TITAN RTX | TITAN Xp | RTX 5080 reference |
|---|---:|---:|---:|
| Reliability | 1068.75 | 1062.5 | 1040 |
| Alternate reliability | 1093.75 | 1093.75 | 1060 |
| Overvoltage | 1125 | 1200 | 1200 |
| Minimum voltage | 650 | 650 | 800 |
| Boost allowance at 100% | 25 | 31.25 | 20 |

All values are mV. Both TITANs' original four deltas were zero. Unlike the
RTX 5080, they have no second rail with a -50 mV reliability default. Absolute
getter header dword 2 reports the *current boost contribution*: zero at 0%
boost and 25/31.25 mV at 100%. The absolute reliability field includes that
contribution; the editable reliability value is its zero-boost base plus delta.

Each field was first changed by 12.5 mV, with the other fields preserved,
and restored. The three ceiling fields were then tested against live voltage
under load. Reliability and alternate clamps at 993.75 mV lowered the live
rail; a 1000 mV overvoltage clamp also lowered it. The rail can settle on a
lower V/F point than the configured limit.

Turing's 875 mV minimum raised a held 850 mV operating point to 875 mV.
Pascal behaved differently: it stayed at 850 mV in the locked/P2 test despite
storing an 875 mV minimum, but an unlocked P8 idle test with a 950 mV minimum
reported 950 mV live. Its minimum is functional, but does not bound every
operating state.

## Druta behavior and reproduction

The two tested PCI/subsystem/VBIOS/driver identities receive their own voltage
bases, stock values, bounds, and NVVDD controls. Unknown identities retain
read-only absolute telemetry. Rail-write opt-in is local to each selected GPU.
The user-selected rail request bounds are **1200 mV normally / 1500 mV
with XOC**, including overvoltage. NVVDD offset permits **+200 / +500 mV**
respectively. These are software request limits, not verified hardware maxima.
Stock values remain the measured per-board defaults. Leaving XOC preserves
existing higher settings and permits reducing them, but prevents raising them
further without re-enabling XOC.

Enable **Rail limits**, link the reliability fields, and set the permitted
ceiling. The V/F planning cap follows a raised ceiling; a suitable de-flattened
curve or requested operating point must actually use the additional voltage.
Use the live rail reading to check the result. Stock restores individual fields
without resetting another field or the independent voltage-boost setting.

The diagnostic defaults to reads. These explicit commands repeat the ceiling
experiment, including temporary curve/lock changes and verified restoration:

```powershell
python tools/probe_volt_rails.py --gpu 0000:01:00.0 --raise-ceiling --output docs/rail-probes/rtx-repeat.json
python tools/probe_volt_rails.py --gpu 0000:02:00.0 --raise-ceiling --output docs/rail-probes/xp-repeat.json
```

The write experiment requires the measured board identity, original zero rail
deltas and 100% boost. It saves exact recovery data before writing. Run it
without another tuner changing the card concurrently.

Sample-level results, identity data, restoration checks and hashes of the
larger local captures are in
[the evidence JSON](experiments/voltage-rails-20260906.json).

The final production setter and individual-field reset were also exercised on
both physical cards for all four fields: all eight writes and restores passed,
with boost preserved.
