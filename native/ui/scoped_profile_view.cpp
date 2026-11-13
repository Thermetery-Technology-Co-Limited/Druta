// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "scoped_profile_view.h"
#include <imgui.h>
#include <algorithm>
#include <iomanip>
#include <sstream>
namespace druta::ui {
namespace {
float u(float value){return value*ImGui::GetFontSize()/18.0f;}
bool ready(const SessionSnapshot& s){return s.data.connected&&!s.busy&&!s.opening;}
std::string value(int64_t raw,const std::string& unit){
    std::ostringstream text;text<<std::fixed<<std::setprecision(3)<<static_cast<double>(raw)/1000.0
        <<(unit=="uV"?" mV request":unit=="mA"?" A request":" W request");return text.str();
}
std::string label(const ScopedKey& key,const SessionSnapshot& s){
    if(key.kind==ScopedKey::Kind::board)return "Board power";
    if(key.kind==ScopedKey::Kind::policy){for(const auto& p:s.data.power_policies)if(p.id==key.policy)return p.label;
        return "Policy "+std::to_string(key.policy);}
    const char* names[]={"reliability","alt reliability","overvoltage","minimum voltage"};
    return std::string(key.rail==0?"NVVDD ":"MSVDD ")+(key.field<4?names[key.field]:"unknown field");
}
void estimate(const ScopedCurrent& row,const SessionSnapshot& s){
    if(row.key.kind!=ScopedKey::Kind::rail)return;
    for(const auto& r:s.data.rail_controls)if(r.id.rail==row.key.rail&&r.id.field==row.key.field&&r.binding==row.binding&&r.estimated_base_mv){
        ImGui::TextDisabled("~%.3f mV absolute estimate",*r.estimated_base_mv+row.value/1000.0);return;}
}
void select(ScopedProfileViewState& state,const ScopedKey& key,bool chosen){
    auto found=std::find(state.selection.begin(),state.selection.end(),key);
    if(chosen&&found==state.selection.end())state.selection.push_back(key);
    else if(!chosen&&found!=state.selection.end())state.selection.erase(found);
}
void refresh_preview(ScopedProfileViewState& state,const SessionSnapshot& s){
    if(state.observed_snapshot==s.data.scoped_profiles)return;
    state.observed_snapshot=s.data.scoped_profiles;
    if(state.loaded_document&&state.observed_snapshot){
        const bool preserve=state.status_from_operation;const auto status=state.status;const auto ok=state.status_ok;
        stage_scoped_profile(*state.loaded_document,*state.observed_snapshot,state);
        if(preserve){state.status=status;state.status_ok=ok;state.status_from_operation=true;}
    }
}
}
void sync_scoped_profile_view(ScopedProfileViewState& state,uint64_t generation){
    if(state.initialized&&state.generation==generation)return;
    const bool open=state.open;const bool changed=state.initialized;state={};state.initialized=true;state.generation=generation;state.open=open;
    if(changed)state.status="Selected GPU changed. Refresh requests to start a new profile workspace.";
}
void mark_scoped_profile_pending(ScopedProfileViewState& state,const SessionSnapshot& baseline,Command::Operation operation,bool accepted){
    sync_scoped_profile_view(state,baseline.generation);
    state.pending_operation=operation;state.pending_action_counter=baseline.completed_actions;
    state.pending_accepted=accepted;
    state.pending_log_id=baseline.log.empty()?0:baseline.log.back().id;
    state.status=operation==Command::Operation::prepare_scoped_profile?"Refreshing captured requests...":
        operation==Command::Operation::save_scoped_profile?"Saving selected requests...":"Applying loaded profile...";
    state.status_ok.reset();state.status_from_operation=true;
}
void observe_scoped_profile_action(ScopedProfileViewState& state,const SessionSnapshot& s){
    sync_scoped_profile_view(state,s.generation);if(!state.pending_operation)return;
    if(s.completed_actions>state.pending_action_counter&&s.last_action_operation==state.pending_operation){
        const auto result=std::find_if(s.log.begin(),s.log.end(),[&](const auto& row){return row.id==s.last_action_log_id;});
        if(result!=s.log.end()&&result->id>state.pending_log_id){state.status=result->message;state.status_ok=result->ok;}
        else {state.status=s.last_action_ok?"Profile action completed; result log unavailable.":"Profile action failed; result log unavailable. Check Undo.";state.status_ok=s.last_action_ok;}
        state.pending_operation.reset();state.status_from_operation=true;return;
    }
    // Dispatch refusals append a new log without advancing completed_actions.
    // Exact action-result IDs above keep later unrelated logs from replacing
    // an actual completed profile result.
    const auto rejected=std::find_if(s.log.rbegin(),s.log.rend(),[&](const auto& row){
        return row.id>state.pending_log_id&&row.message.rfind("Action not queued:",0)==0;});
    if(!state.pending_accepted&&rejected!=s.log.rend()){
        state.status=rejected->message;state.status_ok=false;state.status_from_operation=true;state.pending_operation.reset();return;
    }
    if(!s.busy&&s.completed_actions>state.pending_action_counter){
        state.status="Profile action result unavailable. Check the operation log.";state.status_ok=false;
        state.status_from_operation=true;state.pending_operation.reset();
    }
}
std::string scoped_profile_apply_blocker(const SessionSnapshot& s){
    if(s.tuning_ownership_uncertain||s.i2c_ownership_uncertain)return "Control ownership is uncertain. Resolve recovery before applying.";
    if(s.data.tuning.maintenance_needed())return "Release the V/F hold before applying.";
    const auto& i=s.data.i2c;
    if(i.status.owned||i.status.recovery_pending||i.clock_owned||i.controller.owned||i.controller.recovery)
        return "Release the I2C request and clock clamp before applying.";
    return {};
}
bool stage_scoped_profile(const ProfileJson& document,const ScopedSnapshot& snapshot,ScopedProfileViewState& state){
    // Copy first: refresh may pass the state's current document by reference.
    ProfileJson copy=document;state.plan.reset();state.loaded_document=std::move(copy);state.status_from_operation=false;state.status_ok.reset();
    try{state.plan=plan_scoped_profile(*state.loaded_document,snapshot,false);
        state.status="Profile staged. Review selected requests, then Apply explicitly.";return true;
    }catch(const std::exception& e){state.status=std::string("Profile preview unavailable: ")+e.what();return false;}
}
bool can_save_scoped_profile(const ScopedProfileViewState& state,const SessionSnapshot& snapshot){
    if(!state.initialized||state.generation!=snapshot.generation||state.pending_operation||!ready(snapshot)||!snapshot.data.scoped_profiles||state.selection.empty())return false;
    try{capture_scoped_profile(*snapshot.data.scoped_profiles,state.selection);return true;}catch(...){return false;}
}
bool can_apply_scoped_profile(const ScopedProfileViewState& state,const SessionSnapshot& snapshot,bool unlocked){
    if(!state.initialized||state.generation!=snapshot.generation||state.pending_operation||!unlocked||!ready(snapshot)||!state.loaded_document||!snapshot.data.scoped_profiles||!scoped_profile_apply_blocker(snapshot).empty())return false;
    try{plan_scoped_profile(*state.loaded_document,*snapshot.data.scoped_profiles);return true;}catch(...){return false;}
}
void draw_scoped_profile_workspace(ScopedProfileViewState& state,const SessionSnapshot& s,bool unlocked,std::vector<Command>& commands){
    observe_scoped_profile_action(state,s);if(!state.open)return;refresh_preview(state,s);
    const auto display=ImGui::GetIO().DisplaySize;
    const ImVec2 maximum(std::max(320.0f,display.x-u(16)),std::max(240.0f,display.y-u(16)));
    ImGui::SetNextWindowSizeConstraints(ImVec2(std::min(u(680),maximum.x),std::min(u(420),maximum.y)),maximum);
    ImGui::SetNextWindowSize(ImVec2(std::min(u(900),maximum.x),std::min(u(720),maximum.y)),ImGuiCond_FirstUseEver);
    if(!ImGui::Begin("Saved requests###druta_scoped_profiles",&state.open,ImGuiWindowFlags_NoSavedSettings)){ImGui::End();return;}
    ImGui::TextWrapped("Manual profiles for selected rail-limit and power-policy requests. Saved settings apply only after an explicit Apply in this session.");
    ImGui::BeginDisabled(!ready(s)||state.pending_operation.has_value());
    if(ImGui::Button("Refresh requests")){
        Command command;command.operation=Command::Operation::prepare_scoped_profile;
        command.scoped_profile=std::make_shared<ScopedProfileRequest>();commands.push_back(std::move(command));
        mark_scoped_profile_pending(state,s,Command::Operation::prepare_scoped_profile);
    }ImGui::EndDisabled();ImGui::SameLine();
    if(ImGui::Button("Load profile..."))state.load_requested=true;
    if(!state.status.empty()){
        if(state.status_ok==false)ImGui::PushStyleColor(ImGuiCol_Text,ImVec4(0.96f,0.65f,0.25f,1));
        ImGui::TextWrapped("%s",state.status.c_str());if(state.status_ok==false)ImGui::PopStyleColor();
    }
    const auto capture=s.data.scoped_profiles;
    if(capture){
        ImGui::TextWrapped("Captured target: %s / %s",capture->target.slot.c_str(),capture->target.uuid.c_str());
        ImGui::SeparatorText("Save selection");
        ImGui::TextDisabled("Choose the current requests to include.");
        if(ImGui::BeginTable("scoped-current",4,ImGuiTableFlags_SizingStretchProp|ImGuiTableFlags_RowBg)){
            ImGui::TableSetupColumn("Include",ImGuiTableColumnFlags_WidthFixed,u(55));
            ImGui::TableSetupColumn("Request",ImGuiTableColumnFlags_WidthStretch,1.2f);
            ImGui::TableSetupColumn("Captured value",ImGuiTableColumnFlags_WidthStretch,1.5f);
            ImGui::TableSetupColumn("Captured gate",ImGuiTableColumnFlags_WidthStretch,0.7f);ImGui::TableHeadersRow();
            for(size_t i=0;i<capture->entries.size();++i){const auto& row=capture->entries[i];ImGui::PushID(static_cast<int>(i));
                ImGui::TableNextRow();ImGui::TableNextColumn();bool chosen=std::find(state.selection.begin(),state.selection.end(),row.key)!=state.selection.end();
                ImGui::BeginDisabled(!row.fresh);if(ImGui::Checkbox("##include",&chosen))select(state,row.key,chosen);ImGui::EndDisabled();
                ImGui::TableNextColumn();ImGui::TextWrapped("%s",label(row.key,s).c_str());
                ImGui::TableNextColumn();ImGui::TextWrapped("%s%s",value(row.value,row.unit).c_str(),row.fresh?"":" (stale)");estimate(row,s);
                ImGui::TableNextColumn();ImGui::TextWrapped("%s",row.writable?"Ready":"Check modes / ownership");ImGui::PopID();
            }
            // A transiently missing selected rail/policy stays visible so its
            // absence cannot silently alter the saved selection, or strand it.
            for(size_t i=0;i<state.selection.size();++i){const auto key=state.selection[i];
                if(std::any_of(capture->entries.begin(),capture->entries.end(),[&](const auto& row){return row.key==key;}))continue;
                ImGui::PushID("missing-selection");ImGui::PushID(static_cast<int>(i));ImGui::TableNextRow();ImGui::TableNextColumn();
                bool keep=true;const bool removed=ImGui::Checkbox("##include",&keep)&&!keep;
                ImGui::TableNextColumn();ImGui::TextWrapped("%s",label(key,s).c_str());
                ImGui::TableNextColumn();ImGui::TextDisabled("-- (not captured)");
                ImGui::TableNextColumn();ImGui::TextUnformatted("Refresh");ImGui::PopID();ImGui::PopID();
                if(removed){state.selection.erase(state.selection.begin()+static_cast<std::ptrdiff_t>(i));break;}
            }
            ImGui::EndTable();
        }
    }else ImGui::TextWrapped("Refresh requests to inspect this adapter's available requests.");
    ImGui::BeginDisabled(!can_save_scoped_profile(state,s));
    if(ImGui::Button("Save selected current requests..."))state.save_requested=true;
    ImGui::EndDisabled();
    ImGui::TextWrapped("Refresh captures current modes and requests. Save selection chooses the contents of a new profile.");
    ImGui::SeparatorText("Loaded profile");
    if(state.plan){
        ImGui::TextWrapped("%s",state.plan->summary.c_str());
        if(ImGui::BeginTable("scoped-preview",4,ImGuiTableFlags_SizingStretchProp|ImGuiTableFlags_RowBg)){
            ImGui::TableSetupColumn("Request");ImGui::TableSetupColumn("Captured before");ImGui::TableSetupColumn("After");ImGui::TableSetupColumn("Captured gate");ImGui::TableHeadersRow();
            for(const auto& step:state.plan->steps){ImGui::TableNextRow();ImGui::TableNextColumn();ImGui::TextWrapped("%s",label(step.key,s).c_str());
                ImGui::TableNextColumn();ImGui::TextWrapped("%s",value(step.before,step.unit).c_str());
                ImGui::TableNextColumn();ImGui::TextWrapped("%s",value(step.value,step.unit).c_str());
                ImGui::TableNextColumn();ImGui::TextWrapped("%s",step.writable?"Ready":"Check modes / ownership");}
            ImGui::EndTable();
        }
        if(state.plan->coupled_power)ImGui::TextWrapped("Power writes can move coupled board/current requests. Selected policy requests become explicit session pins. Apply rechecks the whole selection and retains grouped Undo.");
    }else if(!state.loaded_document)ImGui::TextDisabled("No profile staged.");
    ImGui::TextWrapped("Rail values are stored request deltas. Absolute voltage estimates use the current adapter's captured reference.");
    ImGui::BeginDisabled(!can_apply_scoped_profile(state,s,unlocked));
    if(ImGui::Button("Apply loaded profile")){
        auto request=std::make_shared<ScopedProfileRequest>();request->document=*state.loaded_document;
        Command command;command.operation=Command::Operation::apply_scoped_profile;command.scoped_profile=std::move(request);
        commands.push_back(std::move(command));mark_scoped_profile_pending(state,s,Command::Operation::apply_scoped_profile);
    }ImGui::EndDisabled();
    if(!unlocked)ImGui::TextDisabled("Unlock controls to apply.");
    const auto blocker=scoped_profile_apply_blocker(s);if(!blocker.empty())ImGui::TextWrapped("%s",blocker.c_str());
    ImGui::End();
}
}
