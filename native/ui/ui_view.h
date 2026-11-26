// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "backend.h"
#include "curve_draft.h"
#include "advanced_view.h"
#include "timings_view.h"
#include "shunt_view.h"
#include "app_actions.h"
#include "scoped_profile_view.h"
#include "profile_catalog_view.h"
#include "quick_profiles.h"
#include "frequency_lock_view.h"
#include "power_integral_view.h"
#include "power_weights_view.h"
#include "pascal_clock_view.h"
#include "voltage_policy_view.h"
#include "voltage_tables_view.h"
#include "voltage_limits_view.h"
#include "policy_workspace_view.h"
#include "power_saving_view.h"
#include "clock_gating_view.h"
#include <cstdint>
#include <map>

namespace druta::ui {

enum class Page { daily, advanced, monitor, curve, timings, rails, i2c };
enum class Presentation { focused, classic };
inline constexpr Presentation default_presentation = Presentation::classic;
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
    Presentation presentation = default_presentation;
    bool always_refresh = false;
    bool pause_requested = false;
    bool hotkey_available = true;
    AppActionsState app_actions;
    ScopedProfileViewState scoped_profile_view;
    ProfileCatalogViewState profile_catalog_view;
    std::vector<ProfileCatalogIntent> profile_catalog_intents;
    struct ClassicProfileBar {
        struct Selection {
            std::uint64_t generation;
            std::string id, name;
            ProfileJson document;
            bool metadata_only;
        };
        enum class ApplyState { idle, running, applied, failed };
        std::optional<Selection> selected;
        ApplyState apply = ApplyState::idle;
        std::string status;
    } classic_profile_bar;
    using ToolbarSaveRequest = ToolbarProfileSaveRequest;
    struct ToolbarSaveDraft {
        std::uint64_t generation = 0;
        std::array<char, 128> name{};
        std::optional<std::size_t> slot;
        std::optional<std::string> expected_slot_id;
        ProfileScope scope = ProfileScope::daily;
        bool name_edited = false, scope_edited = false;
    } toolbar_save_draft;
    std::optional<ToolbarSaveRequest> toolbar_save_request;
    bool toolbar_save_pending = false;
    std::string toolbar_save_status;
    QuickProfileSlots quick_profiles;
    bool quick_library_requested = false;
    std::optional<std::pair<std::size_t, std::string>> quick_profile_assignment;
    std::string quick_profile_status;
    std::string startup_profile_id, startup_profile_uuid, startup_profile_slot;
    std::optional<bool> startup_profile_request;
    std::string startup_profile_status;
    FrequencyLockViewState frequency_view;
    PowerIntegralViewState power_integral_view;
    PowerWeightsViewState power_weights_view;
    PascalClockViewState pascal_clock_view;
    struct {
        uint64_t generation = 0;
        std::shared_ptr<const PascalClockTable> source;
        std::map<PascalClockDomain,int> changes;
        bool reviewing = false;
    } firmware_timings;
    bool open_pascal_clocks_requested = false;
    std::optional<uint64_t> automatic_clock_read;
    std::vector<PowerWeightsIntent> power_weights_intents;
    std::shared_ptr<const PowerWeightsData> power_weights_saved;
    VoltagePolicyViewState voltage_policy_view;
    VoltageTablesViewState voltage_tables_view;
    VoltageLimitsViewState voltage_limits_view;
    std::vector<VoltageTablesIntent> voltage_tables_intents;
    std::vector<VoltagePolicyIntent> voltage_policy_profile_intents;
    std::vector<PowerIntegralIntent> power_integral_profile_intents;
    std::vector<VoltageLimitsIntent> voltage_limits_intents;
    bool open_power_saving_requested=false,open_clock_gating_requested=false;
    bool open_voltage_policy_requested=false,open_power_integral_requested=false;
    bool open_voltage_tables_requested=false,open_voltage_limits_requested=false,open_power_weights_requested=false;
    PowerSavingViewState power_saving_view;ClockGatingViewState clock_gating_view;
    std::vector<PowerSavingIntent> power_saving_intents;std::vector<ClockGatingIntent> clock_gating_intents;
    PolicyWorkspaceViewState policy_workspace;
    PolicyAnnotations policy_annotations;
    bool save_policy_annotations=false,import_policy_annotations=false;
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
    bool capability_refresh_requested = false;
    bool reconnect_requested = false;
    bool save_profile_requested = false;
    bool load_profile_requested = false;
    bool save_curve_profile_requested = false, load_curve_profile_requested = false;
    std::string curve_profile_status;
    bool timing_helper_requested = false, timing_forget_requested = false, test_signing_requested = false;
    bool timing_save_profile_requested = false, timing_load_profile_requested = false;
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
    std::string curve_plan_note; // 1.9.1 _plan_note text for the Classic banner; dropped with the plan
    // Classic Clocks > "Limited de-flatten <= cap": draw() refreshes whether a
    // plan can be staged; the menu sets the request and draw() stages it locally.
    bool limited_deflatten_ready = false, limited_deflatten_requested = false;

    // Classic seeds the released 1.9.1 editor presets once (curve_draft.h), then
    // applies 1.9.1 vf_read's rules on every fresh read. Focused keeps the above.
    bool classic_curve_presets_applied = false;
    // Classic selects the middle point on a new adapter/session, as 1.9.1 does
    // when it has no selection yet; cleared once a curve has been observed.
    bool curve_selection_pending = true;
    // Set by Read curve and by a new adapter/session: the next fresh curve is a
    // 1.9.1 vf_read (logged, floors and hard target reseeded).
    bool curve_read_requested = false;
    // completed_reads when that read was asked for: a curve already stale that
    // fails again shows no edge, so two further reads answer the request.
    uint64_t curve_read_reads = 0;
    // The command results observe_curve has seen, the Undo label state before
    // the newest one, and whether the catalog profile applied last is a full
    // profile (1.9.1 refresh_full_profile re-reads the curve, scoped loads do not).
    uint64_t curve_read_actions = 0;
    bool classic_undo_metadata_only = false, classic_catalog_rereads = false;
    // 1.9.1 _discard_armed: Read curve over staged edits asks twice.
    bool classic_discard_armed = false;
    // 1.9.1 log_once("read", ...): the read error this failure episode logged.
    std::string classic_read_error_logged;
    // 1.9.1 _power_request_feedback: a board-power Apply / Stock waiting for its
    // result, and the warning its readback left on the Limits footer.
    struct ClassicPowerRequest { uint64_t generation = 0, completed_actions = 0, log_id = 0; double requested_w = 0; };
    std::optional<ClassicPowerRequest> classic_power_request;
    struct ClassicPowerFeedback { uint64_t generation = 0; bool mismatch = false; std::string text; };
    std::optional<ClassicPowerFeedback> classic_power_feedback;
    // Classic log lines the released frontend writes besides command results
    // (backend status, curve reads). after_log_id orders them among the
    // session log; ok empty prints untagged.
    struct ClassicLogLine {
        uint64_t after_log_id = 0;
        std::string text;
        std::optional<bool> ok;
        bool log_only = false;
    };
    std::vector<ClassicLogLine> classic_log_lines;
    std::map<uint32_t, std::string> classic_clock_patterns;
    uint64_t classic_backend_logged_generation = 0;
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
    // Recomputed each frame: Classic's Advanced page drew its Power policies section
    // open. The host passes it to Session::set_policy_readouts (read-only refresh).
    bool power_policies_open = false;
    bool curve_window_focus_requested = false;
    bool curve_fit_requested = true;
    std::optional<uint32_t> curve_drag_point;
    double curve_last_undo_time = -1;
    // Editable route/target hints only. No automatic controller probing or SET.
    int i2c_port = 1, i2c_address = 0x20;
    double i2c_target_mv = 1100;
    int i2c_scan_scope = 0;
    bool i2c_save_profile_requested = false, i2c_load_profile_requested = false;
    bool i2c_cancel_requested = false;
    bool timing_capture_cancel_requested = false;
    bool clock_checks_cancel_requested = false;
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
void mark_catalog_profile_apply(ViewState& state, const ProfileJson& document);
bool front_clock_capture_ready(const ViewState& state);
// Shared by the frequency-lock window and Classic's inline Clocks menu block.
FrequencyLockViewContext frequency_lock_context(const ViewState& state);
void queue_frequency_lock_intents(ViewState& state, const std::vector<FrequencyLockIntent>& intents);

} // namespace druta::ui
