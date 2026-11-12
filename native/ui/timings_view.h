/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include "backend.h"
#include "timing_model.h"
#include <cstdint>
#include <string>
#include <vector>
namespace druta::ui {
struct TimingsHelperSuggestion { std::wstring path; std::string message; };
// Read-only discovery; an explicit environment override is exclusive.
TimingsHelperSuggestion suggest_timing_helper();
struct TimingsViewState {
    std::uint64_t generation = 0;
    bool initialized = false;
    std::string field;
    std::uint32_t value = 0;
};
struct TimingsViewOutput { std::vector<Command> commands; bool choose_helper = false; };
void sync_timings_view(TimingsViewState& state, const TimingViewData* data, std::uint64_t generation);
bool select_timing_field(TimingsViewState& state, const TimingViewData& data, const std::string& field);
bool can_preview_timing(const TimingsViewState& state, const TimingViewData* data, bool busy, bool unlocked);
bool can_apply_timing(const TimingsViewState& state, const TimingViewData* data, bool busy, bool unlocked);
TimingsViewOutput draw_timings(TimingsViewState& state, const TimingViewData* data,
                               std::uint64_t generation, bool busy, bool unlocked);
}
