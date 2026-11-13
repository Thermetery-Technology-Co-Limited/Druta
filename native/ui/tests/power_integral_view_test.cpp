// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Production ImGui rendering; no backend, service calls or hardware.
#include "power_integral_view.h"
#include "imgui_internal.h"
#include <cstdio>
#include <string>
using namespace druta::ui;
namespace {
int failures=0;
void check(bool ok,const char* why){if(!ok){++failures;std::printf("FAIL: %s\n",why);}}
std::vector<PowerIntegralIntent> frame(PowerIntegralViewState& view,PowerIntegralData& data,PowerIntegralViewContext& context,bool inlined=false,std::string* text=nullptr) {
    ImGui::GetIO().DisplaySize={1400,1200};ImGui::NewFrame();ImGui::LogToBuffer(0);
    if(inlined){ImGui::SetNextWindowSize({1100,1100});ImGui::Begin("Inline host");}
    else ImGui::SetNextWindowSize({1100,1100});
    auto intents=draw_power_integral(view,data,context,inlined);
    if(text)*text=ImGui::GetCurrentContext()->LogBuffer.c_str();ImGui::LogFinish();
    if(inlined)ImGui::End();ImGui::Render();return intents;
}
std::vector<PowerIntegralIntent> press(PowerIntegralViewState& v,PowerIntegralData& d,PowerIntegralViewContext& c,const char* label,int field=-1,bool inlined=false) {
    auto* window=ImGui::FindWindowByName(inlined?"Inline host":"Power feedback ratios###druta_power_integral");
    check(window!=nullptr,"feedback surface exists");
    if(window){ImGuiID id=ImHashStr("power_integral_panel",0,window->ID);
        if(field>=0){const int policy=v.selected_policy;id=ImHashData(&policy,sizeof(policy),id);id=ImHashData(&field,sizeof(field),id);}
        ImGui::ActivateItemByID(ImHashStr(label,0,id));}
    return frame(v,d,c,inlined);
}
void run(){
    PowerIntegralViewState v;v.open=true;PowerIntegralData d;d.current=true;
    PowerIntegralRow row;row.policy=3;row.available=true;row.type=5;row.channel=7;row.request=100000;row.lower=1024;row.upper=4096;
    row.reported_lower=2048;row.reported_upper=5000;row.past_samples=4;row.next_samples=5;d.rows={row};
    PowerIntegralViewContext c;c.generation=9;c.slot="0000:7e:02.0";c.uuid="GPU-other-policy";c.connected=c.unlocked=true;
    check(frame(v,d,c).empty() && v.selected_policy==3 && v.percent[3][1]==100,"current requests seed drafts without an apply intent");
    v.percent[3][1]=125;v.dirty[3][1]=true;auto intents=press(v,d,c,"Apply now",1);
    check(intents.size()==1 && intents[0].action==PowerIntegralAction::apply && intents[0].raw==5120
        && intents[0].expected==4096 && intents[0].policy==3 && intents[0].field==PowerIntegralField::upper
        && intents[0].generation==9 && intents[0].slot==c.slot && intents[0].uuid==c.uuid,"explicit Apply emits quantized target-bound request and expected baseline");
    frame(v,d,c);v.percent[3][1]=130;v.dirty[3][1]=true;d.rows[0].upper=4500;frame(v,d,c);
    check(v.percent[3][1]==130 && v.baseline[3][1]==4096,"read refresh preserves dirty draft and its original conflict baseline");
    d.current=false;check(press(v,d,c,"Apply now",1).empty() && v.percent[3][1]==130,"stale capability blocks Apply without discarding draft");
    d.current=true;c.unlocked=false;check(press(v,d,c,"Use reported default",1).empty(),"reported default requires unlocked explicit edit");
    d.rows[0].undo[1].owned=true;d.rows[0].undo[1].can_undo=true;d.rows[0].undo[1].initial=4096;d.rows[0].undo[1].previous=4300;
    intents=press(v,d,c,"Undo previous edit",1);check(intents.size()==1 && intents[0].action==PowerIntegralAction::undo,"owned Undo remains available while edit gate locked");
    intents=press(v,d,c,"Restore captured initial",1);check(intents.size()==1 && intents[0].action==PowerIntegralAction::restore,"captured-initial Restore is a distinct explicit action");
    c.unlocked=true;d.rows[0].undo[1].recovery=true;check(press(v,d,c,"Apply now",1).empty(),"uncertain write globally blocks new edits");
    check(press(v,d,c,"Restore captured initial",1).size()==1,"uncertain request can reach worker recovery");
    c.busy=true;check(press(v,d,c,"Undo previous edit",1).empty() && press(v,d,c,"Read feedback requests").empty(),"busy worker cannot queue overlapping operations");
    c.busy=false;d.rows[0].undo[1]={};d.rows[0].available=false;d.rows[0].reason="Unit not understood";
    std::string text;frame(v,d,c,false,&text);check(text.find("Unit not understood")!=std::string::npos && press(v,d,c,"Apply now",1).empty(),"independent unavailable row explains refusal");
    d.rows[0].available=true;++c.generation;c.uuid="GPU-next";frame(v,d,c);
    check(!v.dirty[3][1] && v.baseline[3][1]==4500,"adapter identity or generation resets drafts");
    v.open=false;check(frame(v,d,c).empty(),"closed popup emits no action");
    frame(v,d,c,true);intents=press(v,d,c,"Read feedback requests",-1,true);
    check(intents.size()==1 && intents[0].action==PowerIntegralAction::refresh,"Classic/Focused inline panel works without popup open state");
    v.percent[3][0]=200;v.dirty[3][0]=true;check(press(v,d,c,"Apply now",0,true).empty(),"lower exceeding upper rejected before intent");
}
}
int main(){ImGui::CreateContext();auto& io=ImGui::GetIO();io.IniFilename=nullptr;io.DeltaTime=1.0f/60;
    unsigned char* pixels;int width,height;io.Fonts->GetTexDataAsRGBA32(&pixels,&width,&height);run();ImGui::DestroyContext();
    std::printf("Power integral view: %d failures\n",failures);return failures?1:0;}
