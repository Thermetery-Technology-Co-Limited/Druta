// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "clock_gating_view.h"
#include <imgui.h>
#include <imgui_internal.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
using namespace druta::ui;
static unsigned checks=0;
#define CHECK(x) do{++checks;if(!(x)){std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x);std::abort();}}while(false)
static std::vector<ClockGatingIntent> frame(ClockGatingViewState& state,const ClockGatingData& data,bool unlocked,bool busy=false,const char* activate=nullptr,int family=-1){ImGui::NewFrame();ImGui::SetNextWindowSize({1600,3000});ImGui::Begin("gating-test");ImGui::PushID("clock-gating");if(family>=0)ImGui::PushID(family);if(activate){auto& c=*ImGui::GetCurrentContext();const auto id=ImGui::GetCurrentWindow()->GetID(activate);c.NavActivateId=c.NavActivateDownId=c.NavActivatePressedId=id;}if(family>=0)ImGui::PopID();ImGui::PopID();auto out=draw_clock_gating_controls(state,&data,1,busy,unlocked);ImGui::End();ImGui::Render();return out;}
int main(){ImGui::CreateContext();auto& io=ImGui::GetIO();io.IniFilename=nullptr;io.DeltaTime=1.f/60;io.DisplaySize={1800,3300};unsigned char* pixels;int w,h;io.Fonts->GetTexDataAsRGBA32(&pixels,&w,&h);ClockGatingViewState state;ClockGatingData data;data.identity.size=sizeof(data.identity);std::strcpy(data.identity.uuid,"GPU-alternate");std::strcpy(data.identity.slot,"0000:06:00.0");
 for(size_t i=0;i<4;++i){auto& s=data.families[i];s.label="Test family";s.binding="binding-"+std::to_string(i);s.availability=VoltagePolicyAvailability::available;s.support=true;s.can_disable=true;s.can_enable=i!=1;}auto& p=data.families[3];p.target_generation=p.generation=p.root_generation=p.initial_generation=1;p.width=p.root_width=16;p.allowed_generations={1,2,3};
 CHECK(frame(state,data,false).empty());auto out=frame(state,data,false,false,"Read clock gating / PCIe");CHECK(out.size()==1&&out[0].families==15);out=frame(state,data,false,false,"Read this family",2);CHECK(out.size()==1&&out[0].families==4);
 CHECK(frame(state,data,false,false,"Own DISABLE request",0).empty());out=frame(state,data,true,false,"Own DISABLE request",0);CHECK(out.size()==1&&out[0].requests[0].family==ClockGatingFamily::engine&&out[0].requests[0].mode==ClockGatingMode::disabled);CHECK(frame(state,data,true,true,"Own DISABLE request",0).empty());
 data.families[0].can_release=true;data.families[0].stale=true;data.families[0].availability=VoltagePolicyAvailability::transient;out=frame(state,data,true,false,"Release owned request",0);CHECK(out.size()==1&&out[0].operation==ClockGatingIntent::Operation::release);out=frame(state,data,true,false,"Recover / free owned object",0);CHECK(out.size()==1&&out[0].operation==ClockGatingIntent::Operation::recover);
 state.pcie_generation=2;state.pcie_dirty=true;p.target_generation=3;out=frame(state,data,true,false,"Apply PCIe generation",3);CHECK(out.size()==1&&out[0].requests[0].generation==2&&out[0].requests[0].expected_generation==1);frame(state,data,true,false,"Use current target",3);CHECK(!state.pcie_dirty&&state.pcie_baseline==3&&state.pcie_generation==3);
 state.included[3]=true;out=frame(state,data,false,false,"Save selected clock profile...");CHECK(out.size()==1&&out[0].selection.size()==1&&out[0].selection[0]==ClockGatingFamily::pcie);out=frame(state,data,false,false,"Load clock profile...");CHECK(out.size()==1&&out[0].operation==ClockGatingIntent::Operation::load_profile);
 auto document=capture_clock_gating_profile(data,{ClockGatingFamily::pcie});document["entries"][0]["generation"]=2;CHECK(stage_clock_gating_document(state,document,data)&&state.staged.size()==1);CHECK(frame(state,data,false,false,"Apply staged clock profile").empty());out=frame(state,data,true,false,"Apply staged clock profile");CHECK(out.size()==1&&out[0].requests[0].generation==2);
 data.undo_available=true;out=frame(state,data,true,false,"Undo clock / PCIe requests");CHECK(out.size()==1&&out[0].operation==ClockGatingIntent::Operation::undo);sync_clock_gating_view(state,&data,2);CHECK(state.staged.empty()&&!state.pcie_dirty&&state.generation==2);
 ImGui::DestroyContext();std::printf("Clock gating view: %u checks\n",checks);return 0;}
