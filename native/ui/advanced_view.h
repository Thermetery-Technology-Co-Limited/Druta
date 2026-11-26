/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include "backend.h"
#include "risk_policy.h"
#include "rail_controls.h"
#include <map>
#include <functional>
#include <optional>
#include <string>
#include <vector>
namespace druta::ui {
struct SessionSnapshot;
class PolicyAnnotations;
enum class OffsetGroup { all, daily_clocks, additional_memory };
struct AdvancedDraft {
    double value = 0;
    bool initialized = false, dirty = false, editing = false;
    std::string binding;
};
struct AdvancedViewState {
    uint64_t generation = 0;
    std::map<uint64_t, AdvancedDraft> drafts;
    std::string notice;
    double rail_gate_flash_until = 0, xoc_gate_flash_until = 0;
    // Classic "Link reliability + alt-reliability" (Python vlim_link, default
    // on): a UI staging choice only, kept across an adapter reopen as Python
    // keeps it across a rebuild.
    bool rail_link = true;
    // Python's log line for each accepted Link toggle ("rail ceilings linked:
    // ..." / "unlinked: ..."), oldest first, for the host to move into the
    // session log. Text only: it never stages a command.
    std::vector<std::string> log_notes;
    // Classic Power policies note cell (Python note_cell): rows showing the
    // "others" text box, and the box text while it is being edited.
    std::map<uint32_t, bool> policy_custom_note;
    std::map<uint32_t, std::string> policy_note_edit;
    std::string policy_note_error;
};
RiskAssessment session_risk(const LiveData& data);
// Released 1.9.1 gate-row badge (vlim_badge, its vlim_status tooltip) built from
// the adapter's rail-limit read; Copy info carries the same read as JSON.
struct RailLimitBadge {
    enum class State { checking, readable, ready, unavailable } state = State::checking;
    std::string text, detail;
};
RailLimitBadge rail_limit_badge(const SessionSnapshot& snapshot);
std::string rail_limit_diagnostics(const SessionSnapshot& snapshot);
// The risk score text. Focused draws it inside draw_mode_bar; Classic draws the
// gate checkboxes there and this at the end of its gate row.
void draw_mode_bar_risk(const SessionSnapshot& snapshot, bool unlocked);
void draw_mode_bar(const SessionSnapshot& snapshot, bool& unlocked, std::vector<Command>& commands,
    bool classic = false, const std::function<void()>& i2c_gate = {}, AdvancedViewState* view = nullptr);
void draw_advanced_controls(const SessionSnapshot& snapshot, bool unlocked, bool verbose,
    AdvancedViewState& state, std::vector<Command>& commands, bool include_rails = true, bool include_offsets = true, const PolicyAnnotations* annotations = nullptr);
void draw_clock_voltage_offsets(const SessionSnapshot& snapshot, bool unlocked, bool classic,
    AdvancedViewState& state, std::vector<Command>& commands, OffsetGroup group = OffsetGroup::all,
    const std::vector<uint32_t>& replaced = {});
// Python domain_knob_label: the per-domain offset row's label and its colour,
// from the same pairing and classified rows the Monitor domain table shows.
struct ClockDomainLabel {
    enum class Tone { text, warn, dim, good } tone = Tone::dim;
    std::string text;
};
ClockDomainLabel clock_domain_label(const SessionSnapshot& snapshot, const AdvancedControl& control, bool show_known_pattern = true);
// Python DOMAIN_KNOBS order (controls 1, 3, 5, 4, 9, then 2 last); a control the
// table does not list keeps its adapter order ahead of the additional-memory row.
std::vector<const AdvancedControl*> classic_clock_domain_order(const LiveData& data);
// Python refresh_power_request_status: configured request, NVML enforced and
// native effective limits kept distinct; warn selects WARN over DIM. feedback
// is apply_pl's readback warning for the last board-power request ("" none):
// appended, and WARN while it stands.
struct PowerRequestStatus { std::string text; bool warn = false; };
PowerRequestStatus power_request_status(std::optional<double> requested_w, std::optional<double> enforced_w,
    std::optional<double> native_w, const std::string& feedback = {});
// Common row services reused by the original Control layout and Focused Daily.
// in_open_table: Classic emits the rows into the caller's open Clock offsets table.
void draw_clock_domain_controls(const SessionSnapshot& snapshot, bool unlocked, bool classic,
    AdvancedViewState& state, std::vector<Command>& commands, bool in_open_table = false,
    const std::vector<uint32_t>& replaced = {});
// in_open_table: emit the row into the caller's open classic knob table (Python's
// Limits table, under Power) instead of a table of its own.
void draw_core_current_limit(const SessionSnapshot& snapshot, bool unlocked, bool classic,
    AdvancedViewState& state, std::vector<Command>& commands, bool in_open_table = false);
using RailReadouts = std::array<RailReadout, DC_RAILS_COUNT>;
// Classic adds rail summary rows for the estimated limits, cap, live and effective values,
// plus the Link checkbox. Readouts are optional: without
// them the live and effective cells read "--".
void draw_daily_rail_controls(const SessionSnapshot& snapshot, bool unlocked, bool classic,
    AdvancedViewState& state, std::vector<Command>& commands, bool include_offsets = true,
    const RailReadouts* readouts = nullptr);
// Released 1.9.1 build_power_policy_section: the PSU warning with Max all, the
// explanatory paragraph and one fixed-column row per policy. A writable row has
// the note cell, slider, input, live reading, Apply and Stock; any other row
// says where it is set or why it cannot be written. Notes edit `annotations`
// (display metadata only). Max all and the workspace are returned, not applied.
struct PowerPolicySectionOutput { bool annotations_changed = false, max_all = false, open_workspace = false; };
PowerPolicySectionOutput draw_power_policy_controls(const SessionSnapshot& snapshot, bool unlocked,
    AdvancedViewState& state, std::vector<Command>& commands, PolicyAnnotations* annotations);
// Python druta.py power_policy_row's reason for a row it shows read-only, or
// empty for a row with its own slider.
std::string power_policy_readonly_reason(const PowerPolicy& policy);
// Python refresh_power_policies' live cell: reading and effective limit, per
// 12 V wire (or the slot as a whole) when the channel note names a connector.
// held: the card still holds this session's pin of the request; the limit then
// ends with " *", and an unbanded held row is drawn GOOD. A row the latest read
// did not return reads "unavailable".
struct PowerPolicyLive { std::string text = "--"; bool known = false; int band = -1; bool held = false; };
PowerPolicyLive power_policy_live(const PowerPolicy& policy, const std::string& note);
// Python build_power_policies' "Power policies unavailable: {error}" line, with
// the read's own failure reason when the session reports one.
std::string power_policies_unavailable_text(const SessionSnapshot& snapshot);
}
