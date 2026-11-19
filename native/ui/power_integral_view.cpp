// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "power_integral_view.h"
#include "classic_section.h"
#include "imgui.h"
#include <cfloat>
#include <cstdio>
#include <algorithm>
namespace druta::ui {
namespace {
// Python power_integral_runtime.raw_to_percent: the exact decimal of raw*100/4096,
// which a double holds exactly and %.15g prints without trailing zeros.
std::string percent_text(uint16_t raw){char b[40];std::snprintf(b,sizeof(b),"%.15g",power_integral_raw_to_percent(raw));return b;}
std::string row_label(const PowerIntegralRow& row){
    return "Policy "+std::to_string(row.policy)+" (type "+std::to_string(row.type)+", channel "+std::to_string(row.channel)+")"+(row.available?"":" (unavailable)");
}
}
std::string power_integral_readback_text(const PowerIntegralRow& row){
    const auto amount=[&](uint32_t value){char b[48];std::snprintf(b,sizeof(b),"%.3f",value/1000.0);std::string t=b;
        while(!t.empty()&&t.back()=='0')t.pop_back();if(!t.empty()&&t.back()=='.')t.pop_back();return t+(row.unit==0?" W":row.unit==1?" A":"");};
    // The default sample counts come from INFO. The PMU output and effective limit
    // come from the explicit Read's STATUS GET; without it they are unknown ("--").
    const auto known=[&](const std::optional<uint32_t>& value){return value?amount(*value):std::string("--");};
    return "Current upper: "+percent_text(row.upper)+"%  |  Reported default upper: "+percent_text(row.reported_upper)+"%  |  "
        "Current lower: "+percent_text(row.lower)+"%  |  Reported default lower: "+percent_text(row.reported_lower)+"%\n"
        "Requested: "+amount(row.request)+"  |  PMU output: "+known(row.pmu_output)+"  |  Effective limit: "+known(row.effective)+"\n"
        "Past samples: "+std::to_string(row.past_samples)+" (default "+std::to_string(row.default_past_samples)+")  |  "
        "Next samples: "+std::to_string(row.next_samples)+" (default "+std::to_string(row.default_next_samples)+")";
}
void stage_power_integral_document(PowerIntegralViewState& state,const ProfileJson& document,const PowerIntegralData& data){
    const auto requests=stage_power_integral_profile(document,data);
    state.included={};for(const auto& r:requests){const auto i=r.field==PowerIntegralField::lower?0u:1u;state.included[r.policy][i]=true;state.percent[r.policy][i]=power_integral_raw_to_percent(static_cast<uint16_t>(r.raw));state.baseline[r.policy][i]=*r.expected;state.dirty[r.policy][i]=state.initialized[r.policy][i]=true;}
    state.loaded_document=document;state.loaded_requests=requests;state.status="Profile staged only. Review selected bounds, Preview, then explicit Apply.";
}
std::vector<PowerIntegralIntent> draw_power_integral(PowerIntegralViewState& state,
    const PowerIntegralData& data,const PowerIntegralViewContext& context,bool inline_panel,bool classic) {
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
    // One bound's Apply now and Use reported default, gated as before.
    const auto edit_buttons=[&](const PowerIntegralRow& row,size_t i){
        const auto field=i==0?PowerIntegralField::lower:PowerIntegralField::upper;
        const uint16_t current=i==0?row.lower:row.upper,reported=i==0?row.reported_lower:row.reported_upper;
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
    };
    // One bound's owned Undo / Restore and its captured state.
    const auto recovery_buttons=[&](const PowerIntegralRow& row,size_t i){
        const auto field=i==0?PowerIntegralField::lower:PowerIntegralField::upper;
        const auto& owner=row.undo[i];
        ImGui::BeginDisabled(context.busy || !context.unlocked || !owner.owned || !owner.can_undo);
        if(ImGui::Button("Undo previous edit"))emit(PowerIntegralAction::undo,row.policy,field);
        ImGui::EndDisabled();ImGui::SameLine();
        ImGui::BeginDisabled(context.busy || !context.unlocked || !owner.owned);
        if(ImGui::Button("Restore captured initial"))emit(PowerIntegralAction::restore,row.policy,field);
        ImGui::EndDisabled();
        if(owner.owned)ImGui::Text("Captured initial: %.8g%% | previous edit: %.8g%%%s%s",power_integral_raw_to_percent(owner.initial),power_integral_raw_to_percent(owner.previous),owner.confirmed?" | verified":" | unconfirmed",owner.conflict?" | foreign replacement":"");
    };
    const auto profile_block=[&]{
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
    };
    if(!inline_panel)ImGui::SetNextWindowSize({790,640},ImGuiCond_FirstUseEver);
    const bool visible=inline_panel || ImGui::Begin("Power feedback ratios###druta_power_integral",&state.open,ImGuiWindowFlags_NoSavedSettings);
    ImGui::PushID("power_integral_panel");
    if(visible && classic) {
        // Released 1.9.1 PowerIntegralPanel.build: the paragraph, Read and the
        // policy list, one row per bound, the status line and a 100 px readback.
        ImGui::PushTextWrapPos(ImGui::GetCursorPosX()+classic_px(900));
        classic_text("100% is a 1x bound relative to the policy limit. This adjustment does not change the board request. "
            "Values shown are software effective limits, not physical power.");
        ImGui::PopTextWrapPos();
        ImGui::BeginDisabled(!context.connected || context.busy);
        if(ImGui::Button("Read"))emit(PowerIntegralAction::refresh);
        ImGui::EndDisabled();ImGui::SameLine();
        // PowerIntegralPanel._show lists only the available policies and keeps the
        // previous choice while it is still listed, else selects the first one.
        const PowerIntegralRow* selected=nullptr;const PowerIntegralRow* first=nullptr;
        for(const auto& row:data.rows)if(row.policy<32 && row.available){
            if(!first)first=&row;
            if(state.selected_policy==static_cast<int>(row.policy))selected=&row;
        }
        if(!selected && first){selected=first;state.selected_policy=static_cast<int>(first->policy);}
        ImGui::SetNextItemWidth(classic_px(220));
        if(ImGui::BeginCombo("##policy",selected?row_label(*selected).c_str():"")) {
            for(const auto& row:data.rows)if(row.policy<32 && row.available && ImGui::Selectable(row_label(row).c_str(),state.selected_policy==static_cast<int>(row.policy)))state.selected_policy=static_cast<int>(row.policy);
            ImGui::EndCombo();
        }
        if(selected)ImGui::PushID(static_cast<int>(selected->policy));
        for(size_t display=0;display<2;++display) {
            const size_t i=display==0?1:0; // Released panel: upper, then lower.
            ImGui::PushID(static_cast<int>(i));
            const char* label=i==1?"Upper adjustment (%)":"Lower adjustment (%)";
            ImGui::SetNextItemWidth(classic_px(130));
            if(selected){
                ImGui::BeginDisabled(context.busy);
                if(ImGui::InputDouble(label,&state.percent[selected->policy][i],0,0,"%.15g"))state.dirty[selected->policy][i]=true;
                ImGui::EndDisabled();ImGui::SameLine();edit_buttons(*selected,i);
            }else{
                char empty[2]{};ImGui::BeginDisabled();ImGui::InputText(label,empty,sizeof(empty));ImGui::SameLine();
                ImGui::Button("Apply now");ImGui::SameLine();ImGui::Button("Use reported default");ImGui::EndDisabled();
            }
            ImGui::PopID();
        }
        if(selected)ImGui::PopID();
        ImGui::PushTextWrapPos(ImGui::GetCursorPosX()+classic_px(900));
        // A completed read with no available policy says so, as _show does; with
        // one it shows Python's "Read complete." for the service's read message.
        // A failed read reads as PowerIntegralPanel._failed("read"): stale once a
        // read has published values (Python's snapshot).
        const bool read_failed=!data.current&&!data.read_error.empty()&&data.message==data.read_error;
        const std::string status=!state.status.empty()?state.status
            :data.current&&!first?std::string("No feedback adjustments are available on this adapter.")
            :data.current&&data.message==power_integral_read_message?std::string("Read complete.")
            :read_failed?"Feedback adjustment read failed: "+data.message+". Read again before applying."+(data.target?" Displayed values are stale.":"")
            :!data.message.empty()?data.message
            :data.rows.empty()?std::string("Read to select an available policy."):!data.current?std::string("Displayed values are stale. Read again before applying."):std::string("Read complete.");
        classic_text(status.c_str());
        if(recovery)ImGui::TextUnformatted("An uncertain or replaced field needs Undo / Restore recovery before further edits.");
        ImGui::PopTextWrapPos();
        const std::string readback=selected&&selected->available?power_integral_readback_text(*selected):std::string();
        ImGui::InputTextMultiline("##readback",const_cast<char*>(readback.c_str()),readback.size()+1,ImVec2(-FLT_MIN,classic_px(100)),ImGuiInputTextFlags_ReadOnly);
        // Native extras (Undo/profile scope): kept reachable in one closed subsection.
        if(ClassicSection extras{"Undo, recovery and profiles##feedback",1}) {
            if(selected){ImGui::PushID(static_cast<int>(selected->policy));
                for(size_t display=0;display<2;++display){const size_t i=display==0?1:0;ImGui::PushID(static_cast<int>(i));
                    ImGui::SeparatorText(i==0?"Lower feedback bound":"Upper feedback bound");
                    ImGui::BeginDisabled(context.busy);ImGui::Checkbox("Include in profile",&state.included[selected->policy][i]);ImGui::EndDisabled();
                    recovery_buttons(*selected,i);ImGui::PopID();}
                ImGui::PopID();}
            // The released list hides unavailable policies; their owned Undo /
            // Restore stays reachable here.
            for(const auto& row:data.rows)if(row.policy<32 && !row.available && (row.undo[0].owned||row.undo[1].owned)){
                ImGui::PushID(static_cast<int>(row.policy));ImGui::TextUnformatted(row_label(row).c_str());
                if(!row.reason.empty())ImGui::TextWrapped("Unavailable: %s",row.reason.c_str());
                for(size_t display=0;display<2;++display){const size_t i=display==0?1:0;if(!row.undo[i].owned)continue;ImGui::PushID(static_cast<int>(i));
                    ImGui::SeparatorText(i==0?"Lower feedback bound":"Upper feedback bound");recovery_buttons(row,i);ImGui::PopID();}
                ImGui::PopID();
            }
            profile_block();
        }
    } else if(visible) {
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
                ImGui::PushID(static_cast<int>(i));
                const uint16_t current=i==0?row.lower:row.upper,reported=i==0?row.reported_lower:row.reported_upper;
                ImGui::SeparatorText(i==0?"Lower feedback bound":"Upper feedback bound");
                ImGui::BeginDisabled(context.busy);ImGui::Checkbox("Include in profile",&state.included[row.policy][i]);ImGui::EndDisabled();
                ImGui::Text("Request: %.8g%% (raw %u) | reported default: %.8g%% (raw %u)",power_integral_raw_to_percent(current),static_cast<unsigned>(current),power_integral_raw_to_percent(reported),static_cast<unsigned>(reported));
                ImGui::BeginDisabled(context.busy);ImGui::SetNextItemWidth(145);
                if(ImGui::InputDouble("Adjustment (%)",&state.percent[row.policy][i],0,0,"%.8f"))state.dirty[row.policy][i]=true;
                ImGui::EndDisabled();
                edit_buttons(row,i);
                recovery_buttons(row,i);
                ImGui::PopID();
            }
            ImGui::PopID();
        }
        profile_block();
    }
    ImGui::PopID();if(!inline_panel)ImGui::End();return intents;
}
}
