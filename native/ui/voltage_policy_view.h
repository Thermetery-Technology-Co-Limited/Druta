// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "voltage_policy_service.h"
#include <vector>
namespace druta::ui {
struct VoltagePolicyIntent {
    enum class Operation {refresh,apply,restore_initial,undo,recover,save_profile,load_profile};
    Operation operation=Operation::refresh;
    VoltagePolicyRequest request;
};
// Classic: the last action a part's controls sent (a Read or a request) and, from the first
// revision published after it, that action's result. A flag's Apply or Restore also keeps
// the flag and the value it asked for, so the part's line reads as registry_panel.py's.
struct VoltagePolicyLastAction {bool read=false;uint64_t revision=0;std::optional<std::string> outcome;uint32_t bit=0;bool value=false;};
struct VoltagePolicyViewState {
    uint64_t generation=0;
    bool initialized=false;
    std::array<bool,3> drafts{},dirty{};
    std::array<std::optional<bool>,3> draft_baseline{};
    std::array<bool,3> included{};
    std::optional<VoltagePolicyRequest> staged;
    std::string status;
    std::array<std::optional<VoltagePolicyLastAction>,2> last; // classic: 0 gate, 1 VF checks
};
struct VoltagePolicyViewOutput {std::vector<VoltagePolicyIntent> intents;};
// Classic draws the admission gate and the VF checks as separate subsections
// (released 1.9.1 Runtime voltage-request / Runtime VF policy control).
enum class VoltagePolicyPart {all,admission,vf_policies};
void sync_voltage_policy_view(VoltagePolicyViewState& state,const VoltagePolicyData* data,uint64_t generation);
bool stage_voltage_policy_document(VoltagePolicyViewState&,const ProfileJson&,const VoltagePolicyData&);
VoltagePolicyViewOutput draw_voltage_policy_controls(VoltagePolicyViewState& state,const VoltagePolicyData* data,
    uint64_t generation,bool busy,bool unlocked,bool classic=false,bool connected=true,VoltagePolicyPart part=VoltagePolicyPart::all);
// A part draws its rows, read, save and its own staged profile. Undo, Recover, Load
// and Discard act on both parts; classic draws them once with the shared controls.
VoltagePolicyViewOutput draw_voltage_policy_shared_controls(VoltagePolicyViewState& state,const VoltagePolicyData* data,
    uint64_t generation,bool busy,bool unlocked,bool connected=true);
}
