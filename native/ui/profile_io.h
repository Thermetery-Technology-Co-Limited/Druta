// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "vendor/json/json.hpp"
#include <cstddef>
#include <string>

namespace druta::ui {
using ProfileJson = nlohmann::json;
inline constexpr std::size_t profile_max_bytes = 1024 * 1024;

// Explicit paths only; these functions never select or apply a profile.
// Read/write UTF-8 JSON objects, limited to 1 MiB. Read rejects duplicate keys.
// Schema, adapter identity and tuning validation belong to the caller.
// Failures throw std::runtime_error with operation/path information.
ProfileJson read_profile(const std::wstring& path, std::size_t maximum_bytes = profile_max_bytes);

// Saves using an owned, same-directory temporary file, flushed before atomic
// replacement. Failure before replacement preserves any existing destination.
void write_profile(const std::wstring& path, const ProfileJson& document);
}
