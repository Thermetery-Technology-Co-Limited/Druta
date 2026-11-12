/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "advanced_view.h"
#include "session.h"
#include "imgui.h"
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
bool table(const char* id) {
    if (!ImGui::BeginTable(id, 4, ImGuiTableFlags_SizingStretchProp)) return false;
    ImGui::TableSetupColumn("Control", ImGuiTableColumnFlags_WidthStretch, 1.3f);
    ImGui::TableSetupColumn("Request", ImGuiTableColumnFlags_WidthStretch, 1.1f);
    ImGui::TableSetupColumn("Readback", ImGuiTableColumnFlags_WidthFixed, u(122));
    ImGui::TableSetupColumn("Action", ImGuiTableColumnFlags_WidthFixed, u(155));
    return true;
}
void row(uint64_t key, const char* label, const ControlState& c, const char* stock_label,
    Command action, const SessionSnapshot& snapshot, bool unlocked, bool verbose,
    AdvancedViewState& state, std::vector<Command>& commands, const std::string& binding) {
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
    ImGui::TextUnformatted(label); hint(c.detail);
    ImGui::TableNextColumn();
    const float width = ImGui::GetContentRegionAvail().x;
    const bool slider = c.low && c.high && *c.low < *c.high && width > u(190);
    bool editing = false;
    if (slider) {
        ImGui::SetNextItemWidth(width - u(92) - ImGui::GetStyle().ItemSpacing.x);
        if (ImGui::SliderScalar("##slider", ImGuiDataType_Double, &d.value, &*c.low, &*c.high, "%.6g", ImGuiSliderFlags_NoInput)) {
            const double step = std::max(c.step.value_or(1.0), c.unit == "mV" || c.unit == "MHz" ? 1.0 : 0.001);
            d.value = std::max(*c.low, std::min(*c.high, std::floor(d.value / step + 0.5) * step));
            d.dirty = true; d.initialized = true;
        }
        editing = ImGui::IsItemActive(); ImGui::SameLine();
    }
    ImGui::SetNextItemWidth(slider ? u(92) : -1);
    if (ImGui::InputDouble("##value", &d.value, 0, 0, "%.9g")) { d.dirty = true; d.initialized = true; }
    d.editing = editing || ImGui::IsItemActive();
    hint("Draft only. Apply sends this selected request; polling preserves unfinished edits.");
    ImGui::TableNextColumn(); ImGui::AlignTextToFramePadding();
    if (c.current) ImGui::Text("%.6g %s%s", *c.current, c.unit.c_str(), c.availability == Availability::transient ? " *" : "");
    else ImGui::TextDisabled("-- %s", c.unit.c_str());
    hint(c.detail);
    ImGui::TableNextColumn();
    ImGui::BeginDisabled(!ready(snapshot) || !unlocked || !c.writable || !d.initialized || !std::isfinite(d.value));
    if (ImGui::Button("Apply", ImVec2(u(64), 0))) { action.operation = Command::Operation::apply; action.value = d.value; commands.push_back(action); }
    ImGui::EndDisabled(); hint(c.detail);
    ImGui::SameLine();
    ImGui::BeginDisabled(!ready(snapshot) || !unlocked || !c.stock_writable);
    if (ImGui::Button(stock_label, ImVec2(u(80), 0))) { action.operation = Command::Operation::stock; commands.push_back(action); }
    ImGui::EndDisabled();
    if (verbose || c.availability == Availability::transient) {
        ImGui::TableNextRow(); ImGui::TableNextColumn(); ImGui::TextDisabled("%s", c.detail.c_str());
    }
    ImGui::PopID();
}
}
RiskAssessment session_risk(const LiveData& data) {
    auto flag = [](bool known, bool enabled) { return !known ? RiskFlag::unknown : enabled ? RiskFlag::active : RiskFlag::inactive; };
    RiskGates gates;
    gates.xoc_enabled = flag(data.xoc_known, data.xoc_enabled);
    gates.rail_limits_enabled = flag(data.rail_mode_known, data.rail_mode_enabled);
    gates.rail_limits_writable = flag(data.rail_writable_known, data.rail_writable);
    // The native UI does not yet arm a direct I2C writer. No bus probe for coloring.
    gates.i2c_enabled = gates.i2c_controller_writable = RiskFlag::inactive;
    return assess_risk(gates);
}
void draw_mode_bar(const SessionSnapshot& s, bool& unlocked, std::vector<Command>& commands) {
    ImGui::Checkbox("Unlock controls", &unlocked); ImGui::SameLine();
    bool xoc = s.data.xoc_enabled;
    ImGui::BeginDisabled(!ready(s) || !s.data.xoc_known || (!unlocked && !xoc));
    if (ImGui::Checkbox("XOC", &xoc)) { Command c; c.operation = Command::Operation::set_xoc; c.enabled = xoc; commands.push_back(c); }
    ImGui::EndDisabled(); hint("Expand software request ranges and enable experimental MSVDD/additional-memory requests. Firmware limits remain independent.");
    ImGui::SameLine(); bool rails = s.data.rail_mode_enabled;
    ImGui::BeginDisabled(!ready(s) || !s.data.rail_mode_known || (!unlocked && !rails));
    if (ImGui::Checkbox("Rail limits", &rails)) { Command c; c.operation = Command::Operation::set_rail_mode; c.enabled = rails; commands.push_back(c); }
    ImGui::EndDisabled(); hint("Enable explicit per-rail limit requests. Displayed absolute voltages are estimates from this adapter's captured reference.");
    const auto risk = session_risk(s.data);
    if (risk.band != RiskBand::stock) {
        ImGui::SameLine(); ImGui::TextColored(color(risk.tint.text), "%s%s", risk.name.c_str(), risk.incomplete ? " / incomplete" : "");
        hint(risk.reason);
    }
}
void draw_advanced_controls(const SessionSnapshot& s, bool unlocked, bool verbose,
    AdvancedViewState& state, std::vector<Command>& commands) {
    if (state.generation != s.generation) { state = {}; state.generation = s.generation; }
    if (!state.notice.empty()) ImGui::TextWrapped("%s", state.notice.c_str());
    if (!s.data.advanced_controls.empty()) {
        ImGui::SeparatorText("Clock and voltage offsets");
        if (table("advanced-offsets")) {
            for (const auto& c : s.data.advanced_controls) {
                Command cmd; cmd.advanced = c.id;
                const char* name = c.id.kind == AdvancedKind::clock_domain && c.id.domain == 2 ? "Additional memory offset" : c.label.c_str();
                row(1000 + static_cast<uint64_t>(c.id.kind) * 100 + c.id.domain, name, c.state, "Stock", cmd, s, unlocked, verbose, state, commands, c.binding);
            }
            ImGui::EndTable();
        }
    }
    const auto section_flags = verbose ? ImGuiTreeNodeFlags_DefaultOpen : ImGuiTreeNodeFlags_None;
    if (!s.data.power_policies.empty() && ImGui::CollapsingHeader("Current and power policies", section_flags)) {
        if (table("power-policies")) {
            for (const auto& p : s.data.power_policies) {
                Command cmd; cmd.power_policy = p.id;
                row(2000 + p.id, p.label.c_str(), p.state, "Default", cmd, s, unlocked, verbose, state, commands, p.binding);
            }
            ImGui::EndTable();
        }
    }
    if (!s.data.rail_controls.empty() && ImGui::CollapsingHeader("Rail limits", section_flags)) {
        ImGui::TextDisabled("Absolute limits are estimates. Initial restores the captured request.");
        const char* names[] = {"Reliability", "Alt reliability", "Overvoltage", "Minimum voltage"};
        if (table("rail-limits")) {
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
                row(3000 + r.id.rail * 4 + r.id.field, name.c_str(), shown, "Initial", cmd, s, unlocked, verbose, state, commands, r.binding);
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
}
