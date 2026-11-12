// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Headless intents/preview contracts: no backend, files, driver or GPU.
#include "scoped_profile_view.h"
#include <imgui.h>
#include <imgui_internal.h>
#include <cstdio>
#include <cstdlib>
using namespace druta::ui;
static unsigned checks=0;
#define CHECK(x) do{++checks;if(!(x)){std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x);std::abort();}}while(false)
static void complete(SessionSnapshot& s,Command::Operation operation,bool ok,const std::string& message){
    ++s.completed_actions;s.last_action_operation=operation;s.last_action_ok=ok;
    s.last_action_log_id=s.log.empty()?1:s.log.back().id+1;s.log.push_back({s.last_action_log_id,message,ok});s.busy=false;
}
static std::string frame(ScopedProfileViewState& state,const SessionSnapshot& s,bool unlocked,std::vector<Command>& commands,
    const char* activate=nullptr,ImVec2 display={1400,1400}){
    ImGui::GetIO().DisplaySize=display;ImGui::NewFrame();
    if(activate){auto* window=ImGui::FindWindowByName("Saved requests###druta_scoped_profiles");CHECK(window!=nullptr);
        auto& context=*ImGui::GetCurrentContext();context.NavActivateId=context.NavActivateDownId=context.NavActivatePressedId=window->GetID(activate);}
    ImGui::LogToBuffer();draw_scoped_profile_workspace(state,s,unlocked,commands);
    const std::string text=ImGui::GetCurrentContext()->LogBuffer.c_str();ImGui::LogFinish();ImGui::Render();return text;
}
int main(){
    ImGui::CreateContext();auto& io=ImGui::GetIO();io.IniFilename=nullptr;io.DeltaTime=1.0f/60;
    unsigned char* pixels=nullptr;int width=0,height=0;io.Fonts->GetTexDataAsRGBA32(&pixels,&width,&height);
    auto current=std::make_shared<ScopedSnapshot>();current->target={"GPU-other","0000:04:00.0"};
    ScopedCurrent rail;rail.key=ScopedKey::rail_field(0,0);rail.binding="rail:verified-current-reference";rail.unit="uV";
    rail.value=25000;rail.low=-100000;rail.high=500000;rail.fresh=true;rail.writable=false;
    ScopedCurrent policy;policy.key=ScopedKey::policy_request(7);policy.binding="policy:7:geometry-current";policy.unit="mA";
    policy.value=120000;policy.low=0;policy.high=300000;policy.fresh=true;policy.writable=true;policy.pin_value=120000;
    current->entries={rail,policy};
    SessionSnapshot s;s.generation=8;s.data.connected=true;s.data.scoped_profiles=current;
    RailControl named_rail;named_rail.id={0,0};named_rail.binding=rail.binding;named_rail.estimated_base_mv=900;s.data.rail_controls={named_rail};
    PowerPolicy named_policy;named_policy.id=7;named_policy.label="NVVDD current limit";s.data.power_policies={named_policy};
    ScopedProfileViewState state;state.open=true;std::vector<Command> commands;
    auto text=frame(state,s,false,commands);CHECK(state.initialized&&state.generation==8&&commands.empty());
    CHECK(text.find("25.000 mV request")!=std::string::npos&&text.find("~925.000 mV absolute estimate")!=std::string::npos);
    CHECK(text.find("NVVDD current limit")!=std::string::npos&&text.find("Check modes / ownership")!=std::string::npos);
    state.selection={rail.key};CHECK(can_save_scoped_profile(state,s)); // capture with gate off is read-only
    frame(state,s,false,commands,"Refresh requests");CHECK(commands.size()==1&&commands[0].operation==Command::Operation::prepare_scoped_profile);
    CHECK(commands[0].scoped_profile&&commands[0].scoped_profile->selection.empty());commands.clear();
    CHECK(state.pending_operation==Command::Operation::prepare_scoped_profile);
    complete(s,Command::Operation::prepare_scoped_profile,true,"Captured 2 understood requests");observe_scoped_profile_action(state,s);
    CHECK(!state.pending_operation&&state.status=="Captured 2 understood requests"&&state.status_ok==true);
    frame(state,s,false,commands,"Save selected current requests...");CHECK(state.save_requested&&commands.empty());state.save_requested=false;
    frame(state,s,false,commands,"Load profile...");CHECK(state.load_requested&&commands.empty());state.load_requested=false;
    auto document=capture_scoped_profile(*current,{rail.key,policy.key});document["entries"][1]["value"]=50000;
    CHECK(stage_scoped_profile(document,*current,state)&&commands.empty()&&state.plan);
    CHECK(!can_apply_scoped_profile(state,s,true)); // selected rail gate remains off
    text=frame(state,s,true,commands,"Apply loaded profile");CHECK(commands.empty());
    CHECK(text.find("50.000 mV request")!=std::string::npos&&text.find("explicit session pins")!=std::string::npos);
    // Newly captured readiness rebuilds the preview. A queued document is an
    // immutable copy; later staging cannot change an already queued intent.
    current=std::make_shared<ScopedSnapshot>(*current);current->entries[0].writable=true;s.data.scoped_profiles=current;
    frame(state,s,true,commands);CHECK(can_apply_scoped_profile(state,s,true));
    s.data.tuning.owned=true;CHECK(!can_apply_scoped_profile(state,s,true)&&can_save_scoped_profile(state,s));
    s.data.tuning.owned=false;s.data.tuning.headroom=true;CHECK(!can_apply_scoped_profile(state,s,true));
    s.data.tuning.headroom=false;s.data.tuning.recovery=true;CHECK(!can_apply_scoped_profile(state,s,true));
    s.data.tuning={};s.tuning_ownership_uncertain=true;CHECK(!can_apply_scoped_profile(state,s,true));s.tuning_ownership_uncertain=false;
    s.i2c_ownership_uncertain=true;CHECK(!can_apply_scoped_profile(state,s,true));s.i2c_ownership_uncertain=false;
    s.data.i2c.status.owned=true;CHECK(!can_apply_scoped_profile(state,s,true));s.data.i2c.status.owned=false;
    s.data.i2c.status.recovery_pending=true;CHECK(!can_apply_scoped_profile(state,s,true));s.data.i2c.status.recovery_pending=false;
    s.data.i2c.clock_owned=true;CHECK(!can_apply_scoped_profile(state,s,true));s.data.i2c.clock_owned=false;
    s.data.i2c.controller.owned=true;CHECK(!can_apply_scoped_profile(state,s,true));s.data.i2c.controller.owned=false;
    s.data.i2c.controller.recovery=true;CHECK(!can_apply_scoped_profile(state,s,true)&&can_save_scoped_profile(state,s));
    frame(state,s,true,commands,"Refresh requests");CHECK(commands.size()==1&&commands[0].operation==Command::Operation::prepare_scoped_profile);commands.clear();
    complete(s,Command::Operation::prepare_scoped_profile,true,"Captured during recovery");observe_scoped_profile_action(state,s);
    s.data.i2c.controller.recovery=false;s.data.i2c.gate=true;s.data.tuning.tripped=true;
    CHECK(can_apply_scoped_profile(state,s,true)); // enabled modes / a released tripped hold are not owners
    frame(state,s,true,commands,"Apply loaded profile");CHECK(commands.size()==1&&commands[0].operation==Command::Operation::apply_scoped_profile);
    const auto queued=commands[0].scoped_profile;CHECK(queued&&queued->document==document);
    auto other=document;other["entries"][1]["value"]=75000;CHECK(stage_scoped_profile(other,*current,state));CHECK(queued->document==document);commands.clear();
    complete(s,Command::Operation::apply_scoped_profile,false,"Scoped profile failed: readback differs; grouped Undo retained");
    // Later unrelated logs must not be mistaken for this exact action result.
    s.log.push_back({s.last_action_log_id+1,"Owned control maintenance failed",false});
    current=std::make_shared<ScopedSnapshot>(*current);s.data.scoped_profiles=current;
    text=frame(state,s,true,commands);
    CHECK(!state.pending_operation&&state.status=="Scoped profile failed: readback differs; grouped Undo retained"&&state.status_ok==false);
    CHECK(text.find("grouped Undo retained")!=std::string::npos);
    CHECK(state.status_from_operation&&state.plan); // pointer refresh kept the actual failure while rebuilding preview
    mark_scoped_profile_pending(state,s,Command::Operation::save_scoped_profile);
    complete(s,Command::Operation::save_scoped_profile,true,"Selected requests saved");
    s.log.push_back({s.last_action_log_id+1,"Action not queued: unrelated busy action",false});
    observe_scoped_profile_action(state,s);CHECK(state.status=="Selected requests saved"&&state.status_ok==true&&!state.pending_operation);
    // An accepted running profile owns its pending result. A later rejected
    // unrelated command cannot turn that accepted operation into a refusal.
    mark_scoped_profile_pending(state,s,Command::Operation::apply_scoped_profile,true);s.busy=true;
    const auto accepted_status=state.status;
    s.log.push_back({s.log.back().id+1,"Action not queued: Another operation is still running",false});
    observe_scoped_profile_action(state,s);
    CHECK(state.pending_operation==Command::Operation::apply_scoped_profile&&state.pending_accepted&&state.status==accepted_status);
    complete(s,Command::Operation::apply_scoped_profile,false,"Scoped profile failed: selected readback differs; grouped Undo retained");
    observe_scoped_profile_action(state,s);
    CHECK(!state.pending_operation&&state.status=="Scoped profile failed: selected readback differs; grouped Undo retained"&&state.status_ok==false);
    mark_scoped_profile_pending(state,s,Command::Operation::apply_scoped_profile);
    s.log.push_back({s.log.back().id+1,"Action not queued: Session is closing",false});
    observe_scoped_profile_action(state,s);CHECK(!state.pending_operation&&state.status=="Action not queued: Session is closing"&&state.status_ok==false);
    // Unrelated pre-existing rejection cannot cancel a newly queued action.
    mark_scoped_profile_pending(state,s,Command::Operation::prepare_scoped_profile);
    observe_scoped_profile_action(state,s);CHECK(state.pending_operation.has_value());
    complete(s,Command::Operation::prepare_scoped_profile,true,"Fresh capture ready");observe_scoped_profile_action(state,s);
    s.busy=true;CHECK(!can_apply_scoped_profile(state,s,true)&&!can_save_scoped_profile(state,s));s.busy=false;
    CHECK(!can_apply_scoped_profile(state,s,false));
    // A missing selected field remains listed and blocks an incomplete save.
    current=std::make_shared<ScopedSnapshot>(*current);current->entries.erase(current->entries.begin());s.data.scoped_profiles=current;
    text=frame(state,s,true,commands);CHECK(text.find("-- (not captured)")!=std::string::npos&&!can_save_scoped_profile(state,s));
    CHECK(!state.plan&&!can_apply_scoped_profile(state,s,true));
    auto malformed=document;malformed["entries"][0]["unit"]="Amps";
    CHECK(!stage_scoped_profile(malformed,*current,state)&&!state.plan);
    state.save_requested=state.load_requested=true;++s.generation;
    state.pending_operation=Command::Operation::save_scoped_profile;
    frame(state,s,true,commands,nullptr,{640,768});
    CHECK(state.selection.empty()&&!state.loaded_document&&!state.plan&&!state.save_requested&&!state.load_requested&&!state.pending_operation&&state.open);
    CHECK(commands.empty());ImGui::DestroyContext();std::printf("scoped_profile_view_test: %u checks passed\n",checks);
}
