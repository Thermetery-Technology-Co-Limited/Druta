// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "voltage_policy_view.h"
#include <imgui.h>
#include <imgui_internal.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
using namespace druta::ui;
static unsigned checks=0;
#define CHECK(x) do{++checks;if(!(x)){std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x);std::abort();}}while(false)
static VoltagePolicyViewOutput frame(VoltagePolicyViewState& state,const VoltagePolicyData* data,bool unlocked,bool busy=false,const char* activate=nullptr,int row=-1,bool connected=true){
 ImGui::NewFrame();ImGui::SetNextWindowSize({1300,1300});ImGui::Begin("test");ImGui::PushID("voltage-policy");if(row>=0)ImGui::PushID(row);
 if(activate){auto& ctx=*ImGui::GetCurrentContext();const auto id=ImGui::GetCurrentWindow()->GetID(activate);ctx.NavActivateId=ctx.NavActivateDownId=ctx.NavActivatePressedId=id;}
 if(row>=0)ImGui::PopID();ImGui::PopID();auto out=draw_voltage_policy_controls(state,data,1,busy,unlocked,false,connected);ImGui::End();ImGui::Render();return out;
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
 CHECK(frame(state,&data,true,false,"Read voltage policies",-1,false).intents.empty());
 CHECK(frame(state,&data,true,false,"Apply",1,false).intents.empty());
 CHECK(frame(state,&data,true,false,"Load policy profile...",-1,false).intents.empty());
 out=frame(state,&data,true,false,"Recover selected policy",-1,false);CHECK(out.intents.size()==1&&out.intents[0].operation==VoltagePolicyIntent::Operation::recover);
 CHECK(frame(state,&data,true,true,"Recover selected policy",-1,false).intents.empty());
 CHECK(frame(state,&data,false,false,"Recover selected policy",-1,false).intents.empty());
 data.recovery_prior_unknown=true;data.recovery_selection=1;out=frame(state,&data,true,false,"Request captured initial",0);CHECK(out.intents.size()==1&&out.intents[0].operation==VoltagePolicyIntent::Operation::restore_initial);
 out=frame(state,&data,true,false,"Request captured initial",0,false);CHECK(out.intents.size()==1&&out.intents[0].operation==VoltagePolicyIntent::Operation::restore_initial);
 data.recovery_pending=false;out=frame(state,&data,true,false,"Undo voltage policy",-1,false);CHECK(out.intents.size()==1&&out.intents[0].operation==VoltagePolicyIntent::Operation::undo);
 state.dirty[2]=true;state.drafts[2]=false;sync_voltage_policy_view(state,&data,1);CHECK(!state.drafts[2]);sync_voltage_policy_view(state,nullptr,2);CHECK(!state.dirty[2]&&!state.drafts[2]);

 data.recovery_pending=false;data.recovery_prior_unknown=false;data.recovery_selection=0;data.identity.size=sizeof(data.identity);std::strcpy(data.identity.slot,"0000:ab:03.0");std::strcpy(data.identity.uuid,"GPU-policy-profile-view");data.identity.nvapi_ok=data.identity.nvapi_selected=1;data.identity.nvapi_handle=99;data.identity.nvapi_devid=0x2345;data.identity.nvapi_subsys=0x12345678;
 dc_target target{};target.size=sizeof(target);std::strcpy(target.slot,data.identity.slot);target.architecture=7;target.device_id=data.identity.nvapi_devid;target.subsystem_id=data.identity.nvapi_subsys;target.handle=data.identity.nvapi_handle;data.target=target;
 for(auto& r:data.rows){r.availability=VoltagePolicyAvailability::available;r.writable=true;}data.rows[0].current=false;data.rows[1].current=true;data.rows[2].current=true;
 frame(state,&data,false);state.included={true,false,false};out=frame(state,&data,false,false,"Save selected policy profile...");CHECK(out.intents.size()==1&&out.intents[0].operation==VoltagePolicyIntent::Operation::save_profile&&out.intents[0].request.selection==1);
 state.included[1]=true;CHECK(frame(state,&data,true,false,"Save selected policy profile...").intents.empty());state.included={false,true,true};out=frame(state,&data,false,false,"Save selected policy profile...");CHECK(out.intents.size()==1&&out.intents[0].request.selection==6);CHECK(frame(state,&data,false,true,"Save selected policy profile...").intents.empty());
 out=frame(state,&data,false,false,"Load policy profile...");CHECK(out.intents.size()==1&&out.intents[0].operation==VoltagePolicyIntent::Operation::load_profile);
 auto document=capture_voltage_policy_profile(data,6);document["values"]=0;CHECK(stage_voltage_policy_document(state,document,data)&&state.staged&&state.staged->selection==6&&state.staged->expected==6);CHECK(frame(state,&data,false,false,"Apply staged policy profile").intents.empty());out=frame(state,&data,true,false,"Apply staged policy profile");CHECK(out.intents.size()==1&&out.intents[0].operation==VoltagePolicyIntent::Operation::apply&&out.intents[0].request.selection==6&&out.intents[0].request.values==0&&out.intents[0].request.expected==6);
 data.rows[1].current=false;sync_voltage_policy_view(state,&data,1);CHECK(state.staged->expected==6);auto bad=document;bad["values"]=9;CHECK(!stage_voltage_policy_document(state,bad,data)&&state.staged->expected==6&&!state.status.empty());
 data.rows[0].writable=false;CHECK(stage_voltage_policy_document(state,document,data));out=frame(state,&data,true,false,"Apply staged policy profile");CHECK(out.intents.size()==1&&out.intents[0].request.selection==6); // admission Off does not block VF flags
 sync_voltage_policy_view(state,&data,2);CHECK(!state.staged&&state.status.empty()&&!state.included[1]);
 ImGui::DestroyContext();std::printf("voltage policy view: %u checks\n",checks);return 0;}
