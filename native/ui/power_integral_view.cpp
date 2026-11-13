// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "power_integral_view.h"
#include "imgui.h"
#include <cstdio>
#include <algorithm>
namespace druta::ui {
void stage_power_integral_document(PowerIntegralViewState& state,const ProfileJson& document,const PowerIntegralData& data){
    const auto requests=stage_power_integral_profile(document,data);
    state.included={};for(const auto& r:requests){const auto i=r.field==PowerIntegralField::lower?0u:1u;state.included[r.policy][i]=true;state.percent[r.policy][i]=power_integral_raw_to_percent(static_cast<uint16_t>(r.raw));state.baseline[r.policy][i]=*r.expected;state.dirty[r.policy][i]=state.initialized[r.policy][i]=true;}
    state.loaded_document=document;state.loaded_requests=requests;state.status="Profile staged only. Review selected bounds, Preview, then explicit Apply.";
}
std::vector<PowerIntegralIntent> draw_power_integral(PowerIntegralViewState& state,
    const PowerIntegralData& data,const PowerIntegralViewContext& context,bool inline_panel) {
    std::vector<PowerIntegralIntent> intents;
    if(state.generation!=context.generation || state.slot!=context.slot || state.uuid!=context.uuid) {
        const bool open=state.open;state={};state.open=open;state.generation=context.generation;state.slot=context.slot;state.uuid=context.uuid;
    }
    for(const auto& row:data.rows)if(row.policy<32) {
        if(state.selected_policy<0)state.selected_policy=static_cast<int>(row.policy);
        for(size_t field=0;field<2;++field)if(!state.dirty[row.policy][field]) {
            state.percent[row.policy][field]=power_integral_raw_to_percent(field==0?row.lower:row.upper);
            state.baseline[row.policy][field]=field==0?row.lower:row.upper;
            state.initialized[row.policy][field]=true;
        }
    }
    if(!inline_panel && !state.open)return intents;
    auto emit=[&](PowerIntegralAction action,uint32_t policy=0,PowerIntegralField field=PowerIntegralField::lower,uint32_t raw=0,std::optional<uint16_t> expected_override={}){
        auto expected=expected_override.value_or(policy<32?state.baseline[policy][field==PowerIntegralField::lower?0:1]:uint16_t{0});
        if(action==PowerIntegralAction::apply){const auto loaded=std::find_if(state.loaded_requests.begin(),state.loaded_requests.end(),[&](const auto& r){return r.policy==policy&&r.field==field;});if(loaded!=state.loaded_requests.end()&&loaded->expected)expected=*loaded->expected;}
        intents.push_back({action,policy,field,raw,expected,context.generation,context.slot,context.uuid});
    };
    bool recovery=false;for(const auto& row:data.rows)for(const auto& owner:row.undo)recovery=recovery || owner.recovery;
    if(!inline_panel)ImGui::SetNextWindowSize({790,640},ImGuiCond_FirstUseEver);
    const bool visible=inline_panel || ImGui::Begin("Power feedback ratios###druta_power_integral",&state.open,ImGuiWindowFlags_NoSavedSettings);
    ImGui::PushID("power_integral_panel");
    if(visible) {
        ImGui::BeginDisabled(!context.connected || context.busy);
        if(ImGui::Button("Read feedback requests"))emit(PowerIntegralAction::refresh);
        ImGui::EndDisabled();
        if(!data.message.empty())ImGui::TextWrapped("%s",data.message.c_str());
        if(!data.current && !data.rows.empty())ImGui::TextWrapped("Captured rows retained. Read current requests before applying.");
        if(!state.status.empty())ImGui::TextWrapped("%s",state.status.c_str());
        if(recovery)ImGui::TextWrapped("An uncertain or replaced field needs Undo / Restore recovery before further edits.");
        char selected[64];std::snprintf(selected,sizeof(selected),"Policy %d",state.selected_policy);
        if(ImGui::BeginCombo("Policy",state.selected_policy<0?"Read to discover policies":selected)) {
            for(const auto& row:data.rows){char label[96];std::snprintf(label,sizeof(label),"Policy %u | type %u | channel %u%s",row.policy,row.type,row.channel,row.available?"":" (unavailable)");
                if(ImGui::Selectable(label,state.selected_policy==static_cast<int>(row.policy)))state.selected_policy=static_cast<int>(row.policy);}
            ImGui::EndCombo();
        }
        for(const auto& row:data.rows)if(row.policy<32 && state.selected_policy==static_cast<int>(row.policy)) {
            ImGui::PushID(static_cast<int>(row.policy));
            if(row.available)ImGui::Text("Ordinary policy request: %u %s | samples %u / %u",row.request,row.unit==0?"mW":"mA",static_cast<unsigned>(row.past_samples),static_cast<unsigned>(row.next_samples));
            else ImGui::TextWrapped("Unavailable: %s",row.reason.c_str());
            for(size_t display=0;display<2;++display) {
                const size_t i=display==0?1:0; // Released panel: upper, then lower.
                ImGui::PushID(static_cast<int>(i));const auto field=i==0?PowerIntegralField::lower:PowerIntegralField::upper;
                const uint16_t current=i==0?row.lower:row.upper,reported=i==0?row.reported_lower:row.reported_upper;
                ImGui::SeparatorText(i==0?"Lower feedback bound":"Upper feedback bound");
                ImGui::BeginDisabled(context.busy);ImGui::Checkbox("Include in profile",&state.included[row.policy][i]);ImGui::EndDisabled();
                ImGui::Text("Request: %.8g%% (raw %u) | reported default: %.8g%% (raw %u)",power_integral_raw_to_percent(current),static_cast<unsigned>(current),power_integral_raw_to_percent(reported),static_cast<unsigned>(reported));
                ImGui::BeginDisabled(context.busy);ImGui::SetNextItemWidth(145);
                if(ImGui::InputDouble("Adjustment (%)",&state.percent[row.policy][i],0,0,"%.8f"))state.dirty[row.policy][i]=true;
                ImGui::EndDisabled();
                const auto raw=power_integral_percent_to_raw(state.percent[row.policy][i]);
                const auto valid=[&](uint32_t value){return i==0?value<=row.upper:(value>0 && value>=row.lower);};
                const bool ready=context.connected && context.unlocked && !context.busy && data.current && row.available && !recovery;
                const auto loaded=std::find_if(state.loaded_requests.begin(),state.loaded_requests.end(),[&](const auto& r){return r.policy==row.policy&&r.field==field;});
                const bool loaded_binding_matches=loaded==state.loaded_requests.end()||(loaded->type==row.type&&loaded->channel==row.channel&&loaded->unit==row.unit&&loaded->binding==row.binding);
                ImGui::BeginDisabled(!ready || !raw || !valid(raw.value_or(0)) || !loaded_binding_matches);
                if(ImGui::Button("Apply now")){emit(PowerIntegralAction::apply,row.policy,field,*raw);state.dirty[row.policy][i]=false;}
                ImGui::EndDisabled();ImGui::SameLine();
                ImGui::BeginDisabled(!ready || !valid(reported));
                if(ImGui::Button("Use reported default")){emit(PowerIntegralAction::reported_default,row.policy,field,reported,current);state.dirty[row.policy][i]=false;}
                ImGui::EndDisabled();
                const auto& owner=row.undo[i];
                ImGui::BeginDisabled(context.busy || !context.unlocked || !owner.owned || !owner.can_undo);
                if(ImGui::Button("Undo previous edit"))emit(PowerIntegralAction::undo,row.policy,field);
                ImGui::EndDisabled();ImGui::SameLine();
                ImGui::BeginDisabled(context.busy || !context.unlocked || !owner.owned);
                if(ImGui::Button("Restore captured initial"))emit(PowerIntegralAction::restore,row.policy,field);
                ImGui::EndDisabled();
                if(owner.owned)ImGui::Text("Captured initial: %.8g%% | previous edit: %.8g%%%s%s",power_integral_raw_to_percent(owner.initial),power_integral_raw_to_percent(owner.previous),owner.confirmed?" | verified":" | unconfirmed",owner.conflict?" | foreign replacement":"");
                ImGui::PopID();
            }
            ImGui::PopID();
        }
        std::vector<PowerIntegralKey> selection;std::vector<PowerIntegralRequest> requests;bool valid=true,binding_changed=false;
        for(const auto& row:data.rows)if(row.policy<32)for(size_t i=0;i<2;++i)if(state.included[row.policy][i]){
            const auto field=i==0?PowerIntegralField::lower:PowerIntegralField::upper;selection.push_back({row.policy,field});const auto raw=power_integral_percent_to_raw(state.percent[row.policy][i]);if(!raw||!row.available)valid=false;
            const auto loaded=std::find_if(state.loaded_requests.begin(),state.loaded_requests.end(),[&](const auto& r){return r.policy==row.policy&&r.field==field;});
            if(loaded!=state.loaded_requests.end()){
                auto request=*loaded;request.raw=raw.value_or(0);requests.push_back(std::move(request));
                if(loaded->type!=row.type||loaded->channel!=row.channel||loaded->unit!=row.unit||loaded->binding!=row.binding)binding_changed=true;
            }else requests.push_back({row.policy,field,raw.value_or(0),row.type,row.channel,row.unit,state.baseline[row.policy][i],row.binding});
        }
        // An omitted/temporarily unavailable policy must not silently turn a
        // loaded multi-policy request into a smaller selected transaction.
        for(uint32_t policy=0;policy<32;++policy)for(size_t i=0;i<2;++i)if(state.included[policy][i]){
            const auto field=i==0?PowerIntegralField::lower:PowerIntegralField::upper;
            if(std::any_of(requests.begin(),requests.end(),[&](const auto& r){return r.policy==policy&&r.field==field;}))continue;
            selection.push_back({policy,field});valid=false;
            const auto loaded=std::find_if(state.loaded_requests.begin(),state.loaded_requests.end(),[&](const auto& r){return r.policy==policy&&r.field==field;});
            if(loaded!=state.loaded_requests.end()){requests.push_back(*loaded);binding_changed=true;}
        }
        valid=valid&&!binding_changed;
        const auto profile_emit=[&](PowerIntegralAction action){emit(action);intents.back().selection=selection;intents.back().requests=requests;};
        ImGui::SeparatorText("Selected feedback profile");ImGui::Text("%u selected bounds",static_cast<unsigned>(selection.size()));
        if(binding_changed)ImGui::TextWrapped("A loaded policy's identity or geometry changed. Reload or discard the loaded profile before applying; draft edits do not rebind it.");
        if(state.loaded_document){ImGui::BeginDisabled(context.busy);if(ImGui::Button("Discard loaded profile")){
            for(const auto& r:state.loaded_requests){const auto i=r.field==PowerIntegralField::lower?0u:1u;state.included[r.policy][i]=false;state.dirty[r.policy][i]=false;}
            state.loaded_requests.clear();state.loaded_document.reset();state.status="Loaded profile discarded. Select current fields to create another profile.";
        }ImGui::EndDisabled();}
        ImGui::BeginDisabled(context.busy||!context.connected||!data.current||selection.empty());
        if(ImGui::Button("Save current selected"))profile_emit(PowerIntegralAction::save_profile);ImGui::EndDisabled();ImGui::SameLine();
        ImGui::BeginDisabled(context.busy||!context.connected||!data.current);if(ImGui::Button("Load into draft"))profile_emit(PowerIntegralAction::load_profile);ImGui::EndDisabled();
        ImGui::BeginDisabled(context.busy||!context.connected||!data.current||selection.empty()||!valid);
        if(ImGui::Button("Preview selected bounds"))profile_emit(PowerIntegralAction::preview_profile);ImGui::EndDisabled();ImGui::SameLine();
        ImGui::BeginDisabled(context.busy||!context.connected||!context.unlocked||!data.current||selection.empty()||!valid||recovery||data.profile_recovery_pending);
        if(ImGui::Button("Apply selected bounds"))profile_emit(PowerIntegralAction::apply_profile);ImGui::EndDisabled();
        ImGui::BeginDisabled(context.busy||!context.unlocked||!data.profile_undo_available);if(ImGui::Button("Undo selected profile"))profile_emit(PowerIntegralAction::undo_profile);ImGui::EndDisabled();ImGui::SameLine();
        ImGui::BeginDisabled(context.busy||!context.unlocked||!data.profile_recovery_pending);if(ImGui::Button("Recover selected profile"))profile_emit(PowerIntegralAction::recover_profile);ImGui::EndDisabled();
        ImGui::TextWrapped("Save captures the current selected requests, not draft edits. Load only stages fields. Apply validates the whole range against fresh state. A new profile or individual edit replaces the previous profile Undo only when its first write launches. Grouped Undo restores only launched fields.");
        if(ImGui::CollapsingHeader("Feedback request details")) {
            ImGui::Text("Selected GPU: %s | %s",context.slot.c_str(),context.uuid.c_str());
            ImGui::TextWrapped("Session-only configured ratios, not measured power or current. 100%% equals raw 4096. Ratios round to an unsigned 16-bit value; upper must be positive and lower must not exceed upper. Restore uses captured initial state, not a verified factory default. Undo / Restore preserves unrelated current fields and refuses a replaced selected field. Saved settings apply only by an explicit action; nothing is reapplied on startup or driver restart.");
        }
    }
    ImGui::PopID();if(!inline_panel)ImGui::End();return intents;
}
}
