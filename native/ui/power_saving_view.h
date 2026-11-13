// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "power_saving_service.h"
#include <array>
namespace druta::ui {
struct PowerSavingIntent {
    enum class Operation {read,apply,restore_initial,undo,recover,save_profile,load_profile,release_owned};
    Operation operation=Operation::read;uint32_t families=31;
    std::vector<PowerSavingRequest> requests;std::vector<PowerSavingKey> selection;
};
struct PowerSavingDraft {PowerSavingKey key;std::array<char,32> text{};bool dirty=false,include=false;std::string binding;std::optional<uint32_t> expected;};
struct PowerSavingViewState {bool initialized=false;uint64_t generation=0;std::vector<PowerSavingDraft> drafts;std::vector<PowerSavingRequest> staged;std::string status;};
std::optional<uint32_t> parse_power_saving_us(const std::string&);
void sync_power_saving_view(PowerSavingViewState&,const PowerSavingData*,uint64_t generation);
bool stage_power_saving_document(PowerSavingViewState&,const ProfileJson&,const PowerSavingData&);
std::vector<PowerSavingIntent> draw_power_saving_controls(PowerSavingViewState&,const PowerSavingData*,uint64_t generation,bool busy,bool unlocked,bool classic=false);
}
