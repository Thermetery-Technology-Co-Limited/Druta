// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "shunt_model.h"
#include <windows.h>
#include <cmath>
#include <limits>
#include <stdexcept>

namespace druta::ui {
namespace {
double capacity(const std::string& kind) {
    if (kind == "slot") return 66.0;
    if (kind == "pin6") return 75.0;
    if (kind == "pin8") return 150.0;
    return 0.0;
}
double number(const ProfileJson& object, const char* key) {
    // Python accepts numeric strings; retain malformed values as invalid.
    try {
        const auto& v = object.at(key);
        if (v.is_number()) return v.get<double>();
        if (v.is_string()) {
            auto s = v.get<std::string>();
            size_t end = 0;
            const double result = std::stod(s, &end);
            if (s.find_first_not_of(" \t\r\n", end) == std::string::npos) return result;
        }
    } catch (const std::exception&) {}
    return std::numeric_limits<double>::quiet_NaN();
}
void create_parent_directories(const std::wstring& path) {
    if (path.empty() || path.find(L'\0') != std::wstring::npos)
        throw std::runtime_error("Invalid shunt config path");
    const DWORD needed = GetFullPathNameW(path.c_str(), 0, nullptr, nullptr);
    if (!needed) throw std::runtime_error("Cannot resolve shunt config path");
    std::wstring full(needed, L'\0');
    const DWORD length = GetFullPathNameW(path.c_str(), needed, full.data(), nullptr);
    if (!length || length >= needed) throw std::runtime_error("Cannot resolve shunt config path");
    full.resize(length);
    const auto parent_end = full.find_last_of(L"\\/");
    if (parent_end == std::wstring::npos) return;
    // Skip the drive root or UNC server/share; create only descendants.
    size_t start = 3;
    if (full.size() >= 2 && full[0] == L'\\' && full[1] == L'\\') {
        const auto server_end = full.find(L'\\', 2);
        if (server_end == std::wstring::npos) throw std::runtime_error("Invalid UNC shunt config path");
        const auto share_end = full.find(L'\\', server_end + 1);
        if (share_end == std::wstring::npos || share_end >= parent_end) return;
        start = share_end + 1;
    }
    for (size_t end = start; end <= parent_end; ++end) {
        if (end != parent_end && full[end] != L'\\' && full[end] != L'/') continue;
        const auto directory = full.substr(0, end);
        if (directory.empty()) continue;
        if (!CreateDirectoryW(directory.c_str(), nullptr)) {
            const DWORD error = GetLastError();
            const DWORD attrs = GetFileAttributesW(directory.c_str());
            if (error != ERROR_ALREADY_EXISTS || attrs == INVALID_FILE_ATTRIBUTES || !(attrs & FILE_ATTRIBUTE_DIRECTORY))
                throw std::runtime_error("Cannot create shunt config directory (Windows error " + std::to_string(error) + ")");
        }
    }
}
std::wstring environment(const wchar_t* name) {
    const DWORD n = GetEnvironmentVariableW(name, nullptr, 0);
    if (!n) return {};
    std::wstring result(n, L'\0');
    const DWORD got = GetEnvironmentVariableW(name, result.data(), n);
    if (!got || got >= n) return {};
    result.resize(got);
    return result;
}
}
std::vector<ShuntRail> shunt_default_rails() {
    return {{"slot", 5, 5}, {"pin8", 5, 5}, {"pin8", 5, 5}};
}
std::optional<double> ShuntCorrection::apply(std::optional<double> watts) const {
    if (!valid || !watts || !std::isfinite(*watts) || *watts < 0) return std::nullopt;
    const double result = *watts * factor;
    return std::isfinite(result) ? std::optional<double>(result) : std::nullopt;
}
ShuntCorrection shunt_correction(const std::vector<ShuntRail>& rails) {
    ShuntCorrection result;
    if (rails.empty()) { result.why = "No rails configured"; return result; }
    double weighted = 0, total = 0, first = 0;
    bool uniform = true;
    for (size_t i = 0; i < rails.size(); ++i) {
        const auto& rail = rails[i];
        const double cap = capacity(rail.kind);
        if (!cap || !std::isfinite(rail.orig) || !std::isfinite(rail.mod) || rail.orig <= 0 || rail.mod <= 0) {
            result.why = "Unknown rail or invalid resistance";
            return result;
        }
        const double m = rail.orig / rail.mod;
        if (!std::isfinite(m) || m <= 0 || !std::isfinite(cap * m)) {
            result.why = "Resistance ratio is out of range";
            return result;
        }
        if (!i) first = m;
        else if (std::abs(m - first) > 1e-9) uniform = false;
        result.active |= std::abs(m - 1.0) > 1e-9;
        weighted += cap * m;
        total += cap;
    }
    result.factor = uniform ? first : weighted / total;
    if (!std::isfinite(result.factor) || result.factor <= 0) {
        result.why = "Correction factor is out of range";
        return result;
    }
    result.valid = true;
    result.exact = uniform;
    result.why = uniform ? "Uniform correction; configured rails assumed complete" :
        "Estimate weighted by rated rail capacity; per-rail power is unavailable";
    return result;
}
std::optional<double> shunt_parallel_resistance(const std::vector<double>& resistors) {
    if (resistors.empty()) return std::nullopt;
    // Scale by the smallest value to avoid overflowing individual reciprocals.
    double smallest = std::numeric_limits<double>::infinity();
    for (double value : resistors) {
        if (!std::isfinite(value) || value <= 0) return std::nullopt;
        if (value < smallest) smallest = value;
    }
    double sum = 0;
    for (double value : resistors) sum += smallest / value;
    const double answer = smallest / sum;
    return std::isfinite(answer) && answer > 0 ? std::optional<double>(answer) : std::nullopt;
}
std::wstring shunt_config_path() {
    auto base = environment(L"LOCALAPPDATA");
    if (base.empty()) base = environment(L"USERPROFILE");
    if (base.empty()) base = environment(L"HOMEDRIVE") + environment(L"HOMEPATH");
    if (base.empty()) throw std::runtime_error("Cannot determine shunt config home directory");
    return base + L"\\Thermetery\\Druta\\shuntmod.json";
}
ShuntConfig ShuntConfig::load(const std::wstring& path_override) {
    ShuntConfig result;
    result.rails = shunt_default_rails();
    try { result.path = path_override.empty() ? shunt_config_path() : path_override; }
    catch (const std::exception& error) { result.load_error = error.what(); return result; }
    if (result.path.find(L'\0') != std::wstring::npos) {
        result.load_error = "Invalid shunt config path";
        return result;
    }
    const DWORD attrs = GetFileAttributesW(result.path.c_str());
    if (attrs == INVALID_FILE_ATTRIBUTES) {
        const DWORD error = GetLastError();
        if (error == ERROR_FILE_NOT_FOUND || error == ERROR_PATH_NOT_FOUND) return result;
    }
    try {
        result.document = read_profile(result.path);
        if (!result.document.contains("rails")) return result;
        const auto& entries = result.document.at("rails");
        if (!entries.is_array()) throw std::runtime_error("Shunt rails must be an array");
        result.rails.clear();
        for (const auto& entry : entries) {
            ShuntRail rail;
            rail.extra = entry;
            if (entry.is_object()) {
                if (entry.contains("kind") && entry["kind"].is_string()) rail.kind = entry["kind"].get<std::string>();
                rail.orig = number(entry, "orig");
                rail.mod = number(entry, "mod");
            } else {
                rail.orig = rail.mod = std::numeric_limits<double>::quiet_NaN();
            }
            result.rails.push_back(std::move(rail));
        }
    } catch (const std::exception& error) { result.load_error = error.what(); }
    return result;
}
void ShuntConfig::save() const {
    if (!load_error.empty()) throw std::runtime_error("Cannot overwrite unreadable shunt config: " + load_error);
    auto out = document;
    auto entries = ProfileJson::array();
    for (const auto& rail : rails) {
        // Preserve unknown or malformed entries verbatim until explicitly removed.
        if (rail.kind.empty() || !capacity(rail.kind)) { entries.push_back(rail.extra); continue; }
        if (!std::isfinite(rail.orig) || !std::isfinite(rail.mod) || rail.orig <= 0 || rail.mod <= 0)
            throw std::runtime_error("Cannot save invalid shunt resistance");
        auto entry = rail.extra.is_object() ? rail.extra : ProfileJson::object();
        entry["kind"] = rail.kind;
        entry["orig"] = rail.orig;
        entry["mod"] = rail.mod;
        entries.push_back(std::move(entry));
    }
    out["rails"] = std::move(entries);
    create_parent_directories(path);
    write_profile(path, out);
}
}
