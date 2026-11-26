// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "quick_profiles.h"
#include <windows.h>
#include <algorithm>
#include <filesystem>
#include <stdexcept>

namespace druta::ui {
namespace {
bool available(const ProfileLibraryEntry& entry) {
    return !entry.autosave && !entry.id.empty() && entry.error.empty() && entry.document && entry.description.valid;
}

// Druta can have one window per adapter. Serialize the read-modify-write so
// changing one slot cannot discard another window's completed assignment.
class AssignmentLock {
    HANDLE handle_;
public:
    explicit AssignmentLock(const std::wstring& path) {
        const auto parent = std::filesystem::path(path).parent_path();
        if (!parent.empty()) std::filesystem::create_directories(parent);
        handle_ = CreateFileW((path + L".lock").c_str(), GENERIC_WRITE | DELETE, 0, nullptr,
                              OPEN_ALWAYS, FILE_ATTRIBUTE_TEMPORARY | FILE_FLAG_DELETE_ON_CLOSE, nullptr);
        if (handle_ == INVALID_HANDLE_VALUE) {
            const auto error = GetLastError();
            if (error == ERROR_SHARING_VIOLATION)
                throw std::runtime_error("Another Druta window is updating quick profiles. Try again.");
            throw std::runtime_error("Cannot lock quick profile settings (Windows error " + std::to_string(error) + ")");
        }
    }
    ~AssignmentLock() { CloseHandle(handle_); }
    AssignmentLock(const AssignmentLock&) = delete;
    AssignmentLock& operator=(const AssignmentLock&) = delete;
};
QuickProfileSlots read_slots(const std::wstring& path) {
    QuickProfileSlots result;
    if (std::filesystem::exists(path)) {
        const auto settings = read_profile(path);
        if (settings.value("version", 0) != 1 || !settings.contains("slots") ||
            !settings["slots"].is_array() || settings["slots"].size() != result.ids.size())
            throw std::runtime_error("Unrecognized quick profile settings");
        for (std::size_t i = 0; i < result.ids.size(); ++i) {
            const auto& id = settings["slots"][i].get_ref<const std::string&>();
            const auto current = result.ids.begin() + i;
            if (!id.empty() && std::find(result.ids.begin(), current, id) != current)
                throw std::runtime_error("Duplicate quick profile assignment");
            *current = id;
        }
    }

    return result;
}

bool fill_empty_slots(QuickProfileSlots& result, const std::vector<ProfileLibraryEntry>& library) {
    // A missing portable profile still owns its button until the user replaces it.
    bool changed = false;
    auto next = result.ids.begin();
    for (const auto& entry : library) {
        next = std::find(next, result.ids.end(), std::string{});
        if (next == result.ids.end()) break;
        if (available(entry) && std::find(result.ids.begin(), result.ids.end(), entry.id) == result.ids.end()) {
            *next++ = entry.id;
            changed = true;
        }
    }
    return changed;
}
}

std::wstring quick_profile_settings_path() {
    const auto native = discover_profile_directories().front();
    return (std::filesystem::path(native.path).parent_path() / L"quick-profiles.json").wstring();
}

QuickProfileSlots load_quick_profiles(const std::wstring& path, const std::vector<ProfileLibraryEntry>& library) {
    auto result = read_slots(path);
    if (!fill_empty_slots(result, library)) return result;
    AssignmentLock lock(path);
    result = read_slots(path);
    if (fill_empty_slots(result, library))
        write_profile(path, {{"version", 1}, {"slots", result.ids}});
    return result;
}

QuickProfileSlots assign_quick_profile(const std::wstring& path, const std::vector<ProfileLibraryEntry>& library,
                                      std::size_t slot, const std::string& profile_id) {
    if (slot >= QuickProfileSlots{}.ids.size()) throw std::out_of_range("Invalid quick profile slot");
    if (std::none_of(library.begin(), library.end(), [&](const auto& entry) {
            return entry.id == profile_id && available(entry);
        }))
        throw std::runtime_error("Choose an available named profile");

    AssignmentLock lock(path);
    auto result = read_slots(path);
    fill_empty_slots(result, library);
    const auto existing = std::find(result.ids.begin(), result.ids.end(), profile_id);
    if (existing == result.ids.end()) result.ids[slot] = profile_id;
    else std::swap(result.ids[slot], *existing);
    write_profile(path, {{"version", 1}, {"slots", result.ids}});
    return result;
}
}
