// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "ui_view.h"
#include "session.h"

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
    if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled)) {
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
        {Page::daily, "Daily tuning"}, {Page::advanced, "Advanced"}, {Page::monitor, "Monitor"}, {Page::curve, "V/F curve"}
    };
    const int count = 3; // The V/F editor is a large independent window from Daily.
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
            : "Apply is explicit. A driver reset starts a fresh session.");
        ImGui::PopStyleColor();
    }
}

void top_bar(ViewState& state) {
    const ImGuiStyle& style = ImGui::GetStyle();
    const float control_width = ImGui::CalcTextSize("Always refresh").x
        + ImGui::GetFrameHeight() + style.ItemInnerSpacing.x + u(60 + 164)
        + style.ItemSpacing.x * 2 + u(12);
    const float brand_width = ImGui::CalcTextSize("DRUTA").x * 1.2f + u(12)
        + ImGui::CalcTextSize("PREVIEW / SAMPLE DATA").x + style.FramePadding.x * 2;
    const bool wrap_controls = ImGui::GetContentRegionAvail().x < brand_width + u(20) + control_width;
    ImGui::SetWindowFontScale(1.2f);
    ImGui::TextColored(accent, "DRUTA");
    ImGui::SetWindowFontScale(1.0f);
    ImGui::SameLine(0, u(12));
    badge(state.preview ? "PREVIEW / SAMPLE DATA" : "LIVE SESSION", state.preview ? amber : teal);
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
    if (ImGui::Combo("##presentation", &presentation, "Focused\0Classic / Verbose\0"))
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
        state.controls_unlocked = false;
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
    if (point == points.end()) {
        point = points.begin();
        state.selected_curve_point = point->idx;
        state.curve_entry_dirty = false;
    }
    if (!state.curve_entry_dirty && !state.curve_entry_editing && state.curve_draft.freq_div())
    {
        state.curve_delta_mhz = point->delta_khz / (1000.0 * state.curve_draft.freq_div());
        state.curve_frequency_mhz = point->freq_mhz;
    }
}

void observe_curve(ViewState& state) {
    if (!state.live) return;
    const auto& snapshot = *state.live;
    const bool generation = !state.curve_observation_initialized || state.observed_curve_generation != snapshot.generation;
    const bool revision = !state.curve_observation_initialized || state.observed_curve_revision != snapshot.curve_revision;
    const bool stale = snapshot.curve_stale || !snapshot.data.connected || snapshot.curve_revision == 0;
    if (generation || revision || (stale && !state.observed_curve_stale)) {
        state.curve_draft.observe(snapshot.generation, snapshot.data.curve, !stale, snapshot.data.curve_freq_div);
        if (generation) {
            state.curve_entry_dirty = false; state.curve_entry_editing = false;
            state.curve_drag_point.reset(); state.curve_fit_requested = true;
            state.acknowledged_curve_action = snapshot.completed_actions;
            state.curve_plan_message.clear();
        }
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
    state.observed_curve_stale = stale;
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

void live_summary(const SessionSnapshot& snapshot) {
    const auto& telemetry = snapshot.data.telemetry;
    if (ImGui::BeginTable("live_summary", 3, ImGuiTableFlags_SizingStretchSame)) {
        ImGui::TableNextColumn();
        measured_value(snapshot, "Core", DC_T_CORE, telemetry.core, "MHz", 0);
        ImGui::TableNextColumn();
        measured_value(snapshot, "GPU", DC_T_TEMP_EDGE, telemetry.temp_edge, "C", 0);
        ImGui::TableNextColumn();
        measured_value(snapshot, "Board", DC_T_POWER_W, telemetry.power_w, "W", 1);
        ImGui::EndTable();
    }
    if (snapshot.paused) ImGui::TextColored(amber, "Telemetry paused; displayed readings are last known.");
    if (!snapshot.data.telemetry_error.empty())
        ImGui::TextWrapped("Telemetry: %s", snapshot.data.telemetry_error.c_str());
}

void live_control_row(ViewState& state, ControlId id, const char* name, bool verbose) {
    const size_t index = static_cast<size_t>(id);
    const auto& snapshot = *state.live;
    const auto& control = snapshot.data.controls[index];
    auto& draft = state.drafts[index];
    if (!verbose) {
        ImGui::TableNextRow();
        ImGui::TableNextColumn();
    }
    ImGui::PushID(static_cast<int>(id));
    if (verbose) ImGui::Separator();
    ImGui::AlignTextToFramePadding();
    ImGui::Text("%s%s", name, draft.dirty ? " *" : "");
    if (!control.detail.empty()) hint(control.detail.c_str());
    if (verbose) ImGui::SameLine();
    else ImGui::TableNextColumn();
    ImGui::AlignTextToFramePadding();
    ImGui::TextColored(control.availability == Availability::transient ? amber : secondary, "%s%s",
        verbose ? "Configured: " : "", number_text(control.current, control.unit.c_str()).c_str());
    hint(control.detail.empty() ? availability_text(control.availability) : control.detail.c_str());
    if (draft.dirty) {
        if (verbose) { ImGui::SameLine(); ImGui::TextColored(amber, "Draft"); }
    }
    if (!verbose) ImGui::TableNextColumn();
    const bool editable = control.writable && draft.initialized && snapshot.data.connected
        && !snapshot.opening && !snapshot.busy;
    const bool ranged = control.low && control.high && std::isfinite(*control.low)
        && std::isfinite(*control.high) && *control.low < *control.high;
    ImGui::BeginDisabled(!editable);
    bool edited = false;
    bool editing = false;
    const float available = ImGui::GetContentRegionAvail().x;
    const bool slider = ranged && available > u(verbose ? 490.0f : 285.0f);
    if (slider) {
        ImGui::SetNextItemWidth(std::max(u(90), available - u(verbose ? 370.0f : 185.0f)));
        if (ImGui::SliderScalar("##slider", ImGuiDataType_Double, &draft.value,
            &*control.low, &*control.high, id == ControlId::memory ? "%.0f" : "%.6g", ImGuiSliderFlags_NoInput)) {
            if (id == ControlId::memory) draft.value = std::round(draft.value);
            edited = true;
        }
        editing = ImGui::IsItemActive();
        ImGui::SameLine();
    }
    if (draft.initialized) {
        ImGui::SetNextItemWidth(std::min(u(verbose ? 130.0f : 105.0f), std::max(u(75), available - u(verbose ? 230.0f : 0.0f))));
        double entered = draft.value;
        if (ImGui::InputDouble("##value", &entered, 0, 0, "%.9g") && std::isfinite(entered)) {
            draft.value = entered;
            edited = true;
        }
        editing = editing || ImGui::IsItemActive();
        hint("Type an exact draft value; dragging memory uses whole MHz. Editing never writes hardware.");
    } else {
        ImGui::AlignTextToFramePadding();
        ImGui::TextUnformatted("--");
    }
    if (verbose) { ImGui::SameLine(); ImGui::TextUnformatted(control.unit.c_str()); }
    ImGui::EndDisabled();
    draft.editing = editing;
    if (edited) draft.dirty = !draft.baseline || draft.value != *draft.baseline;
    const bool in_range = std::isfinite(draft.value)
        && (!control.low || draft.value >= *control.low) && (!control.high || draft.value <= *control.high);
    if (verbose) ImGui::SameLine();
    else ImGui::TableNextColumn();
    ImGui::BeginDisabled(!editable || !state.controls_unlocked || !in_range);
    if (ImGui::Button("Apply")) state.commands.push_back({Command::Operation::apply, id, draft.value});
    ImGui::EndDisabled();
    if (verbose) ImGui::SameLine();
    else ImGui::TableNextColumn();
    // Auto/Stock can be an explicit escape even when a current request cannot
    // be captured. Its independent backend capability never follows a draft.
    const bool stock_ready = control.stock_writable && snapshot.data.connected
        && !snapshot.opening && !snapshot.busy && state.controls_unlocked;
    ImGui::BeginDisabled(!stock_ready);
    if (ImGui::Button(id == ControlId::fan ? "Auto" : "Stock"))
        state.commands.push_back({Command::Operation::stock, id, control.stock.value_or(0)});
    ImGui::EndDisabled();
    if (verbose)
        ImGui::TextColored(control.availability == Availability::transient ? amber : muted,
            "%s%s", availability_text(control.availability), control.writable ? "" : " / read-only");
    if (!in_range && draft.initialized) hint("Draft is outside the reported range; Apply is disabled.");
    if (!control.detail.empty() && verbose)
        ImGui::TextWrapped("%s", control.detail.c_str());
    if (verbose && ranged) ImGui::TextColored(muted, "Range: %s to %s",
        number_text(control.low, control.unit.c_str()).c_str(), number_text(control.high, control.unit.c_str()).c_str());
    ImGui::PopID();
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

void curve_keyboard(ViewState& state) {
    const auto& io = ImGui::GetIO();
    if (!ImGui::IsWindowFocused(ImGuiFocusedFlags_RootAndChildWindows)
        || io.AppFocusLost || io.WantTextInput || state.curve_entry_editing || ImGui::IsAnyItemActive()) return;
    auto& editor = state.curve_draft;
    const auto& points = editor.draft();
    if (points.empty()) return;
    if (io.KeyCtrl) {
        const bool z = ImGui::IsKeyPressed(ImGuiKey_Z);
        const bool y = ImGui::IsKeyPressed(ImGuiKey_Y);
        if ((z || y) && ImGui::GetTime() - state.curve_last_undo_time >= 0.4) {
            state.curve_last_undo_time = ImGui::GetTime();
            if (y || io.KeyShift) editor.redo_edit(); else editor.undo_edit();
            state.curve_entry_dirty = false;
            sync_curve_entry(state);
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
    if (direction) {
        const auto& data = state.live->data;
        editor.stage_frequency_mhz(selected->idx, selected->freq_mhz
            + static_cast<double>(direction * multiplier) * data.curve_step_khz / 1000.0,
            data.curve_step_khz, data.curve_max_abs_delta_khz);
        state.curve_entry_dirty = false;
        sync_curve_entry(state);
    }
}

void interactive_curve_plot(ViewState& state) {
    const auto& snapshot = *state.live;
    auto& editor = state.curve_draft;
    const auto& points = editor.draft();
    if (points.empty()) {
        ImGui::TextWrapped("Curve unavailable: %s", snapshot.data.curve_error.empty() ? editor.status().c_str() : snapshot.data.curve_error.c_str());
        return;
    }
    if (state.curve_drag_point && (!ImGui::IsMouseDown(ImGuiMouseButton_Left)
        || ImGui::GetIO().AppFocusLost || !ImGui::IsWindowFocused(ImGuiFocusedFlags_RootAndChildWindows))) end_curve_drag(state);
    const auto saved_input = ImPlot::GetInputMap();
    auto& input = ImPlot::GetInputMap();
    input.Pan = state.curve_drag_point ? 4 : ImGuiMouseButton_Left;
    input.Fit = 4; // Fit view is explicit; a double click on a dot is still a dot grab.
    if (state.curve_fit_requested) ImPlot::SetNextAxesToFit();
    const float height = std::max(u(180), ImGui::GetContentRegionAvail().y);
    const auto flags = ImPlotFlags_NoTitle | ImPlotFlags_NoMouseText | ImPlotFlags_NoMenus | ImPlotFlags_NoBoxSelect;
    ImPlot::PushStyleVar(ImPlotStyleVar_FitPadding, ImVec2(0.03f, 0.06f));
    if (ImPlot::BeginPlot("##live_curve", ImVec2(-1, height), flags)) {
        double xmin = points.front().volt_mv, xmax = xmin, ymin = points.front().freq_mhz, ymax = ymin;
        for (const auto& point : points) {
            xmin = std::min(xmin, point.volt_mv); xmax = std::max(xmax, point.volt_mv);
            ymin = std::min(ymin, point.freq_mhz); ymax = std::max(ymax, point.freq_mhz);
        }
        xmin -= 25; xmax += 25;
        ymin = std::min(0.0, ymin - 120); ymax = std::max(3000.0, ymax + 200);
        ImPlot::SetupAxes("Voltage (mV)", "Core (MHz)");
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
                editor.begin_gesture();
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
        ImPlotSpec current;
        current.LineColor = secondary; current.LineWeight = u(2);
        current.Marker = ImPlotMarker_Circle; current.MarkerSize = u(5);
        current.MarkerFillColor = secondary;
        ImPlot::PlotLine("Current", current_x.data(), current_y.data(), static_cast<int>(current_count), current);
        ImPlotSpec edited;
        edited.LineColor = accent; edited.LineWeight = u(1);
        edited.Marker = ImPlotMarker_Circle; edited.MarkerSize = u(6);
        edited.MarkerFillColor = accent;
        edited.MarkerLineColor = ImVec4(0.055f, 0.078f, 0.11f, 1);
        ImPlot::PlotLine("Draft", draft_x.data(), draft_y.data(), static_cast<int>(draft_count), edited);
        const auto selected = std::find_if(points.begin(), points.end(), [&](const auto& point) { return point.idx == state.selected_curve_point; });
        if (selected != points.end()) {
            ImPlotSpec mark;
            mark.Marker = ImPlotMarker_Diamond; mark.MarkerSize = u(12);
            mark.MarkerFillColor = amber; mark.MarkerLineColor = amber;
            ImPlot::PlotScatter("Selected", &selected->volt_mv, &selected->freq_mhz, 1, mark);
        }
        const auto& telemetry = snapshot.data.telemetry;
        if ((telemetry.present & (DC_T_VCORE_MV | DC_T_CORE)) == (DC_T_VCORE_MV | DC_T_CORE)
            && !(snapshot.telemetry_stale_mask & (DC_T_VCORE_MV | DC_T_CORE)) && !snapshot.paused) {
            const double clock = telemetry.core;
            ImPlotSpec observed;
            observed.Marker = ImPlotMarker_Circle; observed.MarkerSize = u(4);
            observed.MarkerFillColor = teal;
            ImPlot::PlotScatter("Observed operating point", &telemetry.vcore_mv, &clock, 1, observed);
        }
        ImPlot::EndPlot();
        state.curve_fit_requested = false;
    }
    ImPlot::PopStyleVar();
    ImPlot::GetInputMap() = saved_input;
}

void live_daily(ViewState& state, bool verbose) {
    const auto& snapshot = *state.live;
    if (!short_view()) title(verbose ? "Control" : "Daily tuning", "Stage a request, then Apply. Undo restores the recorded request.");
    live_summary(snapshot);
    ImGui::Checkbox("Unlock tuning controls", &state.controls_unlocked);
    hint("Enable explicit Apply and Stock actions in this session. Editing a draft does not write.");
    ImGui::SameLine();
    ImGui::BeginDisabled(snapshot.busy || snapshot.opening);
    if (ImGui::Button("Refresh readings")) state.refresh_requested = true;
    ImGui::EndDisabled();
    const char* names[] = {"Core offset", "Memory offset", "Board power", "Fan target", "Voltage boost"};
    if (verbose) {
        for (size_t i = 0; i < snapshot.data.controls.size(); ++i)
            live_control_row(state, static_cast<ControlId>(i), names[i], true);
    } else if (ImGui::BeginTable("daily_controls", 5, ImGuiTableFlags_SizingStretchProp | ImGuiTableFlags_RowBg)) {
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
    ImGui::BeginDisabled(!state.controls_unlocked || !snapshot.data.undo_available || !snapshot.data.connected
        || snapshot.busy || snapshot.opening);
    if (ImGui::Button("Undo hardware write")) state.commands.push_back({Command::Operation::undo, ControlId::memory, 0});
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

void live_curve_workspace(ViewState& state, bool verbose) {
    const auto& snapshot = *state.live;
    auto& editor = state.curve_draft;
    const auto& points = editor.draft();
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
    ImGui::EndDisabled();
    ImGui::Checkbox("Unlock tuning controls", &state.controls_unlocked);
    ImGui::SameLine();
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
    ImGui::BeginDisabled(!state.controls_unlocked || !snapshot.data.connected || !snapshot.data.undo_available || snapshot.busy || snapshot.opening);
    if (ImGui::Button("Undo hardware write")) state.commands.push_back({Command::Operation::undo, ControlId::core, 0});
    ImGui::EndDisabled();
    if (ImGui::CollapsingHeader("Curve shape tools")) {
        ImGui::SetNextItemWidth(u(100)); ImGui::InputDouble("Cap (mV)", &state.curve_cap_mv, 0, 0, "%.3f");
        ImGui::SameLine(); ImGui::SetNextItemWidth(u(100)); ImGui::InputDouble("Ramp floor (mV)", &state.curve_floor_mv, 0, 0, "%.3f");
        std::optional<CurvePlanKind> kind;
        const bool can_plan = !points.empty() && snapshot.data.curve_step_khz && snapshot.data.curve_max_abs_delta_khz
            && snapshot.data.curve_model_max_rise_khz && state.planner_api.predict;
        ImGui::BeginDisabled(!can_plan);
        if (ImGui::Button("Stage de-flatten")) kind = CurvePlanKind::deflatten;
        ImGui::SameLine(); if (ImGui::Button("Stage ramp")) kind = CurvePlanKind::ramp;
        ImGui::SameLine(); if (ImGui::Button("Stage re-phase")) kind = CurvePlanKind::rephase;
        ImGui::EndDisabled();
        hint("Shape tools stage local edits. Prediction uses the backend's software model; Apply and readback establish the stored request.");
        if (kind) {
            CurvePlanRequest request;
            request.kind = *kind; request.points = points; request.freq_div = editor.freq_div();
            request.step_khz = snapshot.data.curve_step_khz; request.max_abs_delta_khz = snapshot.data.curve_max_abs_delta_khz;
            request.max_rise_khz = snapshot.data.curve_model_max_rise_khz;
            if (*kind != CurvePlanKind::rephase) request.cap_mv = state.curve_cap_mv;
            if (*kind == CurvePlanKind::ramp) request.floor_mv = state.curve_floor_mv;
            const auto plan = plan_curve(state.planner_api, request);
            state.curve_plan_message = plan.message;
            if (plan.ok && !plan.changes.empty()) {
                auto staged = editor; staged.end_gesture(); staged.begin_gesture(); bool valid = true;
                for (const auto& change : plan.changes) valid = staged.stage_khz(static_cast<uint32_t>(change.idx), static_cast<double>(change.delta_khz)) && valid;
                staged.end_gesture();
                if (valid) {
                    editor = std::move(staged); state.curve_entry_dirty = false; sync_curve_entry(state);
                    state.curve_plan_message = "Staged " + std::to_string(plan.changes.size()) + " points. Review the dots before Apply.";
                    if (!plan.predicted_khz.empty()) state.curve_plan_message += " Model peak: " +
                        number_text(*std::max_element(plan.predicted_khz.begin(), plan.predicted_khz.end()) / 1000.0, "MHz") + " (estimate).";
                } else state.curve_plan_message = "Plan could not be staged; draft unchanged";
            }
        }
        if (!state.curve_plan_message.empty()) ImGui::TextWrapped("%s", state.curve_plan_message.c_str());
    }
    curve_keyboard(state);
    const char* status = snapshot.curve_stale ? "Curve stale; edits retained" : editor.status().c_str();
    ImGui::TextColored(editor.dirty() ? amber : secondary, "%s", status);
    ImGui::TextColored(muted, "Drag dots | A/D select | W/S +/- %.9g MHz | Shift x3 | Ctrl+Z / Y",
        snapshot.data.curve_step_khz / 1000.0);
    if (ImGui::CollapsingHeader("Curve details")) {
        ImGui::TextWrapped("Set MHz floors onto the captured point's clock phase. Drag snaps half up; voltage stays fixed. Empty left-drag pans; wheel zooms. Ctrl+Shift+Z also redoes local edits (64 deep). Closing keeps the draft. Apply and hardware Undo are separate explicit commands.");
        ImGui::TextWrapped("Current card grid: %.9g MHz. Request divisor: %u. Draft frequency predicts a request, not a measurement. Holds/planners remain pending.",
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
    if (!state.curve_window_open) { end_curve_drag(state); return; }
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

void live_advanced(ViewState& state, bool verbose) {
    const auto& snapshot = *state.live;
    title("Advanced", "Stage a selected request, then Apply. Details explain reported capabilities.");
    ImGui::BeginDisabled(snapshot.busy || snapshot.opening);
    if (ImGui::Button("Refresh details")) state.refresh_requested = true;
    ImGui::SameLine();
    if (ImGui::Button("Reconnect")) state.reconnect_requested = true;
    ImGui::EndDisabled();
    draw_advanced_controls(snapshot, state.controls_unlocked, verbose, state.advanced_view, state.commands);
    const auto flags = verbose ? ImGuiTreeNodeFlags_DefaultOpen : ImGuiTreeNodeFlags_None;
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
        ImGui::TextWrapped("Unreadable lock state does not establish an unlocked GPU. Owned holds and recovery controls are not enabled here yet.");
        if (!snapshot.data.status.empty()) ImGui::TextWrapped("%s", snapshot.data.status.c_str());
    }
}

void live_history_plot(const SessionSnapshot& snapshot, const char* id, const char* unit, int channel,
                       const ImVec4& color) {
    if (snapshot.history.empty()) { ImGui::TextColored(muted, "No observed history yet."); return; }
    std::array<double, 360> time{}, values{};
    // Bound view work as well as the session's history storage. NaN leaves a gap for a missing sample.
    const size_t count = std::min(snapshot.history.size(), time.size());
    const size_t start = snapshot.history.size() - count;
    for (size_t i = 0; i < count; ++i) {
        const auto& sample = snapshot.history[start + i];
        const auto value = channel == 0 ? sample.core_mhz : channel == 1 ? sample.temp_c : sample.power_w;
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

void live_monitor(const SessionSnapshot& snapshot, bool verbose) {
    title("Monitor", "Observed telemetry; unavailable readings remain empty.");
    live_summary(snapshot);
    label("CORE CLOCK");
    live_history_plot(snapshot, "##live_clock_history", "MHz", 0, accent);
    label("TEMPERATURE");
    live_history_plot(snapshot, "##live_temp_history", "C", 1, teal);
    label("BOARD POWER");
    live_history_plot(snapshot, "##live_power_history", "W", 2, amber);
    if (ImGui::CollapsingHeader("Sensor readings", verbose ? ImGuiTreeNodeFlags_DefaultOpen : 0)) {
        const auto& t = snapshot.data.telemetry;
        measured_value(snapshot, "Memory clock (driver)", DC_T_MEM, t.mem, "MHz");
        measured_value(snapshot, "Core voltage", DC_T_VCORE_MV, t.vcore_mv, "mV");
        measured_value(snapshot, "Hotspot", DC_T_TEMP_HOTSPOT, t.temp_hotspot, "C");
        measured_value(snapshot, "GPU utilization", DC_T_UTIL_GPU, t.util_gpu, "%");
        measured_value(snapshot, "Configured board request", DC_T_PL_REQUESTED, t.pl_requested_mw / 1000.0, "W");
        measured_value(snapshot, "Reported power limit", DC_T_PL_NOW, t.pl_now_mw / 1000.0, "W");
        measured_value(snapshot, "P-state", DC_T_PSTATE, t.pstate, "");
        const bool fans_stale = snapshot.fan_readings_stale || snapshot.paused;
        for (uint32_t i = 0; i < std::min(t.fan_count, uint32_t{32}); ++i) {
            ImGui::TextColored(fans_stale ? amber : secondary, "Fan %u: duty %s | speed %s%s", i,
                number_text(t.fans[i].has_duty ? std::optional<double>(t.fans[i].duty) : std::nullopt, "%").c_str(),
                number_text(t.fans[i].has_rpm ? std::optional<double>(t.fans[i].rpm) : std::nullopt, "RPM").c_str(),
                fans_stale ? " (stale)" : "");
        }
    }
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

} // namespace

bool ViewState::has_draft() const {
    if (!preview) {
        for (const auto& draft : drafts) if (draft.dirty) return true;
        return false;
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
        return;
    }
    core_offset_mhz = memory_offset_mhz = 0.0f;
    power_limit_w = 310.0f;
    fan_percent = 30.0f;
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
    if (state.page == Page::curve) {
        state.page = Page::daily;
        state.curve_window_open = true;
        state.curve_window_focus_requested = true;
    }
    if (!state.preview) { sync_live_drafts(state); observe_curve(state); }
    unsigned risk_colors = 0;
    if (!state.preview && state.live) {
        const auto risk = session_risk(state.live->data);
        if (risk.score) {
            auto tint = [](RiskColor c) { return ImVec4(c.r / 255.0f, c.g / 255.0f, c.b / 255.0f, 1); };
            ImGui::PushStyleColor(ImGuiCol_WindowBg, tint(risk.tint.background));
            ImGui::PushStyleColor(ImGuiCol_ChildBg, tint(risk.tint.background));
            ImGui::PushStyleColor(ImGuiCol_Border, tint(risk.tint.border));
            ImGui::PushStyleColor(ImGuiCol_TitleBgActive, tint(risk.tint.border));
            risk_colors = 4;
        }
    }
    const bool compact = short_view();
    if (compact) {
        ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(u(8), u(4)));
        ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(u(8), u(3)));
        ImGui::PushStyleVar(ImGuiStyleVar_CellPadding, ImVec2(u(6), u(3)));
    }
    ImGui::SetNextWindowPos(ImVec2(0, 0));
    ImGui::SetNextWindowSize(ImGui::GetIO().DisplaySize);
    const auto flags = ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoResize
        | ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoSavedSettings
        | ImGuiWindowFlags_NoBringToFrontOnFocus | ImGuiWindowFlags_NoScrollbar;
    ImGui::Begin("Druta workspace", nullptr, flags);
    top_bar(state);
    if (!state.preview && state.live) {
        draw_mode_bar(*state.live, state.controls_unlocked, state.commands);
        ImGui::Separator();
    }
    const bool narrow = ImGui::GetContentRegionAvail().x < u(900);
    if (narrow) {
        navigation(state, true);
        ImGui::Separator();
    }
    const char* footer = state.preview
        ? (state.hotkey_available ? "Sample data only. Draft edits stay in this window."
            : "Sample data only. Restore shortcut unavailable; use the taskbar.")
        : (state.hotkey_available ? "Session-only tuning. Saved settings apply only when you load them."
            : "Session-only tuning. Restore shortcut unavailable; use the taskbar.");
    const float footer_height = ImGui::CalcTextSize(footer, nullptr, false,
        ImGui::GetContentRegionAvail().x).y + ImGui::GetStyle().ItemSpacing.y * 2 + u(3);
    const float log_height = !state.preview && state.live
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
    if (!state.preview) {
        if (!state.live) ImGui::TextUnformatted("Waiting for the first session snapshot...");
        else if (state.page == Page::daily) live_daily(state, verbose);
        else if (state.page == Page::advanced) live_advanced(state, verbose);
        else live_monitor(*state.live, verbose);
    } else if (state.page == Page::daily) daily(state, verbose);
    else if (state.page == Page::advanced) advanced(verbose);
    else monitor(verbose);
    ImGui::EndChild();
    if (!state.preview && state.live) live_operations(*state.live);
    ImGui::Separator();
    ImGui::PushTextWrapPos(0);
    ImGui::TextColored(state.hotkey_available ? muted : amber, "%s", footer);
    ImGui::PopTextWrapPos();
    ImGui::End();
    draw_curve_window(state);
    if (compact) ImGui::PopStyleVar(3);
    if (risk_colors) ImGui::PopStyleColor(static_cast<int>(risk_colors));
}

} // namespace druta::ui
