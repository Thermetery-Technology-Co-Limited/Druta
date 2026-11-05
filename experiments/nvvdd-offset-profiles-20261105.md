# Ordinary NVVDD offset: manual profiles and selected Undo

The ordinary NVVDD offset now uses a selected `tuning_action` snapshot with
kind `rail_offset`, index `null` and a signed integer microvolt request. It
captures the validated core-control NVVDD field and restores it through the
existing setter. It is distinct from ordinary rail limits, voltage boost and
the separate voltage-policy offset controls.

Apply previously used a full-tune autosave and could proceed when that capture
failed. It now saves only the selected request, refuses a changed write if
capture or saving fails, and skips both saving and writing for an unchanged
request. Named profiles save applied values rather than staged text. Manual
loading and combined profiles use the same scoped validation; a combined full
tune that includes NVVDD cannot overlap another NVVDD offset component. These
scoped profiles never load at startup or automatically after a driver reset.
Existing bounds and XOC requirements remain in effect.

## Local qualification

On the tested RTX 5080 / 617.42 configuration, root ran the production UI and
profile callbacks against an isolated profile directory. The original NVVDD
request was 0 microvolts; the changed request was 1250 microvolts (+1.25 mV).

| Observation | Stored NVVDD request |
|---|---:|
| Baseline | 0 uV |
| Apply | 1250 uV |
| Unchanged Apply | 1250 uV |
| Action Undo | 0 uV |
| Named profile load | 1250 uV |
| Named profile Undo | 0 uV |
| Combined profile load | 1250 uV |
| Combined profile Undo | 0 uV |
| Final | 0 uV |

The unchanged Apply issued no native SET and created no Undo point. Named
saving used an intentionally different staged input and still captured the
applied request. The combined trial contained one NVVDD component; mixed
component combinations have offline overlap coverage, not new hardware
qualification from this trial.

At all nine observations, the complete native clock-control response matched
baseline with only the selected NVVDD request substituted. Voltage boost,
captured power requests and V/F lock readback also matched baseline. The six
native SET packets and their responses are retained for independent audit in
the [receipt](nvvdd-offset-profiles-20261105.json).

This establishes stored request and callback behavior on the tested
configuration. It does not measure physical rail voltage, establish other
adapters' behavior, or qualify additional rail/domain writes. No workload,
registry change or driver/Windows restart was performed. The user's existing
running application was preserved.

Private raw evidence and the root-reviewed harness are under
`registry-overrides/nvvdd-offset-profile-root-20261105/`. Selected-profile,
named UI, combined overlap, startup exclusion, ordinary Undo, voltage setter,
capability discovery and profile preflight tests passed (210 tests).
