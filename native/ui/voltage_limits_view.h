// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "voltage_limits_service.h"
namespace druta::ui {
struct VoltageLimitsIntent {
    enum class Operation {read,apply,restore_initial,restore_rom,undo,recover,save_profile,load_profile};
    Operation operation=Operation::read;uint32_t families=3;
    std::vector<VoltageLimitRequest> requests;std::vector<VoltageLimitKey> selection;
};
struct VoltageLimitsDraft {VoltageLimitKey key;int64_t value=0,lower=0;bool dirty=false,include=true;std::string binding;std::optional<int64_t> expected_value,expected_lower;};
struct VoltageLimitsViewState {bool initialized=false;uint64_t generation=0;std::vector<VoltageLimitsDraft> drafts;std::vector<VoltageLimitRequest> staged;std::string status;};
void sync_voltage_limits_view(VoltageLimitsViewState&,const VoltageLimitsData*,uint64_t generation);
bool stage_voltage_limits_document(VoltageLimitsViewState&,const ProfileJson&,const VoltageLimitsData&);
std::vector<VoltageLimitsIntent> draw_voltage_limits_controls(VoltageLimitsViewState&,const VoltageLimitsData*,uint64_t generation,bool busy,bool unlocked,bool classic=false);
}
