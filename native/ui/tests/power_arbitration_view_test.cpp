/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "power_arbitration_view.h"
#include <imgui.h>
#include <imgui_internal.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
using namespace druta::ui;
static unsigned checks=0;
#define CHECK(x) do { ++checks; if(!(x)){std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x);std::abort();} }while(false)
static std::string frame(const PowerArbitrationModel& model,bool* requested=nullptr){
    ImGui::NewFrame();ImGui::SetNextWindowSize({1200,900});ImGui::Begin("Arbitration test");ImGui::LogToBuffer();
    const bool r=draw_power_arbitration(model);if(requested)*requested=r;
    const std::string text=ImGui::GetCurrentContext()->LogBuffer.c_str();ImGui::LogFinish();ImGui::End();ImGui::Render();return text;
}
int main(){
    ImGui::CreateContext();auto& io=ImGui::GetIO();io.IniFilename=nullptr;io.DisplaySize={1200,900};io.DeltaTime=1.0f/60;
    unsigned char* pixels;int w,h;io.Fonts->GetTexDataAsRGBA32(&pixels,&w,&h);
    PowerArbitrationModel initial;bool initially_requested=false;const auto initial_text=frame(initial,&initially_requested);
    CHECK(!initially_requested && initial_text.find("Not read yet")!=std::string::npos && initial_text.find("Unavailable (error kind 0)")==std::string::npos);
    PowerArbitrationModel m;m.freshness=ArbitrationFreshness::stale;m.message="STATUS missing";m.status_observed=true;
    auto report=std::make_shared<dc_arbitration_report>();report->policy_count=1;auto& row=report->policies[0];
    row.policy=7;row.type=3;row.channel=4;row.available=1;row.has_unit=1;std::strcpy(row.unit,"mW");
    row.requested=200000;row.effective=180000;row.value=190000;row.delta=-20000;row.host.available=1;std::strcpy(row.host.method,"minimum");
    row.host.output=180000;row.host.count=1;row.host.inputs[0]={12,180000};std::strcpy(row.pmu.reason,"missing");m.report=report;
    frame(m);auto* win=ImGui::FindWindowByName("Arbitration test");CHECK(win);
    const auto seed=win->GetID("power-arbitration");const int policy=7;
    const auto rowid=ImHashData(&policy,sizeof(policy),seed);
    win->StateStorage.SetInt(ImHashStr("Policy 7 | type 3 | channel 4",0,rowid),1);
    bool requested=false;const auto text=frame(m,&requested);
    CHECK(!requested && text.find("last successful read; stale")!=std::string::npos && text.find("STATUS packet")!=std::string::npos);
    CHECK(text.find("Requested 200 W")!=std::string::npos && text.find("Client 12: 180 W")!=std::string::npos && text.find("Firmware: unavailable")!=std::string::npos);
    ImGui::ActivateItemByID(ImHashStr("Refresh power arbitration",0,seed));frame(m,&requested);CHECK(requested);
    ImGui::DestroyContext();std::printf("power_arbitration_view_test: %u checks passed\n",checks);
}
