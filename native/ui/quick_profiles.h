// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "profile_library.h"
#include <array>

namespace druta::ui {
struct QuickProfileSlots {
    std::array<std::string, 5> ids{};
};

std::wstring quick_profile_settings_path();
QuickProfileSlots load_quick_profiles(const std::wstring& path, const std::vector<ProfileLibraryEntry>& library);
QuickProfileSlots assign_quick_profile(const std::wstring& path, const std::vector<ProfileLibraryEntry>& library,
                                      std::size_t slot, const std::string& profile_id);
}
