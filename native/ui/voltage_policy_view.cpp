// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "voltage_policy_view.h"
#include "classic_layout.h"
#include "classic_section.h"
#include <imgui.h>
namespace druta::ui {
bool stage_voltage_policy_document(VoltagePolicyViewState& state,const ProfileJson& document,const VoltagePolicyData& data){
    try{const auto request=stage_voltage_policy_profile(document,data);state.staged=request;state.status="Policy profile staged; review and explicitly Apply";return true;}
    catch(const std::exception& e){state.status=e.what();return false;}
}
void sync_voltage_policy_view(VoltagePolicyViewState& state,const VoltagePolicyData* data,uint64_t generation){
    if(!state.initialized||state.generation!=generation){state={};state.initialized=true;state.generation=generation;}
    // Classic: the first revision published after an action carries that action's result.
    if(data)for(auto& last:state.last)if(last&&!last->outcome&&data->revision!=last->revision)last->outcome=data->message;
    if(data)for(size_t i=0;i<3;++i){if(!state.dirty[i]){state.draft_baseline[i].reset();if(data->rows[i].current)state.drafts[i]=*data->rows[i].current;}
        else if(!state.draft_baseline[i]&&data->rows[i].current)state.draft_baseline[i]=data->rows[i].current;}
}
namespace {
// Undo, Recover and Load act on both the admission gate and the VF checks, so
// classic draws them once in a shared subsection instead of under either part.
VoltagePolicyViewOutput draw_voltage_policy(VoltagePolicyViewState& state,const VoltagePolicyData* data,
    uint64_t generation,bool busy,bool unlocked,bool connected,VoltagePolicyPart part,bool shared){
    sync_voltage_policy_view(state,data,generation);VoltagePolicyViewOutput output;
    const bool all=!shared&&part==VoltagePolicyPart::all,in_part=!shared&&!all;
    // The rows this call draws; a part's subsection header already names it.
    const uint32_t shown=shared?0u:part==VoltagePolicyPart::admission?voltage_admission:part==VoltagePolicyPart::vf_policies?voltage_vf_policies:voltage_all_policies;
    ImGui::PushID("voltage-policy");if(shared)ImGui::PushID(200);else if(!all)ImGui::PushID(static_cast<int>(part));
    if(all)ImGui::TextUnformatted("Voltage requests & VF policies");
    if(shared)ImGui::TextWrapped("Undo and Recover act on this session's last voltage-request or VF policy edit, whichever subsection it was in. Apply a staged profile under its subsection.");
    if(!shared){ImGui::BeginDisabled(busy||!connected);if(ImGui::Button("Read voltage policies"))output.intents.push_back({VoltagePolicyIntent::Operation::refresh,{voltage_all_policies,0,{}}});ImGui::EndDisabled();}
    if(part==VoltagePolicyPart::all)ImGui::TextDisabled("Session-only settings");
    constexpr const char* names[]={"Allow voltage requests","Enforce VF monotonicity","Enforce VF smoothing"};
    for(size_t i=0;i<3;++i){const uint32_t bit=1u<<i;if(!(shown&bit))continue;ImGui::PushID(static_cast<int>(i));
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
    if(!in_part){ImGui::BeginDisabled(busy||!unlocked||!data||!data->undo_available||data->recovery_pending);
    if(ImGui::Button("Undo voltage policy"))output.intents.push_back({VoltagePolicyIntent::Operation::undo,{}});ImGui::EndDisabled();
    ImGui::SameLine();ImGui::BeginDisabled(busy||!unlocked||!data||!data->recovery_pending);
    if(ImGui::Button("Recover selected policy"))output.intents.push_back({VoltagePolicyIntent::Operation::recover,{}});ImGui::EndDisabled();}
    else if(data&&data->recovery_pending)ImGui::TextWrapped("A voltage policy request needs recovery. Recover it under Undo, recovery and profiles at the end of this section.");
    if(!unlocked)ImGui::TextDisabled("Unlock controls to apply, restore or recover. Reading is available while locked.");
    if(data&&!data->message.empty())ImGui::TextWrapped("%s",data->message.c_str());
    if(!shared&&data&&data->recovery_prior_unknown)ImGui::TextWrapped("The driver did not confirm the previous state. Request captured initial applies the value recorded at this session's first qualified read.");
    uint32_t selection=0;for(size_t i=0;i<3;++i)if(state.included[i])selection|=1u<<i;
    selection&=shown;
    const bool mixed=(selection&voltage_admission)&&(selection&voltage_vf_policies);
    bool save_ready=data&&data->target&&data->identity.uuid[0]&&!data->recovery_pending&&selection&&!mixed;
    if(data)for(size_t i=0;i<3;++i)if(selection&(1u<<i))save_ready=save_ready&&data->rows[i].current.has_value()&&data->rows[i].availability==VoltagePolicyAvailability::available;
    if(!shared){ImGui::BeginDisabled(busy||!connected||!save_ready);if(ImGui::Button("Save selected policy profile..."))output.intents.push_back({VoltagePolicyIntent::Operation::save_profile,{selection,0,{}}});ImGui::EndDisabled();}
    if(all)ImGui::SameLine();
    if(!in_part){ImGui::BeginDisabled(busy||!connected);if(ImGui::Button("Load policy profile..."))output.intents.push_back({VoltagePolicyIntent::Operation::load_profile,{}});ImGui::EndDisabled();}
    if(mixed)ImGui::TextDisabled("Save admission separately from monotonicity / smoothing.");
    // Classic: each subsection's Save writes all its flags, as 1.9.1 does; a profile holding
    // one VF check alone stays available here, folded away under the native-only subsection.
    if(shared&&ImGui::TreeNode("Save a selection of policies")){
        for(size_t i=0;i<3;++i){ImGui::PushID(static_cast<int>(i));ImGui::Checkbox(names[i],&state.included[i]);ImGui::PopID();}
        uint32_t chosen=0;for(size_t i=0;i<3;++i)if(state.included[i])chosen|=1u<<i;
        const bool split=(chosen&voltage_admission)&&(chosen&voltage_vf_policies);
        bool ready=data&&data->target&&data->identity.uuid[0]&&!data->recovery_pending&&chosen&&!split;
        if(data)for(size_t i=0;i<3;++i)if(chosen&(1u<<i))ready=ready&&data->rows[i].current.has_value()&&data->rows[i].availability==VoltagePolicyAvailability::available;
        ImGui::BeginDisabled(busy||!connected||!ready);if(ImGui::Button("Save selected policy profile..."))output.intents.push_back({VoltagePolicyIntent::Operation::save_profile,{chosen,0,{}}});ImGui::EndDisabled();
        if(split)ImGui::TextDisabled("Save admission separately from monotonicity / smoothing.");
        ImGui::TreePop();}
    // A part applies a staged profile only when every staged flag is its own.
    if(state.staged&&(shared||(state.staged->selection&shown))){const auto request=*state.staged;ImGui::TextUnformatted("Staged policy requests:");for(size_t i=0;i<3;++i)if(request.selection&(1u<<i))ImGui::Text("%s: %s",names[i],(request.values&(1u<<i))?"On":"Off");
        bool ready=connected&&!busy&&unlocked&&data&&!data->recovery_pending;for(size_t i=0;i<3;++i)if(request.selection&(1u<<i))ready=ready&&data&&data->rows[i].writable&&data->rows[i].current.has_value()&&data->rows[i].availability==VoltagePolicyAvailability::available;
        if(!shared&&!(request.selection&~shown)){ImGui::BeginDisabled(!ready);if(ImGui::Button("Apply staged policy profile"))output.intents.push_back({VoltagePolicyIntent::Operation::apply,request});ImGui::EndDisabled();ImGui::SameLine();}
        if(!in_part&&ImGui::Button("Discard staged policy profile"))state.staged.reset();
    }
    if(!state.status.empty())ImGui::TextWrapped("%s",state.status.c_str());
    if(!in_part&&ImGui::CollapsingHeader("Policy details")) {
        ImGui::TextWrapped("Initial is the first qualified reading in this adapter session. Readback verifies the driver's requested flags; their physical effect has not been established.");
        ImGui::TextWrapped("An On/Off reading cannot reveal another program's identical request, or a value changed and then changed back between reads.");
        ImGui::TextWrapped("Profiles save selected Boolean requests and load as drafts. Admission and VF flags use separate transactions. No startup or post-reset application occurs.");
    }
    if(!all)ImGui::PopID();
    ImGui::PopID();return output;
}
// Classic: what a part's controls last sent. A Read reads every flag, so it is each part's
// last action; the shared Undo and Recover return both lines to their readback once
// published; a profile save or load is the host's.
void remember(VoltagePolicyViewState& state,const VoltagePolicyData* data,const std::vector<VoltagePolicyIntent>& intents){
    using O=VoltagePolicyIntent::Operation;const uint64_t revision=data?data->revision:0;
    for(const auto& intent:intents){
        if(intent.operation==O::refresh||intent.operation==O::undo||intent.operation==O::recover){for(auto& l:state.last)l=VoltagePolicyLastAction{true,revision,{},0,false};continue;}
        if(intent.operation!=O::apply&&intent.operation!=O::restore_initial)continue;
        const uint32_t bit=intent.request.selection;const size_t i=bit==1u?0:bit==2u?1:bit==4u?2:3;
        VoltagePolicyLastAction action{false,revision,{},0,false};
        // One flag's request: the value it asks for (Restore: the captured initial).
        if(i<3){action.bit=bit;action.value=intent.operation==O::apply?(intent.request.values&bit)!=0:data&&data->rows[i].initial.value_or(false);
            if(intent.operation==O::restore_initial&&!(data&&data->rows[i].initial))action.bit=0;}
        state.last[(intent.request.selection&voltage_admission)?0:1]=action;}
}
// registry_panel.py _set_voltage_gate / _set_clock_vf_live: once the request's result is
// published, "Applied and verified without a GPU restart. " / "Applied live. " before the
// readback line when the flag reads back the requested value, else "Gate change failed: " /
// "Live VF policy change failed: " and the result. Empty when the line is the readback's.
std::string request_status(const VoltagePolicyViewState& state,const VoltagePolicyData* data,bool gate,const std::string& readback){
    const auto& last=state.last[gate?0:1];
    if(!last||last->read||!last->outcome||!data)return {};
    const size_t i=last->bit==1u?0:last->bit==2u?1:last->bit==4u?2:3;
    if(last->outcome->empty())return {};
    // A staged profile of several flags is native: the service's own account.
    if(i==3)return *last->outcome;
    if(data->rows[i].current==last->value&&data->rows[i].availability==VoltagePolicyAvailability::available)
        return (gate?"Applied and verified without a GPU restart. ":"Applied live. ")+readback;
    return (gate?"Gate change failed: ":"Live VF policy change failed: ")+*last->outcome;
}
// Released 1.9.1 registry_panel.py build_live_voltage: "Runtime voltage-request
// control" and "Runtime VF policy control". Explainer, one button row holding the
// read, save and checkboxes, Apply / Restore, then the status line. Each Apply or
// Restore still sends one flag's request, exactly as the per-flag buttons did; with
// both VF checks changed, a second press sends the other one.
VoltagePolicyViewOutput draw_voltage_policy_part(VoltagePolicyViewState& state,const VoltagePolicyData* data,
    uint64_t generation,bool busy,bool unlocked,bool connected,VoltagePolicyPart part){
    sync_voltage_policy_view(state,data,generation);VoltagePolicyViewOutput output;
    const bool gate=part==VoltagePolicyPart::admission;
    const uint32_t shown=gate?voltage_admission:voltage_vf_policies;
    const size_t first=gate?0:1,last=gate?1:3;
    ImGui::PushID("voltage-policy");ImGui::PushID(static_cast<int>(part));
    ImGui::PushTextWrapPos(ImGui::GetCursorPosX()+classic_px(900));
    classic_text(gate
        ?"Allow or suppress the driver's voltage-request processing. Changes take effect immediately. "
         "This switch does not set a voltage or raise a voltage ceiling."
        :"Change the driver's active VF checks without restarting the GPU. "
         "Some startup modes reject disabling a check; use the registry setting first in that case. "
         "Live changes retain the startup processing mode and apply to this driver session. "
         "Physical firmware enforcement has not been measured.");
    const auto mutate=[&](size_t i){const auto* row=data?&data->rows[i]:nullptr;
        return connected&&!busy&&unlocked&&row&&row->current.has_value()&&row->writable&&!data->recovery_pending;};
    ImGui::BeginDisabled(busy||!connected);
    if(ImGui::Button(gate?"Read voltage-request gate":"Read VF policies"))output.intents.push_back({VoltagePolicyIntent::Operation::refresh,{voltage_all_policies,0,{}}});
    ImGui::EndDisabled();
    // The subsection saves its own flags, as Save gate / VF policies profile does.
    bool save_ready=data&&data->target&&data->identity.uuid[0]&&!data->recovery_pending;
    for(size_t i=first;i<last;++i)save_ready=save_ready&&data->rows[i].current.has_value()&&data->rows[i].availability==VoltagePolicyAvailability::available;
    ImGui::SameLine();ImGui::BeginDisabled(busy||!connected||!save_ready);
    if(ImGui::Button(gate?"Save gate profile":"Save VF policies profile"))output.intents.push_back({VoltagePolicyIntent::Operation::save_profile,{shown,0,{}}});
    ImGui::EndDisabled();
    constexpr const char* checks[]={"Allow voltage requests","Enforce monotonicity","Enforce smoothing"};
    for(size_t i=first;i<last;++i){ImGui::PushID(static_cast<int>(i));ImGui::SameLine();ImGui::BeginDisabled(!mutate(i));
        if(ImGui::Checkbox(checks[i],&state.drafts[i])){if(!state.dirty[i])state.draft_baseline[i]=data->rows[i].current;state.dirty[i]=true;}
        ImGui::EndDisabled();ImGui::PopID();}
    // Apply: the gate as before; for VF the first check whose draft differs.
    std::optional<size_t> apply;
    if(gate)apply=0;
    else for(size_t i=first;i<last&&!apply;++i)if(data&&data->rows[i].current&&(state.dirty[i]||state.drafts[i]!=*data->rows[i].current))apply=i;
    size_t changed=0;if(!gate&&data)for(size_t i=first;i<last;++i)if(data->rows[i].current&&state.drafts[i]!=*data->rows[i].current)++changed;
    if(gate)ImGui::SameLine();
    ImGui::PushID(static_cast<int>(apply.value_or(first)));ImGui::BeginDisabled(!apply||!mutate(*apply));
    if(ImGui::Button(gate?"Apply gate now":"Apply policies now")&&apply){const size_t i=*apply;const uint32_t bit=1u<<i;
        const auto baseline=state.draft_baseline[i]?state.draft_baseline[i]:data->rows[i].current;
        output.intents.push_back({VoltagePolicyIntent::Operation::apply,{bit,state.drafts[i]?bit:0,baseline&&*baseline?bit:0}});
        state.dirty[i]=false;}
    ImGui::EndDisabled();ImGui::PopID();
    // Restore: the gate as before; for VF the check recovery names, else the first
    // one whose reading differs from its captured initial.
    std::optional<size_t> restore;
    for(size_t i=first;i<last&&!restore;++i)if(data&&data->recovery_prior_unknown&&data->recovery_selection==(1u<<i))restore=i;
    // As registry_panel.py enables it: only while a reading differs from its captured initial.
    size_t differs=0;
    for(size_t i=first;i<last;++i)if(data&&data->rows[i].initial&&data->rows[i].current&&*data->rows[i].initial!=*data->rows[i].current){++differs;if(!restore)restore=i;}
    const bool explicit_initial=restore&&data&&data->recovery_prior_unknown&&data->recovery_selection==(1u<<*restore);
    const auto* restore_row=restore&&data?&data->rows[*restore]:nullptr;
    ImGui::SameLine();ImGui::PushID(static_cast<int>(restore.value_or(first)));
    ImGui::BeginDisabled(busy||!unlocked||!restore_row||!restore_row->initial.has_value()||(!mutate(*restore)&&!explicit_initial));
    if(ImGui::Button(explicit_initial?"Request captured initial":gate?"Restore initial gate":"Restore initial policies")&&restore)
        output.intents.push_back({VoltagePolicyIntent::Operation::restore_initial,{1u<<*restore,0,{}}});
    ImGui::EndDisabled();
    // The captured initial ("Initial" is this session's first qualified read) is the button's tooltip.
    if(data&&ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled)){
        const auto on=[&](size_t i,const char* yes,const char* no){return data->rows[i].initial?std::string(*data->rows[i].initial?yes:no):std::string("unknown");};
        const std::string captured=gate?"Captured initial: "+on(0,"allowed","suppressed")+".":"Captured initial: monotonicity "+on(1,"on","off")+", smoothing "+on(2,"on","off")+".";
        ImGui::SetTooltip("%s",captured.c_str());}
    ImGui::PopID();
    // Status line, as reg_voltage_gate_status / reg_vf_live_status word it: a failed Read
    // says so ("Gate read unavailable: ..." / "VF policy read unavailable: ..."); a reading
    // that a later failed operation left unconfirmed is marked stale. After this part's own
    // request, its published result takes the line, as _set_voltage_gate / _set_clock_vf_live
    // set it; no service text follows the line.
    const auto stale=[&](size_t i){return data->rows[i].availability==VoltagePolicyAvailability::available?"":" (stale / unavailable)";};
    const std::string read_error=data?(gate?data->gate_read_error:data->vf_read_error):std::string();
    std::string readback;
    if(!read_error.empty())readback=(gate?"Gate read unavailable: ":"VF policy read unavailable: ")+read_error;
    else if(gate){if(data&&data->rows[0].current)readback=std::string("Driver voltage requests: ")+(*data->rows[0].current?"allowed.":"suppressed.")+stale(0);
        else readback="Read the current gate before changing it.";}
    else if(data&&data->rows[1].current&&data->rows[2].current)
        readback=std::string("Monotonicity: ")+(*data->rows[1].current?"on":"off")+", smoothing: "+(*data->rows[2].current?"on":"off")+"."
            +(data->rows[1].availability==VoltagePolicyAvailability::available&&data->rows[2].availability==VoltagePolicyAvailability::available?"":" (stale / unavailable)")
            +" Startup mode and registry settings are retained.";
    else readback="Read the policies before changing them.";
    const std::string result=request_status(state,data,gate,readback);
    classic_text(result.empty()?readback:result);
    if(changed>1)ImGui::TextDisabled("Both checks changed: each Apply sends one policy request; press it again for the other.");
    if(!gate&&differs>1)ImGui::TextDisabled("Both checks differ from their initial values: each Restore sends one policy request; press it again for the other.");
    if(data&&data->recovery_pending)ImGui::TextUnformatted("A voltage policy request needs recovery. Recover it under Undo, recovery and profiles at the end of this section.");
    if(!unlocked)ImGui::TextDisabled("Unlock controls to apply, restore or recover. Reading is available while locked.");
    if(data&&data->recovery_prior_unknown&&(data->recovery_selection&shown))ImGui::TextUnformatted("The driver did not confirm the previous state. Request captured initial applies the value recorded at this session's first qualified read.");
    // A staged profile is offered under the subsection that owns every staged flag.
    if(state.staged&&(state.staged->selection&shown)){const auto request=*state.staged;
        constexpr const char* names[]={"Allow voltage requests","Enforce VF monotonicity","Enforce VF smoothing"};
        ImGui::TextUnformatted("Staged policy requests:");for(size_t i=0;i<3;++i)if(request.selection&(1u<<i))ImGui::Text("%s: %s",names[i],(request.values&(1u<<i))?"On":"Off");
        bool ready=connected&&!busy&&unlocked&&data&&!data->recovery_pending;for(size_t i=0;i<3;++i)if(request.selection&(1u<<i))ready=ready&&data&&data->rows[i].writable&&data->rows[i].current.has_value()&&data->rows[i].availability==VoltagePolicyAvailability::available;
        if(!(request.selection&~shown)){ImGui::BeginDisabled(!ready);if(ImGui::Button("Apply staged policy profile"))output.intents.push_back({VoltagePolicyIntent::Operation::apply,request});ImGui::EndDisabled();}
    }
    if(!state.status.empty())ImGui::TextUnformatted(state.status.c_str());
    ImGui::PopTextWrapPos();
    ImGui::PopID();ImGui::PopID();remember(state,data,output.intents);return output;
}
}
VoltagePolicyViewOutput draw_voltage_policy_controls(VoltagePolicyViewState& state,const VoltagePolicyData* data,
    uint64_t generation,bool busy,bool unlocked,bool classic,bool connected,VoltagePolicyPart part){
    (void)classic;
    if(part!=VoltagePolicyPart::all)return draw_voltage_policy_part(state,data,generation,busy,unlocked,connected,part);
    return draw_voltage_policy(state,data,generation,busy,unlocked,connected,part,false);
}
VoltagePolicyViewOutput draw_voltage_policy_shared_controls(VoltagePolicyViewState& state,const VoltagePolicyData* data,
    uint64_t generation,bool busy,bool unlocked,bool connected){
    auto output=draw_voltage_policy(state,data,generation,busy,unlocked,connected,VoltagePolicyPart::all,true);
    remember(state,data,output.intents);return output;
}
}
