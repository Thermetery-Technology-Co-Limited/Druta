// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "frequency_lock.h"
namespace druta::ui {
enum class FrequencyLockAction { refresh,apply,lock_max,legacy_p0_max_fan,release };
struct FrequencyLockIntent {FrequencyLockAction action=FrequencyLockAction::refresh;int64_t minimum_mhz=0,maximum_mhz=0;};
struct FrequencyLockViewContext {
    uint64_t generation=0;
    std::string slot,uuid;
    bool connected=false,busy=false,unlocked=false,fan_manual_available=false;
    std::optional<double> live_core_mhz;
    // Set only when the coordinator proves the visible min=max request is
    // this session's exact owned I2C clamp. Apply still performs handover.
    bool other_owned_clock=false;
};
struct FrequencyLockViewState {
    bool open=false,initialized=false,dirty=false;
    uint64_t generation=0;
    std::string slot,uuid;
    int64_t minimum_mhz=0,maximum_mhz=0;
    std::optional<FrequencyRange> observed_owned;
};
// Emits worker intents only. The host captures fan Undo and coordinates owned
// I2C/VF/headroom handover; this view never invokes a driver or spawns a worker.
std::vector<FrequencyLockIntent> draw_frequency_lock(FrequencyLockViewState& state,
    const FrequencyLockData& data,const FrequencyLockViewContext& context);
}
