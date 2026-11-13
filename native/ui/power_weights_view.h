// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "power_weights_service.h"
namespace druta::ui {
struct PowerWeightDraft {uint32_t index=0;double multiplier=1;std::optional<uint16_t> expected;bool dirty=false,include=false;};
struct PowerWeightsViewState {uint64_t generation=0;bool initialized=false;std::vector<PowerWeightDraft> drafts;std::string status;};
struct PowerWeightsIntent {enum class Operation {read,preview,prepare,inspect,activate_saved,verify,remove};Operation operation=Operation::read;std::vector<PowerWeightRequest> requests;};
void sync_power_weights_view(PowerWeightsViewState&,const PowerWeightsData*,uint64_t generation);
std::vector<PowerWeightsIntent> draw_power_weights(PowerWeightsViewState&,const PowerWeightsData*,uint64_t generation,bool busy,bool classic,bool unlocked=true,bool connected=true);
}
