/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "advanced_view.h"
#include "session.h"
#include "imgui.h"
#include "classic_layout.h"
#include "classic_section.h"
#include "policy_workspace.h"
#include "clock_monitor_model.h"
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <iterator>
#include <optional>

namespace druta::ui {
namespace {
float u(float v) { return v * ImGui::GetFontSize() / 18.0f; }
ImVec4 color(RiskColor c) { return {c.r / 255.0f, c.g / 255.0f, c.b / 255.0f, 1}; }
// Released 1.9.1 WARN, DIM and GOOD.
const ImVec4 classic_warn{255 / 255.0f, 203 / 255.0f, 71 / 255.0f, 1};
const ImVec4 classic_dim{139 / 255.0f, 144 / 255.0f, 153 / 255.0f, 1};
const ImVec4 classic_good{70 / 255.0f, 209 / 255.0f, 122 / 255.0f, 1};
// Released 1.9.1 TEXT: slider_row labels and present live readings, not the theme's white.
const ImVec4 classic_text_tone{230 / 255.0f, 232 / 255.0f, 236 / 255.0f, 1};
void hint(const std::string& text) {
    if (!text.empty() && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled)) {
        ImGui::BeginTooltip(); ImGui::PushTextWrapPos(u(420));
        ImGui::TextUnformatted(text.c_str()); ImGui::PopTextWrapPos(); ImGui::EndTooltip();
    }
}
bool ready(const SessionSnapshot& s) { return s.data.connected && !s.opening && !s.busy; }
bool table(const char* id, bool classic = false) {
    if (classic) return begin_classic_knob_table(id);
    if (!ImGui::BeginTable(id, 4, ImGuiTableFlags_SizingStretchProp)) return false;
    ImGui::TableSetupColumn("Control", ImGuiTableColumnFlags_WidthStretch, 1.3f);
    ImGui::TableSetupColumn("Request", ImGuiTableColumnFlags_WidthStretch, 1.1f);
    ImGui::TableSetupColumn("Readback", ImGuiTableColumnFlags_WidthFixed, u(122));
    ImGui::TableSetupColumn("Action", ImGuiTableColumnFlags_WidthFixed, u(155));
    return true;
}
// Classic Daily readback cell: what the card reports now (Python refresh_control's
// live column). "--" is drawn dim; the request stays in the input widget.
struct LiveCell { std::string text = "--", detail; bool stale = false, dim = false; std::optional<ImVec4> tone; };
std::string mhz(double value, bool sign) {
    char buffer[48]{}; std::snprintf(buffer, sizeof(buffer), sign ? "%+.0f MHz" : "%.0f MHz", value); return buffer;
}
bool private_clocks_stale(const SessionSnapshot& s) {
    return s.paused || !s.data.connected || (s.telemetry_stale_mask & DC_T_PRIV_CLOCKS) || !s.data.clock_monitor || s.data.clock_monitor->stale;
}
// Python: the paired private domain's programmed clock from the rows the Monitor
// domain table shows; on Blackwell the requested control-block offset instead.
LiveCell clock_domain_live(const SessionSnapshot& s, const AdvancedControl& c) {
    LiveCell live;
    if (s.data.clock_domain_requests_only) {
        if (c.state.current && std::isfinite(*c.state.current)) live.text = mhz(*c.state.current, true);
        live.detail = "Requested control-block offset. On this generation the private clock getter's domains are not "
            "treated as the same namespace as these controls, so no live clock is paired.";
        return live;
    }
    const auto pair = std::find_if(s.data.clock_domain_pairs.begin(), s.data.clock_domain_pairs.end(),
        [&](const dc_clkdom_pair& p) { return p.control == c.id.domain; });
    if (pair == s.data.clock_domain_pairs.end() || !s.data.clock_monitor) {
        live.detail = "No private clock domain is paired with this control on this adapter.";
        return live;
    }
    const auto& rows = s.data.clock_monitor->rows;
    const auto row = std::find_if(rows.begin(), rows.end(), [&](const ClockDomainViewRow& r) { return r.raw.domain == pair->domain; });
    live.detail = "Programmed clock of private domain " + std::to_string(pair->domain)
        + (row != rows.end() && row->raw.name[0] ? std::string(" ") + row->raw.name : std::string())
        + ", the same row as the Monitor domain table.";
    if (row != rows.end() && row->raw.has_prog_mhz && std::isfinite(row->raw.prog_mhz) && row->raw.prog_mhz != 0) {
        live.text = mhz(row->raw.prog_mhz, false);
        live.stale = private_clocks_stale(s);
    }
    return live;
}
std::string request_text(const ControlState& c) {
    if (!c.current || !std::isfinite(*c.current)) return "Configured request: --.";
    char buffer[96]{}; std::snprintf(buffer, sizeof(buffer), "Configured request: %.6g %s.", *c.current, c.unit.c_str()); return buffer;
}
void row(uint64_t key, const char* label, const ControlState& c, const char* stock_label,
    Command action, const SessionSnapshot& snapshot, bool unlocked, bool verbose,
    AdvancedViewState& state, std::vector<Command>& commands, const std::string& binding, bool keep_slider = false, bool gate = true, bool xoc_gate = false, const PolicyCurrentDisplay* observed = nullptr,
    const LiveCell* live = nullptr, const char* input_format = "%.9g", const ImVec4* label_tone = nullptr, bool soft_gate = false,
    const std::function<void()>& label_cell = {}, const std::function<std::optional<ImVec4>(double)>& input_tone = {}) {
    auto& d = state.drafts[key];
    const auto current_binding = binding.empty() && (c.availability == Availability::transient || c.availability == Availability::unknown)
        ? d.binding : binding;
    if (d.binding != current_binding) {
        if (d.initialized) state.notice = std::string(label) + " changed identity or reference; its draft was reset.";
        d = {}; d.binding = current_binding;
    }
    action.expected_binding = current_binding;
    if (c.current && !d.editing && (!d.initialized || !d.dirty || d.value == *c.current)) {
        d.value = *c.current; d.initialized = true; d.dirty = false;
    }
    ImGui::PushID(static_cast<int>(key));
    // Classic: a disabled Dear PyGui 2.3.1 item keeps its normal look, so the
    // row refuses input without dimming. soft_gate (Python's rail-limit rows)
    // keeps the widgets live and refuses only the Apply, flashing the gate.
    if (verbose) ImGui::PushStyleVar(ImGuiStyleVar_DisabledAlpha, 1.0f);
    ImGui::TableNextRow(); ImGui::TableNextColumn(); ImGui::AlignTextToFramePadding();
    // Python slider_row: color=color or TEXT.
    if (!label_tone && verbose && !label_cell) label_tone = &classic_text_tone;
    if (label_tone) ImGui::PushStyleColor(ImGuiCol_Text, *label_tone);
    // Python slider_row wraps the long labels (additional memory, current) at
    // KNOB_COLS[0] - 10; the short ones fit within that anyway.
    if (label_cell) label_cell();
    else if (verbose) {
        ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + classic_px(classic_knob_widths[0] - 10));
        ImGui::TextUnformatted(label); ImGui::PopTextWrapPos();
    } else ImGui::TextUnformatted(label);
    if (label_tone) ImGui::PopStyleColor();
    if (!label_cell) hint(c.detail);
    ImGui::TableNextColumn();
    const float width = ImGui::GetContentRegionAvail().x;
    const bool slider = c.low && c.high && std::isfinite(*c.low) && std::isfinite(*c.high)
        && *c.low < *c.high && (keep_slider || verbose || width > u(190));
    const bool stacked = slider && !verbose && width <= u(190);
    bool editing = false;
    // A soft-gated row's widgets and Initial are live, so only its Apply (below) flashes the gate.
    const auto flash_gate = [&] { if (!gate && !soft_gate && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled) && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) (xoc_gate ? state.xoc_gate_flash_until : state.rail_gate_flash_until) = ImGui::GetTime() + 0.8; };
    ImGui::BeginDisabled(!gate && !soft_gate);
    if (slider) {
        ImGui::SetNextItemWidth(verbose || stacked ? -1 : width - u(92) - ImGui::GetStyle().ItemSpacing.x);
        if (ImGui::SliderScalar("##slider", ImGuiDataType_Double, &d.value, &*c.low, &*c.high, verbose ? " " : "%.6g", ImGuiSliderFlags_NoInput)) {
            const double step = keep_slider ? c.step.value_or(0.0)
                : std::max(c.step.value_or(1.0), c.unit == "mV" || c.unit == "MHz" ? 1.0 : 0.001);
            if (std::isfinite(step) && step > 0) {
                const double origin = keep_slider ? *c.low : 0.0;
                d.value = std::max(*c.low, std::min(*c.high, origin + std::floor((d.value - origin) / step + 0.5) * step));
            }
            d.dirty = true; d.initialized = true;
        }
        flash_gate(); editing = ImGui::IsItemActive(); if (!verbose && !stacked) ImGui::SameLine();
    }
    if (verbose) ImGui::TableNextColumn();
    ImGui::SetNextItemWidth(verbose || stacked ? -1 : slider ? u(92) : -1);
    const auto tone = input_tone ? input_tone(d.value) : std::nullopt;
    if (tone) ImGui::PushStyleColor(ImGuiCol_Text, *tone);
    if (ImGui::InputDouble("##value", &d.value, 0, 0, input_format)) { d.dirty = true; d.initialized = true; }
    if (tone) ImGui::PopStyleColor();
    flash_gate(); d.editing = editing || ImGui::IsItemActive();
    ImGui::EndDisabled();
    hint("Draft only. Apply sends this selected request; polling preserves unfinished edits.");
    ImGui::TableNextColumn(); ImGui::AlignTextToFramePadding();
    if (verbose) push_classic_mono();
    if (live && verbose) {
        if (live->text == "--" || live->dim) ImGui::TextDisabled("%s", live->text.c_str());
        else {
            ImGui::PushStyleColor(ImGuiCol_Text, live->tone ? *live->tone : classic_text_tone);
            ImGui::TextWrapped("%s%s", live->text.c_str(), live->stale ? " (stale)" : "");
            ImGui::PopStyleColor();
        }
    }
    else if (c.current && verbose) {
        ImGui::PushStyleColor(ImGuiCol_Text, classic_text_tone);
        ImGui::TextWrapped("%.6g %s%s", *c.current, c.unit.c_str(), c.availability == Availability::transient ? " *" : "");
        ImGui::PopStyleColor();
    }
    else if (c.current) ImGui::Text("%.6g %s%s", *c.current, c.unit.c_str(), c.availability == Availability::transient ? " *" : "");
    else ImGui::TextDisabled("-- %s", c.unit.c_str());
    if (verbose) ImGui::PopFont();
    if (live && verbose) hint(live->detail + (live->detail.empty() ? "" : "\n") + request_text(c));
    if(observed) {
        const auto band=observed->band;
        const ImVec4 tint=band==PolicyCurrentBand::white?ImVec4(1,1,1,1):band==PolicyCurrentBand::yellow?ImVec4(1,0.9f,0,1):band==PolicyCurrentBand::red?ImVec4(1,0.2f,0.2f,1):ImVec4(0.86f,0.08f,0.24f,1);
        ImGui::TextColored(tint,"%.3f A%s",observed->amps,observed->even_split?" / wire":" slot");
        hint(observed->even_split?"Reported channel current divided evenly among the selected connector's 12 V wires; actual sharing is not measured.":"Reported PCIe slot current.");
    }
    hint(c.detail);
    if (verbose && !live) hint("Configured request, not an observed physical clock or rail measurement.");
    ImGui::TableNextColumn();
    // A soft-gated row's writable flag includes the gate itself; with the gate
    // off its Apply only flashes the gate, so it stays clickable.
    const bool refused_by_gate = soft_gate && !gate;
    ImGui::BeginDisabled(!ready(snapshot) || !unlocked || (!c.writable && !refused_by_gate) || (!gate && !soft_gate) || !d.initialized || !std::isfinite(d.value));
    if (ImGui::Button("Apply", ImVec2(verbose ? -1 : u(64), 0))) {
        // Python apply_vlim: with the gate off nothing is written; the gate flashes.
        if (!gate) (xoc_gate ? state.xoc_gate_flash_until : state.rail_gate_flash_until) = ImGui::GetTime() + 0.8;
        else { action.operation = Command::Operation::apply; action.value = d.value; commands.push_back(action); }
    }
    flash_gate(); ImGui::EndDisabled(); hint(c.detail);
    if (verbose) ImGui::TableNextColumn(); else ImGui::SameLine();
    // Python keeps the rail-limit Initial and the XOC rows' Stock/Zero enabled
    // whatever the gate: restoring the first-read value is the recovery action.
    ImGui::BeginDisabled(!ready(snapshot) || !unlocked || (!gate && !xoc_gate && !soft_gate) || !c.stock_writable);
    if (ImGui::Button(stock_label, ImVec2(verbose ? -1 : u(80), 0))) { action.operation = Command::Operation::stock; commands.push_back(action); }
    if (!xoc_gate) flash_gate();
    ImGui::EndDisabled();
    hint(c.detail);
    if (verbose) ImGui::PopStyleVar();
    ImGui::PopID();
}
void policy_row(const PowerPolicy& p,const SessionSnapshot& s,bool unlocked,bool classic,AdvancedViewState& state,std::vector<Command>& commands,const PolicyAnnotations* annotations) {
    ScopedCurrent metadata;metadata.key=ScopedKey::policy_request(p.id);metadata.binding=p.binding;metadata.channel=p.channel;metadata.policy_type=p.policy_type;
    const auto note=annotations?annotations->get({s.data.identity.uuid,s.selected_slot},metadata):std::string{};
    const auto label=p.label+(note.empty()?"":" ("+note+")");
    const auto observed=policy_current_display(note,p.state.unit=="A"?"mA":"",p.state.availability==Availability::available?p.observed_amps:std::nullopt);
    Command cmd;cmd.power_policy=p.id;
    row(2000+p.id,label.c_str(),p.state,"Default",cmd,s,unlocked,classic,state,commands,p.binding,false,true,false,observed?&*observed:nullptr);
}
void ensure_generation(const SessionSnapshot& s, AdvancedViewState& state) {
    if (state.generation != s.generation) {
        const bool link = state.rail_link; auto notes = std::move(state.log_notes);
        state = {}; state.generation = s.generation; state.rail_link = link; state.log_notes = std::move(notes);
    }
}
// Python's vlim_link exists only while the Ceilings row is built, which needs a
// writable rail block; without the row nothing is linked.
bool rail_link_active(const SessionSnapshot& s, const AdvancedViewState& state) {
    return state.rail_link && s.data.rail_writable_known && s.data.rail_writable;
}
// The other reliability field of a linked pair, when both have the estimated
// base the absolute request needs. Python always names both fields; a pair
// that cannot be completed is refused, never sent as one field.
const RailControl* rail_link_partner(const SessionSnapshot& s, const RailControl& r) {
    if (r.id.field > 1 || !r.estimated_base_mv) return nullptr;
    const auto partner = std::find_if(s.data.rail_controls.begin(), s.data.rail_controls.end(), [&](const RailControl& p) {
        return p.id.rail == r.id.rail && p.id.field == 1 - r.id.field; });
    return partner == s.data.rail_controls.end() || !partner->estimated_base_mv ? nullptr : &*partner;
}
void rail_row(const RailControl& r, const SessionSnapshot& s, bool unlocked, bool classic,
    AdvancedViewState& state, std::vector<Command>& commands) {
    if (r.id.field >= 4) return;
    // Python build_volt_limits_rows: "<rail> <field> (~mV)", the minimum named vmin.
    const char* names[] = {"reliability", "alt-reliability", "overvoltage", classic ? "vmin" : "min"};
    Command cmd; cmd.rail = r.id;
    const auto name = std::string(r.id.rail == 0 ? "NVVDD " : "MSVDD ") + names[r.id.field] + " (~mV)";
    auto shown = r.state;
    const double base = r.estimated_base_mv.value_or(0);
    if (r.estimated_base_mv) {
        if (shown.current) *shown.current += base;
        if (shown.stock) *shown.stock += base;
        if (shown.low) *shown.low += base;
        if (shown.high) *shown.high += base;
        shown.unit = "~mV";
        shown.detail += "; absolute estimate = captured base + stored delta, not a factory default";
    } else shown.detail += "; base estimate unavailable; request delta only";
    const auto before = commands.size();
    // Python shows no live reading beside a rail-limit request.
    LiveCell live; live.detail = "No live reading for a rail-limit request.";
    // Python: rail-1 rows in WARN; on a writable block the widgets stay live
    // with Rail limits unticked and only the Apply is refused.
    row(3000 + r.id.rail * 4 + r.id.field, name.c_str(), shown, "Initial", cmd, s, unlocked, classic, state, commands, r.binding, true, s.data.rail_mode_known && s.data.rail_mode_enabled,
        false, nullptr, &live, "%.9g", classic && r.id.rail == 1 ? &classic_warn : nullptr,
        classic && s.data.rail_writable_known && s.data.rail_writable);
    const bool linking = classic && r.id.field <= 1 && rail_link_active(s, state);
    const RailControl* partner = linking ? rail_link_partner(s, r) : nullptr;
    std::vector<Command> staged(commands.begin() + static_cast<std::ptrdiff_t>(before), commands.end());
    commands.resize(before);
    for (auto& c : staged) {
        if (c.operation != Command::Operation::apply) { commands.push_back(std::move(c)); continue; }
        const double absolute = c.value;
        c.value -= base;
        if (!linking) { commands.push_back(std::move(c)); continue; }
        // Python apply_vlim with vlim_link ticked: moving one of reliability and
        // alt-reliability asks for that same absolute voltage on both, each
        // through its own estimated base. Python always names both fields, so
        // a pair that cannot be completed is refused (the Ceilings row says
        // why) instead of writing one field alone.
        if (!partner) continue;
        // One Command, one setter call: the partner carries its own request
        // delta and reference binding, preflighted and read back with the moved field.
        c.rail_partner = RailPartnerRequest{partner->id, absolute - *partner->estimated_base_mv, partner->binding};
        commands.push_back(std::move(c));
        // The partner's draft follows its readback instead of keeping an older edit.
        auto& draft = state.drafts[3000 + partner->id.rail * 4 + partner->id.field];
        draft.dirty = false; draft.editing = false;
    }
}
// Python volt_limits_cells / refresh_rail_live: one summary row per present
// rail above the sliders. Absolute fields are the estimated base plus the stored
// request; the cap adds the reported boost to reliability; live and effective
// come from the driver's absolute rail read. Unknown values stay unknown.
const ImVec4 rail_dim{139 / 255.0f, 144 / 255.0f, 153 / 255.0f, 1};
const ImVec4 rail_good{70 / 255.0f, 209 / 255.0f, 122 / 255.0f, 1};
std::string rail_fields_text(double rel, double alt, double ov, double vmin) {
    char buffer[160]{};
    std::snprintf(buffer, sizeof(buffer), "rel %.9g / alt %.9g\nov %.9g / vmin %.9g mV", rel, alt, ov, vmin);
    return buffer;
}
void dim_wrapped(const std::string& text) {
    ImGui::PushStyleColor(ImGuiCol_Text, rail_dim); ImGui::TextWrapped("%s", text.c_str()); ImGui::PopStyleColor();
}
void rail_summary_rows(const SessionSnapshot& s, AdvancedViewState& state, const RailReadouts* readouts) {
    bool listed[DC_RAILS_COUNT]{};
    for (uint32_t rail = 0; rail < DC_RAILS_COUNT; ++rail) {
        std::optional<double> absolute[DC_RAILS_LIMIT_FIELDS];
        for (const auto& r : s.data.rail_controls)
            if (r.id.rail == rail && r.id.field < DC_RAILS_LIMIT_FIELDS && r.estimated_base_mv && r.state.current && std::isfinite(*r.state.current))
                absolute[r.id.field] = *r.state.current + *r.estimated_base_mv;
        const RailReadout* readout = readouts ? &(*readouts)[rail] : nullptr;
        // A failed read keeps the last reading, marked stale; the cap waits for a fresh one.
        const dc_rails_state_row* live = readout && readout->state ? &*readout->state : nullptr;
        const bool stale = readout && readout->stale;
        const char* stale_mark = stale ? " (stale)" : "";
        const auto known = [](const std::optional<double>& v) { return v.has_value(); };
        const bool supported = std::all_of(std::begin(absolute), std::end(absolute), known);
        // Which rails the driver lists is known only from a readout; without
        // one the NVVDD-only note is not claimed.
        listed[rail] = readout && readout->listed;
        std::string label = rail == 0 ? "NVVDD" : "MSVDD", fields, cap;
        if (supported) {
            label += " estimated limits";
            fields = rail_fields_text(*absolute[0], *absolute[1], *absolute[2], *absolute[3]);
            if (readout && !readout->stale && readout->boost_mv) {
                char buffer[64]{};
                std::snprintf(buffer, sizeof(buffer), "estimated cap %.9g", std::min({*absolute[0] + *readout->boost_mv, *absolute[1], *absolute[2]}));
                cap = buffer;
            }
        } else if (live) {
            label += " (read-only)";
            fields = rail_fields_text(live->reliability, live->alt_reliability, live->overvoltage, live->vmin);
        } else continue;
        ImGui::PushID(static_cast<int>(4000 + rail));
        // Python volt_limits_cells: five add_text cells, each on a frame-height line.
        ImGui::TableNextRow(); ImGui::TableNextColumn(); ImGui::AlignTextToFramePadding();
        ImGui::TextColored(rail_dim, "%s", label.c_str());
        ImGui::TableNextColumn(); ImGui::AlignTextToFramePadding(); dim_wrapped(fields);
        ImGui::TableNextColumn(); ImGui::AlignTextToFramePadding(); ImGui::TextColored(rail_dim, "%s", cap.c_str());
        hint(cap.empty() ? "Estimated cap needs this read's reported voltage-boost contribution."
            : "min(reliability + current boost contribution, alt-reliability, overvoltage); an estimate from the captured reference.");
        ImGui::TableNextColumn(); ImGui::AlignTextToFramePadding();
        char text[64]{};
        const bool has_live = live && live->live != 0 && std::isfinite(live->live);
        if (has_live) std::snprintf(text, sizeof(text), "live %.9g mV%s", live->live, stale_mark);
        else std::snprintf(text, sizeof(text), "live --");
        ImGui::TextColored(has_live && stale ? rail_dim : rail_good, "%s", text);
        hint(std::string("Driver-reported rail reading, not a physical VOUT measurement.")
            + (stale ? "\nThe last rail read failed; this is the previous reading." : ""));
        ImGui::TableNextColumn(); ImGui::AlignTextToFramePadding();
        if (live && live->effective != 0 && std::isfinite(live->effective)) ImGui::TextColored(rail_dim, "eff %.9g%s", live->effective, stale_mark);
        hint("The card's own effective-limit readback.");
        ImGui::PopID();
    }
    if (!s.data.rail_writable_known || !s.data.rail_writable) return;
    // Dear PyGui sets each text item on a frame-height line, 3 px down.
    ImGui::TableNextRow(); ImGui::TableNextColumn(); ImGui::AlignTextToFramePadding(); ImGui::TextColored(rail_dim, "Ceilings");
    ImGui::TableNextColumn();
    // Python enables the box only while Rail limits is live on a writable block;
    // a disabled Dear PyGui item keeps its normal look, so only the click is refused.
    // Python's checkbox callback logs this line on every accepted change.
    const auto link_message = [](bool linked) {
        return linked ? "rail ceilings linked: both fields move together, which leaves the voltage boost slider with no range to work in"
                      : "rail ceilings unlinked: each field moves independently; watch the cap and effective limit";
    };
    bool link = state.rail_link;
    if (ImGui::Checkbox("Link reliability + alt-reliability", &link) && s.data.rail_mode_known && s.data.rail_mode_enabled) {
        state.rail_link = link;
        state.log_notes.emplace_back(link_message(link));
        if (state.log_notes.size() > 32) state.log_notes.erase(state.log_notes.begin()); // undrained host
    }
    hint(link_message(state.rail_link));
    // Native only: a linked Apply needs both fields with an estimated base.
    // When one is missing (a transient read or no captured reference) the
    // linked Apply is refused rather than written as one field.
    if (state.rail_link) for (uint32_t rail = 0; rail < DC_RAILS_COUNT; ++rail) {
        bool incomplete = false;
        for (const auto& r : s.data.rail_controls)
            if (r.id.rail == rail && r.id.field <= 1 && !rail_link_partner(s, r)) incomplete = true;
        if (!incomplete) continue;
        ImGui::TableNextRow(); ImGui::TableNextColumn(); ImGui::TableNextColumn();
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(1, 0.75f, 0.3f, 1));
        ImGui::TextWrapped("%s: reliability and alt-reliability are not both readable with a captured reference; "
            "Apply on either is refused while Link is ticked.", rail == 0 ? "NVVDD" : "MSVDD");
        ImGui::PopStyleColor();
    }
    ImGui::TableNextRow(); ImGui::TableNextColumn(); ImGui::AlignTextToFramePadding(); ImGui::TextColored(rail_dim, "Reference");
    ImGui::TableNextColumn(); ImGui::AlignTextToFramePadding();
    dim_wrapped("Absolute values are estimates from this card's first stable driver reading, which may be quantized. "
        "Initial restores exact first-read controls, not factory defaults.");
    if (listed[0] && !listed[1]) {
        ImGui::TableNextRow(); ImGui::TableNextColumn(); ImGui::AlignTextToFramePadding(); ImGui::TextColored(rail_dim, "NVVDD");
        ImGui::TableNextColumn(); ImGui::AlignTextToFramePadding();
        dim_wrapped("Driver reports NVVDD only. Absolute requests use this card's first-read reference.");
    }
}
// Python: the NVVDD offset row shows the same live Vcore as the boost row
// ("%.2f mV"); the MSVDD request has no live reading.
LiveCell rail_offset_live(const SessionSnapshot& s, const AdvancedControl& c) {
    LiveCell live;
    if (c.id.kind != AdvancedKind::nvvdd) { live.detail = "Stored request; no live MSVDD reading."; return live; }
    const auto& t = s.data.telemetry;
    live.detail = "Live Vcore (NVVDD), the same rail as Core voltage boost.";
    if ((t.present & DC_T_VCORE_MV) && std::isfinite(t.vcore_mv) && t.vcore_mv != 0) {
        char buffer[48]{}; std::snprintf(buffer, sizeof(buffer), "%.2f mV", t.vcore_mv); live.text = buffer;
        live.stale = s.paused || !s.data.connected || (s.telemetry_stale_mask & DC_T_VCORE_MV);
    }
    return live;
}
// Python refresh_current_limits: reported rail current over the effective ceiling.
LiveCell core_current_live(const PowerPolicy& p) {
    LiveCell live; live.detail = "First line: reported rail current.\nSecond line: effective current ceiling.";
    // Python refresh_current_limits: no current-limit row in this read -> DIM "unavailable".
    if (!p.observed_amps || !p.limit_amps || !std::isfinite(*p.observed_amps) || !std::isfinite(*p.limit_amps)) {
        live.text = "unavailable"; live.dim = true; return live;
    }
    char limit[48]{}; std::snprintf(limit, sizeof(limit), "%.3f", *p.limit_amps);
    std::string ceiling = limit;
    while (!ceiling.empty() && ceiling.back() == '0') ceiling.pop_back();
    if (!ceiling.empty() && ceiling.back() == '.') ceiling.pop_back();
    char buffer[96]{}; std::snprintf(buffer, sizeof(buffer), "%.1f A\n\xE2\x89\xA4%s A", *p.observed_amps, ceiling.c_str());
    live.text = buffer; live.stale = p.state.availability == Availability::transient;
    return live;
}
}
RiskAssessment session_risk(const LiveData& data) {
    auto flag = [](bool known, bool enabled) { return !known ? RiskFlag::unknown : enabled ? RiskFlag::active : RiskFlag::inactive; };
    RiskGates gates;
    gates.xoc_enabled = flag(data.xoc_known, data.xoc_enabled);
    gates.rail_limits_enabled = flag(data.rail_mode_known, data.rail_mode_enabled);
    gates.rail_limits_writable = flag(data.rail_writable_known, data.rail_writable);
    const bool owned = data.i2c.status.owned || data.i2c.status.recovery_pending;
    gates.i2c_enabled = data.i2c.gate || owned ? RiskFlag::active : RiskFlag::inactive;
    gates.i2c_controller_writable = data.i2c.status.capable || owned ? RiskFlag::active : RiskFlag::inactive;
    return assess_risk(gates);
}
namespace {
const char* rail_name(uint32_t rail) { return rail == 0 ? "NVVDD" : rail == 1 ? "MSVDD" : "rail"; }
std::string json_string(const std::string& text) {
    std::string out = "\"";
    for (const char c : text) {
        if (c == '"' || c == '\\') { out += '\\'; out += c; }
        else if (c == '\n') out += "\\n";
        else if (static_cast<unsigned char>(c) < 0x20) { char b[8]{}; std::snprintf(b, sizeof(b), "\\u%04x", c); out += b; }
        else out += c;
    }
    return out + "\"";
}
std::string json_number(const std::optional<double>& value) {
    if (!value || !std::isfinite(*value)) return "null";
    char b[48]{}; std::snprintf(b, sizeof(b), "%.9g", *value); return b;
}
}
// Python rail_limit_status: a rail is ready when the driver reports it writable
// and its fresh control carries an estimated base; the others are named missing.
RailLimitBadge rail_limit_badge(const SessionSnapshot& s) {
    RailLimitBadge badge;
    const auto& d = s.data;
    if (s.opening) {
        badge.text = "Rail limits: checking\xE2\x80\xA6";
        badge.detail = "Checking whether this GPU can safely use rail limits\xE2\x80\xA6";
        return badge;
    }
    bool seen[DC_RAILS_COUNT]{}, based[DC_RAILS_COUNT]{};
    for (const auto& r : d.rail_controls) if (r.id.rail < DC_RAILS_COUNT) {
        seen[r.id.rail] = true;
        if (r.estimated_base_mv) based[r.id.rail] = true;
    }
    std::string ready, missing;
    for (uint32_t rail = 0; rail < DC_RAILS_COUNT; ++rail) {
        if (based[rail]) { ready += ready.empty() ? "" : ", "; ready += rail_name(rail); }
        else { missing += " "; missing += rail_name(rail); missing += seen[rail] ? ": no base-voltage estimate" : " unavailable"; }
    }
    if (d.rail_writable_known && d.rail_writable && !ready.empty()) {
        badge.state = RailLimitBadge::State::ready;
        badge.text = "Rail limits: ready";
        badge.detail = "Rail limits ready for " + ready + "." + missing;
        return badge;
    }
    badge.state = RailLimitBadge::State::unavailable;
    badge.text = "Rail limits: unavailable";
    const char* reason = !d.connected ? "No GPU session is connected."
        : !d.rail_writable_known ? "Rail detection did not return a usable result. Retry detection."
        : d.rail_controls.empty() ? "The driver did not provide current rail controls."
        : d.rail_writable ? "No rail control carries a base-voltage estimate."
        : "The driver did not confirm a writable rail.";
    badge.detail = std::string("Rail limits are unavailable: ") + reason;
    return badge;
}
// Python copy_rail_limit_diagnostics: the detection details as indented JSON.
// Read-only; nothing here touches the GPU.
std::string rail_limit_diagnostics(const SessionSnapshot& s) {
    const auto& d = s.data;
    const auto badge = rail_limit_badge(s);
    const char* fields[] = {"reliability", "alt_reliability", "overvoltage", "vmin"};
    std::string out = "{\n  \"available\": ";
    out += badge.state == RailLimitBadge::State::ready ? "true" : badge.state == RailLimitBadge::State::unavailable ? "false" : "null";
    out += ",\n  \"reason\": " + json_string(badge.detail);
    out += ",\n  \"writable\": " + std::string(!d.rail_writable_known ? "null" : d.rail_writable ? "true" : "false");
    out += ",\n  \"session_mode\": " + std::string(!d.rail_mode_known ? "null" : d.rail_mode_enabled ? "true" : "false");
    out += ",\n  \"rails\": {";
    bool first_rail = true;
    for (uint32_t rail = 0; rail < DC_RAILS_COUNT; ++rail) {
        bool first_field = true;
        for (const auto& r : d.rail_controls) {
            if (r.id.rail != rail) continue;
            if (first_field) {
                out += first_rail ? "\n" : ",\n";
                out += "    " + json_string(std::to_string(rail)) + ": {\n      \"name\": " + json_string(rail_name(rail)) + ",\n      \"fields\": {";
                first_rail = false;
            }
            out += first_field ? "\n" : ",\n"; first_field = false;
            const std::string field = r.id.field < 4 ? fields[r.id.field] : "field_" + std::to_string(r.id.field);
            out += "        " + json_string(field) + ": {\"requested_delta_mv\": " + json_number(r.state.current)
                + ", \"initial_delta_mv\": " + json_number(r.state.stock)
                + ", \"estimated_base_mv\": " + json_number(r.estimated_base_mv)
                + ", \"stale\": " + (r.state.availability == Availability::transient ? "true" : "false")
                + ", \"detail\": " + json_string(r.state.detail) + "}";
        }
        if (!first_field) out += "\n      }\n    }";
    }
    out += first_rail ? "}\n}" : "\n  }\n}";
    return out;
}
void draw_mode_bar(const SessionSnapshot& s, bool& unlocked, std::vector<Command>& commands,
    bool classic, const std::function<void()>& i2c_gate, AdvancedViewState* view) {
    ImGui::Checkbox("Unlock controls", &unlocked);
    // Released 1.9.1 unlock tooltip.
    if (classic && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
        ImGui::SetTooltip("Writes are enabled while this is ticked; untick for read-only.\n"
            "I2C changes can persist through a reboot: use Reset or power off.");
    ImGui::SameLine();
    // Released 1.9.1 gate row: a horizontal group (item spacing between every
    // item) with a 24 px spacer between Unlock and XOC.
    if (classic) { ImGui::Dummy(ImVec2(classic_px(24), 0)); ImGui::SameLine(); }
    bool xoc = s.data.xoc_enabled;
    const bool xoc_flashing = view && view->xoc_gate_flash_until > ImGui::GetTime();
    if (xoc_flashing) { ImGui::PushStyleColor(ImGuiCol_FrameBg, ImVec4(0.95f,0.55f,0.08f,1)); ImGui::PushStyleColor(ImGuiCol_CheckMark, ImVec4(1,1,1,1)); }
    ImGui::BeginDisabled(!ready(s) || !s.data.xoc_known || (!unlocked && !xoc));
    if (ImGui::Checkbox("XOC", &xoc)) { Command c; c.operation = Command::Operation::set_xoc; c.enabled = xoc; commands.push_back(c); }
    ImGui::EndDisabled(); if (xoc_flashing) ImGui::PopStyleColor(2); hint("Expand software request ranges and enable experimental MSVDD/additional-memory requests. Firmware limits remain independent.");
    if (i2c_gate) { ImGui::SameLine(); i2c_gate(); }
    else if (classic) {
        ImGui::SameLine(); ImGui::BeginDisabled(); bool i2c = false;
        ImGui::Checkbox("I2C rail", &i2c); ImGui::EndDisabled();
        hint("Native I2C controller workflow pending.");
    }
    ImGui::SameLine(); bool rails = s.data.rail_mode_enabled;
    const bool flashing = view && view->rail_gate_flash_until > ImGui::GetTime();
    if (flashing) { ImGui::PushStyleColor(ImGuiCol_FrameBg, ImVec4(0.95f, 0.55f, 0.08f, 1)); ImGui::PushStyleColor(ImGuiCol_CheckMark, ImVec4(1, 1, 1, 1)); }
    ImGui::BeginDisabled(!ready(s) || !s.data.rail_mode_known || (!unlocked && !rails));
    if (ImGui::Checkbox("Rail limits", &rails)) { Command c; c.operation = Command::Operation::set_rail_mode; c.enabled = rails; commands.push_back(c); }
    ImGui::EndDisabled(); if (flashing) ImGui::PopStyleColor(2); hint("Enable explicit per-rail limit requests. Displayed absolute voltages are estimates from this adapter's captured reference.");
    if (classic) return;
    ImGui::SameLine();
    draw_mode_bar_risk(s, unlocked);
}
void draw_mode_bar_risk(const SessionSnapshot& s, bool unlocked) {
    const auto risk = session_risk(s.data);
    if (!unlocked && risk.band == RiskBand::green) ImGui::TextDisabled("LOCKED");
    else ImGui::TextColored(color(risk.tint.text), "%s / %u %s%s", risk.name.c_str(), risk.score,
        risk.score == 1 ? "pt" : "pts", risk.incomplete ? " / incomplete" : "");
    hint(risk.reason + ". XOC +1; Rail limits +2; I2C +3. Green 0, yellow 1, amber 2, red 3, crimson 4+. "
        "The score describes enabled control modes, not measured hardware safety. Locking edits does not release an active controller request.");
}
void draw_clock_voltage_offsets(const SessionSnapshot& s, bool unlocked, bool verbose,
    AdvancedViewState& state, std::vector<Command>& commands, OffsetGroup group,
    const std::vector<uint32_t>& replaced) {
    ensure_generation(s,state);
    const auto included = [group, &replaced](const AdvancedControl& c) {
        if (c.id.kind == AdvancedKind::clock_domain &&
            std::find(replaced.begin(), replaced.end(), c.id.domain) != replaced.end()) return false;
        if (group == OffsetGroup::all) return true;
        if (c.id.kind != AdvancedKind::clock_domain) return false;
        return group == OffsetGroup::additional_memory ? c.id.domain == 2 : c.id.domain != 2;
    };
    if (std::any_of(s.data.advanced_controls.begin(), s.data.advanced_controls.end(), included)) {
        ImGui::SeparatorText(group == OffsetGroup::all ? "Clock and voltage offsets" : "Clock offsets");
        if (table("advanced-offsets", verbose)) {
            for (const auto& c : s.data.advanced_controls) {
                if (!included(c)) continue;
                Command cmd; cmd.advanced = c.id;
                const char* name = c.id.kind == AdvancedKind::clock_domain && c.id.domain == 2 ? "Additional memory offset" : c.label.c_str();
                const bool xoc_required = c.id.kind == AdvancedKind::msvdd || (c.id.kind == AdvancedKind::clock_domain && c.id.domain == 2);
                row(1000 + static_cast<uint64_t>(c.id.kind) * 100 + c.id.domain, name, c.state, "Stock", cmd, s, unlocked, verbose, state, commands, c.binding, false,
                    !xoc_required || (s.data.xoc_known && s.data.xoc_enabled), xoc_required);
            }
            ImGui::EndTable();
        }
    }
}
void draw_advanced_controls(const SessionSnapshot& s, bool unlocked, bool verbose,
    AdvancedViewState& state, std::vector<Command>& commands, bool include_rails, bool include_offsets, const PolicyAnnotations* annotations) {
    ensure_generation(s,state);
    if (!state.notice.empty()) ImGui::TextWrapped("%s", state.notice.c_str());
    if (include_offsets) draw_clock_voltage_offsets(s,unlocked,verbose,state,commands);
    const auto section_flags = verbose ? ImGuiTreeNodeFlags_DefaultOpen : ImGuiTreeNodeFlags_None;
    // Classic nests these under Advanced > Power policies > Session controls,
    // so they take the third released 1.9.1 section level.
    const auto header = [&](std::optional<ClassicSection>& section, const char* label) {
        return verbose ? section.emplace(label, 2, nullptr, section_flags).open() : ImGui::CollapsingHeader(label, section_flags);
    };
    std::optional<ClassicSection> policies_section;
    if (!s.data.power_policies.empty() && header(policies_section, "Current and power policies")) {
        if (table("power-policies", verbose)) {
            for (const auto& p : s.data.power_policies) {
                policy_row(p,s,unlocked,verbose,state,commands,annotations);
            }
            ImGui::EndTable();
        }
    }
    policies_section.reset();
    std::optional<ClassicSection> rails_section;
    if (include_rails && !s.data.rail_controls.empty() && header(rails_section, "Rail limits")) {
        ImGui::TextDisabled("Absolute limits are estimates. Initial restores the captured request.");
        const char* names[] = {"Reliability", "Alt reliability", "Overvoltage", "Minimum voltage"};
        if (table("rail-limits", verbose)) {
            for (const auto& r : s.data.rail_controls) {
                if (r.id.field >= 4) continue;
                Command cmd; cmd.rail = r.id;
                const auto name = std::string(r.id.rail == 0 ? "NVVDD " : "MSVDD ") + names[r.id.field];
                auto shown = r.state;
                const double base = r.estimated_base_mv.value_or(0);
                if (r.estimated_base_mv) {
                    if (shown.current) *shown.current += base;
                    if (shown.stock) *shown.stock += base;
                    if (shown.low) *shown.low += base;
                    if (shown.high) *shown.high += base;
                    shown.unit = "~mV";
                    shown.detail += "; shown absolute value = captured base + stored delta";
                } else shown.detail += "; no base estimate, showing request delta only";
                const auto before = commands.size();
                row(3000 + r.id.rail * 4 + r.id.field, name.c_str(), shown, "Initial", cmd, s, unlocked, verbose, state, commands, r.binding, true, s.data.rail_mode_known && s.data.rail_mode_enabled);
                for (size_t i = before; i < commands.size(); ++i)
                    if (commands[i].operation == Command::Operation::apply) commands[i].value -= base;
            }
            ImGui::EndTable();
        }
    }
    rails_section.reset();
    if (ImGui::Button("Revert advanced drafts")) state.drafts.clear();
    ImGui::SameLine(); ImGui::BeginDisabled(!ready(s) || !unlocked || !s.data.undo_available);
    if (ImGui::Button("Undo hardware write")) { Command c; c.operation = Command::Operation::undo; commands.push_back(c); }
    ImGui::EndDisabled(); hint(s.data.undo_label);
}

ClockDomainLabel clock_domain_label(const SessionSnapshot& s, const AdvancedControl& c) {
    using Tone = ClockDomainLabel::Tone;
    const uint32_t control = c.id.domain;
    const std::string memory = "Additional Memory Clock Offset (MHz)";
    // Blackwell: the control block's own label, never paired with a private getter domain.
    if (s.data.clock_domain_requests_only)
        return {Tone::good, control == 2 ? memory : c.label + " requested offset (MHz)"};
    const auto pair = std::find_if(s.data.clock_domain_pairs.begin(), s.data.clock_domain_pairs.end(),
        [&](const dc_clkdom_pair& p) { return p.control == control; });
    if (pair == s.data.clock_domain_pairs.end()) {
        // Python DomainKnob fallbacks: "crossbar" for control 1, a bare index otherwise.
        const std::string fallback = control == 1 ? "crossbar" : "control domain " + std::to_string(control);
        return {Tone::dim, control == 2 ? memory : fallback + " offset (MHz)"};
    }
    std::string name, grade = "unnamed";
    if (s.data.clock_monitor) for (const auto& r : s.data.clock_monitor->rows) if (r.raw.domain == pair->domain) {
        name = std::string(r.raw.name, strnlen(r.raw.name, sizeof(r.raw.name)));
        const std::string g(r.raw.grade, strnlen(r.raw.grade, sizeof(r.raw.grade)));
        if (!g.empty()) grade = g;
        break;
    }
    const std::string shown = grade == "likely" ? name + "?" : !name.empty() ? name : "domain " + std::to_string(pair->domain);
    const Tone tone = grade == "confirmed" ? Tone::text : grade == "likely" ? Tone::warn : Tone::dim;
    return {tone, control == 2 ? memory : shown + " clock offset (MHz)"};
}
std::vector<const AdvancedControl*> classic_clock_domain_order(const LiveData& data) {
    static constexpr uint32_t order[] = {1, 3, 5, 4, 9};
    const auto rank = [](uint32_t control) -> size_t {
        if (control == 2) return std::size(order) + 1;
        const auto it = std::find(std::begin(order), std::end(order), control);
        return it == std::end(order) ? std::size(order) : static_cast<size_t>(it - std::begin(order));
    };
    std::vector<const AdvancedControl*> out;
    for (const auto& c : data.advanced_controls) if (c.id.kind == AdvancedKind::clock_domain) out.push_back(&c);
    std::stable_sort(out.begin(), out.end(), [&](const AdvancedControl* a, const AdvancedControl* b) { return rank(a->id.domain) < rank(b->id.domain); });
    return out;
}
PowerRequestStatus power_request_status(std::optional<double> requested, std::optional<double> enforced, std::optional<double> native,
    const std::string& feedback) {
    // Python works in whole mW: valid requests and enforced limits are 0 < mW < 2^32,
    // a native limit 0 <= mW < 0xFFFFFFFF.
    constexpr double ceiling_w = 4294967.296;
    const auto w = [](double value) { char b[48]{}; std::snprintf(b, sizeof(b), "%g", value); return std::string(b); };
    const bool valid = requested && std::isfinite(*requested) && *requested > 0 && *requested < ceiling_w;
    PowerRequestStatus out;
    out.text = valid ? "Configured request: " + w(*requested) + " W." : "Configured power request unverified: readback unavailable.";
    const bool valid_enforced = enforced && std::isfinite(*enforced) && *enforced > 0 && *enforced < ceiling_w;
    const bool different = valid_enforced && valid && *enforced != *requested;
    if (valid_enforced) {
        out.text += " NVML-reported enforced limit: " + w(*enforced) + " W.";
        if (different) out.text += *enforced < *requested ? " Below configured request; downstream limits may apply." : " Differs from configured request.";
    } else out.text += " NVML enforced limit unavailable.";
    const bool valid_native = native && std::isfinite(*native) && *native >= 0 && *native < ceiling_w - 0.001;
    const bool native_below = valid_native && valid && *native < *requested;
    if (valid_native) {
        out.text += " Native effective limit: " + w(*native) + " W.";
        if (native_below) out.text += " Native limit is below configured request; downstream limits may apply.";
    } else out.text += " Native effective limit unavailable; NVML readback does not verify it.";
    if (!feedback.empty()) out.text += " " + feedback;
    out.warn = !valid || different || native_below || !feedback.empty();
    return out;
}
void draw_clock_domain_controls(const SessionSnapshot& s, bool unlocked, bool classic,
    AdvancedViewState& state, std::vector<Command>& commands, bool in_open_table,
    const std::vector<uint32_t>& replaced) {
    ensure_generation(s, state);
    const auto controls = classic_clock_domain_order(s.data);
    if (controls.empty() || (!in_open_table && !table("classic-domain-offsets", classic))) return;
    for (const auto* control : controls) {
        const auto& c = *control;
        if (std::find(replaced.begin(), replaced.end(), c.id.domain) != replaced.end()) continue;
        Command cmd; cmd.advanced = c.id;
        const auto label = clock_domain_label(s, c);
        const ImVec4 tone = label.tone == ClockDomainLabel::Tone::warn ? classic_warn : label.tone == ClockDomainLabel::Tone::good ? classic_good
            : label.tone == ClockDomainLabel::Tone::dim ? classic_dim : classic ? classic_text_tone : ImGui::GetStyleColorVec4(ImGuiCol_Text);
        const bool xoc_required = c.id.domain == 2;
        const auto live = clock_domain_live(s, c);
        row(1000 + c.id.domain, label.text.c_str(), c.state, "Stock", cmd, s, unlocked, classic, state, commands, c.binding, false,
            !xoc_required || (s.data.xoc_known && s.data.xoc_enabled), xoc_required, nullptr, &live, "%.9g", &tone);
    }
    if (!in_open_table) ImGui::EndTable();
}

void draw_core_current_limit(const SessionSnapshot& s, bool unlocked, bool classic,
    AdvancedViewState& state, std::vector<Command>& commands, bool in_open_table) {
    ensure_generation(s, state);
    const auto message = [&](const char* text) {
        if (in_open_table) { ImGui::TableNextRow(); ImGui::TableNextColumn(); ImGui::AlignTextToFramePadding(); }
        ImGui::TextDisabled("%s", text);
    };
    const PowerPolicy* selected = nullptr;
    for (const auto& p : s.data.power_policies) if (p.core_current) {
        if (selected) { message("NVVDD current limit: unavailable (ambiguous binding)"); return; }
        selected = &p;
    }
    if (!selected) { message("NVVDD current limit: --"); return; }
    if (!in_open_table && !table("daily-core-current", classic)) return;
    Command cmd; cmd.power_policy = selected->id;
    const auto name = classic ? "Core current limit (" + selected->state.unit + ")" : "NVVDD current limit";
    // Classic follows Python's current-limit row: "Stock", a %.3f input and the
    // live current over its effective ceiling.
    const auto live = core_current_live(*selected);
    row(2000 + selected->id, name.c_str(), selected->state, classic ? "Stock" : "Default", cmd, s, unlocked, classic, state, commands, selected->binding,
        false, true, false, nullptr, classic ? &live : nullptr, classic ? "%.3f" : "%.9g");
    if (!in_open_table) ImGui::EndTable();
}

void draw_daily_rail_controls(const SessionSnapshot& s, bool unlocked, bool classic,
    AdvancedViewState& state, std::vector<Command>& commands, bool include_offsets, const RailReadouts* readouts) {
    ensure_generation(s, state);
    if (!table("daily-rail-controls", classic)) return;
    if (classic) rail_summary_rows(s, state, readouts);
    bool warned = false;
    for (const auto& r : s.data.rail_controls) {
        if (classic && r.id.rail == 1 && r.id.field < 4 && !warned) {
            // Python build_volt_limits_rows: one WARN note ahead of the rail-1 rows.
            warned = true;
            ImGui::TableNextRow(); ImGui::TableNextColumn(); ImGui::AlignTextToFramePadding(); ImGui::TextColored(classic_warn, "MSVDD");
            ImGui::TableNextColumn(); ImGui::AlignTextToFramePadding();
            ImGui::PushStyleColor(ImGuiCol_Text, classic_warn);
            ImGui::TextWrapped("Driver-reported rail; physical effect of a request must be checked on this board.");
            ImGui::PopStyleColor();
        }
        rail_row(r, s, unlocked, classic, state, commands);
    }
    for (const auto& c : s.data.advanced_controls) if (include_offsets && c.id.kind != AdvancedKind::clock_domain) {
        Command cmd; cmd.advanced = c.id;
        const auto live = rail_offset_live(s, c);
        const bool msvdd = c.id.kind == AdvancedKind::msvdd;
        // Python: "NVVDD offset (mV)" and a WARN "MSVDD requested offset (mV)".
        const std::string label = !classic ? c.label : msvdd ? "MSVDD requested offset (mV)" : "NVVDD offset (mV)";
        row(1000 + static_cast<uint64_t>(c.id.kind) * 100 + c.id.domain, label.c_str(), c.state,
            msvdd ? "Zero" : "Stock", cmd, s, unlocked, classic, state, commands, c.binding, false,
            !msvdd || (s.data.xoc_known && s.data.xoc_enabled), msvdd, nullptr, &live, "%.9g", classic && msvdd ? &classic_warn : nullptr);
    }
    ImGui::EndTable();
    if (s.data.rail_controls.empty()) ImGui::TextDisabled("Rail-limit requests unavailable");
}

namespace {
// Python nvbackend _policy_amount: three decimals, trailing zeros dropped.
std::string policy_amount(double value, const std::string& unit) {
    char buffer[48]{}; std::snprintf(buffer, sizeof(buffer), "%.3f", value);
    std::string text = buffer;
    while (!text.empty() && text.back() == '0') text.pop_back();
    if (!text.empty() && text.back() == '.') text.pop_back();
    return text + " " + (unit == "W" ? "W" : unit == "A" ? "A" : "?");
}
// Python power_policy_hint.
std::string policy_hint(const PowerPolicy& p) {
    char type[16] = "0x??";
    if (p.policy_type) std::snprintf(type, sizeof(type), "0x%02X", *p.policy_type);
    return "policy " + std::to_string(p.id) + ", ch " + (p.channel ? std::to_string(*p.channel) : std::string("?")) + ", " + type + ", "
        + (p.state.unit == "W" ? "W" : p.state.unit == "A" ? "A" : "?");
}
// Python POLICY_COLS and NOTE_W: a wider label (note) and live (per-wire) column.
constexpr float policy_cols[6]{320, 200, 66, 180, 78, 74};
constexpr float policy_note_width = 150;
const char* const policy_note_choices[]{"-", "PCIE 8pin", "PCIE", "EPS 8pin", "12VHPWR/12V-2x6", "others"};
// Python WIRE_BANDS / SLOT_BANDS colours: TEXT, WARN, RISK_RED[2], RISK_CRIMSON[1].
ImVec4 policy_band_colour(int band) {
    switch (band) {
    case 1: return classic_warn;
    case 2: return {255 / 255.0f, 120 / 255.0f, 120 / 255.0f, 1};
    case 3: return {220 / 255.0f, 20 / 255.0f, 60 / 255.0f, 1};
    default: return {230 / 255.0f, 232 / 255.0f, 236 / 255.0f, 1};
    }
}
}
std::string power_policy_readonly_reason(const PowerPolicy& p) {
    if (p.state.detail == "Board request is controlled separately") return "set with Power limit (W) on the Control tab";
    // The core current limit has its slider in the Control tab's Limits table.
    if (p.core_current) return "set with its current-limit slider on the Control tab";
    if (p.state.writable) return {};
    if (p.state.low && p.state.high && *p.state.low >= *p.state.high)
        return "no range to write (" + policy_amount(*p.state.low, p.state.unit) + " .. " + policy_amount(*p.state.high, p.state.unit) + ")";
    return "read-only: " + (p.state.detail.empty() ? std::string("not writable on this adapter") : p.state.detail);
}
PowerPolicyLive power_policy_live(const PowerPolicy& p, const std::string& note) {
    PowerPolicyLive live;
    // Python refresh_power_policies: a row the latest read did not return.
    if (p.state.availability == Availability::transient) { live.text = "unavailable"; return live; }
    // Held only while the card still holds the pinned request: another tool's
    // board-limit write recomputes it, and only this session's writes put it back.
    live.held = p.pinned && p.state.current && *p.pinned == *p.state.current;
    const std::string mark = live.held ? " *" : "";
    // Python: a milliwatt row has no row_scale, so it shows the reading over its
    // effective limit in plain text, never split per wire.
    if (p.state.unit == "W") {
        if (p.state.availability != Availability::available || !p.observed_watts || !p.limit_watts
            || !std::isfinite(*p.observed_watts) || !std::isfinite(*p.limit_watts)) return live;
        live.known = true;
        live.text = policy_amount(*p.observed_watts, "W") + "\n\xE2\x89\xA4" + policy_amount(*p.limit_watts, "W") + mark;
        return live;
    }
    if (p.state.unit != "A" || p.state.availability != Availability::available || !p.observed_amps || !p.limit_amps
        || !std::isfinite(*p.observed_amps) || !std::isfinite(*p.limit_amps)) return live;
    live.known = true;
    const auto now = policy_amount(*p.observed_amps, "A"), limit = policy_amount(*p.limit_amps, "A");
    const auto scale = policy_current_display(note, "mA", p.observed_amps);
    if (!scale) { live.text = now + "\n\xE2\x89\xA4" + limit + mark; return live; }
    live.band = static_cast<int>(scale->band);
    if (!scale->even_split) { live.text = now + "\n\xE2\x89\xA4" + limit + mark; return live; }
    char buffer[160]{};
    std::snprintf(buffer, sizeof(buffer), "%s (%.2f/wire)\n\xE2\x89\xA4%s (%.2f/wire)%s", now.c_str(), scale->amps, limit.c_str(), *p.limit_amps / scale->divisor, mark.c_str());
    live.text = buffer;
    return live;
}
namespace {
// Python refresh_power_policies: the band of a scaled row, else GOOD while held, else TEXT.
ImVec4 policy_live_colour(const PowerPolicyLive& live) {
    if (live.band >= 0) return policy_band_colour(live.band);
    return live.held ? classic_good : policy_band_colour(0);
}
}
std::string power_policies_unavailable_text(const SessionSnapshot& s) {
    if (!s.data.connected) return "Power policies unavailable: no GPU session is connected.";
    // The policy table is read with the power request; a failed read leaves its
    // reason on that control.
    const auto& power = s.data.controls[static_cast<size_t>(ControlId::power)];
    if (power.availability != Availability::available && !power.detail.empty())
        return "Power policies unavailable: " + power.detail;
    return "Power policies unavailable: the driver's power-policy table was not read.";
}
PowerPolicySectionOutput draw_power_policy_controls(const SessionSnapshot& s, bool unlocked,
    AdvancedViewState& state, std::vector<Command>& commands, PolicyAnnotations* annotations) {
    ensure_generation(s, state);
    PowerPolicySectionOutput out;
    if (!state.notice.empty()) ImGui::TextWrapped("%s", state.notice.c_str());
    if (s.data.power_policies.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, classic_dim);
        ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + classic_px(900));
        ImGui::AlignTextToFramePadding();
        ImGui::TextUnformatted(power_policies_unavailable_text(s).c_str());
        ImGui::PopTextWrapPos(); ImGui::PopStyleColor();
        return out;
    }
    ImGui::AlignTextToFramePadding();
    ImGui::TextColored(classic_warn, "Beware of your PSU's rating as you change power limit.");
    ImGui::SameLine();
    ImGui::BeginDisabled(!ready(s) || !unlocked);
    if (ImGui::Button("Max all", ImVec2(classic_px(110), 0))) out.max_all = true;
    ImGui::EndDisabled();
    hint("Opens the Policy workspace with every policy selected and previews each one's maximum request. "
        "Nothing is written until you review the preview and select Apply reviewed maxima. The board power limit is set on the Control tab.");
    ImGui::PushStyleColor(ImGuiCol_Text, classic_dim);
    ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + classic_px(900));
    // Apply pins the request (set_policy with pin) and a board power-limit write
    // re-applies this session's pins (core PowerLimit::reapply), as in 1.9.1; a held
    // request's live cell ends with " *".
    // An add_text item: a frame-height line (classic_section.h classic_text).
    ImGui::AlignTextToFramePadding();
    ImGui::TextUnformatted("Changing the board power limit can recalculate other requests. Values you "
        "set here are marked * and re-applied if recalculated. This preserves the "
        "request; the effective limit can still be lower because of other votes. "
        "Stock hands a policy back to the driver. Note a "
        "channel by its connector, or 'others' for your own words; a current row on a "
        "connector shows its current per 12 V wire, coloured: to 5 A white, 10 A "
        "yellow, 13 A red, above that crimson. The slot (PCIE) is judged as a whole: "
        "to 5.5 A white, 6.25 A yellow, 7 A red, above that crimson.");
    ImGui::PopTextWrapPos(); ImGui::PopStyleColor();
    const ScopedTarget target{s.data.identity.uuid, s.selected_slot};
    ScopedSnapshot rows; rows.target = target;
    for (const auto& p : s.data.power_policies) {
        ScopedCurrent current; current.key = ScopedKey::policy_request(p.id); current.binding = p.binding;
        current.channel = p.channel; current.policy_type = p.policy_type; current.fresh = p.state.availability == Availability::available;
        rows.entries.push_back(std::move(current));
    }
    if (ImGui::BeginTable("power-policies", 6, ImGuiTableFlags_SizingFixedFit | ImGuiTableFlags_NoHostExtendX)) {
        for (const float width : policy_cols) ImGui::TableSetupColumn(nullptr, ImGuiTableColumnFlags_WidthFixed, classic_px(width));
        for (size_t index = 0; index < s.data.power_policies.size(); ++index) {
            const auto& p = s.data.power_policies[index];
            const auto& metadata = rows.entries[index];
            const auto note = annotations ? annotations->get(target, metadata) : std::string{};
            const auto set_note = [&](const std::string& value) {
                if (!annotations) return;
                try { annotations->set_channel(rows, metadata.key, value); out.annotations_changed = true; state.policy_note_error.clear(); }
                catch (const std::exception& e) { state.policy_note_error = std::string("Policy note not changed: ") + e.what(); }
            };
            // Python note_cell: the channel note's choice list, then the policy's
            // description - or, for "others", the text box for the note.
            const auto note_cell = [&] {
                const bool known = std::find(std::begin(policy_note_choices) + 1, std::end(policy_note_choices) - 1, note) != std::end(policy_note_choices) - 1;
                const bool custom = state.policy_custom_note[p.id] || (!note.empty() && !known);
                const std::string choice = custom ? "others" : note.empty() ? "-" : note;
                ImGui::BeginDisabled(!annotations || !metadata.fresh || !p.channel || !p.policy_type || target.uuid.empty() || target.slot.empty());
                ImGui::SetNextItemWidth(classic_px(policy_note_width));
                if (ImGui::BeginCombo("##note", choice.c_str())) {
                    for (const char* option : policy_note_choices) if (ImGui::Selectable(option, choice == option)) {
                        const std::string picked = option;
                        state.policy_custom_note[p.id] = picked == "others";
                        if (picked != "others") { state.policy_note_edit.erase(p.id); set_note(picked == "-" ? std::string{} : picked); }
                        else if (known) set_note({});
                    }
                    ImGui::EndCombo();
                }
                ImGui::EndDisabled();
                hint("A note belongs to the channel: its power and current rows share it. Display metadata only; nothing is written to the GPU.");
                ImGui::SameLine();
                const auto description = policy_hint(p);
                if (!custom) { ImGui::TextColored(classic_dim, "%s", description.c_str()); return; }
                // While the box is being typed in it keeps its own text; otherwise it shows the note.
                const auto edited = state.policy_note_edit.find(p.id);
                const std::string text = edited != state.policy_note_edit.end() ? edited->second : known ? std::string{} : note;
                char buffer[321]{}; std::snprintf(buffer, sizeof(buffer), "%s", text.c_str());
                ImGui::SetNextItemWidth(classic_px(policy_cols[0] - policy_note_width - 18));
                ImGui::BeginDisabled(!annotations || !metadata.fresh || !p.channel || !p.policy_type || target.uuid.empty() || target.slot.empty());
                if (ImGui::InputTextWithHint("##name", description.c_str(), buffer, sizeof(buffer))) {
                    try { set_note(normalize_policy_label(buffer, true)); }
                    catch (const std::exception& e) { state.policy_note_error = std::string("Policy note not changed: ") + e.what(); }
                }
                if (ImGui::IsItemActive()) state.policy_note_edit[p.id] = buffer; else state.policy_note_edit.erase(p.id);
                ImGui::EndDisabled();
            };
            ImGui::PushID(static_cast<int>(p.id));
            const auto reason = power_policy_readonly_reason(p);
            const auto reading = power_policy_live(p, note);
            if (!reason.empty()) {
                // Python power_policy_row, read-only: note, why, request, live.
                ImGui::TableNextRow(); ImGui::TableNextColumn(); ImGui::AlignTextToFramePadding(); note_cell();
                ImGui::TableNextColumn(); ImGui::AlignTextToFramePadding(); ImGui::TextColored(classic_dim, "%s", reason.c_str()); hint(reason);
                ImGui::TableNextColumn(); ImGui::AlignTextToFramePadding();
                if (p.state.current) ImGui::TextColored(classic_dim, "%s", policy_amount(*p.state.current, p.state.unit).c_str());
                else ImGui::TextDisabled("--");
                ImGui::TableNextColumn(); ImGui::AlignTextToFramePadding(); push_classic_mono();
                // Python refresh_power_policies colours a read-only row's reading as
                // a writable row's: TEXT (GOOD while held), or the band of a current
                // row on a connector; "--" or "unavailable" DIM.
                if (reading.known) ImGui::TextColored(policy_live_colour(reading), "%s", reading.text.c_str()); else ImGui::TextDisabled("%s", reading.text.c_str());
                ImGui::PopFont();
                if (!reading.known) hint("This read reports no live value for this policy.");
                ImGui::PopID();
                continue;
            }
            LiveCell live;
            if (reading.known) { live.text = reading.text; live.tone = policy_live_colour(reading); }
            else { live.text = reading.text; live.dim = true; }
            if (!reading.known) live.detail = p.state.unit == "A" ? "This read reports no live current for this policy."
                : "This read reports no live value for this power policy.";
            if (reading.known) live.detail = "First line: reported value.\nSecond line: effective limit.";
            // Python refresh_power_policies: a current row on a connector colours its value
            // box with the band of the staged value, so raising it shows where it lands.
            const auto staged_tone = [&](double staged) -> std::optional<ImVec4> {
                if (p.state.unit != "A") return std::nullopt;
                const auto scale = policy_current_display(note, "mA", staged);
                if (!scale) return std::nullopt;
                return policy_band_colour(static_cast<int>(scale->band));
            };
            Command cmd; cmd.power_policy = p.id;
            row(2000 + p.id, p.label.c_str(), p.state, "Stock", cmd, s, unlocked, true, state, commands, p.binding, false, true, false,
                nullptr, &live, "%.3f", nullptr, false, note_cell, staged_tone);
            ImGui::PopID();
        }
        ImGui::EndTable();
    }
    if (!state.policy_note_error.empty()) ImGui::TextColored(classic_warn, "%s", state.policy_note_error.c_str());
    // Native extras: the reviewed workspace (labels import, selected maxima) and
    // the draft reset the shared knob rows use.
    if (ImGui::Button("Policy workspace...")) out.open_workspace = true;
    hint("Import Python labels, select policies and preview their maxima before an explicit Apply.");
    ImGui::SameLine();
    if (ImGui::Button("Revert advanced drafts")) state.drafts.clear();
    return out;
}
}
