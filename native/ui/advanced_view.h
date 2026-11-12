/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include "backend.h"
#include "risk_policy.h"
#include <map>
#include <functional>
namespace druta::ui {
struct SessionSnapshot;
enum class OffsetGroup { all, daily_clocks, additional_memory };
struct AdvancedDraft {
    double value = 0;
    bool initialized = false, dirty = false, editing = false;
    std::string binding;
};
struct AdvancedViewState {
    uint64_t generation = 0;
    std::map<uint64_t, AdvancedDraft> drafts;
    std::string notice;
    double rail_gate_flash_until = 0, xoc_gate_flash_until = 0;
};
RiskAssessment session_risk(const LiveData& data);
void draw_mode_bar(const SessionSnapshot& snapshot, bool& unlocked, std::vector<Command>& commands,
    bool classic = false, const std::function<void()>& i2c_gate = {}, AdvancedViewState* view = nullptr);
void draw_advanced_controls(const SessionSnapshot& snapshot, bool unlocked, bool verbose,
    AdvancedViewState& state, std::vector<Command>& commands, bool include_rails = true, bool include_offsets = true);
void draw_clock_voltage_offsets(const SessionSnapshot& snapshot, bool unlocked, bool classic,
    AdvancedViewState& state, std::vector<Command>& commands, OffsetGroup group = OffsetGroup::all);
// Common row services reused by the original Control layout and Focused Daily.
void draw_clock_domain_controls(const SessionSnapshot& snapshot, bool unlocked, bool classic,
    AdvancedViewState& state, std::vector<Command>& commands);
void draw_core_current_limit(const SessionSnapshot& snapshot, bool unlocked, bool classic,
    AdvancedViewState& state, std::vector<Command>& commands);
void draw_daily_rail_controls(const SessionSnapshot& snapshot, bool unlocked, bool classic,
    AdvancedViewState& state, std::vector<Command>& commands, bool include_offsets = true);
void draw_power_policy_controls(const SessionSnapshot& snapshot, bool unlocked, bool classic,
    AdvancedViewState& state, std::vector<Command>& commands);
}
