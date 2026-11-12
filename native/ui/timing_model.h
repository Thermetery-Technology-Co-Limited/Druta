/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include "timings_service.h"
#include <optional>
#include <string>
namespace druta::ui {
// Worker-owned result, published as an immutable snapshot. Preview is the
// canonical backend plan; the view cannot synthesize or replace it.
struct TimingViewData {
    std::wstring helper_path;
    TimingSnapshot snapshot;
    std::optional<TimingPlan> preview;
    bool undo_available = false;
    std::string message;
};
}
