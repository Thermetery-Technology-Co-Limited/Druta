// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "backend.h"
#include "curve_draft.h"
#include <cstdint>

namespace druta::ui {

enum class Page { daily, advanced, monitor, curve };
enum class Presentation { focused, classic };
struct SessionSnapshot;

struct ControlDraft {
    double value = 0;
    std::optional<double> baseline;
    bool initialized = false;
    bool dirty = false;
    bool editing = false;
};

// Presentation state and explicit command intents; drawing never calls a GPU.
// Fixture fields remain for preview/smoke compatibility. Both live layouts
// share drafts, so presentation changes cannot lose edits or change ownership.
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

    bool preview = true;
    bool controls_unlocked = false;
    std::shared_ptr<const SessionSnapshot> live;
    bool refresh_requested = false;
    bool reconnect_requested = false;
    bool save_profile_requested = false;
    bool load_profile_requested = false;
    std::optional<std::string> select_slot;
    std::vector<Command> commands;
    std::array<ControlDraft, static_cast<size_t>(ControlId::count)> drafts{};
    uint64_t draft_generation = 0;
    bool generation_initialized = false;
    CurveDraft curve_draft;
    uint64_t observed_curve_revision = 0;
    uint64_t observed_curve_generation = 0;
    bool curve_observation_initialized = false;
    bool observed_curve_stale = true;
    uint32_t selected_curve_point = 0;
    double curve_delta_mhz = 0;
    bool curve_entry_dirty = false;
    bool curve_entry_editing = false;

    bool has_draft() const;
    void revert_draft();
};

// Call after creating the ImGui and ImPlot contexts. Resets the base style,
// then scales it once; the host owns font loading and DPI changes.
void apply_theme(float scale);
void draw(ViewState& state);

} // namespace druta::ui
