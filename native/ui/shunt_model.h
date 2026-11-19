// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "profile_io.h"
#include <optional>
#include <string>
#include <vector>

namespace druta::ui {
// A template, not detected board topology. Values are in milliohms.
struct ShuntRail {
    std::string kind;
    double orig = 5.0;
    double mod = 5.0;
    ProfileJson extra = ProfileJson::object();
};
struct ShuntCorrection {
    bool valid = false;
    bool exact = false;
    bool active = false;
    double factor = 1.0;
    std::string why;
    std::optional<double> apply(std::optional<double> watts) const;
};
std::vector<ShuntRail> shunt_default_rails();
ShuntCorrection shunt_correction(const std::vector<ShuntRail>& rails);
std::optional<double> shunt_parallel_resistance(const std::vector<double>& resistors);
std::wstring shunt_config_path();

// Load once. Saving is explicit and never applies hardware settings. A failed
// read blocks saving so an unreadable document is not silently overwritten.
struct ShuntConfig {
    std::vector<ShuntRail> rails;
    ProfileJson document = ProfileJson::object();
    std::wstring path;
    std::string load_error;
    static ShuntConfig load(const std::wstring& path_override = {});
    void save() const; // throws on failure
};
}
