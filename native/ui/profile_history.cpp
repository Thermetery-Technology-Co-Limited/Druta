// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "profile_history.h"
#include "profile_library.h"
#include <algorithm>
#include <filesystem>
#include <stdexcept>
namespace druta::ui {
std::shared_ptr<ProfileHistorySink> make_profile_history_sink() {
    auto sink = std::make_shared<ProfileHistorySink>();
    sink->save = [](const std::string& action, const ProfileJson& document) {
        const auto directories = discover_profile_directories();
        const auto destination = std::find_if(directories.begin(), directories.end(), [](const auto& d) { return d.writable; });
        if (destination == directories.end()) throw std::runtime_error("Native history storage unavailable");
        (void)save_library_autosave(*destination, action, document);
    };
    sink->backup = [](const ProfileJson& document) {
        const auto directories = discover_profile_directories();
        const auto destination = std::find_if(directories.begin(), directories.end(), [](const auto& d) { return d.writable; });
        if (destination == directories.end()) throw std::runtime_error("Native timing backup storage unavailable");
        return std::filesystem::path(save_library_timing_backup(*destination, document)).u8string();
    };
    return sink;
}
}
