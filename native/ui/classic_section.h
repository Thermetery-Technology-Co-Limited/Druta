// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "classic_layout.h"
#include <algorithm>
#include <cmath>
#include <string>

namespace druta::ui {
// Released 1.9.1 ui_sections.section: each nesting level has its own header
// colours, a nested section (bar and body) is indented 18 px under its parent,
// and a spacer of 8 px (top level) or 2 px (deeper) follows the section. The
// level's colours stay pushed over the body, as Dear PyGui's bound header theme
// reaches plain collapsing headers inside it; nested sections push their own.
inline constexpr float classic_section_indent = 18;
inline constexpr float classic_section_note_wrap = 900;
inline constexpr unsigned char classic_section_colours[3][3][3] = {
    {{38, 66, 104}, {48, 82, 128}, {58, 98, 150}},
    {{46, 50, 58}, {58, 63, 73}, {68, 74, 86}},
    {{39, 42, 48}, {50, 54, 62}, {60, 65, 75}},
};

// Released 1.9.1 add_text: Dear PyGui draws each text item on a frame-height line with
// its text FramePadding.y down, so stacked items sit FrameHeight + ItemSpacing apart
// (26 px at 1x) where plain ImGui text gives 20; the lines inside one wrapped item keep
// the font's pitch. The text wraps at the position the caller pushed.
inline void classic_text(const char* text) { ImGui::AlignTextToFramePadding(); ImGui::TextUnformatted(text); }
inline void classic_text(const std::string& text) { classic_text(text.c_str()); }

class ClassicSection {
public:
    // `note` is the dim scope line Python draws at the top of an open body.
    ClassicSection(const char* label, int level, const char* note = nullptr, ImGuiTreeNodeFlags flags = 0)
        : level_(level), indent_(level ? classic_px(classic_section_indent) : 0) {
        if (indent_ > 0) ImGui::Indent(indent_);
        const auto& colours = classic_section_colours[std::clamp(level, 0, 2)];
        const ImGuiCol slots[3] = {ImGuiCol_Header, ImGuiCol_HeaderHovered, ImGuiCol_HeaderActive};
        for (int i = 0; i < 3; ++i)
            ImGui::PushStyleColor(slots[i], ImVec4(colours[i][0] / 255.0f, colours[i][1] / 255.0f, colours[i][2] / 255.0f, 1));
        open_ = ImGui::CollapsingHeader(label, flags);
        if (open_ && note) {
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(139.0f / 255, 144.0f / 255, 153.0f / 255, 1));
            ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + classic_px(classic_section_note_wrap));
            classic_text(note);
            ImGui::PopTextWrapPos();
            ImGui::PopStyleColor();
        }
    }
    ~ClassicSection() {
        ImGui::PopStyleColor(3);
        ImGui::Dummy(ImVec2(0, std::round(classic_px(level_ ? 2.0f : 8.0f))));
        if (indent_ > 0) ImGui::Unindent(indent_);
    }
    ClassicSection(const ClassicSection&) = delete;
    ClassicSection& operator=(const ClassicSection&) = delete;
    explicit operator bool() const { return open_; }
    bool open() const { return open_; }

private:
    int level_;
    float indent_;
    bool open_ = false;
};
}
