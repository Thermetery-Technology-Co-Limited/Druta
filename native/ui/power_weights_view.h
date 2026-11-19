// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "power_weights_service.h"
#include "pascal_clock_table.h"
namespace druta::ui {
struct PowerWeightDraft {uint32_t index=0;double multiplier=1;std::optional<uint16_t> expected;bool dirty=false,include=false;};
struct PowerWeightsViewState {uint64_t generation=0;bool initialized=false;std::vector<PowerWeightDraft> drafts;std::string status;};
struct PowerWeightsIntent {enum class Operation {read,preview,prepare,inspect,activate_saved,verify,remove,read_clocks,prepare_clocks};Operation operation=Operation::read;std::vector<PowerWeightRequest> requests; std::vector<PascalClockEdit> clock_edits{};};
void sync_power_weights_view(PowerWeightsViewState&,const PowerWeightsData*,uint64_t generation);
// Released 1.9.1 registry_panel relationship_lines: the read-only text Classic
// shows under Diagnostics > Power/current arbitration.
std::string power_relationships_text(const PowerWeightsData*);
// The note, "Read relationships" and a 180 px read-only box; true when Read is pressed.
bool draw_power_relationships(const PowerWeightsData*,bool busy,bool connected);
std::vector<PowerWeightsIntent> draw_power_weights(PowerWeightsViewState&,const PowerWeightsData*,uint64_t generation,bool busy,bool classic,bool unlocked=true,bool connected=true);
}
