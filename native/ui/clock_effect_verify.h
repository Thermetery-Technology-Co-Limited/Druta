// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "drutacore/drutacore.h"
#include <array>
#include <functional>
#include <map>
#include <string>
#include <utility>
#include <vector>

namespace druta::ui {
struct ClockEffectApi {
    decltype(&dc_gpu_identity_get) identity{};
    decltype(&dc_gpu_read_telemetry) telemetry{};
    decltype(&dc_clock_domains_classify) classify{};
    decltype(&dc_gpu_public_clock_readings) public_clocks{}; // optional
    decltype(&dc_gpu_clkdom_measurements) measurements{};   // optional
    decltype(&dc_gpu_clkdom_requests) requests{};
    decltype(&dc_gpu_clkdom_layout) layout{};
    decltype(&dc_gpu_clkdom_offset_write_capability) capability{};
    decltype(&dc_gpu_clkdom_offset_set) set{};
    decltype(&dc_gpu_lock) lock{};
    decltype(&dc_gpu_unlock) unlock{};
    std::function<void(unsigned)> wait_ms;
    decltype(&dc_gpu_architecture) architecture{};          // optional naming metadata
};
struct ClockEffectSample {
    uint32_t pstate{};
    std::map<std::string, uint32_t> counters_khz;
    std::map<std::string, std::string> groups; // confirmed counter aliases
    std::map<unsigned,std::pair<uint32_t,uint32_t>> private_source_scale;
    dc_clkdom_requests requests{};
};
struct ClockEffectChange {
    std::string counter;
    double baseline_khz{}, applied_khz{}, restored_khz{}, delta_khz{};
    bool reversible{}, independent{};
};
struct ClockEffectPending {
    bool active{};
    std::string slot, uuid;
    uint32_t domain{};
    int32_t original_khz{}, test_khz{};
    dc_clkdom_requests before{};
    dc_clkdom_layout layout{};
};
struct ClockEffectResult {
    bool attempted{}, restored{}, verified{}, completed{}, p0_lost{};
    std::string message;
    std::array<std::vector<ClockEffectSample>, 3> phases; // baseline, test, restored
    std::vector<ClockEffectChange> changes;
    bool coupled{};
    std::vector<std::string> affected_groups;
    std::vector<std::string> physical_groups, programmed_groups;
    std::array<std::vector<ClockEffectSample>, 2> transitions, confirmation_transitions;
    std::array<std::vector<ClockEffectSample>, 3> confirmation_phases;
    std::vector<ClockEffectChange> confirmation_changes;
};
ClockEffectResult verify_clock_effect(const ClockEffectApi&, dc_gpu*, const std::string& slot,
    const std::string& uuid, uint32_t domain, ClockEffectPending&);
ClockEffectResult restore_clock_effect(const ClockEffectApi&, dc_gpu*, ClockEffectPending&);
}
