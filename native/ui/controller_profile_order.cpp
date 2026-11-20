// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "controller_profile_order.h"
#include <stdexcept>
#include <utility>
namespace druta::ui {
std::vector<ProfileComponent> split_controller_dependencies(const std::vector<ProfileComponent>& components) {
    // Validate the complete originals, including fields outside either split.
    // In particular, a valid voltage subset cannot conceal malformed clocks.
    for (const auto& component : components) {
        const auto description = inspect_profile(component.document);
        if (!description.valid)
            throw std::runtime_error(component.name + ": " + description.error);
    }

    std::vector<ProfileComponent> result;
    for (const auto& component : components) {
        const auto& document = component.document;
        const auto format = document.value("format", ProfileJson());
        if (format == "druta-native-daily" && document.at("version") == 2) {
            auto voltage_entries = ProfileJson::array();
            auto remaining_entries = ProfileJson::array();
            for (const auto& entry : document.at("advanced")) {
                const auto& kind = entry.at("kind");
                if (kind == "nvvdd" || kind == "msvdd") voltage_entries.push_back(entry);
                else remaining_entries.push_back(entry);
            }
            if (!voltage_entries.empty() && (!document.at("controls").empty() || !remaining_entries.empty())) {
                auto voltage = document;
                voltage["controls"] = ProfileJson::object();
                voltage["advanced"] = std::move(voltage_entries);
                auto remaining = document;
                remaining["advanced"] = std::move(remaining_entries);
                result.push_back({component.name + " / voltage requests", std::move(voltage)});
                result.push_back({component.name + " / other requests", std::move(remaining)});
                continue;
            }
        } else if (format == "druta-native-scoped") {
            auto rail_entries = ProfileJson::array();
            auto power_entries = ProfileJson::array();
            for (const auto& entry : document.at("entries")) {
                if (entry.at("kind") == "rail") rail_entries.push_back(entry);
                else power_entries.push_back(entry); // Validated board or policy.
            }
            if (!rail_entries.empty() && !power_entries.empty()) {
                auto rails = document;
                rails["entries"] = std::move(rail_entries);
                auto power = document;
                power["entries"] = std::move(power_entries);
                result.push_back({component.name + " / rail requests", std::move(rails)});
                result.push_back({component.name + " / power requests", std::move(power)});
                continue;
            }
        }
        result.push_back(component);
    }
    return result;
}
}
