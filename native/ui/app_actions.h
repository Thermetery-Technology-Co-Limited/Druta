// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <cstdint>
#include <optional>
#include <string>
#include <vector>
namespace druta::ui {
struct SessionSnapshot;
struct ViewState;
struct AppTarget {
    std::uint64_t generation = 0;
    std::string slot, uuid;
};
struct DeviceReport { AppTarget target; std::string text; };
struct BundledNotice { std::string title, source, text; bool available = false; };
struct AppActionsState {
    bool administrator=false,recovery_preparing=false;
    bool report_open = false, about_open = false, licences_open = false, shortcuts_open = false;
    bool save_report_requested = false, exit_requested = false;
    std::optional<AppTarget> second_window,restart_device;
    std::optional<DeviceReport> report;
    std::vector<BundledNotice> notices;
    std::string status;
    // Released 1.9.1 headroom_changed / restore_raised_limits_now (Classic).
    // The headroom setting as last seen; the session generation whose raise an
    // untick still has to restore (held while another command runs); the
    // button's restore awaiting its result, and whether a raise was in force.
    std::optional<bool> headroom_setting_seen;
    std::optional<std::uint64_t> headroom_restore_owed;
    struct HeadroomButtonRestore { std::uint64_t generation = 0, completed_actions = 0; bool raised = false; };
    std::optional<HeadroomButtonRestore> headroom_button_restore;
};
// Shutdown never launches another process or opens a pending report dialog.
void cancel_pending_app_actions(AppActionsState& state);
AppTarget app_target(const SessionSnapshot& snapshot);
bool app_target_matches(const AppTarget& target, const SessionSnapshot& snapshot);
std::optional<AppTarget> restart_device_target(const SessionSnapshot&,std::string& why,bool administrator=false);
void draw_device_restart_button(ViewState&);
std::optional<AppTarget> second_window_target(const SessionSnapshot& snapshot, std::string& why);
// Released 1.9.1 open_gpu_window: a second window on any enumerated card. The
// selected card needs its verified session identity (second_window_target);
// another card goes with its enumerated slot and UUID, which the child checks
// against the adapter it opens (--expect-uuid) and refuses on a mismatch.
std::optional<AppTarget> second_window_card_target(const SessionSnapshot& snapshot, const std::string& slot, std::string& why);
// At launch: the target is still the selected verified card, or still one of
// the enumerated cards with the same slot and UUID in the same session.
bool second_window_launchable(const AppTarget& target, const SessionSnapshot& snapshot, std::string& why);
DeviceReport selected_device_report(const SessionSnapshot& snapshot);
std::wstring quote_windows_argument(const std::wstring& argument);
std::wstring second_window_command(const std::wstring& executable, const std::wstring& core,
    const AppTarget& target, bool classic);
std::vector<BundledNotice> load_bundled_notices(const std::wstring& directory);
void draw_app_actions_menu(ViewState& state);
void draw_classic_profile_bar(ViewState& state);
void draw_app_actions_windows(ViewState& state);
// Released 1.9.1 vfheadroom.clamp_margin: DEFAULT_MARGIN_MV, MIN_MARGIN_MV and
// MAX_MARGIN_MV (= drutacore's HOLD_HEADROOM_MAX_MARGIN_MV). A non-finite entry
// falls back to the default.
inline constexpr double classic_headroom_default_margin_mv = 25.0;
inline constexpr double classic_headroom_min_margin_mv = 6.25;
inline constexpr double classic_headroom_max_margin_mv = 100.0;
double clamp_headroom_margin_mv(double value);
// Released 1.9.1 restore_raised_limits_now: queues the explicit restore of the
// voltage limits this session's hold raised (once per frame's command list).
void queue_raised_limits_restore(ViewState& state);
// The note 1.9.1 logs, untagged, after the button restored a raise while the
// hold stays in force. 1.9.1 ends it "Anything that plans the headroom again
// (a new hold, a boost, margin or limit change) raises them again"; this build
// plans the raise only when a hold is issued, so the note names that.
inline constexpr const char* classic_raised_restored_under_hold_note =
    "headroom: raised limits restored with the hold still in force; Release (Ctrl+H) now goes through. "
    "A new hold (Ctrl+H or Max it) raises them again";
// Released 1.9.1 headroom_changed. Runs every frame from draw_app_actions_menu,
// and at once when the Clocks menu's setting changes. In Classic, turning the
// headroom setting off (the Clocks menu or the editor's Hold headroom) queues
// the restore of a raise in force; nothing is queued without one. While another
// command runs, the restore waits for it to end, as the session would refuse
// it; ticking the setting again or a new session drops it.
void hold_headroom_changed(ViewState& state);
}
