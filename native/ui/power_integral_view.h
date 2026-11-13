// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "power_integral_service.h"
namespace druta::ui {
enum class PowerIntegralAction { refresh,apply,reported_default,undo,restore,save_profile,load_profile,preview_profile,apply_profile,undo_profile,recover_profile };
struct PowerIntegralIntent {
    PowerIntegralAction action=PowerIntegralAction::refresh;
    uint32_t policy=0;PowerIntegralField field=PowerIntegralField::lower;uint32_t raw=0;
    uint16_t expected=0;
    uint64_t generation=0;std::string slot,uuid;
    std::vector<PowerIntegralRequest> requests;
    std::vector<PowerIntegralKey> selection;
};
struct PowerIntegralViewContext {
    uint64_t generation=0;std::string slot,uuid;
    bool connected=false,busy=false,unlocked=false;
};
struct PowerIntegralViewState {
    bool open=false;
    uint64_t generation=0;std::string slot,uuid;
    int selected_policy=-1;
    std::array<std::array<double,2>,32> percent{};
    std::array<std::array<uint16_t,2>,32> baseline{};
    std::array<std::array<bool,2>,32> initialized{},dirty{};
    std::array<std::array<bool,2>,32> included{};
    std::optional<ProfileJson> loaded_document;
    std::vector<PowerIntegralRequest> loaded_requests;
    std::string status;
};
void stage_power_integral_document(PowerIntegralViewState&,const ProfileJson&,const PowerIntegralData&);
// Shared dialog for both skins. Drafts survive refresh and page changes, but
// never adapter/generation changes. This view invokes no driver functions.
std::vector<PowerIntegralIntent> draw_power_integral(PowerIntegralViewState& state,
    const PowerIntegralData& data,const PowerIntegralViewContext& context,bool inline_panel=false);
}
