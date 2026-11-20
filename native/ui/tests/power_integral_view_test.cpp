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
std::vector<PowerIntegralIntent> frame(PowerIntegralViewState& view,PowerIntegralData& data,PowerIntegralViewContext& context,bool inlined=false,std::string* text=nullptr,bool classic=false) {
    ImGui::GetIO().DisplaySize={1400,1200};ImGui::NewFrame();ImGui::LogToBuffer(0);
    if(inlined){ImGui::SetNextWindowSize({1100,1100});ImGui::Begin("Inline host");}
    else ImGui::SetNextWindowSize({1100,1100});
    auto intents=draw_power_integral(view,data,context,inlined,classic);
    if(text)*text=ImGui::GetCurrentContext()->LogBuffer.c_str();ImGui::LogFinish();
    if(inlined)ImGui::End();ImGui::Render();return intents;
}
std::vector<PowerIntegralIntent> press(PowerIntegralViewState& v,PowerIntegralData& d,PowerIntegralViewContext& c,const char* label,int field=-1,bool inlined=false,bool classic=false) {
    auto* window=ImGui::FindWindowByName(inlined?"Inline host":"Power feedback ratios###druta_power_integral");
    check(window!=nullptr,"feedback surface exists");
    if(window){ImGuiID id=ImHashStr("power_integral_panel",0,window->ID);
        if(field>=0){const int policy=v.selected_policy;id=ImHashData(&policy,sizeof(policy),id);id=ImHashData(&field,sizeof(field),id);}
        ImGui::ActivateItemByID(ImHashStr(label,0,id));}
    return frame(v,d,c,inlined,nullptr,classic);
}
// Released 1.9.1 PowerIntegralPanel: Read, the policy list, Upper then Lower rows,
// the status line and the readback; the native recovery stays in a subsection.
void classic(){
    PowerIntegralViewState v;PowerIntegralData d;d.current=true;
    PowerIntegralRow row;row.policy=2;row.available=true;row.type=4;row.channel=1;row.unit=0;row.request=320500;row.lower=1;row.upper=4096;
    row.reported_lower=2048;row.reported_upper=4915;row.past_samples=3;row.next_samples=6;row.default_past_samples=2;row.default_next_samples=5;d.rows={row};
    PowerIntegralViewContext c;c.generation=4;c.slot="0000:03:00.0";c.uuid="GPU-classic";c.connected=c.unlocked=true;
    std::string text;frame(v,d,c,true,&text,true);
    check(text.find("100% is a 1x bound relative to the policy limit.")!=std::string::npos,"classic panel opens with the released paragraph");
    const auto upper=text.find("Upper adjustment (%)"),lower=text.find("Lower adjustment (%)");
    check(upper!=std::string::npos&&lower!=std::string::npos&&upper<lower,"Upper adjustment precedes Lower adjustment");
    check(text.find("Policy 2 (type 4, channel 1)")!=std::string::npos,"the policy list uses the released label");
    const auto readback=power_integral_readback_text(row);
    check(readback.find("Current lower: 0.0244140625%")!=std::string::npos&&readback.find("Reported default upper: 119.9951171875%")!=std::string::npos,"readback prints the exact released percentages");
    check(readback.find("Requested: 320.5 W  |  PMU output: --  |  Effective limit: --")!=std::string::npos,"values a failed or absent STATUS read lacks stay unknown");
    {
        // Python _format: the Read's STATUS values in the row's own unit.
        PowerIntegralRow status=row;status.pmu_output=298250;status.effective=350000;
        check(power_integral_readback_text(status).find("Requested: 320.5 W  |  PMU output: 298.25 W  |  Effective limit: 350 W")!=std::string::npos,"STATUS values print as Python's amount()");
        status.unit=1;status.pmu_output.reset();
        check(power_integral_readback_text(status).find("Requested: 320.5 A  |  PMU output: --  |  Effective limit: 350 A")!=std::string::npos,"a current row prints amps and an unknown PMU output stays --");
    }
    check(readback.find("Past samples: 3 (default 2)  |  Next samples: 6 (default 5)")!=std::string::npos,"the reported default sample counts are shown");
    {
        PowerIntegralData done=d;done.message=power_integral_read_message;PowerIntegralViewState z;std::string shown;frame(z,done,c,true,&shown,true);
        check(shown.find("Read complete.")!=std::string::npos&&shown.find(power_integral_read_message)==std::string::npos,"a completed read shows Python's status in Classic");
        PowerIntegralViewState f;f.open=true;frame(f,done,c,true,&shown);
        check(shown.find(power_integral_read_message)!=std::string::npos&&shown.find("Read complete.")==std::string::npos,"Focused keeps the service's own read message");
        done.message="Selected feedback ratio applied and exact packet readback verified";PowerIntegralViewState g;frame(g,done,c,true,&shown,true);
        check(shown.find("Selected feedback ratio applied")!=std::string::npos&&shown.find("Read complete.")==std::string::npos,"a later operation's message replaces the read status");
    }
    // PowerIntegralPanel._failed("read"): Python's sentence, stale only once a
    // read has published values; Focused keeps the service's own text.
    {
        PowerIntegralData failed;failed.current=false;failed.message="Selected adapter identity could not be verified";failed.read_error=failed.message;
        const std::string sentence="Feedback adjustment read failed: Selected adapter identity could not be verified. Read again before applying.";
        PowerIntegralViewState z;std::string shown;frame(z,failed,c,true,&shown,true);
        check(shown.find(sentence)!=std::string::npos&&shown.find("Displayed values are stale.")==std::string::npos,"a failed first read reads as Python's read failure with nothing stale");
        failed.rows=d.rows;failed.target=dc_target{};frame(z,failed,c,true,&shown,true);
        check(shown.find(sentence+" Displayed values are stale.")!=std::string::npos,"a failed read after a published one says the shown values are stale");
        PowerIntegralViewState f;f.open=true;frame(f,failed,c,true,&shown);
        check(shown.find("Feedback adjustment read failed")==std::string::npos&&shown.find("Selected adapter identity could not be verified")!=std::string::npos,"Focused keeps the service's raw read message");
        failed.message="Policy INFO or CONTROL did not read back exactly";PowerIntegralViewState y;frame(y,failed,c,true,&shown,true);
        check(shown.find("Feedback adjustment read failed")==std::string::npos&&shown.find("Policy INFO or CONTROL did not read back exactly")!=std::string::npos,"a later operation's failure is not presented as a read failure");
    }
    check(text.find("Undo previous edit")==std::string::npos,"native recovery is closed by default");
    auto intents=press(v,d,c,"Read",-1,true,true);
    check(intents.size()==1&&intents[0].action==PowerIntegralAction::refresh,"classic Read emits one refresh");
    v.percent[2][1]=125;v.dirty[2][1]=true;intents=press(v,d,c,"Apply now",1,true,true);
    check(intents.size()==1&&intents[0].action==PowerIntegralAction::apply&&intents[0].raw==5120&&intents[0].expected==4096&&intents[0].field==PowerIntegralField::upper,"classic Upper Apply now sends the same request as Focused");
    c.unlocked=false;check(press(v,d,c,"Apply now",1,true,true).empty(),"classic Apply respects the unlock gate");
    // PowerIntegralPanel._show: only available policies are listed, and a read
    // with none says so instead of asking for another read.
    {
        PowerIntegralViewState w;PowerIntegralData e;e.current=true;PowerIntegralRow off=row;off.policy=1;off.available=false;off.reason="Unit not understood";
        e.rows={off,row};c.unlocked=true;std::string shown;frame(w,e,c,true,&shown,true);
        check(w.selected_policy==2&&shown.find("Policy 2 (type 4, channel 1)")!=std::string::npos&&shown.find("(unavailable)")==std::string::npos,"classic selects the first available policy and lists no unavailable one");
        e.rows={off};PowerIntegralViewState x;frame(x,e,c,true,&shown,true);
        check(shown.find("No feedback adjustments are available on this adapter.")!=std::string::npos&&shown.find("Policy 1")==std::string::npos,"a read with no available policy says so");
        PowerIntegralData unread;PowerIntegralViewState y;frame(y,unread,c,true,&shown,true);
        check(shown.find("Read to select an available policy.")!=std::string::npos,"before a read the status asks for one");
    }
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
    unsigned char* pixels;int width,height;io.Fonts->GetTexDataAsRGBA32(&pixels,&width,&height);run();classic();ImGui::DestroyContext();
    std::printf("Power integral view: %d failures\n",failures);return failures?1:0;}
