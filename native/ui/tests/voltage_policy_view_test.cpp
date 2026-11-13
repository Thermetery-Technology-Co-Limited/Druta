// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "voltage_policy_view.h"
#include <imgui.h>
#include <imgui_internal.h>
#include <cstdio>
#include <cstdlib>
using namespace druta::ui;
static unsigned checks=0;
#define CHECK(x) do{++checks;if(!(x)){std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x);std::abort();}}while(false)
static VoltagePolicyViewOutput frame(VoltagePolicyViewState& state,const VoltagePolicyData* data,bool unlocked,bool busy=false,const char* activate=nullptr,int row=-1){
 ImGui::NewFrame();ImGui::SetNextWindowSize({1300,1300});ImGui::Begin("test");ImGui::PushID("voltage-policy");if(row>=0)ImGui::PushID(row);
 if(activate){auto& ctx=*ImGui::GetCurrentContext();const auto id=ImGui::GetCurrentWindow()->GetID(activate);ctx.NavActivateId=ctx.NavActivateDownId=ctx.NavActivatePressedId=id;}
 if(row>=0)ImGui::PopID();ImGui::PopID();auto out=draw_voltage_policy_controls(state,data,1,busy,unlocked);ImGui::End();ImGui::Render();return out;
}
int main(){ImGui::CreateContext();auto& io=ImGui::GetIO();io.IniFilename=nullptr;io.DeltaTime=1.f/60;io.DisplaySize={1400,1400};unsigned char* pixels;int w,h;io.Fonts->GetTexDataAsRGBA32(&pixels,&w,&h);
 VoltagePolicyData data;for(auto& r:data.rows){r.current=true;r.initial=false;r.writable=true;r.availability=VoltagePolicyAvailability::available;}
 VoltagePolicyViewState state;CHECK(frame(state,&data,false).intents.empty()&&state.drafts[0]);
 auto out=frame(state,&data,false,false,"Read voltage policies");CHECK(out.intents.size()==1&&out.intents[0].operation==VoltagePolicyIntent::Operation::refresh);
 CHECK(frame(state,&data,false,false,"Apply",0).intents.empty());CHECK(frame(state,&data,true,true,"Read voltage policies").intents.empty());
 state.drafts[1]=false;state.dirty[1]=true;out=frame(state,&data,true,false,"Apply",1);CHECK(out.intents.size()==1&&out.intents[0].request.selection==2&&out.intents[0].request.values==0&&out.intents[0].request.expected==2);
 state.drafts[1]=false;state.dirty[1]=true;sync_voltage_policy_view(state,&data,1);data.rows[1].current=false;
 out=frame(state,&data,true,false,"Apply",1);CHECK(out.intents.size()==1&&out.intents[0].request.expected==2); // retain original edit baseline across refresh
 out=frame(state,&data,true,false,"Restore initial",2);CHECK(out.intents.size()==1&&out.intents[0].operation==VoltagePolicyIntent::Operation::restore_initial);
 data.rows[0].writable=false;CHECK(frame(state,&data,true,false,"Apply",0).intents.empty());
 data.recovery_pending=true;data.undo_available=true;CHECK(frame(state,&data,true,false,"Apply",1).intents.empty());CHECK(frame(state,&data,true,false,"Undo voltage policy").intents.empty());
 out=frame(state,&data,true,false,"Recover selected policy");CHECK(out.intents.size()==1&&out.intents[0].operation==VoltagePolicyIntent::Operation::recover);
 data.recovery_prior_unknown=true;data.recovery_selection=1;out=frame(state,&data,true,false,"Request captured initial",0);CHECK(out.intents.size()==1&&out.intents[0].operation==VoltagePolicyIntent::Operation::restore_initial);
 state.dirty[2]=true;state.drafts[2]=false;sync_voltage_policy_view(state,&data,1);CHECK(!state.drafts[2]);sync_voltage_policy_view(state,nullptr,2);CHECK(!state.dirty[2]&&!state.drafts[2]);
 ImGui::DestroyContext();std::printf("voltage policy view: %u checks\n",checks);return 0;}
