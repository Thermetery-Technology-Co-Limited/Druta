// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "power_saving_view.h"
#include <imgui_internal.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
using namespace druta::ui;
static int checks=0;
#define CHECK(x) do{++checks;if(!(x)){std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x);std::abort();}}while(false)
namespace {
std::vector<PowerSavingIntent> frame(PowerSavingViewState& state,PowerSavingData& data,bool busy=false,bool unlocked=true,bool classic=false,uint64_t generation=2){ImGui::GetIO().DisplaySize={1700,1600};ImGui::NewFrame();ImGui::SetNextWindowSize({1600,1500});ImGui::Begin("Power-saving test");auto out=draw_power_saving_controls(state,&data,generation,busy,unlocked,classic);ImGui::End();ImGui::Render();return out;}
std::vector<PowerSavingIntent> press(PowerSavingViewState& state,PowerSavingData& data,const char* label,int family=-1,int index=-1,bool busy=false,bool unlocked=true){auto* w=ImGui::FindWindowByName("Power-saving test");auto id=ImHashStr("power-saving",0,w->ID);if(family>=0){id=ImHashData(&family,sizeof(family),id);id=ImHashData(&index,sizeof(index),id);}ImGui::ActivateItemByID(ImHashStr(label,0,id));return frame(state,data,busy,unlocked);}
}
int main(){ImGui::CreateContext();auto& io=ImGui::GetIO();io.IniFilename=nullptr;io.Fonts->AddFontDefault();io.Fonts->Build();
    CHECK(parse_power_saving_us(" 4294967295 ")==UINT32_MAX);CHECK(!parse_power_saving_us("4294967296")&&!parse_power_saving_us("0")&&!parse_power_saving_us("1.5")&&!parse_power_saving_us("-1"));
    PowerSavingData data;data.identity.size=sizeof(data.identity);std::strcpy(data.identity.uuid,"GPU-power-view");std::strcpy(data.identity.slot,"0000:01:02.0");PowerSavingRow row;row.key={PowerSavingFamily::idle_threshold,0};row.label="GR threshold";row.binding="current-route";row.capability="idle-layout";row.supported=row.writable=true;row.availability=VoltagePolicyAvailability::available;row.value=row.initial=2000;row.maximum_us=10000;row.raw_first=2000;row.raw_second=3000;row.scaled_first=2000;row.scaled_second=3000;row.scale=1000;data.rows.push_back(row);
    PowerSavingRow individual;individual.key={PowerSavingFamily::individual,0};individual.label="GR gating";individual.binding="other-route";individual.capability="individual-layout";individual.supported=individual.writable=true;individual.availability=VoltagePolicyAvailability::available;individual.value=individual.initial=0;data.rows.push_back(individual);
    PowerSavingViewState state;CHECK(frame(state,data).empty()&&state.drafts.size()==2);std::strcpy(state.drafts[0].text.data(),"4567");state.drafts[0].dirty=true;auto out=press(state,data,"Apply",2,0);CHECK(out.size()==1&&out[0].requests.size()==1&&out[0].requests[0].value==4567&&out[0].requests[0].expected==2000&&out[0].requests[0].binding=="current-route");
    CHECK(press(state,data,"Hold disable",1,0,true).empty());CHECK(press(state,data,"Hold disable",1,0,false,false).empty());out=press(state,data,"Hold disable",1,0);CHECK(out.size()==1&&out[0].requests[0].value==1);
    data.rows[1].owned=true;data.rows[1].value=1;data.rows[1].stale=true;data.rows[1].writable=false;out=press(state,data,"Release",1,0);CHECK(out.size()==1&&out[0].requests[0].value==0);data.rows[1].uncertain=true;CHECK(press(state,data,"Release",1,0).empty());
    data.owned_requests=true;out=press(state,data,"Release owned power-saving requests");CHECK(out.size()==1&&out[0].operation==PowerSavingIntent::Operation::release_owned);CHECK(press(state,data,"Release owned power-saving requests",-1,-1,true).empty());
    data.recovery_pending=true;out=press(state,data,"Recover power-saving request");CHECK(out.size()==1&&out[0].operation==PowerSavingIntent::Operation::recover);data.recovery_pending=false;
    data.rows[1].stale=data.rows[1].uncertain=false;data.rows[1].value=1;data.rows[1].writable=true;const auto profile=capture_power_saving_profile(data,{{PowerSavingFamily::individual,0},{PowerSavingFamily::idle_threshold,0}});CHECK(stage_power_saving_document(state,profile,data)&&state.staged.size()==2);CHECK(frame(state,data,false,true,true).empty());
    std::strcpy(state.drafts[0].text.data(),"7654");state.drafts[0].dirty=true;data.rows[0].value=2345;frame(state,data);CHECK(!std::strcmp(state.drafts[0].text.data(),"7654")&&state.drafts[0].expected==2000);frame(state,data,false,true,false,3);CHECK(state.staged.empty()&&!state.drafts[0].dirty&&state.drafts[0].expected==2345);
    ImGui::DestroyContext();std::printf("Power-saving view: %d checks\n",checks);return 0;}
