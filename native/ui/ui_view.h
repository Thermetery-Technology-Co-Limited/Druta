// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

namespace druta::ui {

enum class Page { daily, advanced, monitor };
enum class Presentation { focused, classic };

// Presentation state only. These sample drafts are never sent to a GPU.
// Both layouts share this state so a presentation change cannot lose edits.
struct ViewState {
    Page page = Page::daily;
    Presentation presentation = Presentation::focused;
    bool always_refresh = false;
    bool pause_requested = false;
    bool hotkey_available = true;
    float core_offset_mhz = 0.0f;
    float memory_offset_mhz = 0.0f;
    float power_limit_w = 310.0f;
    float fan_percent = 30.0f;

    bool has_draft() const;
    void revert_draft();
};

// Call after creating the ImGui and ImPlot contexts. Resets the base style,
// then scales it once; the host owns font loading and DPI changes.
void apply_theme(float scale);
void draw(ViewState& state);

} // namespace druta::ui
