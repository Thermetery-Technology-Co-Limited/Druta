// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "voltage_policy_service.h"
#include <vector>
namespace druta::ui {
struct VoltagePolicyIntent {
    enum class Operation {refresh,apply,restore_initial,undo,recover};
    Operation operation=Operation::refresh;
    VoltagePolicyRequest request;
};
struct VoltagePolicyViewState {
    uint64_t generation=0;
    bool initialized=false;
    std::array<bool,3> drafts{},dirty{};
    std::array<std::optional<bool>,3> draft_baseline{};
};
struct VoltagePolicyViewOutput {std::vector<VoltagePolicyIntent> intents;};
void sync_voltage_policy_view(VoltagePolicyViewState& state,const VoltagePolicyData* data,uint64_t generation);
VoltagePolicyViewOutput draw_voltage_policy_controls(VoltagePolicyViewState& state,const VoltagePolicyData* data,
    uint64_t generation,bool busy,bool unlocked,bool classic=false);
}
