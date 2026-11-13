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
};
// Shutdown never launches another process or opens a pending report dialog.
void cancel_pending_app_actions(AppActionsState& state);
AppTarget app_target(const SessionSnapshot& snapshot);
bool app_target_matches(const AppTarget& target, const SessionSnapshot& snapshot);
std::optional<AppTarget> restart_device_target(const SessionSnapshot&,std::string& why,bool administrator=false);
void draw_device_restart_button(ViewState&);
std::optional<AppTarget> second_window_target(const SessionSnapshot& snapshot, std::string& why);
DeviceReport selected_device_report(const SessionSnapshot& snapshot);
std::wstring quote_windows_argument(const std::wstring& argument);
std::wstring second_window_command(const std::wstring& executable, const std::wstring& core,
    const AppTarget& target, bool classic);
std::vector<BundledNotice> load_bundled_notices(const std::wstring& directory);
void draw_app_actions_menu(ViewState& state);
void draw_app_actions_windows(ViewState& state);
}
