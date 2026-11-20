// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "imgui.h"
#include <algorithm>
#include <array>
#include <cmath>

namespace druta::ui {
// Released 1.9.1 (fa6ee83), druta.py KNOB_COLS / KNOB_COL_PAD.
// Both ordinary and private-control rows use this one geometry.
inline constexpr std::array<float, 6> classic_knob_widths{210, 200, 66, 92, 78, 74};
inline constexpr float classic_column_width = 754;
// Released 1.9.1 Druta.run: the viewport (the outer window) is
// s(max(1180, both knob columns + 36)) wide and s(1300) tall, the height
// clamped to min(s(1300), max(s(600), screen - s(70)), screen), at Dear
// PyGui's default position (100, 100). s() rounds size * scale as 1.9.1 does.
// The width, and the position where the screen is too small for it, are also
// kept on the screen.
struct ClassicWindowPlacement { int x = 100, y = 100, width = 0, height = 0; };
inline ClassicWindowPlacement classic_default_window_placement(float scale, int screen_width, int screen_height) {
    const auto s = [scale](float value) { return static_cast<int>(std::lround(value * scale)); };
    ClassicWindowPlacement out;
    out.width = s(std::max(1180.0f, 2 * classic_column_width + 36)); out.height = s(1300);
    if (screen_height > 0) {
        out.height = std::min({out.height, std::max(s(600), screen_height - s(70)), screen_height});
        out.y = std::min(out.y, screen_height - out.height);
    }
    if (screen_width > 0) {
        out.width = std::min(out.width, screen_width);
        out.x = std::min(out.x, screen_width - out.width);
    }
    return out;
}
// Released 1.9.1 build_shared_header panic_pnp_reset label.
inline constexpr const char* classic_panic_label = "Panic Button\n(PnP Reset, Deeper than Shift+Ctrl+B)";
// body, heading and mono rasterise through FreeType as 1.9.1 does; mono_plain
// is the same face through stb_truetype for the Focused views that share the
// mono helper. draw() sets `classic` for the frame.
struct ClassicFonts { ImFont* body = nullptr; ImFont* heading = nullptr; ImFont* mono = nullptr; ImFont* mono_plain = nullptr; bool classic = false; };
inline ClassicFonts classic_fonts;
inline float classic_px(float value) { return value * ImGui::GetFontSize() / 16.0f; }
// Released 1.9.1 druta.py ACCENT and DIM text colours.
inline const ImVec4 classic_accent_text{74.0f / 255, 163.0f / 255, 1, 1};
inline const ImVec4 classic_dim_text{139.0f / 255, 144.0f / 255, 153.0f / 255, 1};
inline void push_classic_mono() {
    ImGui::PushFont(classic_fonts.classic || !classic_fonts.mono_plain ? classic_fonts.mono : classic_fonts.mono_plain, 14.0f);
}
// Generic tuning requests are whole-number controls in the UI. Keep the
// backend/readback as doubles because several APIs report fractional values,
// but never expose that precision through a slider or editable request box.
// Controller-specific editors such as I2C retain their hardware-defined grid.
inline ImS64 whole_number_value(double value) {
    return std::isfinite(value) ? static_cast<ImS64>(std::llround(value)) : 0;
}
inline bool whole_number_slider(const char* label, double& value, double low, double high,
                                const char* format = " ", ImGuiSliderFlags flags = ImGuiSliderFlags_None) {
    if (!std::isfinite(low) || !std::isfinite(high)) return false;
    ImS64 minimum = static_cast<ImS64>(std::ceil(low));
    ImS64 maximum = static_cast<ImS64>(std::floor(high));
    if (minimum > maximum) return false;
    ImS64 integer = std::clamp(whole_number_value(value), minimum, maximum);
    if (!ImGui::SliderScalar(label, ImGuiDataType_S64, &integer, &minimum, &maximum, format, flags)) return false;
    value = static_cast<double>(integer);
    return true;
}
inline bool whole_number_input(const char* label, double& value, ImGuiInputTextFlags flags = ImGuiInputTextFlags_None) {
    ImS64 integer = whole_number_value(value);
    if (!ImGui::InputScalar(label, ImGuiDataType_S64, &integer, nullptr, nullptr, "%lld", flags)) return false;
    value = static_cast<double>(integer);
    return true;
}
// Python knob_cols tables take no width of their own: they fill what the
// enclosing cell or window offers and ImGui narrows the last fixed column to it.
inline bool begin_classic_knob_table(const char* id) {
    if (!ImGui::BeginTable(id, 6, ImGuiTableFlags_SizingFixedFit | ImGuiTableFlags_NoHostExtendX)) return false;
    const char* names[]{"Control", "Slider", "Request", "Readback", "Apply", "Extra"};
    for (unsigned i = 0; i < classic_knob_widths.size(); ++i)
        ImGui::TableSetupColumn(names[i], ImGuiTableColumnFlags_WidthFixed, classic_px(classic_knob_widths[i]));
    return true;
}
}
