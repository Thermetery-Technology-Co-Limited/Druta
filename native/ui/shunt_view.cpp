/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "shunt_view.h"
#include <imgui.h>
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <utility>

namespace druta::ui {
namespace {
bool known_kind(const std::string& kind) { return kind == "slot" || kind == "pin6" || kind == "pin8"; }
const char* rail_label(const std::string& kind) {
    return kind == "slot" ? "PCIe slot" : kind == "pin6" ? "6-pin" : kind == "pin8" ? "8-pin" : "Unknown rail";
}
bool positive(double value) { return std::isfinite(value) && value > 0; }
bool can_save(const ShuntViewState& state) {
    if (!state.config.load_error.empty() || state.config.rails.empty()) return false;
    for (const auto& rail : state.config.rails) {
        if (!known_kind(rail.kind)) continue; // preserved by the model, not interpreted here
        if (!positive(rail.orig) || !positive(rail.mod) || !positive(rail.orig / rail.mod)) return false;
    }
    return true;
}
void changed(ShuntViewState& state) {
    state.correction = shunt_correction(state.config.rails);
    state.dirty = true;
    state.status.clear();
}
const char* correction_label(const ShuntCorrection& correction) {
    if (!correction.valid) return "Correction unavailable";
    if (!correction.active) return "No mod: driver-reported power";
    return correction.exact ? "Uniform correction" : "Mixed-rail ESTIMATE";
}
void power_line(const char* label, std::optional<double> raw, const ShuntCorrection& correction) {
    if (!raw || !std::isfinite(*raw)) { ImGui::Text("%s: unavailable", label); return; }
    const auto corrected = correction.apply(raw);
    if (!corrected) ImGui::Text("%s: reported %.1f W | correction unavailable", label, *raw);
    else if (!correction.active) ImGui::Text("%s: reported %.1f W", label, *raw);
    else ImGui::Text("%s: reported %.1f W | %s %.1f W", label, *raw,
        correction.exact ? "corrected" : "ESTIMATE", *corrected);
}
}
ShuntViewState::ShuntViewState() {
    config.rails = shunt_default_rails();
    correction = shunt_correction(config.rails);
}
void set_shunt_config(ShuntViewState& state, ShuntConfig config) {
    state.config = std::move(config);
    state.correction = shunt_correction(state.config.rails);
    state.dirty = false;
    state.save_requested = false;
    state.status.clear();
}
bool edit_shunt_rail(ShuntViewState& state, std::size_t index, double original, double effective) {
    if (index >= state.config.rails.size()) return false;
    auto& rail = state.config.rails[index];
    if (!known_kind(rail.kind)) return false;
    if (rail.orig == original && rail.mod == effective) return false;
    rail.orig = original; rail.mod = effective; changed(state); return true;
}
bool add_shunt_rail(ShuntViewState& state, const std::string& kind) {
    if (!known_kind(kind)) return false;
    ShuntRail rail; rail.kind = kind;
    state.config.rails.push_back(std::move(rail)); changed(state); return true;
}
bool remove_shunt_rail(ShuntViewState& state, std::size_t index) {
    if (index >= state.config.rails.size()) return false;
    if (!known_kind(state.config.rails[index].kind)) return false;
    state.config.rails.erase(state.config.rails.begin() + static_cast<std::ptrdiff_t>(index));
    changed(state); return true;
}
void reset_shunt_no_mod(ShuntViewState& state) {
    // Unknown fields and rail identities remain intact. Invalid original values
    // remain visible for correction by the user instead of inventing defaults.
    for (auto& rail : state.config.rails) if (known_kind(rail.kind)) rail.mod = rail.orig;
    changed(state);
}
bool request_shunt_save(ShuntViewState& state) {
    if (!can_save(state)) return false;
    state.save_requested = true; return true;
}
bool take_shunt_save_request(ShuntViewState& state) {
    const bool requested = state.save_requested; state.save_requested = false; return requested;
}
void draw_shunt_power_summary(const ShuntViewState& state, std::optional<double> reported_draw,
    std::optional<double> requested_limit, std::optional<double> enforced_limit, bool stale) {
    const auto& correction = state.correction;
    ImGui::TextUnformatted(correction_label(correction));
    if (correction.valid && correction.active) { ImGui::SameLine(); ImGui::Text("x%.4g", correction.factor); }
    if (stale) ImGui::TextDisabled("Power telemetry is stale");
    power_line("Board draw", reported_draw, correction);
    power_line("Requested limit", requested_limit, correction);
    power_line("Enforced limit", enforced_limit, correction);
    if (!correction.valid || (correction.active && !correction.exact)) ImGui::TextWrapped("%s", correction.why.c_str());
}
void draw_shunt_window(ShuntViewState& state) {
    if (!state.open) return;
    const auto* viewport = ImGui::GetMainViewport();
    const ImVec2 maximum{std::max(1.0f, viewport->WorkSize.x - 32), std::max(1.0f, viewport->WorkSize.y - 32)};
    ImGui::SetNextWindowSizeConstraints({std::min(480.0f, maximum.x), std::min(300.0f, maximum.y)}, maximum);
    ImGui::SetNextWindowSize({std::min(820.0f, maximum.x), std::min(700.0f, maximum.y)}, ImGuiCond_FirstUseEver);
    ImGui::SetNextWindowPos({viewport->WorkPos.x + viewport->WorkSize.x * 0.5f,
        viewport->WorkPos.y + viewport->WorkSize.y * 0.5f}, ImGuiCond_FirstUseEver, {0.5f, 0.5f});
    if (!ImGui::Begin("Shunt mod", &state.open, ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse)) { ImGui::End(); return; }
    const float footer_height = ImGui::GetFrameHeightWithSpacing() + ImGui::GetStyle().ItemSpacing.y + 1;
    ImGui::BeginChild("shunt-body", {0, -footer_height});
    ImGui::TextUnformatted("Shunt-mod corrected power");
    ImGui::TextWrapped("Enter each rail's original and effective sense resistance. Correction is original / effective. "
        "Two equal 5 mOhm resistors in parallel give 2.5 mOhm (x2). This calculator changes displayed power only.");
    ImGui::TextWrapped("Connector layout and 5 mOhm starting values are editable templates, not detected topology or measured factory values.");
    ImGui::Separator();
    std::size_t remove = state.config.rails.size();
    if (ImGui::BeginTable("shunt-rails", 5, ImGuiTableFlags_Borders | ImGuiTableFlags_SizingStretchProp)) {
        ImGui::TableSetupColumn("Rail"); ImGui::TableSetupColumn("Original mOhm");
        ImGui::TableSetupColumn("Effective mOhm"); ImGui::TableSetupColumn("Multiplier"); ImGui::TableSetupColumn("Action");
        ImGui::TableHeadersRow();
        for (std::size_t i = 0; i < state.config.rails.size(); ++i) {
            const auto& rail = state.config.rails[i];
            ImGui::PushID(static_cast<int>(i)); ImGui::TableNextRow(); ImGui::TableNextColumn();
            ImGui::TextUnformatted(rail_label(rail.kind));
            if (!known_kind(rail.kind)) ImGui::TextWrapped("%s", rail.kind.c_str());
            ImGui::BeginDisabled(!known_kind(rail.kind));
            double original = rail.orig, effective = rail.mod;
            ImGui::TableNextColumn(); ImGui::SetNextItemWidth(-1);
            const bool edit_original = ImGui::InputDouble("##original", &original, 0, 0, "%.4g");
            ImGui::TableNextColumn(); ImGui::SetNextItemWidth(-1);
            const bool edit_effective = ImGui::InputDouble("##effective", &effective, 0, 0, "%.4g");
            if (edit_original || edit_effective) edit_shunt_rail(state, i, original, effective);
            ImGui::TableNextColumn();
            if (positive(original) && positive(effective) && std::isfinite(original / effective)) ImGui::Text("x%.4g", original / effective);
            else ImGui::TextUnformatted("Invalid");
            ImGui::TableNextColumn(); if (ImGui::SmallButton("Remove")) remove = i;
            ImGui::EndDisabled();
            ImGui::PopID();
        }
        ImGui::EndTable();
    }
    if (remove < state.config.rails.size()) remove_shunt_rail(state, remove);
    if (ImGui::Button("Add 8-pin")) add_shunt_rail(state, "pin8");
    ImGui::SameLine(); if (ImGui::Button("Add 6-pin")) add_shunt_rail(state, "pin6");
    ImGui::SameLine(); if (ImGui::Button("Add PCIe slot")) add_shunt_rail(state, "slot");
    if (ImGui::Button("Reset no mod (keep layout and originals)")) reset_shunt_no_mod(state);
    ImGui::TextUnformatted(correction_label(state.correction));
    if (state.correction.valid && state.correction.active) ImGui::Text("Board total x%.4g", state.correction.factor);
    ImGui::TextWrapped("%s", state.correction.why.c_str());
    if (state.correction.valid && state.correction.active && state.correction.exact)
        ImGui::TextWrapped("Every configured rail has the same multiplier; correction is exact under the entered resistance model, not an independent power measurement.");
    ImGui::Separator();
    ImGui::TextUnformatted("Parallel-resistance helper");
    ImGui::TextWrapped("Calculate a resistance, then optionally copy it or use it for all effective rail values. Save changes explicitly.");
    std::size_t remove_resistor = state.parallel_resistors.size();
    for (std::size_t i = 0; i < state.parallel_resistors.size(); ++i) {
        ImGui::PushID(static_cast<int>(i));
        ImGui::SetNextItemWidth(150);
        ImGui::InputDouble(i == 0 ? "Base shunt mOhm" : "Stacked resistor mOhm", &state.parallel_resistors[i], 0, 0, "%.4g");
        if (i >= 2) { ImGui::SameLine(); if (ImGui::SmallButton("Remove resistor")) remove_resistor = i; }
        ImGui::PopID();
    }
    if (remove_resistor < state.parallel_resistors.size())
        state.parallel_resistors.erase(state.parallel_resistors.begin() + static_cast<std::ptrdiff_t>(remove_resistor));
    if (ImGui::Button("Add resistor")) state.parallel_resistors.push_back(5.0);
    ImGui::SameLine();
    const auto equivalent = shunt_parallel_resistance(state.parallel_resistors);
    if (equivalent) ImGui::Text("Equivalent: %.4g mOhm", *equivalent);
    else ImGui::TextUnformatted("Enter positive, finite resistance values.");
    const bool has_known_rail = std::any_of(state.config.rails.begin(), state.config.rails.end(),
        [](const ShuntRail& rail) { return known_kind(rail.kind); });
    ImGui::BeginDisabled(!equivalent || !has_known_rail);
    if (ImGui::Button("Use for all rails")) {
        bool modified = false;
        for (auto& rail : state.config.rails) {
            if (known_kind(rail.kind) && rail.mod != *equivalent) {
                rail.mod = *equivalent;
                modified = true;
            }
        }
        if (modified) changed(state);
    }
    ImGui::EndDisabled(); ImGui::SameLine();
    ImGui::BeginDisabled(!equivalent);
    if (ImGui::Button("Copy value")) {
        char value[64];
        std::snprintf(value, sizeof(value), "%.9g", *equivalent);
        ImGui::SetClipboardText(value);
    }
    ImGui::EndDisabled();
    if (!state.config.load_error.empty()) ImGui::TextWrapped("Save unavailable: %s", state.config.load_error.c_str());
    if (!state.status.empty()) ImGui::TextWrapped("%s", state.status.c_str());
    ImGui::EndChild();
    ImGui::Separator();
    ImGui::BeginDisabled(!can_save(state));
    if (ImGui::Button("Save")) request_shunt_save(state);
    ImGui::EndDisabled(); ImGui::SameLine();
    if (ImGui::Button("Close")) state.open = false;
    ImGui::SameLine(); ImGui::TextDisabled("%s", state.dirty ? "Unsaved edits" : "Configuration loaded");
    ImGui::End();
}
}
