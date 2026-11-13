// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "voltage_policy_view.h"
#include <imgui.h>
namespace druta::ui {
bool stage_voltage_policy_document(VoltagePolicyViewState& state,const ProfileJson& document,const VoltagePolicyData& data){
    try{const auto request=stage_voltage_policy_profile(document,data);state.staged=request;state.status="Policy profile staged; review and explicitly Apply";return true;}
    catch(const std::exception& e){state.status=e.what();return false;}
}
void sync_voltage_policy_view(VoltagePolicyViewState& state,const VoltagePolicyData* data,uint64_t generation){
    if(!state.initialized||state.generation!=generation){state={};state.initialized=true;state.generation=generation;}
    if(data)for(size_t i=0;i<3;++i){if(!state.dirty[i]){state.draft_baseline[i].reset();if(data->rows[i].current)state.drafts[i]=*data->rows[i].current;}
        else if(!state.draft_baseline[i]&&data->rows[i].current)state.draft_baseline[i]=data->rows[i].current;}
}
VoltagePolicyViewOutput draw_voltage_policy_controls(VoltagePolicyViewState& state,const VoltagePolicyData* data,
    uint64_t generation,bool busy,bool unlocked,bool classic,bool connected){
    (void)classic;sync_voltage_policy_view(state,data,generation);VoltagePolicyViewOutput output;
    ImGui::PushID("voltage-policy");
    ImGui::TextUnformatted("Voltage requests & VF policies");
    ImGui::BeginDisabled(busy||!connected);if(ImGui::Button("Read voltage policies"))output.intents.push_back({VoltagePolicyIntent::Operation::refresh,{voltage_all_policies,0,{}}});ImGui::EndDisabled();
    ImGui::TextDisabled("Session-only settings");
    constexpr const char* names[]={"Allow voltage requests","Enforce VF monotonicity","Enforce VF smoothing"};
    for(size_t i=0;i<3;++i){ImGui::PushID(static_cast<int>(i));const uint32_t bit=1u<<i;
        const auto* row=data?&data->rows[i]:nullptr;
        const bool ready=row&&row->current.has_value()&&row->writable;
        const bool mutate=connected&&!busy&&unlocked&&ready&&!(data&&data->recovery_pending);
        ImGui::Separator();ImGui::TextWrapped("%s",names[i]);
        if(row&&row->current){ImGui::Text("Current: %s%s",*row->current?"On":"Off",row->availability==VoltagePolicyAvailability::available?"":" (stale / unavailable)");}
        else ImGui::TextUnformatted("Current: unavailable");
        ImGui::BeginDisabled(!mutate);
        if(ImGui::Checkbox("Requested",&state.drafts[i])){if(!state.dirty[i])state.draft_baseline[i]=row->current;state.dirty[i]=true;}
        ImGui::SameLine();if(ImGui::Button("Apply")){
            const auto baseline=state.draft_baseline[i]?state.draft_baseline[i]:row->current;
            output.intents.push_back({VoltagePolicyIntent::Operation::apply,{bit,state.drafts[i]?bit:0,baseline&&*baseline?bit:0}});
            state.dirty[i]=false;
        }ImGui::EndDisabled();
        const bool explicit_initial=data&&data->recovery_prior_unknown&&data->recovery_selection==bit;
        ImGui::SameLine();ImGui::BeginDisabled(busy||!unlocked||!row||!row->initial.has_value()||(!mutate&&!explicit_initial));
        if(ImGui::Button(explicit_initial?"Request captured initial":"Restore initial"))output.intents.push_back({VoltagePolicyIntent::Operation::restore_initial,{bit,0,{}}});ImGui::EndDisabled();
        if(row&&row->initial){ImGui::SameLine();ImGui::TextDisabled("Captured: %s",*row->initial?"On":"Off");}
        if(row&&!row->message.empty())ImGui::TextWrapped("%s",row->message.c_str());
        ImGui::Checkbox("Include in policy profile",&state.included[i]);
        ImGui::PopID();
    }
    ImGui::BeginDisabled(busy||!unlocked||!data||!data->undo_available||data->recovery_pending);
    if(ImGui::Button("Undo voltage policy"))output.intents.push_back({VoltagePolicyIntent::Operation::undo,{}});ImGui::EndDisabled();
    ImGui::SameLine();ImGui::BeginDisabled(busy||!unlocked||!data||!data->recovery_pending);
    if(ImGui::Button("Recover selected policy"))output.intents.push_back({VoltagePolicyIntent::Operation::recover,{}});ImGui::EndDisabled();
    if(!unlocked)ImGui::TextDisabled("Unlock controls to apply, restore or recover. Reading is available while locked.");
    if(data&&!data->message.empty())ImGui::TextWrapped("%s",data->message.c_str());
    if(data&&data->recovery_prior_unknown)ImGui::TextWrapped("The driver did not confirm the previous state. Request captured initial applies the value recorded at this session's first qualified read.");
    uint32_t selection=0;for(size_t i=0;i<3;++i)if(state.included[i])selection|=1u<<i;
    const bool mixed=(selection&voltage_admission)&&(selection&voltage_vf_policies);
    bool save_ready=data&&data->target&&data->identity.uuid[0]&&!data->recovery_pending&&selection&&!mixed;
    if(data)for(size_t i=0;i<3;++i)if(selection&(1u<<i))save_ready=save_ready&&data->rows[i].current.has_value()&&data->rows[i].availability==VoltagePolicyAvailability::available;
    ImGui::BeginDisabled(busy||!connected||!save_ready);if(ImGui::Button("Save selected policy profile..."))output.intents.push_back({VoltagePolicyIntent::Operation::save_profile,{selection,0,{}}});ImGui::EndDisabled();
    ImGui::SameLine();ImGui::BeginDisabled(busy||!connected);if(ImGui::Button("Load policy profile..."))output.intents.push_back({VoltagePolicyIntent::Operation::load_profile,{}});ImGui::EndDisabled();
    if(mixed)ImGui::TextDisabled("Save admission separately from monotonicity / smoothing.");
    if(state.staged){const auto request=*state.staged;ImGui::TextUnformatted("Staged policy requests:");for(size_t i=0;i<3;++i)if(request.selection&(1u<<i))ImGui::Text("%s: %s",names[i],(request.values&(1u<<i))?"On":"Off");
        bool ready=connected&&!busy&&unlocked&&data&&!data->recovery_pending;for(size_t i=0;i<3;++i)if(request.selection&(1u<<i))ready=ready&&data&&data->rows[i].writable&&data->rows[i].current.has_value()&&data->rows[i].availability==VoltagePolicyAvailability::available;
        ImGui::BeginDisabled(!ready);if(ImGui::Button("Apply staged policy profile"))output.intents.push_back({VoltagePolicyIntent::Operation::apply,request});ImGui::EndDisabled();ImGui::SameLine();if(ImGui::Button("Discard staged policy profile"))state.staged.reset();
    }
    if(!state.status.empty())ImGui::TextWrapped("%s",state.status.c_str());
    if(ImGui::CollapsingHeader("Policy details")) {
        ImGui::TextWrapped("Initial is the first qualified reading in this adapter session. Readback verifies the driver's requested flags; their physical effect has not been established.");
        ImGui::TextWrapped("An On/Off reading cannot reveal another program's identical request, or a value changed and then changed back between reads.");
        ImGui::TextWrapped("Profiles save selected Boolean requests and load as drafts. Admission and VF flags use separate transactions. No startup or post-reset application occurs.");
    }
    ImGui::PopID();return output;
}
}
