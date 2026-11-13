// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "clock_gating_service.h"
namespace druta::ui {
struct ClockGatingIntent {
    enum class Operation {read,apply,release,restore_initial,recover,undo,save_profile,load_profile};
    Operation operation=Operation::read;uint32_t families=15;
    ClockGatingFamily family=ClockGatingFamily::engine;
    std::vector<ClockGatingRequest> requests;std::vector<ClockGatingFamily> selection;
};
struct ClockGatingViewState {bool initialized=false;uint64_t generation=0;uint32_t pcie_generation=1;bool pcie_dirty=false;std::optional<uint32_t> pcie_baseline;std::string pcie_binding;std::array<bool,4> included{};std::vector<ClockGatingRequest> staged;std::string status;};
void sync_clock_gating_view(ClockGatingViewState&,const ClockGatingData*,uint64_t generation);
bool stage_clock_gating_document(ClockGatingViewState&,const ProfileJson&,const ClockGatingData&);
std::vector<ClockGatingIntent> draw_clock_gating_controls(ClockGatingViewState&,const ClockGatingData*,uint64_t generation,bool busy,bool unlocked,bool classic=false);
}
