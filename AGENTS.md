# Coding strategy

Trace the existing path before editing it. Fix the underlying cause using
actual API or protocol evidence, rather than assumptions drawn from one local
configuration. Keep changes focused and reuse the project's existing
abstractions where they fit.

Preserve fields and state whose meaning is unknown or outside the requested
change. Treat a transient read failure differently from an unsupported
capability: use bounded read-only recovery for the former and preserve controls
whose current valid state has already been established. Add meaningful tests
for observable behavior and failure cases, rather than tests that merely mirror
the implementation.

# Agent collaboration

The root agent supervises the work. Subagents are authorized for concrete,
independent tasks when useful; prefer Sol or Terra for easy, bounded
implementation, documentation, or test work when that is the user's stated
model preference. Give each task explicit paths, scope, and acceptance
criteria. Assign one owner per file, or coordinate clear non-overlapping
boundaries before editing shared files.

Agents must share relevant findings and blockers promptly. Coordinate hardware
access; only one agent may write to a GPU at a time. The root agent reviews
diffs and validation evidence and integrates the result; it must not accept
subagent work blindly. Follow the current user model instructions for any
adversarial review.

# Hardware capability and evidence

Never implement a safety check or feature gate solely from observations on a
developer's devices. Gate architectural support by GPU generation, then validate
the understood API/layout and capabilities reported by the user's current
adapter. Developer-owned device IDs, subsystem IDs, VBIOS versions and driver
versions must not become compatibility allowlists or denylists merely because
those combinations were tested locally.

Never add a driver whitelist, allowlist or denylist, and never make a decision
using only the NVIDIA driver version (version strings, R-branch numbers,
version comparisons). Use the lower-level checks instead: capability probes,
export presence, transport and packet-geometry validation, and exact request
readback. An export-presence fallback (for example NVML v1 versus v2 entry
points) is fine, because it tests a capability, not a version.

On the legacy-OS branches (`codex/windows-7-support`,
`codex/windows-vista-support`), main's refusal and validation logic takes
precedence over the branches' older refusals, which rejected things at the
wrong time. Report any remaining difference to the user instead of carrying an
old refusal forward as a legacy necessity.

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

# Instruction files

This file is shared by Codex, Grok and Claude Code (`CLAUDE.md` imports it).
Work on this repo alternates between those agents, so when either file gains a
newer standing instruction, bring the other one up to date.

# Process management

You can always terminate a working process if it's necessary for review, updates, or changes. Even `sudo kill -9` or an elevated `taskkill /f` are authorized.

# End-of-arc handback and Git

When handing a completed work arc back for human review, leave the freshly
built application EXE running on the desktop. Do not push to `main`, a
production branch, or force-push without explicit authorization. Ask before
opening a pull request. Explicit authorization to publish a release permits
the release tag and release, but does not by itself permit a push to `main`.
