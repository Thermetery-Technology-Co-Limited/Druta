# Agent collaboration

Subagents are always authorized. Use them for concrete, independent subtasks
when useful, without asking for additional permission. Coordinate shared-file
edits and hardware access; only one agent may write to a GPU at a time.

# Hardware capability and evidence

Never implement a safety check or feature gate solely from observations on a
developer's devices. Gate architectural support by GPU generation, then validate
the understood API/layout and capabilities reported by the user's current
adapter. Developer-owned device IDs, subsystem IDs, VBIOS versions and driver
versions must not become compatibility allowlists or denylists merely because
those combinations were tested locally.

No developer's hardware is a universal source of truth. A successful or failed
local experiment establishes evidence only for the tested device, configuration
and operating point. Do not generalize its voltage bases, factory defaults,
thresholds, rail topology, precision, routing or physical response to other
devices, including other devices of the same generation. Record that scope
explicitly. Generation is a basis for architectural support, not permission to
turn one board's measurements into generation-wide constants.

Obtain device-specific values from the current adapter or authoritative
interface specifications. When a value or physical effect is unknown, expose
that uncertainty; distinguish estimated values and captured initial state from
verified measurements and factory defaults. Missing unrelated rails or telemetry
and transient read failures must not silently remove otherwise supported
controls; provide independent capability checks and a bounded retry path.

Retain checks that establish the correct target and an understood operation:
GPU-handle pairing, controller identity, packet geometry, units, selected-field
validation, exact request readback and restoration. These checks must validate
the operation itself, not whether the user owns a developer's tested hardware.
Regression coverage for capability changes must include alternate identities,
partial capabilities and transient failures beyond the local hardware profile.

# Process management

You can always terminate a working process if it's necessary for review, updates, or changes. Even `sudo kill -9` or an elevated `taskkill /f` are authorized.
