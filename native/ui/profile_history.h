// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "profile_io.h"
#include <functional>
#include <memory>
namespace druta::ui {
// The worker captures semantic requests before invoking this storage boundary.
// Tests inject their own sink; ordinary polling and construction never save.
struct ProfileHistorySink {
    std::function<void(const std::string& action, const ProfileJson& document)> save;
    // Required captured timing backup, stored outside the autosave retention ring.
    std::function<std::string(const ProfileJson& document)> backup;
};
std::shared_ptr<ProfileHistorySink> make_profile_history_sink();
}
