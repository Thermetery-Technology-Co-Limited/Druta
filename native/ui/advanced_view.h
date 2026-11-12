/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include "backend.h"
#include "risk_policy.h"
#include <map>
namespace druta::ui {
struct SessionSnapshot;
struct AdvancedDraft {
    double value = 0;
    bool initialized = false, dirty = false, editing = false;
    std::string binding;
};
struct AdvancedViewState {
    uint64_t generation = 0;
    std::map<uint64_t, AdvancedDraft> drafts;
    std::string notice;
};
RiskAssessment session_risk(const LiveData& data);
void draw_mode_bar(const SessionSnapshot& snapshot, bool& unlocked, std::vector<Command>& commands);
void draw_advanced_controls(const SessionSnapshot& snapshot, bool unlocked, bool verbose,
    AdvancedViewState& state, std::vector<Command>& commands);
}
