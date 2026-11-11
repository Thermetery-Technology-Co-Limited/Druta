// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "ui_view.h"

#include "imgui.h"
#include "implot.h"

#include <array>
#include <cmath>

namespace druta::ui {
namespace {

const ImVec4 accent{0.38f, 0.71f, 1.0f, 1.0f};
const ImVec4 secondary{0.56f, 0.63f, 0.72f, 1.0f};
const ImVec4 muted{0.43f, 0.50f, 0.60f, 1.0f};
const ImVec4 teal{0.29f, 0.81f, 0.70f, 1.0f};
const ImVec4 amber{0.96f, 0.72f, 0.37f, 1.0f};

float u(float value) { return value * ImGui::GetFontSize() / 18.0f; }
bool short_view() { return ImGui::GetIO().DisplaySize.y < u(720); }

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
        {Page::daily, "Daily tuning"}, {Page::advanced, "Advanced"}, {Page::monitor, "Monitor"}
    };
    const float width = horizontal ? (ImGui::GetContentRegionAvail().x - u(16)) / 3.0f : 0.0f;
    for (int i = 0; i < 3; ++i) {
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
        ImGui::TextColored(muted, "Preview only");
        ImGui::PushStyleColor(ImGuiCol_Text, muted);
        ImGui::TextWrapped("GPU controls are not connected yet.");
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
    badge("PREVIEW / SAMPLE DATA", amber);
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
    ImGui::TextUnformatted("NVIDIA GeForce RTX 3070 Ti (sample)");
    if (ImGui::GetContentRegionAvail().x > u(860)) {
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
    if (!short_view()) ImGui::Dummy(ImVec2(0, u(14)));
    ImGui::SeparatorText("Voltage / frequency");
    label("Sample curve. Core-offset edits preview their shape here.");
    vf_plot(state, u(250));
    if (ImGui::CollapsingHeader("Curve details", verbose ? ImGuiTreeNodeFlags_DefaultOpen : 0)) {
        ImGui::TextWrapped("The curve, draft and operating point remain distinct. "
                           "This sample is for layout review; it is not a measurement of this GPU.");
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

} // namespace

bool ViewState::has_draft() const {
    return core_offset_mhz != 0.0f || memory_offset_mhz != 0.0f
        || power_limit_w != 310.0f || fan_percent != 30.0f;
}

void ViewState::revert_draft() {
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
    const bool narrow = ImGui::GetContentRegionAvail().x < u(900);
    if (narrow) {
        navigation(state, true);
        ImGui::Separator();
    }
    const char* footer = state.hotkey_available
        ? "Sample data only. Draft edits stay in this window."
        : "Sample data only. Restore shortcut unavailable; use the taskbar.";
    const float footer_height = ImGui::CalcTextSize(footer, nullptr, false,
        ImGui::GetContentRegionAvail().x).y + ImGui::GetStyle().ItemSpacing.y * 2 + u(3);
    const float body_height = ImGui::GetContentRegionAvail().y - footer_height;
    if (!narrow) {
        ImGui::BeginChild("navigation", ImVec2(u(162), body_height), false);
        navigation(state, false);
        ImGui::EndChild();
        ImGui::SameLine(0, u(18));
    }
    ImGui::BeginChild("workspace", ImVec2(0, body_height), false);
    const bool verbose = state.presentation == Presentation::classic;
    if (state.page == Page::daily) daily(state, verbose);
    else if (state.page == Page::advanced) advanced(verbose);
    else monitor(verbose);
    ImGui::EndChild();
    ImGui::Separator();
    ImGui::PushTextWrapPos(0);
    ImGui::TextColored(state.hotkey_available ? muted : amber, "%s", footer);
    ImGui::PopTextWrapPos();
    ImGui::End();
    if (compact) ImGui::PopStyleVar(3);
}

} // namespace druta::ui
