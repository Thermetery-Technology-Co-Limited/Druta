/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "perf_diagnostics_view.h"
#include <imgui.h>
#include <imgui_internal.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
using namespace druta::ui;
static unsigned checks=0;
#define CHECK(x) do { ++checks; if(!(x)){std::fprintf(stderr,"%s:%d: %s\n",__FILE__,__LINE__,#x);std::abort();} }while(false)
static std::string frame(const PerfViewModel& model,bool* requested=nullptr){
    ImGui::NewFrame();ImGui::SetNextWindowSize({1200,900});ImGui::Begin("PERF test");ImGui::LogToBuffer();
    const bool r=draw_perf_diagnostics(model);if(requested)*requested=r;
    const std::string text=ImGui::GetCurrentContext()->LogBuffer.c_str();ImGui::LogFinish();ImGui::End();ImGui::Render();return text;
}
int main(){
    ImGui::CreateContext();auto& io=ImGui::GetIO();io.IniFilename=nullptr;io.DisplaySize={1200,900};io.DeltaTime=1.0f/60;
    unsigned char* pixels;int w,h;io.Fonts->GetTexDataAsRGBA32(&pixels,&w,&h);
    PerfViewModel m; m.groups[0].name="limits";m.groups[0].message="Read failed";m.groups[0].freshness=PerfFreshness::stale;
    auto snap=std::make_shared<dc_perf_snapshot>();snap->capacity=8;snap->mask_bytes=1;snap->mask[0]=0xa5;snap->entry_count=1;snap->entries[0].index=7;
    m.groups[0].snapshot=snap;m.groups[1].name="cf_controller";m.groups[2].name="cf_policy";
    frame(m); auto* win=ImGui::FindWindowByName("PERF test");CHECK(win);
    const auto seed=win->GetID("perf-diagnostics");const auto group=ImHashStr("limits",0,seed);
    win->StateStorage.SetInt(ImHashStr("limits",0,group),1);
    bool requested=false;const auto text=frame(m,&requested);
    CHECK(!requested && text.find("last successful read; stale")!=std::string::npos && text.find("a5")!=std::string::npos);
    ImGui::ActivateItemByID(ImHashStr("Refresh PERF diagnostics",0,seed));frame(m,&requested);CHECK(requested);
    ImGui::DestroyContext();std::printf("perf_diagnostics_view_test: %u checks passed\n",checks);
}
