/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include "backend.h"
#include "timing_model.h"
#include "timing_profile.h"
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
    std::vector<TimingRequest> staged;
    std::string message;
    std::wstring observed_helper;
    std::string observed_token;
};
struct TimingsViewOutput { std::vector<Command> commands; bool choose_helper = false,save_profile=false,load_profile=false; };
void sync_timings_view(TimingsViewState& state, const TimingViewData* data, std::uint64_t generation);
bool select_timing_field(TimingsViewState& state, const TimingViewData& data, const std::string& field);
bool can_preview_timing(const TimingsViewState& state, const TimingViewData* data, bool busy, bool unlocked);
bool can_apply_timing(const TimingsViewState& state, const TimingViewData* data, bool busy, bool unlocked);
bool stage_timing_field(TimingsViewState& state,const TimingViewData& data,const std::string& field,uint32_t value);
bool stage_timing_profile(const ProfileJson& document,const TimingViewData& data,TimingsViewState& state);
bool can_preview_timing_batch(const TimingsViewState& state,const TimingViewData* data,bool busy);
bool can_apply_timing_batch(const TimingsViewState& state,const TimingViewData* data,bool busy,bool unlocked);
TimingsViewOutput draw_timings(TimingsViewState& state, const TimingViewData* data,
                               std::uint64_t generation, bool busy, bool unlocked);
}
