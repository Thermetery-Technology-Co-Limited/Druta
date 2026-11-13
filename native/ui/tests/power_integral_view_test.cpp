// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Production ImGui rendering; no backend, service calls or hardware.
#include "power_integral_view.h"
#include "imgui_internal.h"
#include <cstdio>
#include <string>
#include <cstring>
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
    check(press(v,d,c,"Undo previous edit",1).empty()&&press(v,d,c,"Restore captured initial",1).empty(),"individual recovery requires explicit unlock");c.unlocked=true;
    intents=press(v,d,c,"Undo previous edit",1);check(intents.size()==1 && intents[0].action==PowerIntegralAction::undo,"unlocked owned Undo is reachable");
    intents=press(v,d,c,"Restore captured initial",1);check(intents.size()==1 && intents[0].action==PowerIntegralAction::restore,"captured-initial Restore is a distinct explicit action");
    c.connected=false;check(press(v,d,c,"Undo previous edit",1).size()==1&&press(v,d,c,"Restore captured initial",1).size()==1,"individual recovery remains reachable after telemetry disconnect");check(press(v,d,c,"Apply now",1).empty()&&press(v,d,c,"Read feedback requests").empty(),"disconnect still blocks ordinary edit and read");c.connected=true;
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
    v.included[3][0]=v.included[3][1]=true;v.percent[3][0]=120;v.percent[3][1]=150;v.dirty[3][1]=true;
    auto batch=press(v,d,c,"Preview selected bounds",-1,true);check(batch.size()==1&&batch[0].action==PowerIntegralAction::preview_profile&&batch[0].requests.size()==2&&batch[0].requests[0].raw==4915&&batch[0].requests[1].raw==6144,"whole selected Preview emits both bounds including range shift");
    c.unlocked=false;check(press(v,d,c,"Apply selected bounds",-1,true).empty(),"profile Apply respects unlock gate");check(press(v,d,c,"Preview selected bounds",-1,true).size()==1,"profile read-only Preview allowed while locked");
    c.unlocked=true;d.profile_undo_available=true;check(press(v,d,c,"Apply selected bounds",-1,true).size()==1,"confirmed prior profile Undo does not block next profile Apply");d.profile_undo_available=false;
    auto saved=press(v,d,c,"Save current selected",-1,true);check(saved.size()==1&&saved[0].selection.size()==2&&saved[0].action==PowerIntegralAction::save_profile,"Save selects current fields independently from drafts");
    d.profile_undo_available=d.profile_recovery_pending=true;c.connected=false;check(press(v,d,c,"Recover selected profile",-1,true).size()==1,"owned profile recovery remains reachable after telemetry disconnect");
    c.unlocked=false;check(press(v,d,c,"Recover selected profile",-1,true).empty()&&press(v,d,c,"Undo selected profile",-1,true).empty(),"grouped Undo/recovery consistently requires unlock even disconnected");
    c.connected=c.unlocked=true;d.profile_undo_available=d.profile_recovery_pending=false;d.target=dc_target{};d.target->architecture=7;d.target->device_id=1234;d.target->subsystem_id=55;std::strcpy(d.identity.uuid,c.uuid.c_str());std::strcpy(d.identity.slot,c.slot.c_str());d.rows[0].binding="qualified-integral-row";
    auto document=capture_power_integral_profile(d,{{3,PowerIntegralField::upper}});document["fields"][0]["raw"]=6000;stage_power_integral_document(v,document,d);check(v.dirty[3][1]&&v.included[3][1]&&!v.included[3][0]&&v.baseline[3][1]==4500,"Load stages only selected value with original expected baseline");d.rows[0].upper=4700;frame(v,d,c,true);check(v.baseline[3][1]==4500&&v.percent[3][1]==power_integral_raw_to_percent(6000),"refresh cannot silently rebase loaded profile draft");
    d.rows[0].upper=4500;d.rows[0].type=6;d.rows[0].channel=8;d.rows[0].unit=1;d.rows[0].binding="replacement-row-with-same-selected-value";
    check(press(v,d,c,"Apply selected bounds",-1,true).empty()&&v.loaded_requests.size()==1&&v.loaded_requests[0].type==5&&v.loaded_requests[0].channel==7&&v.loaded_requests[0].unit==0&&v.loaded_requests[0].binding=="qualified-integral-row"&&v.loaded_requests[0].expected==4500,"metadata-only refresh with identical raw baseline cannot rebind a loaded profile");
    check(press(v,d,c,"Apply now",1,true).empty(),"single-field Apply cannot bypass loaded metadata drift protection");
    auto reported=press(v,d,c,"Use reported default",1,true);check(reported.size()==1&&reported[0].action==PowerIntegralAction::reported_default&&reported[0].expected==4500&&reported[0].raw==5000,"reported default remains an explicit fresh current request independent of loaded desired value");
    v.percent[3][1]=160;v.dirty[3][1]=true;check(press(v,d,c,"Apply selected bounds",-1,true).empty(),"editing a loaded draft does not implicitly accept replacement geometry");
    const auto current_row=d.rows[0];d.rows.clear();check(press(v,d,c,"Apply selected bounds",-1,true).empty()&&v.included[3][1]&&v.loaded_requests.size()==1,"temporarily missing selected row cannot silently shrink loaded scope");d.rows={current_row};
    press(v,d,c,"Discard loaded profile",-1,true);check(!v.loaded_document&&v.loaded_requests.empty()&&!v.included[3][1]&&!v.dirty[3][1],"explicit discard releases loaded binding and restores current-draft workflow without a write intent");
    document=capture_power_integral_profile(d,{{3,PowerIntegralField::upper}});document["fields"][0]["raw"]=6000;stage_power_integral_document(v,document,d);frame(v,d,c,true);check(v.loaded_requests[0].type==6&&press(v,d,c,"Apply selected bounds",-1,true).size()==1,"explicit reload qualifies replacement metadata and stages a new typed request");
    v.percent[3][0]=30;v.dirty[3][0]=true;check(press(v,d,c,"Apply now",0,true).size()==1,"unrelated single-field edit remains independent");d.rows[0].upper=4700;frame(v,d,c,true);auto loaded_single=press(v,d,c,"Apply now",1,true);check(loaded_single.size()==1&&loaded_single[0].expected==4500&&v.loaded_requests[0].expected==4500,"unrelated single Apply does not rebase a loaded field's original comparison baseline");
    ++c.generation;frame(v,d,c,true);check(!v.loaded_document&&!v.included[3][1]&&v.status.empty(),"generation change clears staged document/selection/status");
}
}
int main(){ImGui::CreateContext();auto& io=ImGui::GetIO();io.IniFilename=nullptr;io.DeltaTime=1.0f/60;
    unsigned char* pixels;int width,height;io.Fonts->GetTexDataAsRGBA32(&pixels,&width,&height);run();ImGui::DestroyContext();
    std::printf("Power integral view: %d failures\n",failures);return failures?1:0;}
