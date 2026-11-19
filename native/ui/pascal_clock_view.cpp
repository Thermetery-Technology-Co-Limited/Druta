// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "pascal_clock_view.h"
#include <imgui.h>
#include <algorithm>
#include <cmath>
#include <string>

namespace druta::ui {

namespace {
const char* domain_name(PascalClockDomain domain, bool state_targets) {
    switch (domain) {
        case PascalClockDomain::gpc: return "GPC";
        case PascalClockDomain::xbar: return "XBAR";
        case PascalClockDomain::sys: return state_targets ? "SYS" : "SYS / Video";
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

bool writable(const PascalClockField& field) {
    return field.minimum < field.maximum && field.current >= field.minimum && field.current <= field.maximum;
}
}

bool has_writable_pascal_clock(const PascalClockTable* table, PascalClockDomain domain) {
    return table && table->ok && std::any_of(table->fields.begin(), table->fields.end(), [&](const auto& field) {
        return field.domain == domain && writable(field);
    });
}

double pascal_clock_display_value(int pstate, int raw) {
    return pstate == -1 ? raw : raw * 0.5;
}

int pascal_clock_raw_value(const PascalClockField& field, double displayed) {
    if (!writable(field) || !std::isfinite(displayed)) return field.current;
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
    if (!writable(field) || raw_value < field.minimum || raw_value > field.maximum) return;
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
        if (it == table->fields.end() || !writable(*it) || it->current != draft.expected ||
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
void draw_field(PascalClockViewState& view, const PascalClockTable& table, const PascalClockField& field,
                bool editable, const std::optional<PascalClockReference>& reference, bool compact_ratio) {
    const bool ratio = field.pstate == -1;
    const auto draft = matching_row(view.drafts, field.domain, field.pstate);
    const bool stale = draft != view.drafts.end() && draft->expected != field.current;
    int value = draft == view.drafts.end() ? field.current : draft->value;
    ImGui::PushID(field.pstate);
    ImGui::PushID(static_cast<int>(field.domain));
    ImGui::TableNextRow();
    ImGui::TableSetColumnIndex(0);
    ImGui::AlignTextToFramePadding();
    ImGui::TextUnformatted(domain_name(field.domain, table.state_targets));
    ImGui::TableSetColumnIndex(1);
    ImGui::BeginDisabled(!editable || stale);
    ImGui::SetNextItemWidth(-1);
    if (ImGui::SliderInt("##slider", &value, field.minimum, field.maximum, " ", ImGuiSliderFlags_NoInput))
        view.stage(field, value);
    ImGui::TableSetColumnIndex(2);
    double number = pascal_clock_display_value(field.pstate, value);
    const double step = ratio ? 1.0 : 0.5;
    ImGui::SetNextItemWidth(-1);
    if (ImGui::InputDouble("##value", &number, step, 0.0, ratio ? "%.0f" : "%.1f",
                           ImGuiInputTextFlags_EnterReturnsTrue) && std::isfinite(number)) {
        view.stage(field, pascal_clock_raw_value(field, number));
    }
    ImGui::EndDisabled();
    if (ratio) {
        if (!compact_ratio) {
            ImGui::TableSetColumnIndex(3);
            ImGui::AlignTextToFramePadding();
        }
        const auto preview = pascal_clock_preview(table, view.drafts, field.domain, 0,
            reference ? std::optional<double>(reference->peak_mhz) : std::nullopt);
        if (preview.nominal_mhz) ImGui::Text(compact_ratio ? "~%.1f MHz" : "%.1f MHz", *preview.nominal_mhz);
        else ImGui::TextDisabled("--");
    }
    ImGui::PopID();
    ImGui::PopID();
}

// The selector uses just the rows decoded on this adapter. P0 sorts first even
// when the firmware lists its low-power states before the performance state.
void draw_controls(PascalClockViewState& state, const PascalClockTable& table, bool editable,
                   const std::optional<PascalClockReference>& reference) {
    std::vector<int> states;
    bool ratios = false;
    for (const auto& field : table.fields) {
        if (!writable(field)) continue;
        if (field.pstate == -1) ratios = true;
        else if (std::find(states.begin(), states.end(), field.pstate) == states.end())
            states.push_back(field.pstate);
    }
    std::sort(states.begin(), states.end());
    const auto& style = ImGui::GetStyle();
    float digit_width = 0;
    for (char digit = '0'; digit <= '9'; ++digit) {
        const char text[] = {digit, '\0'};
        digit_width = std::max(digit_width, ImGui::CalcTextSize(text).x);
    }
    // InputDouble takes the frame padding, two square buttons and two gaps out of the column.
    const float frame_height = ImGui::GetFrameHeight();
    const float input_chrome = 2 * (style.FramePadding.x + frame_height + style.ItemInnerSpacing.x);
    const float label_width = ImGui::CalcTextSize(table.state_targets ? "XBAR" : "SYS / Video").x;
    const float target_width = ImGui::CalcTextSize("9999.9 MHz").x;
    auto draw_group = [&](int pstate) {
        const bool ratio = pstate == -1;
        const float number_width = ratio ? 3 * digit_width : 5 * digit_width + ImGui::CalcTextSize(".").x;
        const float input_width = std::ceil(number_width + input_chrome) + 1;
        const bool compact_ratio = ratio && ImGui::GetContentRegionAvail().x <
            label_width + input_width + target_width + 8 * style.CellPadding.x + 3 * frame_height;
        const bool target_column = ratio && !compact_ratio;
        if (ImGui::BeginTable(ratio ? "shared_ratios" : "state_ceilings", target_column ? 4 : 3,
                              ImGuiTableFlags_SizingStretchProp)) {
            ImGui::TableSetupColumn("Clock", ImGuiTableColumnFlags_WidthFixed, label_width);
            ImGui::TableSetupColumn(ratio ? "GPC ratio" : table.state_targets ? "Nominal target" : "Maximum",
                                    ImGuiTableColumnFlags_WidthStretch, 1.0f);
            ImGui::TableSetupColumn(ratio ? "%" : "MHz", ImGuiTableColumnFlags_WidthFixed,
                                    compact_ratio ? std::max(input_width, ImGui::CalcTextSize("~9999.9 MHz").x) : input_width);
            if (target_column) ImGui::TableSetupColumn("Est. target", ImGuiTableColumnFlags_WidthFixed, target_width);
            ImGui::TableHeadersRow();
            for (const auto& field : table.fields)
                if (field.pstate == pstate && writable(field))
                    draw_field(state, table, field, editable, reference, compact_ratio);
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
        if (reference)
            ImGui::TextWrapped("%sP0 V/F peak is %.1f MHz at %.2f mV (NVVDD limit %.2f mV).",
                reference->staged ? "Staged " : "", reference->peak_mhz, reference->point_mv, reference->ceiling_mv);
        else
            ImGui::TextWrapped("P0 estimate needs a current V/F curve and NVVDD limit.");
    }
}
}

void draw_pascal_clock_sliders(PascalClockViewState& state, const PascalClockTable* table,
                               uint64_t generation, bool edit_enabled, bool apply_enabled,
                               std::vector<PascalClockEdit>& confirmed, const std::optional<PascalClockReference>& reference) {
    state.sync(generation);
    ImGui::PushID("PascalClockSliders");
    if (table && table->ok) draw_controls(state, *table, edit_enabled, reference);
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
                const char* format = ratio ? "%s  %s %s   %.0f%% to %.0f%%" : "%s  %s %s   %.1f to %.1f MHz";
                ImGui::Text(format, state_name(edit.pstate).c_str(), domain_name(edit.domain, table->state_targets),
                            ratio ? "ratio" : table->state_targets ? "target" : "maximum",
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
