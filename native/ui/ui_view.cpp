// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "ui_view.h"
#include "perf_diagnostics_view.h"
#include "power_arbitration_view.h"
#include "session.h"
#include "i2c_view.h"
#include "classic_layout.h"
#include "classic_section.h"
#include "monitor_view.h"

#include "imgui.h"
#include "implot.h"

#include <array>
#include <cmath>
#include <algorithm>
#include <limits>
#include <cstdio>
#include <utility>

namespace druta::ui {
namespace {

const ImVec4 accent{0.38f, 0.71f, 1.0f, 1.0f};
const ImVec4 secondary{0.56f, 0.63f, 0.72f, 1.0f};
const ImVec4 muted{0.43f, 0.50f, 0.60f, 1.0f};
const ImVec4 teal{0.29f, 0.81f, 0.70f, 1.0f};
const ImVec4 amber{0.96f, 0.72f, 0.37f, 1.0f};

float u(float value) { return value * ImGui::GetFontSize() / 18.0f; }
bool short_view() { return ImGui::GetIO().DisplaySize.y < u(840); }

void hint(const char* text) {
    if (text && *text && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled)) {
        ImGui::BeginTooltip();
        ImGui::PushTextWrapPos(u(360));
        ImGui::TextUnformatted(text);
        ImGui::PopTextWrapPos();
        ImGui::EndTooltip();
    }
}

void label(const char* text) {
    ImGui::TextColored(secondary, "%s", text);
}

void badge(const char* text, const ImVec4& color) {
    const ImVec2 position = ImGui::GetCursorScreenPos();
    const ImVec2 padding = ImGui::GetStyle().FramePadding;
    const ImVec2 text_size = ImGui::CalcTextSize(text);
    const ImVec2 size(text_size.x + padding.x * 2, text_size.y + padding.y * 2);
    ImDrawList* list = ImGui::GetWindowDrawList();
    list->AddRectFilled(position, ImVec2(position.x + size.x, position.y + size.y),
                        ImGui::ColorConvertFloat4ToU32(ImVec4(color.x, color.y, color.z, 0.12f)), u(4));
    list->AddText(ImVec2(position.x + padding.x, position.y + padding.y),
                  ImGui::ColorConvertFloat4ToU32(color), text);
    ImGui::Dummy(size);
}

void title(const char* text, const char* description) {
    const bool compact = short_view();
    ImGui::SetWindowFontScale(compact ? 1.15f : 1.35f);
    ImGui::TextUnformatted(text);
    ImGui::SetWindowFontScale(1.0f);
    ImGui::PushStyleColor(ImGuiCol_Text, secondary);
    ImGui::TextWrapped("%s", description);
    ImGui::PopStyleColor();
    if (!compact) ImGui::Dummy(ImVec2(0, u(8)));
}

void navigation(ViewState& state, bool horizontal) {
    struct Entry { Page page; const char* label; };
    constexpr Entry entries[] = {
        {Page::daily, "Daily tuning"}, {Page::rails, "Rails"}, {Page::i2c, "I2C"}, {Page::advanced, "Advanced"}, {Page::monitor, "Monitor"}, {Page::timings, "Timings"}
    };
    const int count = static_cast<int>(std::size(entries)); // V/F opens in its own window from Daily.
    const float width = horizontal ? (ImGui::GetContentRegionAvail().x - ImGui::GetStyle().ItemSpacing.x * (count - 1)) / count : 0.0f;
    for (int i = 0; i < count; ++i) {
        if (horizontal && i != 0) ImGui::SameLine();
        const bool selected = state.page == entries[i].page;
        if (selected) {
            ImGui::PushStyleColor(ImGuiCol_Text, accent);
            ImGui::PushStyleColor(ImGuiCol_Header, ImVec4(0.13f, 0.23f, 0.34f, 1.0f));
        }
        if (ImGui::Selectable(entries[i].label, selected, 0,
                              ImVec2(width, u(horizontal && short_view() ? 25.0f : 36.0f))))
            state.page = entries[i].page;
        if (selected) ImGui::PopStyleColor(2);
    }
    if (!horizontal) {
        ImGui::Dummy(ImVec2(0, u(26)));
        ImGui::Separator();
        ImGui::Dummy(ImVec2(0, u(12)));
        ImGui::TextColored(secondary, "WORKSPACE");
        ImGui::TextWrapped("Draft edits stay with you when you change the layout.");
        ImGui::Dummy(ImVec2(0, u(12)));
        ImGui::TextColored(muted, "%s", state.preview ? "Preview only" : "Session only");
        ImGui::PushStyleColor(ImGuiCol_Text, muted);
        ImGui::TextWrapped("%s", state.preview ? "GPU controls are not connected yet."
            : "Apply is explicit. Saved settings apply only when you load them.");
        ImGui::PopStyleColor();
    }
}

void top_bar(ViewState& state) {
    draw_app_actions_menu(state);
    const ImGuiStyle& style = ImGui::GetStyle();
    const float control_width = ImGui::CalcTextSize("Always refresh").x
        + ImGui::GetFrameHeight() + style.ItemInnerSpacing.x + u(60 + 164)
        + style.ItemSpacing.x * 2 + u(12);
    const float brand_width = ImGui::CalcTextSize("DRUTA").x * 1.2f + u(12)
        + ImGui::CalcTextSize("PREVIEW / SAMPLE DATA").x + style.FramePadding.x * 2 + u(205);
    const bool wrap_controls = ImGui::GetContentRegionAvail().x < brand_width + u(20) + control_width;
    ImGui::SetWindowFontScale(1.2f);
    ImGui::TextColored(accent, "DRUTA");
    ImGui::SetWindowFontScale(1.0f);
    ImGui::SameLine(0, u(12));
    badge(state.preview ? "PREVIEW / SAMPLE DATA" : "LIVE SESSION", state.preview ? amber : teal);
    ImGui::SameLine();draw_device_restart_button(state);
    if (!wrap_controls) {
        const float x = ImGui::GetWindowContentRegionMax().x - control_width;
        ImGui::SameLine(x);
    }
    ImGui::Checkbox("Always refresh", &state.always_refresh);
    hint("Off by default. Keep rendering and telemetry running when minimized. "
         "Explicit Pause overrides this option.");
    ImGui::SameLine();
    if (ImGui::Button("Pause", ImVec2(u(60), 0))) state.pause_requested = true;
    hint(state.hotkey_available
             ? "Pause rendering and telemetry. Restore from the taskbar or Ctrl+Shift+F11."
             : "Pause rendering and telemetry. Restore from the taskbar; the shortcut is unavailable.");
    ImGui::SameLine(0, u(12));
    ImGui::SetNextItemWidth(u(164));
    int presentation = state.presentation == Presentation::focused ? 0 : 1;
    if (ImGui::Combo("##presentation", &presentation, "Focused\0Verbose (Classic)\0"))
        state.presentation = presentation == 0 ? Presentation::focused : Presentation::classic;
    hint("Change presentation without changing your draft or GPU settings.");
    if (!state.preview) {
        const auto snapshot = state.live;
        if (snapshot) {
            ImGui::SetNextItemWidth(std::max(u(180), std::min(u(560), ImGui::GetContentRegionAvail().x - u(290))));
            const auto& identity = snapshot->data.identity;
            const std::string selected = std::string(identity.name[0] ? identity.name : "Select GPU")
                + (snapshot->selected_slot.empty() ? "" : " | " + snapshot->selected_slot);
            ImGui::BeginDisabled(snapshot->busy || snapshot->opening);
            if (ImGui::BeginCombo("##adapter", selected.c_str())) {
                for (const auto& device : snapshot->devices) {
                    const std::string item = device.name + " | " + device.slot;
                    if (ImGui::Selectable(item.c_str(), device.slot == snapshot->selected_slot))
                        state.select_slot = device.slot;
                }
                ImGui::EndCombo();
            }
            ImGui::EndDisabled();
            ImGui::SameLine();
            const bool profile_ready = snapshot->data.connected && !snapshot->busy && !snapshot->opening;
            ImGui::BeginDisabled(!profile_ready);
            if (ImGui::Button("Save profile...")) state.save_profile_requested = true;
            ImGui::EndDisabled();
            hint("Capture current requests to a file you select. Saving does not apply tuning.");
            ImGui::SameLine();
            ImGui::BeginDisabled(!profile_ready || !state.controls_unlocked);
            if (ImGui::Button("Load profile...")) state.load_profile_requested = true;
            ImGui::EndDisabled();
            hint("Select a saved profile to apply explicitly in this session. Unlock tuning controls first.");
            ImGui::SameLine();
            if (ImGui::Button("Profile catalog...")) state.profile_catalog_view.open=true;
            if (!snapshot->data.connected || snapshot->opening || snapshot->busy)
                ImGui::TextWrapped("%s", snapshot->status.c_str());
            if (identity.pairing_error[0])
                ImGui::TextWrapped("Target pairing: %s", identity.pairing_error);
            const auto& t = snapshot->data.telemetry;
            if (t.present & DC_T_VF_LOCKED)
                ImGui::TextColored((snapshot->telemetry_stale_mask & DC_T_VF_LOCKED) ? amber : secondary,
                    "V/F lock entries: %u%s", t.vf_locked_count,
                    ((snapshot->telemetry_stale_mask & DC_T_VF_LOCKED) || snapshot->paused) ? " (stale)" : "");
            else ImGui::TextColored(muted, "Hold state: unverified");
            if (t.present & DC_T_CLK_LOCK) {
                ImGui::SameLine();
                ImGui::TextColored(secondary, "Clock lock: %u-%u MHz%s", t.clk_lock_min_mhz,
                    t.clk_lock_max_mhz, ((snapshot->telemetry_stale_mask & DC_T_CLK_LOCK) || snapshot->paused) ? " (stale)" : "");
            }
            ImGui::SameLine();
            if (ImGui::TreeNode("Session details")) {
                ImGui::TextWrapped("%s", snapshot->status.c_str());
                ImGui::TextWrapped("Driver: %s | VBIOS: %s", identity.driver[0] ? identity.driver : "--",
                    identity.vbios[0] ? identity.vbios : "--");
                ImGui::TextWrapped("UUID: %s", identity.uuid[0] ? identity.uuid : "--");
                ImGui::TreePop();
            }
        } else ImGui::TextUnformatted("Opening GPU session...");
    } else ImGui::TextUnformatted("NVIDIA GeForce RTX 3070 Ti (sample)");
    if (state.preview && ImGui::GetContentRegionAvail().x > u(860)) {
        ImGui::SameLine(0, u(22));
        ImGui::TextColored(secondary, "Sample: no active hold");
    }
    ImGui::Separator();
}

void stat_cards() {
    const bool compact = short_view();
    if (!ImGui::BeginTable("summary", 3, ImGuiTableFlags_SizingStretchSame)) return;
    const char* names[] = {"CORE CLOCK", "TEMPERATURE", "BOARD POWER"};
    const char* values[] = {"1,935 MHz", "64 C", "228 W"};
    const char* details[] = {"Sample operating clock", "Sample GPU temperature", "Sample board reading"};
    for (int i = 0; i < 3; ++i) {
        ImGui::TableNextColumn();
        ImGui::PushID(i);
        ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0.075f, 0.10f, 0.14f, 1.0f));
        if (compact) ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(u(10), u(6)));
        ImGui::BeginChild("stat", ImVec2(0, u(compact ? 58.0f : 98.0f)), true, ImGuiWindowFlags_NoScrollbar);
        ImGui::TextColored(secondary, "%s", names[i]);
        ImGui::SetWindowFontScale(compact ? 1.15f : 1.35f);
        ImGui::TextUnformatted(values[i]);
        ImGui::SetWindowFontScale(1.0f);
        if (!compact) ImGui::TextColored(muted, "%s", details[i]);
        ImGui::EndChild();
        if (compact) ImGui::PopStyleVar();
        ImGui::PopStyleColor();
        ImGui::PopID();
    }
    ImGui::EndTable();
}

void draft_row(const char* name, const char* unit, float& value, float lo, float hi,
               const char* readback, bool verbose) {
    ImGui::PushID(name);
    ImGui::TableNextRow();
    ImGui::TableNextColumn();
    ImGui::AlignTextToFramePadding();
    ImGui::TextUnformatted(name);
    ImGui::TableNextColumn();
    const float available = ImGui::GetContentRegionAvail().x;
    const float entry = u(94);
    const bool show_slider = available > u(205);
    if (show_slider) {
        ImGui::SetNextItemWidth(available - entry - ImGui::GetStyle().ItemSpacing.x);
        ImGui::SliderFloat("##slider", &value, lo, hi, "%.0f", ImGuiSliderFlags_NoInput);
        if (ImGui::IsItemEdited()) value = std::round(value);
        ImGui::SameLine();
    }
    ImGui::SetNextItemWidth(show_slider ? entry : available);
    float edited = value;
    if (ImGui::InputFloat("##value", &edited, 0.0f, 0.0f, "%.9g") && std::isfinite(edited))
        value = edited;
    hint("Type a precise draft value. Slider bounds are sample values for this preview.");
    ImGui::TableNextColumn();
    ImGui::AlignTextToFramePadding();
    ImGui::TextColored(secondary, "%s", unit);
    if (verbose) {
        ImGui::TableNextColumn();
        ImGui::AlignTextToFramePadding();
        ImGui::TextColored(secondary, "%s", readback);
    }
    ImGui::PopID();
}

void tuning_controls(ViewState& state, bool verbose) {
    ImGui::SeparatorText("Clock and cooling");
    const bool wide = verbose && ImGui::GetContentRegionAvail().x >= u(650);
    const auto flags = ImGuiTableFlags_SizingStretchProp | ImGuiTableFlags_PadOuterX
                     | (verbose ? ImGuiTableFlags_RowBg : ImGuiTableFlags_None);
    if (ImGui::BeginTable("tuning", wide ? 4 : 3, flags)) {
        ImGui::TableSetupColumn("Control", ImGuiTableColumnFlags_WidthFixed, u(135));
        ImGui::TableSetupColumn("Draft", ImGuiTableColumnFlags_WidthStretch);
        ImGui::TableSetupColumn("Unit", ImGuiTableColumnFlags_WidthFixed, u(78));
        if (wide) ImGui::TableSetupColumn("Sample readback", ImGuiTableColumnFlags_WidthFixed, u(138));
        if (verbose) ImGui::TableHeadersRow();
        draft_row("Core offset", "MHz", state.core_offset_mhz, -300, 300, "+0 MHz", wide);
        draft_row("Memory offset", "MHz true", state.memory_offset_mhz, -125, 250, "+0 MHz true", wide);
        draft_row("Power limit", "W", state.power_limit_w, 100, 350, "310 W requested", wide);
        draft_row("Fan duty", "%", state.fan_percent, 0, 100, "30% requested", wide);
        ImGui::EndTable();
    }
    if (!short_view()) ImGui::Dummy(ImVec2(0, u(10)));
    ImGui::BeginDisabled();
    ImGui::Button("Apply to GPU", ImVec2(u(150), u(34)));
    ImGui::EndDisabled();
    hint("GPU controls are not connected in this layout preview. No setting will be applied.");
    ImGui::SameLine();
    ImGui::BeginDisabled(!state.has_draft());
    if (ImGui::Button("Revert draft", ImVec2(u(130), u(34)))) state.revert_draft();
    ImGui::EndDisabled();
    ImGui::SameLine(0, u(14));
    ImGui::TextColored(state.has_draft() ? amber : secondary,
                       state.has_draft() ? "Draft changed" : "No draft changes");
    if (verbose && !short_view()) {
        ImGui::Dummy(ImVec2(0, u(6)));
        ImGui::TextColored(muted, "Configured requests and live readings stay separate.");
        ImGui::TextColored(muted, "This preview changes local drafts only; Apply is unavailable.");
    }
}

void vf_plot(const ViewState& state, float height) {
    constexpr std::array<double, 15> voltage{
        700, 725, 750, 775, 800, 825, 850, 875, 900, 925, 950, 975, 1000, 1025, 1050};
    constexpr std::array<double, 15> base{
        1200, 1290, 1380, 1470, 1545, 1620, 1680, 1755, 1815, 1875, 1935, 1980, 2010, 2040, 2070};
    std::array<double, 15> draft{};
    for (std::size_t i = 0; i < draft.size(); ++i) draft[i] = base[i] + state.core_offset_mhz;
    if (ImPlot::BeginPlot("##voltage_frequency", ImVec2(-1, height), ImPlotFlags_NoTitle)) {
        ImPlot::SetupAxes("Voltage (mV)", "Core (MHz)");
        ImPlot::SetupAxesLimits(680, 1070, 900, 2450, ImGuiCond_Once);
        ImPlotSpec base_style;
        base_style.LineColor = secondary;
        base_style.LineWeight = 2.0f;
        ImPlot::PlotLine("Sample curve", voltage.data(), base.data(), static_cast<int>(base.size()), base_style);
        if (state.core_offset_mhz != 0) {
            ImPlotSpec draft_style;
            draft_style.LineColor = accent;
            draft_style.LineWeight = 2.5f;
            ImPlot::PlotLine("Offset draft", voltage.data(), draft.data(), static_cast<int>(draft.size()), draft_style);
        }
        const double point_x = 950, point_y = 1935;
        ImPlotSpec point_style;
        point_style.LineColor = teal;
        point_style.Marker = ImPlotMarker_Circle;
        point_style.MarkerSize = 5.0f;
        point_style.MarkerFillColor = teal;
        ImPlot::PlotScatter("Sample operating point", &point_x, &point_y, 1, point_style);
        ImPlot::EndPlot();
    }
}

void daily(ViewState& state, bool verbose) {
    if (!short_view())
        title(verbose ? "Control" : "Daily tuning",
              verbose ? "Classic / Verbose presentation - sample values and shared draft controls."
                      : "Clock, power and cooling in one place.");
    stat_cards();
    if (!short_view()) ImGui::Dummy(ImVec2(0, u(12)));
    tuning_controls(state, verbose);
    if (ImGui::Button("Open V/F curve")) {
        state.curve_window_open = true;
        state.curve_window_focus_requested = true;
    }
}

void detail_table(const char* id, const char* const* names, const char* const* values, int count) {
    if (!ImGui::BeginTable(id, 2, ImGuiTableFlags_RowBg | ImGuiTableFlags_SizingStretchProp)) return;
    ImGui::TableSetupColumn("Item", ImGuiTableColumnFlags_WidthStretch, 1.0f);
    ImGui::TableSetupColumn("Sample", ImGuiTableColumnFlags_WidthStretch, 1.0f);
    for (int i = 0; i < count; ++i) {
        ImGui::TableNextRow();
        ImGui::TableNextColumn();
        ImGui::TextUnformatted(names[i]);
        ImGui::TableNextColumn();
        ImGui::TextColored(secondary, "%s", values[i]);
    }
    ImGui::EndTable();
}

void advanced(bool verbose) {
    title("Advanced", "Inspect one system at a time. Expand only the detail you need.");
    ImGui::TextColored(amber, "Sample data - advanced GPU controls are not connected.");
    ImGui::Dummy(ImVec2(0, u(14)));
    const auto flags = verbose ? ImGuiTreeNodeFlags_DefaultOpen : ImGuiTreeNodeFlags_None;
    if (ImGui::CollapsingHeader("Voltage rails", flags)) {
        const char* names[] = {"NVVDD sample reading", "Reliability ceiling", "Minimum request"};
        const char* values[] = {"950 mV", "1,081.25 mV (sample estimate)", "662.5 mV (sample request)"};
        detail_table("rails", names, values, 3);
        ImGui::TextWrapped("A configured limit, a driver estimate and a measured voltage describe "
                           "different things. The live view will keep those distinctions visible.");
    }
    ImGui::Dummy(ImVec2(0, u(8)));
    if (ImGui::CollapsingHeader("Current and power policies", flags)) {
        const char* names[] = {"Core-current request", "Board-power request", "Enforced limit"};
        const char* values[] = {"243 A (sample)", "310 W (sample)", "310 W (sample)"};
        detail_table("policies", names, values, 3);
        ImGui::TextWrapped("Power-policy details stay here so daily tuning can stay compact.");
    }
    ImGui::Dummy(ImVec2(0, u(8)));
    if (ImGui::CollapsingHeader("Diagnostics and experiment tools", flags)) {
        ImGui::TextWrapped("I2C, timing and firmware tools will retain their individual readback "
                           "and recovery workflows. They will be opened explicitly when needed.");
        ImGui::TextColored(secondary, "No diagnostics are running in this preview.");
    }
}

struct History {
    std::array<double, 61> seconds{};
    std::array<double, 61> clocks{};
    std::array<double, 61> temperature{};
    std::array<double, 61> power{};
    History() {
        for (std::size_t i = 0; i < seconds.size(); ++i) {
            const double t = static_cast<double>(i);
            seconds[i] = t - 60.0;
            clocks[i] = 1935.0 + 15.0 * std::sin(t * 0.42) + 7.5 * std::sin(t * 0.91);
            temperature[i] = 62.0 + 2.0 * std::sin(t * 0.04) + 0.4 * std::sin(t * 0.22);
            power[i] = 228.0 + 8.0 * std::sin(t * 0.31) + 3.0 * std::sin(t * 0.73);
        }
    }
};

void history_plot(const char* id, const char* unit, const std::array<double, 61>& values,
                  double low, double high, const ImVec4& color) {
    static const History history;
    if (ImPlot::BeginPlot(id, ImVec2(-1, u(195)), ImPlotFlags_NoTitle | ImPlotFlags_NoLegend)) {
        ImPlot::SetupAxes("Seconds", unit);
        ImPlot::SetupAxesLimits(-60, 0, low, high, ImGuiCond_Once);
        ImPlotSpec style;
        style.LineColor = color;
        style.LineWeight = 2.0f;
        ImPlot::PlotLine("Sample history", history.seconds.data(), values.data(),
                         static_cast<int>(values.size()), style);
        ImPlot::EndPlot();
    }
}

void monitor(bool verbose) {
    static const History history;
    title("Monitor", "A quiet view of the essentials, with detail on demand.");
    ImGui::TextColored(amber, "Static sample history - no live telemetry is connected.");
    ImGui::Dummy(ImVec2(0, u(12)));
    stat_cards();
    ImGui::Dummy(ImVec2(0, u(12)));
    label("CORE CLOCK");
    history_plot("##clock_history", "MHz", history.clocks, 1850, 2000, accent);
    if (ImGui::BeginTable("monitor_pair", 2, ImGuiTableFlags_SizingStretchSame)) {
        ImGui::TableNextColumn();
        label("TEMPERATURE");
        history_plot("##temperature_history", "C", history.temperature, 55, 75, teal);
        ImGui::TableNextColumn();
        label("BOARD POWER");
        history_plot("##power_history", "W", history.power, 200, 250, amber);
        ImGui::EndTable();
    }
    if (ImGui::CollapsingHeader("All sample readings", verbose ? ImGuiTreeNodeFlags_DefaultOpen : 0)) {
        const char* names[] = {"Core", "Memory (true)", "Voltage", "Temperature", "Board power", "Fan"};
        const char* values[] = {"1,935 MHz", "1,187.5 MHz", "950 mV", "64 C", "228 W", "30%"};
        detail_table("sensors", names, values, 6);
    }
}

void sync_live_drafts(ViewState& state) {
    if (!state.live) return;
    const auto& snapshot = *state.live;
    if (!state.generation_initialized || state.draft_generation != snapshot.generation) {
        state.drafts = {};
        state.draft_generation = snapshot.generation;
        state.generation_initialized = true;
        // Unlock is a presentation preference. Discard adapter-specific drafts,
        // but preserve an explicit lock choice across session changes.
    }
    for (size_t i = 0; i < state.drafts.size(); ++i) {
        auto& draft = state.drafts[i];
        const auto& current = snapshot.data.controls[i].current;
        if (!current || !std::isfinite(*current)) continue;
        draft.baseline = current;
        if (!draft.initialized || (!draft.dirty && !draft.editing)) {
            draft.value = *current;
            draft.initialized = true;
        }
        if (!draft.editing) draft.dirty = draft.value != *current;
    }
}

void sync_curve_entry(ViewState& state) {
    const auto& points = state.curve_draft.draft();
    if (points.empty()) return;
    auto point = std::find_if(points.begin(), points.end(), [&](const auto& row) { return row.idx == state.selected_curve_point; });
    // 1.9.1 vf_read: with no selection yet, or one this curve lacks, select the
    // middle point. Focused keeps its first-point fallback.
    const bool classic = state.presentation == Presentation::classic;
    if (point == points.end() || (classic && state.curve_selection_pending)) {
        const auto middle = curve_editor_seed(points, 0, 0).middle_idx;
        point = classic && middle ? std::find_if(points.begin(), points.end(), [&](const auto& row) { return row.idx == *middle; })
                                  : points.begin();
        if (point == points.end()) point = points.begin();
        state.selected_curve_point = point->idx;
        state.curve_entry_dirty = false;
    }
    state.curve_selection_pending = false;
    if (!state.curve_entry_dirty && !state.curve_entry_editing && state.curve_draft.freq_div())
    {
        state.curve_delta_mhz = point->delta_khz / (1000.0 * state.curve_draft.freq_div());
        state.curve_frequency_mhz = point->freq_mhz;
    }
}

void classic_log_line(ViewState& state, std::string text, std::optional<bool> ok) {
    const auto& log = state.live->log;
    state.classic_log_lines.push_back({log.empty() ? 0 : log.back().id, std::move(text), ok});
    if (state.classic_log_lines.size() > 200) state.classic_log_lines.erase(state.classic_log_lines.begin());
}

std::string bounded_text(const char* text, size_t size) {
    return std::string(text, std::find(text, text + size, '\0'));
}

// The commands 1.9.1 follows with vf_read(force=True): V/F Apply, Reset curve and
// Max it (its curve write) once they succeed; Reset all to stock, a full profile
// load and Undo last write (a full profile load) whatever their result. A scoped
// profile load and a label-only Undo do not re-read.
bool command_rereads_curve(const ViewState& state, const SessionSnapshot& snapshot) {
    if (!snapshot.last_action_operation) return false;
    switch (*snapshot.last_action_operation) {
    case Command::Operation::apply_curve: case Command::Operation::reset_curve: case Command::Operation::max_it:
        return snapshot.last_action_ok;
    case Command::Operation::reset_controls: case Command::Operation::load_profile: return true;
    case Command::Operation::apply_catalog_profile: return state.classic_catalog_rereads;
    case Command::Operation::undo: return !state.classic_undo_metadata_only;
    default: return false;
    }
}

// 1.9.1 refresh_full_profile runs for a full profile, also inside a combined
// one; the scoped kinds refresh only their own controls.
bool profile_rereads_curve(const ProfileJson& document) {
    const auto full = [](const ProfileJson& part) {
        const auto scope = inspect_profile(part).scope;
        return scope == ProfileScope::daily || scope == ProfileScope::curve || scope == ProfileScope::full_tune
            || scope == ProfileScope::python_full_tune;
    };
    try {
        if (inspect_profile(document).scope != ProfileScope::combined) return full(document);
        for (const auto& component : flatten_profile_collection(document)) if (full(component.document)) return true;
    } catch (...) {}
    return false;
}

void observe_curve(ViewState& state) {
    if (!state.live) return;
    const auto& snapshot = *state.live;
    const bool classic = state.presentation == Presentation::classic;
    state.curve_draft.keep_history_across_reads(classic);
    if (classic && !state.classic_curve_presets_applied) {
        // The released frontend's presets, bounded to each curve on its read below.
        state.curve_cap_mv = curve_cap_default_mv; state.curve_floor_mv = curve_ramp_floor_default_mv;
        state.hard_floor_mv = curve_hard_floor_default_mv; state.classic_curve_presets_applied = true;
    }
    // 1.9.1 logs the backend status once the adapter is open, before its first curve read.
    const auto& identity = snapshot.data.identity;
    if (snapshot.generation && state.classic_backend_logged_generation != snapshot.generation && !snapshot.opening
        && (identity.size == sizeof(identity) || identity.nvapi_ok || identity.nvml_ok)) {
        const auto half = [](int32_t ok, const char* error, size_t size) {
            const auto detail = bounded_text(error, size);
            return ok ? std::string("ok") : "FAIL (" + detail + ")";
        };
        auto slot = bounded_text(identity.slot, sizeof(identity.slot));
        if (slot.empty()) slot = snapshot.selected_slot;
        classic_log_line(state, "backend: NVAPI: " + half(identity.nvapi_ok, identity.nvapi_error, sizeof(identity.nvapi_error))
            + "   |   NVML: " + half(identity.nvml_ok, identity.nvml_error, sizeof(identity.nvml_error))
            + (slot.empty() ? std::string() : "   |   slot " + slot), std::nullopt);
        state.classic_backend_logged_generation = snapshot.generation;
    }
    const bool generation = !state.curve_observation_initialized || state.observed_curve_generation != snapshot.generation;
    const bool revision = !state.curve_observation_initialized || state.observed_curve_revision != snapshot.curve_revision;
    const bool stale = snapshot.curve_stale || !snapshot.data.connected || snapshot.curve_revision == 0;
    // A finished command arrives with the session's read after it: when 1.9.1
    // follows that command with vf_read, this snapshot answers it.
    bool command_read = false;
    if (!generation && !snapshot.busy && state.curve_read_actions != snapshot.completed_actions)
        command_read = command_rereads_curve(state, snapshot);
    const bool edge = generation || revision || (stale && !state.observed_curve_stale);
    const bool was_diverged = !generation && state.curve_draft.diverged();
    if (edge) {
        state.curve_draft.observe(snapshot.generation, snapshot.data.curve, !stale, snapshot.data.curve_freq_div);
        if (generation) {
            state.curve_entry_dirty = false; state.curve_entry_editing = false;
            state.curve_drag_point.reset(); state.curve_fit_requested = true;
            state.acknowledged_curve_action = snapshot.completed_actions;
            state.curve_plan_message.clear();
            state.classic_discard_armed = false;
            // A different adapter/session starts without a selection, as a new ViewState does.
            if (state.curve_observation_initialized) state.curve_selection_pending = true;
            // 1.9.1 reads the curve once the adapter is open, at startup and on a GPU switch.
            state.curve_read_requested = true; state.curve_read_reads = snapshot.completed_reads;
        }
    }
    if (generation || !snapshot.busy) {
        state.curve_read_actions = snapshot.completed_actions;
        state.classic_undo_metadata_only = snapshot.data.undo_metadata_only;
    }
    // The session reads the curve after every command; only startup, a GPU
    // switch, Read curve and the commands above are a 1.9.1 vf_read. A fresh
    // curve answers one; a failed read answers it too: the curve turned stale,
    // the command's own read failed, or one already stale failed twice more.
    const bool fresh = edge && !stale && (generation || revision);
    const bool vf_read = state.curve_read_requested || command_read;
    if (vf_read && fresh && !snapshot.data.curve.empty()) {
        // 1.9.1 vf_read: rebase the editor on the hardware curve (staged edits,
        // the plan and its acknowledgement and the editor history go), bound the
        // floors to this curve's voltages, seed the hard target from its peak,
        // and log what the read found.
        if (classic) {
            // 1.9.1 writes the curve on the UI thread, so nothing can be staged
            // while its Apply runs. Here an edit staged while the write was in
            // flight differs from what it wrote: keep it, as Focused does.
            const bool newer_edits = command_read && snapshot.last_action_operation == Command::Operation::apply_curve
                && curve_pending_edits(snapshot.data.curve, state.curve_draft.draft()) != 0;
            if (!newer_edits) {
                if (state.curve_draft.revert()) { state.curve_entry_dirty = false; state.curve_entry_editing = false; }
                state.hard_mod_ack = false; state.hard_plan_staged = false; state.curve_plan_note.clear();
            }
            state.classic_discard_armed = false;
            const auto seed = curve_editor_seed(snapshot.data.curve, state.curve_floor_mv, state.hard_floor_mv);
            if (seed.ramp_floor_mv) state.curve_floor_mv = *seed.ramp_floor_mv;
            if (seed.hard_floor_mv) state.hard_floor_mv = *seed.hard_floor_mv;
            if (seed.hard_target_mhz) state.hard_target_mhz = *seed.hard_target_mhz;
        }
        classic_log_line(state, curve_read_summary(snapshot.data.curve, state.curve_cap_mv), true);
        state.curve_read_requested = false;
    } else if (vf_read && stale && !generation && !snapshot.opening
        && (command_read || !state.observed_curve_stale || snapshot.completed_reads >= state.curve_read_reads + 2)) {
        // 1.9.1 logs the read error and keeps the previous read on screen.
        classic_log_line(state, snapshot.data.curve_error.empty() ? std::string("Curve read unavailable") : snapshot.data.curve_error, false);
        state.curve_read_requested = false;
    } else if (vf_read && fresh) state.curve_read_requested = false; // an adapter without a V/F curve: nothing to log
    if (edge) {
        sync_curve_entry(state);
        state.observed_curve_revision = snapshot.curve_revision;
        state.observed_curve_generation = snapshot.generation;
        state.curve_observation_initialized = true;
    }
    if (!stale && !snapshot.busy && state.acknowledged_curve_action != snapshot.completed_actions) {
        if (snapshot.last_action_ok && snapshot.last_action_operation == Command::Operation::apply_curve) {
            if (state.curve_draft.accept_applied()) { state.curve_entry_dirty = false; sync_curve_entry(state); }
        }
        state.acknowledged_curve_action = snapshot.completed_actions;
    }
    // Classic has no editor status line: a read that leaves the staged edits on
    // a curve the card no longer has says so once in the log, which the status
    // line under the statistics mirrors. Apply refuses until Revert rebases.
    if (classic && state.curve_draft.diverged() && !was_diverged)
        classic_log_line(state, state.curve_draft.status(), false);
    state.observed_curve_stale = stale;
}

// 1.9.1 apply_pl: after a board-power write, a configured readback that is
// unavailable or differs from the request leaves a warning on the Limits
// footer. The result is the next completed action after the queued request
// (the session refusing it logs a line instead); its own read follows it.
void observe_power_request(ViewState& state) {
    if (!state.live) return;
    const auto& s = *state.live;
    auto& feedback = state.classic_power_feedback;
    auto& pending = state.classic_power_request;
    if (feedback && feedback->generation != s.generation) feedback.reset();
    const auto& control = s.data.controls[static_cast<size_t>(ControlId::power)];
    // A transient read keeps the last known request on screen; it is not a readback.
    const bool valid = control.availability == Availability::available && control.current
        && std::isfinite(*control.current) && *control.current > 0;
    if (pending) {
        const auto request = *pending;
        if (request.generation != s.generation) pending.reset();
        else if (s.completed_actions == request.completed_actions) {
            if (!s.busy && !s.log.empty() && s.log.back().id > request.log_id) pending.reset();
        } else if (!s.busy) {
            pending.reset();
            // Python checks a readback only once the request reached the driver:
            // the board-power result says whether it may have (a refusal before
            // the write leaves no feedback), what it asked for and what it read.
            const bool ours = s.completed_actions == request.completed_actions + 1 && s.last_action_operation && !s.last_action_ok
                && (*s.last_action_operation == Command::Operation::apply || *s.last_action_operation == Command::Operation::stock);
            const bool unread = ours && s.last_action_may_have_changed && !s.last_action_readback;
            const bool differs = ours && s.last_action_readback && s.last_action_requested
                && std::llround(*s.last_action_readback * 1000) != std::llround(*s.last_action_requested * 1000);
            const auto w = [](double value) { char b[48]{}; std::snprintf(b, sizeof(b), "%g", value); return std::string(b); };
            auto& draft = state.drafts[static_cast<size_t>(ControlId::power)];
            // The read that followed the action is the next refresh: a valid one
            // already settles an unread readback, as 1.9.1's next refresh does.
            if ((unread || differs) && !valid) {
                std::string text = "Current configured power request is unverified: readback unavailable.";
                if (draft.baseline) {
                    text += " Slider shows last verified " + w(*draft.baseline) + " W, not a confirmed current setting.";
                    draft.value = *draft.baseline; draft.dirty = draft.editing = false;
                } else text += " Slider value is an unverified proposal.";
                feedback = ViewState::ClassicPowerFeedback{s.generation, false, text};
                return;
            }
            if (differs && std::llround(*control.current * 1000) != std::llround(request.requested_w * 1000)) {
                feedback = ViewState::ClassicPowerFeedback{s.generation, true, "Requested " + w(request.requested_w)
                    + " W did not match configured readback " + w(*control.current) + " W; slider returned to readback."};
                draft.value = *control.current; draft.dirty = draft.editing = false;
                return;
            }
        }
    }
    // An unverified request clears on the next valid configured read; a
    // mismatch stays until the next board-power request or adapter.
    if (feedback && !feedback->mismatch && valid) feedback.reset();
}

const char* availability_text(Availability availability) {
    switch (availability) {
    case Availability::available: return "Available";
    case Availability::unsupported: return "Unsupported";
    case Availability::transient: return "Read temporarily unavailable";
    default: return "Not established";
    }
}

std::string number_text(const std::optional<double>& value, const char* unit) {
    if (!value || !std::isfinite(*value)) return "--";
    char buffer[96]{};
    std::snprintf(buffer, sizeof(buffer), "%.9g %s", *value, unit);
    return buffer;
}

std::optional<double> telemetry_value(const SessionSnapshot& snapshot, uint64_t bit, double value) {
    if (!(snapshot.data.telemetry.present & bit) || !std::isfinite(value)) return std::nullopt;
    return value;
}

void measured_value(const SessionSnapshot& snapshot, const char* name, uint64_t bit,
                    double value, const char* unit, int decimals = -1) {
    ImGui::TextUnformatted(name);
    ImGui::SameLine();
    const auto observed = telemetry_value(snapshot, bit, value);
    const bool stale = observed && ((snapshot.telemetry_stale_mask & bit) || snapshot.paused);
    auto text = number_text(observed, unit);
    if (observed && decimals >= 0) {
        char buffer[96]{};
        std::snprintf(buffer, sizeof(buffer), "%.*f %s", decimals, *observed, unit);
        text = buffer;
    }
    ImGui::TextColored(stale ? amber : secondary, "%s%s", text.c_str(), stale ? " (stale)" : "");
}

std::string board_power_text(const SessionSnapshot& snapshot, const ShuntCorrection& correction) {
    const auto raw=telemetry_value(snapshot,DC_T_POWER_W,snapshot.data.telemetry.power_w);
    const auto raw_text=number_text(raw,"W");
    if(!correction.valid || !correction.active)return raw_text;
    return number_text(correction.apply(raw),"W")+(correction.exact?" corrected":" estimate")+" ("+raw_text+" raw)";
}
void board_power_reading(const SessionSnapshot& snapshot,const ShuntCorrection& correction) {
    const bool stale=snapshot.paused || (snapshot.telemetry_stale_mask&DC_T_POWER_W);
    ImGui::TextColored(stale?amber:secondary,"%s%s",board_power_text(snapshot,correction).c_str(),stale?" (stale)":"");
}
void live_summary(const SessionSnapshot& snapshot,const ShuntCorrection& correction) {
    const auto& telemetry = snapshot.data.telemetry;
    if (ImGui::BeginTable("live_summary", 3, ImGuiTableFlags_SizingStretchSame)) {
        ImGui::TableNextColumn();
        measured_value(snapshot, "Core", DC_T_CORE, telemetry.core, "MHz", 0);
        ImGui::TableNextColumn();
        measured_value(snapshot, "GPU", DC_T_TEMP_EDGE, telemetry.temp_edge, "C", 0);
        ImGui::TableNextColumn();
        ImGui::TextUnformatted("Board");ImGui::SameLine();board_power_reading(snapshot,correction);
        ImGui::EndTable();
    }
    if (snapshot.paused) ImGui::TextColored(amber, "Telemetry paused; displayed readings are last known.");
    if (!snapshot.data.telemetry_error.empty())
        ImGui::TextWrapped("Telemetry: %s", snapshot.data.telemetry_error.c_str());
}

// Python refresh_control's live column: what the card reports now, never the
// request. Zero or absent core/memory/power/Vcore reads "--" as in Python;
// a stopped fan's 0% is a valid reading.
std::string classic_live_reading(const SessionSnapshot& snapshot, ControlId id) {
    const auto& t = snapshot.data.telemetry;
    const auto whole = [](const std::optional<double>& value, const char* suffix) -> std::string {
        if (!value || !std::isfinite(*value) || *value == 0) return "--";
        char buffer[64]{}; std::snprintf(buffer, sizeof(buffer), "%.0f%s", *value, suffix); return buffer;
    };
    switch (id) {
    case ControlId::core: return whole(telemetry_value(snapshot, DC_T_CORE, t.core), " MHz");
    case ControlId::memory: {
        // The unit follows mem_fmt: true MHz only with a known divisor; the raw
        // driver figure is not MHz and is left without a unit.
        const auto reported = telemetry_value(snapshot, DC_T_MEM, t.mem);
        if (!reported || *reported == 0) return "--";
        return memory_clock_text(snapshot.data, reported).value + (snapshot.data.memory_divisor ? " MHz" : "");
    }
    // Raw draw even with a shunt correction: the limit is enforced against
    // the number the card believes. Corrected watts stay in the tooltip/Monitor.
    case ControlId::power: return whole(telemetry_value(snapshot, DC_T_POWER_W, t.power_w), " W");
    case ControlId::boost: return whole(telemetry_value(snapshot, DC_T_VCORE_MV, t.vcore_mv), " mV");
    case ControlId::fan: {
        if (!t.fan_count || !t.fans[0].has_duty) return "--";
        char buffer[32]{}; std::snprintf(buffer, sizeof(buffer), "%.0f%%", static_cast<double>(t.fans[0].duty)); return buffer;
    }
    default: return "--";
    }
}

void live_control_row(ViewState& state, ControlId id, const char* name, bool classic) {
    const size_t index = static_cast<size_t>(id);
    const auto& snapshot = *state.live;
    const auto& control = snapshot.data.controls[index];
    auto& draft = state.drafts[index];
    ImGui::TableNextRow(); ImGui::TableNextColumn();
    // Table headers use numeric column IDs. A namespace is required before
    // numeric control IDs, otherwise Apply(column 3) aliases Apply(fan 3).
    ImGui::PushID("control-row"); ImGui::PushID(static_cast<int>(id));
    ImGui::AlignTextToFramePadding();
    // Python slider_row labels and a present live reading use its TEXT colour.
    const ImVec4 classic_text(230 / 255.0f, 232 / 255.0f, 236 / 255.0f, 1);
    if (classic) {
        ImGui::PushStyleColor(ImGuiCol_Text, classic_text);
        ImGui::TextWrapped("%s%s%s", name, draft.dirty ? " *" : "", control.availability == Availability::transient ? " (last read)" : "");
        ImGui::PopStyleColor();
    }
    else ImGui::Text("%s%s%s", name, draft.dirty ? " *" : "",
        control.availability == Availability::transient ? " (last read)" : "");
    hint(control.detail.c_str());
    if(id==ControlId::power) {
        if(ImGui::Button("Shunt mod..."))state.shunt.open=true;
        hint("Correct displayed watts using your shunt resistances. The board-power slider stays in driver-reported watts.");
        const auto& correction=state.shunt.correction;
        if(!correction.valid || !state.shunt.config.load_error.empty()) ImGui::TextColored(amber,"Check shunt config");
        else if(correction.active) ImGui::TextColored(amber,"x%.4g %s",correction.factor,correction.exact?"uniform":"estimate");
    }
    ImGui::TableNextColumn();
    const bool editable = control.writable && draft.initialized && snapshot.data.connected && !snapshot.opening && !snapshot.busy;
    const bool ranged = control.low && control.high && std::isfinite(*control.low) && std::isfinite(*control.high) && *control.low < *control.high;
    bool edited = false, editing = false;
    if (!classic) {
        ImGui::AlignTextToFramePadding();
        ImGui::TextColored(control.availability == Availability::transient ? amber : secondary, "%s",
            number_text(control.current, control.unit.c_str()).c_str());
        hint(control.detail.empty() ? availability_text(control.availability) : control.detail.c_str());
        if(id==ControlId::power && state.shunt.correction.valid && state.shunt.correction.active)
            ImGui::TextWrapped("%s %s",number_text(state.shunt.correction.apply(control.current),"W").c_str(),state.shunt.correction.exact?"corrected":"estimate");
        ImGui::TableNextColumn();
    }
    ImGui::BeginDisabled(!editable);
    const float draft_width = ImGui::GetContentRegionAvail().x;
    const bool slider = ranged && (classic || draft_width > u(285));
    if (slider) {
        ImGui::SetNextItemWidth(classic ? -1 : std::max(u(90), draft_width - u(120)));
        if (ImGui::SliderScalar("##slider", ImGuiDataType_Double, &draft.value, &*control.low, &*control.high,
            classic ? " " : id == ControlId::memory ? "%.0f" : "%.6g", ImGuiSliderFlags_NoInput)) {
            if (id == ControlId::memory) draft.value = std::round(draft.value);
            edited = true;
        }
        editing = ImGui::IsItemActive();
        if (!classic) ImGui::SameLine();
    }
    if (classic) ImGui::TableNextColumn();
    if (draft.initialized) {
        ImGui::SetNextItemWidth(classic ? -1 : std::min(u(105), std::max(u(75), draft_width)));
        double value = draft.value;
        if (ImGui::InputDouble("##value", &value, 0, 0, "%.9g") && std::isfinite(value)) { draft.value = value; edited = true; }
        editing = editing || ImGui::IsItemActive();
        hint("Editable request. Dragging changes only the draft; Apply writes it explicitly.");
    } else ImGui::TextDisabled("--");
    ImGui::EndDisabled();
    draft.editing = editing;
    if (edited) draft.dirty = !draft.baseline || draft.value != *draft.baseline;
    if (classic) {
        ImGui::TableNextColumn(); ImGui::AlignTextToFramePadding();
        push_classic_mono();
        auto reading = classic_live_reading(snapshot,id);
        const bool missing = reading == "--";
        const uint64_t live_bit = id == ControlId::core ? DC_T_CORE : id == ControlId::memory ? DC_T_MEM
            : id == ControlId::power ? DC_T_POWER_W : id == ControlId::boost ? DC_T_VCORE_MV : 0;
        const auto& telemetry = snapshot.data.telemetry;
        const bool have_reading = id == ControlId::fan ? telemetry.fan_count && telemetry.fans[0].has_duty
            : (telemetry.present & live_bit) != 0;
        const bool stale = snapshot.paused || !snapshot.data.connected || (snapshot.telemetry_stale_mask & live_bit)
            || (id == ControlId::fan && (snapshot.fan_duty_stale_mask & 1));
        if (have_reading && stale && !missing) reading += " (stale)";
        if (missing) ImGui::TextDisabled("%s", reading.c_str());
        else { ImGui::PushStyleColor(ImGuiCol_Text, classic_text); ImGui::TextWrapped("%s", reading.c_str()); ImGui::PopStyleColor(); }
        ImGui::PopFont();
        std::string readback_hint = "Configured request: " + number_text(control.current, control.unit.c_str()) + ". Live reading is separate.";
        const auto& correction = state.shunt.correction;
        if (id == ControlId::power && correction.valid && correction.active) {
            const auto raw = telemetry_value(snapshot, DC_T_POWER_W, snapshot.data.telemetry.power_w);
            const auto corrected = correction.apply(raw);
            if (raw && corrected && std::isfinite(*corrected)) {
                char buffer[128]{};
                std::snprintf(buffer, sizeof(buffer), "\nShunt-%s board draw: %.0f W (raw %.0f W x%.4g). The limit is enforced on the raw figure shown here.",
                    correction.exact ? "corrected" : "estimated", *corrected, *raw, correction.factor);
                readback_hint += buffer;
            }
        }
        hint(readback_hint.c_str());
    }
    ImGui::TableNextColumn();
    const bool in_range = std::isfinite(draft.value) && (!control.low || draft.value >= *control.low) && (!control.high || draft.value <= *control.high);
    // 1.9.1 apply_pl: a new board-power request drops the previous readback
    // warning and waits for its own (observe_power_request).
    const auto power_request = [&](double requested_w) {
        if (!classic || id != ControlId::power) return;
        state.classic_power_feedback.reset();
        state.classic_power_request = ViewState::ClassicPowerRequest{snapshot.generation, snapshot.completed_actions,
            snapshot.log.empty() ? 0 : snapshot.log.back().id, requested_w};
    };
    ImGui::BeginDisabled(!editable || !state.controls_unlocked || !in_range);
    if (ImGui::Button("Apply", ImVec2(classic ? -1.0f : 0.0f, 0))) {
        state.commands.push_back({Command::Operation::apply, id, draft.value});
        power_request(draft.value);
    }
    ImGui::EndDisabled(); hint(control.detail.c_str());
    ImGui::TableNextColumn();
    ImGui::BeginDisabled(!control.stock_writable || !snapshot.data.connected || snapshot.opening || snapshot.busy || !state.controls_unlocked);
    if (ImGui::Button(id == ControlId::fan ? "Auto" : "Stock", ImVec2(classic ? -1.0f : 0.0f, 0))) {
        state.commands.push_back({Command::Operation::stock, id, control.stock.value_or(0)});
        if (control.stock) power_request(*control.stock);
    }
    ImGui::EndDisabled();
    ImGui::PopID(); ImGui::PopID();
}

void end_curve_drag(ViewState& state) {
    state.curve_draft.end_gesture();
    state.curve_drag_point.reset();
}

void select_curve_point(ViewState& state, uint32_t index) {
    state.selected_curve_point = index;
    state.curve_entry_dirty = false;
    state.curve_entry_editing = false;
    sync_curve_entry(state);
}

bool hold_queued(const ViewState& state) {
    return std::any_of(state.commands.begin(), state.commands.end(), [](const Command& c) {
        return c.operation == Command::Operation::hold_curve_point || c.operation == Command::Operation::release_hold;
    });
}
bool can_hold_selected(const ViewState& state) {
    const auto& s = *state.live;
    const auto& points = state.curve_draft.draft();
    const auto point = std::find_if(points.begin(), points.end(), [&](const auto& p) { return p.idx == state.selected_curve_point; });
    return state.controls_unlocked && s.data.connected && !s.busy && !s.opening && !s.curve_stale
        && s.curve_revision && s.data.curve_freq_div && s.data.hold_available && !s.data.tuning.maintenance_needed()
        && !s.tuning_ownership_uncertain && !state.curve_draft.dirty() && !state.curve_entry_dirty
        && point != points.end() && std::isfinite(point->volt_mv) && point->volt_mv > 0 && !hold_queued(state);
}
void queue_selected_hold(ViewState& state) {
    if (!can_hold_selected(state)) return;
    const auto& s = *state.live;
    Command c; c.operation = Command::Operation::hold_curve_point; c.hold_point_index = state.selected_curve_point;
    c.expected_curve = s.data.curve; c.expected_curve_freq_div = s.data.curve_freq_div;
    c.hold_headroom = state.hold_headroom; c.headroom_margin_mv = state.headroom_margin_mv;
    state.commands.push_back(std::move(c));
}
void queue_hold_release(ViewState& state) {
    if (!state.live->busy && state.live->data.tuning.maintenance_needed() && !hold_queued(state)) {
        Command c; c.operation = Command::Operation::release_hold; state.commands.push_back(std::move(c));
    }
}
void hold_status(const SessionSnapshot& snapshot) {
    const auto& t = snapshot.data.tuning;
    if (t.maintenance_needed())
        ImGui::TextColored(t.confirmed && !t.recovery ? secondary : amber, "Owned V/F hold %.3f mV%s%s%s",
            t.voltage_uv / 1000.0, t.confirmed ? "" : " / unconfirmed", t.recovery ? " / recovery pending" : "", t.headroom ? " + headroom" : "");
    else if (snapshot.tuning_ownership_uncertain) ImGui::TextColored(amber, "V/F hold ownership uncertain");
}

void nudge_selected_curve(ViewState& state, int bins) {
    const auto& points = state.curve_draft.draft();
    const auto selected = std::find_if(points.begin(), points.end(), [&](const auto& point) { return point.idx == state.selected_curve_point; });
    if (selected == points.end() || !state.live) return;
    const auto& data = state.live->data;
    // 1.9.1 vf_nudge's undo label: whole MHz, truncated as int() does.
    char label[64]{};
    std::snprintf(label, sizeof(label), "nudge idx %u %+d MHz", selected->idx,
        static_cast<int>(static_cast<double>(bins) * data.curve_step_khz / 1000.0));
    state.curve_draft.stage_frequency_mhz(selected->idx, selected->freq_mhz
        + static_cast<double>(bins) * data.curve_step_khz / 1000.0,
        data.curve_step_khz, data.curve_max_abs_delta_khz, false, label);
    state.curve_entry_dirty = false; sync_curve_entry(state);
}
// One step through the editor's own history (never the card's). Classic logs
// it untagged as 1.9.1 vf_undo / vf_redo do.
void curve_history_step(ViewState& state, bool redo) {
    auto& editor = state.curve_draft;
    const bool moved = redo ? editor.redo_edit() : editor.undo_edit();
    state.curve_entry_dirty = false;
    sync_curve_entry(state);
    if (state.presentation != Presentation::classic || !state.live) return;
    if (!moved) classic_log_line(state, redo ? "nothing to redo" : "nothing to undo", std::nullopt);
    else if (redo) classic_log_line(state, "redo: " + editor.undo_label() + " (" + std::to_string(editor.redo_depth()) + " more)", std::nullopt);
    else classic_log_line(state, "undo: " + editor.redo_label() + " (" + std::to_string(editor.undo_depth()) + " more) - "
        + std::to_string(curve_pending_edits(editor.baseline(), editor.draft())) + " edit(s) still staged", std::nullopt);
}
void curve_keyboard(ViewState& state, bool window_wide = false) {
    const auto& io = ImGui::GetIO();
    if (!ImGui::IsWindowFocused(window_wide ? ImGuiFocusedFlags_AnyWindow : ImGuiFocusedFlags_ChildWindows)
        || io.AppFocusLost || io.WantTextInput || (!window_wide && state.curve_entry_editing) || ImGui::IsAnyItemActive()) return;
    if (window_wide && (io.KeyAlt || io.KeySuper || ImGui::IsPopupOpen(nullptr,
        ImGuiPopupFlags_AnyPopupId | ImGuiPopupFlags_AnyPopupLevel))) return;
    auto& editor = state.curve_draft;
    const auto& points = editor.draft();
    if (points.empty()) return;
    if (io.KeyCtrl) {
        const bool z = ImGui::IsKeyPressed(ImGuiKey_Z);
        const bool y = ImGui::IsKeyPressed(ImGuiKey_Y);
        if ((z || y) && ImGui::GetTime() - state.curve_last_undo_time >= 0.4) {
            state.curve_last_undo_time = ImGui::GetTime();
            curve_history_step(state, y || io.KeyShift);
        }
        return;
    }
    if (io.KeyAlt || io.KeySuper) return;
    const int multiplier = io.KeyShift ? 3 : 1;
    auto selected = std::find_if(points.begin(), points.end(), [&](const auto& point) { return point.idx == state.selected_curve_point; });
    if (selected == points.end()) return;
    int position = static_cast<int>(selected - points.begin());
    if (ImGui::IsKeyPressed(ImGuiKey_A)) position -= multiplier;
    if (ImGui::IsKeyPressed(ImGuiKey_D)) position += multiplier;
    position = std::max(0, std::min(static_cast<int>(points.size()) - 1, position));
    if (points[static_cast<size_t>(position)].idx != state.selected_curve_point) {
        select_curve_point(state, points[static_cast<size_t>(position)].idx);
        selected = points.begin() + position;
    }
    int direction = 0;
    if (ImGui::IsKeyPressed(ImGuiKey_W)) ++direction;
    if (ImGui::IsKeyPressed(ImGuiKey_S)) --direction;
    if (direction) nudge_selected_curve(state, direction * multiplier);
}

void interactive_curve_plot(ViewState& state, std::optional<float> fixed_height = std::nullopt) {
    const auto& snapshot = *state.live;
    auto& editor = state.curve_draft;
    const auto& points = editor.draft();
    if (points.empty()) {
        ImGui::TextWrapped("Curve unavailable: %s", snapshot.data.curve_error.empty() ? editor.status().c_str() : snapshot.data.curve_error.c_str());
        return;
    }
    if (state.curve_drag_point && (!ImGui::IsMouseDown(ImGuiMouseButton_Left)
        || ImGui::GetIO().AppFocusLost || !ImGui::IsWindowFocused(ImGuiFocusedFlags_ChildWindows))) end_curve_drag(state);
    const auto saved_input = ImPlot::GetInputMap();
    auto& input = ImPlot::GetInputMap();
    input.Pan = state.curve_drag_point ? 4 : ImGuiMouseButton_Left;
    input.Fit = 4; // Fit view is explicit; a double click on a dot is still a dot grab.
    if (state.curve_fit_requested) ImPlot::SetNextAxesToFit();
    const float height = fixed_height ? *fixed_height : std::max(u(180), ImGui::GetContentRegionAvail().y);
    const auto flags = ImPlotFlags_NoTitle | ImPlotFlags_NoMouseText | ImPlotFlags_NoMenus | ImPlotFlags_NoBoxSelect;
    const bool classic = state.presentation == Presentation::classic;
    ImPlot::PushStyleVar(ImPlotStyleVar_FitPadding, ImVec2(0.03f, 0.06f));
    if (classic) {
        // 1.9.1 binds no plot theme: neutral grey frame and plot area with the
        // stock thin border, not the Focused navy.
        ImPlot::PushStyleColor(ImPlotCol_PlotBg, ImVec4(37 / 255.0f, 37 / 255.0f, 38 / 255.0f, 1));
        ImPlot::PushStyleColor(ImPlotCol_FrameBg, ImVec4(51 / 255.0f, 51 / 255.0f, 55 / 255.0f, 1));
        ImPlot::PushStyleColor(ImPlotCol_PlotBorder, ImVec4(0.43f, 0.43f, 0.50f, 0.50f));
        ImPlot::PushStyleColor(ImPlotCol_LegendBg, ImVec4(37 / 255.0f, 37 / 255.0f, 38 / 255.0f, 1));
        ImPlot::PushStyleVar(ImPlotStyleVar_PlotBorderSize, 1.0f);
    }
    if (ImPlot::BeginPlot("##live_curve", ImVec2(-1, height), flags)) {
        double xmin = points.front().volt_mv, xmax = xmin, ymin = points.front().freq_mhz, ymax = ymin;
        for (const auto& point : points) {
            xmin = std::min(xmin, point.volt_mv); xmax = std::max(xmax, point.volt_mv);
            ymin = std::min(ymin, point.freq_mhz); ymax = std::max(ymax, point.freq_mhz);
        }
        xmin -= 25; xmax += 25;
        ymin = std::min(0.0, ymin - 120); ymax = std::max(3000.0, ymax + 200);
        // 1.9.1 labels the axes with the bare units.
        if (classic) ImPlot::SetupAxes("mV", "MHz");
        else ImPlot::SetupAxes("Voltage (mV)", "Core (MHz)");
        // View constraints only: they are not device voltage/frequency limits.
        ImPlot::SetupAxisLimitsConstraints(ImAxis_X1, xmin, xmax);
        ImPlot::SetupAxisLimitsConstraints(ImAxis_Y1, ymin, ymax);
        ImPlot::SetupAxisZoomConstraints(ImAxis_X1, std::min(20.0, xmax - xmin), xmax - xmin);
        ImPlot::SetupAxisZoomConstraints(ImAxis_Y1, std::min(100.0, ymax - ymin), ymax - ymin);
        ImPlot::SetupFinish();
        if (ImPlot::IsPlotHovered() && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
            end_curve_drag(state);
            const ImVec2 mouse = ImGui::GetIO().MousePos;
            double closest = static_cast<double>(u(14)) * u(14);
            std::optional<uint32_t> grabbed;
            for (const auto& point : points) {
                const auto pixel = ImPlot::PlotToPixels(point.volt_mv, point.freq_mhz);
                const double dx = pixel.x - mouse.x, dy = pixel.y - mouse.y;
                const double distance = dx * dx + dy * dy;
                if (distance <= closest) { closest = distance; grabbed = point.idx; }
            }
            if (grabbed) {
                state.curve_drag_point = grabbed;
                select_curve_point(state, *grabbed);
                editor.begin_gesture("drag idx " + std::to_string(*grabbed)); // 1.9.1: one undo step per grab
            }
        }
        if (state.curve_drag_point && ImGui::IsMouseDragging(ImGuiMouseButton_Left)) {
            const auto& data = snapshot.data;
            editor.stage_frequency_mhz(*state.curve_drag_point, ImPlot::GetPlotMousePos().y,
                data.curve_step_khz, data.curve_max_abs_delta_khz);
            state.curve_entry_dirty = false;
            sync_curve_entry(state);
        }
        std::array<double, DC_VFP_POINTS> current_x{}, current_y{}, draft_x{}, draft_y{};
        const size_t current_count = std::min(snapshot.data.curve.size(), current_x.size());
        const size_t draft_count = std::min(points.size(), draft_x.size());
        for (size_t i = 0; i < current_count; ++i) {
            current_x[i] = snapshot.data.curve[i].volt_mv; current_y[i] = snapshot.data.curve[i].freq_mhz;
        }
        for (size_t i = 0; i < draft_count; ++i) { draft_x[i] = points[i].volt_mv; draft_y[i] = points[i].freq_mhz; }
        // Classic uses 1.9.1's series: ImPlot's default Deep colormap (blue,
        // orange, green) with its marker sizes and line weights.
        const ImVec4 current_color = classic ? ImVec4(76 / 255.0f, 114 / 255.0f, 176 / 255.0f, 1) : secondary;
        const ImVec4 edited_color = classic ? ImVec4(221 / 255.0f, 132 / 255.0f, 82 / 255.0f, 1) : accent;
        const ImVec4 selected_color = classic ? ImVec4(85 / 255.0f, 168 / 255.0f, 104 / 255.0f, 1) : amber;
        const auto px = [classic](float value) { return classic ? classic_px(value) : u(value); };
        // ImPlot 1.0 strokes marker edges at the line weight; 1.9.1's ImPlot drew
        // them 1 px, so Classic leaves the edge out and keeps a round dot.
        const ImVec4 no_edge(0, 0, 0, 0);
        ImPlotSpec current;
        current.LineColor = current_color; current.LineWeight = px(2);
        current.Marker = ImPlotMarker_Circle; current.MarkerSize = px(5);
        current.MarkerFillColor = current_color;
        if (classic) current.MarkerLineColor = no_edge;
        ImPlot::PlotLine(classic ? "current" : "Current", current_x.data(), current_y.data(), static_cast<int>(current_count), current);
        ImPlotSpec edited;
        edited.LineColor = edited_color; edited.LineWeight = classic ? px(3) : u(1);
        edited.Marker = ImPlotMarker_Circle; edited.MarkerSize = px(6);
        edited.MarkerFillColor = edited_color;
        edited.MarkerLineColor = classic ? no_edge : ImVec4(0.055f, 0.078f, 0.11f, 1);
        ImPlot::PlotLine(classic ? "edited" : "Draft", draft_x.data(), draft_y.data(), static_cast<int>(draft_count), edited);
        const auto selected = std::find_if(points.begin(), points.end(), [&](const auto& point) { return point.idx == state.selected_curve_point; });
        // 1.9.1's legend lists all seven series, an empty one included.
        const double none = 0;
        if (selected != points.end() || classic) {
            const bool found = selected != points.end();
            ImPlotSpec mark;
            mark.Marker = ImPlotMarker_Diamond; mark.MarkerSize = px(12);
            mark.MarkerFillColor = selected_color; mark.MarkerLineColor = selected_color;
            ImPlot::PlotScatter(classic ? "selected" : "Selected", found ? &selected->volt_mv : &none,
                found ? &selected->freq_mhz : &none, found ? 1 : 0, mark);
        }
        if (classic) {
            // 1.9.1 guide lines: cap WARN, ramp floor VIOLET (2 px), hard floor
            // BAD only while the acknowledgement is ticked, held GOOD (3 px).
            const auto py = [](int r, int g, int b) { return ImVec4(r / 255.0f, g / 255.0f, b / 255.0f, 1); };
            const auto guide = [&](const char* name, std::optional<double> voltage, ImVec4 color, float weight) {
                const bool shown = voltage && std::isfinite(*voltage);
                ImPlotSpec line; line.LineColor = color; line.LineWeight = px(weight); line.Flags = ImPlotItemFlags_NoFit;
                ImPlot::PlotInfLines(name, shown ? &*voltage : &none, shown ? 1 : 0, line);
            };
            guide("cap", state.curve_cap_mv, py(255, 203, 71), 2);
            guide("ramp floor", state.curve_floor_mv, py(160, 108, 255), 2);
            guide("hard floor", state.hard_mod_ack ? std::optional<double>(state.hard_floor_mv) : std::nullopt, py(255, 92, 92), 3);
            const auto& hold = snapshot.data.tuning;
            const bool held = hold.owned && hold.confirmed && !hold.recovery && !snapshot.tuning_ownership_uncertain;
            guide("held", held ? std::optional<double>(hold.voltage_uv / 1000.0) : std::nullopt, py(70, 209, 122), 3);
            if (selected != points.end()) {
                const auto limits = ImPlot::GetPlotLimits();
                ImPlot::Annotation(limits.X.Max, limits.Y.Min, ImVec4(0,0,0,0), {-px(8),-px(8)}, true,
                    "%s", curve_corner_text(*selected, editor.freq_div()).c_str());
            }
        }
        const auto& telemetry = snapshot.data.telemetry;
        if ((telemetry.present & (DC_T_VCORE_MV | DC_T_CORE)) == (DC_T_VCORE_MV | DC_T_CORE)
            && !(snapshot.telemetry_stale_mask & (DC_T_VCORE_MV | DC_T_CORE)) && !snapshot.paused) {
            const double clock = telemetry.core;
            ImPlotSpec observed;
            observed.Marker = ImPlotMarker_Circle; observed.MarkerSize = px(4);
            observed.MarkerFillColor = teal;
            // A native addition: Classic names it in 1.9.1's lowercase legend style, with a teal swatch.
            if (classic) observed.MarkerLineColor = teal;
            ImPlot::PlotScatter(classic ? "observed" : "Observed operating point", &telemetry.vcore_mv, &clock, 1, observed);
        }
        ImPlot::EndPlot();
        state.curve_fit_requested = false;
    }
    if (classic) { ImPlot::PopStyleVar(); ImPlot::PopStyleColor(4); }
    ImPlot::PopStyleVar();
    ImPlot::GetInputMap() = saved_input;
}

std::vector<uint32_t> replaced_clock_offsets(const SessionSnapshot& snapshot);
void queue_front_pascal_clock_read(ViewState& state);
void front_pascal_clock_panel(ViewState& state);

void live_daily(ViewState& state, bool verbose) {
    const auto& snapshot = *state.live;
    state.open_pascal_clocks_requested = false;
    queue_front_pascal_clock_read(state);
    if (!short_view()) title(verbose ? "Control" : "Daily tuning", "Stage a request, then Apply. Undo restores the recorded request.");
    live_summary(snapshot,state.shunt.correction);
    ImGui::BeginDisabled(snapshot.busy || snapshot.opening);
    if (ImGui::Button("Refresh readings")) state.refresh_requested = true;
    ImGui::EndDisabled();
    const char* names[] = {"Core offset", "Memory offset", "Board power", "Fan target", "Voltage boost"};
    if (ImGui::BeginTable("daily_controls", 5, ImGuiTableFlags_SizingStretchProp | ImGuiTableFlags_RowBg)) {
        ImGui::TableSetupColumn("Control", ImGuiTableColumnFlags_WidthFixed, u(120));
        ImGui::TableSetupColumn("Configured", ImGuiTableColumnFlags_WidthFixed, u(125));
        ImGui::TableSetupColumn("Draft", ImGuiTableColumnFlags_WidthStretch);
        ImGui::TableSetupColumn("Apply", ImGuiTableColumnFlags_WidthFixed, u(58));
        ImGui::TableSetupColumn("Reset", ImGuiTableColumnFlags_WidthFixed, u(58));
        ImGui::TableHeadersRow();
        for (size_t i = 0; i < snapshot.data.controls.size(); ++i)
            live_control_row(state, static_cast<ControlId>(i), names[i], false);
        ImGui::EndTable();
    }
    ImGui::SeparatorText("NVVDD current limit");
    draw_core_current_limit(snapshot, state.controls_unlocked, false, state.advanced_view, state.commands);
    front_pascal_clock_panel(state);
    draw_clock_voltage_offsets(snapshot,state.controls_unlocked,false,state.advanced_view,state.commands,
                               OffsetGroup::daily_clocks,replaced_clock_offsets(snapshot));
    if (!state.advanced_view.notice.empty()) ImGui::TextWrapped("%s", state.advanced_view.notice.c_str());
    if (verbose) {
        if (ImGui::CollapsingHeader("Rail limits", ImGuiTreeNodeFlags_DefaultOpen))
            draw_daily_rail_controls(snapshot, state.controls_unlocked, false, state.advanced_view, state.commands);
        if (ImGui::CollapsingHeader("I2C regulator")) draw_i2c_panel(state);
    }
    if (!verbose && ImGui::CollapsingHeader("Control details")) {
        for (size_t i = 0; i < snapshot.data.controls.size(); ++i) {
            const auto& control = snapshot.data.controls[i];
            ImGui::Text("%s: %s%s", names[i], availability_text(control.availability), control.writable ? "" : " / read-only");
            if (!control.detail.empty()) ImGui::TextWrapped("%s", control.detail.c_str());
            ImGui::TextWrapped("Range: %s to %s", number_text(control.low, control.unit.c_str()).c_str(),
                number_text(control.high, control.unit.c_str()).c_str());
        }
    }
    ImGui::Separator();
    ImGui::BeginDisabled(!state.has_draft());
    if (ImGui::Button("Revert local drafts")) state.revert_draft();
    ImGui::EndDisabled();
    ImGui::SameLine();
    ImGui::BeginDisabled((!state.controls_unlocked&&!snapshot.data.undo_metadata_only) || !snapshot.data.undo_available || (!snapshot.data.connected&&!snapshot.data.undo_metadata_only)
        || snapshot.busy || snapshot.opening);
    if (ImGui::Button(snapshot.data.undo_metadata_only?"Undo label update":"Undo hardware write")) state.commands.push_back({Command::Operation::undo, ControlId::memory, 0});
    ImGui::EndDisabled();
    if (!snapshot.data.undo_label.empty()) hint(snapshot.data.undo_label.c_str());
    if (snapshot.data.enforced_power_w)
        ImGui::TextWrapped("NVML enforced power limit: %s. This is separate from the configured request and actual draw.",
            number_text(snapshot.data.enforced_power_w, "W").c_str());
    if (ImGui::Button("Open V/F curve")) {
        state.curve_window_open = true;
        state.curve_window_focus_requested = true;
    }
}

bool curve_plan_possible(const ViewState& state) {
    const auto& data = state.live->data;
    return !state.curve_draft.draft().empty() && data.curve_step_khz && data.curve_max_abs_delta_khz
        && data.curve_model_max_rise_khz && state.planner_api.predict;
}

// Plans are computed by core, then staged as one local edit. Nothing is written.
// True once a plan is staged; curve_plan_message says what happened either way.
bool stage_curve_plan(ViewState& state, CurvePlanKind kind) {
    const auto& snapshot = *state.live;
    auto& editor = state.curve_draft;
    CurvePlanRequest request;
    request.kind = kind; request.points = editor.draft(); request.freq_div = editor.freq_div();
    request.step_khz = snapshot.data.curve_step_khz; request.max_abs_delta_khz = snapshot.data.curve_max_abs_delta_khz;
    request.max_rise_khz = snapshot.data.curve_model_max_rise_khz;
    if (kind == CurvePlanKind::deflatten || kind == CurvePlanKind::ramp) request.cap_mv = state.curve_cap_mv;
    if (kind == CurvePlanKind::ramp) request.floor_mv = state.curve_floor_mv;
    if (kind == CurvePlanKind::hard_deflatten) { request.floor_mv = state.hard_floor_mv; request.target_mhz = state.hard_target_mhz; }
    const auto plan = plan_curve(state.planner_api, request);
    state.curve_plan_message = plan.message;
    if (!plan.ok || plan.changes.empty()) return false;
    const bool hard_underneath = state.hard_plan_staged && editor.dirty();
    // 1.9.1 push_undo labels for the four planners.
    const char* label = kind == CurvePlanKind::deflatten ? "limited de-flatten" : kind == CurvePlanKind::ramp ? "de-flatten"
        : kind == CurvePlanKind::hard_deflatten ? "hard de-flatten" : "re-phase";
    auto staged = editor; staged.end_gesture(); staged.begin_gesture(label); bool valid = true;
    for (const auto& change : plan.changes) valid = staged.stage_khz(static_cast<uint32_t>(change.idx), static_cast<double>(change.delta_khz)) && valid;
    staged.end_gesture();
    if (!valid) { state.curve_plan_message = "Plan could not be staged; draft unchanged"; return false; }
    editor = std::move(staged); state.curve_entry_dirty = false; sync_curve_entry(state);
    if (kind == CurvePlanKind::hard_deflatten) { state.hard_plan_staged = true; state.hard_staged_floor_mv = state.hard_floor_mv; state.hard_mod_ack = false; }
    // 1.9.1 stage_note: only the ramp and the hard de-flatten replace the note.
    if (kind == CurvePlanKind::ramp || kind == CurvePlanKind::hard_deflatten)
        state.curve_plan_note = curve_stage_note(plan.note, kind == CurvePlanKind::hard_deflatten, hard_underneath);
    state.curve_plan_message = "Staged " + std::to_string(plan.changes.size()) + " points. Review the dots before Apply.";
    if (!plan.predicted_khz.empty()) state.curve_plan_message += " Model peak: " +
        number_text(*std::max_element(plan.predicted_khz.begin(), plan.predicted_khz.end()) / 1000.0, "MHz") + " (estimate).";
    return true;
}
// Classic: 1.9.1 logs what a planner staged, untagged, and a refusal as [!!];
// the editor gains no line of its own.
void classic_stage_curve_plan(ViewState& state, CurvePlanKind kind) {
    const bool staged = stage_curve_plan(state, kind);
    if (!state.curve_plan_message.empty()) classic_log_line(state, state.curve_plan_message, staged ? std::nullopt : std::optional<bool>(false));
    state.curve_plan_message.clear();
}

// Released 1.9.1 palette (druta.py TEXT/DIM/WARN/BAD) for the Classic V/F editor.
ImVec4 rgb(int r, int g, int b, int a = 255) { return ImVec4(r / 255.0f, g / 255.0f, b / 255.0f, a / 255.0f); }
const ImVec4 py_text = rgb(230, 232, 236), py_dim = rgb(139, 144, 153), py_warn = rgb(255, 203, 71), py_bad = rgb(255, 92, 92);
const ImVec4 py_good = rgb(70, 209, 122);
constexpr const char* classic_apply_label = "Apply V/F curve to GPU";
constexpr const char* classic_hard_header = "Hard de-flatten  (needs an external voltage mod - read this before using)";

// The Classic log: session command results with the frontend's own lines
// (backend status, curve reads) placed where they were written, oldest first.
struct ClassicLogEntry { std::string text; std::optional<bool> ok; };
// One session result as 1.9.1 logs the same action.
void classic_session_lines(std::vector<ClassicLogEntry>& out, const LogEntry& entry) {
    const auto op = entry.operation;
    // 1.9.1 _timings_worker logs a timing capture only when it fails, and finds
    // the helper at launch without a log line (only Locate nvtune logs).
    const bool timing = op == Command::Operation::timings_read || op == Command::Operation::timings_capture_p0;
    if (entry.ok && (timing || (op == Command::Operation::timings_select_helper && entry.automatic))) return;
    if (timing) { out.push_back({"timings capture failed: " + entry.message, entry.ok}); return; }
    // The session's generic fallback for a write the core left unnamed.
    const std::string applied = "V/F curve applied";
    if (op == Command::Operation::reset_curve && entry.message.compare(0, applied.size(), applied) == 0) {
        out.push_back({"V/F curve reset to stock" + entry.message.substr(applied.size()), entry.ok}); return;
    }
    // Reset controls and Max it return one entry: a heading line, then one line
    // per step. 1.9.1 logs each step on its own line, [ok] or [!!], and ends
    // Reset with its summary. The entry's steps tag their own lines (a Reset
    // step without its "name: "); other lines, or an entry without steps, stay
    // untagged and the summary carries the result.
    const bool reset = op == Command::Operation::reset_controls, max = op == Command::Operation::max_it;
    const auto newline = entry.message.find('\n');
    if ((!reset && !max) || newline == std::string::npos) { out.push_back({entry.message, entry.ok}); return; }
    std::string heading = entry.message.substr(0, newline), text = entry.message.substr(newline + 1);
    if (max) {
        // "Max it applied: <first step>": the heading names the result.
        const auto colon = heading.find(": ");
        if (colon != std::string::npos) { text = heading.substr(colon + 2) + "\n" + text; heading.resize(colon); }
    }
    size_t next = 0, failed = 0;
    for (size_t start = 0; start <= text.size();) {
        // Skip a Max it step that reported nothing: it added no line.
        while (max && next < entry.steps.size() && entry.steps[next].message.empty()) ++next;
        if (next < entry.steps.size()) {
            const auto& step = entry.steps[next];
            const std::string own = reset ? step.name + ": " + step.message : step.message;
            const size_t end = start + own.size();
            // A history notice may follow the last step after "; ".
            const bool whole = end == text.size() || (end < text.size() && text[end] == '\n');
            if (text.compare(start, own.size(), own) == 0 && (whole || text.compare(end, 2, "; ") == 0)) {
                const size_t line_end = whole ? end : std::min(text.find('\n', end), text.size());
                out.push_back({(max ? "max: " : "") + step.message + text.substr(end, line_end - end), step.ok});
                failed += step.ok ? 0 : 1; ++next; start = line_end + 1; continue;
            }
        }
        const auto end = std::min(text.find('\n', start), text.size());
        if (end > start) out.push_back({(max ? "max: " : "") + text.substr(start, end - start), std::nullopt});
        start = end + 1;
    }
    if (reset && failed) out.push_back({"reset incomplete: " + std::to_string(failed) + " step(s) failed", false});
    else if (reset) out.push_back({entry.ok ? "reset to stock complete" : "reset incomplete", entry.ok});
    else out.push_back({heading, entry.ok});
}
std::vector<ClassicLogEntry> classic_log_entries(const ViewState& state) {
    std::vector<ClassicLogEntry> out;
    if (!state.live) return out;
    const auto& own = state.classic_log_lines;
    size_t next = 0;
    for (const auto& entry : state.live->log) {
        for (; next < own.size() && own[next].after_log_id < entry.id; ++next) out.push_back({own[next].text, own[next].ok});
        classic_session_lines(out, entry);
    }
    for (; next < own.size(); ++next) out.push_back({own[next].text, own[next].ok});
    return out;
}
std::string classic_log_text(const ClassicLogEntry& entry) {
    return (!entry.ok ? "" : *entry.ok ? "[ok] " : "[!!] ") + entry.text;
}
void classic_log(const ViewState& state);

// 1.9.1 apply_plan: one plan for the banner before the click and the log's
// receipt at it.
CurvePlanBanner classic_plan(const ViewState& state, const std::string& rephase_label) {
    CurvePlanBannerInput input;
    input.cap_mv = state.curve_cap_mv; input.table_rows = state.live->data.curve.size();
    input.apply_label = classic_apply_label; input.rephase_label = rephase_label;
    // 1.9.1 _plan_note: the ramp or hard de-flatten that was staged last, for as long as edits stay staged.
    input.note_hard = state.hard_plan_staged && state.curve_draft.dirty();
    if (state.curve_draft.dirty()) input.note = state.curve_plan_note;
    return curve_plan_banner(state.curve_draft.baseline(), state.curve_draft.draft(), input);
}
// 1.9.1 vf_apply logs plan["warn"] + "writing " + plan["text"] (+ ". " + the
// planner's note) before the write, [!!] when the plan lowers the peak or is a
// hard de-flatten: the banner's first body line, reworded. Empty with nothing staged.
std::optional<ClassicLogEntry> classic_apply_receipt(const ViewState& state, const std::string& rephase_label) {
    const auto banner = classic_plan(state, rephase_label);
    const auto line = banner.body.substr(0, banner.body.find('\n'));
    const auto writes = line.find("Writes ");
    if (banner.band == CurvePlanBand::idle || writes == std::string::npos) return std::nullopt;
    auto text = line.substr(0, writes) + "writing " + line.substr(writes + 7);
    if (!text.empty() && text.back() == '.') text.pop_back();
    if (state.curve_draft.dirty() && !state.curve_plan_note.empty()) text += ". " + state.curve_plan_note;
    return ClassicLogEntry{text, banner.band == CurvePlanBand::bad ? std::optional<bool>(false) : std::nullopt};
}

// 1.9.1 vf_plan: a bordered box that always says what one click on Apply writes.
void classic_plan_banner(const ViewState& state, const std::string& rephase_label) {
    const auto banner = classic_plan(state, rephase_label);
    const ImVec4 head = banner.band == CurvePlanBand::idle ? py_dim : banner.band == CurvePlanBand::warn ? py_warn : py_bad;
    const ImVec4 fill = banner.band == CurvePlanBand::idle ? rgb(32, 35, 42) : banner.band == CurvePlanBand::warn ? rgb(58, 44, 10) : rgb(66, 20, 20);
    const ImVec4 border = banner.band == CurvePlanBand::idle ? rgb(72, 78, 92) : head;
    ImGui::PushStyleColor(ImGuiCol_ChildBg, fill); ImGui::PushStyleColor(ImGuiCol_Border, border);
    ImGui::PushStyleVar(ImGuiStyleVar_ChildBorderSize, classic_px(2));
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(classic_px(12), classic_px(8)));
    if (ImGui::BeginChild("vf_plan", ImVec2(0, 0), ImGuiChildFlags_Borders | ImGuiChildFlags_AutoResizeY
        | ImGuiChildFlags_AlwaysUseWindowPadding, ImGuiWindowFlags_NoScrollWithMouse | ImGuiWindowFlags_NoScrollbar)) {
        // Dear PyGui draws each add_text on a frame-height line, the text 3 px
        // down: the heading's line is its 26 px font plus the frame padding.
        ImGui::PushFont(classic_fonts.heading, 26.0f);
        ImGui::AlignTextToFramePadding();
        ImGui::PushStyleColor(ImGuiCol_Text, head); ImGui::TextWrapped("%s", banner.head.c_str()); ImGui::PopStyleColor();
        ImGui::PopFont();
        // 1.9.1 sizes this box from measured text plus 5 px per line (plan_h, 119
        // px idle at 1x): the body and reset lines also sit 3 px down on their
        // lines, and a strip of empty colour stays under the reset line.
        ImGui::SetCursorPosY(ImGui::GetCursorPosY() + classic_px(3));
        ImGui::PushStyleColor(ImGuiCol_Text, py_text); ImGui::TextWrapped("%s", banner.body.c_str()); ImGui::PopStyleColor();
        ImGui::SetCursorPosY(ImGui::GetCursorPosY() + classic_px(3));
        ImGui::PushStyleColor(ImGuiCol_Text, py_dim); ImGui::TextWrapped("%s", banner.reset.c_str()); ImGui::PopStyleColor();
        ImGui::Dummy(ImVec2(0, std::max(0.0f, classic_px(9) - ImGui::GetStyle().ItemSpacing.y)));
    }
    ImGui::EndChild();
    ImGui::PopStyleVar(2); ImGui::PopStyleColor(2);
}

void classic_tooltip(const char* text) {
    if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled)) ImGui::SetTooltip("%s", text);
}

// The height 1.9.1 relayout shares out: one H for every page, so the Control
// page uses the Timings page's rule (client height less menu_h(), the s(28)
// tab selector and the s(28) gate bar; 80 px at 1x).
float classic_curve_share_height() {
    return classic_timing_share_height();
}

// Released 1.9.1 build_vf order: cap row, ramp-floor row, hard de-flatten,
// statistics and status, the plan banner, the selected-point row with Apply,
// then the plot. Native-only tools sit in one collapsed header. Every button
// sends exactly what the Focused editor's equivalent sends.
void classic_curve_workspace(ViewState& state, bool embedded) {
    const auto& snapshot = *state.live;
    const auto& data = snapshot.data;
    auto& editor = state.curve_draft;
    const auto& points = editor.draft();
    const bool can_plan = curve_plan_possible(state);
    const bool ready = data.connected && !snapshot.busy && !snapshot.opening
        && !snapshot.curve_stale && snapshot.curve_revision != 0 && data.curve_freq_div != 0 && state.controls_unlocked;
    std::optional<CurvePlanKind> kind;
    char rephase[96]{};
    if (data.curve_step_khz) std::snprintf(rephase, sizeof(rephase), "Re-phase to %.4g MHz increments", data.curve_step_khz / 1000.0);
    else std::snprintf(rephase, sizeof(rephase), "Re-phase (clock grid unavailable)");
    // 1.9.1 build_vf tooltips: gs is this card's clock grid; "--" until it is known.
    const auto text_of = [](const char* format, auto... values) {
        char buffer[2048]{}; std::snprintf(buffer, sizeof(buffer), format, values...); return std::string(buffer);
    };
    const std::string gs = data.curve_step_khz ? text_of("%.4g", data.curve_step_khz / 1000.0) : std::string("--");

    // 1.9.1 snaps an edited voltage DOWN onto the 6.25 mV grid and clamps it;
    // the floors are bounded to this curve's own voltages, as vf_read does.
    const auto curve_bound = [&](double ramp, double hard) { return curve_editor_seed(editor.baseline(), ramp, hard); };
    ImGui::AlignTextToFramePadding(); ImGui::TextUnformatted("voltage cap (mV)"); ImGui::SameLine();
    ImGui::SetNextItemWidth(classic_px(130));
    if (ImGui::InputDouble("##vcap", &state.curve_cap_mv, curve_voltage_grid_mv, 0, "%.2f"))
        state.curve_cap_mv = curve_snap_mv(state.curve_cap_mv, curve_cap_min_mv, curve_cap_max_mv);
    const auto cap_tip = text_of("Voltage ceiling for the deflattener (clamped %.0f-%.0f mV).\n"
        "VF points are %.4g mV apart, so the reachable top is\n"
        "the highest point at or below this value - and editing\n"
        "this box snaps it DOWN onto that same %.4g mV grid, so\n"
        "+/- always lands on a voltage a point really has.",
        curve_cap_min_mv, curve_cap_max_mv, curve_voltage_grid_mv, curve_voltage_grid_mv);
    classic_tooltip(cap_tip.c_str());
    ImGui::SameLine(); ImGui::BeginDisabled(snapshot.busy || snapshot.opening);
    if (ImGui::Button("Read curve", ImVec2(classic_px(110), 0))) {
        // 1.9.1 vf_read: re-reading rebases the editor and throws staged edits
        // away with no undo, so with edits staged the first press only warns.
        const auto pending = curve_pending_edits(editor.baseline(), points);
        if (pending && !state.classic_discard_armed) {
            state.classic_discard_armed = true;
            classic_log_line(state, std::to_string(pending) + " unapplied edit(s) would be discarded - press Read curve again to confirm, or Apply first", false);
        } else {
            state.classic_discard_armed = false;
            state.refresh_requested = state.curve_read_requested = true; state.curve_read_reads = snapshot.completed_reads;
        }
    }
    ImGui::EndDisabled(); ImGui::SameLine();
    ImGui::BeginDisabled(!can_plan);
    if (ImGui::Button(rephase, ImVec2(classic_px(350), 0))) kind = CurvePlanKind::rephase;
    ImGui::EndDisabled();
    const auto rephase_tip = "Puts every point back on ONE " + gs + " MHz phase - this\n"
        "card's own grid, read from the driver's lockable-clock\n"
        "table, not a fixed 15 MHz.\n\n"
        "The clock is floor((base+delta)/step)*step, so two points\n"
        "only move together when their deltas share a remainder\n"
        "mod the step. A stray point crosses bin boundaries at a\n"
        "different offset and silently re-creates a flat.\n\n"
        "Off-phase deltas are rounded DOWN, never up, so a point\n"
        "can only drop, by at most " + gs + " MHz.\n\n"
        "Rephase is NOT a reset function.\n"
        "If every point already agrees, it does nothing.";
    classic_tooltip(rephase_tip.c_str());
    ImGui::SameLine();
    if (ImGui::Button("Fit view", ImVec2(classic_px(90), 0))) state.curve_fit_requested = true;
    ImGui::SameLine();
    ImGui::BeginDisabled(!ready || !data.curve_reset_writable || data.curve.empty() || data.curve.size() > DC_VFP_POINTS);
    if (ImGui::Button("Reset curve to stock", ImVec2(classic_px(170), 0))) {
        // 1.9.1 vf_reset says what the reset discards before it writes.
        classic_log_line(state, "zeroing all " + std::to_string(data.curve.size()) + " V/F deltas back to the factory curve, discarding "
            + std::to_string(curve_pending_edits(editor.baseline(), points)) + " staged edit(s)", std::nullopt);
        Command command;
        command.operation = Command::Operation::reset_curve; command.expected_curve = data.curve;
        command.expected_curve_freq_div = data.curve_freq_div; state.commands.push_back(std::move(command));
    }
    ImGui::EndDisabled();

    ImGui::AlignTextToFramePadding(); ImGui::TextUnformatted("ramp floor (mV)"); ImGui::SameLine();
    ImGui::SetNextItemWidth(classic_px(130));
    if (ImGui::InputDouble("##rfloor", &state.curve_floor_mv, curve_voltage_grid_mv, 0, "%.2f")) {
        state.curve_floor_mv = curve_snap_mv(state.curve_floor_mv, curve_floor_min_mv, curve_floor_max_mv);
        if (const auto bounded = curve_bound(state.curve_floor_mv, state.hard_floor_mv).ramp_floor_mv) state.curve_floor_mv = *bounded;
    }
    const auto floor_tip = text_of("Bottom of the band the ramp rebuilds. Snapped DOWN onto\n"
        "the same %.4g mV grid as the voltage cap, and clamped to\n"
        "the voltages this curve actually has.\n\n"
        "Everything BELOW this is left alone: the low-voltage floor\n"
        "is many points pinned at the minimum clock, and ramping\n"
        "them means demanding high clocks at tiny voltages.\n\n"
        "The wider the band, the more the top clips - and a clipped\n"
        "ramp pays for it at the floor. The staged plan says by how\n"
        "much, in MHz, before you press Apply.", curve_voltage_grid_mv);
    classic_tooltip(floor_tip.c_str());
    ImGui::SameLine(); ImGui::BeginDisabled(!can_plan);
    if (ImGui::Button("De-flatten", ImVec2(classic_px(150), 0))) kind = CurvePlanKind::ramp;
    ImGui::EndDisabled();
    // The released text's TU102 point count is one card's measurement: left out.
    const auto ramp_tip = "THE DE-FLATTEN TOOL. Stages a plan (nothing is written\n"
        "yet): rebuilds every point from the floor up to the cap\n"
        "as a STRICTLY INCREASING " + gs + " MHz ramp - one distinct\n"
        "frequency per voltage point, no ties.\n\n"
        "A throttling card walks LEFT down the curve until it is\n"
        "under budget, and the arbiter can only sit at the LOWEST\n"
        "voltage carrying each frequency - so every flat run is a\n"
        "voltage band it cannot use at all.\n\n"
        "The cap point takes the highest allowed clock and each\n"
        "point below it drops one " + gs + " MHz bin. Where the cap\n"
        "point is ALREADY at the hardware maximum there is nothing\n"
        "to raise into, so the band shrinks to the rungs that fit\n"
        "rather than sliding down and being flattened - the plan\n"
        "says how many were dropped and why.\n\n"
        "Where it does have headroom this is an OVERCLOCK as well\n"
        "as a granularity fix - every rung asks for more clock at\n"
        "its voltage than stock did, so every rung has to be\n"
        "stable in its own right.\n\n"
        "'Limited de-flatten' (Clocks menu) is the narrow version:\n"
        "it only makes the cap point the unique top and leaves the\n"
        "rest of the band alone.";
    classic_tooltip(ramp_tip.c_str());

    // Open blocks above the plot take their height from the plot, as 1.9.1's
    // hard_block_h does, so the banner and Apply stay on screen together.
    float open_blocks = 0;
    if (ClassicSection hard{classic_hard_header, 1}) {
        const float top = ImGui::GetCursorPosY();
        ImGui::Checkbox("I have a functional hardware voltage mod on this card, and `refin_adj` (or this board's "
            "equivalent circuit) is completely nonoperational", &state.hard_mod_ack);
        // 1.9.1's text; this build clears the tick when the plan is staged as well.
        classic_tooltip("This tick is never remembered. It starts clear every\n"
            "session - nothing in this app writes it to disk - and it\n"
            "clears itself again as soon as the plan it authorised is\n"
            "staged, written or dropped, so one tick buys exactly one\n"
            "hard de-flatten.\n\n"
            "While it is set, the hard floor is drawn on the plot in\n"
            "red, so 'this card is armed for a mode that assumes a\n"
            "soldering iron' is visible without opening this header.");
        char repair[48]{};
        if (data.curve_model_max_rise_khz > 0) std::snprintf(repair, sizeof(repair), "the %.4g MHz", data.curve_model_max_rise_khz / 1000.0);
        else std::snprintf(repair, sizeof(repair), "the model's");
        ImGui::PushStyleColor(ImGuiCol_Text, py_bad);
        ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + classic_px(1100));
        ImGui::Text("Without it the card really IS at the floor voltage, cannot hold the flat top, is asked for high clocks "
            "hundreds of mV lower by %s repair, and crashes the driver.", repair);
        ImGui::PopTextWrapPos(); ImGui::PopStyleColor();
        ImGui::AlignTextToFramePadding(); ImGui::TextUnformatted("floor (mV)"); ImGui::SameLine();
        ImGui::SetNextItemWidth(classic_px(130));
        if (ImGui::InputDouble("##hdf_floor", &state.hard_floor_mv, curve_voltage_grid_mv, 0, "%.2f")) {
            state.hard_floor_mv = curve_snap_mv(state.hard_floor_mv, curve_floor_min_mv, curve_floor_max_mv);
            if (const auto bounded = curve_bound(state.curve_floor_mv, state.hard_floor_mv).hard_floor_mv) state.hard_floor_mv = *bounded;
        }
        const auto hard_floor_tip = text_of("The voltage the GPU will BELIEVE it is running at, and\n"
            "the voltage the card will park at. Everything at or\n"
            "above it is flattened onto one frequency; the arbiter\n"
            "runs the lowest voltage of a flat run, so that bottom\n"
            "point is where it lands.\n\n"
            "%.2f by default. Lower is allowed and lowers the\n"
            "believed voltage further - it also deepens the cascade\n"
            "the driver drags out from under the floor, which the\n"
            "staged plan states in points and MHz.", curve_hard_floor_default_mv);
        classic_tooltip(hard_floor_tip.c_str());
        ImGui::SameLine(); ImGui::TextUnformatted("   target (MHz)"); ImGui::SameLine();
        ImGui::SetNextItemWidth(classic_px(130));
        const double target_step = std::max(1.0, std::round(data.curve_step_khz / 1000.0));
        if (ImGui::InputScalar("##hdf_target", ImGuiDataType_Double, &state.hard_target_mhz, &target_step, nullptr, "%.0f")
            && std::isfinite(state.hard_target_mhz))
            state.hard_target_mhz = curve_snap_target_mhz(curve_clamp_entry_mhz(state.hard_target_mhz,
                data.frequency.minimum_mhz, data.frequency.maximum_mhz), data.curve_step_khz);
        const auto target_tip = "The single frequency every point at or above the floor\n"
            "is set to. Snapped DOWN to the " + gs + " MHz grid - a mid-bin\n"
            "target floors on evaluation and the ONE frequency the\n"
            "whole mechanism depends on would quietly become two.\n\n"
            "It has to be high enough to hold the card in P0, so it\n"
            "is seeded from the curve's own peak on every read.";
        classic_tooltip(target_tip.c_str());
        ImGui::SameLine();
        ImGui::BeginDisabled(!can_plan || !state.planner_api.hard_deflatten || !state.hard_mod_ack);
        if (ImGui::Button("Hard de-flatten", ImVec2(classic_px(170), 0))) kind = CurvePlanKind::hard_deflatten;
        ImGui::EndDisabled();
        // The released text's 72 points and 800 mV are one card's: named by role here.
        classic_tooltip("Stages a plan (nothing is written yet): sets EVERY\n"
            "point at or above the floor to the target frequency -\n"
            "deliberately making the curve completely flat.\n\n"
            "THE OPPOSITE OF THE RAMP, on purpose. The ramp removes\n"
            "flats so a throttling card has fine steps to walk down.\n"
            "This builds the biggest flat it can, because the arbiter\n"
            "runs the LOWEST voltage of a peak-frequency flat run:\n"
            "flatten every point above the floor onto one frequency\n"
            "and the card asks for that frequency at the bottom of\n"
            "the run.\n\n"
            "The point is not performance through granularity, it is\n"
            "DECEIVING THE POWER ESTIMATOR: the GPU believes it is at\n"
            "the floor voltage, computes low power from that belief and\n"
            "stops throttling - while the real rail is driven externally\n"
            "by the hard mod and is invisible to all GPU software,\n"
            "including this app.\n\n"
            "Needs the acknowledgement above ticked. Without the mod\n"
            "the card really is at the floor voltage and this will\n"
            "crash it.");
        open_blocks += ImGui::GetCursorPosY() - top;
    }
    if (ClassicSection more{"More V/F tools", 1}) {
        const float top = ImGui::GetCursorPosY();
        if (embedded && ImGui::Button("Open V/F curve in a separate window")) {
            end_curve_drag(state);
            state.curve_window_open = true; state.curve_window_focus_requested = true;
        }
        ImGui::BeginDisabled(!editor.can_undo());
        if (ImGui::Button("Undo edit")) curve_history_step(state, false);
        ImGui::EndDisabled(); ImGui::SameLine();
        ImGui::BeginDisabled(!editor.can_redo());
        if (ImGui::Button("Redo edit")) curve_history_step(state, true);
        ImGui::EndDisabled(); ImGui::SameLine();
        ImGui::BeginDisabled((!state.controls_unlocked&&!data.undo_metadata_only) || (!data.connected&&!data.undo_metadata_only) || !data.undo_available || snapshot.busy || snapshot.opening);
        if (ImGui::Button(data.undo_metadata_only?"Undo label update":"Undo hardware write")) state.commands.push_back({Command::Operation::undo, ControlId::core, 0});
        ImGui::EndDisabled(); ImGui::SameLine();
        ImGui::BeginDisabled(snapshot.busy || !data.connected || snapshot.curve_stale || points.empty());
        if (ImGui::Button("Profiles...")) ImGui::OpenPopup("curve-profiles");
        if (ImGui::BeginPopup("curve-profiles")) {
            if (ImGui::MenuItem("Save configured curve...")) state.save_curve_profile_requested = true;
            hint("Save fresh configured V/F requests. Unsaved local edits are not included.");
            if (ImGui::MenuItem("Load curve to draft...", nullptr, false, !editor.dirty() && !state.curve_entry_dirty)) state.load_curve_profile_requested = true;
            hint("Validate the saved adapter and curve geometry, then stage for review. Apply writes explicitly. Finish or revert pending edits first.");
            ImGui::EndPopup();
        }
        ImGui::EndDisabled();
        ImGui::BeginDisabled(!can_plan);
        if (ImGui::Button("Limited de-flatten \xE2\x89\xA4 cap")) kind = CurvePlanKind::deflatten;
        ImGui::EndDisabled();
        classic_tooltip("Stages only the narrow plan: makes the cap point the unique top and leaves the rest of\n"
            "the band alone. De-flatten rebuilds the whole band and is the one to reach for first.");
        ImGui::BeginDisabled(!can_hold_selected(state));
        if (ImGui::Button("Hold selected point")) queue_selected_hold(state);
        ImGui::EndDisabled(); ImGui::SameLine(); ImGui::Checkbox("Hold headroom",&state.hold_headroom);
        ImGui::SameLine(); ImGui::SetNextItemWidth(u(85));
        if (ImGui::InputDouble("Margin (mV)",&state.headroom_margin_mv,0,0,"%.3f")) state.headroom_margin_mv = clamp_headroom_margin_mv(state.headroom_margin_mv);
        if (!embedded) ImGui::Checkbox("Unlock tuning controls", &state.controls_unlocked);
        ImGui::TextWrapped("Set MHz floors onto the captured point's clock phase. Drag snaps half up; voltage stays fixed. Empty left-drag pans; wheel zooms. Ctrl+Shift+Z also redoes local edits (64 deep). Closing keeps the draft. Apply and hardware Undo are separate explicit commands.");
        ImGui::TextWrapped("Current card grid: %.9g MHz. Request divisor: %u. Draft frequency predicts a request, not a measurement.",
            data.curve_step_khz / 1000.0, editor.freq_div());
        if (!data.curve_error.empty()) ImGui::TextWrapped("%s", data.curve_error.c_str());
        // The native curve-profile result stays with the tool that made it.
        if (!state.curve_profile_status.empty()) ImGui::TextWrapped("%s", state.curve_profile_status.c_str());
        open_blocks += ImGui::GetCursorPosY() - top;
    }
    if (kind) classic_stage_curve_plan(state, *kind);
    if (!editor.dirty()) { state.hard_plan_staged = false; state.curve_plan_note.clear(); }

    // 1.9.1 add_text lines sit on frame-height rows (AlignTextToFramePadding).
    const float wrap = ImGui::GetCursorPosX() + std::max(classic_px(300), ImGui::GetContentRegionAvail().x - classic_px(20));
    // vf_info: the statistics, or after a failed read 1.9.1's WARN sentence
    // over the previous read (or none) with the read's error.
    std::string info = curve_info_text(editor.baseline(), points, state.curve_cap_mv);
    const auto curve_error = data.curve_error.empty() ? editor.status() : data.curve_error;
    const bool read_failed = snapshot.curve_stale || (points.empty() && !snapshot.opening && !data.curve_error.empty());
    if (read_failed) info = (points.empty() ? "Curve unavailable: " : "Curve refresh failed; showing previous read: ") + curve_error;
    ImGui::PushStyleColor(ImGuiCol_Text, editor.dirty() || read_failed ? py_warn : py_dim); ImGui::PushTextWrapPos(wrap);
    ImGui::AlignTextToFramePadding(); ImGui::TextUnformatted(info.empty() ? "--" : info.c_str());
    ImGui::PopTextWrapPos(); ImGui::PopStyleColor();
    // vf_status: 1.9.1 mirrors the newest log line here, green for [ok], red for
    // [!!], DIM untagged. Editor feedback reaches it through the log, so the rows
    // below never move; the hold state is the line under the read-out.
    // A multi-line entry shows its last line only, so the rows below keep their place.
    const auto log = classic_log_entries(state);
    if (!log.empty()) {
        const auto& last = log.back();
        const auto newline = last.text.rfind('\n');
        const auto mirror = classic_log_text({newline == std::string::npos ? last.text : last.text.substr(newline + 1), last.ok});
        ImGui::PushStyleColor(ImGuiCol_Text, !last.ok ? py_dim : *last.ok ? py_good : py_bad); ImGui::PushTextWrapPos(wrap);
        ImGui::AlignTextToFramePadding(); ImGui::TextUnformatted(mirror.c_str());
        ImGui::PopTextWrapPos(); ImGui::PopStyleColor();
    }
    classic_plan_banner(state, rephase);
    const float banner_height = ImGui::GetItemRectSize().y;

    if (!points.empty()) {
        const auto selected = std::find_if(points.begin(), points.end(), [&](const auto& point) { return point.idx == state.selected_curve_point; });
        ImGui::AlignTextToFramePadding(); ImGui::TextUnformatted("selected"); ImGui::SameLine();
        int shown = static_cast<int>(state.selected_curve_point);
        ImGui::SetNextItemWidth(classic_px(110));
        if (ImGui::InputInt("##vf_idx", &shown, 1, 10)) {
            const auto next = curve_nearest_index(points, shown, state.selected_curve_point);
            if (next && *next != state.selected_curve_point) select_curve_point(state, *next);
        }
        for (int bins : {-5,-1,1,5}) {
            ImGui::SameLine(); char label[64]{};
            std::snprintf(label,sizeof(label),"%+.9g", static_cast<double>(bins) * data.curve_step_khz / 1000.0);
            ImGui::PushID(bins);
            if (ImGui::Button(label, ImVec2(classic_px(58), 0))) nudge_selected_curve(state,bins);
            if (ImGui::IsItemHovered()) ImGui::SetTooltip("Stage %d clock bin(s), %+.9g MHz. No GPU write.", bins,
                static_cast<double>(bins) * data.curve_step_khz / 1000.0);
            ImGui::PopID();
        }
        ImGui::SameLine();
        ImGui::SetNextItemWidth(classic_px(120));
        double target = state.curve_frequency_mhz;
        const double step = data.curve_step_khz / 1000.0;
        if (ImGui::InputScalar("##MHz", ImGuiDataType_Double, &target, step > 0 ? &step : nullptr, nullptr, "%.0f")
            && std::isfinite(target)) {
            // 1.9.1 clamps entry to the adapter's supported graphics-clock range.
            state.curve_frequency_mhz = curve_clamp_entry_mhz(target, data.frequency.minimum_mhz, data.frequency.maximum_mhz);
            state.curve_entry_dirty = true;
        }
        state.curve_entry_editing = ImGui::IsItemActive();
        ImGui::SameLine();
        if (ImGui::Button("Set MHz", ImVec2(classic_px(90), 0))) {
            const double asked = state.curve_frequency_mhz;
            char label[64]{}; std::snprintf(label, sizeof(label), "set idx %u to %.0f MHz", state.selected_curve_point, asked);
            if (editor.stage_frequency_mhz(state.selected_curve_point, asked,
                data.curve_step_khz, data.curve_max_abs_delta_khz, true, label)) {
                state.curve_entry_dirty = false; sync_curve_entry(state);
                // 1.9.1 vf_set_freq: the request is floored to a bin; say where it landed.
                char line[128]{};
                std::snprintf(line, sizeof(line), "idx %u: asked %.0f -> landed %.0f MHz (%.4g MHz grid)", state.selected_curve_point,
                    asked, state.curve_frequency_mhz, data.curve_step_khz / 1000.0);
                classic_log_line(state, line, std::nullopt);
            }
        }
        ImGui::SameLine();
        // 1.9.1 vf_revert: itself one undo step (Ctrl+Z brings the edits back),
        // with no fresh read needed; the plan and its acknowledgement go.
        if (ImGui::Button("Revert edits", ImVec2(classic_px(120), 0))) {
            if (editor.revert_undoable()) { state.curve_entry_dirty = false; state.curve_entry_editing = false; sync_curve_entry(state); }
            state.hard_mod_ack = false;
        }
        ImGui::SameLine();
        // Green like 1.9.1's: the one control on this row that reaches the hardware.
        ImGui::PushStyleColor(ImGuiCol_Button, rgb(28, 92, 48));
        ImGui::PushStyleColor(ImGuiCol_ButtonHovered, rgb(40, 124, 65));
        ImGui::PushStyleColor(ImGuiCol_ButtonActive, rgb(52, 152, 82));
        ImGui::PushStyleColor(ImGuiCol_Text, rgb(232, 248, 236));
        // Full green whenever writes are possible, as in 1.9.1; a click with
        // nothing applicable writes nothing and says why.
        ImGui::BeginDisabled(!ready || !data.curve_writable);
        if (ImGui::Button(classic_apply_label, ImVec2(classic_px(300), 0))) {
            if (editor.can_apply()) {
                // 1.9.1 vf_apply: the log is the write's receipt, written at the click.
                if (auto receipt = classic_apply_receipt(state, rephase)) classic_log_line(state, std::move(receipt->text), receipt->ok);
                Command command;
                command.operation = Command::Operation::apply_curve;
                command.expected_curve = editor.baseline(); command.expected_curve_freq_div = editor.freq_div();
                command.curve_deltas = editor.changes(); state.commands.push_back(std::move(command));
            } else if (editor.dirty()) classic_log_line(state, editor.status(), false);
            else classic_log_line(state, "no edits to apply", std::nullopt); // 1.9.1 vf_apply, untagged
        }
        ImGui::EndDisabled();
        ImGui::PopStyleColor(4);
        if (selected != points.end()) {
            // 1.9.1 vf_sel_info: mono in its TEXT colour.
            push_classic_mono(); ImGui::PushStyleColor(ImGuiCol_Text, py_text);
            ImGui::AlignTextToFramePadding();
            if (editor.freq_div()) ImGui::Text("idx %u   %.2f mV   %.0f MHz   delta %+.0f MHz   |   edits pending: %zu", selected->idx,
                selected->volt_mv, selected->freq_mhz, selected->delta_khz / (1000.0 * editor.freq_div()),
                curve_pending_edits(editor.baseline(), points));
            else ImGui::Text("idx %u   %.2f mV   %.0f MHz   delta unverified   |   edits pending: %zu", selected->idx,
                selected->volt_mv, selected->freq_mhz, curve_pending_edits(editor.baseline(), points));
            ImGui::PopStyleColor(); ImGui::PopFont();
        }
    }
    // 1.9.1's DIM key help: one unwrapped line, amounts from this card's clock bin.
    ImGui::AlignTextToFramePadding();
    ImGui::TextColored(py_dim, "%s", curve_key_help(data.curve_step_khz).c_str());
    // Inline, the plot takes 1.9.1's budget so the rows, banner, plot and log
    // fit one view; the separate window keeps filling its own height.
    if (embedded) interactive_curve_plot(state, curve_plot_budget(classic_curve_share_height(), classic_px(104),
        banner_height, open_blocks, classic_px(240)));
    else interactive_curve_plot(state);
}

void live_curve_workspace(ViewState& state, bool verbose, bool embedded = false) {
    const auto& snapshot = *state.live;
    auto& editor = state.curve_draft;
    const auto& points = editor.draft();
    if (state.hard_ack_generation != snapshot.generation) {
        state.hard_ack_generation = snapshot.generation; state.hard_mod_ack = false; state.hard_plan_staged = false;
        state.curve_plan_note.clear();
    }
    if (state.presentation == Presentation::classic) { classic_curve_workspace(state, embedded); return; }
    if (!points.empty()) {
        const auto selected = std::find_if(points.begin(), points.end(), [&](const auto& point) { return point.idx == state.selected_curve_point; });
        char preview[128]{};
        if (selected != points.end()) std::snprintf(preview, sizeof(preview), "#%u | %.3f mV", selected->idx, selected->volt_mv);
        ImGui::SetNextItemWidth(u(165));
        if (ImGui::BeginCombo("Point", preview)) {
            for (const auto& point : points) {
                char name[128]{};
                std::snprintf(name, sizeof(name), "#%u | %.3f mV | %.9g MHz", point.idx, point.volt_mv, point.freq_mhz);
                if (ImGui::Selectable(name, point.idx == state.selected_curve_point)) select_curve_point(state, point.idx);
            }
            ImGui::EndCombo();
        }
        if (state.presentation == Presentation::classic) {
            for (int bins : {-5,-1,1,5}) {
                ImGui::SameLine(); char label[64]{};
                std::snprintf(label,sizeof(label),"%+.9g", static_cast<double>(bins) * snapshot.data.curve_step_khz / 1000.0);
                ImGui::PushID(bins);
                if (ImGui::Button(label)) nudge_selected_curve(state,bins);
                if (ImGui::IsItemHovered()) ImGui::SetTooltip("Stage %d clock bin(s), %+.9g MHz. No GPU write.", bins,
                    static_cast<double>(bins) * snapshot.data.curve_step_khz / 1000.0);
                ImGui::PopID();
            }
        }
        ImGui::SameLine();
        ImGui::SetNextItemWidth(u(100));
        double target = state.curve_frequency_mhz;
        if (ImGui::InputDouble("MHz", &target, 0, 0, "%.9g") && std::isfinite(target)) {
            state.curve_frequency_mhz = target; state.curve_entry_dirty = true;
        }
        state.curve_entry_editing = ImGui::IsItemActive();
        ImGui::SameLine();
        if (ImGui::Button("Set MHz")) {
            if (editor.stage_frequency_mhz(state.selected_curve_point, state.curve_frequency_mhz,
                snapshot.data.curve_step_khz, snapshot.data.curve_max_abs_delta_khz, true)) {
                state.curve_entry_dirty = false; sync_curve_entry(state);
            }
        }
        ImGui::SameLine();
        ImGui::TextColored(secondary, "Delta: %s", number_text(state.curve_delta_mhz, "MHz").c_str());
    }
    ImGui::BeginDisabled(!editor.can_undo());
    if (ImGui::Button("Undo edit")) { editor.undo_edit(); state.curve_entry_dirty = false; sync_curve_entry(state); }
    ImGui::EndDisabled(); ImGui::SameLine();
    ImGui::BeginDisabled(!editor.can_redo());
    if (ImGui::Button("Redo edit")) { editor.redo_edit(); state.curve_entry_dirty = false; sync_curve_entry(state); }
    ImGui::EndDisabled(); ImGui::SameLine();
    if (ImGui::Button("Revert local curve")) {
        if (editor.revert()) { state.curve_entry_dirty = false; state.curve_entry_editing = false; sync_curve_entry(state); }
    }
    ImGui::SameLine();
    if (ImGui::Button("Fit view")) state.curve_fit_requested = true;
    ImGui::SameLine();
    ImGui::BeginDisabled(snapshot.busy || snapshot.opening);
    if (ImGui::Button("Refresh curve")) state.refresh_requested = true;
    ImGui::SameLine();
    ImGui::BeginDisabled(snapshot.busy || !snapshot.data.connected || snapshot.curve_stale || points.empty());
    if (ImGui::Button("Profiles...")) ImGui::OpenPopup("curve-profiles");
    if (ImGui::BeginPopup("curve-profiles")) {
        if (ImGui::MenuItem("Save configured curve...")) state.save_curve_profile_requested = true;
        hint("Save fresh configured V/F requests. Unsaved local edits are not included.");
        if (ImGui::MenuItem("Load curve to draft...", nullptr, false, !editor.dirty() && !state.curve_entry_dirty)) state.load_curve_profile_requested = true;
        hint("Validate the saved adapter and curve geometry, then stage for review. Apply writes explicitly. Finish or revert pending edits first.");
        ImGui::EndPopup();
    }
    ImGui::EndDisabled();
    ImGui::EndDisabled();
    if (!state.curve_profile_status.empty()) ImGui::TextWrapped("%s", state.curve_profile_status.c_str());
    if (!embedded) {
        ImGui::Checkbox("Unlock tuning controls", &state.controls_unlocked);
        ImGui::SameLine();
    }
    const bool ready = snapshot.data.connected && !snapshot.busy && !snapshot.opening
        && !snapshot.curve_stale && snapshot.curve_revision != 0 && snapshot.data.curve_freq_div != 0 && state.controls_unlocked;
    ImGui::BeginDisabled(!ready || !snapshot.data.curve_writable || !editor.can_apply());
    if (ImGui::Button("Apply curve")) {
        Command command;
        command.operation = Command::Operation::apply_curve;
        command.expected_curve = editor.baseline(); command.expected_curve_freq_div = editor.freq_div();
        command.curve_deltas = editor.changes(); state.commands.push_back(std::move(command));
    }
    ImGui::EndDisabled(); ImGui::SameLine();
    ImGui::BeginDisabled(!ready || !snapshot.data.curve_reset_writable || snapshot.data.curve.empty() || snapshot.data.curve.size() > DC_VFP_POINTS);
    if (ImGui::Button("Reset curve to stock")) {
        Command command;
        command.operation = Command::Operation::reset_curve; command.expected_curve = snapshot.data.curve;
        command.expected_curve_freq_div = snapshot.data.curve_freq_div; state.commands.push_back(std::move(command));
    }
    ImGui::EndDisabled(); ImGui::SameLine();
    ImGui::BeginDisabled((!state.controls_unlocked&&!snapshot.data.undo_metadata_only) || (!snapshot.data.connected&&!snapshot.data.undo_metadata_only) || !snapshot.data.undo_available || snapshot.busy || snapshot.opening);
    if (ImGui::Button(snapshot.data.undo_metadata_only?"Undo label update":"Undo hardware write")) state.commands.push_back({Command::Operation::undo, ControlId::core, 0});
    ImGui::EndDisabled();
    if (ImGui::CollapsingHeader("Curve shape tools", embedded ? ImGuiTreeNodeFlags_DefaultOpen : 0)) {
        ImGui::SetNextItemWidth(u(100)); ImGui::InputDouble("Cap (mV)", &state.curve_cap_mv, 0, 0, "%.3f");
        ImGui::SameLine(); ImGui::SetNextItemWidth(u(100)); ImGui::InputDouble("Ramp floor (mV)", &state.curve_floor_mv, 0, 0, "%.3f");
        std::optional<CurvePlanKind> kind;
        const bool can_plan = curve_plan_possible(state);
        ImGui::BeginDisabled(!can_plan);
        if (ImGui::Button("Stage de-flatten")) kind = CurvePlanKind::deflatten;
        ImGui::SameLine(); if (ImGui::Button("Stage ramp")) kind = CurvePlanKind::ramp;
        ImGui::SameLine(); if (ImGui::Button("Stage re-phase")) kind = CurvePlanKind::rephase;
        ImGui::EndDisabled();
        if (ImGui::CollapsingHeader("Hard de-flatten (needs an external voltage mod)")) {
            ImGui::SetNextItemWidth(u(100)); ImGui::InputDouble("Hard floor (mV)", &state.hard_floor_mv, 0, 0, "%.3f");
            ImGui::SameLine(); ImGui::SetNextItemWidth(u(100)); ImGui::InputDouble("Hard target (MHz)", &state.hard_target_mhz, 0, 0, "%.3f");
            ImGui::Checkbox("I have the external voltage modification required for hard de-flatten", &state.hard_mod_ack);
            ImGui::TextWrapped("Hard de-flatten requests one clock for all points at or above the floor. It requires an external voltage modification; without it the requested clocks may be unstable or crash the driver. Effects below the floor are model estimates, not measurements.");
            ImGui::BeginDisabled(!can_plan || !state.planner_api.hard_deflatten || !state.hard_mod_ack);
            if (ImGui::Button("Stage hard de-flatten")) kind = CurvePlanKind::hard_deflatten;
            ImGui::EndDisabled();
        }
        hint("Shape tools stage local edits. Prediction uses the backend's software model; Apply and readback establish the stored request.");
        if (kind) stage_curve_plan(state, *kind);
        if (!state.curve_plan_message.empty()) ImGui::TextWrapped("%s", state.curve_plan_message.c_str());
    }
    if (state.presentation != Presentation::classic) curve_keyboard(state);
    if (!editor.dirty()) { state.hard_plan_staged = false; state.curve_plan_note.clear(); }
    if (editor.dirty()) {
        const auto& baseline = editor.baseline();
        double before_peak = 0, after_peak = 0; std::size_t changed_points = 0;
        for (const auto& point : points) {
            after_peak = std::max(after_peak, point.freq_mhz);
            const auto previous = std::find_if(baseline.begin(), baseline.end(), [&](const auto& p) { return p.idx == point.idx; });
            if (previous != baseline.end()) { before_peak = std::max(before_peak, previous->freq_mhz); if (previous->delta_khz != point.delta_khz) ++changed_points; }
        }
        ImGui::TextColored(amber, "Apply would request %zu changed point(s). Curve peak: %.3f -> %.3f MHz (requested model, not measured).", changed_points, before_peak, after_peak);
        if (state.hard_plan_staged) {
            ImGui::TextColored(amber, "Hard de-flatten remains in this draft; external voltage modification is required. Revert to discard the draft.");
            const auto estimate = estimate_below_floor(state.planner_api, baseline, points, state.hard_staged_floor_mv,
                snapshot.data.curve_model_max_rise_khz);
            if (estimate.available) {
                ImGui::TextWrapped("Model estimate below %.3f mV: %zu point(s) raised%s", state.hard_staged_floor_mv, estimate.raised, estimate.raised ? ":" : ".");
                if (estimate.raised) ImGui::TextWrapped("Lowest affected %.3f mV; largest increase %.3f MHz. These are predictions, not measurements.",
                    estimate.lowest_mv, estimate.largest_rise_mhz);
            } else ImGui::TextWrapped("Below-floor model estimate unavailable; physical response is unknown.");
        }
    }
    ImGui::BeginDisabled(!can_hold_selected(state));
    if (ImGui::Button("Hold selected point")) queue_selected_hold(state);
    ImGui::EndDisabled(); ImGui::SameLine(); ImGui::Checkbox("Hold headroom",&state.hold_headroom);
    ImGui::SameLine(); ImGui::SetNextItemWidth(u(85));
    if (ImGui::InputDouble("Margin (mV)",&state.headroom_margin_mv,0,0,"%.3f")) state.headroom_margin_mv = clamp_headroom_margin_mv(state.headroom_margin_mv);
    hold_status(snapshot);
    const char* status = snapshot.curve_stale ? "Curve stale; edits retained" : editor.status().c_str();
    ImGui::TextColored(editor.dirty() ? amber : secondary, "%s", status);
    ImGui::TextColored(muted, "Drag dots | A/D select | W/S +/- %.9g MHz | Shift x3 | Ctrl+Z / Y | Ctrl+H hold / release",
        snapshot.data.curve_step_khz / 1000.0);
    if (ImGui::CollapsingHeader("Curve details")) {
        ImGui::TextWrapped("Set MHz floors onto the captured point's clock phase. Drag snaps half up; voltage stays fixed. Empty left-drag pans; wheel zooms. Ctrl+Shift+Z also redoes local edits (64 deep). Closing keeps the draft. Apply and hardware Undo are separate explicit commands.");
        ImGui::TextWrapped("Current card grid: %.9g MHz. Request divisor: %u. Draft frequency predicts a request, not a measurement.",
            snapshot.data.curve_step_khz / 1000.0, editor.freq_div());
        if (verbose) ImGui::TextWrapped("%s", snapshot.data.curve_error.c_str());
    }
    if (!snapshot.log.empty()) {
        const auto& last = snapshot.log.back();
        ImGui::TextColored(last.ok ? secondary : amber, "%s", last.message.c_str());
        hint(last.message.c_str());
    }
    interactive_curve_plot(state);
}

void draw_curve_window(ViewState& state) {
    if (state.curve_drag_point && (!ImGui::IsMouseDown(ImGuiMouseButton_Left) || ImGui::GetIO().AppFocusLost))
        end_curve_drag(state);
    if (!state.curve_window_open) { if (!state.curve_inline_active) end_curve_drag(state); return; }
    const auto display = ImGui::GetIO().DisplaySize;
    const ImVec2 maximum(std::max(320.0f, display.x - u(16)), std::max(240.0f, display.y - u(16)));
    ImGui::SetNextWindowSizeConstraints(ImVec2(std::min(u(640), maximum.x), std::min(u(480), maximum.y)), maximum);
    ImGui::SetNextWindowSize(ImVec2(maximum.x - u(12), maximum.y - u(12)), ImGuiCond_FirstUseEver);
    ImGui::SetNextWindowPos(ImVec2(display.x * 0.5f, display.y * 0.5f), ImGuiCond_FirstUseEver, ImVec2(0.5f, 0.5f));
    if (state.curve_window_focus_requested) { ImGui::SetNextWindowFocus(); state.curve_window_focus_requested = false; }
    if (ImGui::Begin("V/F curve editor###druta_curve_editor", &state.curve_window_open, ImGuiWindowFlags_NoSavedSettings)) {
        if (state.preview) { label("Preview / sample data. No GPU session is connected."); vf_plot(state, ImGui::GetContentRegionAvail().y); }
        else if (state.live) live_curve_workspace(state, state.presentation == Presentation::classic);
        else ImGui::TextUnformatted("Waiting for a fresh curve...");
    }
    ImGui::End();
    if (!state.curve_window_open) end_curve_drag(state);
}

void live_identity(const SessionSnapshot& snapshot) {
    const auto& identity = snapshot.data.identity;
    ImGui::TextWrapped("Name: %s", identity.name[0] ? identity.name : "--");
    ImGui::TextWrapped("Slot: %s", snapshot.selected_slot.empty() ? "--" : snapshot.selected_slot.c_str());
    ImGui::TextWrapped("UUID: %s", identity.uuid[0] ? identity.uuid : "--");
    ImGui::TextWrapped("Driver: %s", identity.driver[0] ? identity.driver : "--");
    ImGui::TextWrapped("VBIOS: %s", identity.vbios[0] ? identity.vbios : "--");
    ImGui::Text("NVAPI: %s | NVML: %s", identity.nvapi_ok ? "available" : "unavailable",
        identity.nvml_ok ? "available" : "unavailable");
    if (identity.nvapi_error[0]) ImGui::TextWrapped("NVAPI: %s", identity.nvapi_error);
    if (identity.nvml_error[0]) ImGui::TextWrapped("NVML: %s", identity.nvml_error);
    if (identity.pairing_error[0]) ImGui::TextWrapped("Pairing: %s", identity.pairing_error);
}

// Panel bodies shared by Focused's flat headers and Classic's released 1.9.1
// section hierarchy. Each queues its intents and commands exactly as before.
void perf_panel_body(ViewState& state, bool classic=false) {
    const auto& snapshot=*state.live;
    ImGui::BeginDisabled(snapshot.busy || snapshot.opening || !snapshot.data.connected);
    const PerfViewModel empty{};
    if (draw_perf_diagnostics(snapshot.data.perf_diagnostics ? *snapshot.data.perf_diagnostics : empty, classic)) {
        Command command; command.operation=Command::Operation::refresh_perf_diagnostics;
        state.commands.push_back(command);
    }
    ImGui::EndDisabled();
}

void arbitration_panel_body(ViewState& state, bool classic=false) {
    const auto& snapshot=*state.live;
    ImGui::BeginDisabled(snapshot.busy || snapshot.opening || !snapshot.data.connected);
    const PowerArbitrationModel empty;
    if (draw_power_arbitration(snapshot.data.power_arbitration ? *snapshot.data.power_arbitration : empty, classic, snapshot.selected_slot)) {
        Command command; command.operation=Command::Operation::refresh_power_arbitration;
        state.commands.push_back(command);
    }
    ImGui::EndDisabled();
}

// Released 1.9.1 registry_panel build_diagnostics: the read-only relationships
// view under Power/current arbitration. Read queues the same weights read the
// Registry editor uses.
void relationships_panel_body(ViewState& state) {
    const auto& snapshot=*state.live;
    if (draw_power_relationships(snapshot.data.power_weights.get(), snapshot.busy || snapshot.opening, snapshot.data.connected))
        state.power_weights_intents.push_back({PowerWeightsIntent::Operation::read, {}});
}

const PowerWeightReceipt* selected_override(const ViewState& state) {
    const auto& s = *state.live;
    auto data = s.data.power_weights;
    if (!data || !data->receipt) data = state.power_weights_saved;
    if (!data || !data->receipt || data->receipt->identity.slot != s.selected_slot ||
        (s.data.identity.uuid[0] && data->receipt->uuid != s.data.identity.uuid)) return nullptr;
    return data->receipt.get();
}

bool front_clock_candidate(const SessionSnapshot& snapshot) {
    const auto arch = snapshot.data.clock_architecture;
    return arch && clock_override_architecture(*arch);
}

bool show_pascal_clocks(const ViewState& state) {
    const auto arch = state.live->data.clock_architecture;
    const auto* receipt = selected_override(state);
    return !arch || front_clock_candidate(*state.live) || (receipt && receipt->is_pascal_clocks());
}

const PascalClockTable* front_clock_table(const SessionSnapshot& snapshot) {
    return front_clock_candidate(snapshot) && snapshot.data.pascal_clock_availability != Availability::unsupported ?
        snapshot.data.pascal_clocks.get() : nullptr;
}

std::vector<uint32_t> replaced_clock_offsets(const SessionSnapshot& snapshot) {
    const auto* table = front_clock_table(snapshot);
    const bool xbar = has_writable_pascal_clock(table, PascalClockDomain::xbar);
    const bool sys = has_writable_pascal_clock(table, PascalClockDomain::sys);
    std::vector<uint32_t> replaced;
    for (const auto& control : snapshot.data.advanced_controls) {
        if (control.id.kind != AdvancedKind::clock_domain || control.id.domain == 2) continue;
        const bool shared_video = table && !table->state_targets && (control.label == "VIDEO" || control.label == "Video");
        if ((xbar && (control.label == "XBAR" || control.label == "Crossbar")) ||
            (sys && (control.label == "SYS" || shared_video)))
            replaced.push_back(control.id.domain);
    }
    return replaced;
}

bool front_clock_fields(const SessionSnapshot& snapshot) {
    const auto* table = front_clock_table(snapshot);
    return has_writable_pascal_clock(table, PascalClockDomain::gpc) ||
        has_writable_pascal_clock(table, PascalClockDomain::xbar) ||
        has_writable_pascal_clock(table, PascalClockDomain::sys);
}

bool front_clock_read_needed(const SessionSnapshot& s, unsigned attempts) {
    const auto availability = s.data.pascal_clock_availability;
    const bool initial = availability == Availability::unknown && attempts == 0;
    const bool recovery = availability == Availability::transient && attempts < 2;
    return front_clock_candidate(s) && s.data.connected && !s.data.pascal_clock_fresh && (initial || recovery);
}

void queue_front_pascal_clock_read(ViewState& state) {
    const auto& s = *state.live;
    if (!s.opening && !s.busy && front_clock_read_needed(s, state.pascal_clock_view.automatic_reads))
        state.automatic_clock_read = s.generation;
}

std::optional<PascalClockReference> clock_preview_reference(const ViewState& state) {
    const auto& s = *state.live;
    const auto cap = s.data.pascal_p0_nvvdd_effective_mv;
    if (s.data.clock_architecture != 4u || !s.data.connected || !cap || !s.curve_revision || s.curve_stale)
        return std::nullopt;
    const auto& curve = state.curve_draft;
    const bool staged_curve = curve.dirty();
    if (staged_curve && (curve.generation() != s.generation || !curve.can_apply())) return std::nullopt;

    const auto index = static_cast<size_t>(ControlId::core);
    const auto& control = s.data.controls[index];
    const auto& draft = state.drafts[index];
    double offset_change = 0;
    if (draft.dirty) {
        if (control.availability != Availability::available || !control.current ||
            !std::isfinite(*control.current) || !std::isfinite(draft.value) ||
            (control.low && draft.value < *control.low) || (control.high && draft.value > *control.high))
            return std::nullopt;
        offset_change = draft.value - *control.current;
    }
    const auto peak = pascal_p0_curve_peak(staged_curve ? curve.draft() : s.data.curve,
        s.data.curve_freq_div, cap, offset_change);
    if (!peak) return std::nullopt;
    return PascalClockReference{peak->peak_mhz, peak->park_mv, *cap, staged_curve || draft.dirty};
}

void firmware_timing_panel(ViewState& state) {
    const auto& s=*state.live;
    ImGui::SeparatorText("BIOS memory timing");
    if(s.data.clock_architecture!=4u) {
        ImGui::TextWrapped("P0 BIOS timing control requires a Pascal GPU with a supported memory table.");
        return;
    }
    const bool busy=s.busy||s.opening||!s.data.connected;
    const auto table=s.data.pascal_clocks;
    const auto sample=table&&s.data.pascal_clock_fresh?table->timing_sample:std::nullopt;
    auto& draft=state.firmware_timings;
    if(draft.generation!=s.generation || draft.source!=table) {
        draft={};
        draft.generation=s.generation;
        draft.source=table;
    }
    ImGui::TextWrapped("Read the selected GPU at P0 to locate its memory timing row. "
                       "This path uses the NVIDIA driver and works without nvtune.");
    ImGui::BeginDisabled(busy);
    if(ImGui::Button("Read P0 BIOS timings"))state.power_weights_intents.push_back({PowerWeightsIntent::Operation::read_firmware_timings,{}});
    ImGui::EndDisabled();
    if(table&&!table->timing_message.empty())ImGui::TextWrapped("%s",table->timing_message.c_str());
    if(!s.data.pascal_clock_fresh&&!s.data.pascal_clock_status.empty())ImGui::TextWrapped("%s",s.data.pascal_clock_status.c_str());
    if(!state.power_weights_view.status.empty())ImGui::TextWrapped("%s",state.power_weights_view.status.c_str());
    const auto* receipt=selected_override(state);
    const bool owned=receipt&&receipt->phase!="removed";
    const bool ready=!owned || (receipt->is_pascal_clocks() && receipt->phase=="active" && !receipt->uncertain
                              && !receipt->restoring && s.data.pascal_clock_active_receipt==receipt->id);
    std::string why;
    const bool restart=restart_device_target(s,why,state.app_actions.administrator).has_value();
    if (sample && !table->timing_fields.empty()) {
        ImGui::Text("P0 memory clock: %d MHz. Values use the controller's register encoding.",sample->memory_mhz);
        const bool scroll = table->timing_fields.size() > 8;
        ImGuiTableFlags flags = ImGuiTableFlags_RowBg | ImGuiTableFlags_SizingFixedFit;
        if (scroll) flags |= ImGuiTableFlags_ScrollY;
        if (ImGui::BeginTable("firmware-timing-fields",5,flags,
                              ImVec2(0,scroll ? 9 * ImGui::GetFrameHeightWithSpacing() : 0))) {
            ImGui::TableSetupScrollFreeze(0,1);
            ImGui::TableSetupColumn("Timing");
            ImGui::TableSetupColumn("Controller");
            ImGui::TableSetupColumn("New value",ImGuiTableColumnFlags_WidthFixed,ImGui::GetFontSize()*9);
            ImGui::TableSetupColumn("Range");
            ImGui::TableSetupColumn("Register");
            ImGui::TableHeadersRow();
            for (const auto& field : table->timing_fields) {
                const auto* descriptor = pascal_timing_field(field.domain);
                if (!descriptor) continue;
                ImGui::TableNextRow();
                ImGui::TableSetColumnIndex(0);
                ImGui::TextUnformatted(pascal_timing_label(field.domain));
                ImGui::TableSetColumnIndex(1);
                if (field.domain == PascalClockDomain::tfaw) ImGui::Text("%d cycles",field.current);
                else ImGui::Text("%d",field.current);
                ImGui::TableSetColumnIndex(4);
                const unsigned high_bit = descriptor->shift + descriptor->width - 1;
                ImGui::Text("CONFIG%u[%u:%u]",descriptor->word,high_bit,descriptor->shift);
                ImGui::TableSetColumnIndex(2);
                if (!descriptor->writable) {
                    ImGui::TextDisabled("Read only");
                    continue;
                }
                ImGui::PushID(static_cast<int>(field.domain));
                const auto change = draft.changes.find(field.domain);
                int value = change == draft.changes.end() ? field.current : change->second;
                ImGui::BeginDisabled(busy || !ready);
                ImGui::SetNextItemWidth(-FLT_MIN);
                if (ImGui::InputInt("##value",&value)) {
                    if (value == field.current) draft.changes.erase(field.domain);
                    else draft.changes[field.domain] = value;
                    draft.reviewing = false;
                }
                ImGui::EndDisabled();
                ImGui::PopID();
                ImGui::TableSetColumnIndex(3);
                ImGui::Text("%d to %d",field.minimum,field.maximum);
            }
            ImGui::EndTable();
        }
        std::vector<PascalClockEdit> edits;
        bool valid = true;
        for (const auto& field : table->timing_fields) {
            const auto* descriptor = pascal_timing_field(field.domain);
            const auto change = draft.changes.find(field.domain);
            if (!descriptor || !descriptor->writable || change == draft.changes.end() || change->second == field.current) continue;
            if (change->second < field.minimum || change->second > field.maximum) {
                ImGui::TextWrapped("%s must be between %d and %d.",pascal_timing_label(field.domain),field.minimum,field.maximum);
                valid = false;
            }
            edits.push_back({field.domain,0,field.current,change->second,sample});
        }
        const bool can_apply = valid && !edits.empty() && !busy && ready && restart && state.controls_unlocked;
        if (!can_apply) draft.reviewing = false;
        ImGui::BeginDisabled(!can_apply || draft.reviewing);
        if (ImGui::Button("Review BIOS timing changes")) draft.reviewing = true;
        ImGui::EndDisabled();
        if (!draft.changes.empty()) {
            ImGui::SameLine();
            if (ImGui::Button("Discard timing changes")) {
                draft.changes.clear();
                draft.reviewing = false;
            }
        }
        if (draft.reviewing) {
            for (const auto& edit : edits)
                ImGui::Text("%s: %d to %d",pascal_timing_label(edit.domain),edit.expected,edit.value);
            ImGui::TextWrapped("Apply changes the P0 memory-band row and restarts the selected GPU. "
                               "Verification needs the same P0 memory clock after restart.");
            if (ImGui::Button("Apply timings and refresh selected GPU")) {
                state.power_weights_intents.push_back({PowerWeightsIntent::Operation::prepare_clocks,{},std::move(edits)});
                draft.reviewing = false;
            }
        }
        if (!restart) ImGui::TextWrapped("%s",why.c_str());
        if (!state.controls_unlocked) ImGui::TextWrapped("Unlock controls to apply BIOS timing edits.");
    }
    if(owned && receipt->is_pascal_clocks() && receipt_timing_sample(*receipt,PascalTimingPhase::applied)) {
        ImGui::TextWrapped("Saved timing override (%s). %s",receipt->phase.c_str(),receipt->message.c_str());
        ImGui::BeginDisabled(busy);
        if(ImGui::Button("Verify P0 timing override"))state.power_weights_intents.push_back({PowerWeightsIntent::Operation::verify,{}});
        ImGui::EndDisabled();
        ImGui::SameLine();
        ImGui::BeginDisabled(busy||!state.controls_unlocked||!restart);
        if(ImGui::Button("Restore original firmware settings and refresh GPU"))
            state.power_weights_intents.push_back({PowerWeightsIntent::Operation::remove,{}});
        ImGui::EndDisabled();
    } else if(owned&&!ready) {
        ImGui::TextWrapped("Verify or restore the existing override before editing BIOS timings.");
    }
    ImGui::SeparatorText("nvtune timings");
}

void pascal_clock_panel(ViewState& state, bool front = false) {
    auto& s = *state.live;
    const bool busy = s.busy || s.opening;
    const auto* receipt = selected_override(state);
    const auto live_record = s.data.power_weights && s.data.power_weights->receipt ?
                             s.data.power_weights : state.power_weights_saved;
    const bool owned = receipt && (receipt->phase != "removed" || live_record->receipt_from_active_pointer);
    const bool active_clocks = owned && receipt->is_pascal_clocks() && live_record->receipt_from_active_pointer &&
        receipt->phase == "active" && !receipt->uncertain && !receipt->restoring &&
        !s.data.pascal_clock_active_receipt.empty() && receipt->id == s.data.pascal_clock_active_receipt;
    std::string restart_reason;
    const bool can_restart = restart_device_target(s, restart_reason, state.app_actions.administrator).has_value();
    ImGui::PushID("pascal-clock-panel");
    if (!front || !s.data.pascal_clock_fresh) {
        ImGui::BeginDisabled(busy || !s.data.connected);
        if (ImGui::Button("Read clock table")) state.power_weights_intents.push_back({PowerWeightsIntent::Operation::read_clocks, {}});
        ImGui::EndDisabled();
    }
    if (!front) {
        ImGui::SameLine();
        ImGui::BeginDisabled(busy);
        if (ImGui::Button("Inspect saved override")) state.power_weights_intents.push_back({PowerWeightsIntent::Operation::inspect, {}});
        ImGui::EndDisabled();
    }
    if ((!front || !s.data.pascal_clock_fresh) && !s.data.pascal_clock_status.empty())
        ImGui::TextWrapped("%s", s.data.pascal_clock_status.c_str());
    if (!state.power_weights_view.status.empty()) ImGui::TextWrapped("%s",state.power_weights_view.status.c_str());

    const bool editable = !busy && s.data.connected && s.data.pascal_clock_fresh && (!owned || active_clocks);
    const auto* table = front ? front_clock_table(s) : s.data.pascal_clocks.get();
    auto apply = draw_pascal_clock_sliders(state.pascal_clock_view, table, s.generation,
                                           editable, editable && state.controls_unlocked && can_restart,
                                           clock_preview_reference(state));
    if (editable && !state.controls_unlocked) ImGui::TextWrapped("Unlock tuning controls to apply clock changes.");
    if (!busy && !can_restart && (!front || front_clock_fields(s) || (receipt && receipt->is_pascal_clocks())))
        ImGui::TextWrapped("%s", restart_reason.c_str());
    if (!apply.edits.empty())
        state.power_weights_intents.push_back({PowerWeightsIntent::Operation::prepare_clocks, {},
                                              std::move(apply.edits), front, apply.arm_safe_mode});

    if (receipt && receipt->is_pascal_clocks()) {
        if (receipt->id == s.data.pascal_clock_original_receipt)
            ImGui::TextWrapped("The original clock image is running. Verify the clock override to close the previous session.");
        else {
            ImGui::TextWrapped("Saved clock override (%s). %s",receipt->phase.c_str(),receipt->message.c_str());
            if (active_clocks && s.data.pascal_clock_fresh)
                ImGui::TextWrapped("You can adjust the running clocks again. Restore returns to the clocks from before the first apply.");
            else if (owned)
                ImGui::TextWrapped("Read the clock table before editing. If editing is still unavailable, verify the saved override or restore the original clocks.");
        }
        if (owned) {
            ImGui::BeginDisabled(busy);
            if (ImGui::Button("Verify clock override"))
                state.power_weights_intents.push_back({PowerWeightsIntent::Operation::verify, {}, {}, front});
            ImGui::EndDisabled();
            ImGui::SameLine();
            ImGui::BeginDisabled(busy || !state.controls_unlocked || !can_restart);
            if (ImGui::Button("Restore original clocks and refresh GPU"))
                state.power_weights_intents.push_back({PowerWeightsIntent::Operation::remove, {}, {}, front});
            ImGui::EndDisabled();
        }
    } else if (owned) {
        ImGui::TextWrapped("Resolve the saved power relationship override before applying clock changes.");
    }
    ImGui::PopID();
}

void front_pascal_clock_panel(ViewState& state) {
    const auto& s = *state.live;
    const auto* receipt = selected_override(state);
    if (!front_clock_fields(s) && !(receipt && receipt->is_pascal_clocks()) &&
        (!front_clock_candidate(s) || s.data.pascal_clock_availability != Availability::transient)) return;
    ImGui::SeparatorText(s.data.pascal_clocks && s.data.pascal_clocks->state_targets ?
        "Clock targets" : "Clock ceilings and ratios");
    pascal_clock_panel(state, true);
}

const char* advanced_clock_title(const SessionSnapshot& snapshot) {
    const auto arch = snapshot.data.clock_architecture;
    return arch && *arch < 4 ? "Firmware clock targets" : "Pascal clock ceilings and ratios";
}

void power_weights_panel(ViewState& state, bool classic) {
    const auto& snapshot=*state.live;
    const auto* weights=snapshot.data.power_weights.get();PowerWeightsData with_saved_receipt;
    if(state.power_weights_saved&&state.power_weights_saved->receipt&&state.power_weights_saved->receipt->identity.slot==snapshot.selected_slot&&
       (!snapshot.data.identity.uuid[0]||state.power_weights_saved->receipt->uuid==snapshot.data.identity.uuid)&&(!weights||!weights->receipt)){
        if(weights)with_saved_receipt=*weights;
        with_saved_receipt.receipt=state.power_weights_saved->receipt;with_saved_receipt.receipt_message=state.power_weights_saved->receipt_message;weights=&with_saved_receipt;
    }
    auto intents=draw_power_weights(state.power_weights_view,weights,snapshot.generation,snapshot.busy||snapshot.opening,classic,state.controls_unlocked,snapshot.data.connected);
    state.power_weights_intents.insert(state.power_weights_intents.end(),std::make_move_iterator(intents.begin()),std::make_move_iterator(intents.end()));
}

void power_saving_panel(ViewState& state, bool classic, std::optional<PowerSavingFamily> family={}) {
    const auto& snapshot=*state.live;
    auto intents=draw_power_saving_controls(state.power_saving_view,snapshot.data.power_saving.get(),snapshot.generation,snapshot.busy||snapshot.opening,state.controls_unlocked,classic,family);
    state.power_saving_intents.insert(state.power_saving_intents.end(),intents.begin(),intents.end());
}

void clock_gating_panel(ViewState& state, bool classic, std::optional<ClockGatingFamily> family={}) {
    const auto& snapshot=*state.live;
    auto intents=draw_clock_gating_controls(state.clock_gating_view,snapshot.data.clock_gating.get(),snapshot.generation,snapshot.busy||snapshot.opening,state.controls_unlocked,classic,family);
    state.clock_gating_intents.insert(state.clock_gating_intents.end(),intents.begin(),intents.end());
}

// Classic's per-family subsections leave out the actions that span every family;
// these draw them once, under "Undo, recovery and profiles" in the same section.
void power_saving_shared_panel(ViewState& state) {
    const auto& snapshot=*state.live;
    auto intents=draw_power_saving_shared_controls(state.power_saving_view,snapshot.data.power_saving.get(),snapshot.generation,snapshot.busy||snapshot.opening,state.controls_unlocked);
    state.power_saving_intents.insert(state.power_saving_intents.end(),intents.begin(),intents.end());
}

void clock_gating_shared_panel(ViewState& state) {
    const auto& snapshot=*state.live;
    auto intents=draw_clock_gating_shared_controls(state.clock_gating_view,snapshot.data.clock_gating.get(),snapshot.generation,snapshot.busy||snapshot.opening,state.controls_unlocked);
    state.clock_gating_intents.insert(state.clock_gating_intents.end(),intents.begin(),intents.end());
}

void voltage_tables_panel(ViewState& state, bool classic, VoltageTablesPart part=VoltageTablesPart::all) {
    const auto& snapshot=*state.live;
    const bool busy=snapshot.busy||snapshot.opening||!snapshot.data.connected;
    auto intents=draw_voltage_tables_controls(state.voltage_tables_view,snapshot.data.voltage_tables.get(),snapshot.generation,busy,state.controls_unlocked,classic,part);
    state.voltage_tables_intents.insert(state.voltage_tables_intents.end(),intents.begin(),intents.end());
}

void voltage_tables_shared_panel(ViewState& state) {
    const auto& snapshot=*state.live;
    const bool busy=snapshot.busy||snapshot.opening||!snapshot.data.connected;
    auto intents=draw_voltage_tables_shared_controls(state.voltage_tables_view,snapshot.data.voltage_tables.get(),snapshot.generation,busy,state.controls_unlocked);
    state.voltage_tables_intents.insert(state.voltage_tables_intents.end(),intents.begin(),intents.end());
}

void voltage_limits_panel(ViewState& state, bool classic, VoltageLimitsPart part=VoltageLimitsPart::all) {
    const auto& snapshot=*state.live;
    const bool busy=snapshot.busy||snapshot.opening||!snapshot.data.connected;
    auto intents=draw_voltage_limits_controls(state.voltage_limits_view,snapshot.data.voltage_limits.get(),snapshot.generation,busy,state.controls_unlocked,classic,part);
    state.voltage_limits_intents.insert(state.voltage_limits_intents.end(),intents.begin(),intents.end());
}

void voltage_limits_shared_panel(ViewState& state) {
    const auto& snapshot=*state.live;
    const bool busy=snapshot.busy||snapshot.opening||!snapshot.data.connected;
    auto intents=draw_voltage_limits_shared_controls(state.voltage_limits_view,snapshot.data.voltage_limits.get(),snapshot.generation,busy,state.controls_unlocked);
    state.voltage_limits_intents.insert(state.voltage_limits_intents.end(),intents.begin(),intents.end());
}

void power_integral_panel(ViewState& state, bool classic=false) {
    const auto& snapshot=*state.live;
    PowerIntegralViewContext context;
    context.generation = snapshot.generation; context.slot = snapshot.selected_slot;
    context.uuid = snapshot.data.identity.uuid;
    context.connected = snapshot.data.connected; context.busy = snapshot.busy || snapshot.opening; context.unlocked = state.controls_unlocked;
    const PowerIntegralData empty;
    for (const auto& intent : draw_power_integral(state.power_integral_view,
        snapshot.data.power_integral ? *snapshot.data.power_integral : empty, context, true, classic)) {
        if (intent.generation != snapshot.generation || intent.slot != snapshot.selected_slot || intent.uuid != snapshot.data.identity.uuid) continue;
        Command command;
        switch (intent.action) {
        case PowerIntegralAction::refresh: command.operation = Command::Operation::power_integral_read; break;
        case PowerIntegralAction::apply:
        case PowerIntegralAction::reported_default: command.operation = Command::Operation::power_integral_apply; command.expected_request_raw = intent.expected; break;
        case PowerIntegralAction::undo: command.operation = Command::Operation::power_integral_undo; break;
        case PowerIntegralAction::restore: command.operation = Command::Operation::power_integral_restore; break;
        case PowerIntegralAction::save_profile: case PowerIntegralAction::load_profile:
        case PowerIntegralAction::preview_profile: case PowerIntegralAction::apply_profile:
        case PowerIntegralAction::undo_profile: case PowerIntegralAction::recover_profile:
            state.power_integral_profile_intents.push_back(intent); continue;
        }
        command.integral_policy = intent.policy; command.integral_upper = intent.field == PowerIntegralField::upper;
        command.integral_raw = intent.raw; state.commands.push_back(std::move(command));
    }
}

void voltage_policy_commands(ViewState& state, const VoltagePolicyViewOutput& output) {
    for (const auto& intent : output.intents) {
        Command command;
        switch (intent.operation) {
        case VoltagePolicyIntent::Operation::refresh: command.operation = Command::Operation::voltage_policy_read; break;
        case VoltagePolicyIntent::Operation::apply: command.operation = Command::Operation::voltage_policy_apply; break;
        case VoltagePolicyIntent::Operation::restore_initial: command.operation = Command::Operation::voltage_policy_restore; break;
        case VoltagePolicyIntent::Operation::undo: command.operation = Command::Operation::voltage_policy_undo; break;
        case VoltagePolicyIntent::Operation::recover: command.operation = Command::Operation::voltage_policy_recover; break;
        case VoltagePolicyIntent::Operation::save_profile: case VoltagePolicyIntent::Operation::load_profile:
            state.voltage_policy_profile_intents.push_back(intent); continue;
        }
        command.voltage_policy = std::make_shared<VoltagePolicyRequest>(intent.request);
        state.commands.push_back(std::move(command));
    }
}

void voltage_policy_panel(ViewState& state, bool classic, VoltagePolicyPart part=VoltagePolicyPart::all) {
    const auto& snapshot=*state.live;
    voltage_policy_commands(state, draw_voltage_policy_controls(state.voltage_policy_view, snapshot.data.voltage_policies.get(),
        snapshot.generation, snapshot.busy || snapshot.opening, state.controls_unlocked, classic, snapshot.data.connected, part));
}

void voltage_policy_shared_panel(ViewState& state) {
    const auto& snapshot=*state.live;
    voltage_policy_commands(state, draw_voltage_policy_shared_controls(state.voltage_policy_view, snapshot.data.voltage_policies.get(),
        snapshot.generation, snapshot.busy || snapshot.opening, state.controls_unlocked, snapshot.data.connected));
}

void live_perf_panel(ViewState& state) {
    if (ImGui::CollapsingHeader("PERF diagnostics")) perf_panel_body(state);
    if (ImGui::CollapsingHeader("Power arbitration")) arbitration_panel_body(state);
}

void live_semantic_controls(ViewState& state, bool classic) {
    const bool voltage_navigation=state.open_voltage_policy_requested;
    const bool integral_navigation=state.open_power_integral_requested;
    const bool tables_navigation=state.open_voltage_tables_requested;
    const bool limits_navigation=state.open_voltage_limits_requested;
    const bool saving_navigation=state.open_power_saving_requested;
    const bool gating_navigation=state.open_clock_gating_requested;
    const bool weights_navigation=state.open_power_weights_requested;
    const bool clocks_navigation=state.open_pascal_clocks_requested;
    state.open_pascal_clocks_requested=false;
    const bool policy_navigation=clocks_navigation||voltage_navigation||integral_navigation||tables_navigation||limits_navigation||saving_navigation||gating_navigation||weights_navigation;
    ImGui::PushID("advanced-semantic-controls");
    if (ImGui::Button("Policy workspace...")) state.policy_workspace.open=true;
    if(policy_navigation)ImGui::SetNextItemOpen(weights_navigation);
    state.open_power_weights_requested=false;
    if(ImGui::CollapsingHeader("Power relationship weights")) power_weights_panel(state, classic);
    if (show_pascal_clocks(state)) {
        if (policy_navigation) ImGui::SetNextItemOpen(clocks_navigation);
        if (ImGui::CollapsingHeader(advanced_clock_title(*state.live))) {
            if (clocks_navigation) ImGui::SetScrollHereY(0.0f);
            pascal_clock_panel(state);
        }
    }
    if(policy_navigation)ImGui::SetNextItemOpen(saving_navigation);
    state.open_power_saving_requested=false;
    if(ImGui::CollapsingHeader("Power saving")){
        if(saving_navigation)ImGui::SetScrollHereY(0.0f);
        power_saving_panel(state, classic);
    }
    if(policy_navigation)ImGui::SetNextItemOpen(gating_navigation);
    state.open_clock_gating_requested=false;
    if(ImGui::CollapsingHeader("Clock gating and PCIe speed")){
        if(gating_navigation)ImGui::SetScrollHereY(0.0f);
        clock_gating_panel(state, classic);
    }
    if(policy_navigation)ImGui::SetNextItemOpen(tables_navigation);
    state.open_voltage_tables_requested=false;
    if (ImGui::CollapsingHeader("Voltage request and offset tables")) {
        if(tables_navigation)ImGui::SetScrollHereY(0.0f);
        voltage_tables_panel(state, classic);
    }
    if(policy_navigation)ImGui::SetNextItemOpen(limits_navigation);
    state.open_voltage_limits_requested=false;
    if (ImGui::CollapsingHeader("Voltage definitions and device delays")) {
        if(limits_navigation)ImGui::SetScrollHereY(0.0f);
        voltage_limits_panel(state, classic);
    }
    if(policy_navigation)ImGui::SetNextItemOpen(integral_navigation);
    state.open_power_integral_requested=false;
    if (ImGui::CollapsingHeader("Power feedback ratios")) {
        if(integral_navigation)ImGui::SetScrollHereY(0.0f);
        power_integral_panel(state);
    }
    if(policy_navigation)ImGui::SetNextItemOpen(voltage_navigation);
    state.open_voltage_policy_requested=false;
    if (ImGui::CollapsingHeader("Voltage admission and V/F policies")) {
        if(voltage_navigation)ImGui::SetScrollHereY(0.0f);
        voltage_policy_panel(state, classic);
    }
    ImGui::PopID();
}

// Released 1.9.1 scope notes (druta.py build_advanced, registry_panel.py).
constexpr const char* classic_session_only_note = "This driver session only. A GPU restart returns these to the driver's values.";

// Released 1.9.1 max_all_power_policies writes every policy at once. Here Max all
// selects every policy in the Policy workspace and previews each maximum; only
// the workspace's explicit "Apply reviewed maxima" sends anything. The current
// requests are re-read so a stale preview is withdrawn when they differ.
void classic_policy_max_all(ViewState& state) {
    const auto& s = *state.live;
    auto& workspace = state.policy_workspace;
    workspace.open = true; workspace.generation = s.generation;
    workspace.selected.clear(); workspace.preview.reset();
    const auto* scoped = s.data.scoped_profiles.get();
    if (scoped) { for (const auto& row : scoped->entries) if (row.key.kind == ScopedKey::Kind::policy) workspace.selected.push_back(row.key); }
    else for (const auto& p : s.data.power_policies) workspace.selected.push_back(ScopedKey::policy_request(p.id));
    if (scoped && !workspace.selected.empty()) {
        workspace.preview = preview_policy_max(*scoped, workspace.selected);
        workspace.status = "Max all: every policy is selected. Review the maxima, then Apply reviewed maxima.";
    } else workspace.status = "Max all: reading the current policy requests; select Preview selected maxima when the read completes.";
    Command command; command.operation = Command::Operation::prepare_scoped_profile; state.commands.push_back(command);
}

// Released 1.9.1 build_advanced + RegistryPanel.build: five closed sections in
// reading order, each holding its subsections (registry_panel.py SECTIONS).
// Native-only panels stay reachable inside the section they belong to.
void classic_advanced_sections(ViewState& state) {
    const auto& s = *state.live;
    const bool voltage=state.open_voltage_policy_requested, integral=state.open_power_integral_requested;
    const bool tables=state.open_voltage_tables_requested, limits=state.open_voltage_limits_requested;
    const bool saving=state.open_power_saving_requested, gating=state.open_clock_gating_requested;
    const bool weights=state.open_power_weights_requested;
    const bool clocks=state.open_pascal_clocks_requested;
    state.open_pascal_clocks_requested=false;
    const bool navigating=clocks||voltage||integral||tables||limits||saving||gating||weights;
    state.open_voltage_policy_requested=state.open_power_integral_requested=false;
    state.open_voltage_tables_requested=state.open_voltage_limits_requested=false;
    state.open_power_saving_requested=state.open_clock_gating_requested=state.open_power_weights_requested=false;
    // Menu navigation opens the path to the requested panel and closes the rest.
    const auto next=[&](bool target){ if(navigating) ImGui::SetNextItemOpen(target); };
    const auto sub=[&](const char* label, bool target, bool scroll, const auto& body){
        next(target);
        if (ClassicSection section{label, 1}) { if (target && scroll) ImGui::SetScrollHereY(0.0f); body(); }
    };
    next(integral);
    if (ClassicSection section{"Power policies", 0, classic_session_only_note}) {
        // Python refresh_power_policies re-reads the table about once a second
        // while this header is open; the host passes this on as a read-only hint.
        state.power_policies_open = true;
        // Python draws the policy rows once, here; offsets, rail limits and the
        // core current limit stay on the Control tab.
        const auto policies = draw_power_policy_controls(s, state.controls_unlocked, state.advanced_view, state.commands, &state.policy_annotations);
        state.save_policy_annotations |= policies.annotations_changed;
        if (policies.open_workspace) state.policy_workspace.open = true;
        if (policies.max_all) classic_policy_max_all(state);
        // power_integral_panel.py build: a separator and a plain heading at the end
        // of Power policies, not a subsection of its own.
        ImGui::Separator();
        if (integral) ImGui::SetScrollHereY(0.0f);
        classic_text("Power-policy feedback adjustment");
        power_integral_panel(state, true);
    }
    next(weights||clocks);
    if (ClassicSection section{"Registry / BIOS overrides", 0,
        "Applying changes refreshes the selected GPU. Power relationship weights persist until removed. "
        "Firmware clock settings last for the driver session."}) {
        if (show_pascal_clocks(state))
            sub(advanced_clock_title(*state.live), clocks, true, [&]{ pascal_clock_panel(state); });
        sub("Power relationship weights", weights, true, [&]{ power_weights_panel(state, true); });
        // registry_panel.py build_registry "DIFR configured state". Its reader
        // (lpwr_runtime.read) has no native port yet, so the subsection says so
        // rather than offering a Read that cannot run.
        sub("DIFR configured state", false, false, [&]{
            ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + classic_px(900));
            classic_text("Read the loaded P-state to LPWR mapping and configured DFPR/MS DIFR support. "
                "These values describe configuration, not the current P-state, residency or measured power savings. "
                "Persistent DIFR entry and subfeature disables use the registry field editing this build does not offer.");
            ImGui::AlignTextToFramePadding(); ImGui::TextDisabled("The DIFR configuration read is not available in this build.");
            ImGui::PopTextWrapPos(); });
    }
    next(voltage||tables||limits);
    if (ClassicSection section{"Live voltage controls", 0,
        "This driver session only. A GPU restart returns these to the driver's values. Exception: voltage policy "
        "offsets need offset definitions installed persistently beforehand, with a GPU restart; this build does not "
        "install them."}) {
        sub("Runtime voltage-request control", voltage, true, [&]{ voltage_policy_panel(state, true, VoltagePolicyPart::admission); });
        sub("Runtime VF policy control", voltage, false, [&]{ voltage_policy_panel(state, true, VoltagePolicyPart::vf_policies); });
        sub("Voltage policy offsets", tables, true, [&]{ voltage_tables_panel(state, true, VoltageTablesPart::offsets); });
        sub("Live voltage rail requests", tables, false, [&]{ voltage_tables_panel(state, true, VoltageTablesPart::requests); });
        sub("Live REL equation bounds", limits, true, [&]{ voltage_limits_panel(state, true, VoltageLimitsPart::rel); });
        sub("Live voltage constants", limits, false, [&]{ voltage_limits_panel(state, true, VoltageLimitsPart::constants); });
        sub("Voltage switch-delay requests", limits, false, [&]{ voltage_limits_panel(state, true, VoltageLimitsPart::switch_delay); });
        sub("Undo, recovery and profiles##live-voltage", false, false, [&]{
            ImGui::SeparatorText("Voltage requests and VF policies"); voltage_policy_shared_panel(state);
            ImGui::SeparatorText("Voltage policy offsets and rail requests"); voltage_tables_shared_panel(state);
            ImGui::SeparatorText("REL bounds, constants and switch delays"); voltage_limits_shared_panel(state); });
    }
    next(saving||gating);
    if (ClassicSection section{"Live power-saving & PCIe", 0, classic_session_only_note}) {
        sub("Live engine-level power gating (ELPG)", saving, true, [&]{ power_saving_panel(state, true, PowerSavingFamily::gr_elpg); });
        sub("Individual engine power gating", saving, false, [&]{ power_saving_panel(state, true, PowerSavingFamily::individual); });
        sub("Live idle thresholds", saving, false, [&]{ power_saving_panel(state, true, PowerSavingFamily::idle_threshold); });
        sub("Live adaptive power policy", saving, false, [&]{ power_saving_panel(state, true, PowerSavingFamily::adaptive); });
        sub("Live engine clock gating", gating, true, [&]{ clock_gating_panel(state, true, ClockGatingFamily::engine); });
        sub("Live block clock gating", gating, false, [&]{ clock_gating_panel(state, true, ClockGatingFamily::block); });
        sub("Live second-level clock gating (SLCG)", gating, false, [&]{ clock_gating_panel(state, true, ClockGatingFamily::second_level); });
        sub("Live DIFR disable request", saving, false, [&]{ power_saving_panel(state, true, PowerSavingFamily::difr); });
        sub("Manual PCIe speed request", gating, false, [&]{ clock_gating_panel(state, true, ClockGatingFamily::pcie); });
        sub("Undo, recovery and profiles##live-saving", false, false, [&]{
            ImGui::SeparatorText("Power saving"); power_saving_shared_panel(state);
            ImGui::SeparatorText("Clock gating and PCIe"); clock_gating_shared_panel(state); });
    }
    next(false);
    if (ClassicSection section{"Diagnostics", 0, "Read-only."}) {
        // Adapter identity and Refresh capabilities are in the Device menu and its
        // report, as in 1.9.1; this section holds only the two read-only views.
        sub("Driver policy diagnostics", false, false, [&]{ perf_panel_body(state, true); });
        sub("Power/current arbitration", false, false, [&]{ arbitration_panel_body(state, true); relationships_panel_body(state); });
    }
}

void live_advanced(ViewState& state, bool verbose) {
    const auto& snapshot = *state.live;
    title("Advanced", "Stage a selected request, then Apply. Details explain reported capabilities.");
    ImGui::BeginDisabled(snapshot.busy || snapshot.opening);
    if (ImGui::Button("Refresh details")) state.refresh_requested = true;
    ImGui::SameLine();
    if (ImGui::Button("Reconnect")) state.reconnect_requested = true;
    ImGui::SameLine();
    if (ImGui::Button("Frequency locks...")) state.frequency_view.open=true;
    ImGui::EndDisabled();
    if (!verbose) draw_clock_voltage_offsets(snapshot,state.controls_unlocked,false,state.advanced_view,state.commands,OffsetGroup::additional_memory);
    draw_advanced_controls(snapshot, state.controls_unlocked, verbose, state.advanced_view, state.commands, verbose, verbose, &state.policy_annotations);
    live_semantic_controls(state, verbose);
    const auto flags = verbose ? ImGuiTreeNodeFlags_DefaultOpen : ImGuiTreeNodeFlags_None;
    live_perf_panel(state);
    if (ImGui::CollapsingHeader("Adapter and driver", flags)) live_identity(snapshot);
    if (ImGui::CollapsingHeader("Control capabilities", flags)) {
        const char* names[] = {"Core offset", "Memory offset", "Board power", "Fan target", "Voltage boost"};
        for (size_t i = 0; i < snapshot.data.controls.size(); ++i) {
            const auto& control = snapshot.data.controls[i];
            ImGui::Text("%s: %s%s", names[i], availability_text(control.availability), control.writable ? " / writable" : " / read-only");
            ImGui::TextWrapped("Configured: %s | Range: %s to %s",
                number_text(control.current, control.unit.c_str()).c_str(),
                number_text(control.low, control.unit.c_str()).c_str(), number_text(control.high, control.unit.c_str()).c_str());
            if (!control.detail.empty()) ImGui::TextWrapped("%s", control.detail.c_str());
            ImGui::Separator();
        }
    }
    if (ImGui::CollapsingHeader("Locks and diagnostics", flags)) {
        const auto& telemetry = snapshot.data.telemetry;
        measured_value(snapshot, "V/F lock entries", DC_T_VF_LOCKED, telemetry.vf_locked_count, "");
        measured_value(snapshot, "Requested lock voltage", DC_T_VF_LOCK_MV, telemetry.vf_lock_mv, "mV");
        measured_value(snapshot, "Clock lock minimum", DC_T_CLK_LOCK, telemetry.clk_lock_min_mhz, "MHz");
        measured_value(snapshot, "Clock lock maximum", DC_T_CLK_LOCK, telemetry.clk_lock_max_mhz, "MHz");
        hold_status(snapshot);
        ImGui::TextWrapped("Unreadable lock state does not establish an unlocked GPU. Use the V/F editor to hold a point; release owned holds from the main controls.");
        if (!snapshot.data.status.empty()) ImGui::TextWrapped("%s", snapshot.data.status.c_str());
    }
}

void live_history_plot(const SessionSnapshot& snapshot, const char* id, const char* unit, int channel,
                       const ImVec4& color,const ShuntCorrection* correction=nullptr) {
    if (snapshot.history.empty()) { ImGui::TextColored(muted, "No observed history yet."); return; }
    std::array<double, 360> time{}, values{};
    // Bound view work as well as the session's history storage. NaN leaves a gap for a missing sample.
    const size_t count = std::min(snapshot.history.size(), time.size());
    const size_t start = snapshot.history.size() - count;
    for (size_t i = 0; i < count; ++i) {
        const auto& sample = snapshot.history[start + i];
        auto value = channel == 0 ? sample.core_mhz : channel == 1 ? sample.temp_c : sample.power_w;
        if(channel==2 && correction && correction->valid && correction->active)value=correction->apply(value);
        time[i] = sample.time - snapshot.history.back().time;
        values[i] = value.value_or(std::numeric_limits<double>::quiet_NaN());
    }
    if (ImPlot::BeginPlot(id, ImVec2(-1, u(180)), ImPlotFlags_NoTitle | ImPlotFlags_NoLegend)) {
        ImPlot::SetupAxes("Seconds from latest sample", unit, ImPlotAxisFlags_AutoFit, ImPlotAxisFlags_AutoFit);
        ImPlotSpec style;
        style.LineColor = color;
        style.LineWeight = 2;
        ImPlot::PlotLine("Observed history", time.data(), values.data(), static_cast<int>(count), style);
        ImPlot::EndPlot();
    }
}

void monitor_history(const SessionSnapshot& snapshot,const ShuntViewState& shunt) {
    label("CORE CLOCK");
    live_history_plot(snapshot, "##live_clock_history", "MHz", 0, accent);
    label("TEMPERATURE");
    live_history_plot(snapshot, "##live_temp_history", "C", 1, teal);
    const bool corrected=shunt.correction.valid && shunt.correction.active;
    label(corrected?(shunt.correction.exact?"BOARD POWER — CORRECTED":"BOARD POWER — ESTIMATE"):"BOARD POWER");
    live_history_plot(snapshot, "##live_power_history", corrected?(shunt.correction.exact?"W corrected":"W estimate"):"W", 2, amber,&shunt.correction);
    if(corrected || !shunt.correction.valid || !shunt.config.load_error.empty())
        draw_shunt_power_summary(shunt,telemetry_value(snapshot,DC_T_POWER_W,snapshot.data.telemetry.power_w),snapshot.data.controls[static_cast<size_t>(ControlId::power)].current,snapshot.data.enforced_power_w,snapshot.paused || (snapshot.telemetry_stale_mask&DC_T_POWER_W));
}

void live_monitor(const SessionSnapshot& snapshot, bool verbose,const ShuntViewState& shunt) {
    title("Monitor", "Observed telemetry; unavailable readings remain empty.");
    draw_monitor_tiles(snapshot,shunt.correction);
    draw_monitor_details(snapshot,verbose);
    if (ImGui::CollapsingHeader("History")) monitor_history(snapshot,shunt);
}

void live_operations(const SessionSnapshot& snapshot) {
    if (snapshot.log.empty()) {
        ImGui::TextColored(muted, "Operation log: no tuning actions in this session.");
        return;
    }
    ImGui::SeparatorText("Operation log");
    ImGui::BeginChild("operations", ImVec2(0, ImGui::GetTextLineHeightWithSpacing() * 2), false);
    for (auto it = snapshot.log.rbegin(); it != snapshot.log.rend(); ++it) {
        ImGui::PushStyleColor(ImGuiCol_Text, it->ok ? secondary : amber);
        ImGui::TextWrapped("%s", it->message.c_str());
        ImGui::PopStyleColor();
    }
    ImGui::EndChild();
}

unsigned push_classic_palette() {
    // Dear PyGui 2.3.1's default theme, which released 1.9.1 runs unmodified:
    // background (37,37,38), panel (51,51,55), border (78,78,78), light
    // (82,82,85) and very light (90,90,95) greys, hover (29,151,236,103) and
    // active (0,119,200,153) blues, white text. TextDisabled stays 1.9.1's DIM,
    // the colour its dim labels set explicitly. Level-0 section headers use
    // ui_sections' blue (classic_section.h). Measured on tab_control-00.png:
    // menu bar, border lines, selected tab, check mark, slider grab, scrollbar.
    const auto rgb = [](int r, int g, int b, int a = 255) { return ImVec4(r / 255.0f, g / 255.0f, b / 255.0f, a / 255.0f); };
    const ImVec4 background = rgb(37, 37, 38), panel = rgb(51, 51, 55), border = rgb(78, 78, 78);
    const ImVec4 light = rgb(82, 82, 85), very_light = rgb(90, 90, 95);
    const ImVec4 hover = rgb(29, 151, 236, 103), active = rgb(0, 119, 200, 153);
    const auto& section = classic_section_colours[0];
    const std::pair<ImGuiCol, ImVec4> colors[] = {
        {ImGuiCol_WindowBg, background},
        {ImGuiCol_ChildBg, background},
        {ImGuiCol_PopupBg, background},
        {ImGuiCol_MenuBarBg, panel},
        {ImGuiCol_Border, border},
        {ImGuiCol_Separator, border},
        {ImGuiCol_Text, rgb(255, 255, 255)},
        {ImGuiCol_TextDisabled, rgb(139, 144, 153)},
        {ImGuiCol_FrameBg, panel},
        {ImGuiCol_FrameBgHovered, hover},
        {ImGuiCol_FrameBgActive, active},
        {ImGuiCol_Button, panel},
        {ImGuiCol_ButtonHovered, hover},
        {ImGuiCol_ButtonActive, active},
        {ImGuiCol_Header, rgb(section[0][0], section[0][1], section[0][2])},
        {ImGuiCol_HeaderHovered, rgb(section[1][0], section[1][1], section[1][2])},
        {ImGuiCol_HeaderActive, rgb(section[2][0], section[2][1], section[2][2])},
        {ImGuiCol_SliderGrab, hover},
        {ImGuiCol_SliderGrabActive, active},
        {ImGuiCol_CheckMark, active},
        {ImGuiCol_ScrollbarBg, panel},
        {ImGuiCol_ScrollbarGrab, light},
        {ImGuiCol_ScrollbarGrabHovered, very_light},
        {ImGuiCol_ScrollbarGrabActive, very_light},
        {ImGuiCol_Tab, panel},
        {ImGuiCol_TabHovered, hover},
        {ImGuiCol_TabSelected, active},
        {ImGuiCol_TabDimmed, panel},
        {ImGuiCol_TabDimmedSelected, active},
    };
    for (const auto& item : colors) ImGui::PushStyleColor(item.first, item.second);
    return static_cast<unsigned>(std::size(colors));
}

// The error of a telemetry read that failed ("" while reads succeed). An
// adapter still opening, or one that never opened, has the status line instead.
std::string classic_read_error(const SessionSnapshot& s) {
    if (s.opening || s.paused || !s.completed_reads || (s.data.connected && s.data.telemetry_valid)) return {};
    if (!s.data.connected && !s.status.empty()) return s.status;
    return !s.data.telemetry_error.empty() ? s.data.telemetry_error : !s.status.empty() ? s.status : "telemetry unavailable";
}
// 1.9.1 set_stale's wording; the age runs from the last sample with a reading
// to the newest read, which the session takes about once a second.
std::string classic_stale_text(const SessionSnapshot& s) {
    std::string text = "   \xE2\x9A\xA0 TELEMETRY STALE - ";
    const auto good = std::find_if(s.history.rbegin(), s.history.rend(),
        [](const HistorySample& h) { return h.core_mhz || h.temp_c || h.power_w; });
    if (good == s.history.rend()) return text + "no reading yet";
    char age[48]{}; std::snprintf(age, sizeof(age), "last good read %.0fs ago", std::max(0.0, s.history.back().time - good->time));
    return text + age;
}

void classic_header(ViewState& state) {
    draw_app_actions_menu(state);
    const auto snapshot = state.live;
    const auto* id = snapshot ? &snapshot->data.identity : nullptr;
    // Released 1.9.1 build_shared_header / layout_shared_header: a two-column
    // table, card identity on the left and a fixed recovery column sized to
    // the panic label plus 28 px on the right. The identity row stacks when
    // the left column cannot hold title, "card" and a 460 px selector.
    const char* title_text = "Thermetery Druta 1.9.1";
    ImGui::PushFont(classic_fonts.heading, 26.0f);
    const float title_width = ImGui::CalcTextSize(title_text).x;
    ImGui::PopFont();
    ImGui::PushFont(classic_fonts.heading, 21.0f);
    const float card_width = ImGui::CalcTextSize("card").x;
    ImGui::PopFont();
    const float button_width = ImGui::CalcTextSize(classic_panic_label).x + classic_px(28);
    const float left_width = std::max(classic_px(80), ImGui::GetIO().DisplaySize.x - button_width - classic_px(32));
    const bool inline_identity = left_width >= title_width + card_width + classic_px(460);
    float combo_space = left_width - card_width - classic_px(12);
    if (inline_identity) combo_space -= title_width + classic_px(12);
    const float combo_width = std::min(classic_px(420), std::max(classic_px(80), combo_space));
    // 1.9.1's viewport menu bar overlays root, so its menu_pad spacer puts the
    // header 36 px down; this window's own menu bar leaves the cursor at 30.
    ImGui::Dummy(ImVec2(0, std::max(0.0f, classic_px(36) - ImGui::GetCursorPosY() - ImGui::GetStyle().ItemSpacing.y)));
    if (!ImGui::BeginTable("classic-header", 2, ImGuiTableFlags_SizingStretchProp)) return;
    ImGui::TableSetupColumn("identity", ImGuiTableColumnFlags_WidthStretch);
    ImGui::TableSetupColumn("recovery", ImGuiTableColumnFlags_WidthFixed, button_width);
    ImGui::TableNextRow(); ImGui::TableNextColumn();
    ImGui::PushTextWrapPos(0.0f);
    ImGui::PushFont(classic_fonts.heading, 26.0f);
    // 1.9.1 renders the title and "card" on the selector's frame baseline.
    ImGui::AlignTextToFramePadding();
    ImGui::TextColored(ImVec4(74.0f/255, 163.0f/255, 1, 1), "%s", title_text);
    ImGui::PopFont(); if (inline_identity) ImGui::SameLine();
    ImGui::PushFont(classic_fonts.heading, 21.0f);
    if (!inline_identity) ImGui::AlignTextToFramePadding();
    ImGui::TextDisabled("card"); ImGui::SameLine();
    const std::string selected = state.preview ? "Sample adapter (preview)"
        : id && id->name[0] ? std::string(id->name) + "   " + snapshot->selected_slot : "Opening adapter...";
    ImGui::SetNextItemWidth(combo_width);
    ImGui::BeginDisabled(!snapshot || snapshot->busy || snapshot->opening);
    if (ImGui::BeginCombo("##adapter", selected.c_str())) {
        if (snapshot) for (const auto& device : snapshot->devices) {
            // 1.9.1 card_label: three spaces between name and slot.
            const auto name = device.name + "   " + device.slot;
            if (ImGui::Selectable(name.c_str(), device.slot == snapshot->selected_slot)) state.select_slot = device.slot;
        }
        ImGui::EndCombo();
    }
    ImGui::PopFont(); ImGui::EndDisabled();
    // Dear PyGui 2.3.1 lays every mvText out on a frame-height line, text 3 px
    // down (FramePadding.y): a DPG rebuild of this header puts the details at
    // y 74 (text 77-93), the empty "stale" line at 100-122 and the gate row at
    // 128. Align each header line the same way so the rows below line up.
    if (state.preview) { ImGui::AlignTextToFramePadding(); ImGui::TextDisabled("Preview / sample data. No GPU session is connected."); }
    else if (id) {
        ImGui::AlignTextToFramePadding();
        // 1.9.1 hdr_details: bullet separators, DIM as admin and WARN when not.
        ImGui::TextColored(id->admin ? py_dim : py_warn, "%s  \xE2\x80\xA2  driver %s  \xE2\x80\xA2  vbios %s  \xE2\x80\xA2  %s",
            id->name[0] ? id->name : "--", id->driver[0] ? id->driver : "--", id->vbios[0] ? id->vbios : "--",
            id->admin ? "admin" : "NOT admin");
    }
    // 1.9.1 set_stale: a failed telemetry read fills the header's "stale" line
    // in BAD with the age of the last good read, and log_once("read") logs the
    // error once per failure episode.
    const auto read_error = snapshot && !state.preview ? classic_read_error(*snapshot) : std::string();
    if (read_error.empty()) state.classic_read_error_logged.clear();
    else if (read_error != state.classic_read_error_logged) {
        state.classic_read_error_logged = read_error;
        classic_log_line(state, "read error: " + read_error, false);
    }
    const bool status_line = snapshot && (!read_error.empty() || !snapshot->data.connected || snapshot->opening || snapshot->busy);
    const bool pairing_line = id && id->pairing_error[0];
    if (!read_error.empty()) { ImGui::AlignTextToFramePadding(); ImGui::TextColored(py_bad, "%s", classic_stale_text(*snapshot).c_str()); }
    else if (status_line) { ImGui::AlignTextToFramePadding(); ImGui::TextWrapped("%s", snapshot->status.c_str()); }
    if (pairing_line) { ImGui::AlignTextToFramePadding(); ImGui::TextColored(amber, "%s", id->pairing_error); }
    // 1.9.1 keeps its "stale" status line in the header even while it is empty.
    if (!status_line && !pairing_line) { ImGui::AlignTextToFramePadding(); ImGui::TextUnformatted(""); }
    ImGui::PopTextWrapPos();
    ImGui::TableNextColumn();
    draw_device_restart_button(state);
    ImGui::EndTable();
    // 1.9.1 has no row here: Save / Load profile and the native catalog live in
    // the Profiles menu, Always refresh / Pause / presentation in File.
}

constexpr Page classic_pages[]{Page::daily, Page::advanced, Page::monitor, Page::timings};
int classic_page_index(Page page) {
    for (int i = 0; i < 4; ++i) if (classic_pages[i] == page) return i;
    return -1;
}
void float_classic_page(ViewState& state, int index) {
    if (index < 0 || index >= 4) return;
    state.classic_floating[index] = state.classic_float_focus[index] = true;
    state.classic_tab_drag = -1;
    for (int i = 0; i < 4; ++i) if (!state.classic_floating[i]) { state.page = classic_pages[i]; break; }
}
void classic_tabs(ViewState& state) {
    const std::pair<Page, const char*> tabs[] = {
        {Page::daily,"  Control  "}, {Page::advanced,"  Advanced  "},
        {Page::monitor,"  Monitor  "}, {Page::timings,"  Timings  (needs nvtune)  "}};
    const auto selected_page = state.page;
    if (ImGui::BeginTabBar("legacy-tabs", ImGuiTabBarFlags_NoTooltip)) {
        for (int index = 0; index < 4; ++index) {
            if (state.classic_floating[index]) continue;
            const auto& tab = tabs[index];
            const bool active = ImGui::BeginTabItem(tab.second, nullptr,
                tab.first == selected_page ? ImGuiTabItemFlags_SetSelected : ImGuiTabItemFlags_None);
            if (ImGui::IsItemClicked() || ImGui::IsItemActivated()) state.page = tab.first;
            if (ImGui::IsItemClicked()) {
                state.classic_tab_drag = index;
                state.classic_tab_top = ImGui::GetItemRectMin().y;
                state.classic_tab_bottom = ImGui::GetItemRectMax().y;
            }
            if (ImGui::BeginPopupContextItem()) {
                if (ImGui::MenuItem("Float page")) float_classic_page(state, index);
                ImGui::EndPopup();
            }
            if (active) ImGui::EndTabItem();
        }
        ImGui::EndTabBar();
    }
    if (state.classic_tab_drag >= 0 && ImGui::IsMouseDragging(ImGuiMouseButton_Left, classic_px(6))) {
        const float y = ImGui::GetIO().MousePos.y;
        if (y < state.classic_tab_top - classic_px(12) || y > state.classic_tab_bottom + classic_px(12))
            float_classic_page(state, state.classic_tab_drag);
    }
    if (!ImGui::IsMouseDown(ImGuiMouseButton_Left)) state.classic_tab_drag = -1;
}

bool classic_knob_table(const char* id) {
    return begin_classic_knob_table(id);
}

// Python draw_hold_banner (hold_info): this session's own lock record, the V/F
// hold first, then the Clocks-menu frequency request; empty while it holds
// nothing. GOOD only for a confirmed V/F hold on the point it asked for.
struct ClassicHoldInfo { std::string text; bool good = false; };
ClassicHoldInfo classic_hold_line(const SessionSnapshot& s) {
    constexpr const char* dot = "  \xE2\x80\xA2  ";
    const auto fixed = [](const char* format, double value) {
        char buffer[48]{}; std::snprintf(buffer, sizeof(buffer), format, value); return std::string(buffer);
    };
    const auto& hold = s.data.tuning;
    if (hold.owned || hold.recovery) {
        const auto requested_mv = hold.voltage_uv / 1000.0;
        const auto requested = fixed("%.2f", requested_mv);
        if (!hold.confirmed || hold.recovery || s.tuning_ownership_uncertain)
            return {std::string(hold.recovery ? "V/F HOLD RECOVERY REQUIRED" : "V/F HOLD readback unavailable") + dot
                + "requested " + requested + " mV; current state is unconfirmed" + dot + "Release retries cleanup"};
        // The request is a point's own voltage. Where the card sits follows
        // Python's resolve_vf_point on the read curve: the highest point at or
        // below the request fixes the clock, the lowest voltage on that clock's
        // flat is the point held.
        const auto& curve = s.data.curve;
        const dc_vf_point* asked = nullptr; int asked_count = 0;
        const dc_vf_point* top = nullptr;
        for (const auto& p : curve) {
            if (!std::isfinite(p.volt_mv)) continue;
            if (std::llround(p.volt_mv * 1000) == static_cast<long long>(hold.voltage_uv)) { asked = &p; ++asked_count; }
            if (curve_below_cap(p.volt_mv, requested_mv) && (!top || p.volt_mv > top->volt_mv)) top = &p;
        }
        const dc_vf_point* held = nullptr;
        if (top) for (const auto& p : curve)
            if (std::isfinite(p.volt_mv) && p.freq_mhz == top->freq_mhz && (!held || p.volt_mv < held->volt_mv)) held = &p;
        const auto head = "HOLD  V/F point lock on domain " + std::to_string(hold.domain) + dot;
        const std::string tail = "   \xE2\x80\xA2   Ctrl+H releases";
        // No single curve point carries the request (no curve read yet): name only what was held.
        if (asked_count != 1 || !held) return {head + requested + " mV" + tail, true};
        const auto point = "point " + std::to_string(held->idx) + " @ " + fixed("%.2f", held->volt_mv) + " mV, "
            + fixed("%.0f", held->freq_mhz) + " MHz";
        if (held->idx == asked->idx) return {head + point + tail, true};
        return {head + "asked for point " + std::to_string(asked->idx) + " @ " + requested
            + " mV, the card holds the point at or below it: " + point + tail};
    }
    const auto& clock = s.data.frequency.ownership;
    if (clock.mode == FrequencyLockMode::legacy_p0)
        // Python names a measured boost only for boards in its own profile
        // table; without one it prints this sentence.
        return {std::string(clock.confirmed && !clock.recovery ? "HOLD  legacy P0 hold"
            : "HOLD  legacy P0 request remains after failed verification / release") + dot
            + "Maximum core boost is not guaranteed by a performance-state hold" + dot + "Release P0 drops the hold"};
    if (clock.mode == FrequencyLockMode::nvml) {
        if (clock.confirmed && !clock.recovery && clock.range)
            return {"clock lock requested at [" + std::to_string(clock.range->minimum) + ".." + std::to_string(clock.range->maximum)
                + "] MHz from the Clocks menu (NVML locked clocks) - no V/F point is held"};
        return {std::string("GPU clock lock accepted; its submitted range is unavailable") + dot + "Release retries cleanup"};
    }
    // Native: the session lost track of whether it still owns a hold.
    if (s.tuning_ownership_uncertain) return {"V/F hold ownership uncertain"};
    return {};
}
ClassicHoldInfo classic_hold_info(const SessionSnapshot& s) {
    auto info = classic_hold_line(s);
    // 1.9.1 _headroom_note "raised": a raise still in force after its hold ended
    // is shown whatever the lock says, in WARN. The service does not report why
    // a raise was withheld or not applied, so those notes have no source here.
    if (s.data.tuning.headroom && !s.data.tuning.owned) {
        info.text = (info.text.empty() ? std::string("No V/F hold") : info.text)
            + "\nVOLTAGE LIMITS STILL RAISED - the headroom raise could not be taken off; Druta retries (see log)";
        info.good = false;
    }
    return info;
}

void classic_control_summary(const SessionSnapshot& s) {
    const auto& t = s.data.telemetry;
    // Python ctl_clocks: two mono lines, P0 maxima beside the live clocks and
    // VRAM through mem_fmt. Missing fields read "?" or "--" exactly as Python.
    const auto whole = [&](uint64_t bit, double value, const char* missing) -> std::string {
        const auto observed = telemetry_value(s, bit, value);
        if (!observed) return missing;
        char buffer[48]{}; std::snprintf(buffer, sizeof(buffer), "%.0f", *observed); return buffer;
    };
    const auto memory = [&](uint64_t bit, double value) { return memory_clock_text(s.data, telemetry_value(s, bit, value)).value; };
    const auto clocks = "core " + whole(DC_T_CORE, t.core, "?") + " MHz (P0 max " + whole(DC_T_CORE_P0MAX, t.core_p0max, "?") + ")   "
        + "VRAM " + memory(DC_T_MEM, t.mem) + " MHz (P0 max " + memory(DC_T_MEM_P0MAX, t.mem_p0max) + ")";
    const auto rail = "Vcore " + whole(DC_T_VCORE_MV, t.vcore_mv, "--") + " mV   volt-boost "
        + whole(DC_T_VBOOST, t.vboost_pct, "--") + "%   P" + whole(DC_T_PSTATE, t.pstate, "?");
    // One text item holding both lines, as Python's single dpg text with "\n":
    // the second line sits at the font's line height, without ItemSpacing.
    const auto readout = clocks + "\n" + rail;
    // Dear PyGui sets every text item 3 px down on a frame-height line, the
    // two-line read-out and the hold line under it included.
    // Python's TEXT colour (ctl_clocks color=TEXT), not the theme's white.
    push_classic_mono();
    ImGui::AlignTextToFramePadding();
    ImGui::PushStyleColor(ImGuiCol_Text, py_text); ImGui::TextUnformatted(readout.c_str()); ImGui::PopStyleColor();
    ImGui::PopFont();
    // A failed read is the header's stale line (1.9.1 set_stale); a single
    // stale field is marked "(stale)" on its own row. Nothing is added here.
    // Python hold_info: this session's own lock record, not the telemetry lock
    // count (Monitor lists that); an empty frame-height line while nothing is held.
    const auto hold = classic_hold_info(s);
    ImGui::AlignTextToFramePadding();
    ImGui::PushStyleColor(ImGuiCol_Text, hold.good ? py_good : py_warn);
    ImGui::TextUnformatted(hold.text.c_str());
    ImGui::PopStyleColor();
    ImGui::Separator();
}

void classic_daily(ViewState& state) {
    if (!state.live) { ImGui::TextUnformatted("Opening adapter..."); return; }
    const auto& s = *state.live;
    queue_front_pascal_clock_read(state);
    classic_control_summary(s);
    // Python build_control: a two-column table with no width of its own, each
    // column fixed at the released knob width. ImGui narrows the columns that do
    // not fit to the table's work rect and the cells clip; there is no
    // horizontal scrollbar at any window width.
    const float column_width = classic_px(classic_column_width);
    if (ImGui::BeginChild("classic-knob-columns", ImVec2(0, 0),
        ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysAutoResize)) {
    // A disabled Dear PyGui 2.3.1 item keeps its normal look: the knob rows
    // (live_control_row included) refuse input without dimming.
    ImGui::PushStyleVar(ImGuiStyleVar_DisabledAlpha, 1.0f);
    // Python ui_sections.section: an 8 px spacer after every level-0 section, open or not.
    const auto section_gap = [] { ImGui::Dummy(ImVec2(0, classic_px(8))); };
    if (ImGui::BeginTable("classic-control-columns", 2,
        ImGuiTableFlags_SizingFixedFit | ImGuiTableFlags_NoHostExtendX)) {
        ImGui::TableSetupColumn("Clocks and limits", ImGuiTableColumnFlags_WidthFixed, column_width);
        ImGui::TableSetupColumn("Rails", ImGuiTableColumnFlags_WidthFixed, column_width);
        ImGui::TableNextColumn();
        if (state.open_pascal_clocks_requested) ImGui::SetNextItemOpen(true);
        state.open_pascal_clocks_requested = false;
        if (ImGui::CollapsingHeader("Clock offsets", ImGuiTreeNodeFlags_DefaultOpen)) {
            if (classic_knob_table("classic-clock-offsets")) {
                live_control_row(state, ControlId::core, "Core clock offset (MHz)", true);
                // Python: f"Memory offset ({munit})" from mem_offset_scale(), the
                // unit the slider is in ("MHz true" with a known divisor, else "MHz eff").
                const auto& memory = s.data.controls[static_cast<size_t>(ControlId::memory)];
                const auto memory_label = "Memory offset (" + (!memory.unit.empty() ? memory.unit
                    : std::string(s.data.memory_divisor ? "MHz true" : "MHz eff")) + ")";
                live_control_row(state, ControlId::memory, memory_label.c_str(), true);
                // Python build_domain_offset_rows: the per-domain rows share this table.
                draw_clock_domain_controls(s, state.controls_unlocked, true, state.advanced_view, state.commands,
                                           true, replaced_clock_offsets(s));
                ImGui::EndTable();
            }
            front_pascal_clock_panel(state);
        }
        section_gap();
        if (ImGui::CollapsingHeader("Limits", ImGuiTreeNodeFlags_DefaultOpen)) {
            // Python's single Limits table: power, current limit, boost, fan.
            if (classic_knob_table("classic-limits")) {
                live_control_row(state, ControlId::power, "Power limit (W)", true);
                draw_core_current_limit(s, state.controls_unlocked, true, state.advanced_view, state.commands, true);
                live_control_row(state, ControlId::boost, "Core voltage boost (%)", true);
                live_control_row(state, ControlId::fan, "Fan duty (%)", true);
                ImGui::EndTable();
            }
            // Python power_request_status: DIM, or WARN on a mismatch, wrapped at
            // the knob column less 20 px. The native effective limit is read on
            // Classic polls; an unread or failed read prints "unavailable".
            const auto status = power_request_status(s.data.controls[static_cast<size_t>(ControlId::power)].current,
                s.data.enforced_power_w, s.data.native_power_limit_w,
                state.classic_power_feedback ? state.classic_power_feedback->text : std::string());
            ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + classic_px(classic_column_width - 20));
            ImGui::AlignTextToFramePadding();
            ImGui::TextColored(status.warn ? py_warn : py_dim, "%s", status.text.c_str());
            ImGui::PopTextWrapPos();
        }
        section_gap();
        ImGui::TableNextColumn();
        if (ImGui::CollapsingHeader("Rails", ImGuiTreeNodeFlags_DefaultOpen)) {
            // Python's estimated-limits summary, Link and Reference rows lead
            // the same knob table as the rail sliders.
            draw_daily_rail_controls(s, state.controls_unlocked, true, state.advanced_view, state.commands, true,
                s.data.rail_readouts ? &*s.data.rail_readouts : nullptr);
        }
        section_gap();
        ImGui::EndTable();
    }
    ImGui::PopStyleVar();
    }
    ImGui::EndChild();
    const auto discovery_id = ImGui::GetID(("classic-i2c-discovered-" + std::to_string(s.generation)).c_str());
    if (!s.data.i2c.candidates.empty() && !ImGui::GetStateStorage()->GetBool(discovery_id)) {
        ImGui::SetNextItemOpen(true); ImGui::GetStateStorage()->SetBool(discovery_id, true);
    }
    // Python ui_sections.section: the I2C and V/F sections are level-0 sections
    // too, each followed by its 8 px spacer, open or not.
    if (ImGui::CollapsingHeader("I2C regulator")) draw_i2c_panel(state);
    ImGui::Dummy(ImVec2(0, classic_px(8)));
    if (ImGui::CollapsingHeader("V/F curve editor", ImGuiTreeNodeFlags_DefaultOpen)) {
        // 1.9.1 opens straight onto the cap row; the separate window is a native
        // extra reached from the editor's "More V/F tools" header.
        if (state.curve_window_open) {
            if (ImGui::Button("Open V/F curve", ImVec2(u(220), 0))) {
                end_curve_drag(state);
                state.curve_window_focus_requested = true;
            }
            ImGui::SameLine();
            ImGui::TextDisabled("%zu points | editing in separate window", s.data.curve.size());
        } else {
            // A separate child scopes WASD to the editor while the parent scrolls
            // normally past the curve and operation log. It is as tall as its
            // rows plus the plot's 1.9.1 budget, so nothing scrolls inside it.
            if (ImGui::BeginChild("inline-vf-editor", ImVec2(0, 0), ImGuiChildFlags_AutoResizeY)) {
                state.curve_inline_active = true;
                live_curve_workspace(state, true, true);
            }
            ImGui::EndChild();
        }
    }
    ImGui::Dummy(ImVec2(0, classic_px(8)));
    // 1.9.1's log follows the editor directly; the native draft/undo row moves below it.
    classic_log(state);
    if (ImGui::Button("Revert local drafts")) state.revert_draft();
    ImGui::SameLine(); ImGui::BeginDisabled((!state.controls_unlocked&&!s.data.undo_metadata_only) || !s.data.undo_available || (!s.data.connected&&!s.data.undo_metadata_only) || s.busy || s.opening);
    if (ImGui::Button(s.data.undo_metadata_only?"Undo label update":"Undo hardware write")) state.commands.push_back({Command::Operation::undo, ControlId::memory, 0});
    ImGui::EndDisabled();
}

void classic_advanced(ViewState& state) {
    ImGui::SetNextWindowContentSize(ImVec2(std::max(classic_px(classic_column_width), ImGui::GetContentRegionAvail().x), 0));
    if (ImGui::BeginChild("classic-advanced-columns", ImVec2(0, 0),
        ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysAutoResize, ImGuiWindowFlags_HorizontalScrollbar)) {
    // druta.py build_advanced: the dim scope line, then adv_status mirroring the
    // log's newest line with its [ok]/[!!] tag in GOOD/BAD (an empty line until
    // the first entry), then a 4 px spacer.
    ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + classic_px(900));
    ImGui::AlignTextToFramePadding();
    ImGui::TextColored(py_dim, "Every section states whether its changes last for this driver session or persist across GPU restarts.");
    ImGui::AlignTextToFramePadding();
    // The same Classic log the Control page shows (1.9.1 log() sets vf_status
    // and adv_status together): untagged lines DIM, timing captures filtered.
    const auto log = classic_log_entries(state);
    if (log.empty()) ImGui::TextUnformatted("");
    else ImGui::TextColored(!log.back().ok ? py_dim : *log.back().ok ? py_good : py_bad, "%s", classic_log_text(log.back()).c_str());
    ImGui::PopTextWrapPos();
    ImGui::Dummy(ImVec2(0, classic_px(4)));
    classic_advanced_sections(state);
    }
    ImGui::EndChild();
}

void classic_monitor(const SessionSnapshot& s,const ShuntViewState& shunt) {
    // druta.py build_monitor: the tiles start the page; no status line mirrors the
    // log here (Control's log box and Advanced's status line still show it).
    draw_monitor_tiles(s,shunt.correction,true);
    draw_monitor_details(s,true);
    const bool history = ImGui::CollapsingHeader("History");
    note_classic_monitor_page_end();
    if (history) monitor_history(s,shunt);
}

void classic_log(const ViewState& state) {
    ImGui::Separator(); ImGui::AlignTextToFramePadding(); ImGui::TextDisabled("log  (newest line first)");
    // 1.9.1 shows the newest 40 lines, newest first, in a box sized
    // max(90 px, 13% of the page share height) by its relayout, with no button
    // beside the caption. The native-only Copy log moves to the box's
    // right-click menu and still copies every retained entry.
    const auto entries = classic_log_entries(state);
    std::string text, all;
    for (size_t shown = 0; shown < entries.size(); ++shown) {
        const auto line = classic_log_text(entries[entries.size() - 1 - shown]);
        if (!all.empty()) all += '\n';
        all += line;
        if (shown < 40) text = all;
    }
    if (text.empty()) text = all = "No tuning actions in this session.";
    const float height = curve_log_height(classic_curve_share_height(), classic_px(90));
    push_classic_mono();
    ImGui::InputTextMultiline("##classic-log", text.data(), text.size() + 1,
        ImVec2(-1, height), ImGuiInputTextFlags_ReadOnly);
    ImGui::PopFont();
    if (ImGui::BeginPopupContextItem("classic-log-menu")) {
        if (ImGui::MenuItem("Copy log")) ImGui::SetClipboardText(all.c_str());
        ImGui::EndPopup();
    }
}

void classic_page_content(ViewState& state, Page page) {
    if (state.preview) {
        if (page == Page::daily) daily(state, true);
        else if (page == Page::advanced) advanced(true);
        else monitor(true);
        return;
    }
    if (!state.live) { ImGui::TextUnformatted("Waiting for the first session snapshot..."); return; }
    if (page == Page::daily) classic_daily(state);
    else if (page == Page::advanced) classic_advanced(state);
    else if (page == Page::monitor) classic_monitor(*state.live, state.shunt);
    else if (page == Page::timings) {
        const auto& s = *state.live;
        // Released 1.9.1 tim_busy reads the live memory clock and p-state.
        TimingLiveClock clock;
        const auto fresh = [&](std::uint64_t bit) { return s.data.telemetry_valid && (s.data.telemetry.present & bit) && !(s.telemetry_stale_mask & bit); };
        if (fresh(DC_T_MEM)) clock.memory_mhz = s.data.telemetry.mem;
        if (fresh(DC_T_PSTATE)) clock.pstate = s.data.telemetry.pstate;
        clock.top_memory_mhz = s.data.timing_top_memory_mhz; clock.divisor = s.data.memory_divisor;
        firmware_timing_panel(state);
        auto output = draw_timings(state.timings_view, s.data.timings.get(), s.generation,
            s.busy, state.controls_unlocked, s.data.connected, true, &clock);
        state.timing_capture_cancel_requested |= output.cancel_capture;
        state.timing_helper_requested |= output.choose_helper;
        state.timing_forget_requested |= output.forget_helper;
        state.test_signing_requested |= output.test_signing_setup;
        state.timing_save_profile_requested |= output.save_profile;
        state.timing_load_profile_requested |= output.load_profile;
        for (auto& line : output.classic_log) classic_log_line(state, std::move(line.first), line.second);
        state.commands.insert(state.commands.end(), output.commands.begin(), output.commands.end());
    }
}
void classic_floating_pages(ViewState& state) {
    const char* titles[]{"Control###druta_float_control", "Advanced###druta_float_advanced",
        "Monitor###druta_float_monitor", "Timings###druta_float_timings"};
    for (int index = 0; index < 4; ++index) {
        if (!state.classic_floating[index]) continue;
        ImGui::SetNextWindowSize({classic_px(760), classic_px(640)}, ImGuiCond_FirstUseEver);
        const auto display = ImGui::GetIO().DisplaySize;
        ImGui::SetNextWindowSizeConstraints({std::min(classic_px(320), display.x), std::min(classic_px(240), display.y)},
            {display.x, display.y});
        if (state.classic_float_focus[index]) {
            const float offset = 24.0f * static_cast<float>(index);
            ImGui::SetNextWindowPos({classic_px(60 + offset), classic_px(90 + offset)}, ImGuiCond_Appearing);
            ImGui::SetNextWindowFocus(); state.classic_float_focus[index] = false;
        }
        bool open = true;
        if (ImGui::Begin(titles[index], &open, ImGuiWindowFlags_NoSavedSettings | ImGuiWindowFlags_NoCollapse)) {
            if (ImGui::Button("Dock back")) { state.classic_floating[index] = false; state.page = classic_pages[index]; }
            ImGui::SameLine(); ImGui::TextDisabled("Same GPU session; shared drafts. Close also docks this page.");
            if (state.classic_floating[index]) {
                ImGui::BeginChild("floating-page-content", {0, 0});
                classic_page_content(state, classic_pages[index]);
                ImGui::EndChild();
            }
        }
        ImGui::End();
        if (!open) { state.classic_floating[index] = false; state.page = classic_pages[index]; }
    }
}

} // namespace

bool front_clock_capture_ready(const ViewState& state) {
    if (!state.live || state.page != Page::daily) return true;
    const auto& s = *state.live;
    if (!front_clock_candidate(s) || !s.data.connected || s.data.pascal_clock_fresh) return true;
    const auto attempts = state.pascal_clock_view.generation == s.generation ? state.pascal_clock_view.automatic_reads : 0;
    return !s.busy && !front_clock_read_needed(s, attempts);
}

bool ViewState::has_draft() const {
    if (!preview) {
        for (const auto& draft : drafts) if (draft.dirty) return true;
        return !pascal_clock_view.drafts.empty();
    }
    return core_offset_mhz != 0.0f || memory_offset_mhz != 0.0f
        || power_limit_w != 310.0f || fan_percent != 30.0f;
}

void ViewState::revert_draft() {
    if (!preview) {
        for (auto& draft : drafts) {
            if (draft.baseline) draft.value = *draft.baseline;
            draft.dirty = false;
            draft.editing = false;
        }
        pascal_clock_view.discard();
        return;
    }
    core_offset_mhz = memory_offset_mhz = 0.0f;
    power_limit_w = 310.0f;
    fan_percent = 30.0f;
}

FrequencyLockViewContext frequency_lock_context(const ViewState& state) {
    FrequencyLockViewContext context;
    if (!state.live) return context;
    const auto& snapshot=*state.live;
    context.generation=snapshot.generation; context.slot=snapshot.selected_slot; context.uuid=snapshot.data.identity.uuid;
    context.connected=snapshot.data.connected; context.busy=snapshot.busy || snapshot.opening;
    context.unlocked=state.controls_unlocked;
    const auto& i2c=snapshot.data.i2c;
    const auto& clock=snapshot.data.frequency;
    context.other_owned_clock=i2c.clock_owned && clock.current_known && clock.current &&
        clock.current->minimum==i2c.clock_mhz && clock.current->maximum==i2c.clock_mhz;
    context.fan_manual_available=snapshot.data.controls[static_cast<size_t>(ControlId::fan)].writable;
    context.live_core_mhz=telemetry_value(snapshot,DC_T_CORE,snapshot.data.telemetry.core);
    return context;
}

void queue_frequency_lock_intents(ViewState& state, const std::vector<FrequencyLockIntent>& intents) {
    for (const auto& intent:intents) {
        Command command;
        switch(intent.action) {
        case FrequencyLockAction::refresh: command.operation=Command::Operation::frequency_lock_refresh; break;
        case FrequencyLockAction::apply: command.operation=Command::Operation::frequency_lock_apply; break;
        case FrequencyLockAction::lock_max: command.operation=Command::Operation::frequency_lock_max; break;
        case FrequencyLockAction::legacy_p0_max_fan: command.operation=Command::Operation::frequency_legacy_p0_max_fan; break;
        case FrequencyLockAction::release: command.operation=Command::Operation::frequency_lock_release; break;
        }
        command.frequency_min_mhz=intent.minimum_mhz; command.frequency_max_mhz=intent.maximum_mhz;
        state.commands.push_back(command);
    }
}

void apply_theme(float scale) {
    ImGuiStyle& style = ImGui::GetStyle();
    style = ImGuiStyle{};
    ImGui::StyleColorsDark(&style);
    style.WindowPadding = ImVec2(14, 10);
    style.FramePadding = ImVec2(10, 7);
    style.ItemSpacing = ImVec2(10, 9);
    style.ItemInnerSpacing = ImVec2(8, 6);
    style.CellPadding = ImVec2(8, 7);
    style.WindowRounding = 0;
    style.ChildRounding = 8;
    style.FrameRounding = 5;
    style.PopupRounding = 7;
    style.ScrollbarRounding = 8;
    style.GrabRounding = 4;
    style.WindowBorderSize = 0;
    style.ChildBorderSize = 1;
    style.Colors[ImGuiCol_WindowBg] = ImVec4(0.045f, 0.061f, 0.086f, 1);
    style.Colors[ImGuiCol_ChildBg] = ImVec4(0.045f, 0.061f, 0.086f, 1);
    style.Colors[ImGuiCol_Text] = ImVec4(0.90f, 0.93f, 0.97f, 1);
    style.Colors[ImGuiCol_TextDisabled] = muted;
    style.Colors[ImGuiCol_Border] = ImVec4(0.16f, 0.21f, 0.28f, 1);
    style.Colors[ImGuiCol_Separator] = ImVec4(0.16f, 0.21f, 0.28f, 1);
    style.Colors[ImGuiCol_FrameBg] = ImVec4(0.11f, 0.15f, 0.20f, 1);
    style.Colors[ImGuiCol_FrameBgHovered] = ImVec4(0.16f, 0.23f, 0.31f, 1);
    style.Colors[ImGuiCol_FrameBgActive] = ImVec4(0.19f, 0.29f, 0.40f, 1);
    style.Colors[ImGuiCol_Button] = ImVec4(0.13f, 0.22f, 0.32f, 1);
    style.Colors[ImGuiCol_ButtonHovered] = ImVec4(0.20f, 0.34f, 0.47f, 1);
    style.Colors[ImGuiCol_ButtonActive] = ImVec4(0.25f, 0.42f, 0.58f, 1);
    style.Colors[ImGuiCol_Header] = ImVec4(0.11f, 0.17f, 0.24f, 1);
    style.Colors[ImGuiCol_HeaderHovered] = ImVec4(0.16f, 0.26f, 0.36f, 1);
    style.Colors[ImGuiCol_HeaderActive] = ImVec4(0.20f, 0.34f, 0.47f, 1);
    style.Colors[ImGuiCol_CheckMark] = accent;
    style.Colors[ImGuiCol_SliderGrab] = accent;
    style.Colors[ImGuiCol_SliderGrabActive] = teal;
    style.ScaleAllSizes(scale);
    ImPlot::StyleColorsDark();
    ImPlotStyle& plot = ImPlot::GetStyle();
    plot.PlotBorderSize = 0;
    plot.PlotPadding = ImVec2(12 * scale, 10 * scale);
    plot.Colors[ImPlotCol_PlotBg] = ImVec4(0.055f, 0.078f, 0.11f, 1);
    plot.Colors[ImPlotCol_FrameBg] = ImVec4(0.055f, 0.078f, 0.11f, 1);
}

void draw(ViewState& state) {
    state.curve_inline_active = false;
    state.power_policies_open = false;
    state.automatic_clock_read.reset();
    if (state.page == Page::curve) {
        state.page = Page::daily;
        state.curve_window_open = true;
        state.curve_window_focus_requested = true;
    }
    if (!state.preview) {
        if (state.live) state.pascal_clock_view.sync(state.live->generation);
        sync_live_drafts(state); observe_curve(state); observe_power_request(state);
    }
    const bool classic = state.presentation == Presentation::classic;
    classic_fonts.classic = classic;
    if (!classic || !state.live) state.classic_reset_deadline = 0;
    if (classic && (state.page == Page::rails || state.page == Page::i2c)) state.page = Page::daily;
    unsigned classic_colors = 0;
    if (classic) {
        classic_colors = push_classic_palette();
        ImGui::PushFont(classic_fonts.body, 16.0f);
        // Released 1.9.1 binds no global theme, so it runs the stock Dear ImGui
        // metrics (ImGuiStyle{}); restore them over the Focused theme.
        const ImGuiStyle stock;
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(classic_px(stock.WindowPadding.x), classic_px(stock.WindowPadding.y)));
        ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(classic_px(stock.FramePadding.x), classic_px(stock.FramePadding.y)));
        ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(classic_px(stock.ItemSpacing.x), classic_px(stock.ItemSpacing.y)));
        ImGui::PushStyleVar(ImGuiStyleVar_ItemInnerSpacing, ImVec2(classic_px(stock.ItemInnerSpacing.x), classic_px(stock.ItemInnerSpacing.y)));
        ImGui::PushStyleVar(ImGuiStyleVar_CellPadding, ImVec2(classic_px(stock.CellPadding.x), classic_px(stock.CellPadding.y)));
        ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, 0);
        ImGui::PushStyleVar(ImGuiStyleVar_ChildRounding, 0);
        ImGui::PushStyleVar(ImGuiStyleVar_WindowRounding, 0);
        ImGui::PushStyleVar(ImGuiStyleVar_PopupRounding, 0);
        ImGui::PushStyleVar(ImGuiStyleVar_GrabRounding, 0);
        // Dear PyGui draws a 1 px (78,78,78) border round the root window.
        ImGui::PushStyleVar(ImGuiStyleVar_WindowBorderSize, 1);
    }
    unsigned risk_colors = 0;
    if (!state.preview && state.live) {
        const auto risk = session_risk(state.live->data);
        if (risk.score || (state.controls_unlocked && risk.band == RiskBand::green)) {
            auto tint = [](RiskColor c) { return ImVec4(c.r / 255.0f, c.g / 255.0f, c.b / 255.0f, 1); };
            ImGui::PushStyleColor(ImGuiCol_WindowBg, tint(risk.tint.background));
            ImGui::PushStyleColor(ImGuiCol_ChildBg, tint(risk.tint.background));
            ImGui::PushStyleColor(ImGuiCol_Border, tint(risk.tint.border));
            ImGui::PushStyleColor(ImGuiCol_TitleBgActive, tint(risk.tint.border));
            risk_colors = 4;
        }
    }
    const bool compact = short_view();
    if (compact && !classic) {
        ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(u(8), u(4)));
        ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(u(8), u(3)));
        ImGui::PushStyleVar(ImGuiStyleVar_CellPadding, ImVec2(u(6), u(3)));
    }
    ImGui::SetNextWindowPos(ImVec2(0, 0));
    ImGui::SetNextWindowSize(ImGui::GetIO().DisplaySize);
    const auto flags = ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoResize
        | ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoSavedSettings
        | ImGuiWindowFlags_NoBringToFrontOnFocus | ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_MenuBar;
    ImGui::Begin("Druta workspace", nullptr, flags);
    state.limited_deflatten_ready = classic && !state.preview && state.live && curve_plan_possible(state);
    if (classic) classic_header(state); else top_bar(state);
    // Clocks > Limited de-flatten stages the same local plan as the editor's
    // button; nothing is written until Apply.
    if (state.limited_deflatten_requested) {
        state.limited_deflatten_requested = false;
        if (state.limited_deflatten_ready) classic_stage_curve_plan(state, CurvePlanKind::deflatten);
    }
    if (!state.preview && state.live) {
        if (classic) {
            // 1.9.1 risk_banner: above the gate row while the score is above zero,
            // in the band's text colour, wrapped at the knob-table width:
            // "<BAND> (risk N) - " + RISK_BAND_TEXT + RISK_FEATURE_TEXT per live
            // feature, heaviest first, joined by two spaces. The native weights
            // and bands stay (approved); each native band covers the same feature
            // sets as one 1.9.1 band (yellow = XOC, amber = Rail limits: one
            // software envelope off, 1.9.1 AMBER; red = I2C, or XOC with Rail
            // limits: 1.9.1 RED; crimson = I2C with anything: 1.9.1 CRIMSON), so
            // the sentence is keyed by native band. A known adapter that leaves a
            // mode state unknown keeps that visible here.
            const auto risk = session_risk(state.live->data);
            const bool unknown = risk.band == RiskBand::unknown && state.live->data.connected && !state.live->opening;
            if (risk.score || unknown) {
                std::string banner = risk.name + " (risk " + std::to_string(risk.score) + ") - ";
                const char* band_text = risk.band == RiskBand::yellow || risk.band == RiskBand::amber
                    ? "A software envelope has been removed. Requests still go through the driver, so the GPU's own reliability ceiling is still underneath you."
                    : risk.band == RiskBand::red
                    ? "A write on this path reaches hardware with no firmware clamp on it, or two software envelopes are off at once."
                    : risk.band == RiskBand::crimson
                    ? "NOTHING MEANINGFUL IS BETWEEN THESE SLIDERS AND YOUR HARDWARE. A wrong number here kills the card."
                    : "";
                std::string parts = band_text;
                const auto part = [&](const std::string& text) { parts += (parts.empty() ? "" : "  ") + text; };
                if (risk.i2c == RiskFlag::active)
                    part("I2C: direct board-controller writes bypass the GPU's voltage request limits. Register readback is not "
                        "a physical rail-voltage measurement; changes may persist through a reboot.");
                if (risk.rail_limits == RiskFlag::active)
                    part("RAIL LIMITS: the driver-held per-rail voltage ceilings are being written directly. EXPERIMENTAL.");
                if (risk.xoc == RiskFlag::active) {
                    // The cap is the core's own XOC request envelope (1.9.1 POWER_LIMIT_XOC_MAX_MW).
                    char cap[32]{}; std::snprintf(cap, sizeof(cap), "%g", DC_PL_XOC_MAX_MW / 1000.0);
                    part(std::string("XOC: expanded voltage and clock ranges; current limits use the API ceilings. Supported GPUs "
                        "also expose live extended power requests; the ") + cap + " W software request cap does not raise firmware "
                        "limits. Native policy limits can wrap at high requests.");
                }
                if (risk.incomplete) part("Some control states are unknown.");
                banner += parts;
                ImGui::AlignTextToFramePadding();
                ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + classic_px(720));
                ImGui::TextColored(rgb(risk.tint.text.r, risk.tint.text.g, risk.tint.text.b), "%s", banner.c_str());
                ImGui::PopTextWrapPos();
                hint("XOC +1; Rail limits +2; I2C +3. Green 0, yellow 1, amber 2, red 3, crimson 4+. "
                    "The score describes enabled control modes, not measured hardware safety. Locking edits does not release an active controller request.");
            }
        }
        draw_mode_bar(*state.live, state.controls_unlocked, state.commands, classic, [&] { draw_i2c_gate(state); }, &state.advanced_view);
        {
            const auto& s=*state.live;
            // 1.9.1 gate row: horizontal-group spacing round 16 / 12 px spacers,
            // 28 px tall Reset (150) and Max it (290) buttons.
            const auto spacer = [](float width) { ImGui::SameLine(); ImGui::Dummy(ImVec2(classic_px(width), 0)); ImGui::SameLine(); };
            const ImVec2 reset_size = classic ? ImVec2(classic_px(150), classic_px(28)) : ImVec2(0, 0);
            const ImVec2 max_size = classic ? ImVec2(classic_px(290), classic_px(28)) : ImVec2(0, 0);
            const char* max_label = classic ? "Max it  (fan + power + volts + curve)" : "Max it (fan + power + volts + curve)";
            if (classic) spacer(16);
            const bool reset_ready = s.data.connected && !s.busy && !s.opening && state.controls_unlocked && s.data.reset_controls_available;
            if (!reset_ready || state.classic_reset_deadline <= ImGui::GetTime()
                || state.classic_reset_generation != s.generation || state.classic_reset_slot != s.selected_slot
                || (state.select_slot && *state.select_slot != s.selected_slot)) state.classic_reset_deadline = 0;
            ImGui::BeginDisabled(!reset_ready);
            if (ImGui::Button("Reset controls", reset_size)) {
                if (!classic || state.classic_reset_deadline > ImGui::GetTime()) {
                    Command c; c.operation=Command::Operation::reset_controls; state.commands.push_back(c);
                    state.classic_reset_deadline = 0;
                } else {
                    // 1.9.1 reset_all: the first press arms with no timeout and
                    // says so in the log; nothing is added under the gate row.
                    state.classic_reset_deadline = std::numeric_limits<double>::infinity();
                    state.classic_reset_generation = s.generation; state.classic_reset_slot = s.selected_slot;
                    classic_log_line(state, "this zeroes offsets + voltage boost, restores the default power limit, releases the clock lock, "
                        "returns fans to auto and resets the V/F curve - press again to confirm", false);
                }
            }
            ImGui::EndDisabled();
            // Native-only, kept reachable: disarm without unlocking, from the Reset
            // button's right-click menu (1.9.1 has no cancel; it writes nothing).
            if (classic && ImGui::BeginPopupContextItem("##reset-arm")) {
                if (ImGui::MenuItem("Cancel reset", nullptr, false, state.classic_reset_deadline > ImGui::GetTime())) {
                    state.classic_reset_deadline = 0;
                    classic_log_line(state, "reset disarmed - nothing was written", std::nullopt);
                }
                ImGui::EndPopup();
            }
            if (classic) hint("First press arms; press again to reset offsets, voltage boost, power, fan and V/F curve, and release owned holds/I2C requests. "
                "Registry overrides are unchanged. Unticking Unlock controls, changing card, or Cancel reset in this button's right-click menu cancels the arm.");
            if (classic) spacer(12); else ImGui::SameLine();
            // 1.9.1: an adapter with legacy-P0 support gets Lock P0 and max fan
            // and Release P0 in Max it's place (the Frequency locks window keeps
            // its own copy).
            if (classic && legacy_p0_gate_shown(s.data.frequency))
                queue_frequency_lock_intents(state, draw_legacy_p0_gate(s.data.frequency, frequency_lock_context(state)));
            else {
            ImGui::PushStyleColor(ImGuiCol_Button, {168.0f/255, 88.0f/255, 16.0f/255, 1});
            // 1.9.1 ocmax_th: orange hover / active and dark label text.
            if (classic) {
                ImGui::PushStyleColor(ImGuiCol_ButtonHovered, {206.0f/255, 112.0f/255, 24.0f/255, 1});
                ImGui::PushStyleColor(ImGuiCol_ButtonActive, {238.0f/255, 138.0f/255, 34.0f/255, 1});
                ImGui::PushStyleColor(ImGuiCol_Text, {24.0f/255, 16.0f/255, 6.0f/255, 1});
            }
            ImGui::BeginDisabled(!s.data.connected || s.busy || s.curve_stale || !state.controls_unlocked || !s.data.max_it_available || state.curve_draft.dirty());
            if (ImGui::Button(max_label, max_size)) {
                Command c; c.operation=Command::Operation::max_it; c.expected_curve=s.data.curve; c.expected_curve_freq_div=s.data.curve_freq_div;
                c.curve_cap_mv=state.curve_cap_mv; c.curve_floor_mv=state.curve_floor_mv;
                c.hold_headroom=state.hold_headroom; c.headroom_margin_mv=state.headroom_margin_mv; state.commands.push_back(c);
            }
            ImGui::EndDisabled();
            ImGui::PopStyleColor(classic ? 4 : 1);
            // 1.9.1's Max it tooltip, with the undo, hold and step-order lines
            // stated as this build does them: Undo releases the hold first and
            // the steps stop at the first refusal.
            if (classic) classic_tooltip(
                "The four things done by hand at the start of every\n"
                "session, in one click and in a safe order:\n\n"
                "  1. fan            -> 100%\n"
                "  2. power limit    -> this card's maximum\n"
                "  3. voltage boost  -> 100%\n"
                "  4. V/F curve      -> de-flatten, apply, then hold\n"
                "                       the cap point (same as Ctrl+H)\n\n"
                "Headroom first, clocks last: cooling before the power\n"
                "budget rises, budget before the extra voltage spends\n"
                "it, and the curve last because it is the only step\n"
                "that asks for more clock.\n\n"
                "ONE undo point covers the four WRITES - Profiles >\n"
                "Undo last write puts them back.\n\n"
                "UNDO RELEASES THE HOLD FIRST. A lock is driver state,\n"
                "not a profile value, so Undo lets go of it before it\n"
                "puts the writes back. Ctrl+H, the Release button and\n"
                "'Reset all to stock' drop the hold too.\n\n"
                "Steps run in order and stop at the first refusal; the\n"
                "undo point is kept and the outcome is logged.\n\n"
                "The fans stay at 100% MANUAL until you press Auto or\n"
                "Reset all to stock - they do not ramp back down.\n\n"
                "De-flatten works BELOW the voltage cap in the V/F\n"
                "editor's cap box, so that box bounds what 'max'\n"
                "means. Unavailable while V/F edits are staged - it\n"
                "will not write a plan it did not make.\n\n"
                "De-flatten usually LOWERS the curve's nominal top:\n"
                "the cap point becomes the unique peak so the arbiter\n"
                "parks THERE instead of at the bottom of a flat run.\n\n"
                "This raises voltage, power and clocks together. It is\n"
                "an overclock, and it can destabilise the driver.");
            else hint("Uses the normal board maximum, 100% fan/boost, the selected ramp floor/cap, and a V/F hold. Apply or revert curve drafts first.");
            }
            // 1.9.1's gate row has no hold button or line: Classic releases from
            // Ctrl+H, the Clocks menu or Reset, and states the hold under the read-out.
            if (!classic && s.data.tuning.maintenance_needed()) {
                ImGui::SameLine(); ImGui::BeginDisabled(s.busy || hold_queued(state));
                if (ImGui::Button("Release hold (Ctrl+H)")) queue_hold_release(state);
                ImGui::EndDisabled();
                hold_status(s);
            }
            if (classic) {
                // 1.9.1 vlim_badge with the driver message in its tooltip, then
                // Retry (refresh_capabilities) and Copy info.
                spacer(12);
                const auto badge = rail_limit_badge(s);
                const ImVec4 badge_color = badge.state == RailLimitBadge::State::ready ? rgb(70, 209, 122)
                    : badge.state == RailLimitBadge::State::unavailable ? py_warn : py_dim;
                ImGui::TextColored(badge_color, "%s", badge.text.c_str());
                if (ImGui::IsItemHovered()) {
                    ImGui::BeginTooltip(); ImGui::PushTextWrapPos(classic_px(420));
                    ImGui::TextColored(badge.state == RailLimitBadge::State::ready ? rgb(70, 209, 122) : py_warn, "%s", badge.detail.c_str());
                    ImGui::PopTextWrapPos(); ImGui::EndTooltip();
                }
                ImGui::SameLine(); ImGui::BeginDisabled(s.opening || s.busy || bool(state.select_slot));
                if (ImGui::Button("Retry")) state.refresh_requested = true;
                ImGui::EndDisabled(); hint("Read this adapter's capabilities again, rail limits included. Nothing is written.");
                ImGui::SameLine();
                // 1.9.1 copy_rail_limit_diagnostics confirms the copy in the log.
                if (ImGui::Button("Copy info")) {
                    ImGui::SetClipboardText(rail_limit_diagnostics(s).c_str());
                    classic_log_line(state, "rail detection details copied to clipboard", true);
                }
                hint("Copy the rail-limit detection details for support. Nothing is written.");
                // 1.9.1 tear_hint_marker: the tab drag-off help sits here rather
                // than on the tab labels, whose tooltip would fire over the page.
                // The risk score is the banner above the row.
                spacer(12);
                ImGui::TextColored(py_dim, "(?)");
                classic_tooltip("Drag a Control, Advanced, Monitor or Timings tab off the selector to float that page in this window.\n"
                    "Dock back returns it. Right-click a tab for Float page.");
            }
            const auto& io = ImGui::GetIO();
            if (!io.AppFocusLost && !io.WantTextInput && !ImGui::IsAnyItemActive()
                && ImGui::IsWindowFocused(ImGuiFocusedFlags_AnyWindow)
                && !ImGui::IsPopupOpen(nullptr, ImGuiPopupFlags_AnyPopupId | ImGuiPopupFlags_AnyPopupLevel)
                && io.KeyCtrl && !io.KeyAlt && !io.KeySuper && !io.KeyShift && ImGui::IsKeyPressed(ImGuiKey_H, false)) {
                if (s.data.tuning.maintenance_needed()) queue_hold_release(state);
                else queue_selected_hold(state);
            }
        }
        // 1.9.1 unlock_note: a WARN line under the gate row only while read-only;
        // no rule separates the row from the tabs.
        if (classic && !state.controls_unlocked)
            ImGui::TextColored(py_warn, "READ-ONLY - every write control below is disabled%s",
                state.live->data.curve.empty() ? "" : " (curve edits are still staged, not written)");
        if (!classic) ImGui::Separator();
    }
    if (classic) classic_tabs(state);
    const bool narrow = classic || ImGui::GetContentRegionAvail().x < u(900);
    if (narrow && !classic) {
        navigation(state, true);
        ImGui::Separator();
    }
    const char* footer = state.preview
        ? (state.hotkey_available ? "Sample data only. Draft edits stay in this window."
            : "Sample data only. Restore shortcut unavailable; use the taskbar.")
        : (state.hotkey_available ? "Session-only tuning. Saved settings apply only when you load them."
            : "Session-only tuning. Restore shortcut unavailable; use the taskbar.");
    // 1.9.1 has no footer; Classic keeps the hotkey state in Help > Keyboard
    // shortcuts and the Pause tooltip, and session-only tuning in About.
    const float footer_height = classic ? 0.0f : ImGui::CalcTextSize(footer, nullptr, false,
        ImGui::GetContentRegionAvail().x).y + ImGui::GetStyle().ItemSpacing.y * 2 + u(3);
    const float log_height = !classic && !state.preview && state.live
        ? ImGui::GetTextLineHeightWithSpacing() * (state.live->log.empty() ? 1 : 3) + u(8) : 0;
    const float body_height = std::max(u(50), ImGui::GetContentRegionAvail().y - footer_height - log_height);
    if (!narrow) {
        ImGui::BeginChild("navigation", ImVec2(u(162), body_height), false);
        navigation(state, false);
        ImGui::EndChild();
        ImGui::SameLine(0, u(18));
    }
    ImGui::BeginChild("workspace", ImVec2(0, body_height), false);
    const bool verbose = state.presentation == Presentation::classic;
    if (classic) {
        const int index = classic_page_index(state.page);
        if (index >= 0 && state.classic_floating[index]) ImGui::TextUnformatted("This page is floating. Use Dock back in its window to return it.");
        else classic_page_content(state, state.page);
    } else if (!state.preview) {
        if (!state.live) ImGui::TextUnformatted("Waiting for the first session snapshot...");
        else if (state.page == Page::daily) { if (classic) classic_daily(state); else live_daily(state, false); }
        else if (state.page == Page::advanced) { if (classic) classic_advanced(state); else live_advanced(state, false); }
        else if (state.page == Page::rails) {
            title("Rails", "Rail limits and voltage offsets.");
            draw_daily_rail_controls(*state.live, state.controls_unlocked, false, state.advanced_view, state.commands);
            if (!state.advanced_view.notice.empty()) ImGui::TextWrapped("%s",state.advanced_view.notice.c_str());
        }
        else if (state.page == Page::i2c) { title("I2C", "Inspect and control the selected I2C regulator."); draw_i2c_panel(state); }
        else if (state.page == Page::timings) {
            const auto& s=*state.live;
            firmware_timing_panel(state);
            auto output=draw_timings(state.timings_view,s.data.timings.get(),s.generation,s.busy,state.controls_unlocked,s.data.connected);
            state.timing_capture_cancel_requested|=output.cancel_capture;
            state.timing_helper_requested|=output.choose_helper;
            state.timing_forget_requested|=output.forget_helper;
            state.test_signing_requested|=output.test_signing_setup;
            state.timing_save_profile_requested|=output.save_profile;
            state.timing_load_profile_requested|=output.load_profile;
            state.commands.insert(state.commands.end(),output.commands.begin(),output.commands.end());
        }
        else { if (classic) classic_monitor(*state.live,state.shunt); else live_monitor(*state.live, false,state.shunt); }
    } else if (state.page == Page::daily) daily(state, verbose);
    else if (state.page == Page::advanced) advanced(verbose);
    else monitor(verbose);
    ImGui::EndChild();
    if (!classic && !state.preview && state.live) live_operations(*state.live);
    if (!classic) {
        ImGui::Separator();
        ImGui::PushTextWrapPos(0);
        ImGui::TextColored(state.hotkey_available ? muted : amber, "%s", footer);
        ImGui::PopTextWrapPos();
    }
    ImGui::End();
    draw_app_actions_windows(state);
    if (state.live)
        queue_frequency_lock_intents(state, draw_frequency_lock(state.frequency_view,state.live->data.frequency,frequency_lock_context(state)));
    if (state.live) {
        const auto& s=*state.live;
        draw_scoped_profile_workspace(state.scoped_profile_view, s, state.controls_unlocked, state.commands);
        const auto output=draw_policy_workspace(state.policy_workspace,state.policy_annotations,s.data.scoped_profiles.get(),s.generation,s.busy||s.opening,s.data.connected,state.controls_unlocked);
        state.save_policy_annotations|=output.save_annotations;state.import_policy_annotations|=output.import_annotations;
        if(output.refresh) {Command c;c.operation=Command::Operation::prepare_scoped_profile;state.commands.push_back(c);}
        if(output.apply) {Command c;c.operation=Command::Operation::apply_scoped_profile;auto request=std::make_shared<ScopedProfileRequest>();request->document=*output.apply;c.scoped_profile=std::move(request);state.commands.push_back(c);}
    }
    {
        auto output=draw_profile_catalog_workspace(state.profile_catalog_view,state.live ? state.live->generation : 0,
            !state.live || state.live->busy || state.live->opening || !state.live->data.connected,state.controls_unlocked);
        // Whether the profile applied next re-reads the curve as 1.9.1's full load does.
        for (const auto& intent : output.intents)
            if (intent.operation == ProfileCatalogIntent::Operation::apply) state.classic_catalog_rereads = profile_rereads_curve(intent.document);
        state.profile_catalog_intents.insert(state.profile_catalog_intents.end(),output.intents.begin(),output.intents.end());
    }
    if (classic) classic_floating_pages(state);
    draw_curve_window(state);
    draw_shunt_window(state.shunt);
    if (classic && !state.preview && state.live) curve_keyboard(state, true);
    if (compact && !classic) ImGui::PopStyleVar(3);
    if (risk_colors) ImGui::PopStyleColor(static_cast<int>(risk_colors));
    if (classic) { ImGui::PopStyleVar(11); ImGui::PopFont(); ImGui::PopStyleColor(static_cast<int>(classic_colors)); }
}

} // namespace druta::ui
