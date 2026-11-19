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
    // Classic (registry_panel.py "Driver policy diagnostics"): one paragraph, one Read button
    // and the readback as text in one framed read-only box, with no group headers.
    {
        PerfViewModel empty;CHECK(perf_diagnostics_text(empty).empty()&&perf_diagnostics_raw_text(empty).empty());  // Python's box is empty before a read
        snap->flag_count=1;std::strcpy(snap->flags[0].label,"bit0");snap->flags[0].raw=1;
        snap->target_count=1;std::strcpy(snap->targets[0].field,"bits2_1");snap->targets[0].has_index=1;snap->targets[0].index=5;snap->targets[0].has_flag_raw=1;snap->targets[0].flag_raw=2;
        snap->entries[0].has_driver_label=1;std::strcpy(snap->entries[0].driver_label,"PERF_LIMIT_X");snap->entries[0].has_policy=1;snap->entries[0].role_id=3;snap->entries[0].controller_mask=0x9;
        // perf_diagnostics.py: a failed group's status is its error kind, and a failed group
        // with no previous read ends without the blank separator line.
        m.groups[0].error_kind=DC_ERR_TRANSIENT;
        m.groups[1].freshness=PerfFreshness::unavailable;m.groups[1].error_kind=DC_ERR_UNSUPPORTED;m.groups[1].message="Unsupported controller layout";
        m.groups[2].freshness=PerfFreshness::current;m.groups[2].snapshot=std::make_shared<dc_perf_snapshot>();m.groups[2].message="No active entries";
        const auto t=perf_diagnostics_text(m);
        CHECK(t.find("Performance-limit slots: transient\n  Read failed\n  Previous successful read follows; these values are historical.\n  Registered entries: 1")==0);
        CHECK(t.find("  bit0: 1 (raw flag)")!=std::string::npos&&t.find("  RmPerfCfPolicyOverrides bits2_1: policy 5, raw flag 2")!=std::string::npos);
        CHECK(t.find("  7: PERF_LIMIT_X")!=std::string::npos&&t.find("  Policy 7: role 3, controller selection 0, 3")!=std::string::npos);
        CHECK(t.find("\n\nFeedback controllers: unsupported\n  Unsupported controller layout\nFeedback policies: empty\n  Registered entries: 0")!=std::string::npos);
        CHECK(t.find(": failed")==std::string::npos&&t.find("Mask (raw)")==std::string::npos&&t.find("Raw capture")==std::string::npos);
        CHECK(perf_diagnostics_raw_text(m).find("Mask (raw): a5")!=std::string::npos&&perf_diagnostics_raw_text(m).find("INFO SHA-256")!=std::string::npos);
        const auto draw=[&](bool* r){ImGui::NewFrame();ImGui::SetNextWindowSize({1200,900});ImGui::Begin("PERF classic");ImGui::LogToBuffer();
            *r=draw_perf_diagnostics(m,true);std::string s=ImGui::GetCurrentContext()->LogBuffer.c_str();ImGui::LogFinish();ImGui::End();ImGui::Render();return s;};
        bool r=false;const auto shown=draw(&r);
        CHECK(!r&&shown.find("Read registered performance limits")!=std::string::npos&&shown.find("Read driver policies")!=std::string::npos&&shown.find("Refresh PERF diagnostics")==std::string::npos);
        CHECK(shown.find("Raw capture")!=std::string::npos);  // the native raw block stays reachable in its closed section
        auto* classic=ImGui::FindWindowByName("PERF classic");const auto cseed=classic->GetID("perf-diagnostics");
        CHECK(ImGui::GetCurrentContext()->Windows.Size>0&&classic->DC.ChildWindows.Size==1);  // the framed readback box
        ImGui::ActivateItemByID(ImHashStr("Read driver policies",0,cseed));draw(&r);CHECK(r);
    }
    ImGui::DestroyContext();std::printf("perf_diagnostics_view_test: %u checks passed\n",checks);
}
