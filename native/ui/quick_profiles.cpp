// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "quick_profiles.h"
#include "python_full_profile.h"
#include <set>
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
        const auto filename = path + L".lock";
        for (unsigned attempt = 0; ; ++attempt) {
            handle_ = CreateFileW(filename.c_str(), GENERIC_WRITE | DELETE, 0, nullptr,
                                  OPEN_ALWAYS, FILE_ATTRIBUTE_TEMPORARY | FILE_FLAG_DELETE_ON_CLOSE, nullptr);
            if (handle_ != INVALID_HANDLE_VALUE) break;
            const auto error = GetLastError();
            if (error != ERROR_SHARING_VIOLATION)
                throw std::runtime_error("Cannot lock quick profile settings (Windows error " + std::to_string(error) + ")");
            if (attempt == 8) throw std::runtime_error("Another Druta window is updating quick profiles. Try again.");
            Sleep(10);
        }
    }
    ~AssignmentLock() { CloseHandle(handle_); }
    AssignmentLock(const AssignmentLock&) = delete;
    AssignmentLock& operator=(const AssignmentLock&) = delete;
};
struct QuickSettings {
    QuickProfileSlots slots;
    ProfileJson document = {{"version", 1}};
};

QuickSettings read_settings(const std::wstring& path) {
    QuickSettings settings;
    if (!std::filesystem::exists(path)) return settings;
    settings.document = read_profile(path);
    const auto& doc = settings.document;
    auto& slots = settings.slots;
    if (doc.value("version", 0) != 1 || !doc.contains("slots") ||
        !doc["slots"].is_array() || doc["slots"].size() != slots.ids.size())
        throw std::runtime_error("Unrecognized quick profile settings");
    for (std::size_t i = 0; i < slots.ids.size(); ++i) {
        const auto& id = doc["slots"][i].get_ref<const std::string&>();
        const auto current = slots.ids.begin() + i;
        if (!id.empty() && std::find(slots.ids.begin(), current, id) != current)
            throw std::runtime_error("Duplicate quick profile assignment");
        *current = id;
    }
    const auto scope = doc.find("save_scope");
    if (scope != doc.end()) {
        if (*scope == "daily") slots.save_scope = ProfileScope::daily;
        else if (*scope == "curve") slots.save_scope = ProfileScope::curve;
        else if (*scope == "full_tune") slots.save_scope = ProfileScope::full_tune;
    }
    const auto slot = doc.find("save_slot");
    if (slot != doc.end() && slot->is_number_integer() && *slot >= 1 && *slot <= slots.ids.size())
        slots.save_slot = slot->get<std::size_t>() - 1;
    return settings;
}

void change_save_preferences(QuickSettings& settings, const ToolbarSavePreferenceChange& change) {
    if (change.scope) {
        switch (*change.scope) {
        case ProfileScope::daily: settings.document["save_scope"] = "daily"; break;
        case ProfileScope::curve: settings.document["save_scope"] = "curve"; break;
        case ProfileScope::full_tune: settings.document["save_scope"] = "full_tune"; break;
        default: throw std::invalid_argument("Unknown profile capture scope");
        }
        settings.slots.save_scope = change.scope;
    }
    if (change.destination_changed) {
        if (change.slot && *change.slot >= settings.slots.ids.size()) throw std::out_of_range("Invalid quick profile slot");
        settings.slots.save_slot = change.slot;
        settings.document["save_slot"] = change.slot ? ProfileJson(*change.slot + 1) : ProfileJson(nullptr);
    }
}

void write_settings(const std::wstring& path, QuickSettings& settings) {
    settings.document["slots"] = settings.slots.ids;
    write_profile(path, settings.document);
}

// The catalog intentionally groups jointly applied fields (power, fan, V/F) under
// one target. An update must also retain the individually captured rows in those groups.
std::set<std::string> saved_controls(const ProfileJson& document, const ProfileDescription& description) {
    std::set<std::string> controls(description.targets.begin(), description.targets.end());
    if (description.scope == ProfileScope::curve) {
        for (const auto& point : document.at("points"))
            controls.insert("V/F point " + point.at("idx").dump() + " at " + ProfileJson(point.at("volt_mv").get<double>()).dump());
    }
    if (description.scope != ProfileScope::python_full_tune) return controls;

    const auto& source = python_full_source(document);
    const auto add_rows = [&](const char* group, const char* field) {
        if (!controls.count(group) || !source.contains(field) || source[field].is_null()) return;
        for (auto row = source[field].begin(); row != source[field].end(); ++row) {
            std::string key = std::string(field) + "[" + row.key() + "]";
            controls.insert(key);
            if (row->is_object() && row->contains("channel") && row->contains("type"))
                controls.insert(key + "/" + row->at("channel").dump() + "/" + row->at("type").dump());
        }
    };
    add_rows("clock:core", "vf_deltas");
    for (const auto field : {"current_limits_ma", "power_policies", "power_policy_pins"})
        add_rows("power:*", field);
    add_rows("annotation:power", "power_policy_names");
    if (controls.count("power:*") && source.contains("power_limit_mw") && !source["power_limit_mw"].is_null())
        controls.insert("power_limit_mw");
    if (controls.count("fan:policy") && source.contains("fan_control_state") && source["fan_control_state"].is_object()) {
        const auto& state = source["fan_control_state"];
        for (const auto& fan : state.at("fans"))
            controls.insert("fan " + state.at("source").dump() + "/" + fan.at("id").dump());
    }
    if (controls.count("i2c:controller")) {
        const auto& controller = source.at("i2c");
        for (const auto field : {"sha256", "port", "addr7", "rail", "format"})
            if (controller.contains(field) && !controller[field].is_null())
                controls.insert(std::string("i2c ") + field + "/" + controller[field].dump());
    }
    return controls;
}

void set_slot(QuickProfileSlots& slots, std::size_t slot, const std::string& id) {
    const auto existing = std::find(slots.ids.begin(), slots.ids.end(), id);
    if (existing == slots.ids.end()) slots.ids[slot] = id;
    else std::swap(slots.ids[slot], *existing);
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

ToolbarProfileSaveResult save_toolbar_profile(const ProfileLibraryDirectory& native, const std::wstring& settings,
    const ToolbarProfileSaveRequest& request, const ProfileJson& captured, const std::string& uuid,
    const std::string& pci_slot) {
    if (request.scope != ProfileScope::daily && request.scope != ProfileScope::curve && request.scope != ProfileScope::full_tune)
        throw std::runtime_error("Choose Daily / Advanced, V/F curve, or Full tune");
    if (request.slot && (*request.slot >= QuickProfileSlots{}.ids.size() || !request.expected_slot_id))
        throw std::runtime_error("Choose the quick profile slot again");
    const bool update = !request.replace_id.empty();
    if (update != request.replace_document.has_value())
        throw std::runtime_error("Review the existing named profile before updating it");

    const auto inspect = [&](const ProfileJson& document) {
        auto description = inspect_profile(document);
        const auto scope = description.scope == ProfileScope::python_full_tune ? ProfileScope::full_tune : description.scope;
        if (!description.valid) throw std::runtime_error("Invalid captured profile: " + description.error);
        if (scope != request.scope || uuid.empty() || pci_slot.empty() || description.uuid != uuid || description.slot != pci_slot)
            throw std::runtime_error("Profile capture scope or target GPU differs from the requested profile");
        return description;
    };
    ToolbarProfileSaveResult result;
    auto& entry = result.entry;
    entry.description = inspect(captured);
    if (request.replace_document) {
        const auto old = saved_controls(*request.replace_document, inspect(*request.replace_document));
        const auto current = saved_controls(captured, entry.description);
        for (const auto& control : old) {
            if (!current.count(control))
                throw std::runtime_error("The new capture omitted " + control + ". Retry or save it under a new name.");
        }
    }
    entry.name = profile_library_named_stem(request.name);
    entry.path = profile_library_named_path(native, request.name);
    entry.id = profile_library_id(entry.path);
    entry.source = native.source;
    entry.document = captured;
    if (request.replace_document && request.replace_id != entry.id)
        throw std::runtime_error("The update must use the reviewed file in the native named library");

    // Use the same lock as quick-slot assignment, from the last slot check through
    // the file commit and pin. A failed capture or stale request cannot move a slot.
    std::optional<AssignmentLock> slot_lock;
    QuickSettings quick;
    if (request.slot) {
        slot_lock.emplace(settings);
        quick = read_settings(settings);
        if (quick.slots.ids[*request.slot] != *request.expected_slot_id)
            throw std::runtime_error("The quick profile slot changed. Choose the destination again.");
    }
    if (request.replace_document)
        write_profile(entry.path, captured, ProfileWriteMode::update_only, &*request.replace_document);
    else
        write_profile(entry.path, captured, ProfileWriteMode::create_only);
    const ToolbarSavePreferenceChange used{request.scope, true, request.slot};
    try {
        if (request.slot) {
            set_slot(quick.slots, *request.slot, entry.id);
            change_save_preferences(quick, used);
            write_settings(settings, quick);
            result.slots = std::move(quick.slots);
        } else {
            result.slots = remember_toolbar_save_preferences(settings, used);
        }
    } catch (const std::exception& error) {
        result.settings_error = error.what();  // The named profile is already durable and remains selectable.
    }
    return result;
}

std::wstring quick_profile_settings_path() {
    const auto native = discover_profile_directories().front();
    return (std::filesystem::path(native.path).parent_path() / L"quick-profiles.json").wstring();
}

QuickProfileSlots load_quick_profiles(const std::wstring& path, const std::vector<ProfileLibraryEntry>& library) {
    auto settings = read_settings(path);
    if (!fill_empty_slots(settings.slots, library)) return settings.slots;
    AssignmentLock lock(path);
    settings = read_settings(path);
    if (fill_empty_slots(settings.slots, library)) write_settings(path, settings);
    return settings.slots;
}

QuickProfileSlots remember_toolbar_save_preferences(const std::wstring& path, const ToolbarSavePreferenceChange& change) {
    AssignmentLock lock(path);
    auto settings = read_settings(path);
    if ((change.scope && change.scope != settings.slots.save_scope) ||
        (change.destination_changed && change.slot != settings.slots.save_slot)) {
        change_save_preferences(settings, change);
        write_settings(path, settings);
    }
    return settings.slots;
}

QuickProfileSlots assign_quick_profile(const std::wstring& path, const std::vector<ProfileLibraryEntry>& library,
                                      std::size_t slot, const std::string& profile_id) {
    if (slot >= QuickProfileSlots{}.ids.size()) throw std::out_of_range("Invalid quick profile slot");
    if (std::none_of(library.begin(), library.end(), [&](const auto& entry) {
            return entry.id == profile_id && available(entry);
        }))
        throw std::runtime_error("Choose an available named profile");

    AssignmentLock lock(path);
    auto settings = read_settings(path);
    fill_empty_slots(settings.slots, library);
    set_slot(settings.slots, slot, profile_id);
    write_settings(path, settings);
    return settings.slots;
}
}
