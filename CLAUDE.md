# Druta: Claude Code instructions

@AGENTS.md

`AGENTS.md`, imported above, is the instruction file shared with Codex and Grok.
Work on this repo alternates between those agents, so when either file gains a
newer standing instruction, bring the other one up to date.

## Session-only tuning

The current session is for released Python 1.9.1 UI/behavior parity and the
user's explicit layout differences. Follow AGENTS.md: no purely theoretical
issues, improvements beyond that scope, or closed GitHub issues. Check issue
state before issue-linked work; keep broader research/qualification deferred.

Undo and manual saved-profile integration for qualified controls are now in
scope, as recorded in AGENTS.md. Unresolved control research remains separate.

Follow AGENTS.md's session-only requirement: GPU driver restart is the user's
panic/reset action. Do not persist or automatically reapply new tuning requests
across it, or add restart-persistence validation to the functional backlog.

The user authorizes a narrow exception for power relationship-weight overrides
through registry/VBIOS configuration. These overrides require a GPU driver
refresh to load and persist until removed or changed. Ordinary tuning controls
remain session-only.

## Driver versions

- Never add a driver whitelist, allowlist or denylist, and never make a
  decision using only the NVIDIA driver version (version strings, R-branch
  numbers, version comparisons). Use the lower-level checks main already has:
  capability probes, export presence, transport and packet-geometry
  validation, and exact request readback. An export-presence fallback (for
  example NVML v1 versus v2 entry points) is fine, because it tests a
  capability, not a version.
- On the legacy-OS branches (`codex/windows-7-support`,
  `codex/windows-vista-support`), main's refusal and validation logic takes
  precedence over the branches' older refusals, which rejected things at the
  wrong time. Report any remaining difference to the user instead of carrying
  an old refusal forward as a legacy necessity.
