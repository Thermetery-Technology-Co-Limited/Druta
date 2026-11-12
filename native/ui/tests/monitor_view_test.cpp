// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "monitor_view.h"
#include <imgui.h>
#include <imgui_internal.h>
#include <cstdio>
#include <cstdlib>
using namespace druta::ui;
static unsigned checks=0;
#define CHECK(x) do{++checks;if(!(x)){std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x);std::abort();}}while(false)
static std::string frame(const SessionSnapshot& s,const ShuntCorrection& correction,bool classic,ImVec2 display){
    ImGui::GetIO().DisplaySize=display;ImGui::NewFrame();ImGui::SetNextWindowSize(display);
    ImGui::Begin("monitor test",nullptr,ImGuiWindowFlags_NoSavedSettings);ImGui::LogToBuffer();
    draw_monitor_tiles(s,correction,classic);draw_monitor_details(s,classic);
    const std::string text=ImGui::GetCurrentContext()->LogBuffer.c_str();
    ImGui::LogFinish();ImGui::End();ImGui::Render();return text;
}
int main(){
    ImGui::CreateContext();auto& io=ImGui::GetIO();io.IniFilename=nullptr;io.DeltaTime=1.0f/60;
    unsigned char* pixels=nullptr;int width=0,height=0;io.Fonts->GetTexDataAsRGBA32(&pixels,&width,&height);
    SessionSnapshot s;s.data.telemetry_valid=true;auto& t=s.data.telemetry;
    t.present=DC_T_XBAR|DC_T_TEMP_HOTSPOT|DC_T_POWER_W|DC_T_PCIE_WIDTH|DC_T_EVENT_MASK;
    t.xbar=1700;t.temp_hotspot=87.5;t.power_w=100;t.pcie_width=8;t.event_mask=4;
    s.telemetry_stale_mask=DC_T_XBAR;ShuntCorrection correction;correction.valid=correction.active=correction.exact=true;correction.factor=2;
    const auto revision=s.revision;auto text=frame(s,correction,true,{1500,2200});
    CHECK(text.find("XBAR CLOCK")!=std::string::npos && text.find("1700 MHz driver (stale)")!=std::string::npos);
    CHECK(text.find("HOTSPOT")!=std::string::npos && text.find("87.5 C")!=std::string::npos);
    CHECK(text.find("200.0 W corrected")!=std::string::npos && text.find("100.0 W raw")!=std::string::npos);
    CHECK(text.find("SW power cap: ACTIVE")!=std::string::npos);
    CHECK(text.find("Link width")!=std::string::npos);
    CHECK(text.find("Memory offset")!=std::string::npos);
    t.fan_count=1;t.fans[0]={1,0,1,1234};s.fan_readings_stale=true;s.fan_rpm_stale_mask=1;
    text=frame(s,correction,true,{1500,2200});
    CHECK(text.find("1234 RPM (stale)")!=std::string::npos);
    CHECK(text.find("0 % (stale)")==std::string::npos); // current duty zero is independent of stale RPM
    text=frame(s,correction,false,{640,768});
    CHECK(text.find("XBAR CLOCK")!=std::string::npos && text.find("Throttle / clocks-event reasons")!=std::string::npos);
    CHECK(s.revision==revision && t.power_w==100 && t.event_mask==4);
    ImGui::DestroyContext();std::printf("monitor_view_test: %u checks passed\n",checks);
}
