// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "backend.h"
#include "curve_draft.h"
#include "advanced_view.h"
#include "timings_view.h"
#include "shunt_view.h"
#include <cstdint>

namespace druta::ui {

enum class Page { daily, advanced, monitor, curve, timings, rails, i2c };
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
    bool controls_unlocked = true;
    // Classic pages move between one docked body and same-window floating
    // windows. They retain this ViewState and never acquire another Session.
    std::array<bool, 4> classic_floating{};
    std::array<bool, 4> classic_float_focus{};
    int classic_tab_drag = -1;
    float classic_tab_top = 0, classic_tab_bottom = 0;
    double classic_reset_deadline = 0;
    uint64_t classic_reset_generation = 0;
    std::string classic_reset_slot;
    std::shared_ptr<const SessionSnapshot> live;
    bool refresh_requested = false;
    bool reconnect_requested = false;
    bool save_profile_requested = false;
    bool load_profile_requested = false;
    bool save_curve_profile_requested = false, load_curve_profile_requested = false;
    std::string curve_profile_status;
    bool timing_helper_requested = false;
    TimingsViewState timings_view;
    ShuntViewState shunt;
    bool hold_headroom = true;
    double headroom_margin_mv = 25;
    std::optional<std::string> select_slot;
    std::vector<Command> commands;
    std::array<ControlDraft, static_cast<size_t>(ControlId::count)> drafts{};
    uint64_t draft_generation = 0;
    bool generation_initialized = false;
    CurveDraft curve_draft;
    AdvancedViewState advanced_view;
    PlannerApi planner_api;
    double curve_cap_mv = 1100, curve_floor_mv = 1000;
    double hard_floor_mv = 0, hard_target_mhz = 0, hard_staged_floor_mv = 0;
    bool hard_mod_ack = false, hard_plan_staged = false;
    uint64_t hard_ack_generation = 0;
    std::string curve_plan_message;
    uint64_t observed_curve_revision = 0;
    uint64_t acknowledged_curve_action = 0;
    uint64_t observed_curve_generation = 0;
    bool curve_observation_initialized = false;
    bool observed_curve_stale = true;
    uint32_t selected_curve_point = 0;
    double curve_delta_mhz = 0;
    double curve_frequency_mhz = 0;
    bool curve_entry_dirty = false;
    bool curve_entry_editing = false;
    bool curve_window_open = false;
    bool curve_inline_active = false; // Recomputed each frame; only one editor is drawn.
    bool curve_window_focus_requested = false;
    bool curve_fit_requested = true;
    std::optional<uint32_t> curve_drag_point;
    double curve_last_undo_time = -1;
    // Editable route/target hints only. No automatic controller probing or SET.
    int i2c_port = 1, i2c_address = 0x20;
    double i2c_target_mv = 1100;
    int i2c_scan_scope = 0;
    bool i2c_save_profile_requested = false, i2c_load_profile_requested = false;
    std::vector<std::pair<std::string, ControlDraft>> i2c_field_drafts;
    std::vector<I2cField> i2c_field_cache;
    uint64_t i2c_draft_generation = 0, i2c_draft_selection_revision = 0;
    bool i2c_draft_initialized = false;
    int i2c_nct_mapping = 0; // Explicit session display choice; native drafts stay in nominal uA.
    double i2c_gate_flash_until = 0;

    bool has_draft() const;
    void revert_draft();
};

// Call after creating the ImGui and ImPlot contexts. Resets the base style,
// then scales it once; the host owns font loading and DPI changes.
void apply_theme(float scale);
void draw(ViewState& state);

} // namespace druta::ui
