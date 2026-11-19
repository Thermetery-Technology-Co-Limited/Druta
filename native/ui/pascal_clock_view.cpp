// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "pascal_clock_view.h"
#include <imgui.h>
#include <algorithm>
#include <cmath>
#include <string>

namespace druta::ui {

namespace {
const char* domain_name(PascalClockDomain domain) {
    switch (domain) {
        case PascalClockDomain::gpc: return "GPC";
        case PascalClockDomain::xbar: return "XBAR";
        case PascalClockDomain::sys: return "SYS / Video";
    }
    return "";
}

std::string state_name(int pstate) {
    return pstate == -1 ? "All states" : "P" + std::to_string(pstate);
}

template<class Rows>
auto matching_row(Rows& rows, PascalClockDomain domain, int pstate) {
    return std::find_if(rows.begin(), rows.end(), [&](const auto& row) {
        return row.domain == domain && row.pstate == pstate;
    });
}
}

double pascal_clock_display_value(int pstate, int raw) {
    return pstate == -1 ? raw : raw * 0.5;
}

int pascal_clock_raw_value(const PascalClockField& field, double displayed) {
    if (!std::isfinite(displayed)) return field.current;
    const double raw = field.pstate == -1 ? displayed : displayed * 2.0;
    return static_cast<int>(std::lround(std::clamp(raw, double(field.minimum), double(field.maximum))));
}

void PascalClockViewState::sync(uint64_t next_generation) {
    if (generation != next_generation) {
        *this = {};
        generation = next_generation;
    }
}

void PascalClockViewState::stage(const PascalClockField& field, int raw_value) {
    if (raw_value < field.minimum || raw_value > field.maximum) return;
    auto it = matching_row(drafts, field.domain, field.pstate);
    if (it != drafts.end() && it->expected != field.current) return;
    if (raw_value == field.current) {
        if (it != drafts.end()) drafts.erase(it);
    } else if (it == drafts.end()) {
        drafts.push_back({field.domain, field.pstate, field.current, raw_value});
    } else {
        it->value = raw_value;
    }
    reviewing = false;
}

void PascalClockViewState::discard() {
    drafts.clear();
    reviewing = false;
}

bool PascalClockViewState::current(const PascalClockTable* table) const {
    if (!table || !table->ok || drafts.empty()) return false;
    for (const auto& draft : drafts) {
        const auto it = matching_row(table->fields, draft.domain, draft.pstate);
        if (it == table->fields.end() || it->current != draft.expected ||
            draft.value < it->minimum || draft.value > it->maximum) return false;
    }
    return true;
}

bool PascalClockViewState::confirm(const PascalClockTable* table, bool enabled,
                                   std::vector<PascalClockEdit>& output) {
    if (!enabled || !reviewing || !current(table)) {
        reviewing = false;
        return false;
    }
    output = drafts;
    discard();
    return true;
}

namespace {
void draw_field(PascalClockViewState& view, const PascalClockField& field, bool editable) {
    const bool ratio = field.pstate == -1;
    const auto draft = matching_row(view.drafts, field.domain, field.pstate);
    const bool stale = draft != view.drafts.end() && draft->expected != field.current;
    int value = draft == view.drafts.end() ? field.current : draft->value;
    ImGui::PushID(field.pstate);
    ImGui::PushID(static_cast<int>(field.domain));
    ImGui::TableNextRow();
    ImGui::TableSetColumnIndex(0);
    ImGui::AlignTextToFramePadding();
    ImGui::TextUnformatted(domain_name(field.domain));
    ImGui::TableSetColumnIndex(1);
    ImGui::BeginDisabled(!editable || stale || field.minimum == field.maximum);
    ImGui::SetNextItemWidth(-1);
    if (ImGui::SliderInt("##slider", &value, field.minimum, field.maximum, " ", ImGuiSliderFlags_NoInput))
        view.stage(field, value);
    ImGui::TableSetColumnIndex(2);
    double number = pascal_clock_display_value(field.pstate, value);
    const double step = ratio ? 1.0 : 0.5;
    ImGui::SetNextItemWidth(-1);
    if (ImGui::InputDouble("##value", &number, step, 0.0, ratio ? "%.0f" : "%.1f",
                           ImGuiInputTextFlags_EnterReturnsTrue) && std::isfinite(number))
        view.stage(field, pascal_clock_raw_value(field, number));
    ImGui::EndDisabled();
    ImGui::PopID();
    ImGui::PopID();
}

// The selector uses just the rows decoded on this adapter. P0 sorts first even
// when the firmware lists its low-power states before the performance state.
void draw_controls(PascalClockViewState& state, const PascalClockTable& table, bool editable) {
    std::vector<int> states;
    bool ratios = false;
    for (const auto& field : table.fields) {
        if (field.pstate == -1) ratios = true;
        else if (std::find(states.begin(), states.end(), field.pstate) == states.end())
            states.push_back(field.pstate);
    }
    std::sort(states.begin(), states.end());
    auto draw_group = [&](int pstate) {
        const bool ratio = pstate == -1;
        if (ImGui::BeginTable(ratio ? "shared_ratios" : "state_ceilings", 3,
                              ImGuiTableFlags_SizingStretchProp)) {
            ImGui::TableSetupColumn("Clock", ImGuiTableColumnFlags_WidthFixed, ImGui::CalcTextSize("SYS / Video  ").x);
            ImGui::TableSetupColumn(ratio ? "GPC ratio" : "Maximum", ImGuiTableColumnFlags_WidthStretch);
            ImGui::TableSetupColumn(ratio ? "%" : "MHz", ImGuiTableColumnFlags_WidthFixed,
                                    ImGui::GetFrameHeight() * 2 + ImGui::CalcTextSize("9999.9    ").x);
            ImGui::TableHeadersRow();
            for (const auto& field : table.fields)
                if (field.pstate == pstate) draw_field(state, field, editable);
            ImGui::EndTable();
        }
    };
    if (!states.empty()) {
        if (std::find(states.begin(), states.end(), state.selected_pstate) == states.end())
            state.selected_pstate = states.front();
        ImGui::SetNextItemWidth(ImGui::GetFontSize() * 6);
        if (ImGui::BeginCombo("Performance state", state_name(state.selected_pstate).c_str())) {
            for (int pstate : states) {
                const bool selected = pstate == state.selected_pstate;
                if (ImGui::Selectable(state_name(pstate).c_str(), selected)) state.selected_pstate = pstate;
                if (selected) ImGui::SetItemDefaultFocus();
            }
            ImGui::EndCombo();
        }
        draw_group(state.selected_pstate);
    }
    if (ratios) {
        ImGui::TextUnformatted("GPC clock ratios (all states)");
        draw_group(-1);
    }
}
}

void draw_pascal_clock_sliders(PascalClockViewState& state, const PascalClockTable* table,
                               uint64_t generation, bool edit_enabled, bool apply_enabled,
                               std::vector<PascalClockEdit>& confirmed) {
    state.sync(generation);
    ImGui::PushID("PascalClockSliders");
    if (table && table->ok) draw_controls(state, *table, edit_enabled);
    if (!state.drafts.empty()) {
        const bool current = state.current(table);
        const bool can_apply = current && edit_enabled && apply_enabled;
        if (!can_apply) state.reviewing = false;
        if (!current && table && table->ok)
            ImGui::TextWrapped("Clock values or bounds changed. Discard the drafts and stage the changes again.");
        ImGui::BeginDisabled(!can_apply || state.reviewing);
        if (ImGui::Button("Review clock changes")) state.reviewing = true;
        ImGui::EndDisabled();
        ImGui::SameLine();
        if (ImGui::Button("Discard drafts")) state.discard();
        if (state.reviewing) {
            for (const auto& edit : state.drafts) {
                const bool ratio = edit.pstate == -1;
                const char* format = ratio ? "%s  %s ratio   %.0f%% to %.0f%%" : "%s  %s maximum   %.1f to %.1f MHz";
                ImGui::Text(format, state_name(edit.pstate).c_str(), domain_name(edit.domain),
                            pascal_clock_display_value(edit.pstate, edit.expected),
                            pascal_clock_display_value(edit.pstate, edit.value));
            }
            ImGui::TextWrapped("Apply refreshes the selected GPU and clears its current tuning. "
                               "Druta restores the original registry settings after loading the clocks. "
                               "The next driver restart clears these changes.");
            if (ImGui::Button("Apply and refresh selected GPU")) state.confirm(table, can_apply, confirmed);
        }
    }
    ImGui::PopID();
}
}
