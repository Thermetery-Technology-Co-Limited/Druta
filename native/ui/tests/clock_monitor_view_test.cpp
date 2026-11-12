// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
#include "clock_monitor_view.h"
#include "clock_monitor_model.h"
#include <imgui.h>
#include <imgui_internal.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
using namespace druta::ui;
static unsigned checks=0;
#define CHECK(x) do{++checks;if(!(x)){std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x);std::abort();}}while(false)
static std::string frame(const SessionSnapshot& s,ImVec2 size,bool classic){
    ImGui::GetIO().DisplaySize=size;ImGui::NewFrame();ImGui::SetNextWindowSize(size);ImGui::Begin("clock monitor");ImGui::LogToBuffer();
    draw_clock_secondary(s,"core");draw_clock_monitor_table(s,classic);
    const std::string text=ImGui::GetCurrentContext()->LogBuffer.c_str();ImGui::LogFinish();ImGui::End();ImGui::Render();return text;
}
int main(){
    ImGui::CreateContext();auto& io=ImGui::GetIO();io.IniFilename=nullptr;io.DeltaTime=1.0f/60;
    unsigned char* pixels=nullptr;int width=0,height=0;io.Fonts->GetTexDataAsRGBA32(&pixels,&width,&height);
    SessionSnapshot s;s.data.connected=true;auto data=std::make_shared<ClockMonitorData>();data->available=true;
    auto& r=data->readings[0];r.available=true;r.key="core";r.name="GPC";r.trust="checking";r.state="checking";
    dc_clock_domain_row row{};row.domain=31;row.scale=1;row.prog_khz=4;row.meas_khz=17;
    std::snprintf(row.name,sizeof(row.name),"%s","PCIe link gen");std::snprintf(row.grade,sizeof(row.grade),"%s","likely");
    std::snprintf(row.kind,sizeof(row.kind),"%s","pcie_gen");
    data->rows.push_back(normalize_clock_domain(row,15));s.data.clock_monitor=data;
    auto text=frame(s,{1400,1000},true);
    CHECK(text.find("B: checking")!=std::string::npos);
    CHECK(text.find("PCIe link gen ?")!=std::string::npos&&text.find("gen 4")!=std::string::npos&&text.find("raw 17")!=std::string::npos);
    CHECK(text.find("not proof that B is a physical measurement")!=std::string::npos);
    r.trust="measured";r.state="judged";r.verdict="warn";r.b_mhz=1485;r.delta_mhz=-15;
    text=frame(s,{640,768},false);CHECK(text.find("measured 1485.0 MHz")!=std::string::npos);
    s.paused=true;text=frame(s,{640,768},false);
    CHECK(text.find("last B 1485.0 MHz")!=std::string::npos&&text.find("/ stale")!=std::string::npos);
    CHECK(text.find("Last reported clock rows (stale)")!=std::string::npos);
    CHECK(text.find("/ warn")==std::string::npos);
    ImGui::DestroyContext();std::printf("clock_monitor_view_test: %u checks passed\n",checks);
}
