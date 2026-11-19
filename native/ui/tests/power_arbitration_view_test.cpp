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
    // Classic (registry_panel.py "Power/current arbitration"): paragraph, Read arbitration and
    // arbitration_lines' text in one framed read-only box instead of per-policy headers.
    {
        CHECK(power_arbitration_text(PowerArbitrationModel{}).empty());  // Python's box is empty before a read
        // An unavailable policy prints only its reason in arbitration_lines; its observations are native detail.
        report->policy_count=2;auto& off=report->policies[1];off.policy=9;off.type=1;off.channel=2;std::strcpy(off.reason,"Policy row is not decoded");
        off.observation_count=1;std::strcpy(off.observations[0],"Raw words retained");
        row.host.initial_value=150000;row.host.has_computed_output=1;row.host.computed_output=180000;row.host.consistent=1;
        const auto t=power_arbitration_text(m,"0000:41:00.0");
        CHECK(t.find("Arbitration read unavailable: STATUS missing\nPrevious successful read follows; these values are historical.\nAdapter: 0000:41:00.0\nRead sequentially;")==0);
        CHECK(t.find("Policy 7: type 3, channel 4\n  Requested 200 W; effective 180 W; adjustment -20 W\n  Host: minimum -> 180 W; client 12: 180 W\n  Firmware: unavailable (missing)\n"
            "Policy 9: type 1, channel 2\n  Unavailable: Policy row is not decoded\n\nAdditional native readback:\n")!=std::string::npos);
        // The native extras (value, initial / computed vote, STATUS packet, unavailable observations) only follow Python's lines.
        const auto native=t.find("Additional native readback:");
        CHECK(t.find("; value")==std::string::npos&&t.find("initial")>native&&t.find("STATUS packet was not returned.")>native
            &&t.find("Policy 7: value 190 W")>native&&t.find("  Host: initial 150 W; computed 180 W")>native&&t.find("Policy 9: Raw words retained")>native);
        report->policy_count=1;
        PowerArbitrationModel current;current.freshness=ArbitrationFreshness::current;current.message="Read complete; policy packets were acquired sequentially";
        auto none=std::make_shared<dc_arbitration_report>();current.report=none;
        const auto u=power_arbitration_text(current);
        CHECK(u.find("Adapter: unknown\nRead sequentially;")==0&&u.find("No registered power/current policies.")!=std::string::npos&&u.find("Read complete")==std::string::npos&&u.find("Additional native")==std::string::npos);
        const auto draw=[&](bool* r){ImGui::NewFrame();ImGui::SetNextWindowSize({1200,900});ImGui::Begin("Arbitration classic");ImGui::LogToBuffer();
            *r=draw_power_arbitration(m,true);std::string s=ImGui::GetCurrentContext()->LogBuffer.c_str();ImGui::LogFinish();ImGui::End();ImGui::Render();return s;};
        bool r=false;const auto shown=draw(&r);
        CHECK(!r&&shown.find("Compare each request with host votes")!=std::string::npos&&shown.find("Read arbitration")!=std::string::npos&&shown.find("Policy 7 | type 3")==std::string::npos);
        auto* classic=ImGui::FindWindowByName("Arbitration classic");
        ImGui::ActivateItemByID(ImHashStr("Read arbitration",0,classic->GetID("power-arbitration")));draw(&r);CHECK(r);
    }
    ImGui::DestroyContext();std::printf("power_arbitration_view_test: %u checks passed\n",checks);
}
