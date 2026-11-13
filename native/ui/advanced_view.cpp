/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "advanced_view.h"
#include "session.h"
#include "imgui.h"
#include "classic_layout.h"
#include "policy_workspace.h"
#include <algorithm>
#include <cmath>

namespace druta::ui {
namespace {
float u(float v) { return v * ImGui::GetFontSize() / 18.0f; }
ImVec4 color(RiskColor c) { return {c.r / 255.0f, c.g / 255.0f, c.b / 255.0f, 1}; }
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
void row(uint64_t key, const char* label, const ControlState& c, const char* stock_label,
    Command action, const SessionSnapshot& snapshot, bool unlocked, bool verbose,
    AdvancedViewState& state, std::vector<Command>& commands, const std::string& binding, bool keep_slider = false, bool gate = true, bool xoc_gate = false, const PolicyCurrentDisplay* observed = nullptr) {
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
    ImGui::TableNextRow(); ImGui::TableNextColumn(); ImGui::AlignTextToFramePadding();
    if (verbose) ImGui::TextWrapped("%s", label); else ImGui::TextUnformatted(label);
    hint(c.detail);
    ImGui::TableNextColumn();
    const float width = ImGui::GetContentRegionAvail().x;
    const bool slider = c.low && c.high && std::isfinite(*c.low) && std::isfinite(*c.high)
        && *c.low < *c.high && (keep_slider || verbose || width > u(190));
    const bool stacked = slider && !verbose && width <= u(190);
    bool editing = false;
    const auto flash_gate = [&] { if (!gate && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled) && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) (xoc_gate ? state.xoc_gate_flash_until : state.rail_gate_flash_until) = ImGui::GetTime() + 0.8; };
    ImGui::BeginDisabled(!gate);
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
    if (ImGui::InputDouble("##value", &d.value, 0, 0, "%.9g")) { d.dirty = true; d.initialized = true; }
    flash_gate(); d.editing = editing || ImGui::IsItemActive();
    ImGui::EndDisabled();
    hint("Draft only. Apply sends this selected request; polling preserves unfinished edits.");
    ImGui::TableNextColumn(); ImGui::AlignTextToFramePadding();
    if (verbose) push_classic_mono();
    if (c.current && verbose) ImGui::TextWrapped("%.6g %s%s", *c.current, c.unit.c_str(), c.availability == Availability::transient ? " *" : "");
    else if (c.current) ImGui::Text("%.6g %s%s", *c.current, c.unit.c_str(), c.availability == Availability::transient ? " *" : "");
    else ImGui::TextDisabled("-- %s", c.unit.c_str());
    if (verbose) ImGui::PopFont();
    if(observed) {
        const auto band=observed->band;
        const ImVec4 tint=band==PolicyCurrentBand::white?ImVec4(1,1,1,1):band==PolicyCurrentBand::yellow?ImVec4(1,0.9f,0,1):band==PolicyCurrentBand::red?ImVec4(1,0.2f,0.2f,1):ImVec4(0.86f,0.08f,0.24f,1);
        ImGui::TextColored(tint,"%.3f A%s",observed->amps,observed->even_split?" / wire":" slot");
        hint(observed->even_split?"Reported channel current divided evenly among the selected connector's 12 V wires; actual sharing is not measured.":"Reported PCIe slot current.");
    }
    hint(c.detail);
    if (verbose) hint("Configured request, not an observed physical clock or rail measurement.");
    ImGui::TableNextColumn();
    ImGui::BeginDisabled(!ready(snapshot) || !unlocked || !c.writable || !gate || !d.initialized || !std::isfinite(d.value));
    if (ImGui::Button("Apply", ImVec2(verbose ? -1 : u(64), 0))) { action.operation = Command::Operation::apply; action.value = d.value; commands.push_back(action); }
    flash_gate(); ImGui::EndDisabled(); hint(c.detail);
    if (verbose) ImGui::TableNextColumn(); else ImGui::SameLine();
    ImGui::BeginDisabled(!ready(snapshot) || !unlocked || (!gate && !xoc_gate) || !c.stock_writable);
    if (ImGui::Button(stock_label, ImVec2(verbose ? -1 : u(80), 0))) { action.operation = Command::Operation::stock; commands.push_back(action); }
    if (!xoc_gate) flash_gate();
    ImGui::EndDisabled();
    hint(c.detail);
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
    if (state.generation != s.generation) { state = {}; state.generation = s.generation; }
}
void rail_row(const RailControl& r, const SessionSnapshot& s, bool unlocked, bool classic,
    AdvancedViewState& state, std::vector<Command>& commands) {
    if (r.id.field >= 4) return;
    const char* names[] = {"reliability", "alt-reliability", "overvoltage", "min"};
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
    row(3000 + r.id.rail * 4 + r.id.field, name.c_str(), shown, "Initial", cmd, s, unlocked, classic, state, commands, r.binding, true, s.data.rail_mode_known && s.data.rail_mode_enabled);
    for (size_t i = before; i < commands.size(); ++i)
        if (commands[i].operation == Command::Operation::apply) commands[i].value -= base;
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
void draw_mode_bar(const SessionSnapshot& s, bool& unlocked, std::vector<Command>& commands,
    bool classic, const std::function<void()>& i2c_gate, AdvancedViewState* view) {
    ImGui::Checkbox("Unlock controls", &unlocked); ImGui::SameLine();
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
    const auto risk = session_risk(s.data);
    ImGui::SameLine();
    if (!unlocked && risk.band == RiskBand::green) ImGui::TextDisabled("LOCKED");
    else ImGui::TextColored(color(risk.tint.text), "%s / %u %s%s", risk.name.c_str(), risk.score,
        risk.score == 1 ? "pt" : "pts", risk.incomplete ? " / incomplete" : "");
    hint(risk.reason + ". XOC +1; Rail limits +2; I2C +3. Green 0, yellow 1, amber 2, red 3, crimson 4+. "
        "The score describes enabled control modes, not measured hardware safety. Locking edits does not release an active controller request.");
}
void draw_clock_voltage_offsets(const SessionSnapshot& s, bool unlocked, bool verbose,
    AdvancedViewState& state, std::vector<Command>& commands, OffsetGroup group) {
    ensure_generation(s,state);
    const auto included = [group](const AdvancedControl& c) {
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
    if (!s.data.power_policies.empty() && ImGui::CollapsingHeader("Current and power policies", section_flags)) {
        if (table("power-policies", verbose)) {
            for (const auto& p : s.data.power_policies) {
                policy_row(p,s,unlocked,verbose,state,commands,annotations);
            }
            ImGui::EndTable();
        }
    }
    if (include_rails && !s.data.rail_controls.empty() && ImGui::CollapsingHeader("Rail limits", section_flags)) {
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
    if (ImGui::Button("Revert advanced drafts")) state.drafts.clear();
    ImGui::SameLine(); ImGui::BeginDisabled(!ready(s) || !unlocked || !s.data.undo_available);
    if (ImGui::Button("Undo hardware write")) { Command c; c.operation = Command::Operation::undo; commands.push_back(c); }
    ImGui::EndDisabled(); hint(s.data.undo_label);
}

void draw_clock_domain_controls(const SessionSnapshot& s, bool unlocked, bool classic,
    AdvancedViewState& state, std::vector<Command>& commands) {
    ensure_generation(s, state);
    bool any = false;
    for (const auto& c : s.data.advanced_controls) any = any || c.id.kind == AdvancedKind::clock_domain;
    if (!any || !table("classic-domain-offsets", classic)) return;
    for (const auto& c : s.data.advanced_controls) if (c.id.kind == AdvancedKind::clock_domain) {
        Command cmd; cmd.advanced = c.id;
        const auto name = c.id.domain == 2 ? "Additional Memory Clock Offset (MHz)" : c.label;
        const bool xoc_required = c.id.domain == 2;
        row(1000 + c.id.domain, name.c_str(), c.state, "Stock", cmd, s, unlocked, classic, state, commands, c.binding, false,
            !xoc_required || (s.data.xoc_known && s.data.xoc_enabled), xoc_required);
    }
    ImGui::EndTable();
}

void draw_core_current_limit(const SessionSnapshot& s, bool unlocked, bool classic,
    AdvancedViewState& state, std::vector<Command>& commands) {
    ensure_generation(s, state);
    const PowerPolicy* selected = nullptr;
    for (const auto& p : s.data.power_policies) if (p.core_current) {
        if (selected) { ImGui::TextDisabled("NVVDD current limit: unavailable (ambiguous binding)"); return; }
        selected = &p;
    }
    if (!selected) { ImGui::TextDisabled("NVVDD current limit: --"); return; }
    if (!table("daily-core-current", classic)) return;
    Command cmd; cmd.power_policy = selected->id;
    const auto name = classic ? "Core current limit (" + selected->state.unit + ")" : "NVVDD current limit";
    row(2000 + selected->id, name.c_str(), selected->state, "Default", cmd, s, unlocked, classic, state, commands, selected->binding);
    ImGui::EndTable();
}

void draw_daily_rail_controls(const SessionSnapshot& s, bool unlocked, bool classic,
    AdvancedViewState& state, std::vector<Command>& commands, bool include_offsets) {
    ensure_generation(s, state);
    if (!table("daily-rail-controls", classic)) return;
    for (const auto& r : s.data.rail_controls) rail_row(r, s, unlocked, classic, state, commands);
    for (const auto& c : s.data.advanced_controls) if (include_offsets && c.id.kind != AdvancedKind::clock_domain) {
        Command cmd; cmd.advanced = c.id;
        row(1000 + static_cast<uint64_t>(c.id.kind) * 100 + c.id.domain, c.label.c_str(), c.state,
            c.id.kind == AdvancedKind::msvdd ? "Zero" : "Stock", cmd, s, unlocked, classic, state, commands, c.binding, false,
            c.id.kind != AdvancedKind::msvdd || (s.data.xoc_known && s.data.xoc_enabled), c.id.kind == AdvancedKind::msvdd);
    }
    ImGui::EndTable();
    if (s.data.rail_controls.empty()) ImGui::TextDisabled("Rail-limit requests unavailable");
}

void draw_power_policy_controls(const SessionSnapshot& s, bool unlocked, bool classic,
    AdvancedViewState& state, std::vector<Command>& commands,const PolicyAnnotations* annotations) {
    ensure_generation(s, state);
    if (!table("power-policies", classic)) return;
    for (const auto& p : s.data.power_policies) {
        policy_row(p,s,unlocked,classic,state,commands,annotations);
    }
    ImGui::EndTable();
}
}
