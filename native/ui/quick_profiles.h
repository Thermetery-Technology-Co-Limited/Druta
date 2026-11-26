// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "profile_library.h"
#include <array>

namespace druta::ui {
struct QuickProfileSlots {
    std::array<std::string, 5> ids{};
    std::optional<ProfileScope> save_scope;
    std::optional<std::size_t> save_slot; // Library when empty.
};
struct ToolbarSavePreferenceChange {
    std::optional<ProfileScope> scope;
    bool destination_changed = false;
    std::optional<std::size_t> slot;
};

struct ToolbarProfileSaveRequest {
    uint64_t generation = 0;
    std::string name;
    std::optional<std::size_t> slot;
    std::string replace_id;
    ProfileScope scope = ProfileScope::daily;
    std::optional<ProfileJson> replace_document;
    std::optional<std::string> expected_slot_id;
};
struct ToolbarProfileSaveResult {
    ProfileLibraryEntry entry;
    std::optional<QuickProfileSlots> slots;
    std::string settings_error;
};
ToolbarProfileSaveResult save_toolbar_profile(const ProfileLibraryDirectory& native, const std::wstring& settings,
    const ToolbarProfileSaveRequest&, const ProfileJson& captured, const std::string& uuid, const std::string& pci_slot);
std::wstring quick_profile_settings_path();
QuickProfileSlots load_quick_profiles(const std::wstring& path, const std::vector<ProfileLibraryEntry>& library);
QuickProfileSlots remember_toolbar_save_preferences(const std::wstring& path, const ToolbarSavePreferenceChange& change);
QuickProfileSlots assign_quick_profile(const std::wstring& path, const std::vector<ProfileLibraryEntry>& library,
                                      std::size_t slot, const std::string& profile_id);
}
