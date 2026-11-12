// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "imgui.h"
#include <array>

namespace druta::ui {
// Released 1.9.1 (fa6ee83), druta.py KNOB_COLS / KNOB_COL_PAD.
// Both ordinary and private-control rows use this one geometry.
inline constexpr std::array<float, 6> classic_knob_widths{210, 200, 66, 92, 78, 74};
inline constexpr float classic_column_width = 754;
struct ClassicFonts { ImFont* body = nullptr; ImFont* heading = nullptr; ImFont* mono = nullptr; };
inline ClassicFonts classic_fonts;
inline float classic_px(float value) { return value * ImGui::GetFontSize() / 16.0f; }
inline void push_classic_mono() { ImGui::PushFont(classic_fonts.mono, 14.0f); }
inline bool begin_classic_knob_table(const char* id) {
    if (!ImGui::BeginTable(id, 6, ImGuiTableFlags_SizingFixedFit | ImGuiTableFlags_NoHostExtendX,
        ImVec2(classic_px(classic_column_width), 0))) return false;
    const char* names[]{"Control", "Slider", "Request", "Readback", "Apply", "Extra"};
    for (unsigned i = 0; i < classic_knob_widths.size(); ++i)
        ImGui::TableSetupColumn(names[i], ImGuiTableColumnFlags_WidthFixed, classic_px(classic_knob_widths[i]));
    return true;
}
}
